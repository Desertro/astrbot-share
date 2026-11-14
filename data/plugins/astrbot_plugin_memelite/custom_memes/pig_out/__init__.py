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
    items = []
    for ext in ("*.png", "*.jpg", "*.jpeg"):
        items.extend(frames_dir.glob(ext))
    frames = sorted(items)
    return [Image.open(p).convert("RGBA") for p in frames]


def _green_alpha(frame_rgba: Image.Image) -> np.ndarray:
    a = np.array(frame_rgba, dtype=np.uint8)
    rgb = a[:, :, :3].astype(np.int16)
    r = rgb[:, :, 0]
    g = rgb[:, :, 1]
    b = rgb[:, :, 2]
    key = g - np.maximum(r, b)
    alpha = (key - 60) / 140.0
    alpha = np.clip(alpha, 0.0, 1.0).astype(np.float32)
    alpha *= ((g >= 140) & (r <= 140) & (b <= 140)).astype(np.float32)
    return alpha


def _avatar_box_from_alpha(alpha: np.ndarray) -> tuple[float, float, float]:
    ys, xs = np.where(alpha > 0.05)
    if len(xs) == 0:
        return 0.0, 0.0, 0.0
    cx = float(xs.mean())
    cy = float(ys.mean())
    dx = xs.astype(np.float32) - cx
    dy = ys.astype(np.float32) - cy
    dist = np.sqrt(dx * dx + dy * dy)
    r = float(np.percentile(dist, 98))
    return cx, cy, max(1.0, r)


def pig_out(images: list[BuildImage], texts, args):
    frames = _iter_frames()
    if not frames:
        return None

    expand = float(args.get("expand", 0.16)) if isinstance(args, dict) else 0.16
    out_frames: list[Image.Image] = []

    for fr in frames:
        fr_u8 = np.array(fr, dtype=np.uint8)
        alpha = _green_alpha(fr)
        cx, cy, r = _avatar_box_from_alpha(alpha)
        if r <= 0:
            out_frames.append(fr)
            continue
        r = r * (1.0 + expand)
        d = int(round(r * 2))
        d = max(1, d)
        x0 = int(round(cx - d / 2))
        y0 = int(round(cy - d / 2))

        avatar_img = (
            images[0].convert("RGBA").circle().resize((d, d), keep_ratio=True, inside=True).image
        )
        av_u8 = np.array(avatar_img, dtype=np.uint8)
        extra_px = 3
        if extra_px > 0:
            kernel = np.ones((extra_px * 2 + 1, extra_px * 2 + 1), np.uint8)
            a0 = av_u8[:, :, 3]
            a1 = cv2.dilate(a0, kernel, iterations=1)
            grow = (a1 > 0) & (a0 == 0)
            if grow.any():
                for ch in range(3):
                    src = np.where(a0 > 0, av_u8[:, :, ch], 0).astype(np.uint8)
                    dil = cv2.dilate(src, kernel, iterations=1)
                    av_u8[:, :, ch][grow] = dil[grow]
                av_u8[:, :, 3] = a1
        av = av_u8.astype(np.float32)
        base = np.array(fr, dtype=np.float32)

        h, w = base.shape[:2]
        ax0 = max(0, x0)
        ay0 = max(0, y0)
        ax1 = min(w, x0 + d)
        ay1 = min(h, y0 + d)
        if ax0 >= ax1 or ay0 >= ay1:
            out_frames.append(fr)
            continue

        sx0 = ax0 - x0
        sy0 = ay0 - y0
        sx1 = sx0 + (ax1 - ax0)
        sy1 = sy0 + (ay1 - ay0)

        rgb = fr_u8[:, :, :3].astype(np.int16)
        key = rgb[:, :, 1] - np.maximum(rgb[:, :, 0], rgb[:, :, 2])
        greenish = (rgb[:, :, 1] > 140) & (key > 25)
        mask0 = (alpha > 0.02) | greenish
        if extra_px > 0:
            k2 = np.ones((extra_px * 2 + 1, extra_px * 2 + 1), np.uint8)
            mask0 = cv2.dilate(mask0.astype(np.uint8) * 255, k2, iterations=1) > 0
        m = np.maximum(alpha, mask0.astype(np.float32))[ay0:ay1, ax0:ax1]
        m = m[:, :, None].astype(np.float32)
        base[ay0:ay1, ax0:ax1, :3] = (
            av[sy0:sy1, sx0:sx1, :3] * m + base[ay0:ay1, ax0:ax1, :3] * (1.0 - m)
        )

        out_frames.append(Image.fromarray(base.astype(np.uint8), mode="RGBA"))

    return save_gif(out_frames, 0.06)


add_meme(
    "pig_out",
    pig_out,
    min_images=1,
    max_images=1,
    keywords=["猪出栏"],
    date_created=datetime(2026, 1, 25),
    date_modified=datetime(2026, 1, 25),
)
