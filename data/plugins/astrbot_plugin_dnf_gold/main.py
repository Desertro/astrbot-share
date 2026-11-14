import asyncio
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

try:
    import aiohttp
except Exception:
    aiohttp = None

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    Image = None
    ImageDraw = None
    ImageFont = None

from astrbot.api import star
from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import LogManager
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.utils.io import save_temp_img
from astrbot.core.utils.t2i.local_strategy import FontManager


_LOG = LogManager.GetLogger("dnf_gold")


@dataclass
class PluginConfig:
    default_zone: str
    broadcast_hour: int
    broadcast_enabled: bool
    broadcast_groups: list[str]
    use_node_project: bool


@dataclass
class HistoryRange:
    days: int
    label: str


def _default_config() -> PluginConfig:
    return PluginConfig(
        default_zone="K3A",
        broadcast_hour=8,
        broadcast_enabled=True,
        broadcast_groups=[],
        use_node_project=False,
    )


def _config_path() -> str:
    base = get_astrbot_data_path()
    cfg_dir = os.path.join(base, "config")
    os.makedirs(cfg_dir, exist_ok=True)
    return os.path.join(cfg_dir, "astrbot_plugin_dnf_gold_config.json")


def _load_config() -> PluginConfig:
    path = _config_path()
    cfg = _default_config()
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
            cfg.default_zone = str(data.get("default_zone") or cfg.default_zone)
            cfg.broadcast_hour = int(data.get("broadcast_hour") or cfg.broadcast_hour)
            cfg.broadcast_enabled = bool(
                data.get("broadcast_enabled") if "broadcast_enabled" in data else cfg.broadcast_enabled
            )
            groups = data.get("broadcast_groups")
            if isinstance(groups, list):
                cfg.broadcast_groups = [str(g) for g in groups]
            if "use_node_project" in data:
                cfg.use_node_project = bool(data.get("use_node_project"))
    except Exception as e:
        _LOG.warning(f"[dnf_gold] load config failed: {e}")
    try:
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "default_zone": cfg.default_zone,
                        "broadcast_hour": cfg.broadcast_hour,
                        "broadcast_enabled": cfg.broadcast_enabled,
                        "broadcast_groups": cfg.broadcast_groups,
                        "use_node_project": cfg.use_node_project,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
    except Exception as e:
        _LOG.warning(f"[dnf_gold] write default config failed: {e}")
    return cfg


ZONE_LABELS: dict[str, str] = {
    "K1A": "跨一",
    "K2A": "跨二",
    "K3A": "跨三A",
    "K4A": "跨四",
    "K5A": "跨五",
    "K6A": "跨六",
    "K7A": "跨七",
    "K8A": "跨八",
    "K3B": "跨三B",
}


