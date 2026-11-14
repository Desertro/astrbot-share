"""Render private-chat clocks and persisted message times in China Standard Time."""

import math
import time
from datetime import datetime, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
UNKNOWN_TIME = "时间未知"


def format_chat_time(timestamp: object) -> str:
    """Format a Unix timestamp without inventing dates for legacy records.

    Args:
        timestamp: Persisted Unix seconds, or an absent/invalid legacy value.

    Returns:
        An absolute local timestamp, or an explicit unknown-time marker.
    """
    try:
        value = float(timestamp)
        if isinstance(timestamp, bool) or not math.isfinite(value) or value <= 0:
            return UNKNOWN_TIME
        return datetime.fromtimestamp(value, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return UNKNOWN_TIME


def build_private_time_context(
    history: list[dict] | None = None, *, now: float | None = None
) -> str:
    """Describe the live clock and the latest reliably dated private turns.

    Args:
        history: Saved conversation rows, including the current input batch.
        now: Optional Unix clock for deterministic validation.

    Returns:
        A system-only time reference independent of historical dialogue text.
    """
    now = time.time() if now is None else now
    current = datetime.fromtimestamp(now, SHANGHAI)
    weekday = "一二三四五六日"[current.weekday()]
    hour = current.hour
    period = (
        "凌晨"
        if hour < 6
        else "早上"
        if hour < 9
        else "上午"
        if hour < 12
        else "中午"
        if hour < 14
        else "下午"
        if hour < 18
        else "晚上"
    )
    lines = [
        "【私聊时间参考】",
        f"当前北京时间：{format_chat_time(now)}，星期{weekday}，{period}。",
        "时区：Asia/Shanghai（UTC+08:00）。这是本轮请求的系统时钟，优先于历史中的时间说法。",
    ]
    visible = [
        row
        for row in history or []
        if isinstance(row, dict) and not row.get("_no_reply")
    ]
    for role, label in [("user", "最近一条用户消息"), ("assistant", "上次机器人回复")]:
        row = next(
            (item for item in reversed(visible) if item.get("role") == role), None
        )
        if row is None:
            continue
        stamp = row.get("_timestamp")
        formatted = format_chat_time(stamp)
        line = f"{label}时间：{formatted}"
        if formatted != UNKNOWN_TIME:
            elapsed = now - float(stamp)
            if elapsed >= 0:
                seconds = int(elapsed)
                days, remainder = divmod(seconds, 86400)
                hours, remainder = divmod(remainder, 3600)
                minutes = remainder // 60
                gap = (
                    f"{days}天{hours}小时"
                    if days
                    else f"{hours}小时{minutes}分钟"
                    if hours
                    else f"{minutes}分钟"
                    if minutes
                    else f"{seconds}秒"
                )
                line += f"（距今约{gap}）"
                date_gap = (
                    current.date()
                    - datetime.fromtimestamp(float(stamp), SHANGHAI).date()
                ).days
                if date_gap > 0:
                    line += f"；已跨过{date_gap}个自然日"
            else:
                line += "（记录时间晚于当前时钟，不据此推断间隔）"
        lines.append(line + "。")
    lines.extend(
        [
            "历史消息前的[消息时间 …]是系统元数据，不是对方说的话，也不要复制到回复正文。",
            "历史中的‘今天/昨天/明天/今晚/刚才’按那条消息的日期理解；没有时间的旧记录只作背景，不能断定发生在今天。",
            "已经跨日或相隔较久时，不默认旧约定、晚安、睡觉或正在做的事仍在进行；结合最新消息判断。",
            "只在时间与话题有关时自然使用，不必每轮报时或重复问候，不凭时间推断用户刚醒或尚未睡觉。",
        ]
    )
    return "\n".join(lines)
