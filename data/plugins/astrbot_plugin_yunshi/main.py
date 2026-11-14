import asyncio
import os
import ssl
import tempfile
import time
import urllib.request
from io import BytesIO
from urllib.parse import quote, urlparse

from PIL import Image as PILImage

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.config.astrbot_config import AstrBotConfig

try:
    import aiohttp
except ImportError:
    aiohttp = None

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MIN_IMAGE_BYTES = 15 * 1024
MIN_IMAGE_DIMENSION = 300
REQUEST_TIMEOUT_SECONDS = 20


@register(
    "astrbot_plugin_yunshi", "Abyss-Seeker", "发送'运势'获取随机二次元运势图", "1.0.1"
)
class YunshiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.api_url = "https://www.hhlqilongzhu.cn/api/tu_yunshi.php"
        self.session = None
        self.rate_limits = {}
        self.cooldown = max(0, int(config.get("cooldown", 0) or 0))
        self.fallback_warning_logged = False
        data_dir = StarTools.get_data_dir("astrbot_plugin_yunshi")
        self.cache_path = data_dir / "last_good.jpg"

    async def initialize(self):
        """Initialize the HTTP session used to fetch fortune images."""
        if aiohttp is None:
            self.session = None
            logger.warning("aiohttp is unavailable; using urllib fallback")
            return

        self.session = aiohttp.ClientSession(
            trust_env=True,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
        )
        logger.info("Fortune plugin initialized")

    async def terminate(self):
        """Close the HTTP session when the plugin is unloaded."""
        if self.session:
            await self.session.close()
            self.session = None
            logger.info("Fortune plugin HTTP session closed")

    async def _download_image(self) -> bytes:
        """Download and fully validate a fortune image.

        The upstream certificate is expired. A verified direct request is always
        attempted first. For that legacy host only, several cache-busted GET
        requests without TLS verification are retried, followed by a verified
        HTTPS image proxy. The unverified path is limited to this public image
        endpoint because the upstream certificate cannot currently be validated.

        A successfully decoded image is cached. If every live request returns a
        truncated, empty, or invalid image, the last known-good cached image is
        returned instead of sending a broken image.

        Returns:
            Raw bytes for a supported, complete image.

        Raises:
            RuntimeError: If every live request and the cached image fail.
            ValueError: If a response is not a valid or reasonably sized image.
        """
        request_attempts = [(self.api_url, "direct", True)]
        parsed_url = urlparse(self.api_url)
        if parsed_url.hostname == "www.hhlqilongzhu.cn":
            separator = "&" if parsed_url.query else "?"
            for attempt in range(1, 4):
                retry_url = f"{self.api_url}{separator}t={time.time_ns()}"
                request_attempts.append(
                    (retry_url, f"direct without TLS verification #{attempt}", False)
                )

            query = f"{parsed_url.query}&" if parsed_url.query else ""
            upstream_url = (
                f"http://www.hhlqilongzhu.cn{parsed_url.path}?{query}t={time.time_ns()}"
            )
            proxy_url = (
                "https://images.weserv.nl/?url="
                + quote(upstream_url, safe="")
                + "&n=-1"
            )
            request_attempts.append((proxy_url, "HTTPS image proxy", True))

        errors = []
        for index, (url, label, verify_tls) in enumerate(request_attempts):
            if index:
                await asyncio.sleep(0.4)
            try:
                if self.session is not None:
                    request_kwargs = {}
                    if not verify_tls:
                        request_kwargs["ssl"] = False
                        request_kwargs["headers"] = {"Connection": "close"}
                    async with self.session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
                        **request_kwargs,
                    ) as response:
                        if response.status != 200:
                            raise RuntimeError(f"HTTP {response.status}")
                        content_type = (
                            response.headers.get("Content-Type", "")
                            .split(";", 1)[0]
                            .strip()
                            .lower()
                        )
                        if content_type and not content_type.startswith("image/"):
                            raise ValueError(f"unexpected Content-Type: {content_type}")
                        image_data = await response.read()
                else:

                    def _fetch() -> bytes:
                        """Fetch the image synchronously with urllib.

                        Returns:
                            Raw response bytes.

                        Raises:
                            ValueError: If the response is not an image or is too
                                large.
                        """
                        context = None
                        headers = {
                            "Accept": "image/*",
                            "User-Agent": "AstrBot-Yunshi/1.0.1",
                        }
                        if not verify_tls:
                            context = ssl.create_default_context()
                            context.check_hostname = False
                            context.verify_mode = ssl.CERT_NONE
                            headers["Connection"] = "close"
                        request = urllib.request.Request(url, headers=headers)
                        with urllib.request.urlopen(
                            request,
                            timeout=REQUEST_TIMEOUT_SECONDS,
                            context=context,
                        ) as response:
                            content_type = response.headers.get_content_type()
                            if not content_type.startswith("image/"):
                                raise ValueError(
                                    f"unexpected Content-Type: {content_type}"
                                )
                            return response.read()

                    image_data = await asyncio.to_thread(_fetch)

                if len(image_data) > MAX_IMAGE_BYTES:
                    raise ValueError("image response exceeds 10 MiB")
                if len(image_data) < MIN_IMAGE_BYTES:
                    raise ValueError(
                        f"image response is suspiciously small: {len(image_data)} bytes"
                    )
                if not (
                    image_data.startswith(b"\xff\xd8\xff")
                    or image_data.startswith(b"\x89PNG\r\n\x1a\n")
                    or image_data.startswith((b"GIF87a", b"GIF89a"))
                    or (
                        len(image_data) >= 12
                        and image_data[:4] == b"RIFF"
                        and image_data[8:12] == b"WEBP"
                    )
                ):
                    raise ValueError("response is not a supported image")
                if image_data.startswith(b"\xff\xd8\xff") and not (
                    image_data.rstrip(b"\x00").endswith(b"\xff\xd9")
                ):
                    raise ValueError(
                        f"incomplete JPEG response: {len(image_data)} bytes"
                    )

                try:
                    with PILImage.open(BytesIO(image_data)) as decoded_image:
                        width, height = decoded_image.size
                        decoded_image.load()
                except Exception as exc:
                    raise ValueError(f"invalid image response: {exc}") from exc
                if width < MIN_IMAGE_DIMENSION or height < MIN_IMAGE_DIMENSION:
                    raise ValueError(
                        f"image dimensions are too small: {width}x{height}"
                    )

                try:
                    cache_tmp = self.cache_path.with_name(
                        f".last_good.{time.time_ns()}.tmp"
                    )
                    cache_tmp.write_bytes(image_data)
                    os.replace(cache_tmp, self.cache_path)
                except Exception as exc:
                    logger.warning("Failed to cache fortune image: %s", exc)

                if label != "direct" and not self.fallback_warning_logged:
                    if verify_tls:
                        logger.warning(
                            "Direct fortune API request failed; using %s. "
                            "Last error: %s",
                            label,
                            errors[-1],
                        )
                    else:
                        logger.warning(
                            "Verified direct request failed; used an unverified GET "
                            "for the legacy image endpoint. Last error: %s",
                            errors[-1],
                        )
                    self.fallback_warning_logged = True
                return image_data
            except Exception as exc:
                error = f"{label}: {type(exc).__name__}: {exc}"
                errors.append(error)
                logger.warning("Fortune image request failed (%s)", error)

        try:
            cached_data = self.cache_path.read_bytes()
            if len(cached_data) < MIN_IMAGE_BYTES:
                raise ValueError("cached image is suspiciously small")
            if not cached_data.rstrip(b"\x00").endswith(b"\xff\xd9"):
                raise ValueError("cached image is incomplete")
            with PILImage.open(BytesIO(cached_data)) as decoded_image:
                width, height = decoded_image.size
                decoded_image.load()
            if width < MIN_IMAGE_DIMENSION or height < MIN_IMAGE_DIMENSION:
                raise ValueError("cached image dimensions are too small")
            logger.warning(
                "All live fortune requests failed; using cached image %s",
                self.cache_path,
            )
            return cached_data
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.error("Cached fortune image is invalid: %s", exc)

        raise RuntimeError("; ".join(errors))

    @filter.regex(r".*[运運運][势勢勢].*")
    async def handle_yunshi(self, event: AstrMessageEvent):
        """Handle a fortune image request."""
        user_id = event.get_sender_id()
        user_nick = event.get_sender_name()
        current_time = asyncio.get_running_loop().time()

        last_request = self.rate_limits.get(user_id, 0)
        if current_time - last_request < self.cooldown:
            logger.info("User %s requested fortune too frequently", user_id)
            yield event.plain_result(f"{user_nick}亲，别想逆天改命哦~等会再来问吧")
            return

        self.rate_limits[user_id] = current_time
        temp_file = None
        try:
            user_name = event.get_sender_name()
            logger.info("Received fortune request from %s", user_name)

            image_data = await self._download_image()
            logger.info("Fetched fortune image (%s bytes)", len(image_data))

            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp.write(image_data)
                temp_file = tmp.name
            logger.info("Saved fortune image to %s", temp_file)

            image_component = Image.fromFileSystem(temp_file)
            yield event.chain_result([image_component])
        except Exception as exc:
            self.rate_limits.pop(user_id, None)
            logger.error("Failed to fetch fortune image: %s", exc, exc_info=True)
            yield event.plain_result("运势服务暂时不可用，请稍后再试")
        finally:
            if temp_file and os.path.exists(temp_file):
                try:
                    os.unlink(temp_file)
                    logger.info("Deleted temporary fortune image: %s", temp_file)
                except Exception as exc:
                    logger.error("Failed to delete temporary image: %s", exc)
