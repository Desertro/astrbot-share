
import hashlib
import io
import time
from pathlib import Path

from PIL import Image

from astrbot.core.utils.astrbot_path import get_astrbot_data_path


def _image_suffix(image: bytes) -> str:
    try:
        with Image.open(io.BytesIO(image)) as img:
            fmt = (img.format or "").lower()
    except Exception:
        return ".bin"
    if fmt == "jpeg":
        return ".jpg"
    if fmt in {"png", "gif", "webp"}:
        return f".{fmt}"
    return f".{fmt}" if fmt else ".bin"


def save_image_temp(image: bytes, prefix: str = "meme") -> str:
    temp_dir = Path(get_astrbot_data_path()) / "temp" / "memelite"
    temp_dir.mkdir(parents=True, exist_ok=True)

    now = time.time()
    for path in temp_dir.glob("*"):
        try:
            if path.is_file() and now - path.stat().st_mtime > 24 * 60 * 60:
                path.unlink()
        except OSError:
            pass

    digest = hashlib.sha256(image).hexdigest()[:16]
    path = temp_dir / f"{prefix}_{digest}{_image_suffix(image)}"
    if not path.exists() or path.stat().st_size != len(image):
        path.write_bytes(image)
    return str(path)



def is_effectively_black_image(image: bytes) -> bool:
    """Return True when a generated image contains no visible pixel data."""
    try:
        with Image.open(io.BytesIO(image)) as img:
            rgb = img.convert("RGB")
            rgb.thumbnail((32, 32), Image.Resampling.BILINEAR)
            return all(high <= 2 for _low, high in rgb.getextrema())
    except Exception:
        return False



def optimize_static_image(
    image: bytes,
    max_width: int = 2200,
    jpeg_quality: int = 86,
) -> bytes:
    """把说明类静态图片转成更适合聊天发送的 JPEG。"""
    try:
        with Image.open(io.BytesIO(image)) as img:
            if getattr(img, "is_animated", False):
                return image

            if img.width > max_width:
                ratio = max_width / img.width
                img = img.resize(
                    (max_width, max(1, int(img.height * ratio))),
                    Image.Resampling.LANCZOS,
                )

            if img.mode in {"RGBA", "LA"} or (
                img.mode == "P" and "transparency" in img.info
            ):
                base = Image.new("RGB", img.size, (248, 250, 252))
                alpha = img.convert("RGBA").getchannel("A")
                base.paste(img.convert("RGB"), mask=alpha)
                img = base
            else:
                img = img.convert("RGB")

            output = io.BytesIO()
            img.save(output, format="JPEG", quality=jpeg_quality, optimize=True)
            optimized = output.getvalue()
            return optimized if len(optimized) < len(image) else image
    except Exception:
        return image


def compress_image(image: bytes, max_size: int = 512) -> bytes | None:
    """压缩静态图片或GIF到max_size大小"""
    try:
        img = Image.open(io.BytesIO(image))
        if img.format == "GIF":
            return

        if img.width > max_size or img.height > max_size:
            img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

        output = io.BytesIO()
        img.save(output, format=img.format)
        return output.getvalue()

    except Exception as e:
        raise ValueError(f"图片压缩失败: {e}")