@register("dnf_gold", "desertro", "DNF 金价查询与走势图插件", "0.1.0", "")
class Main(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.context = context
        self.config = _load_config()
        self._node_project_root = os.path.join(os.path.expanduser("~"), "astrbot-dnf-gold")
        self._group_platforms: dict[str, str] = {}
        self._last_broadcast_date: str | None = None
        self._last_auto_sample_ts_ms: int | None = None
        self._last_backup_date: str | None = None
        self._last_cleanup_date: str | None = None
        self._db_lock = asyncio.Lock()
        self._auto_sample_lock = asyncio.Lock()
        self._init_db()
        self._migrate_json_history()
        self._load_auto_sample_ts()
        asyncio.create_task(self._daily_loop())

    def info(self) -> dict[str, Any]:
        return {
            "name": "dnf_gold",
            "desc": "DNF 金价查询与走势图（接入 astrbot-dnf-gold）",
            "version": "0.1.0",
            "author": "desertro",
        }

    def _render_history_chart(
        self,
        zone_label: str,
        samples: list[dict],
        collecting: bool,
        period_label: str = "近 7 天",
    ) -> str | None:
        if Image is None or ImageDraw is None or ImageFont is None:
            return None
        points: list[tuple[int, float]] = []
        for item in samples:
            ratio = item.get("ratioBuy")
            ts_ms = item.get("ts_ms")
            if not isinstance(ratio, (int, float)) or not isinstance(ts_ms, int):
                continue
            points.append((ts_ms, float(ratio)))
        if not points:
            return None
        sample_count = len(points)
        if len(points) > 240:
            last_idx = len(points) - 1
            points = [points[round(i * last_idx / 239)] for i in range(240)]
        times = [p[0] for p in points]
        values = [p[1] for p in points]
        width, height = 800, 360
        margin_x, margin_y = 60, 60
        img = Image.new("RGB", (width, height), "#f3f4f6")
        draw = ImageDraw.Draw(img)
        card_margin = 12
        card_rect = (card_margin, card_margin, width - card_margin, height - card_margin)
        try:
            draw.rounded_rectangle(
                card_rect,
                radius=16,
                fill="#ffffff",
                outline="#e5e7eb",
                width=1,
            )
        except Exception:
            draw.rectangle(card_rect, fill="#ffffff", outline="#e5e7eb", width=1)
        font = FontManager.get_font(12)
        font_value = FontManager.get_font(12)
        font_title = FontManager.get_font(16)
        min_v = min(values)
        max_v = max(values)
        if min_v == max_v:
            delta = max(abs(min_v) * 0.1, 1.0)
            min_v -= delta
            max_v += delta
        y_min = 0.0 if min_v < 10 else min_v * 0.8
        y_max = max_v * 1.05
        if y_min >= y_max:
            y_min = min_v
            y_max = max_v
        plot_w = width - margin_x * 2
        plot_h = height - margin_y * 2
        plot_left = margin_x
        plot_top = margin_y
        plot_right = margin_x + plot_w
        plot_bottom = margin_y + plot_h
        draw.rectangle(
            [plot_left, plot_top, plot_right, plot_bottom],
            fill="#f8fafc",
            outline="#e5e7eb",
            width=1,
        )
        grid_steps = 4
        for i in range(grid_steps + 1):
            gy = plot_top + plot_h * i / grid_steps
            if 0 < i < grid_steps:
                draw.line(
                    [plot_left, gy, plot_right, gy],
                    fill="#e5e7eb",
                    width=1,
                )
            tick_value = y_max - (y_max - y_min) * i / grid_steps
            tick_label = f"{tick_value:.1f}"
            try:
                tick_bbox = draw.textbbox((0, 0), tick_label, font=font)
                tick_w = tick_bbox[2] - tick_bbox[0]
                tick_h = tick_bbox[3] - tick_bbox[1]
            except Exception:
                tick_w = len(tick_label) * 7
                tick_h = 12
            draw.text(
                (plot_left - tick_w - 10, gy - tick_h / 2),
                tick_label,
                fill="#6b7280",
                font=font,
            )
            draw.line(
                [plot_left - 4, gy, plot_left, gy],
                fill="#cbd5e1",
                width=1,
            )
        for i in range(1, grid_steps):
            gx = plot_left + plot_w * i / grid_steps
            draw.line(
                [gx, plot_top, gx, plot_bottom],
                fill="#f3f4f6",
                width=1,
            )
        min_ts = min(times)
        max_ts = max(times)
        if min_ts == max_ts:
            xs = [plot_left + plot_w / 2 for _ in values]
        else:
            xs = [
                plot_left + (ts - min_ts) / (max_ts - min_ts) * plot_w
                for ts in times
            ]
        den = y_max - y_min if y_max > y_min else 1.0
        ys = [
            plot_bottom - (v - y_min) / den * plot_h
            for v in values
        ]
        if len(xs) >= 2:
            area_points: list[tuple[float, float]] = []
            area_points.append((xs[0], plot_bottom))
            for x, y in zip(xs, ys):
                area_points.append((x, y))
            area_points.append((xs[-1], plot_bottom))
            draw.polygon(area_points, fill="#ebe5ff")
        for i in range(len(values) - 1):
            draw.line(
                [xs[i], ys[i], xs[i + 1], ys[i + 1]],
                fill="#5e35b1",
                width=3,
            )
        marker_radius = 2 if len(values) > 120 else 3
        marker_step = max(1, len(values) // 120)
        for idx, (x, y) in enumerate(zip(xs, ys)):
            if idx % marker_step != 0 and idx != len(values) - 1:
                continue
            draw.ellipse(
                [x - marker_radius, y - marker_radius, x + marker_radius, y + marker_radius],
                fill="#7c3aed",
            )
        latest_v = values[-1]
        latest_x = xs[-1]
        latest_y = ys[-1]
        latest_label = f"{latest_v:.2f}"
        try:
            bbox = draw.textbbox((0, 0), latest_label, font=font_value)
            label_w = bbox[2] - bbox[0]
            label_h = bbox[3] - bbox[1]
        except Exception:
            try:
                bbox_f = font_value.getbbox(latest_label)
                label_w = bbox_f[2] - bbox_f[0]
                label_h = bbox_f[3] - bbox_f[1]
            except Exception:
                label_w = len(latest_label) * 8
                label_h = 14
        label_x = latest_x + 8
        if label_x + label_w > plot_right:
            label_x = latest_x - label_w - 8
        label_y = latest_y - label_h - 4
        if label_y < plot_top:
            label_y = latest_y + 4
        draw.text((label_x, label_y), latest_label, fill="#111827", font=font_value)
        if min_ts != max_ts:
            tick_count = 5
            for i in range(tick_count):
                frac = i / (tick_count - 1)
                t = int(min_ts + (max_ts - min_ts) * frac)
                x = plot_left + plot_w * frac
                label_fmt = "%m/%d" if max_ts - min_ts > 14 * 24 * 3600 * 1000 else "%m/%d %H:%M"
                label = datetime.fromtimestamp(t / 1000).strftime(label_fmt)
                draw.text(
                    (x - 28, plot_bottom + 8),
                    label,
                    fill="#555555",
                    font=font,
                )
        title = f"{zone_label} 金价走势（万金币/元）{period_label}"
        title_text = title
        try:
            title_bbox = draw.textbbox((0, 0), title_text, font=font_title)
            title_w = title_bbox[2] - title_bbox[0]
            title_h = title_bbox[3] - title_bbox[1]
        except Exception:
            try:
                title_bbox = font_title.getbbox(title_text)
                title_w = title_bbox[2] - title_bbox[0]
                title_h = title_bbox[3] - title_bbox[1]
            except Exception:
                title_w = len(title_text) * 10
                title_h = 20
        title_x = margin_x
        if title_x + title_w > width - margin_x - 40:
            title_x = margin_x
        title_y = 22
        draw.text((title_x, title_y), title_text, fill="#111827", font=font_title)
        subtitle_text = f"{period_label}采样 {sample_count} 条 · 单位：万金币/元"
        subtitle_y = title_y + title_h + 4
        draw.text((margin_x, subtitle_y), subtitle_text, fill="#6b7280", font=font)
        if collecting:
            draw.text(
                (margin_x, height - 26),
                "数据收集进行中",
                fill="#b45309",
                font=font,
            )
        logo_path = os.path.join(os.path.dirname(__file__), "logo.png")
        if os.path.isfile(logo_path):
            try:
                logo = Image.open(logo_path).convert("RGBA")
                logo_size = 36
                logo = logo.resize((logo_size, logo_size))
                logo_x = width - margin_x - logo_size
                logo_y = 8
                img.paste(logo, (logo_x, logo_y), logo)
            except Exception:
                pass
        return save_temp_img(img)

    def _storage_dir(self) -> str:
        base = get_astrbot_data_path()
        data_dir = os.path.join(base, "plugin_data", "dnf_gold")
        os.makedirs(data_dir, exist_ok=True)
        return data_dir

    def _db_path(self) -> str:
        return os.path.join(self._storage_dir(), "dnf_gold_history.sqlite3")

    def _backup_dir(self) -> str:
        path = os.path.join(self._storage_dir(), "backups")
        os.makedirs(path, exist_ok=True)
        return path

    def _legacy_json_path(self) -> str:
        base = os.path.dirname(__file__)
        return os.path.join(base, "dnf_gold_history.json")

    def _connect_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path(), timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        try:
            conn = self._connect_db()
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    zone_code TEXT NOT NULL,
                    ts_ms INTEGER NOT NULL,
                    ratio_buy REAL,
                    price_per_wan REAL,
                    ratio_sell REAL,
                    source TEXT
                )
                """
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_samples_zone_ts ON samples(zone_code, ts_ms)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sample_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER NOT NULL,
                    success INTEGER NOT NULL,
                    zone_code TEXT,
                    error TEXT
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_sample_log_ts ON sample_log(ts_ms)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            conn.commit()
            conn.close()
        except Exception as e:
            _LOG.warning(f"[dnf_gold] init db failed: {e}")

    def _get_meta(self, key: str) -> str | None:
        try:
            conn = self._connect_db()
            cur = conn.cursor()
            cur.execute("SELECT value FROM meta WHERE key = ?", (key,))
            row = cur.fetchone()
            conn.close()
            if row:
                return row[0]
        except Exception:
            return None
        return None

    def _set_meta(self, key: str, value: str) -> None:
        try:
            conn = self._connect_db()
            cur = conn.cursor()
            cur.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                (key, value),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            _LOG.warning(f"[dnf_gold] save meta failed: {e}")

    def _load_auto_sample_ts(self) -> None:
        val = self._get_meta("last_auto_sample_ts_ms")
        if val and val.isdigit():
            self._last_auto_sample_ts_ms = int(val)

    def _migrate_json_history(self) -> None:
        marker = self._get_meta("migrated_legacy_json")
        if marker == "1":
            return
        legacy_path = self._legacy_json_path()
        if not os.path.exists(legacy_path):
            self._set_meta("migrated_legacy_json", "1")
            return
        try:
            with open(legacy_path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            _LOG.warning(f"[dnf_gold] read legacy json failed: {e}")
            return
        zones = data.get("zones") if isinstance(data, dict) else None
        if not isinstance(zones, dict):
            self._set_meta("migrated_legacy_json", "1")
            return
        items: list[tuple] = []
        for zone_code, entries in zones.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                date_str = entry.get("date")
                ratio = entry.get("ratioBuy")
                price = entry.get("pricePerWan")
                if not date_str or not isinstance(ratio, (int, float)):
                    continue
                try:
                    dt = datetime.strptime(str(date_str), "%Y/%m/%d")
                    ts_ms = int(dt.replace(hour=12, minute=0, second=0).timestamp() * 1000)
                except Exception:
                    continue
                if price is None and isinstance(ratio, (int, float)) and ratio:
                    price = 1.0 / float(ratio)
                items.append(
                    (
                        str(zone_code),
                        ts_ms,
                        float(ratio),
                        float(price) if isinstance(price, (int, float)) else None,
                        None,
                        "legacy_json",
                    )
                )
        if not items:
            self._set_meta("migrated_legacy_json", "1")
            return
        try:
            conn = self._connect_db()
            cur = conn.cursor()
            cur.executemany(
                """
                INSERT OR IGNORE INTO samples
                (zone_code, ts_ms, ratio_buy, price_per_wan, ratio_sell, source)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                items,
            )
            conn.commit()
            conn.close()
            self._set_meta("migrated_legacy_json", "1")
        except Exception as e:
            _LOG.warning(f"[dnf_gold] migrate legacy json failed: {e}")

    async def _record_history(self, zone_code: str, report: dict, ts_ms: int | None = None) -> None:
        if not zone_code or not isinstance(report, dict):
            return
        latest = report.get("latest") or {}
        ratio_buy = latest.get("ratioBuy")
        price = latest.get("pricePerWan")
        ratio_sell = latest.get("ratioSell")
        if ratio_buy is None and isinstance(price, (int, float)) and price:
            ratio_buy = 1.0 / float(price)
        if ratio_buy is None:
            return
        if price is None and isinstance(ratio_buy, (int, float)) and ratio_buy:
            price = 1.0 / float(ratio_buy)
        if ts_ms is None:
            ts_ms = int(time.time() * 1000)
        source = report.get("source")
        try:
            async with self._db_lock:
                conn = self._connect_db()
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT OR IGNORE INTO samples
                    (zone_code, ts_ms, ratio_buy, price_per_wan, ratio_sell, source)
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        zone_code,
                        ts_ms,
                        float(ratio_buy),
                        float(price) if isinstance(price, (int, float)) else None,
                        float(ratio_sell) if isinstance(ratio_sell, (int, float)) else None,
                        str(source) if source is not None else None,
                    ),
                )
                conn.commit()
                conn.close()
        except Exception as e:
            _LOG.warning(f"[dnf_gold] record history failed: {e}")

    async def _log_sample_event(self, zone_code: str, success: bool, error: str = "") -> None:
        ts_ms = int(time.time() * 1000)
        try:
            async with self._db_lock:
                conn = self._connect_db()
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO sample_log(ts_ms, success, zone_code, error) VALUES(?, ?, ?, ?)",
                    (ts_ms, 1 if success else 0, zone_code, error),
                )
                conn.commit()
                conn.close()
        except Exception as e:
            _LOG.warning(f"[dnf_gold] log sample failed: {e}")

    def _get_recent_samples(self, zone_code: str, hours: int = 168) -> list[dict]:
        now_ms = int(time.time() * 1000)
        since_ms = now_ms - hours * 3600 * 1000
        try:
            conn = self._connect_db()
            cur = conn.cursor()
            cur.execute(
                """
                SELECT ts_ms, ratio_buy
                FROM samples
                WHERE zone_code = ? AND ts_ms >= ?
                ORDER BY ts_ms ASC
                """,
                (zone_code, since_ms),
            )
            rows = cur.fetchall()
            conn.close()
            return [{"ts_ms": int(r[0]), "ratioBuy": float(r[1])} for r in rows if r[1] is not None]
        except Exception as e:
            _LOG.warning(f"[dnf_gold] query samples failed: {e}")
            return []

    def _default_history_range(self) -> HistoryRange:
        return HistoryRange(days=7, label="近 7 天")

    def _history_range_label(self, days: int) -> str:
        days = max(1, min(365, int(days)))
        if days == 7:
            return "近 7 天"
        if days % 30 == 0 and days >= 30:
            months = days // 30
            return f"近 {months} 个月"
        return f"近 {days} 天"

    def _parse_cn_int(self, text: str) -> int | None:
        raw = str(text or "").strip()
        if not raw:
            return None
        if raw.isdigit():
            return int(raw)
        if raw in ("几", "数"):
            return 3
        mapping = {
            "一": 1,
            "二": 2,
            "两": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
        }
        if raw in mapping:
            return mapping[raw]
        if raw == "十":
            return 10
        if "十" in raw:
            left, right = raw.split("十", 1)
            tens = mapping.get(left, 1) if left else 1
            ones = mapping.get(right, 0) if right else 0
            return tens * 10 + ones
        return None

    def _parse_history_range(self, text: str | None) -> HistoryRange | None:
        if not text:
            return None
        fullwidth_map = str.maketrans("０１２３４５６７８９", "0123456789")
        compact = re.sub(r"\s+", "", str(text).strip().lower()).translate(fullwidth_map)
        if not compact:
            return None
        compact = compact.replace("最近", "近")
        compact = compact.replace("個月", "个月")
        compact = compact.replace("星期", "周")
        compact = compact.replace("礼拜", "周")
        aliases = {
            "周": 7,
            "本周": 7,
            "一周": 7,
            "近一周": 7,
            "week": 7,
            "7d": 7,
            "月": 30,
            "本月": 30,
            "一个月": 30,
            "一个月内": 30,
            "一月": 30,
            "近一月": 30,
            "month": 30,
            "30d": 30,
            "几个月": 90,
            "近几个月": 90,
            "数月": 90,
            "近数月": 90,
            "月报": 90,
            "季度": 90,
            "近季度": 90,
            "一季度": 90,
            "近一季度": 90,
            "半年": 180,
            "近半年": 180,
        }
        if compact in aliases:
            days = aliases[compact]
            return HistoryRange(days=days, label=self._history_range_label(days))
        day_match = re.fullmatch(r"(?:近)?([0-9]+|[一二两三四五六七八九十几数]+)(?:天|日|d)", compact)
        if day_match:
            days = self._parse_cn_int(day_match.group(1))
            if days:
                days = max(1, min(365, days))
                return HistoryRange(days=days, label=self._history_range_label(days))
        month_match = re.fullmatch(
            r"(?:近)?([0-9]+|[一二两三四五六七八九十几数]+)(?:个?月|m)",
            compact,
        )
        if month_match:
            months = self._parse_cn_int(month_match.group(1))
            if months:
                days = max(1, min(365, months * 30))
                return HistoryRange(days=days, label=self._history_range_label(days))
        return None

    def _parse_report_args(self, args: str | None) -> tuple[str | None, HistoryRange]:
        history_range = self._default_history_range()
        if not args:
            return None, history_range
        zone_code: str | None = None
        remaining: list[str] = []
        for token in str(args).split():
            normalized = self._normalize_zone_code(token)
            if normalized and zone_code is None:
                zone_code = normalized
            else:
                remaining.append(token)
        range_text = " ".join(remaining)
        parsed_range = self._parse_history_range(range_text)
        if parsed_range is None:
            for token in remaining:
                parsed_range = self._parse_history_range(token)
                if parsed_range:
                    break
        if parsed_range:
            history_range = parsed_range
        return zone_code, history_range

    def _build_history_summary(self, samples: list[dict], history_range: HistoryRange) -> str:
        points: list[tuple[int, float]] = []
        for item in samples:
            ratio = item.get("ratioBuy")
            ts_ms = item.get("ts_ms")
            if isinstance(ratio, (int, float)) and isinstance(ts_ms, int):
                points.append((ts_ms, float(ratio)))
        if not points:
            return f"{history_range.label}: 暂无历史样本。"
        points.sort(key=lambda x: x[0])
        if len(points) == 1:
            ts_ms, ratio = points[0]
            ts_text = datetime.fromtimestamp(ts_ms / 1000).strftime("%m/%d %H:%M")
            return f"{history_range.label}: 暂有 1 条样本，{ts_text} 为 {ratio:.2f} 万/元。"
        values = [p[1] for p in points]
        first_ts, first_v = points[0]
        last_ts, last_v = points[-1]
        date_fmt = "%m/%d" if history_range.days > 14 else "%m/%d %H:%M"
        start_text = datetime.fromtimestamp(first_ts / 1000).strftime(date_fmt)
        end_text = datetime.fromtimestamp(last_ts / 1000).strftime(date_fmt)
        delta = last_v - first_v
        pct = delta / first_v * 100 if first_v else 0.0
        if abs(delta) < 0.05:
            trend = "基本持平"
        elif delta > 0:
            trend = "走高"
        else:
            trend = "走低"
        return (
            f"{history_range.label}: {start_text} {first_v:.2f} -> {end_text} {last_v:.2f}，"
            f"{trend} {delta:+.2f} 万/元（{pct:+.1f}%）；"
            f"均值 {sum(values) / len(values):.2f}，区间 {min(values):.2f}-{max(values):.2f}。"
        )

    def _build_monthly_digest(self, samples: list[dict]) -> str:
        buckets: dict[str, list[float]] = {}
        for item in samples:
            ratio = item.get("ratioBuy")
            ts_ms = item.get("ts_ms")
            if not isinstance(ratio, (int, float)) or not isinstance(ts_ms, int):
                continue
            month = datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m")
            buckets.setdefault(month, []).append(float(ratio))
        if len(buckets) < 2:
            return ""
        parts = []
        for month in sorted(buckets)[-6:]:
            values = buckets[month]
            parts.append(f"{month}均 {sum(values) / len(values):.2f}")
        return "月均: " + " / ".join(parts)

    def _get_latest_sample_ts(self, zone_code: str) -> int | None:
        try:
            conn = self._connect_db()
            cur = conn.cursor()
            cur.execute(
                "SELECT ts_ms FROM samples WHERE zone_code = ? ORDER BY ts_ms DESC LIMIT 1",
                (zone_code,),
            )
            row = cur.fetchone()
            conn.close()
            if row:
                return int(row[0])
        except Exception:
            return None
        return None

    def _get_status_snapshot(self, zone_code: str) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "db_size": 0,
            "backup_size": 0,
            "total_samples": 0,
            "latest_ts_ms": None,
            "success_rate": None,
            "log_items": [],
        }
        db_path = self._db_path()
        if os.path.exists(db_path):
            stats["db_size"] = os.path.getsize(db_path)
        backup_dir = self._backup_dir()
        if os.path.isdir(backup_dir):
            stats["backup_size"] = sum(
                os.path.getsize(os.path.join(backup_dir, f))
                for f in os.listdir(backup_dir)
                if os.path.isfile(os.path.join(backup_dir, f))
            )
        try:
            conn = self._connect_db()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM samples WHERE zone_code = ?", (zone_code,))
            stats["total_samples"] = int(cur.fetchone()[0])
            cur.execute(
                "SELECT ts_ms FROM samples WHERE zone_code = ? ORDER BY ts_ms DESC LIMIT 1",
                (zone_code,),
            )
            row = cur.fetchone()
            stats["latest_ts_ms"] = int(row[0]) if row else None
            since_ms = int(time.time() * 1000) - 24 * 3600 * 1000
            cur.execute(
                "SELECT COUNT(*), SUM(success) FROM sample_log WHERE ts_ms >= ?",
                (since_ms,),
            )
            total, success = cur.fetchone()
            if total:
                stats["success_rate"] = float(success or 0) / float(total)
            cur.execute(
                "SELECT ts_ms, success, error FROM sample_log ORDER BY ts_ms DESC LIMIT 5"
            )
            stats["log_items"] = cur.fetchall()
            conn.close()
        except Exception as e:
            _LOG.warning(f"[dnf_gold] status query failed: {e}")
        return stats

    async def _fetch_d173_report(self, zone_code: str) -> dict | None:
        if not zone_code:
            return None
        url = "https://www.d173.cn/dnf_echarts/dnf_jb.asp"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0 Safari/537.36"
            )
        }
        raw: bytes | None = None
        try:
            if aiohttp is not None:
                async with aiohttp.ClientSession(trust_env=True) as session:
                    async with session.get(url, timeout=10, headers=headers) as resp:
                        if resp.status != 200:
                            _LOG.warning(f"[dnf_gold] d173 http status {resp.status}")
                            return None
                        raw = await resp.read()
            else:
                import urllib.request

                def _fetch() -> bytes:
                    req = urllib.request.Request(
                        url,
                        headers=headers,
                        method="GET",
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        return resp.read()

                raw = await asyncio.to_thread(_fetch)
        except Exception as e:
            _LOG.warning(f"[dnf_gold] d173 fetch failed: {e}")
            return None
        if not raw:
            _LOG.warning("[dnf_gold] d173 empty response")
            return None
        row_pattern = (
            r"<td[^>]*id=['\"]kua(?P<idx>\\d+)['\"][^>]*>(?P<label>[^<]+)</td>"
            r"\\s*<td[^>]*id=['\"]Price(?P=idx)['\"][^>]*>(?P<p1>\\d+\\.\\d+)</td>"
            r"\\s*<td[^>]*id=['\"]CPrice(?P=idx)['\"][^>]*>(?P<p2>\\d+\\.\\d+)</td>"
            r"\\s*<td[^>]*id=['\"]dtime(?P=idx)['\"][^>]*>(?P<date>\\d{4}/\\d+/\\d+)</td>"
        )
        pattern = r"跨(?P<zone>[12345678](?:A|B)?)(?P<p1>\d+\.\d+)(?P<p2>\d+\.\d+)(?P<date>\d{4}/\d+/\d+)"
        zone_key = zone_code.replace("K", "") if zone_code else zone_code
        fullwidth_map = str.maketrans(
            "０１２３４５６７８９ＡＢａｂ",
            "0123456789ABab",
        )
        parsed_labels: list[str] = []
        def _build_report(p1: float, p2: float, dt: str, history: list[dict] | None = None) -> dict | None:
            if p1 <= 0:
                return None
            price = 1.0 / p1
            latest = {
                "pricePerWan": price,
                "ratioBuy": p1,
                "ratioSell": p2,
                "date": dt,
            }
            prediction = {"pricePerWan": price * 1.01}
            return {
                "latest": latest,
                "prediction": prediction,
                "svgPath": "",
                "source": "d173",
                "history": history or [],
            }
        def _parse(text: str) -> dict | None:
            for m in re.finditer(row_pattern, text, flags=re.I):
                label = (m.group("label") or "").replace("\xa0", "").strip()
                label = label.translate(fullwidth_map)
                parsed_labels.append(label)
                norm = self._normalize_zone_code(label)
                if norm != zone_code:
                    continue
                p1 = float(m.group("p1"))
                p2 = float(m.group("p2"))
                dt = m.group("date")
                return _build_report(p1, p2, dt)
            label_map: dict[str, str] = {}
            price_map: dict[str, float] = {}
            cprice_map: dict[str, float] = {}
            date_map: dict[str, str] = {}
            zprice_map: dict[str, float] = {}
            zdate_map: dict[str, str] = {}
            qprice_map: dict[str, float] = {}
            qdate_map: dict[str, str] = {}
            for idx, label in re.findall(
                r"id=['\"]kua(\d+)['\"][^>]*>([^<]+)</td>", text, flags=re.I
            ):
                clean = label.replace("\xa0", "").strip().translate(fullwidth_map)
                label_map[idx] = clean
                parsed_labels.append(clean)
            for idx, val in re.findall(
                r"id=['\"]Price(\d+)['\"][^>]*>(\d+\.\d+)</td>", text, flags=re.I
            ):
                price_map[idx] = float(val)
            for idx, val in re.findall(
                r"id=['\"]CPrice(\d+)['\"][^>]*>(\d+\.\d+)</td>", text, flags=re.I
            ):
                cprice_map[idx] = float(val)
            for idx, val in re.findall(
                r"id=['\"]dtime(\d+)['\"][^>]*>(\d{4}/\d+/\d+)</td>", text, flags=re.I
            ):
                date_map[idx] = val
            for idx, val in re.findall(
                r"id=['\"]zPrice(\d+)['\"][^>]*>(\d+\.\d+)</td>", text, flags=re.I
            ):
                zprice_map[idx] = float(val)
            for idx, val in re.findall(
                r"id=['\"]zdtime(\d+)['\"][^>]*>(\d{4}/\d+/\d+)</td>", text, flags=re.I
            ):
                zdate_map[idx] = val
            for idx, val in re.findall(
                r"id=['\"]qPrice(\d+)['\"][^>]*>(\d+\.\d+)</td>", text, flags=re.I
            ):
                qprice_map[idx] = float(val)
            for idx, val in re.findall(
                r"id=['\"]qdtime(\d+)['\"][^>]*>(\d{4}/\d+/\d+)</td>", text, flags=re.I
            ):
                qdate_map[idx] = val
            for idx, label in label_map.items():
                norm = self._normalize_zone_code(label)
                if norm != zone_code:
                    continue
                p1 = price_map.get(idx)
                p2 = cprice_map.get(idx, p1 or 0.0)
                dt = date_map.get(idx, "")
                if p1 is None:
                    continue
                history: list[dict] = []
                qv = qprice_map.get(idx)
                qd = qdate_map.get(idx)
                if qv and qd:
                    history.append(
                        {
                            "date": qd,
                            "ratioBuy": qv,
                            "pricePerWan": 1.0 / qv,
                        }
                    )
                zv = zprice_map.get(idx)
                zd = zdate_map.get(idx)
                if zv and zd:
                    history.append(
                        {
                            "date": zd,
                            "ratioBuy": zv,
                            "pricePerWan": 1.0 / zv,
                        }
                    )
                history.append(
                    {
                        "date": dt or "",
                        "ratioBuy": p1,
                        "pricePerWan": 1.0 / p1,
                    }
                )
                report = _build_report(p1, p2, dt, history=history)
                if report:
                    return report
            for m in re.finditer(pattern, text):
                if m.group("zone") != zone_key:
                    continue
                p1 = float(m.group("p1"))
                p2 = float(m.group("p2"))
                dt = m.group("date")
                return _build_report(p1, p2, dt)
            return None
        try:
            for enc in ("utf-8", "gb18030", "gbk"):
                text = raw.decode(enc, errors="ignore")
                report = _parse(text)
                if report:
                    return report
        except Exception as e:
            _LOG.warning(f"[dnf_gold] d173 parse failed: {e}")
        _LOG.warning(
            f"[dnf_gold] d173 no match for zone {zone_code}, labels={parsed_labels[:12]}"
        )
        return None

    async def _run_node_report(self, zone_label: str) -> dict | None:
        zone_code = self._normalize_zone_code(zone_label) or self.config.default_zone
        node_report: dict | None = None
        if not self.config.use_node_project:
            fallback = await self._fetch_d173_report(zone_code)
            if fallback:
                return fallback
            return None
        if os.path.isdir(self._node_project_root):
            cmd = ["npm", "run", "astrbot:report", "--silent", "--", f"--zone={zone_label}"]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=self._node_project_root,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if stderr:
                    _LOG.debug(f"[dnf_gold] node stderr: {stderr.decode('utf-8', errors='ignore')}")
                if stdout:
                    text = stdout.decode("utf-8", errors="ignore").strip()
                    try:
                        data = json.loads(text)
                    except Exception as e:
                        _LOG.error(f"[dnf_gold] parse node json failed: {e} text={text!r}")
                    else:
                        if isinstance(data, dict) and data.get("ok"):
                            report = data.get("report")
                            if isinstance(report, dict):
                                node_report = report
                        else:
                            _LOG.error(f"[dnf_gold] node report not ok: {data}")
                else:
                    _LOG.error("[dnf_gold] node returned empty stdout")
            except Exception as e:
                _LOG.error(f"[dnf_gold] failed to start node process: {e}")
        else:
            _LOG.warning(f"[dnf_gold] node project not found at {self._node_project_root}, use d173 fallback")
        if node_report:
            return node_report
        fallback = await self._fetch_d173_report(zone_code)
        if fallback:
            return fallback
        return None

    def _normalize_zone_code(self, code: str | None) -> str | None:
        if not code:
            return None
        raw = str(code).strip().upper()
        if raw in ZONE_LABELS:
            return raw
        s = raw.replace(" ", "").replace("区", "")
        s = s.replace("夸", "跨")
        cn_to_num = {
            "一": "1",
            "二": "2",
            "三": "3",
            "四": "4",
            "五": "5",
            "六": "6",
            "七": "7",
            "八": "8",
        }
        for cn, num in cn_to_num.items():
            s = s.replace(cn, num)
        if s.startswith("跨"):
            rest = s[1:]
            if rest and rest[0] in "12345678":
                num = rest[0]
                suffix = "A"
                if len(rest) > 1 and rest[1] in "AB":
                    suffix = "B" if num == "3" and rest[1] == "B" else "A"
                code_norm = f"K{num}{suffix}"
                if code_norm in ZONE_LABELS:
                    return code_norm
        if s.startswith("K"):
            rest = s[1:]
            if rest and rest[0] in "12345678":
                num = rest[0]
                suffix = "A"
                if len(rest) > 1 and rest[1] in "AB":
                    suffix = "B" if num == "3" and rest[1] == "B" else "A"
                code_norm = f"K{num}{suffix}"
                if code_norm in ZONE_LABELS:
                    return code_norm
        if s and s[0] in "12345678":
            num = s[0]
            suffix = "A"
            if len(s) > 1 and s[1] in "AB":
                suffix = "B" if num == "3" and s[1] == "B" else "A"
            code_norm = f"K{num}{suffix}"
            if code_norm in ZONE_LABELS:
                return code_norm
        return None

    def _zone_label(self, code: str | None) -> tuple[str, str]:
        normalized = self._normalize_zone_code(code) or self.config.default_zone
        label = ZONE_LABELS.get(normalized, normalized)
        return normalized, label

    async def _handle_report_for_event(
        self,
        event: AstrMessageEvent,
        zone_code: str | None,
        history_range: HistoryRange | None = None,
    ):
        history_range = history_range or self._default_history_range()
        zone_code_norm, zone_label = self._zone_label(zone_code)
        if event.get_group_id():
            gid = str(event.get_group_id())
            try:
                plat = str(event.get_platform_name() or "")
                if plat:
                    self._group_platforms[gid] = plat
            except Exception:
                pass
        report = await self._run_node_report(zone_label)
        if not report:
            yield event.plain_result("当前无法获取 DNF 金价，请稍后重试。")
            return
        await self._record_history(zone_code_norm, report)
        latest = report.get("latest") or {}
        prediction = report.get("prediction") or {}
        svg_rel = report.get("svgPath") or ""
        if not isinstance(svg_rel, str):
            svg_rel = ""
        if not self.config.use_node_project:
            svg_rel = ""
        svg_path = svg_rel
        if svg_path and not os.path.isabs(svg_path):
            svg_path = os.path.join(self._node_project_root, svg_path)
        try:
            latest_price = float(latest.get("pricePerWan"))
        except Exception:
            latest_price = latest.get("pricePerWan")
        try:
            pred_price = float(prediction.get("pricePerWan"))
        except Exception:
            pred_price = prediction.get("pricePerWan")
        ratio_buy = latest.get("ratioBuy")
        if ratio_buy is None and isinstance(latest_price, (int, float)) and latest_price:
            ratio_buy = 1.0 / latest_price
        pred_ratio = None
        if isinstance(pred_price, (int, float)) and pred_price:
            pred_ratio = 1.0 / pred_price
        ratio_text = f"{ratio_buy:.2f} 万/元" if isinstance(ratio_buy, (int, float)) else "暂无"
        pred_text = f"{pred_ratio:.2f} 万/元" if isinstance(pred_ratio, (int, float)) else "暂无"
        base_text = (
            f"区服: {zone_label}\n"
            f"当前约 {ratio_text}\n"
            f"明日预测约 {pred_text}\n"
            f"(数据来源: yxdr + d173，算法加权样本价，仅供参考)"
        )
        image_path = ""
        collecting = False
        samples = self._get_recent_samples(zone_code_norm, hours=history_range.days * 24)
        history_text = self._build_history_summary(samples, history_range)
        monthly_text = self._build_monthly_digest(samples) if history_range.days >= 30 else ""
        reply_text = base_text
        if history_text:
            reply_text = f"{reply_text}\n{history_text}"
        if monthly_text:
            reply_text = f"{reply_text}\n{monthly_text}"
        yield event.plain_result(reply_text)
        if samples:
            span_hours = (samples[-1]["ts_ms"] - samples[0]["ts_ms"]) / 3600000
            collecting = span_hours < history_range.days * 24 * 0.9
            image_path = self._render_history_chart(
                zone_label,
                samples,
                collecting,
                period_label=history_range.label,
            ) or ""
        elif svg_path and os.path.isfile(svg_path):
            image_path = svg_path
        if image_path:
            yield event.image_result(image_path)
        if collecting:
            yield event.plain_result("数据收集进行中")
        prompt = (
            "你是 DNF 金价分析助手，请基于给定数据，用简体中文给出 2-3 句分析，"
            "语气轻松但要提醒风险，不要胡编历史走势，不要输出表格：\n\n"
            f"{reply_text}\n"
        )
        yield event.request_llm(prompt=prompt)

    @filter.command("查金价")
    async def cmd_check_gold(self, event: AstrMessageEvent, args: GreedyStr):
        zone, history_range = self._parse_report_args(args)
        async for res in self._handle_report_for_event(event, zone, history_range):
            yield res

    async def _daily_loop(self):
        while True:
            try:
                await self._maybe_auto_sample()
                await self._maybe_broadcast_daily()
                await self._maybe_cleanup_history()
                await self._maybe_backup_daily()
            except Exception as e:
                _LOG.error(f"[dnf_gold] daily loop error: {e}")
            await asyncio.sleep(300)

    async def _maybe_auto_sample(self):
        async with self._auto_sample_lock:
            try:
                interval_ms = 3 * 60 * 60 * 1000
                now_ms = int(time.time() * 1000)
                last_ts = self._last_auto_sample_ts_ms
                if last_ts is None:
                    last_ts = self._get_latest_sample_ts(self.config.default_zone)
                if last_ts is not None and now_ms - last_ts < interval_ms:
                    return
                code = self.config.default_zone
                label = ZONE_LABELS.get(code, code)
                _LOG.info(f"[dnf_gold] auto sample for {label}")
                report = await self._run_node_report(label)
                if report:
                    await self._record_history(code, report, ts_ms=now_ms)
                    await self._log_sample_event(code, True)
                    self._last_auto_sample_ts_ms = now_ms
                    self._set_meta("last_auto_sample_ts_ms", str(now_ms))
                else:
                    await self._log_sample_event(code, False, "empty report")
            except Exception as e:
                await self._log_sample_event(self.config.default_zone, False, str(e))
                _LOG.warning(f"[dnf_gold] auto sample failed: {e}")

    async def _maybe_broadcast_daily(self):
        if not self.config.broadcast_enabled:
            return
        try:
            import datetime as _dt

            now = _dt.datetime.now()
            if now.hour != int(self.config.broadcast_hour):
                return
            date_str = now.strftime("%Y-%m-%d")
            if self._last_broadcast_date == date_str:
                return
            self._last_broadcast_date = date_str
        except Exception as e:
            _LOG.warning(f"[dnf_gold] time check failed: {e}")
            return
        if not self.config.broadcast_groups:
            return
        zone_code, zone_label = self._zone_label(None)
        report = await self._run_node_report(zone_label)
        if not report:
            _LOG.warning("[dnf_gold] skip daily broadcast, no report")
            return
        await self._record_history(zone_code, report)
        svg_rel = report.get("svgPath") or ""
        if not isinstance(svg_rel, str):
            svg_rel = ""
        if not self.config.use_node_project:
            svg_rel = ""
        svg_path = svg_rel
        if not os.path.isabs(svg_path):
            svg_path = os.path.join(self._node_project_root, svg_path)
        image_path = ""
        collecting = False
        samples = self._get_recent_samples(zone_code, hours=168)
        if samples:
            span_hours = (samples[-1]["ts_ms"] - samples[0]["ts_ms"]) / 3600000
            collecting = span_hours < 168
            image_path = self._render_history_chart(zone_label, samples, collecting) or ""
        elif svg_path and os.path.isfile(svg_path):
            image_path = svg_path
        latest = report.get("latest") or {}
        prediction = report.get("prediction") or {}
        try:
            latest_price = float(latest.get("pricePerWan"))
            pred_price = float(prediction.get("pricePerWan"))
        except Exception:
            latest_price = latest.get("pricePerWan")
            pred_price = prediction.get("pricePerWan")
        analysis_text = (
            f"区服 {zone_label} 今日每万金币约 {latest_price} 元，"
            f"明日简单预测约 {pred_price} 元，仅供参考，理性消费。"
        )
        if collecting:
            analysis_text = f"{analysis_text} 数据收集进行中。"
        try:
            mem_chat_meta = self.context.get_registered_star("mem_chat")
        except Exception:
            mem_chat_meta = None
        mem_chat = mem_chat_meta.star_cls if mem_chat_meta else None
        if not (mem_chat and hasattr(mem_chat, "persona_share")):
            _LOG.warning("[dnf_gold] mem_chat not available, skip persona broadcast")
            return
        mem_chat_any: Any = mem_chat
        for gid in self.config.broadcast_groups:
            gid_str = str(gid)
            plat = self._group_platforms.get(gid_str, "")
            if not plat:
                continue
            umo = f"{plat}:group:{gid_str}"
            items = [
                {
                    "id": f"dnf_gold_{zone_code}_{int(time.time())}",
                    "title": f"DNF 金价 {zone_label}",
                    "author": "dnf_gold",
                    "image_url": image_path,
                }
            ]
            try:
                await mem_chat_any.persona_share(
                    target_umo=umo,
                    items=items,
                    intro=analysis_text,
                    query=f"{zone_label} DNF 金价播报",
                )
            except Exception as e:
                _LOG.warning(f"[dnf_gold] persona_share failed for group {gid_str}: {e}")

    async def _maybe_cleanup_history(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_cleanup_date == today:
            return
        cutoff_ms = int((datetime.now() - timedelta(days=365)).timestamp() * 1000)
        try:
            async with self._db_lock:
                conn = self._connect_db()
                cur = conn.cursor()
                cur.execute("DELETE FROM samples WHERE ts_ms < ?", (cutoff_ms,))
                conn.commit()
                cur.execute("VACUUM")
                conn.commit()
                conn.close()
            self._last_cleanup_date = today
        except Exception as e:
            _LOG.warning(f"[dnf_gold] cleanup failed: {e}")

    async def _maybe_backup_daily(self):
        today = datetime.now().strftime("%Y%m%d")
        if self._last_backup_date == today:
            return
        backup_path = os.path.join(self._backup_dir(), f"dnf_gold_history_{today}.sqlite3")
        try:
            async with self._db_lock:
                conn = self._connect_db()
                backup = sqlite3.connect(backup_path)
                conn.backup(backup)
                backup.close()
                conn.close()
            self._last_backup_date = today
        except Exception as e:
            _LOG.warning(f"[dnf_gold] backup failed: {e}")

    @filter.command("查金价状态")
    async def cmd_gold_status(self, event: AstrMessageEvent):
        zone_code, zone_label = self._zone_label(None)
        stats = self._get_status_snapshot(zone_code)
        latest = (
            datetime.fromtimestamp(stats["latest_ts_ms"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
            if stats.get("latest_ts_ms")
            else "暂无"
        )
        rate = stats.get("success_rate")
        rate_text = f"{rate * 100:.2f}%" if isinstance(rate, float) else "暂无"
        log_lines = []
        for ts_ms, success, err in stats.get("log_items") or []:
            ts_text = datetime.fromtimestamp(ts_ms / 1000).strftime("%m-%d %H:%M")
            status = "成功" if success else "失败"
            extra = f" ({err})" if err else ""
            log_lines.append(f"- {ts_text} {status}{extra}")
        log_text = "\n".join(log_lines) if log_lines else "- 暂无记录"
        text = (
            f"DNF 金价状态\n"
            f"区服: {zone_label}\n"
            f"最新采样: {latest}\n"
            f"最近24小时采样成功率: {rate_text}\n"
            f"采样记录数: {stats.get('total_samples', 0)}\n"
            f"存储占用: {stats.get('db_size', 0) / 1024:.1f} KB\n"
            f"备份占用: {stats.get('backup_size', 0) / 1024:.1f} KB\n"
            f"运行日志: logs/astrbot.log (筛选 [dnf_gold])\n"
            f"最近采样日志:\n{log_text}"
        )
        yield event.plain_result(text)
