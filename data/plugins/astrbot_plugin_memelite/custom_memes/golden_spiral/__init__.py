import io
import math
from bisect import bisect_right
from datetime import datetime

from meme_generator import add_meme
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
from pil_utils import BuildImage


CANVAS_W = 809
CANVAS_H = 500
FPS = 30
N_FRAMES = 240
SPRITE_SIZE = 150
AA = 3
ROTATION_CYCLES = 2.0
Y_ROTATION_CYCLES = 2.0
Y_AXIS_MIN_SCALE = 0.16
BACK_FACE_BRIGHTNESS = 0.82
CIRCLE_OUTLINE_WIDTH = 3
CIRCLE_BORDER_WIDTH = 7
CIRCLE_OUTLINE_COLOR = (220, 159, 33, 255)
CIRCLE_BORDER_COLOR = (255, 255, 255, 255)


def _center_crop_square(rgba: Image.Image) -> Image.Image:
    side = min(rgba.size)
    left = (rgba.width - side) // 2
    top = (rgba.height - side) // 2
    return rgba.crop((left, top, left + side, top + side))


def _ellipse_mask(size: int) -> Image.Image:
    scale = 4
    mask = Image.new("L", (size * scale, size * scale), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size * scale - 1, size * scale - 1), fill=255)
    return mask.resize((size, size), Image.Resampling.LANCZOS)


