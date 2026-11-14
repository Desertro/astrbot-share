"""Cache group member role / level / custom title for prompt rendering.

Other plugins (mem_chat) read the resulting SQLite table to label who is
speaking: 「群主」「管理员」「LV12」「自定义头衔」. Everything here is
best-effort — a failed API call must never disturb the chat.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

DATA_DIR = Path(__file__).resolve().parents[2]
DB_PATH = DATA_DIR / "plugin_data" / "member_tags" / "tags.db"

SCHEMA = """
create table if not exists member_tags (
    group_id text not null,
    user_id text not null,
    name text default '',
    role text default '',
    level integer default 0,
    title text default '',
    updated_at real not null,
    primary key (group_id, user_id)
);
"""


@register("member_tags", "dsh", "缓存群成员的等级/头衔，供聊天提示词使用", "0.1.0")
class MemberTags(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.conf = config or {}
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            con = sqlite3.connect(DB_PATH, timeout=3)
            con.executescript(SCHEMA)
            con.commit()
            con.close()
        except Exception as exc:  # noqa: BLE001
            self.logger.error(f"[member_tags] schema init failed: {exc}")

    def _ttl(self) -> int:
        try:
            return max(60, int(self.conf.get("refresh_sec", 21600)))
        except (TypeError, ValueError):
            return 21600

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """Refresh the sender's cached tags when they are stale."""
        group_id = str(event.get_group_id() or "").strip()
        user_id = str(event.get_sender_id() or "").strip()
        if not group_id or not user_id:
            return
        if not self._stale(group_id, user_id):
            return
        api = getattr(event, "bot", None)
        if api is None or not hasattr(api, "call_action"):
            return
        try:
            resp = await api.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=False,
            )
        except Exception as exc:  # noqa: BLE001 - tags are cosmetic
            self.logger.debug(f"[member_tags] fetch {group_id}/{user_id} failed: {exc}")
            return
        info = resp if isinstance(resp, dict) else {}
        if not info.get("user_id"):
            return
        self._save(
            group_id,
            user_id,
            name=str(info.get("card") or info.get("nickname") or "").strip(),
            role=str(info.get("role") or "").strip(),
            level=self._as_int(info.get("level")),
            title=str(info.get("title") or "").strip(),
        )

    def _as_int(self, value) -> int:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return 0

    def _stale(self, group_id: str, user_id: str) -> bool:
        try:
            con = sqlite3.connect(DB_PATH, timeout=3)
            row = con.execute(
                "select updated_at from member_tags where group_id = ? and user_id = ?",
                (group_id, user_id),
            ).fetchone()
            con.close()
        except Exception as exc:  # noqa: BLE001 - never trade a chat reply for a tag lookup
            self.logger.warning(f"[member_tags] stale check failed: {exc}")
            return False
        return not row or time.time() - float(row[0]) > self._ttl()

    def _save(self, group_id: str, user_id: str, *, name: str, role: str, level: int, title: str) -> None:
        try:
            con = sqlite3.connect(DB_PATH, timeout=3)
            con.execute(
                "insert into member_tags (group_id, user_id, name, role, level, title, updated_at)"
                " values (?,?,?,?,?,?,?)"
                " on conflict(group_id, user_id) do update set"
                " name = excluded.name, role = excluded.role, level = excluded.level,"
                " title = excluded.title, updated_at = excluded.updated_at",
                (group_id, user_id, name, role, level, title, time.time()),
            )
            con.commit()
            con.close()
        except Exception as exc:  # noqa: BLE001
            self.logger.debug(f"[member_tags] save failed: {exc}")

    @filter.command("群身份")
    async def cmd_tags(self, event: AstrMessageEvent):
        """#群身份 —— 看看这个群里已经记下谁是什么身份"""
        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            yield event.plain_result("这个只在群里看。")
            return
        try:
            con = sqlite3.connect(DB_PATH, timeout=3)
            rows = con.execute(
                "select name, user_id, role, level, title from member_tags"
                " where group_id = ? order by level desc limit 15",
                (group_id,),
            ).fetchall()
            con.close()
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"读不到：{exc}")
            return
        if not rows:
            yield event.plain_result("还没记到人，等大家说几句话。")
            return
        role_cn = {"owner": "群主", "admin": "管理员"}
        lines = []
        for name, uid, role, level, title in rows:
            bits = [role_cn.get(role, "群员")]
            if level:
                bits.append(f"LV{level}")
            if title:
                bits.append(title)
            lines.append(f"{name or uid}（{'·'.join(bits)}）")
        yield event.plain_result("记到的：\n" + "\n".join(lines))
