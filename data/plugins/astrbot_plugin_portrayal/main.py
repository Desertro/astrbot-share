from typing import Any

import astrbot.api.message_components as Comp
from astrbot import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

import re
import json
import os
import sqlite3


@register("astrbot_plugin_portrayal", "Zhalslar", "...", "...")
class Relationship(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        # 上下文缓存
        self.contexts_cache: dict[str, list[dict[str, str]]] = {}
        self._index_db_ready = False
        self._last_context_source = "unknown"
        self._last_query_stats: dict[str, int] = {}

    @staticmethod
    def _safe_turn_text(text: str) -> str:
        """将转录文本规整为单行，避免破坏 TURN 字段结构"""
        return (
            text.replace("\r", " ")
            .replace("\n", " ")
            .replace("|", "｜")
            .strip()
        )

    def _get_index_db_path(self) -> str:
        conf_path = str(self.conf.get("msg_index_db_path", "") or "").strip()
        if conf_path:
            return conf_path
        return os.path.join(os.path.dirname(__file__), "portrayal_msg_index.db")

    def _ensure_index_db(self) -> None:
        if self._index_db_ready:
            return
        db_path = self._get_index_db_path()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS group_msg_index (
                group_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                sender_id TEXT NOT NULL,
                sender_name TEXT NOT NULL,
                text TEXT NOT NULL,
                mentions_json TEXT NOT NULL,
                reply_to TEXT NOT NULL,
                is_command INTEGER NOT NULL,
                created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                PRIMARY KEY (group_id, message_id)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_msg_index_group_sender_mid ON group_msg_index(group_id, sender_id, message_id DESC)"
        )
        conn.commit()
        cur.close()
        conn.close()
        self._index_db_ready = True

    def _index_upsert_messages(self, group_id: str, round_messages: list[dict[str, Any]]) -> None:
        if not bool(self.conf.get("enable_msg_index", True)):
            return
        try:
            self._ensure_index_db()
            rows = []
            for msg in round_messages:
                item = self._extract_text_item(msg)
                if not item or item.get("message_id") is None:
                    continue
                rows.append(
                    (
                        str(group_id),
                        int(item["message_id"]),
                        str(item.get("sender_id", "")),
                        str(item.get("sender_name", "")),
                        str(item.get("text", "")),
                        json.dumps(item.get("mentions", []), ensure_ascii=False),
                        str(item.get("reply_to", "")),
                        1 if self._is_command_like(str(item.get("text", ""))) else 0,
                    )
                )
            if not rows:
                return
            conn = sqlite3.connect(self._get_index_db_path())
            cur = conn.cursor()
            cur.executemany(
                """
                INSERT INTO group_msg_index(
                    group_id, message_id, sender_id, sender_name, text, mentions_json, reply_to, is_command
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id, message_id) DO UPDATE SET
                    sender_id=excluded.sender_id,
                    sender_name=excluded.sender_name,
                    text=excluded.text,
                    mentions_json=excluded.mentions_json,
                    reply_to=excluded.reply_to,
                    is_command=excluded.is_command
                """,
                rows,
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logger.warning("[portrayal] msg index upsert failed: %s", e)

    def _index_load_items(
        self,
        group_id: str,
        before_mid: int | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not bool(self.conf.get("enable_msg_index", True)):
            return []
        try:
            self._ensure_index_db()
            conn = sqlite3.connect(self._get_index_db_path())
            cur = conn.cursor()
            params: list[Any] = [str(group_id)]
            sql = (
                "SELECT message_id, sender_id, sender_name, text, mentions_json, reply_to "
                "FROM group_msg_index WHERE group_id=? "
            )
            if before_mid is not None:
                sql += "AND message_id < ? "
                params.append(int(before_mid))
            sql += "ORDER BY message_id DESC LIMIT ?"
            params.append(int(limit))
            cur.execute(sql, params)
            rows = cur.fetchall()
            cur.close()
            conn.close()
            items: list[dict[str, Any]] = []
            for row in rows:
                mentions: list[str] = []
                try:
                    parsed = json.loads(str(row[4] or "[]"))
                    if isinstance(parsed, list):
                        mentions = [str(x) for x in parsed if str(x)]
                except Exception:
                    mentions = []
                items.append(
                    {
                        "message_id": int(row[0]) if str(row[0]).isdigit() else None,
                        "sender_id": str(row[1] or ""),
                        "sender_name": str(row[2] or ""),
                        "text": str(row[3] or ""),
                        "mentions": mentions,
                        "reply_to": str(row[5] or ""),
                    }
                )
            return items
        except Exception as e:
            logger.warning("[portrayal] msg index load failed: %s", e)
            return []

    def _load_contexts_from_memchat(
        self,
        group_id: str,
        target_id: str,
        target_name: str,
        target_min: int,
        target_max: int,
    ) -> list[dict[str, str]]:
        """
        第一优先层：从 mem_chat 的 memory_embeddings 中读取“文本化历史”（含图片转写），
        转为统一消息项后复用参与式上下文构建。
        """
        if not bool(self.conf.get("prefer_memchat_history", True)):
            return []
        db_path = self._get_memchat_db_path()
        if not os.path.exists(db_path):
            return []
        scan_limit = max(500, int(self.conf.get("memchat_history_scan_limit", 12000)))
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            # 使用较新的语义化历史作为主语料；若 type 字段不可靠，使用格式约束兜底过滤
            cur.execute(
                """
                SELECT content, created_at
                FROM memory_embeddings
                WHERE group_id = ?
                  AND content LIKE '%/%:%'
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (str(group_id), int(scan_limit)),
            )
            rows: list[tuple[Any, Any]] = cur.fetchall() or []
            cur.close()
            conn.close()
        except Exception as e:
            logger.warning("[portrayal] load mem_chat history failed: %s", e)
            return []
        if not rows:
            return []

        items: list[dict[str, Any]] = []
        # 反转为时间正序，便于窗口上下文自然构建
        for idx, (content, created_at) in enumerate(reversed(rows), start=1):
            parsed = self._parse_memchat_line(str(content or ""))
            if not parsed:
                continue
            # 优先使用 created_at 生成稳定排序键，不足时回退 idx
            mid = None
            try:
                mid = int(float(created_at) * 1000)
            except Exception:
                mid = None
            if mid is None:
                mid = idx
            txt = parsed["text"]
            items.append(
                {
                    "message_id": int(mid) + idx,
                    "sender_id": parsed["sender_id"],
                    "sender_name": parsed["sender_name"] or target_name,
                    "text": txt,
                    "mentions": self._extract_mentions_from_text(txt),
                    "reply_to": "",
                }
            )
        if not items:
            return []

        contexts = self._build_user_context_from_items(items, target_id, target_name)
        if not contexts:
            return []
        if len(contexts) > target_max:
            contexts = contexts[:target_max]
        # 若 mem_chat 语料中目标行偏少，允许返回不足 target_min，交由下一层补齐
        return contexts

    def _get_memchat_db_path(self) -> str:
        conf_path = str(self.conf.get("memchat_db_path", "") or "").strip()
        if conf_path:
            return conf_path
        # 默认读取同级 mem_chat 插件数据库
        return os.path.normpath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "astrbot_plugin_mem_chat",
                "mem_chat_embeddings.db",
            )
        )

    @staticmethod
    def _parse_memchat_line(content: str) -> dict[str, str] | None:
        """
        解析 mem_chat 的历史行：
        形如 `昵称/123456: 文本`
        """
        s = str(content or "").strip()
        if not s:
            return None
        m = re.match(r"^\s*(.+?)\s*/\s*(\d{5,20})\s*:\s*(.+)\s*$", s)
        if not m:
            return None
        return {
            "sender_name": m.group(1).strip(),
            "sender_id": m.group(2).strip(),
            "text": m.group(3).strip(),
        }

    @staticmethod
    def _extract_mentions_from_text(text: str) -> list[str]:
        mentions = set(re.findall(r"<at:(\d{5,20})>", str(text or "")))
        mentions.update(re.findall(r"@(\d{5,20})", str(text or "")))
        return sorted(mentions)

    @staticmethod
    def _extract_history_cursor(msg: dict[str, Any]) -> int | None:
        """
        提取用于 get_group_msg_history 翻页的游标。
        仅使用 seq 类字段，避免将 message_id 误用为 message_seq 导致翻页异常。
        """
        # 注意：部分实现的 real_seq 固定为 0，会导致回溯直接跳到最早端并提前结束。
        for key in ("message_seq", "seq"):
            raw = msg.get(key)
            if str(raw).isdigit():
                return int(raw)
        return None

    @staticmethod
    def _relation_from_affinity(affinity: float) -> str:
        if affinity >= 35:
            return "明显喜欢"
        if affinity >= 12:
            return "偏喜欢"
        if affinity <= -35:
            return "明显反感"
        if affinity <= -12:
            return "偏反感"
        return "中性"

    def _load_memchat_impression(
        self, group_id: str, target_id: str, target_name: str
    ) -> str:
        if not bool(self.conf.get("use_memchat_impression", True)):
            return ""
        gid = str(group_id or "").strip()
        uid = str(target_id or "").strip()
        if not gid or not uid:
            return ""
        db_path = self._get_memchat_db_path()
        if not os.path.exists(db_path):
            logger.warning("[portrayal] mem_chat db not found: %s", db_path)
            return ""

        lines: list[str] = []
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()

            # V3 人物印象（优先）
            cur.execute(
                """
                SELECT display_name, summary, tags_json, confidence, updated_at
                FROM impression_profiles_v3
                WHERE group_id = ? AND user_id = ?
                """,
                (gid, uid),
            )
            row = cur.fetchone()
            if row:
                disp = str(row[0] or target_name or uid).strip()
                summary = str(row[1] or "").strip()
                tags_raw = str(row[2] or "").strip()
                tags = []
                if tags_raw:
                    try:
                        arr = json.loads(tags_raw)
                        if isinstance(arr, list):
                            tags = [str(x).strip() for x in arr if str(x).strip()]
                    except Exception:
                        tags = []
                try:
                    conf = float(row[3] or 0.0)
                except Exception:
                    conf = 0.0
                if summary:
                    lines.append(f"- 画像摘要({disp}/{uid})：{summary}")
                if tags:
                    lines.append(f"- 标签：{', '.join(tags[:10])}")
                if conf > 0:
                    lines.append(f"- 置信度：{conf:.2f}")

            # 关系倾向
            cur.execute(
                """
                SELECT affinity, trust, mood_bias
                FROM affinity_state
                WHERE group_id = ? AND user_id = ?
                """,
                (gid, uid),
            )
            row2 = cur.fetchone()
            if row2:
                try:
                    affinity = float(row2[0] or 0.0)
                except Exception:
                    affinity = 0.0
                try:
                    trust = float(row2[1] or 50.0)
                except Exception:
                    trust = 50.0
                bias = str(row2[2] or "中性").strip() or "中性"
                rel = self._relation_from_affinity(affinity)
                lines.append(
                    f"- 关系倾向：{rel} (affinity={affinity:.1f}, trust={trust:.1f}, bias={bias})"
                )

            # 旧档案 canonical_text 兜底补充
            cur.execute(
                """
                SELECT canonical_text
                FROM user_profiles
                WHERE group_id = ? AND user_id = ?
                """,
                (gid, uid),
            )
            row3 = cur.fetchone()
            if row3:
                canonical = str(row3[0] or "").strip()
                if canonical:
                    lines.append(f"- 档案补充：{canonical}")

            cur.close()
            conn.close()
        except Exception as e:
            logger.warning("[portrayal] load mem_chat impression failed: %s", e)
            return ""

        if not lines:
            return ""
        text = "【mem_chat人物印象基准】\n" + "\n".join(lines)
        max_chars = max(200, int(self.conf.get("memchat_impression_max_chars", 1000)))
        if len(text) > max_chars:
            text = text[:max_chars] + "\n（基准画像已截断）"
        return text

    def _extract_text_item(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """提取一条群消息中的关键信息，仅保留有效文本消息"""
        sender = msg.get("sender") or {}
        text_parts: list[str] = []
        mentions: list[str] = []
        reply_to: str | None = None
        image_summaries: list[str] = []
        for seg in msg.get("message", []):
            seg_type = seg.get("type")
            data = seg.get("data", {})
            if seg_type == "text":
                text_parts.append(data.get("text", ""))
            elif seg_type == "at":
                qq = str(data.get("qq", "")).strip()
                if qq:
                    mentions.append(qq)
                    text_parts.append(f"<at:{qq}>")
            elif seg_type == "reply":
                rid = str(data.get("id", "")).strip()
                if rid:
                    reply_to = rid
                    text_parts.append(f"<reply:{rid}>")
            elif seg_type == "image" and bool(self.conf.get("include_image_summary", True)):
                summary = str(data.get("summary", "") or "").strip()
                # [动画表情] 基本无语义价值，默认忽略
                if summary and summary != "[动画表情]":
                    max_len = max(20, int(self.conf.get("image_summary_max_len", 200)))
                    image_summaries.append(summary[:max_len])
        text = "".join(text_parts).strip()
        if image_summaries:
            image_desc = "；".join(image_summaries)
            if text:
                text = f"{text} [图片内容: {image_desc}]"
            else:
                text = f"[图片内容: {image_desc}]"
        if not text:
            return None

        raw_mid = msg.get("message_id")
        mid = int(raw_mid) if str(raw_mid).isdigit() else None
        sender_id = str(sender.get("user_id", ""))
        sender_name = sender.get("card") or sender.get("nickname") or sender_id
        return {
            "message_id": mid,
            "sender_id": sender_id,
            "sender_name": str(sender_name),
            "text": text,
            "mentions": mentions,
            "reply_to": reply_to or "",
        }

    def _parse_turn_line(self, line: str) -> dict[str, str] | None:
        """解析 TURN 行为结构化字典"""
        if not line.startswith("TURN|"):
            return None
        data: dict[str, str] = {}
        for part in line.split("|")[1:]:
            if "=" not in part:
                continue
            key, val = part.split("=", 1)
            data[key] = val
        required = {"block", "turn", "type", "qq", "name", "text"}
        if not required.issubset(set(data.keys())):
            return None
        return data

    def _is_command_like(self, text: str) -> bool:
        s = text.strip()
        return bool(s) and (s.startswith("#") or s.startswith("/"))

    @staticmethod
    def _sanitize_render_text(text: str) -> str:
        """规整文转图输入，减少渲染器因特殊字符导致的空白图"""
        if not text:
            return ""
        s = text.replace("\x00", "")
        # 避免 markdown/html 解析把正文吞掉
        s = s.replace("<", "＜").replace(">", "＞")
        return s

    async def _render_text_card(self, text: str, scene: str) -> str | None:
        """
        渲染文本卡片：
        - 清洗非法字符
        - 限制最大长度，避免内置渲染器超长输入偶发空白
        """
        max_chars = max(600, int(self.conf.get("render_text_max_chars", 2200)))
        safe = self._sanitize_render_text(text).strip()
        if not safe:
            return None
        if len(safe) > max_chars:
            safe = self._truncate_at_boundary(
                safe,
                max_chars,
                "（内容较长，已按完整段落截断显示）",
            )
        logger.info("[portrayal] %s render_text length=%s", scene, len(safe))
        try:
            url = await self.text_to_image(safe)
            if url and str(url).strip():
                return url
            return None
        except Exception as e:
            logger.warning("[portrayal] text_to_image failed in %s: %s", scene, e)
            return None

    @staticmethod
    def _truncate_at_boundary(text: str, max_chars: int, notice: str) -> str:
        """按段落/行边界截断，避免渲染卡片出现半句话尾巴。"""
        if not text or len(text) <= max_chars:
            return text
        room = max(0, max_chars - len(notice) - 2)
        chunk = text[:room].rstrip()
        boundary = max(
            chunk.rfind("\n\n"),
            chunk.rfind("\n- "),
            chunk.rfind("\n【"),
            chunk.rfind("。"),
            chunk.rfind("；"),
            chunk.rfind("！"),
            chunk.rfind("？"),
        )
        if boundary >= max(240, int(room * 0.55)):
            chunk = chunk[: boundary + 1].rstrip()
        return f"{chunk}\n\n{notice}"

    def _limit_portrait_output(self, text: str) -> str:
        max_chars = max(800, int(self.conf.get("portrait_output_max_chars", 1500)))
        return self._truncate_at_boundary(
            text.strip(),
            max_chars,
            "（画像过长，已按完整段落压缩显示）",
        )

    @staticmethod
    def _extract_keywords(text: str) -> set[str]:
        if not text:
            return set()
        kws = set()
        for tok in re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9_]{2,}", text.lower()):
            if tok.startswith("at"):
                continue
            kws.add(tok)
        return kws

    def _score_peer_relevance(
        self,
        anchor_idx: int,
        anchor_item: dict[str, Any],
        cand_idx: int,
        cand_item: dict[str, Any],
        target_id: str,
    ) -> int:
        """为候选群友消息计算与目标发言的相关性分数"""
        score = 0
        distance = abs(cand_idx - anchor_idx)
        score += max(0, 2 - distance)
        if str(target_id) in set(cand_item.get("mentions", [])):
            score += 6
        if str(cand_item.get("sender_id", "")) in set(anchor_item.get("mentions", [])):
            score += 2
        overlap = self._extract_keywords(anchor_item.get("text", "")) & self._extract_keywords(
            cand_item.get("text", "")
        )
        score += min(3, len(overlap))
        if self._is_command_like(cand_item.get("text", "")):
            score -= 2
        return score

    def _build_dialogue_lines(
        self, contexts: list[dict], target_id: str, self_id: str
    ) -> list[str]:
        """
        将 TURN 转成 mem_chat 风格的简洁历史对话：
        - 名称/QQ: 文本
        并做总量裁剪。
        """
        turns: list[dict[str, str]] = []
        for ctx in contexts:
            line = str(ctx.get("content", "")).strip()
            item = self._parse_turn_line(line)
            if item:
                turns.append(item)
        if not turns:
            return []

        max_total = max(20, int(self.conf.get("dialogue_total_line_limit", 800)))
        max_target = max(10, int(self.conf.get("dialogue_target_line_limit", 400)))
        max_peer = max(10, int(self.conf.get("dialogue_peer_line_limit", 400)))
        cmd_limit = max(0, int(self.conf.get("dialogue_command_line_limit", 24)))

        target_cnt = 0
        peer_cnt = 0
        cmd_cnt = 0
        compact: list[str] = []
        for t in turns:
            qq = t["qq"]
            is_target = qq == str(target_id)
            is_self = qq == str(self_id)
            if is_self:
                continue
            text = t["text"].strip()
            if not text:
                continue
            if is_target and target_cnt >= max_target:
                continue
            if (not is_target) and peer_cnt >= max_peer:
                continue
            if self._is_command_like(text):
                if cmd_cnt >= cmd_limit:
                    continue
                cmd_cnt += 1
            name = t["name"]
            compact.append(f"- {name}/{qq}: {text}")
            if is_target:
                target_cnt += 1
            else:
                peer_cnt += 1

        if len(compact) > max_total:
            compact = compact[-max_total:]
        return compact

    async def _build_current_lines(
        self,
        event: AiocqhttpMessageEvent,
        target_id: str,
        target_name: str,
    ) -> list[str]:
        """
        构建“当前消息片段”：
        从最新一轮群聊消息中提取最近若干行，作为当前语境（相对历史画像更高优先级）。
        """
        try:
            payloads = {
                "group_id": event.get_group_id(),
                "message_seq": 0,
                "count": max(10, int(self.conf.get("current_context_scan_count", 40))),
                "reverseOrder": True,
            }
            result: dict = await event.bot.api.call_action(
                "get_group_msg_history", **payloads
            )
            msgs = result.get("messages") or []
        except Exception as e:
            logger.warning("[portrayal] get current context failed: %s", e)
            return []

        items: list[dict[str, Any]] = []
        for msg in msgs:
            item = self._extract_text_item(msg)
            if item:
                items.append(item)
        if not items:
            return []

        items.sort(key=lambda x: x["message_id"] if x["message_id"] is not None else -1)
        line_limit = max(4, int(self.conf.get("current_context_line_limit", 12)))
        chosen = items[-line_limit:]

        out: list[str] = []
        self_id = str(event.get_self_id())
        for it in chosen:
            qq = str(it.get("sender_id", ""))
            if qq == self_id and not bool(self.conf.get("current_context_keep_self", False)):
                continue
            role = "目标" if qq == str(target_id) else "群友"
            name = target_name if qq == str(target_id) else str(it.get("sender_name", qq))
            text = str(it.get("text", "")).strip()
            if not text:
                continue
            out.append(f"- {role}[{name}]/{qq}: {text}")
        return out

    def _build_user_context(
        self,
        round_messages: list[dict[str, Any]],
        target_id: str,
        target_name: str,
    ) -> list[dict[str, str]]:
        """
        构建目标用户“参与式上下文”：
        命中目标用户发言后，带上该发言前后窗口的群消息。
        """
        items: list[dict[str, Any]] = []
        for msg in round_messages:
            item = self._extract_text_item(msg)
            if item:
                items.append(item)
        return self._build_user_context_from_items(items, target_id, target_name)

    def _build_user_context_from_items(
        self,
        items: list[dict[str, Any]],
        target_id: str,
        target_name: str,
    ) -> list[dict[str, str]]:
        """基于已解析的消息项构建参与式上下文（供在线抓取与本地索引复用）。"""
        if not items:
            return []

        # 按消息 ID 近似还原时间顺序，便于窗口语义更自然
        items.sort(key=lambda x: x["message_id"] if x["message_id"] is not None else -1)

        prev_n = max(0, int(self.conf.get("context_prev_msgs", 6)))
        next_n = max(0, int(self.conf.get("context_next_msgs", 4)))
        force_prev = max(0, int(self.conf.get("context_force_prev_msgs", 3)))
        peer_per_block_limit = max(1, int(self.conf.get("context_peer_per_block_limit", 8)))
        block_line_limit = max(3, int(self.conf.get("context_block_line_limit", 16)))
        anchor_limit = max(1, int(self.conf.get("context_anchor_limit", 120)))
        relevance_min = int(self.conf.get("context_relevance_min_score", 4))
        min_anchor_gap = max(0, int(self.conf.get("context_min_anchor_gap", 2)))
        target_indexes = [
            idx
            for idx, item in enumerate(items)
            if item["sender_id"] == str(target_id)
            and (not self._is_command_like(item.get("text", "")))
        ]
        # 若目标近期几乎只有命令消息，兜底保留原逻辑避免完全无数据
        if not target_indexes:
            target_indexes = [
                idx for idx, item in enumerate(items) if item["sender_id"] == str(target_id)
            ]
        if not target_indexes:
            return []

        # 目标用户高频发言时，合并过近锚点，避免重复片段和噪音膨胀
        anchor_indexes: list[int] = []
        last_anchor = -10**9
        for idx in target_indexes:
            if idx - last_anchor <= min_anchor_gap:
                continue
            anchor_indexes.append(idx)
            last_anchor = idx
        anchor_indexes = anchor_indexes[-anchor_limit:]

        contexts: list[dict[str, str]] = []
        for block_id, idx in enumerate(anchor_indexes, start=1):
            start = max(0, idx - prev_n)
            end = min(len(items) - 1, idx + next_n)
            anchor_item = items[idx]
            peer_scores: list[tuple[int, int]] = []
            for cand_idx in range(start, end + 1):
                if cand_idx == idx:
                    continue
                cand_item = items[cand_idx]
                if cand_item["sender_id"] == str(target_id):
                    continue
                if self._is_command_like(cand_item.get("text", "")):
                    continue
                s = self._score_peer_relevance(
                    idx, anchor_item, cand_idx, cand_item, str(target_id)
                )
                if s >= relevance_min:
                    peer_scores.append((cand_idx, s))
            peer_scores.sort(key=lambda x: (-x[1], abs(x[0] - idx), x[0]))
            selected_peers = {cand_idx for cand_idx, _ in peer_scores[:peer_per_block_limit]}
            forced_prev_idxs = list(range(max(start, idx - force_prev), idx))
            forced_prev_set = set(forced_prev_idxs)
            block_indexes = [
                i
                for i in range(start, end + 1)
                if i == idx or i in selected_peers or i in forced_prev_set
            ]
            if len(block_indexes) > block_line_limit:
                # 先保留“回复前N条 + 目标行”，剩余配额再按距离补充
                must_keep = sorted(set(forced_prev_idxs + [idx]))
                remain = max(0, block_line_limit - len(must_keep))
                if remain <= 0:
                    # 强制保留过多时，至少保留最接近目标的前文和目标本身
                    must_keep = sorted(set(forced_prev_idxs[-max(0, block_line_limit - 1):] + [idx]))
                    block_indexes = must_keep
                else:
                    candidates = sorted(
                        [i for i in block_indexes if i not in set(must_keep)],
                        key=lambda i: (abs(i - idx), i),
                    )
                    block_indexes = must_keep + candidates[:remain]
                block_indexes = sorted(set(block_indexes))

            contexts.append(
                {
                    "role": "user",
                    "content": f"<<<DIALOGUE_BLOCK_{block_id:03d}_START>>>",
                    "_msg_id": "",
                }
            )
            for turn_id, turn_idx in enumerate(block_indexes, start=1):
                item = items[turn_idx]
                is_target = item["sender_id"] == str(target_id)
                speaker_type = "target" if is_target else "peer"
                role_tag = "目标" if is_target else "群友"
                speaker = target_name if is_target else item["sender_name"]
                safe_speaker = self._safe_turn_text(str(speaker))
                safe_text = self._safe_turn_text(item["text"])
                contexts.append(
                    {
                        "role": "user",
                        "content": (
                            f"TURN|block={block_id:03d}|turn={turn_id:02d}|"
                            f"type={speaker_type}|qq={item['sender_id']}|"
                            f"name={role_tag}[{safe_speaker}]|"
                            f"reply={item.get('reply_to', '')}|text={safe_text}"
                        ),
                        "_msg_id": str(item["message_id"] or ""),
                    }
                )
            contexts.append(
                {
                    "role": "user",
                    "content": f"<<<DIALOGUE_BLOCK_{block_id:03d}_END>>>",
                    "_msg_id": "",
                }
            )
        return contexts

    def _load_contexts_from_index(
        self,
        group_id: str,
        target_id: str,
        target_name: str,
        target_min: int,
        target_max: int,
    ) -> list[dict[str, str]]:
        if not bool(self.conf.get("enable_msg_index", True)):
            return []
        before_mid: int | None = None
        batch = max(200, int(self.conf.get("index_query_batch_size", 1200)))
        max_rounds = max(1, int(self.conf.get("index_query_max_rounds", 120)))
        contexts: list[dict[str, str]] = []
        seen_msg_ids: set[str] = set()
        rounds = 0
        while len(contexts) < target_min and rounds < max_rounds:
            items = self._index_load_items(group_id, before_mid, batch)
            if not items:
                break
            mids = [int(i["message_id"]) for i in items if str(i.get("message_id")).isdigit()]
            if not mids:
                break
            batch_contexts = self._build_user_context_from_items(items, target_id, target_name)
            for ctx in batch_contexts:
                msg_id = str(ctx.pop("_msg_id", ""))
                if msg_id and msg_id in seen_msg_ids:
                    continue
                if msg_id:
                    seen_msg_ids.add(msg_id)
                contexts.append(ctx)
            next_before = min(mids)
            if before_mid is not None and next_before >= before_mid:
                break
            before_mid = next_before
            rounds += 1
        if len(contexts) > target_max:
            contexts = contexts[:target_max]
        return contexts

    def _index_load_target_items(
        self,
        group_id: str,
        target_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not bool(self.conf.get("enable_msg_index", True)):
            return []
        try:
            self._ensure_index_db()
            conn = sqlite3.connect(self._get_index_db_path())
            cur = conn.cursor()
            cur.execute(
                """
                SELECT message_id, sender_id, sender_name, text, mentions_json, reply_to
                FROM group_msg_index
                WHERE group_id=? AND sender_id=?
                ORDER BY message_id DESC
                LIMIT ?
                """,
                (str(group_id), str(target_id), int(limit)),
            )
            rows = cur.fetchall() or []
            cur.close()
            conn.close()
            items: list[dict[str, Any]] = []
            for row in rows:
                txt = str(row[3] or "").strip()
                if not txt or self._is_command_like(txt):
                    continue
                mentions: list[str] = []
                try:
                    parsed = json.loads(str(row[4] or "[]"))
                    if isinstance(parsed, list):
                        mentions = [str(x) for x in parsed if str(x)]
                except Exception:
                    mentions = []
                items.append(
                    {
                        "message_id": int(row[0]) if str(row[0]).isdigit() else None,
                        "sender_id": str(row[1] or ""),
                        "sender_name": str(row[2] or ""),
                        "text": txt,
                        "mentions": mentions,
                        "reply_to": str(row[5] or ""),
                    }
                )
            return items
        except Exception as e:
            logger.warning("[portrayal] msg index target load failed: %s", e)
            return []

    def _load_target_items_from_memchat(
        self, group_id: str, target_id: str, target_name: str, limit: int
    ) -> list[dict[str, Any]]:
        if not bool(self.conf.get("prefer_memchat_history", True)):
            return []
        db_path = self._get_memchat_db_path()
        if not os.path.exists(db_path):
            return []
        scan_limit = max(200, int(limit))
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                """
                SELECT content, created_at
                FROM memory_embeddings
                WHERE group_id = ?
                  AND content LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (str(group_id), f"%/{target_id}:%", int(scan_limit)),
            )
            rows: list[tuple[Any, Any]] = cur.fetchall() or []
            cur.close()
            conn.close()
        except Exception as e:
            logger.warning("[portrayal] load mem_chat target history failed: %s", e)
            return []
        items: list[dict[str, Any]] = []
        for idx, (content, created_at) in enumerate(rows, start=1):
            parsed = self._parse_memchat_line(str(content or ""))
            if not parsed:
                continue
            txt = str(parsed.get("text", "")).strip()
            if not txt or self._is_command_like(txt):
                continue
            mid: int
            try:
                mid = int(float(created_at) * 1000)
            except Exception:
                mid = idx
            items.append(
                {
                    "message_id": int(mid) + idx,
                    "sender_id": str(target_id),
                    "sender_name": parsed.get("sender_name") or target_name,
                    "text": txt,
                    "mentions": self._extract_mentions_from_text(txt),
                    "reply_to": "",
                }
            )
        return items

    def _count_target_items_from_memchat(self, group_id: str, target_id: str) -> int:
        if not bool(self.conf.get("prefer_memchat_history", True)):
            return 0
        db_path = self._get_memchat_db_path()
        if not os.path.exists(db_path):
            return 0
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COUNT(*)
                FROM memory_embeddings
                WHERE group_id = ?
                  AND content LIKE ?
                """,
                (str(group_id), f"%/{target_id}:%"),
            )
            row = cur.fetchone()
            cur.close()
            conn.close()
            return int(row[0]) if row and str(row[0]).isdigit() else 0
        except Exception as e:
            logger.warning("[portrayal] count mem_chat target history failed: %s", e)
            return 0

    def _build_target_only_contexts(
        self, target_items: list[dict[str, Any]], target_name: str
    ) -> list[dict[str, str]]:
        if not target_items:
            return []
        # 输入通常是“新到旧”，转成“旧到新”后写 TURN，便于模型按时间理解。
        ordered = sorted(
            target_items,
            key=lambda x: int(x.get("message_id") or 0),
        )
        contexts: list[dict[str, str]] = []
        for i, item in enumerate(ordered, start=1):
            qq = str(item.get("sender_id", "")).strip()
            txt = self._safe_turn_text(str(item.get("text", "")).strip())
            if not qq or not txt:
                continue
            speaker = self._safe_turn_text(str(item.get("sender_name", "") or target_name))
            contexts.append(
                {
                    "role": "user",
                    "content": (
                        f"TURN|block={i:03d}|turn=01|"
                        f"type=target|qq={qq}|name=目标[{speaker}]|"
                        f"reply={item.get('reply_to', '')}|text={txt}"
                    ),
                    "_msg_id": str(item.get("message_id") or ""),
                }
            )
        return contexts

    async def get_msg_contexts(
        self,
        event: AiocqhttpMessageEvent,
        target_id: str,
        target_name: str,
        max_query_rounds: int,
    ) -> tuple[list[dict], int]:
        """索引优先 + 在线补齐：从最新消息向过去回溯，尽量凑够目标参与式上下文。"""
        self._last_context_source = "online"
        self._last_query_stats = {
            "prefill_contexts": 0,
            "online_contexts": 0,
            "online_rounds": 0,
        }
        group_id = event.get_group_id()
        query_rounds = 0
        message_seq = 0
        contexts: list[dict] = []
        seen_msg_ids: set[str] = set()
        seen_effective_ids: set[str] = set()
        target_items: list[dict[str, Any]] = []
        target_uid = str(target_id)
        target_base = max(1, int(self.conf.get("context_target_count", 400)))
        target_tol = max(0, int(self.conf.get("context_target_tolerance", 10)))
        context_target_min = max(1, target_base - target_tol)
        context_target_max = target_base + target_tol
        effective_target = max(1, int(self.conf.get("max_msg_count", target_base)))
        target_scan_limit = max(
            effective_target,
            int(self.conf.get("target_msg_scan_limit", effective_target * 6)),
        )
        memchat_target_only_first = bool(self.conf.get("memchat_target_only_first", True))

        def add_target_item(item: dict[str, Any]) -> int:
            if str(item.get("sender_id", "")).strip() != target_uid:
                return 0
            txt = str(item.get("text", "")).strip()
            if not txt or self._is_command_like(txt):
                return 0
            eff_id = str(item.get("message_id") or "").strip()
            if not eff_id:
                eff_id = f"{item.get('sender_id','')}|{txt}"
            if eff_id in seen_effective_ids:
                return 0
            seen_effective_ids.add(eff_id)
            target_items.append(item)
            return 1

        memchat_target_items = self._load_target_items_from_memchat(
            str(group_id), target_uid, target_name, target_scan_limit
        )
        for item in memchat_target_items:
            add_target_item(item)
        if memchat_target_only_first:
            memchat_target_total = self._count_target_items_from_memchat(
                str(group_id), target_uid
            )
            # 先判断“目标本人消息量”是否足够；足够则直接用目标本人语料构造 400±10，避免被在线回溯卡住。
            if memchat_target_total >= context_target_min and len(target_items) >= context_target_min:
                self._last_context_source = "memchat_target"
                target_only_contexts = self._build_target_only_contexts(
                    target_items[:context_target_max], target_name
                )
                self._last_query_stats = {
                    "prefill_contexts": len(target_only_contexts),
                    "online_contexts": 0,
                    "online_rounds": 0,
                    "target_effective": len(target_items),
                    "target_only_added": len(target_only_contexts),
                }
                for ctx in target_only_contexts:
                    ctx.pop("_msg_id", None)
                return target_only_contexts[:context_target_max], 0
        index_target_items = self._index_load_target_items(
            str(group_id), target_uid, target_scan_limit
        )
        for item in index_target_items:
            add_target_item(item)
        effective_count = len(target_items)

        memchat_contexts = self._load_contexts_from_memchat(
            str(group_id),
            str(target_id),
            target_name,
            context_target_min,
            context_target_max,
        )
        if memchat_contexts:
            self._last_context_source = "memchat"
            contexts.extend(memchat_contexts)
            for ctx in memchat_contexts:
                mid = str(ctx.get("_msg_id", "")).strip()
                if mid:
                    seen_msg_ids.add(mid)
            if len(contexts) >= context_target_min:
                self._last_query_stats = {
                    "prefill_contexts": len(contexts),
                    "online_contexts": 0,
                    "online_rounds": 0,
                }
                for ctx in contexts:
                    ctx.pop("_msg_id", None)
                return contexts[:context_target_max], 0

        indexed_contexts = self._load_contexts_from_index(
            str(group_id),
            str(target_id),
            target_name,
            context_target_min,
            context_target_max,
        )
        if indexed_contexts:
            self._last_context_source = "index"
            contexts.extend(indexed_contexts)
            for ctx in indexed_contexts:
                mid = str(ctx.get("_msg_id", "")).strip()
                if mid:
                    seen_msg_ids.add(mid)
            if len(contexts) >= context_target_min:
                self._last_query_stats = {
                    "prefill_contexts": len(contexts),
                    "online_contexts": 0,
                    "online_rounds": 0,
                }
                for ctx in contexts:
                    ctx.pop("_msg_id", None)
                return contexts[:context_target_max], 0

        prefill_contexts = len(contexts)
        online_contexts_total = 0
        target_only_added = 0
        stall_rounds = 0
        invalid_cursor_resets = 0
        invalid_cursor_streak = 0
        while effective_count < effective_target or len(contexts) < context_target_min:
            prev_effective_count = effective_count
            prev_context_count = len(contexts)
            prev_seq = int(message_seq) if message_seq else 0
            payloads = {
                "group_id": group_id,
                "message_seq": message_seq,
                "count": self.conf["per_msg_count"],
                "reverseOrder": True,
            }
            try:
                result: dict = await event.bot.api.call_action(
                    "get_group_msg_history", **payloads
                )
            except Exception as e:
                err = str(e)
                if "不存在" in err or "retcode=1200" in err:
                    if message_seq and invalid_cursor_resets < max_query_rounds:
                        invalid_cursor_resets += 1
                        invalid_cursor_streak += 1
                        jump_base = max(1, int(self.conf.get("per_msg_count", 200)))
                        jump = jump_base * (2 ** min(invalid_cursor_streak, 6))
                        fallback_seq = max(1, int(message_seq) - jump)
                        logger.info(
                            "[portrayal] online backfill cursor invalid, jump fallback and retry. seq=%s jump=%s fallback_seq=%s error=%s",
                            message_seq,
                            jump,
                            fallback_seq,
                            err,
                        )
                        message_seq = fallback_seq
                        query_rounds += 1
                        continue
                    logger.info(
                        "[portrayal] online backfill stopped: invalid cursor seq=%s error=%s",
                        message_seq,
                        err,
                    )
                    break
                raise
            round_messages = result.get("messages") or []
            if not round_messages:
                logger.info(
                    "[portrayal] online backfill stopped: empty page. rounds=%s contexts=%s target_min=%s",
                    query_rounds,
                    len(contexts),
                    context_target_min,
                )
                break
            invalid_cursor_streak = 0
            self._index_upsert_messages(str(group_id), round_messages)
            # 回溯方向固定为“更早消息”：优先使用 history seq 游标，避免 message_id 导致的翻页卡死。
            cursors = [self._extract_history_cursor(m) for m in round_messages]
            cursors = [c for c in cursors if c is not None]
            if not cursors:
                logger.info(
                    "[portrayal] online backfill stopped: no valid cursor in page. rounds=%s contexts=%s",
                    query_rounds,
                    len(contexts),
                )
                break
            next_seq = min(cursors)
            if message_seq and next_seq >= int(message_seq):
                older = [c for c in cursors if c < int(message_seq)]
                if older:
                    next_seq = min(older)
                else:
                    # 同页重复时强制向前推进，避免卡在同一页。
                    next_seq = max(1, int(message_seq) - 1)
            else:
                # 该接口按锚点“包含当前序号”返回，向前翻页需使用更小序号。
                next_seq = max(1, int(next_seq) - 1)
            message_seq = int(next_seq)

            # 统计“有效消息”：目标用户的非空且非命令文本。
            for msg in round_messages:
                item = self._extract_text_item(msg)
                if not item:
                    continue
                effective_count += add_target_item(item)

            round_contexts = self._build_user_context(
                round_messages, target_id, target_name
            )
            round_added = 0
            for ctx in round_contexts:
                msg_id = str(ctx.pop("_msg_id", ""))
                # 同一条消息跨轮查询可能重复出现，按 message_id 去重
                if msg_id and msg_id in seen_msg_ids:
                    continue
                if msg_id:
                    seen_msg_ids.add(msg_id)
                contexts.append(ctx)
                round_added += 1
            online_contexts_total += round_added
            if round_added > 0:
                self._last_context_source = "online"
            if (
                message_seq == prev_seq
                and effective_count == prev_effective_count
                and len(contexts) == prev_context_count
            ):
                stall_rounds += 1
                if stall_rounds < 3:
                    query_rounds += 1
                    continue
                logger.info(
                    "[portrayal] online backfill stopped: no progress for %s rounds. contexts=%s target_min=%s seq=%s",
                    stall_rounds,
                    len(contexts),
                    context_target_min,
                    message_seq,
                )
                break
            stall_rounds = 0
            query_rounds += 1
            if query_rounds >= max_query_rounds:
                logger.info(
                    "[portrayal] online backfill reached max rounds=%s contexts=%s target_min=%s",
                    max_query_rounds,
                    len(contexts),
                    context_target_min,
                )
                break
        if len(contexts) < context_target_min and target_items:
            target_only_contexts = self._build_target_only_contexts(target_items, target_name)
            for ctx in target_only_contexts:
                msg_id = str(ctx.pop("_msg_id", ""))
                if msg_id and msg_id in seen_msg_ids:
                    continue
                if msg_id:
                    seen_msg_ids.add(msg_id)
                contexts.append(ctx)
                target_only_added += 1
                if len(contexts) >= context_target_max:
                    break
        if len(contexts) > context_target_max:
            # contexts 按“新到旧”累计，超出上限时保留较新的片段，满足 400±10 的目标区间。
            contexts = contexts[:context_target_max]
        self._last_query_stats = {
            "prefill_contexts": prefill_contexts,
            "online_contexts": online_contexts_total,
            "online_rounds": query_rounds,
            "target_effective": effective_count,
            "target_only_added": target_only_added,
        }
        for ctx in contexts:
            ctx.pop("_msg_id", None)
        return contexts, query_rounds

    async def get_llm_respond(
        self,
        nickname: str,
        gender: str,
        target_id: str,
        self_id: str,
        contexts: list[dict],
        current_lines: list[str] | None = None,
        memchat_impression: str = "",
    ) -> str | None:
        """调用llm回复"""
        get_using = self.context.get_using_provider()
        if not get_using:
            return None
        try:
            llm_model = str(
                self.conf.get("llm_model", "deepseek-flash")
                or "deepseek-flash"
            ).strip()
            base_prompt = self.conf["system_prompt_template"].format(
                nickname=nickname, gender=("他" if gender == "male" else "她")
            )
            # 强约束：仅允许把“目标”行当作目标本人发言，降低错归因
            system_prompt = (
                f"{base_prompt}\n\n"
                "你会收到结构化的“对话块”群聊转录。\n"
                "规则：\n"
                f"1) 目标用户QQ号是 {target_id}，只把 TURN 中 type=target 且 qq={target_id} 的发言视为目标本人。\n"
                "2) TURN 中 type=peer 或 qq!=目标QQ 的发言仅用于语境参考，不能归因给目标。\n"
                "3) 每个 <<<DIALOGUE_BLOCK_xxx_START>>> 到 <<<..._END>>> 是一个完整对话片段。\n"
                "4) 昵称可能变化或重名，身份判断必须优先用 qq 字段，不得仅凭 name 字段。\n"
                "5) 输出结论时，避免把群友观点、口癖、立场写成目标特征。\n"
                "6) 若证据不足，明确写“证据不足”，不要硬推断。\n"
                "7) 输出必须精炼，总长度控制在 900-1300 中文字以内。\n"
                "8) 固定输出 6 段：一句话印象、萌点、雷点、口癖、社交定位、剧情定位；每段 1-2 条即可。\n"
                "9) 证据只引用短句或概括，不要大段复述聊天记录。"
            )
            dialogue_lines = self._build_dialogue_lines(contexts, target_id, self_id)
            if not dialogue_lines:
                return None
            dialogue_text = "\n".join(dialogue_lines)
            preview_lines = int(self.conf.get("debug_preview_lines", 20))
            if preview_lines > 0:
                logger.info(
                    "[portrayal] dialogue preview (first %s lines):\n%s",
                    preview_lines,
                    "\n".join(dialogue_lines[:preview_lines]),
                )
                logger.info(
                    "[portrayal] system_prompt_template in use:\n%s",
                    base_prompt,
                )
            prompt_text = (
                f"【系统提示词模板】\n{base_prompt}\n\n"
                f"【任务】请基于以下历史对话，为 {nickname}({target_id}) 生成画像。\n"
                "【优先级】当前消息 > 历史消息。\n"
                "【补充约束】mem_chat人物印象基准是长期信息，若与当前消息冲突，请在输出中说明“近期变化”。\n"
                "【要求】优先根据目标用户发言判断，群友发言只作语境参考。\n"
                + (memchat_impression + "\n\n" if memchat_impression else "")
                + (
                    "【当前消息】\n" + "\n".join(current_lines) + "\n\n"
                    if current_lines
                    else ""
                )
                + "【历史对话】\n"
                + dialogue_text
            )
            llm_response = await self._get_llm_for_portrayal(
                system_prompt=system_prompt,
                prompt=prompt_text,
                model=llm_model,
            )
            return self._limit_portrait_output(llm_response) if llm_response else None

        except Exception as e:
            logger.error(f"LLM 调用失败：{e}")
            return None

    async def _get_llm_for_portrayal(
        self,
        system_prompt: str,
        prompt: str,
        model: str,
    ) -> str | None:
        """专用画像LLM获取，支持切换provider"""
        provider_id = str(self.conf.get("provider_id") or "").strip()
        try:
            if provider_id:
                prov = await self.context.provider_manager.get_provider_by_id(provider_id)
                if prov:
                    resp = await prov.text_chat(
                        system_prompt=system_prompt,
                        prompt=prompt,
                        model=model,
                    )
                    return resp.completion_text
            # Fallback to default provider
            get_using = self.context.get_using_provider()
            if not get_using:
                return None
            resp = await get_using.text_chat(
                system_prompt=system_prompt,
                prompt=prompt,
                model=model,
            )
            return resp.completion_text
        except Exception as e:
            logger.error(f"LLM 调用失败：{e}")
            return None

    async def get_llm_id(
        self,
        portrait: str,
        history_excerpt: str = "",
        current_excerpt: str = "",
        old_card: str = "",
        memchat_impression: str = "",
    ) -> tuple[str | None, str]:
        """调用 LLM 基于画像生成 ID"""
        base_system_prompt = self.conf.get(
            "id_prompt_template",
            (
                "你是起名专家。请根据用户画像生成一个简短的ID，用于群名片。"
                "要求：1) 2-36 字符；2) 允许多语言字符（无需限制语种）；3) 能体现画像关键特征。"
                "请只输出以下格式：\n新ID：**<ID>**\n理由：'<一句话理由>'\n"
                "不要输出多余内容。"
            ),
        )
        system_prompt = (
            f"{base_system_prompt}\n"
            "额外约束：\n"
            "1) 忽略并不要模仿用户当前群名片。\n"
            "2) 新ID不能与当前群名片同字、同谐音、同结构改写。\n"
            "3) 若当前群名片有中二风格，也不要沿用其关键词。"
        )
        prompt = portrait
        if history_excerpt or current_excerpt:
            prompt = (
                "请综合画像与对话语境来取名。当前消息优先，历史消息次之。\n\n"
                + (f"【当前群名片（仅用于排除，不可沿用）】\n{old_card}\n\n" if old_card else "")
                + (memchat_impression + "\n\n" if memchat_impression else "")
                + ("【当前消息】\n" + current_excerpt + "\n\n" if current_excerpt else "")
                + ("【历史消息摘录】\n" + history_excerpt + "\n\n" if history_excerpt else "")
                + "【画像】\n"
                + portrait
            )
        llm_response = await self.get_using_provider_text(system_prompt, prompt)
        if not llm_response:
            return None, "LLM响应为空"

        match = re.search(r"\*\*(.+?)\*\*", llm_response)
        raw_id = (match.group(1) if match else "").strip()
        if not raw_id:
            line_match = re.search(r"新ID\s*[:：]\s*([^\n\r]+)", llm_response)
            raw_id = (line_match.group(1) if line_match else "").strip()
        if not raw_id:
            return None, "未能从LLM回复中提取到ID"
        max_len = max(2, int(self.conf.get("id_max_len", 36)))
        cleaned = re.sub(r"[\x00-\x1f\x7f]", "", raw_id)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -*_`'\"“”‘’")
        new_id = cleaned[:max_len].strip()
        if not new_id:
            return None, "提取到的ID为空"
        if len(new_id) < 2:
            return None, "提取到的ID过短（少于2字符）"

        reason_match = re.search(r"'([^']*)'", llm_response)
        reason = reason_match.group(1) if reason_match else ""

        return new_id, reason

    async def get_using_provider_text(self, system_prompt: str, prompt: str) -> str | None:
        """调用 LLM（纯文本提示）"""
        llm_model = str(
            self.conf.get("llm_model", "deepseek-flash")
            or "deepseek-flash"
        ).strip()
        return await self._get_llm_for_portrayal(system_prompt, prompt, llm_model)

    async def get_nickname(
        self, event: AiocqhttpMessageEvent, user_id: str | int
    ) -> tuple[str, str]:
        """获取指定群友的昵称和性别"""
        all_info = await event.bot.get_group_member_info(
            group_id=int(event.get_group_id()), user_id=int(user_id)
        )
        nickname = all_info.get("card") or all_info.get("nickname")
        gender = all_info.get("sex")
        return nickname, gender

    async def get_at_id(self, event: AiocqhttpMessageEvent) -> str | None:
        return next(
            (
                str(seg.qq)
                for seg in event.get_messages()
                if (isinstance(seg, Comp.At)) and str(seg.qq) != event.get_self_id()
            ),
            None,
        )

    @filter.command("画像")
    async def get_portrayal(self, event: AiocqhttpMessageEvent):
        """
        画像 @群友 <查询轮数>
        """
        target_id: str = await self.get_at_id(event) or event.get_sender_id()
        nickname, gender = await self.get_nickname(event, target_id)
        contexts, query_rounds = None, None
        from_cache = False
        if self.contexts_cache and target_id in self.contexts_cache:
            contexts = self.contexts_cache[target_id]
            from_cache = True
        else:
            # 每轮查询200条消息，200轮查询4w条消息,几乎接近漫游极限
            end_parm = event.message_str.split(" ")[-1]
            requested_rounds = int(end_parm) if end_parm.isdigit() else 0
            hard_cap = int(self.conf.get("max_query_rounds_hard_cap", 600))
            if requested_rounds > 0:
                target_query_rounds = min(max(1, hard_cap), requested_rounds)
            else:
                target_query_rounds = max(1, hard_cap)
            target_base = int(self.conf.get("context_target_count", 400))
            target_tol = int(self.conf.get("context_target_tolerance", 10))
            yield event.plain_result(
                f"正在获取{nickname}的消息（优先本地历史，不足时再在线回溯，在线上限{target_query_rounds}轮；目标：参与式上下文约{target_base}条，允许±{target_tol}）..."
            )
            contexts, query_rounds = await self.get_msg_contexts(
                event, target_id, nickname, target_query_rounds
            )
            self.contexts_cache[target_id] = contexts
        if not contexts:
            yield event.plain_result("没有找到该群友的任何消息")
            return

        if query_rounds:
            target_base = int(self.conf.get("context_target_count", 400))
            target_tol = int(self.conf.get("context_target_tolerance", 10))
            context_target_min = max(1, target_base - target_tol)
            stats = self._last_query_stats or {}
            prefill = int(stats.get("prefill_contexts", 0))
            online_ctx = int(stats.get("online_contexts", 0))
            online_rounds = int(stats.get("online_rounds", query_rounds))
            target_effective = int(stats.get("target_effective", 0))
            target_only_added = int(stats.get("target_only_added", 0))
            suffix = (
                "（未达目标，可能受可用历史或查询轮数限制）"
                if len(contexts) < context_target_min
                else ""
            )
            detail = (
                f"（在线新增{online_ctx}条，预加载{prefill}条）"
                if prefill > 0
                else ""
            )
            target_detail = (
                f"（目标本人有效发言{target_effective}条，目标直出补齐{target_only_added}条）"
                if (target_effective > 0 or target_only_added > 0)
                else ""
            )
            yield event.plain_result(
                f"已从{online_rounds * self.conf['per_msg_count']}条群消息中获取了{len(contexts)}条参与式上下文{detail}{target_detail}（目标≈{target_base}，允许±{target_tol}）{suffix}，正在分析..."
            )
        else:
            if from_cache:
                yield event.plain_result(
                    f"已从缓存中获取了{len(contexts)}条参与式上下文，正在分析..."
                )
            elif self._last_context_source == "memchat":
                yield event.plain_result(
                    f"已从mem_chat语义历史中获取了{len(contexts)}条参与式上下文，正在分析..."
                )
            elif self._last_context_source == "memchat_target":
                yield event.plain_result(
                    f"已从mem_chat目标用户语料中获取了{len(contexts)}条参与式上下文，正在分析..."
                )
            elif self._last_context_source == "index":
                yield event.plain_result(
                    f"已从本地消息索引中获取了{len(contexts)}条参与式上下文，正在分析..."
                )
            else:
                yield event.plain_result(
                    f"已获取{len(contexts)}条参与式上下文，正在分析..."
                )

        current_lines = await self._build_current_lines(event, target_id, nickname)
        memchat_impression = self._load_memchat_impression(
            str(event.get_group_id() or ""),
            str(target_id),
            str(nickname),
        )
        try:
            llm_respond = await self.get_llm_respond(
                nickname,
                gender,
                target_id,
                str(event.get_self_id()),
                contexts,
                current_lines=current_lines,
                memchat_impression=memchat_impression,
            )
            if llm_respond:
                url = await self._render_text_card(llm_respond, "画像")
                if url:
                    yield event.image_result(url)
                else:
                    logger.warning("[portrayal] text card render failed for 画像")
                    yield event.plain_result(llm_respond[:3000])
            else:
                yield event.plain_result("LLM响应为空")
        except Exception as e:
            logger.error(f"LLM 调用失败：{e}")
            yield event.plain_result(f"分析失败:{e}")
        finally:
            self.contexts_cache.pop(target_id, None)

    @filter.command("取名")
    async def get_portrayal_name(self, event: AiocqhttpMessageEvent):
        """
        取名 @群友 <查询轮数>
        先输出画像，再基于画像生成群名片
        """
        target_id: str = await self.get_at_id(event) or event.get_sender_id()
        all_info = await event.bot.get_group_member_info(
            group_id=int(event.get_group_id()), user_id=int(target_id)
        )
        # 取名阶段优先使用QQ昵称而非群名片，降低“沿用旧名片”倾向
        nickname = all_info.get("nickname") or all_info.get("card") or str(target_id)
        gender = all_info.get("sex")
        old_card = str(all_info.get("card") or "").strip()
        contexts, query_rounds = None, None
        from_cache = False
        if self.contexts_cache and target_id in self.contexts_cache:
            contexts = self.contexts_cache[target_id]
            from_cache = True
        else:
            end_parm = event.message_str.split(" ")[-1]
            requested_rounds = int(end_parm) if end_parm.isdigit() else 0
            hard_cap = int(self.conf.get("max_query_rounds_hard_cap", 600))
            if requested_rounds > 0:
                target_query_rounds = min(max(1, hard_cap), requested_rounds)
            else:
                target_query_rounds = max(1, hard_cap)
            target_base = int(self.conf.get("context_target_count", 400))
            target_tol = int(self.conf.get("context_target_tolerance", 10))
            yield event.plain_result(
                f"正在获取{nickname}的消息（优先本地历史，不足时再在线回溯，在线上限{target_query_rounds}轮；目标：参与式上下文约{target_base}条，允许±{target_tol}）..."
            )
            contexts, query_rounds = await self.get_msg_contexts(
                event, target_id, nickname, target_query_rounds
            )
            self.contexts_cache[target_id] = contexts
        if not contexts:
            yield event.plain_result("没有找到该群友的任何消息")
            return

        if query_rounds:
            target_base = int(self.conf.get("context_target_count", 400))
            target_tol = int(self.conf.get("context_target_tolerance", 10))
            context_target_min = max(1, target_base - target_tol)
            stats = self._last_query_stats or {}
            prefill = int(stats.get("prefill_contexts", 0))
            online_ctx = int(stats.get("online_contexts", 0))
            online_rounds = int(stats.get("online_rounds", query_rounds))
            target_effective = int(stats.get("target_effective", 0))
            target_only_added = int(stats.get("target_only_added", 0))
            suffix = (
                "（未达目标，可能受可用历史或查询轮数限制）"
                if len(contexts) < context_target_min
                else ""
            )
            detail = (
                f"（在线新增{online_ctx}条，预加载{prefill}条）"
                if prefill > 0
                else ""
            )
            target_detail = (
                f"（目标本人有效发言{target_effective}条，目标直出补齐{target_only_added}条）"
                if (target_effective > 0 or target_only_added > 0)
                else ""
            )
            yield event.plain_result(
                f"已从{online_rounds * self.conf['per_msg_count']}条群消息中获取了{len(contexts)}条参与式上下文{detail}{target_detail}（目标≈{target_base}，允许±{target_tol}）{suffix}，正在分析..."
            )
        else:
            if from_cache:
                yield event.plain_result(
                    f"已从缓存中获取了{len(contexts)}条参与式上下文，正在分析..."
                )
            elif self._last_context_source == "memchat":
                yield event.plain_result(
                    f"已从mem_chat语义历史中获取了{len(contexts)}条参与式上下文，正在分析..."
                )
            elif self._last_context_source == "memchat_target":
                yield event.plain_result(
                    f"已从mem_chat目标用户语料中获取了{len(contexts)}条参与式上下文，正在分析..."
                )
            elif self._last_context_source == "index":
                yield event.plain_result(
                    f"已从本地消息索引中获取了{len(contexts)}条参与式上下文，正在分析..."
                )
            else:
                yield event.plain_result(
                    f"已获取{len(contexts)}条参与式上下文，正在分析..."
                )

        current_lines = await self._build_current_lines(event, target_id, nickname)
        memchat_impression = self._load_memchat_impression(
            str(event.get_group_id() or ""),
            str(target_id),
            str(nickname),
        )
        try:
            llm_respond = await self.get_llm_respond(
                nickname,
                gender,
                target_id,
                str(event.get_self_id()),
                contexts,
                current_lines=current_lines,
                memchat_impression=memchat_impression,
            )
            if not llm_respond:
                yield event.plain_result("LLM响应为空")
                return

            # 基于画像生成ID并设置群名片
            history_excerpt = "\n".join(
                self._build_dialogue_lines(contexts, target_id, str(event.get_self_id()))[-20:]
            )
            current_excerpt = "\n".join(current_lines[:20]) if current_lines else ""
            new_id, reason = await self.get_llm_id(
                llm_respond,
                history_excerpt=history_excerpt,
                current_excerpt=current_excerpt,
                old_card=old_card,
                memchat_impression=memchat_impression,
            )
            if not new_id:
                yield event.plain_result(f"生成失败：{reason}")
                return

            try:
                await event.bot.set_group_card(
                    group_id=int(event.get_group_id()),
                    user_id=int(target_id),
                    card=new_id,
                )
            except Exception as e:
                logger.warning(f"设置群名片失败：{e}")

            # 合并卡片：画像文本 + 理由 + 新群名片
            reason_line = reason or "（无）"
            merged_text = (
                f"【画像】\n{llm_respond}\n\n"
                f"【新群名片】\n{new_id}\n"
                f"【理由】\n{reason_line}"
            )
            url = await self._render_text_card(merged_text, "取名")
            if url:
                yield event.image_result(url)
            else:
                logger.warning("[portrayal] text card render failed for 取名")
                yield event.plain_result(merged_text[:3000])
        except Exception as e:
            logger.error(f"LLM 调用失败：{e}")
            yield event.plain_result(f"分析失败:{e}")
        finally:
            self.contexts_cache.pop(target_id, None)

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        self.contexts_cache.clear()
