import asyncio
import html
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from re import Match
from typing import Any, ClassVar

from bs4 import BeautifulSoup

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from ..data import DynamicContent
from ..download import Downloader
from ..exception import DownloadException
from ..utils import exec_ffmpeg_cmd, generate_file_name, safe_unlink
from .base import BaseParser, ParseException, Platform, handle


class PixivParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="pixiv", display_name="Pixiv")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update(
            {
                "accept": "application/json, text/plain, */*",
                "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
                "referer": "https://www.pixiv.net/",
                "origin": "https://www.pixiv.net",
                "x-requested-with": "XMLHttpRequest",
            }
        )
        if pixiv_ck := str(config.get("pixiv_ck") or "").strip():
            self.headers["cookie"] = pixiv_ck

    @handle(
        "pixiv.net",
        r"pixiv\.net/(?:en/)?artworks/(?P<illust_id>\d+)",
    )
    async def _parse_artwork(self, searched: Match[str]):
        illust_id = searched.group("illust_id")
        return await self.parse_artwork(illust_id)

    async def parse_artwork(self, illust_id: str):
        detail = await self._get_json(
            f"https://www.pixiv.net/ajax/illust/{illust_id}?lang=zh"
        )
        body = self._require_body(detail, "作品信息为空")

        title = body.get("title") or body.get("illustTitle")
        text = self._clean_html(body.get("description") or body.get("illustComment"))
        author = self.create_author(
            body.get("userName") or "unknown",
            self._find_profile_image(body),
            ext_headers=self.headers,
        )
        timestamp = self._parse_timestamp(
            body.get("createDate") or body.get("uploadDate")
        )
        url = f"https://www.pixiv.net/artworks/{illust_id}"

        illust_type = int(body.get("illustType") or 0)
        if illust_type == 2:
            ugoira_meta = await self._get_ugoira_meta(illust_id)
            contents = [
                DynamicContent(
                    asyncio.create_task(
                        self._download_ugoira_gif(illust_id, ugoira_meta)
                    )
                )
            ]
            extra = {"info": "Pixiv 动图已转换为 GIF"}
        else:
            image_urls = await self._get_image_urls(illust_id)
            if not image_urls:
                self._raise_login_required(body)
            contents = self.create_image_contents(image_urls, ext_headers=self.headers)
            extra = {}

        return self.result(
            title=title,
            text=text,
            author=author,
            contents=contents,
            timestamp=timestamp,
            url=url,
            extra=extra,
        )

    async def _get_image_urls(self, illust_id: str) -> list[str]:
        data = await self._get_json(
            f"https://www.pixiv.net/ajax/illust/{illust_id}/pages?lang=zh"
        )
        pages = data.get("body")
        if not isinstance(pages, list):
            return []

        preferred = str(self.config.get("pixiv_image_quality") or "original")
        fallback_order = (preferred, "original", "regular", "small")
        urls: list[str] = []
        for page in pages:
            page_urls = page.get("urls") if isinstance(page, dict) else None
            if not isinstance(page_urls, dict):
                continue
            for key in fallback_order:
                url = page_urls.get(key)
                if isinstance(url, str) and url.startswith("http"):
                    urls.append(url)
                    break
        return urls

    async def _get_ugoira_meta(self, illust_id: str) -> dict[str, Any]:
        try:
            meta = await self._get_json(
                f"https://www.pixiv.net/ajax/illust/{illust_id}/ugoira_meta?lang=zh"
            )
        except ParseException as e:
            raise ParseException("Pixiv 动图地址为空，请配置 pixiv_ck 后重试") from e
        body = meta.get("body")
        if meta.get("error") or not isinstance(body, dict):
            raise ParseException("Pixiv 动图地址为空，请配置 pixiv_ck 后重试")
        return body

    async def _download_ugoira_gif(
        self,
        illust_id: str,
        body: dict[str, Any],
    ) -> Path:
        gif_path = self.downloader.cache_dir / f"pixiv_{illust_id}_ugoira.gif"
        if gif_path.exists():
            return gif_path

        zip_url = body.get("originalSrc") or body.get("src")
        frames = body.get("frames")
        if not isinstance(zip_url, str) or not zip_url.startswith("http"):
            raise ParseException("Pixiv 动图地址为空，请配置 pixiv_ck 后重试")
        if not isinstance(frames, list) or not frames:
            raise ParseException("Pixiv 动图帧信息为空")

        zip_path = await self._download_ugoira_zip(illust_id, zip_url)

        work_dir = self.downloader.cache_dir / f"pixiv_{illust_id}_ugoira_frames"
        concat_path = self.downloader.cache_dir / f"pixiv_{illust_id}_ugoira.txt"
        await asyncio.to_thread(self._extract_ugoira_zip, zip_path, work_dir)

        try:
            await asyncio.to_thread(
                self._write_concat_file, concat_path, work_dir, frames
            )
            await exec_ffmpeg_cmd(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_path),
                    "-vf",
                    "scale=iw:-1:flags=lanczos",
                    "-loop",
                    "0",
                    str(gif_path),
                ]
            )
        except Exception as e:
            await safe_unlink(gif_path)
            raise DownloadException("Pixiv 动图转换失败") from e
        finally:
            await safe_unlink(concat_path)
            await asyncio.to_thread(shutil.rmtree, work_dir, True)

        logger.info(f"Pixiv ugoira 转 GIF 成功: {gif_path}")
        return gif_path

    async def _download_ugoira_zip(self, illust_id: str, zip_url: str) -> Path:
        zip_name = generate_file_name(zip_url, ".zip")
        zip_path = self.downloader.cache_dir / zip_name
        headers = {
            **self.headers,
            "referer": f"https://www.pixiv.net/artworks/{illust_id}",
        }

        last_error: Exception | None = None
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(1.5 * attempt)
            try:
                path = await self.downloader.download_file(
                    zip_url,
                    file_name=zip_name,
                    ext_headers=headers,
                    proxy=self.proxy,
                )
                await asyncio.to_thread(self._check_zip, path)
                return path
            except Exception as e:
                last_error = e
                await safe_unlink(zip_path)
                await safe_unlink(zip_path.with_name(zip_path.name + ".part"))
                logger.warning(
                    f"Pixiv ugoira zip 下载/校验失败，准备重试: illust_id={illust_id}, attempt={attempt + 1}"
                )

        raise DownloadException("Pixiv 动图下载失败") from last_error

    async def _get_json(self, url: str) -> dict[str, Any]:
        async def request_json(headers: dict[str, str]) -> dict[str, Any]:
            async with self.client.get(
                url,
                headers=headers,
                allow_redirects=True,
                proxy=self.proxy,
            ) as resp:
                if resp.status >= 400:
                    raise ParseException(f"Pixiv API {resp.status} {resp.reason}")
                try:
                    return await resp.json(content_type=None)
                except Exception as e:
                    text = await resp.text()
                    raise ParseException(f"Pixiv 响应不是 JSON: {text[:80]}") from e

        try:
            return await request_json(self.headers)
        except ParseException as exc:
            if "cookie" not in self.headers or not any(
                marker in str(exc) for marker in (" 401 ", " 403 ")
            ):
                raise
            logger.warning(
                "[pixiv] Configured cookie was rejected; retrying anonymously. "
                "Refresh pixiv_ck if the artwork requires login."
            )
            anonymous_headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() != "cookie"
            }
            return await request_json(anonymous_headers)

    @staticmethod
    def _require_body(data: dict[str, Any], message: str) -> dict[str, Any]:
        if data.get("error"):
            raise ParseException(data.get("message") or message)
        body = data.get("body")
        if not isinstance(body, dict):
            raise ParseException(message)
        return body

    @staticmethod
    def _clean_html(raw: str | None) -> str | None:
        if not raw:
            return None
        text = html.unescape(raw)
        text = BeautifulSoup(text, "html.parser").get_text("\n", strip=True)
        return text or None

    @staticmethod
    def _parse_timestamp(value: str | None) -> int | None:
        if not value:
            return None
        try:
            return int(datetime.fromisoformat(value).timestamp())
        except Exception:
            return None

    @staticmethod
    def _find_profile_image(body: dict[str, Any]) -> str | None:
        user_illusts = body.get("userIllusts")
        if not isinstance(user_illusts, dict):
            return None
        for item in user_illusts.values():
            if isinstance(item, dict):
                url = item.get("profileImageUrl")
                if isinstance(url, str) and url.startswith("http"):
                    return url
        return None

    @staticmethod
    def _extract_ugoira_zip(zip_path: Path, work_dir: Path) -> None:
        if work_dir.exists():
            shutil.rmtree(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.infolist():
                member_path = Path(member.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    continue
                zf.extract(member, work_dir)

    @staticmethod
    def _check_zip(zip_path: Path) -> None:
        with zipfile.ZipFile(zip_path) as zf:
            bad_file = zf.testzip()
        if bad_file is not None:
            raise zipfile.BadZipFile(f"bad file in zip: {bad_file}")

    @staticmethod
    def _write_concat_file(
        concat_path: Path,
        work_dir: Path,
        frames: list[dict[str, Any]],
    ) -> None:
        lines: list[str] = []
        last_file: Path | None = None
        for frame in frames:
            file_name = frame.get("file")
            if not isinstance(file_name, str):
                continue
            frame_path = (work_dir / file_name).resolve()
            delay = max(float(frame.get("delay") or 60) / 1000.0, 0.01)
            lines.append(f"file '{PixivParser._concat_quote(frame_path)}'")
            lines.append(f"duration {delay:.3f}")
            last_file = frame_path

        if not lines or last_file is None:
            raise ParseException("Pixiv 动图帧列表为空")
        lines.append(f"file '{PixivParser._concat_quote(last_file)}'")
        concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _concat_quote(path: Path) -> str:
        return str(path).replace("'", "'\\''")

    @staticmethod
    def _raise_login_required(body: dict[str, Any]) -> None:
        x_restrict = int(body.get("xRestrict") or 0)
        if x_restrict:
            raise ParseException("Pixiv R-18/R-18G 作品需要配置 pixiv_ck 后才能解析")
        raise ParseException("Pixiv 图片地址为空，可能需要配置 pixiv_ck 后重试")