def _load_sprite(source: Image.Image) -> Image.Image:
    inset = CIRCLE_OUTLINE_WIDTH + CIRCLE_BORDER_WIDTH
    inner_size = SPRITE_SIZE - inset * 2
    rgba = _center_crop_square(source.convert("RGBA"))
    rgba = rgba.resize((inner_size, inner_size), Image.Resampling.LANCZOS)

    # Fill transparent input pixels with white so the avatar circle cleanly covers grid lines.
    image_layer = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    image_layer.alpha_composite(rgba)
    image_layer.putalpha(_ellipse_mask(inner_size))

    sprite = Image.new("RGBA", (SPRITE_SIZE, SPRITE_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(sprite)
    draw.ellipse(
        (0, 0, SPRITE_SIZE - 1, SPRITE_SIZE - 1),
        fill=CIRCLE_OUTLINE_COLOR,
    )
    border_box = (
        CIRCLE_OUTLINE_WIDTH,
        CIRCLE_OUTLINE_WIDTH,
        SPRITE_SIZE - CIRCLE_OUTLINE_WIDTH - 1,
        SPRITE_SIZE - CIRCLE_OUTLINE_WIDTH - 1,
    )
    draw.ellipse(border_box, fill=CIRCLE_BORDER_COLOR)
    sprite.alpha_composite(image_layer, (inset, inset))
    return sprite


def _load_sprite_frames(source: Image.Image) -> tuple[list[Image.Image], list[int], int]:
    frame_count = getattr(source, "n_frames", 1)
    sprites: list[Image.Image] = []
    cumulative_ms: list[int] = []
    total_ms = 0

    for frame_index in range(frame_count):
        try:
            source.seek(frame_index)
        except EOFError:
            break
        sprites.append(_load_sprite(source.copy()))
        default_duration = round(1000 / FPS)
        duration_ms = int(source.info.get("duration", default_duration) or default_duration)
        total_ms += max(20, duration_ms)
        cumulative_ms.append(total_ms)

    try:
        source.seek(0)
    except EOFError:
        pass

    if not sprites:
        sprites.append(_load_sprite(source))
        total_ms = round(1000 / FPS)
        cumulative_ms = [total_ms]

    return sprites, cumulative_ms, total_ms


def _sprite_at_frame(
    sprites: list[Image.Image],
    cumulative_ms: list[int],
    total_ms: int,
    idx: int,
) -> Image.Image:
    if len(sprites) == 1 or total_ms <= 0:
        return sprites[0]

    elapsed_ms = (idx * 1000 / FPS) % total_ms
    sprite_idx = min(bisect_right(cumulative_ms, elapsed_ms), len(sprites) - 1)
    return sprites[sprite_idx]


def _build_squares() -> list[tuple[int, int, int]]:
    return [
        (0, 0, 500),
        (500, 0, 309),
        (618, 309, 191),
        (500, 382, 118),
        (500, 309, 73),
        (573, 309, 45),
        (590, 354, 28),
        (573, 365, 17),
        (573, 354, 11),
    ]


def _arc_segments() -> list[list[tuple[float, float]]]:
    arcs = [
        ((500, 500), 500, 180, 270),
        ((500, 309), 309, 270, 360),
        ((618, 309), 191, 0, 90),
        ((618, 382), 118, 90, 180),
        ((573, 382), 73, 180, 270),
        ((573, 354), 45, 270, 360),
        ((590, 354), 28, 0, 90),
        ((590, 365), 17, 90, 180),
        ((584, 365), 11, 180, 270),
    ]
    segments: list[list[tuple[float, float]]] = []
    for (cx, cy), r, a0, a1 in arcs:
        steps = max(10, round((math.pi * r / 2) / 4))
        pts = []
        for step in range(steps):
            t = step / (steps - 1)
            angle = math.radians(a0 + (a1 - a0) * t)
            pts.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
        segments.append(pts)
    return segments


def _flatten_segments(segments: list[list[tuple[float, float]]]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for segment in segments:
        if points:
            points.extend(segment[1:])
        else:
            points.extend(segment)
    return points


def _path_lengths(points: list[tuple[float, float]]) -> tuple[list[float], float]:
    lengths = [0.0]
    total = 0.0
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        total += math.hypot(x1 - x0, y1 - y0)
        lengths.append(total)
    return lengths, total


def _ease(t: float) -> float:
    return 0.5 - 0.5 * math.cos(math.pi * t)


def _sample_path(
    points: list[tuple[float, float]],
    lengths: list[float],
    total_length: float,
    progress: float,
) -> tuple[float, float]:
    if progress < 0.5:
        p = _ease(progress * 2)
    else:
        p = _ease(2 - progress * 2)
    p = 0.14 + p * 0.80
    target = p * total_length

    lo, hi = 0, len(lengths) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if lengths[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    i = max(0, lo - 1)
    if i >= len(points) - 1:
        return points[-1]
    segment_len = lengths[i + 1] - lengths[i]
    f = 0 if segment_len == 0 else (target - lengths[i]) / segment_len
    x0, y0 = points[i]
    x1, y1 = points[i + 1]
    return x0 + (x1 - x0) * f, y0 + (y1 - y0) * f


def _scaled_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    return [(x * AA, y * AA) for x, y in points]


def _draw_grid(squares, segments) -> Image.Image:
    bg = Image.new("RGBA", (CANVAS_W * AA, CANVAS_H * AA), (255, 255, 255, 255))
    draw = ImageDraw.Draw(bg)
    draw.rectangle(
        (0, 0, CANVAS_W * AA - 1, CANVAS_H * AA - 1),
        outline=(194, 140, 36),
        width=3 * AA,
    )
    for x, y, s in squares:
        draw.rectangle(
            (x * AA, y * AA, (x + s) * AA, (y + s) * AA),
            outline=(215, 165, 54),
            width=2 * AA,
        )
    for segment in segments:
        draw.line(
            _scaled_points(segment),
            fill=(220, 159, 33),
            width=5 * AA,
            joint="curve",
        )
    return bg.resize((CANVAS_W, CANVAS_H), Image.Resampling.LANCZOS)


def _dim_rgba(image: Image.Image, brightness: float) -> Image.Image:
    r, g, b, a = image.split()
    rgb = Image.merge("RGB", (r, g, b))
    rgb = ImageEnhance.Brightness(rgb).enhance(brightness)
    return Image.merge("RGBA", (*rgb.split(), a))


def _y_axis_turn(image: Image.Image, idx: int) -> Image.Image:
    phase = 2 * math.pi * Y_ROTATION_CYCLES * idx / N_FRAMES
    facing = math.cos(phase)
    scale_x = Y_AXIS_MIN_SCALE + (1 - Y_AXIS_MIN_SCALE) * abs(facing)
    width = max(2, round(image.width * scale_x))

    card = image
    if facing < 0:
        card = card.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        card = _dim_rgba(card, BACK_FACE_BRIGHTNESS)

    card = card.resize((width, image.height), Image.Resampling.BICUBIC)
    turned = Image.new("RGBA", image.size, (0, 0, 0, 0))
    turned.alpha_composite(card, ((image.width - width) // 2, 0))
    return turned


def _rotated_sprite(sprite: Image.Image, idx: int) -> Image.Image:
    angle = -360 * ROTATION_CYCLES * idx / N_FRAMES
    rotated = sprite.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False)
    rotated = _y_axis_turn(rotated, idx)
    alpha = rotated.getchannel("A").filter(ImageFilter.MaxFilter(5)).filter(
        ImageFilter.GaussianBlur(1.2)
    )
    underlay = Image.new("RGBA", rotated.size, (255, 255, 255, 255))
    underlay.putalpha(alpha)
    underlay.alpha_composite(rotated)
    return underlay


def _make_frame(
    sprites: list[Image.Image],
    cumulative_ms: list[int],
    input_total_ms: int,
    path_points: list[tuple[float, float]],
    lengths: list[float],
    total_length: float,
    bg_template: Image.Image,
    idx: int,
) -> Image.Image:
    bg = bg_template.copy()
    x, y = _sample_path(path_points, lengths, total_length, idx / N_FRAMES)
    sprite = _sprite_at_frame(sprites, cumulative_ms, input_total_ms, idx)
    piece = _rotated_sprite(sprite, idx)
    pos = (round(x - piece.width / 2), round(y - piece.height / 2))
    bg.alpha_composite(piece, pos)
    return bg.convert("P", palette=Image.Palette.ADAPTIVE, colors=256)


def golden_spiral(images: list[BuildImage], texts, args):
    sprites, cumulative_ms, input_total_ms = _load_sprite_frames(images[0].image)
    squares = _build_squares()
    segments = _arc_segments()
    points = _flatten_segments(segments)
    lengths, total_length = _path_lengths(points)
    bg_template = _draw_grid(squares, segments)
    frames = [
        _make_frame(
            sprites,
            cumulative_ms,
            input_total_ms,
            points,
            lengths,
            total_length,
            bg_template,
            i,
        )
        for i in range(N_FRAMES)
    ]

    output = io.BytesIO()
    frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=round(1000 / FPS),
        loop=0,
        optimize=False,
        disposal=2,
    )
    return output


add_meme(
    "golden_spiral",
    golden_spiral,
    min_images=1,
    max_images=1,
    keywords=["黄金回旋"],
    date_created=datetime(2026, 7, 4),
    date_modified=datetime(2026, 7, 4),
)
