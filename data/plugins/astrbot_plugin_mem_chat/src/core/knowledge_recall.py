"""Retrieve source knowledge and retain bounded conversational topic references."""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger("astrbot")


def clean_retrieval_text(text: str) -> str:
    """Remove transport metadata without changing the conversational wording.

    Args:
        text: A message or quoted message body.

    Returns:
        Plain text suitable for an embedding query.
    """
    text = re.sub(
        r"\[CQ:[^\]]*\]|\[REF:\d+\]|\[At:[^\]]*\]|\[回复\]|<at:[^>]*>|@\d{5,20}",
        " ",
        str(text or ""),
    )
    text = re.sub(r"https?://\S+|data:image/\S+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class KnowledgeHit:
    """A catalog source and the evidence for selecting it."""

    memory_id: str
    text: str
    vector: list[float]
    score: float
    current_score: float
    context_score: float
    source: str
    strong: bool = False
    aliases: tuple[str, ...] = ()


@dataclass
class KnowledgeSelection:
    """A pending selection; only injected sources may become topic references."""

    scope: str
    sender_id: str
    message_id: str
    query: str
    hits: list[KnowledgeHit] = field(default_factory=list)
    inherited: dict | None = None


class KnowledgeRecall:
    """Keep retrieval and short-lived source knowledge isolated by conversation."""

    def __init__(self, db_mgr, config: dict, *, now=None):
        """Initialize retrieval without modifying the knowledge database.

        Args:
            db_mgr: The plugin database and embedding provider.
            config: Existing plugin configuration.
            now: Optional wall-clock function for deterministic expiry checks.
        """
        self.db = db_mgr
        self.config = config
        self.now = now or time.time
        self.topics: dict[str, list[dict]] = {}

    async def recall(
        self,
        query: str,
        qemb: list[float],
        group_id: str,
        *,
        scope: str,
        sender_id: str = "",
        self_id: str = "",
        message_id: str = "",
        reply_ids: set[str] | None = None,
        limit: int = 5,
    ) -> KnowledgeSelection:
        """Merge current, recent-context and alias evidence with topic fallback.

        Args:
            query: Current user message.
            qemb: Embedding of the cleaned current message, possibly empty.
            group_id: Database visibility bucket.
            scope: Full platform and conversation identifier.
            sender_id: Current speaker, used to isolate unquoted follow-ups.
            self_id: Bot identifier; its replies cannot become retrieval evidence.
            message_id: Trigger message identifier.
            reply_ids: Referenced messages, including resolved reply ancestors.
            limit: Maximum number of returned source chunks.

        Returns:
            Selected source chunks and pending topic-retention information.
        """
        now = self.now()
        query = clean_retrieval_text(query)
        scope = scope or f"group:{group_id}"
        selection = KnowledgeSelection(scope, str(sender_id), str(message_id), query)
        model = str(self.config.get("embedding_openai_model") or "")
        for key in list(self.topics):
            self.topics[key] = [
                t
                for t in self.topics[key]
                if t["expires"] > now and t["model"] == model
            ]
            if not self.topics[key]:
                del self.topics[key]
        topics = self.topics.get(scope, [])
        followup = bool(
            re.fullmatch(
                r"(?:然后呢?|接着呢?|还有呢?|继续(?:说)?|再说(?:说)?|什么意思|什么谐音|啥意思|怎么说|怎么回事|为什么|为啥|真的吗|是吗|那你呢|那[他她它]呢|[他她它]是谁|那是什么意思)[呀啊呢吗吧？?！!。…\s]*",
                query,
            )
        )
        reply_ids = {str(value) for value in (reply_ids or set()) if value}
        catalog = None
        recent = []
        try:
            catalog, recent = await asyncio.gather(
                self.db._read_recall_rows(
                    "SELECT memory_id, content, embedding, vector_dimension FROM memory_embeddings "
                    "WHERE type = 'knowledge' AND (group_id = ? OR group_id = 'global') AND model_name = ?",
                    (str(group_id), model),
                ),
                self.db._read_recall_rows(
                    "SELECT message_id, sender_id, text, created_at, reply_to_msg_id FROM raw_group_messages "
                    "WHERE group_id = ? AND created_at >= ? AND created_at <= ? "
                    "ORDER BY created_at DESC LIMIT 12",
                    (str(group_id), now - 300, now),
                ),
            )
        except Exception:
            logger.exception(
                "[mem_chat] Knowledge catalog/context read failed scope=%s", scope
            )
        # Context comes from recent human messages, never assistant-generated claims.
        context_lines = []
        seen = {query}
        for mid, sid, body, _created, parent in recent:
            text = clean_retrieval_text(body)
            if str(mid) == str(message_id) and parent:
                reply_ids.add(str(parent))
            if str(sid) == str(self_id) or not text or text in seen:
                continue
            # Same-speaker context and an explicit reference are safer than a
            # whole group window containing several independent conversations.
            if str(sid) != str(sender_id) and str(mid) not in reply_ids:
                continue
            seen.add(text)
            context_lines.append(text[:180])
            # Stop at the nearest eligible utterance instead of reaching back
            # across a new topic just because an older alias still matches.
            break
        context_query = "\n".join(reversed(context_lines))[:600]
        context_vector = []
        if context_query:
            try:
                context_vector = await self.db.get_embedding(context_query)
            except Exception:
                logger.exception(
                    "[mem_chat] Knowledge context embedding failed scope=%s", scope
                )
        rows = []
        for mid, text, blob, dim in catalog or []:
            if not text:
                continue
            vector = self.db._deserialize_embedding(blob, int(dim or 0)) or []
            current = (
                self.db._cosine_similarity(qemb, vector)
                if len(qemb) == len(vector) and vector
                else 0.0
            )
            contextual = (
                self.db._cosine_similarity(context_vector, vector)
                if len(context_vector) == len(vector) and vector
                else 0.0
            )
            aliases = []
            heading = re.match(r"【([^】]+)】", text)
            explicit = re.search(r"检索别名[：:]([^。\n]+)", text)
            for block in [
                heading.group(1).split("：", 1)[0] if heading else "",
                explicit.group(1) if explicit else "",
            ]:
                for alias in re.split(r"[、,，／/|]+", block):
                    alias = alias.strip().casefold()
                    if 2 <= len(alias) <= 40 and alias not in aliases:
                        aliases.append(alias)
            direct_aliases = [a for a in aliases if a in query.casefold()]
            context_aliases = [a for a in aliases if a in context_query.casefold()]
            rows.append(
                (
                    str(mid),
                    text,
                    vector,
                    current,
                    contextual,
                    direct_aliases,
                    context_aliases,
                )
            )
        current_best = max((r[3] for r in rows), default=0.0)
        context_best = max((r[4] for r in rows), default=0.0)
        anchored_context = any(r[6] and r[4] >= 0.48 for r in rows)
        hits = []
        for (
            mid,
            text,
            vector,
            current,
            contextual,
            direct_aliases,
            context_aliases,
        ) in rows:
            direct = current >= max(0.35, current_best * 0.8)
            alias_direct = bool(direct_aliases) and (
                current >= 0.30
                or (not qemb and any(len(a) >= 3 for a in direct_aliases))
            )
            related = current >= max(0.35, current_best - 0.12) or followup
            contextual_hit = (
                related
                and contextual >= max(0.48, context_best * 0.85)
                and (bool(context_aliases) or not anchored_context)
            )
            alias_context = related and bool(context_aliases) and contextual >= 0.40
            if not (direct or alias_direct or contextual_hit or alias_context):
                continue
            score = max(
                current, contextual * 0.95 if contextual_hit or alias_context else 0.0
            )
            if alias_direct:
                if not qemb:
                    score = max(score, 0.45)
                score += 0.12 if any(len(a) >= 3 for a in direct_aliases) else 0.04
            if alias_context:
                score += 0.08 if any(len(a) >= 3 for a in context_aliases) else 0.03
            source = "context" if contextual_hit or alias_context else "current"
            if alias_direct:
                source = "alias"
            strong = (
                alias_direct
                or current >= 0.55
                or (contextual_hit and bool(context_aliases) and contextual >= 0.50)
            )
            hits.append(
                KnowledgeHit(
                    mid,
                    text,
                    vector,
                    score,
                    current,
                    contextual,
                    source,
                    strong,
                    tuple(direct_aliases + context_aliases),
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        # Each route was already filtered on its own evidence. An alias boost
        # must not raise a second cutoff and discard valid current-query hits.
        threshold = max(0.35, current_best * 0.8)
        available = {str(row[0]) for row in catalog} if catalog is not None else None
        # A substantive new message without matching source evidence ends this
        # speaker's old topic, while other speakers' parallel topics survive.
        if not followup:
            current_ids = {hit.memory_id for hit in hits[: max(1, limit)]}
            for topic in topics[:]:
                if not current_ids.intersection(hit.memory_id for hit in topic["hits"]):
                    topic["senders"].discard(str(sender_id))
                    if not topic["senders"]:
                        topics.remove(topic)
        # Elliptical follow-ups can reuse a source even when embedding fails.
        # A different speaker must explicitly refer to an anchored message.
        for topic in reversed(topics):
            referenced = bool(reply_ids.intersection(topic["message_ids"]))
            same_speaker = str(sender_id) in topic["senders"]
            if not (followup and (same_speaker or referenced)):
                continue
            if topic["remaining"] <= 0:
                # Keep an exhausted anchor until its original expiry so the
                # same old context cannot start another three fallback turns.
                exhausted = {h.memory_id for h in topic["hits"]}
                hits = [
                    h
                    for h in hits
                    if h.memory_id not in exhausted
                    or (h.strong and h.source in ("current", "alias"))
                ]
                continue
            inherited = [
                hit
                for hit in topic["hits"]
                if available is None or hit.memory_id in available
            ]
            if not inherited:
                continue
            selection.inherited = topic
            inherited_ids = {h.memory_id for h in inherited}
            # Weak re-retrieval of a cached source is still fallback use; it
            # must consume a round rather than silently extending the topic.
            hits = [
                hit
                for hit in hits
                if hit.memory_id not in inherited_ids
                or (hit.strong and hit.source in ("current", "alias"))
            ]
            existing = {h.memory_id for h in hits}
            carry = [
                KnowledgeHit(
                    h.memory_id,
                    h.text,
                    h.vector,
                    max(0.56, h.score),
                    h.current_score,
                    h.context_score,
                    "topic",
                    False,
                    h.aliases,
                )
                for h in inherited
                if h.memory_id not in existing
            ]
            hits = carry + hits
            break
        selection.hits = hits[: max(1, limit)]
        logger.info(
            "[mem_chat] Knowledge recall scope=%s query=%r context=%r threshold=%.4f hits=%s",
            scope,
            query[:160],
            context_query[:240],
            threshold,
            [
                {
                    "id": h.memory_id,
                    "score": round(h.score, 4),
                    "current": round(h.current_score, 4),
                    "context": round(h.context_score, 4),
                    "source": h.source,
                    "aliases": h.aliases,
                }
                for h in selection.hits
            ],
        )
        return selection

    def retain(self, selection: KnowledgeSelection, injected_ids: set[str]) -> None:
        """Remember only catalog sources that survived the prompt budget.

        Args:
            selection: The completed retrieval result.
            injected_ids: Sources actually included in the final prompt.
        """
        if not injected_ids:
            return
        inherited = selection.inherited
        used = [h for h in selection.hits if h.memory_id in injected_ids]
        fresh = [h for h in used if h.strong and h.source != "topic"][:3]
        if not fresh and not (inherited and any(h.source == "topic" for h in used)):
            return
        topics = self.topics.setdefault(selection.scope, [])
        if fresh:
            ids = {h.memory_id for h in fresh}
            previous = next(
                (t for t in topics if ids.intersection(h.memory_id for h in t["hits"])),
                None,
            )
            senders = {selection.sender_id} if selection.sender_id else set()
            message_ids = {selection.message_id} if selection.message_id else set()
            if previous:
                topics.remove(previous)
                senders |= previous["senders"]
                message_ids |= previous["message_ids"]
            topics.append(
                {
                    "hits": fresh,
                    "senders": senders,
                    "message_ids": set(list(message_ids)[-24:]),
                    "expires": self.now() + 300,
                    "remaining": 3,
                    "model": str(self.config.get("embedding_openai_model") or ""),
                }
            )
            del topics[:-3]
        elif inherited and any(h.source == "topic" for h in used):
            inherited["remaining"] -= 1
            if selection.message_id:
                inherited["message_ids"].add(selection.message_id)
            logger.info(
                "[mem_chat] Knowledge topic reused scope=%s remaining=%d expires=%.0f",
                selection.scope,
                inherited["remaining"],
                inherited["expires"],
            )
        if len(self.topics) > 128:
            oldest = min(
                self.topics, key=lambda key: max(t["expires"] for t in self.topics[key])
            )
            del self.topics[oldest]
