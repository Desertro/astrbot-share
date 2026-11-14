from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from meme_generator import add_meme
from meme_generator.utils import save_gif
from PIL import Image
from pil_utils import BuildImage

img_dir = Path(__file__).parent / "images"


def _iter_frames() -> list[Image.Image]:
    frames_dir = img_dir / "frames"
    if frames_dir.exists():
        items = []
        for ext in ("*.png", "*.jpg", "*.jpeg"):
            items.extend(frames_dir.glob(ext))
        frames = sorted(items)
        if frames:
            return [Image.open(p).convert("RGBA") for p in frames]
    base_path = img_dir / "base.gif"
    return _iter_gif_frames(base_path) if base_path.exists() else []


def _iter_gif_frames(path: Path) -> list[Image.Image]:
    im = Image.open(path)
    frames: list[Image.Image] = []
    i = 0
    while True:
        try:
            im.seek(i)
        except EOFError:
            break
        frames.append(im.convert("RGBA"))
        i += 1
    return frames


def _largest_component_bbox(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    if mask.dtype != np.uint8:
        mask = mask.astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n <= 1:
        return None
    areas = stats[1:, cv2.CC_STAT_AREA]
    idx = int(np.argmax(areas)) + 1
    x = int(stats[idx, cv2.CC_STAT_LEFT])
    y = int(stats[idx, cv2.CC_STAT_TOP])
    w = int(stats[idx, cv2.CC_STAT_WIDTH])
    h = int(stats[idx, cv2.CC_STAT_HEIGHT])
    if w <= 1 or h <= 1:
        return None
    return x, y, x + w, y + h


def _detect_green_circle(frame_rgba: Image.Image) -> tuple[float, float, float] | None:
    rgb = np.array(frame_rgba.convert("RGB"))
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h = hsv[:, :, 0]
    s = hsv[:, :, 1]
    v = hsv[:, :, 2]
    mask = ((h >= 35) & (h <= 90) & (s >= 80) & (v >= 80)).astype(np.uint8) * 255
    mask = cv2.medianBlur(mask, 5)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    c = max(contours, key=cv2.contourArea)
    area = float(cv2.contourArea(c))
    if area < 60:
        return None
    m = cv2.moments(c)
    if m.get("m00", 0) > 0:
        x = float(m["m10"] / m["m00"])
        y = float(m["m01"] / m["m00"])
    else:
        (x, y), _ = cv2.minEnclosingCircle(c)
        x = float(x)
        y = float(y)
    r = float(np.sqrt(area / np.pi))
    if r < 6:
        return None
    return float(x), float(y), float(r)


def _detect_placeholder_bbox(frame_rgba: Image.Image) -> tuple[int, int, int, int]:
    a = np.array(frame_rgba)
    alpha = a[:, :, 3]
    hole = (alpha < 20).astype(np.uint8) * 255
    hole = cv2.medianBlur(hole, 5)
    bbox = _largest_component_bbox(hole)
    if bbox is not None:
        return bbox

    rgb = a[:, :, :3]
    white = (
        (alpha > 200)
        & (rgb[:, :, 0] > 245)
        & (rgb[:, :, 1] > 245)
        & (rgb[:, :, 2] > 245)
    ).astype(np.uint8) * 255
    white = cv2.medianBlur(white, 5)
    white = cv2.morphologyEx(white, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    bbox = _largest_component_bbox(white)
    if bbox is not None:
        x0, y0, x1, y1 = bbox
        inset = 6
        x0 = min(x1 - 1, x0 + inset)
        y0 = min(y1 - 1, y0 + inset)
        x1 = max(x0 + 1, x1 - inset)
        y1 = max(y0 + 1, y1 - inset)
        return x0, y0, x1, y1

    w, h = frame_rgba.size
    return 0, 0, w, h


def pig_head(images: list[BuildImage], texts, args):
    frames = _iter_frames()
    if not frames:
        return None

    out_frames: list[Image.Image] = []
    for f in frames:
        cxcy = _detect_green_circle(f)
        if cxcy is not None:
            cx, cy, r = cxcy
            expand = float(args.get("expand", 0.10)) if isinstance(args, dict) else 0.10
            r = r * (1.0 + expand)
            d = max(1, int(round(r * 2)))
            x0 = int(round(cx - d / 2))
            y0 = int(round(cy - d / 2))
            x0 = max(-d, min(f.size[0], x0))
            y0 = max(-d, min(f.size[1], y0))
            avatar = (
                images[0]
                .convert("RGBA")
                .circle()
                .resize((d, d), keep_ratio=True, inside=True)
            )
            out_frames.append(
                BuildImage(f.copy()).paste(avatar, (x0, y0), alpha=True).image
            )
        else:
            x0, y0, x1, y1 = _detect_placeholder_bbox(f)
            w = max(1, x1 - x0)
            h = max(1, y1 - y0)
            avatar = (
                images[0]
                .convert("RGBA")
                .circle()
                .resize((w, h), keep_ratio=True, inside=True)
            )
            under = Image.new("RGBA", f.size, (0, 0, 0, 0))
            under = BuildImage(under).paste(avatar, (x0, y0), alpha=True).image
            out_frames.append(Image.alpha_composite(under, f))
    return save_gif(out_frames, 0.06)


add_meme(
    "pi_pig",
    pig_head,
    min_images=1,
    max_images=1,
    keywords=["劈猪"],
    date_created=datetime(2026, 1, 24),
    date_modified=datetime(2026, 1, 24),
)
