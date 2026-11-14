import asyncio
import hashlib
import importlib.util
import io
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from meme_generator import Meme, get_memes
from meme_generator.version import __version__
from PIL import Image, ImageDraw, ImageFont

from astrbot import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .param import ParamsCollector


@dataclass
class MemeProperties:
    disabled: bool = False
    labels: list[Literal["new", "hot"]] = field(default_factory=list)


class MemeManager:
    is_py_version = tuple(map(int, __version__.split("."))) < (0, 2, 0)
    emoji_patch_version = 2
    def __init__(self, config: AstrBotConfig, collect: ParamsCollector):
        self.conf = config
        self.collect = collect
        self._meme_list_image_cache: bytes | None = None

        try:
            import re
            from pathlib import Path

            from PIL import Image, ImageDraw, ImageFont
            from pil_utils import Text2Image as _Text2Image
            from pil_utils.text2image import DEFAULT_FALLBACK_FONTS

            home = Path.home()
            plugin_font_dir = Path(__file__).resolve().parents[1] / "fonts"
            try:
                import skia
                from importlib import import_module

                text2image_mod = import_module("pil_utils.text2image")
                if not getattr(text2image_mod, "_astrbot_fontmgr_patched", False):
                    text2image_mod.font_collection.setAssetFontManager(
                        skia.FontMgr.New_Custom_Directory(str(plugin_font_dir))
                    )
                    setattr(text2image_mod, "_astrbot_fontmgr_patched", True)
            except Exception:
                pass
            emoji_font_candidates = [
                (plugin_font_dir / "AppleColorEmoji.ttf", "Apple Color Emoji"),
                (plugin_font_dir / "seguiemj.ttf", "Segoe UI Emoji"),
                (plugin_font_dir / "SegoeUIEmoji.ttf", "Segoe UI Emoji"),
                (
                    home / ".meme_generator" / "resources" / "fonts" / "AppleColorEmoji.ttf",
                    "Apple Color Emoji",
                ),
                (
                    home / ".meme_generator" / "libraries" / "fonts" / "AppleColorEmoji.ttf",
                    "Apple Color Emoji",
                ),
                (
                    home
                    / ".meme_generator"
                    / "resources"
                    / "fonts"
                    / "TwitterColorEmoji-SVGinOT.ttf",
                    "Twitter Color Emoji",
                ),
                (
                    home
                    / ".meme_generator"
                    / "libraries"
                    / "fonts"
                    / "TwitterColorEmoji-SVGinOT.ttf",
                    "Twitter Color Emoji",
                ),
                (
                    home / ".meme_generator" / "resources" / "fonts" / "seguiemj.ttf",
                    "Segoe UI Emoji",
                ),
                (
                    home / ".meme_generator" / "libraries" / "fonts" / "seguiemj.ttf",
                    "Segoe UI Emoji",
                ),
                (
                    home / ".meme_generator" / "resources" / "fonts" / "SegoeUIEmoji.ttf",
                    "Segoe UI Emoji",
                ),
                (
                    home / ".meme_generator" / "libraries" / "fonts" / "SegoeUIEmoji.ttf",
                    "Segoe UI Emoji",
                ),
                (
                    home / ".meme_generator" / "resources" / "fonts" / "Segoe UI Emoji.ttf",
                    "Segoe UI Emoji",
                ),
                (
                    home / ".meme_generator" / "libraries" / "fonts" / "Segoe UI Emoji.ttf",
                    "Segoe UI Emoji",
                ),
                (
                    home / ".meme_generator" / "resources" / "fonts" / "NotoColorEmoji.ttf",
                    "Noto Color Emoji",
                ),
                (
                    home / ".meme_generator" / "libraries" / "fonts" / "NotoColorEmoji.ttf",
                    "Noto Color Emoji",
                ),
            ]
            strike_sizes = [109, 160, 128, 96, 72, 64, 48, 32, 24, 16]
            emoji_font_path: Path | None = None
            emoji_font_family: str | None = None
            strike_size: int | None = None
            for candidate, family in emoji_font_candidates:
                if not candidate.exists():
                    continue
                for s in strike_sizes:
                    try:
                        ImageFont.truetype(str(candidate), s)
                        emoji_font_path = candidate
                        emoji_font_family = family
                        strike_size = s
                        break
                    except Exception:
                        continue
                if strike_size is not None:
                    break

            if (
                getattr(_Text2Image, "_astrbot_color_emoji_patch_version", 0)
                < self.emoji_patch_version
                and emoji_font_path is not None
                and strike_size is not None
            ):
                try:
                    logger.info(
                        f"meme_lite emoji字体: {emoji_font_path} (strike={strike_size})"
                    )
                except Exception:
                    pass

                apple_snowflake_path = plugin_font_dir / "emoji_u2744.png"
                orig_from_text = _Text2Image.from_text
                orig_from_bbcode_text = _Text2Image.from_bbcode_text
                if True:

                    emoji_char = r"[\u200d\uFE0E\uFE0F\u2600-\u27BF\U0001F000-\U0001FAFF]"
                    emoji_re = re.compile(emoji_char)

                    emoji_token_re = re.compile(
                        rf"(?:{emoji_char})(?:[\uFE0E\uFE0F])*(?:\u200d(?:{emoji_char})(?:[\uFE0E\uFE0F])*)*"
                    )
                    emoji_family_names = [
                        family
                        for _, family in emoji_font_candidates
                        if family
                    ]
                    if emoji_font_family:
                        emoji_family_names.insert(0, emoji_font_family)

                    def _has_emoji_font(families) -> bool:
                        for family in families or []:
                            family_name = str(family).lower()
                            if "emoji" in family_name or "symbol" in family_name:
                                return True
                        return False

                    def _with_default_emoji_font(
                        font_families=None, fallback_fonts_families=None
                    ):
                        fonts = list(font_families or [])
                        fallbacks = list(
                            DEFAULT_FALLBACK_FONTS
                            if fallback_fonts_families is None
                            else fallback_fonts_families
                        )
                        if _has_emoji_font(fonts) or _has_emoji_font(fallbacks):
                            return fonts, fallbacks
                        for family in emoji_family_names:
                            if family not in fallbacks:
                                fallbacks.append(family)
                        return fonts, fallbacks

                    class _PillowText2Image:
                        def __init__(self, text: str, font_size: float, fill):
                            self._text = text
                            self._font_size = float(font_size)
                            self._fill = fill
                            self._wrap_width: int | None = None

                        def _emoji_font(self):
                            return ImageFont.truetype(str(emoji_font_path), strike_size)  # type: ignore[arg-type]

                        def _text_font(self):
                            for text_font_path in [
                                Path("/mnt/c/Windows/Fonts/msyh.ttc"),
                                Path("/mnt/c/Windows/Fonts/msyhbd.ttc"),
                                plugin_font_dir / "msyh.ttc",
                                home / ".meme_generator" / "libraries" / "fonts" / "msyh.ttc",
                                home / ".meme_generator" / "libraries" / "fonts" / "SourceHanSansSC-Regular.otf",
                                home / ".meme_generator" / "resources" / "fonts" / "NotoSansSC-Regular.ttf",
                                home / ".meme_generator" / "libraries" / "fonts" / "NotoSansSC-Regular.ttf",
                            ]:
                                if text_font_path.exists():
                                    try:
                                        return ImageFont.truetype(
                                            str(text_font_path), int(self._font_size)
                                        )
                                    except Exception:
                                        continue
                            return ImageFont.load_default()

                        def _is_emoji(self, token: str) -> bool:
                            return bool(emoji_re.search(token))

                        def _tokenize(self) -> list[str]:
                            out: list[str] = []
                            s = self._text
                            i = 0
                            while i < len(s):
                                if s[i] == "\n":
                                    out.append("\n")
                                    i += 1
                                    continue
                                m = emoji_token_re.match(s, i)
                                if m:
                                    out.append(m.group(0))
                                    i = m.end()
                                    continue
                                out.append(s[i])
                                i += 1
                            return out

                        def _render_token_image(self, token: str):
                            if token == "\n":
                                return None, 0, 0
                            if self._is_emoji(token):
                                if "\u2744" in token and apple_snowflake_path.exists():
                                    try:
                                        with Image.open(apple_snowflake_path) as raw:
                                            img = raw.convert("RGBA")
                                        target = max(1, int(round(self._font_size * 1.2)))
                                        img = img.resize(
                                            (target, target),
                                            resample=Image.Resampling.LANCZOS,
                                        )
                                        return img, img.size[0], img.size[1]
                                    except Exception:
                                        pass
                                if (
                                    "\ufe0f" not in token
                                    and "\ufe0e" not in token
                                    and len(token) == 1
                                ):
                                    cp = ord(token)
                                    if 0x2600 <= cp <= 0x27BF:
                                        token = f"{token}\ufe0f"
                                font = self._emoji_font()
                                tmp = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
                                d = ImageDraw.Draw(tmp)
                                bbox = d.textbbox(
                                    (0, 0), token, font=font, embedded_color=True
                                )
                                w = max(1, bbox[2] - bbox[0])
                                h = max(1, bbox[3] - bbox[1])
                                img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                                d2 = ImageDraw.Draw(img)
                                d2.text(
                                    (-bbox[0], -bbox[1]),
                                    token,
                                    font=font,
                                    fill=(255, 255, 255, 255),
                                    embedded_color=True,
                                )
                                scale = self._font_size / float(strike_size)
                                nw = max(1, int(round(w * scale)))
                                nh = max(1, int(round(h * scale)))
                                img = img.resize(
                                    (nw, nh), resample=Image.Resampling.LANCZOS
                                )
                                return img, img.size[0], img.size[1]

                            font = self._text_font()
                            tmp = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
                            d = ImageDraw.Draw(tmp)
                            bbox = d.textbbox((0, 0), token, font=font)
                            w = max(1, bbox[2] - bbox[0])
                            h = max(1, bbox[3] - bbox[1])
                            img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                            d2 = ImageDraw.Draw(img)
                            d2.text((-bbox[0], -bbox[1]), token, font=font, fill=self._fill)
                            return img, w, h

                        def _layout_lines(self) -> list[list[tuple]]:
                            tokens = self._tokenize()
                            lines: list[list[tuple]] = [[]]
                            x = 0
                            max_w = self._wrap_width
                            for tok in tokens:
                                if tok == "\n":
                                    lines.append([])
                                    x = 0
                                    continue
                                img, w, h = self._render_token_image(tok)
                                if img is None:
                                    continue
                                if max_w is not None and x > 0 and x + w > max_w:
                                    lines.append([])
                                    x = 0
                                lines[-1].append((img, w, h))
                                x += w
                            return lines

                        @property
                        def longest_line(self) -> float:
                            lines = self._layout_lines()
                            if not lines:
                                return 0
                            return float(
                                max(sum(w for _, w, _ in line) for line in lines)
                            )

                        @property
                        def height(self) -> float:
                            lines = self._layout_lines()
                            if not lines:
                                return 0
                            return float(
                                sum(
                                    (
                                        max((h for _, _, h in line), default=0)
                                        or int(self._font_size)
                                    )
                                    for line in lines
                                )
                            )

                        def wrap(self, width: float):
                            self._wrap_width = int(width)
                            return self

                        def to_image(self, max_width=None, bg_color=None, padding=(0, 0)):
                            lines = self._layout_lines()
                            if not lines:
                                return Image.new("RGBA", (1, 1), (0, 0, 0, 0))

                            line_heights: list[int] = []
                            line_widths: list[int] = []
                            for line in lines:
                                line_widths.append(sum(w for _, w, _ in line))
                                line_heights.append(
                                    max(
                                        (h for _, _, h in line),
                                        default=int(self._font_size),
                                    )
                                )

                            width = max(line_widths) if line_widths else 1
                            height = sum(line_heights) if line_heights else 1

                            img = Image.new(
                                "RGBA",
                                (max(1, width), max(1, height)),
                                (0, 0, 0, 0),
                            )
                            y = 0
                            for line, lh in zip(lines, line_heights):
                                x = 0
                                for token_img, w, h in line:
                                    oy = (lh - h) // 2
                                    img.paste(token_img, (x, y + oy), token_img)
                                    x += w
                                y += lh
                            return img

                        def draw_on_image(self, img, pos, max_width=None):
                            token_img = self.to_image(max_width=max_width)
                            x, y = int(pos[0]), int(pos[1])
                            img.paste(token_img, (x, y), token_img)

                    def _from_text(cls, text: str, font_size: float, **kwargs):
                        font_families, fallback_fonts = _with_default_emoji_font(
                            kwargs.get("font_families"),
                            kwargs.get("fallback_fonts_families"),
                        )
                        kwargs["font_families"] = font_families
                        kwargs["fallback_fonts_families"] = fallback_fonts

                        if not font_families and isinstance(text, str):
                            has_cjk = bool(re.search(r"[\u4e00-\u9fff]", text))
                            if has_cjk:
                                kwargs["font_families"] = [
                                    "Microsoft YaHei",
                                    "微软雅黑",
                                    "Segoe UI",
                                    "Noto Sans SC",
                                    "Source Han Sans SC",
                                ]
                        fill = kwargs.get("fill", "black")
                        if (
                            isinstance(text, str)
                            and emoji_re.search(text)
                            and isinstance(fill, (str, tuple, list))
                            and kwargs.get("stroke_fill") is None
                        ):
                            return _PillowText2Image(text, font_size, fill)
                        return orig_from_text(text, font_size, **kwargs)

                    def _from_bbcode_text(cls, text: str, font_size: float, **kwargs):
                        font_families, fallback_fonts = _with_default_emoji_font(
                            kwargs.get("font_families"),
                            kwargs.get("fallback_fonts_families"),
                        )
                        kwargs["font_families"] = font_families
                        kwargs["fallback_fonts_families"] = fallback_fonts
                        return orig_from_bbcode_text(text, font_size, **kwargs)

                    _Text2Image.from_text = classmethod(_from_text)
                    _Text2Image.from_bbcode_text = classmethod(_from_bbcode_text)
                    setattr(_Text2Image, "_astrbot_color_emoji_patched", True)
                    setattr(
                        _Text2Image,
                        "_astrbot_color_emoji_patch_version",
                        self.emoji_patch_version,
                    )
        except Exception:
            pass

        try:
            from meme_generator import load_memes

            meme_dirs: list[Path] = []
            plugin_custom_dir = Path(__file__).resolve().parents[1] / "custom_memes"
            if plugin_custom_dir.exists():
                meme_dirs.append(plugin_custom_dir)
            plugin_data_dir = Path(get_astrbot_data_path()) / "plugin_data"
            meme_emoji_dir = (
                plugin_data_dir
                / "astrbot_plugin_memelite"
                / "meme_emoji"
                / "emoji"
            )
            if meme_emoji_dir.exists():
                meme_dirs.append(meme_emoji_dir)
            default_dir = Path.home() / ".meme_generator" / "memes"
            if default_dir.exists():
                meme_dirs.append(default_dir)

            conf_dirs = self.conf.get("custom_meme_dirs") or []
            if isinstance(conf_dirs, str):
                conf_dirs = [conf_dirs]
            if isinstance(conf_dirs, list):
                for raw in conf_dirs:
                    if not raw:
                        continue
                    try:
                        p = Path(str(raw)).expanduser()
                    except Exception:
                        continue
                    if p.exists():
                        meme_dirs.append(p)

            meme_emoji_dir_key = (
                str(meme_emoji_dir.resolve()) if meme_emoji_dir.exists() else None
            )
            seen: set[str] = set()
            for d in meme_dirs:
                key = str(d.resolve())
                if key in seen:
                    continue
                seen.add(key)
                if key == meme_emoji_dir_key:
                    self._load_meme_emoji_dir(d)
                else:
                    load_memes(d)
        except Exception:
            pass

        if self.is_py_version:
            from meme_generator.download import check_resources
            from meme_generator.utils import run_sync, render_meme_list

            self.render_meme_list = render_meme_list
            self.check_resources_func = check_resources
            self.run_sync = run_sync
        else:
            from meme_generator.tools import MemeProperties, MemeSortBy, render_meme_list
            from meme_generator.resources import check_resources_in_background
            from meme_generator import Image as MemeImage
            self.render_meme_list = render_meme_list
            self.check_resources_func = check_resources_in_background
            self.MemeImage = MemeImage
        self.memes: list[Meme] = get_memes()
        self.meme_keywords = self._build_meme_keywords()
        self.meme_keyword_set = set(self.meme_keywords)

    def _load_meme_emoji_dir(self, meme_emoji_dir: Path) -> None:
        if str(meme_emoji_dir.parent) not in sys.path:
            sys.path.insert(0, str(meme_emoji_dir.parent))

        cairo_available = importlib.util.find_spec("cairo") is not None
        for init_path in sorted(meme_emoji_dir.glob("*/__init__.py")):
            folder = init_path.parent.name
            if folder == "louvre" and not cairo_available:
                logger.warning("跳过 meme_emoji/louvre：缺少 pycairo 依赖")
                continue
            digest = hashlib.sha1(str(init_path).encode("utf-8")).hexdigest()[:12]
            module_name = f"astrbot_memelite_meme_emoji_{digest}"
            if module_name in sys.modules:
                continue
            try:
                spec = importlib.util.spec_from_file_location(module_name, init_path)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
            except Exception as e:
                sys.modules.pop(module_name, None)
                logger.warning(f"加载 meme_emoji 模板失败: {folder} ({e})")

    def _build_meme_keywords(self) -> list[str]:
        keywords: list[str] = []
        seen: set[str] = set()
        for meme in self.memes:
            for name in self._meme_names(meme):
                if not name or name in seen:
                    continue
                seen.add(name)
                keywords.append(name)
        keywords.sort(key=len, reverse=True)
        return keywords

    def _meme_names(self, meme: Meme) -> list[str]:
        names = [meme.key]
        names.extend(meme.keywords if self.is_py_version else meme.info.keywords)
        return [str(name) for name in names if name]

    def _meme_params(self, meme: Meme):
        return meme.params_type if self.is_py_version else meme.info.params

    def _meme_tags(self, meme: Meme):
        return meme.tags if self.is_py_version else meme.info.tags

    def _args_type(self, params):
        return getattr(params, "args_type", None)

    def _format_count(self, min_count: int, max_count: int) -> str:
        if min_count == max_count:
            return f"`{min_count}`"
        return f"`{min_count}` ~ `{max_count}`"

    def _format_default_texts(self, texts: list[str]) -> str:
        return "[" + "、".join(f"`{text}`" for text in texts) + "]"

    def _field_items(self, args_type) -> list[tuple[str, object]]:
        model = getattr(args_type, "args_model", None)
        fields = getattr(model, "model_fields", None) or getattr(model, "__fields__", {})
        return [(name, field_info) for name, field_info in fields.items() if name != "user_infos"]

    def _field_type_name(self, field_info) -> str:
        annotation = getattr(field_info, "annotation", None) or getattr(
            field_info, "type_", None
        )
        name = getattr(annotation, "__name__", str(annotation or "string"))
        mapping = {
            "str": "string",
            "int": "integer",
            "float": "number",
            "bool": "boolean",
        }
        return mapping.get(name, name)

    def _field_default(self, field_info):
        is_required = getattr(field_info, "is_required", None)
        if callable(is_required) and is_required():
            return None
        default = getattr(field_info, "default", None)
        if str(default) == "PydanticUndefined":
            return None
        return default

    def _field_description(self, field_info) -> str:
        return str(getattr(field_info, "description", "") or "")

    def _format_parser_option(self, option) -> str:
        names = "│".join(option.names)
        args = ""
        if option.args:
            args = " " + " ".join(f"<{arg.name}: {arg.value}>" for arg in option.args)
        help_text = f" {option.help_text}" if option.help_text else ""
        return f"{names}{args}{help_text}"

    def _option_field_names(self, args_type) -> list[str]:
        names: list[str] = []
        for name, _ in self._field_items(args_type):
            names.append(name)
        return names

    def _usage_example(self, meme: Meme) -> str:
        params = self._meme_params(meme)
        names = self._meme_names(meme)
        trigger = names[1] if len(names) > 1 else names[0]
        parts = [f"#{trigger}"]
        if params.min_images > 0:
            parts.append("@某人")
        if params.max_texts > 0:
            if params.default_texts:
                parts.append(str(params.default_texts[0]))
            else:
                parts.append("文字")
        args_type = self._args_type(params)
        options = getattr(args_type, "parser_options", []) if args_type else []
        if options:
            option = options[0]
            if option.names:
                parts.append(option.names[0])
            if option.args:
                arg = option.args[0]
                parts.append("1" if arg.value in {"int", "integer"} else arg.name)
        return " ".join(parts)

    def _font(self, size: int, bold: bool = False):
        font_dir = Path(__file__).resolve().parents[1] / "fonts"
        candidates = []
        if bold:
            candidates.extend(
                [
                    font_dir / "msyhbd.ttc",
                    Path("/mnt/c/Windows/Fonts/msyhbd.ttc"),
                ]
            )
        candidates.extend(
            [
                font_dir / "msyh.ttc",
                Path("/mnt/c/Windows/Fonts/msyh.ttc"),
                Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            ]
        )
        for path in candidates:
            if path.exists():
                try:
                    return ImageFont.truetype(str(path), size)
                except Exception:
                    continue
        return ImageFont.load_default()

    def _text_width(self, text: str, font) -> int:
        bbox = font.getbbox(text)
        return int(bbox[2] - bbox[0])

    def _wrap_text(self, text: str, font, max_width: int) -> list[str]:
        if not text:
            return [""]
        lines: list[str] = []
        for raw_line in str(text).splitlines() or [""]:
            current = ""
            for ch in raw_line:
                candidate = current + ch
                if current and self._text_width(candidate, font) > max_width:
                    lines.append(current)
                    current = ch
                else:
                    current = candidate
            lines.append(current)
        return lines

    def _draw_wrapped(
        self,
        draw: ImageDraw.ImageDraw,
        xy: tuple[int, int],
        text: str,
        font,
        fill,
        max_width: int,
        line_gap: int = 7,
    ) -> int:
        x, y = xy
        line_height = font.size + line_gap
        for line in self._wrap_text(text, font, max_width):
            draw.text((x, y), line, font=font, fill=fill)
            y += line_height
        return y

    def _render_card(
        self,
        title: str,
        sections: list[tuple[str, list[str]]],
        gallery: list[tuple[str, Path]] | None = None,
        width: int = 1180,
    ) -> bytes:
        margin = 48
        section_gap = 22
        bg = (248, 250, 252)
        card = (255, 255, 255)
        ink = (23, 31, 45)
        muted = (87, 99, 115)
        accent = (43, 101, 236)
        border = (219, 226, 235)

        title_font = self._font(38, True)
        heading_font = self._font(25, True)
        body_font = self._font(23)
        small_font = self._font(19)

        y = margin
        content_width = width - margin * 2
        y += 54
        for heading, lines in sections:
            y += section_gap + 36
            for line in lines:
                y += len(self._wrap_text(line, body_font, content_width - 40)) * 30
            y += 12

        gallery_items = gallery or []
        thumb_w = 220
        thumb_h = 170
        gap = 18
        cols = max(1, (content_width + gap) // (thumb_w + gap))
        if gallery_items:
            y += section_gap + 38
            rows = (len(gallery_items) + cols - 1) // cols
            y += rows * (thumb_h + 46 + gap)

        height = max(360, y + margin)
        image = Image.new("RGB", (width, height), bg)
        draw = ImageDraw.Draw(image)

        y = margin
        draw.rounded_rectangle(
            (margin - 18, y - 18, width - margin + 18, y + 72),
            radius=18,
            fill=card,
            outline=border,
        )
        draw.text((margin, y), title, font=title_font, fill=ink)
        y += 92

        for heading, lines in sections:
            draw.text((margin, y), heading, font=heading_font, fill=accent)
            y += 38
            for line in lines:
                y = self._draw_wrapped(
                    draw,
                    (margin + 14, y),
                    line,
                    body_font,
                    ink,
                    content_width - 28,
                )
            y += section_gap

        if gallery_items:
            draw.text((margin, y), "预设图片", font=heading_font, fill=accent)
            y += 42
            x = margin
            for index, path in gallery_items:
                try:
                    with Image.open(path) as raw:
                        thumb = raw.convert("RGB")
                    thumb.thumbnail((thumb_w, thumb_h), Image.Resampling.LANCZOS)
                except Exception:
                    continue
                draw.rounded_rectangle(
                    (x, y, x + thumb_w, y + thumb_h + 34),
                    radius=12,
                    fill=card,
                    outline=border,
                )
                tx = x + (thumb_w - thumb.width) // 2
                ty = y + (thumb_h - thumb.height) // 2
                image.paste(thumb, (tx, ty))
                label = f"{index}. {path.name}"
                draw.text((x + 12, y + thumb_h + 8), label, font=small_font, fill=muted)
                x += thumb_w + gap
                if x + thumb_w > width - margin + 2:
                    x = margin
                    y += thumb_h + 46 + gap

        out = io.BytesIO()
        image.save(out, format="JPEG", quality=88, optimize=True)
        return out.getvalue()

    def _meme_image_paths(self, meme: Meme) -> list[Path]:
        func = getattr(meme, "function", None)
        img_dir = None
        if func is not None:
            img_dir = getattr(func, "__globals__", {}).get("img_dir")
        if img_dir is None and func is not None:
            module = sys.modules.get(getattr(func, "__module__", ""))
            module_file = getattr(module, "__file__", None)
            if module_file:
                img_dir = Path(module_file).parent / "images"
        if img_dir is None:
            return []

        path = Path(img_dir)
        if not path.exists():
            return []
        suffixes = {".png", ".jpg", ".jpeg", ".webp"}

        def natural_sort_key(image_path: Path):
            parts = re.split(r"(\d+)", image_path.name.lower())
            return [
                int(part) if part.isdigit() else part
                for part in parts
            ]

        return sorted(
            [p for p in path.iterdir() if p.is_file() and p.suffix.lower() in suffixes],
            key=natural_sort_key,
        )

    def search_memes(self, query: str, limit: int = 30) -> list[Meme]:
        query = (query or "").strip().casefold()
        if not query:
            return self.memes[:limit]

        def score(meme: Meme) -> int:
            names = [name.casefold() for name in self._meme_names(meme)]
            tags = [str(tag).casefold() for tag in self._meme_tags(meme)]
            haystack = names + tags
            if query in names:
                return 0
            if any(name.startswith(query) for name in haystack):
                return 1
            if any(query in name for name in haystack):
                return 2
            return 99

        matched = [meme for meme in self.memes if score(meme) < 99]
        return sorted(matched, key=lambda meme: (score(meme), meme.key))[:limit]

    def format_meme_search_result(self, query: str, limit: int = 30) -> str:
        memes = self.search_memes(query, limit)
        if not memes:
            return f"没有找到和「{query}」相关的 meme。"

        title = f"meme搜索：{query or '前 30 个'}"
        lines = [title]
        for idx, meme in enumerate(memes, 1):
            params = self._meme_params(meme)
            names = self._meme_names(meme)
            aliases = "、".join(names[1:5]) if len(names) > 1 else "无"
            args_type = self._args_type(params)
            need_parts = []
            need_parts.append(
                f"{params.min_images}图"
                if params.min_images == params.max_images
                else f"{params.min_images}-{params.max_images}图"
            )
            need_parts.append(
                f"{params.min_texts}文"
                if params.min_texts == params.max_texts
                else f"{params.min_texts}-{params.max_texts}文"
            )
            if args_type and self._option_field_names(args_type):
                need_parts.append("参数:" + "、".join(self._option_field_names(args_type)))
            need = "、".join(need_parts) if need_parts else "无额外参数"
            lines.append(f"{idx}. {meme.key}｜别名: {aliases}｜需要: {need}")
        lines.append("详情：#meme详情 关键词；指令：#meme指令")
        return "\n".join(lines)

    def render_meme_search_image(self, query: str, limit: int = 30) -> bytes:
        memes = self.search_memes(query, limit)
        if not memes:
            sections = [("搜索结果", [f"没有找到和「{query}」相关的 meme。"])]
            return self._render_card("meme搜索", sections)

        lines = []
        for idx, meme in enumerate(memes, 1):
            params = self._meme_params(meme)
            names = self._meme_names(meme)
            aliases = "、".join(names[1:5]) if len(names) > 1 else "无"
            args_type = self._args_type(params)
            need = [
                f"{params.min_images}图"
                if params.min_images == params.max_images
                else f"{params.min_images}-{params.max_images}图",
                f"{params.min_texts}文"
                if params.min_texts == params.max_texts
                else f"{params.min_texts}-{params.max_texts}文",
            ]
            if args_type and self._option_field_names(args_type):
                need.append("参数:" + "、".join(self._option_field_names(args_type)))
            lines.append(f"{idx}. {meme.key}    别名: {aliases}    需要: {'、'.join(need)}")
        lines.append("详情：#meme详情 关键词；指令：#meme指令")
        return self._render_card(f"meme搜索：{query or '前 30 个'}", [("结果", lines)])

    def format_meme_commands(self) -> str:
        return "\n".join(
            [
                "meme相关指令",
                "#meme帮助：生成全部 meme 列表图",
                "#meme搜索 [关键词]：按 key、别名、标签搜索 meme",
                "#meme详情 <关键词>：查看参数说明和预览图",
                "#meme黑名单：查看禁用列表",
                "#禁用meme <关键词> / #启用meme <关键词>：管理禁用列表",
                "",
                "生成用法",
                "#关键词 [文字] [@用户/图片] [选项]",
                "选项兼容：-n 3、--number 3、--number=3、number=3、n=3",
                "多段文字可用竖线分隔，例如：#某meme 第一段|第二段",
                "",
                "例子",
                "#长离举牌 弈棋布势之道 -n 3",
                "#AI高手 @某人",
            ]
        )

    def render_meme_commands_image(self) -> bytes:
        sections = [
            (
                "meme相关指令",
                [
                    "#meme帮助：生成全部 meme 列表图",
                    "#meme搜索 [关键词]：按 key、别名、标签搜索 meme",
                    "#meme详情 <关键词>：查看参数说明和预设图片编号",
                    "#meme黑名单：查看禁用列表",
                    "#禁用meme <关键词> / #启用meme <关键词>：管理禁用列表",
                ],
            ),
            (
                "生成用法",
                [
                    "#关键词 [文字] [@用户/图片] [选项]",
                    "选项兼容：-n 3、--number 3、--number=3、number=3、n=3",
                    "多段文字可用竖线分隔，例如：#某meme 第一段|第二段",
                ],
            ),
            ("例子", ["#长离举牌 弈棋布势之道 -n 3", "#AI高手 @某人"]),
        ]
        return self._render_card("meme指令", sections)

    async def check_resources(self):
        if not self.conf["is_check_resources"]:
            return
        logger.info("开始检查memes资源...")
        if self.is_py_version:
            asyncio.create_task(self.check_resources_func())
        else:
            asyncio.create_task(asyncio.to_thread(self.check_resources_func))

    def find_meme(self, keyword: str) -> Meme | None:
        for meme in self.memes:
            keywords = meme.keywords if self.is_py_version else meme.info.keywords
            if keyword == meme.key or keyword in keywords:
                return meme

    def is_meme_keyword(self, meme_name: str) -> bool:
        return meme_name in self.meme_keywords

    def match_meme_keyword(self, text: str, fuzzy_match: bool) -> str | None:
        if fuzzy_match:
            # 模糊匹配：检查关键词是否在消息字符串中
            keyword = next((k for k in self.meme_keywords if k in text), None)
        else:
            # 精确匹配：检查关键词是否等于消息字符串的第一个单词
            keyword = next(
                (k for k in self.meme_keywords if k == text.split()[0]), None
            )
        return keyword

    async def render_meme_list_image(self) -> bytes | None:
        if self._meme_list_image_cache is not None:
            return self._meme_list_image_cache

        if self.is_py_version:
            meme_list = [(m, MemeProperties(labels=[])) for m in self.memes]
            def _render() -> bytes:
                return self.render_meme_list(
                    meme_list=meme_list,  # type: ignore
                    text_template="{index}.{keywords}",
                    add_category_icon=True,
                ).getvalue()

            output = await asyncio.to_thread(_render)
        else:
            meme_props = {m.key: MemeProperties() for m in self.memes}
            output = await asyncio.to_thread(
                self.render_meme_list,
                meme_properties=meme_props,
                exclude_memes=[],
                sort_by=MemeSortBy.KeywordsPinyin,
                sort_reverse=False,
                text_template="{index}. {keywords}",
                add_category_icon=True,
            )
        self._meme_list_image_cache = output
        return output

    def get_meme_info(self, keyword: str) -> tuple[str, bytes] | None:
        """
        根据关键词返回 meme 的详情
        返回 (描述文本, 预览图bytes)
        如果未找到，返回 None
        """
        meme = self.find_meme(keyword)
        if not meme:
            return None

        if self.is_py_version:
            p = meme.params_type
            keywords = meme.keywords
            tags = meme.tags
        else:
            p = meme.info.params
            keywords = meme.info.keywords
            tags = meme.info.tags

        args_type = self._args_type(p)
        lines = [f"## {meme.key}"]
        if keywords:
            lines.append("关键词：" + "、".join(f"`{keyword}`" for keyword in keywords))
        if tags:
            lines.append("标签：" + "、".join(f'"{tag}"' for tag in tags))
        lines.append(f"需要图片数目：{self._format_count(p.min_images, p.max_images)}")
        lines.append(f"需要文字数目：{self._format_count(p.min_texts, p.max_texts)}")
        if p.default_texts:
            lines.append(f"默认文字：{self._format_default_texts(p.default_texts)}")
        if args_type and self._field_items(args_type):
            lines.append("其他参数：")
            for name, field_info in self._field_items(args_type):
                lines.append(f"* `{name}`")
                desc = self._field_description(field_info)
                if desc:
                    lines.append(f"  描述：{desc}")
                lines.append(f"  类型：`{self._field_type_name(field_info)}`")
                default = self._field_default(field_info)
                if default is not None:
                    lines.append(f"  默认值：`{default}`")
        parser_options = getattr(args_type, "parser_options", []) if args_type else []
        if parser_options:
            lines.append("其他参数（命令行选项）：")
            for option in parser_options:
                lines.append(f"* {self._format_parser_option(option)}")
        lines.append(f"触发示例：{self._usage_example(meme)}")
        lines.append("预览：见下图")

        previewed = meme.generate_preview()
        image: bytes = previewed.getvalue() if isinstance(previewed, io.BytesIO) else previewed
        meme_info = "\n".join(lines)
        return meme_info, image

    def render_meme_detail_image(self, keyword: str) -> bytes | None:
        meme = self.find_meme(keyword)
        if not meme:
            return None

        p = self._meme_params(meme)
        keywords = meme.keywords if self.is_py_version else meme.info.keywords
        tags = self._meme_tags(meme)
        args_type = self._args_type(p)

        basic = []
        if keywords:
            basic.append("关键词：" + "、".join(f"`{keyword}`" for keyword in keywords))
        if tags:
            basic.append("标签：" + "、".join(f'"{tag}"' for tag in tags))
        basic.append(f"需要图片数目：{self._format_count(p.min_images, p.max_images)}")
        basic.append(f"需要文字数目：{self._format_count(p.min_texts, p.max_texts)}")
        if p.default_texts:
            basic.append(f"默认文字：{self._format_default_texts(p.default_texts)}")

        sections: list[tuple[str, list[str]]] = [("基础信息", basic)]

        if args_type and self._field_items(args_type):
            arg_lines = []
            for name, field_info in self._field_items(args_type):
                pieces = [
                    f"`{name}`",
                    f"类型 `{self._field_type_name(field_info)}`",
                ]
                default = self._field_default(field_info)
                if default is not None:
                    pieces.append(f"默认 `{default}`")
                desc = self._field_description(field_info)
                if desc:
                    pieces.append(desc)
                arg_lines.append("；".join(pieces))
            sections.append(("其他参数", arg_lines))

        parser_options = getattr(args_type, "parser_options", []) if args_type else []
        if parser_options:
            sections.append(
                (
                    "命令行选项",
                    [self._format_parser_option(option) for option in parser_options],
                )
            )

        sections.append(
            (
                "触发示例",
                [
                    self._usage_example(meme),
                    "编号说明：有 number 参数时，-n 1/2/3... 对应下方预设图片编号；0 通常表示随机或默认。",
                ],
            )
        )

        gallery = [
            (str(idx), path) for idx, path in enumerate(self._meme_image_paths(meme), 1)
        ]
        return self._render_card(f"meme详情：{meme.key}", sections, gallery)

    async def generate_meme(
        self, event: AstrMessageEvent, keyword: str
    ) -> bytes | None:
        # 匹配meme
        meme = self.find_meme(keyword)
        if not meme:
            return
        # 收集参数
        params = meme.params_type if self.is_py_version else meme.info.params
        images, texts, options = await self.collect.collect_params(
            event, params, keyword
        )

        if self.is_py_version:
            meme_images = [i[1] for i in images]
            return (
                await self.run_sync(meme)(images=meme_images, texts=texts, args=options)
            ).getvalue()
        else:
            meme_images = [self.MemeImage(name=str(i[0]), data=i[1]) for i in images]
            return await asyncio.to_thread(meme.generate, meme_images, texts, options)
