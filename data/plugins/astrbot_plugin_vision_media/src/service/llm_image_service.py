import logging
import os
from collections.abc import Callable

import aiohttp

logger = logging.getLogger("astrbot")


class LLMImageService:
    def __init__(self, get_conf: Callable[[str, object], object]):
        self._get_conf = get_conf

    async def prepare_llm_image_contents(self, image_urls: list[str]) -> list[dict]:
        contents = []
        try:
            import base64
            import io

            from PIL import Image
        except Exception:
            return [
                {"type": "image_url", "image_url": {"url": u}}
                for u in (image_urls or [])
            ]

        max_kb = int(self._get_conf("mem_chat_llm_image_max_kb", 2048))
        max_dim = int(self._get_conf("mem_chat_llm_image_max_dim", 4096))
        init_quality = int(self._get_conf("mem_chat_llm_image_quality_init", 90))
        min_quality = int(self._get_conf("mem_chat_llm_image_quality_min", 65))
        gif_max_frames = int(self._get_conf("mem_chat_llm_gif_max_frames", 6))
        gif_total_max_kb = int(self._get_conf("mem_chat_llm_gif_total_max_kb", 3072))

        async def _load_bytes(u: str) -> bytes | None:
            try:
                if not u:
                    return None
                s = u.strip()
                if s.startswith("data:image/"):
                    try:
                        _head, b64 = s.split(",", 1)
                        return base64.b64decode(b64)
                    except Exception:
                        return None
                if os.path.exists(s):
                    with open(s, "rb") as f:
                        return f.read()
                if s.startswith("http://") or s.startswith("https://"):
                    try:
                        async with aiohttp.ClientSession(trust_env=True) as session:
                            async with session.get(s, timeout=15) as resp:
                                if resp.status == 200:
                                    return await resp.read()
                    except Exception:
                        return None
                return None
            except Exception:
                return None

        def _guess_mime(data: bytes) -> str:
            try:
                if data.startswith(b"\x89PNG"):
                    return "image/png"
                if data.startswith(b"GIF8"):
                    return "image/gif"
                if data.startswith(b"RIFF") and b"WEBP" in data[:12]:
                    return "image/webp"
            except Exception:
                pass
            return "image/jpeg"

        def _compress_pil_to_jpeg(im: Image.Image, max_bytes: int) -> bytes:
            try:
                w, h = im.size
                if max(w, h) > max_dim:
                    ratio = max_dim / float(max(w, h))
                    new_size = (max(1, int(w * ratio)), max(1, int(h * ratio)))
                    try:
                        im = im.resize(new_size, Image.LANCZOS)
                    except Exception:
                        im = im.resize(new_size)
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                q = init_quality
                step = 7
                last_bytes = b""
                for _ in range(10):
                    buf = io.BytesIO()
                    try:
                        im.save(buf, format="JPEG", quality=q, optimize=True)
                    except Exception:
                        im.save(buf, format="JPEG", quality=q)
                    b = buf.getvalue()
                    if len(b) <= max_bytes:
                        return b
                    last_bytes = b
                    q = max(min_quality, q - step)
                    if q == min_quality:
                        break
                return last_bytes
            except Exception:
                return b""

        def _extract_gif_frame_indices(frame_count: int, take: int) -> list[int]:
            if frame_count <= 0:
                return [0]
            if take <= 1 or frame_count == 1:
                return [0]
            take = min(take, frame_count)
            idxs: list[int] = []
            for i in range(take):
                pos = int(round(i * (frame_count - 1) / float(take - 1)))
                if not idxs or idxs[-1] != pos:
                    idxs.append(pos)
            return idxs

        for u in image_urls or []:
            b = await _load_bytes(u)
            if not b:
                contents.append({"type": "image_url", "image_url": {"url": u}})
                continue

            mime = _guess_mime(b)
            try:
                im = Image.open(io.BytesIO(b))
            except Exception:
                try:
                    b64 = base64.b64encode(b).decode("utf-8")
                    contents.append(
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
                    )
                except Exception:
                    contents.append({"type": "image_url", "image_url": {"url": u}})
                continue

            is_animated = bool(getattr(im, "is_animated", False)) and (
                str(getattr(im, "format", "") or "").upper() == "GIF"
            )
            if is_animated:
                try:
                    frame_count = int(getattr(im, "n_frames", 1) or 1)
                except Exception:
                    frame_count = 1
                take = max(1, min(gif_max_frames, frame_count))
                idxs = _extract_gif_frame_indices(frame_count, take)
                total_budget = max(1, gif_total_max_kb) * 1024
                per_budget = max(1, total_budget // max(1, len(idxs)))
                per_budget = min(per_budget, max_kb * 1024)

                gif_contents: list[dict] = []
                for idx in idxs:
                    try:
                        im.seek(idx)
                        frame = im.convert("RGB")
                    except Exception:
                        continue
                    jb = _compress_pil_to_jpeg(frame, per_budget)
                    if not jb:
                        continue
                    try:
                        b64 = base64.b64encode(jb).decode("utf-8")
                        gif_contents.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                            }
                        )
                    except Exception:
                        continue
                if gif_contents:
                    contents.extend(gif_contents)
                    continue

            target_bytes = max_kb * 1024
            if len(b) > target_bytes or mime != "image/jpeg":
                jb = _compress_pil_to_jpeg(im, target_bytes)
                if jb:
                    b = jb
                    mime = "image/jpeg"
            try:
                b64 = base64.b64encode(b).decode("utf-8")
                contents.append(
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
                )
            except Exception:
                contents.append({"type": "image_url", "image_url": {"url": u}})

        return contents
