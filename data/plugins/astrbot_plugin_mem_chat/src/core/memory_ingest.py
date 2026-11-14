"""Microbatch ordinary group embeddings while preserving independent memory rows."""

from __future__ import annotations

import asyncio
import logging
from collections import deque

logger = logging.getLogger("astrbot")


class GroupMemoryBatcher:
    """Own a single background ingestion lane, separate from directed input."""

    def __init__(self, db, config):
        self.db = db
        self.wait_sec = max(0, float(config.get("group_memory_batch_wait_sec", 30)))
        self.batch_size = max(1, min(8, int(config.get("group_memory_batch_size", 8))))
        self.pending = deque()
        self.wake = asyncio.Event()
        self.worker = None
        self.closed = False

    def submit(
        self, content, group_id, sender_info, *, immediate=False, importance=None
    ):
        """Queue one independent memory or start an immediate directed write.

        Args:
            content: Rendered message text.
            group_id: Original owning conversation bucket.
            sender_info: Original speaker name and id.
            immediate: Bypass batching for private or directed input.
            importance: Optional initial memory salience.

        Returns:
            An awaitable completed only after this input has been processed.
        """
        if self.closed:
            raise RuntimeError("Memory ingestion is closed")
        if immediate or not self.wait_sec:
            return asyncio.create_task(
                self.db.ensure_message_embedded(
                    content,
                    group_id,
                    sender_info,
                    importance=importance,
                )
            )
        future = asyncio.get_running_loop().create_future()
        self.pending.append(
            (
                content,
                group_id,
                sender_info,
                importance,
                future,
                asyncio.get_running_loop().time(),
            )
        )
        if len(self.pending) >= self.batch_size:
            self.wake.set()
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._run())
        return future

    async def _run(self):
        """Flush at a fixed deadline or size, without extending on each arrival."""
        try:
            while self.pending:
                if not self.closed and len(self.pending) < self.batch_size:
                    try:
                        remaining = max(
                            0,
                            self.pending[0][5]
                            + self.wait_sec
                            - asyncio.get_running_loop().time(),
                        )
                        await asyncio.wait_for(self.wake.wait(), remaining)
                    except asyncio.TimeoutError:
                        pass
                self.wake.clear()
                batch = []
                chars = 0
                while self.pending and len(batch) < self.batch_size:
                    row = self.pending[0]
                    size = len(row[0]) + len(row[2] or "")
                    if batch and chars + size > 24000:
                        break
                    batch.append(self.pending.popleft())
                    chars += size
                try:
                    texts = [
                        f"{row[2]}: {row[0]}" if row[2] else row[0] for row in batch
                    ]
                    # Populate the same cache used by individual memory writes.
                    # Each row keeps its speaker, scope, content hash and salience.
                    vectors = await self.db.get_embedding(texts)
                    if len(vectors) != len(batch):
                        raise RuntimeError(
                            "Group memory batch returned no complete vectors"
                        )
                    for content, gid, sender, importance, _future, _queued_at in batch:
                        await self.db.ensure_message_embedded(
                            content,
                            gid,
                            sender,
                            importance=importance,
                        )
                    logger.info(
                        "[mem_chat] Group memory batch completed messages=%s",
                        len(batch),
                    )
                except asyncio.CancelledError:
                    for row in batch:
                        if not row[4].done():
                            row[4].cancel()
                    raise
                except Exception:
                    logger.exception(
                        "[mem_chat] Group memory batch failed; raw messages remain available"
                    )
                finally:
                    for row in batch:
                        future = row[4]
                        if not future.done():
                            future.set_result(None)
        finally:
            if self.closed:
                while self.pending:
                    future = self.pending.popleft()[4]
                    if not future.done():
                        future.cancel()

    async def close(self):
        """Flush queued memories before the database closes, with bounded shutdown."""
        self.closed = True
        self.wake.set()
        if self.worker and not self.worker.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.worker), 10)
            except asyncio.TimeoutError:
                self.worker.cancel()
                await asyncio.gather(self.worker, return_exceptions=True)
                logger.warning(
                    "[mem_chat] Memory ingestion shutdown timed out; raw messages retained"
                )
