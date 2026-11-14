import argparse
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw


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


def _sat_bbox(frame_rgba: Image.Image, pad: int = 10) -> tuple[int, int, int, int]:
    rgb = np.array(frame_rgba.convert("RGB"))
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    s = hsv[:, :, 1]
    _, th = cv2.threshold(s, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    th = cv2.medianBlur(th, 5)
    th = cv2.morphologyEx(th, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        w, h = frame_rgba.size
        return 0, 0, w, h
    areas = [cv2.contourArea(c) for c in contours]
    i = int(np.argmax(areas))
    x, y, w, h = cv2.boundingRect(contours[i])
    x0 = max(0, x - pad)
    y0 = max(0, y - pad)
    x1 = min(frame_rgba.size[0], x + w + pad)
    y1 = min(frame_rgba.size[1], y + h + pad)
    return x0, y0, x1, y1


def _track_bboxes(frames: list[Image.Image], pad: int = 10) -> list[tuple[int, int, int, int]]:
    if not frames:
        return []
    f0 = frames[0]
    x0, y0, x1, y1 = _sat_bbox(f0, pad=pad)
    g0 = cv2.cvtColor(np.array(f0.convert("RGB")), cv2.COLOR_RGB2GRAY)
    tpl = g0[y0:y1, x0:x1]
    if tpl.size == 0:
        w, h = f0.size
        return [(0, 0, w, h) for _ in frames]
    bboxes: list[tuple[int, int, int, int]] = []
    for fr in frames:
        g = cv2.cvtColor(np.array(fr.convert("RGB")), cv2.COLOR_RGB2GRAY)
        res = cv2.matchTemplate(g, tpl, cv2.TM_CCOEFF_NORMED)
        _, _, _, maxloc = cv2.minMaxLoc(res)
        tx, ty = maxloc
        bx0 = int(tx)
        by0 = int(ty)
        bx1 = int(tx + tpl.shape[1])
        by1 = int(ty + tpl.shape[0])
        bboxes.append((bx0, by0, bx1, by1))
    return bboxes


def _draw_placeholder(
    frame_rgba: Image.Image,
    bbox: tuple[int, int, int, int],
    inset: int,
    outline: int,
    outline_color: tuple[int, int, int, int],
) -> Image.Image:
    w, h = frame_rgba.size
    x0, y0, x1, y1 = bbox
    x0 = max(0, min(w - 1, x0 + inset))
    y0 = max(0, min(h - 1, y0 + inset))
    x1 = max(x0 + 1, min(w, x1 - inset))
    y1 = max(y0 + 1, min(h, y1 - inset))
    out = frame_rgba.copy()
    draw = ImageDraw.Draw(out)
    if outline > 0:
        draw.ellipse([x0, y0, x1 - 1, y1 - 1], outline=outline_color, width=outline)
    a = np.array(out, dtype=np.uint8)
    alpha = a[:, :, 3]
    yy, xx = np.ogrid[:h, :w]
    cx = (x0 + x1 - 1) / 2.0
    cy = (y0 + y1 - 1) / 2.0
    rx = max(1.0, (x1 - x0) / 2.0)
    ry = max(1.0, (y1 - y0) / 2.0)
    inside = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2 <= 1.0
    alpha[inside] = 0
    a[:, :, 3] = alpha
    return Image.fromarray(a, mode="RGBA")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gif", type=str)
    ap.add_argument("--out", type=str, default="frames_out")
    ap.add_argument("--pad", type=int, default=10)
    ap.add_argument("--inset", type=int, default=6)
    ap.add_argument("--outline", type=int, default=6)
    ap.add_argument("--outline-color", type=str, default="#FFFFFF")
    args = ap.parse_args()

    gif_path = Path(args.gif).expanduser()
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    frames = _iter_gif_frames(gif_path)
    if not frames:
        raise SystemExit("no frames")

    outline_color = tuple(int(args.outline_color.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore
    outline_rgba = (outline_color[0], outline_color[1], outline_color[2], 255)

    bboxes = _track_bboxes(frames, pad=args.pad)
    for i, (fr, bb) in enumerate(zip(frames, bboxes)):
        out = _draw_placeholder(
            fr,
            bb,
            inset=args.inset,
            outline=args.outline,
            outline_color=outline_rgba,
        )
        out.save(out_dir / f"frame{i:04d}.png")


if __name__ == "__main__":
    main()

