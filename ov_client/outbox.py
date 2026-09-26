"""
Durable outbox for capture writes.

Captured messages are the only thing this plugin cannot re-derive: the bot's own
transcript is the source of truth, and once a message is dropped the memory it
would have produced is gone for good. A failed ``add_message`` used to end at a
log line, so a rate-limit window or an OV restart silently punched holes in the
session.

This outbox makes those failures self-healing:

* the write is persisted before the caller moves on, and replayed later;
* ordering is preserved — a failed head blocks its own venue's queue instead of
  letting later messages overtake it;
* only failures that can plausibly succeed later are retried (timeouts, 408/429,
  5xx, transport errors). A 4xx that means "this request is wrong" is dropped
  with a loud log rather than blocking the queue forever;
* entries expire, and each venue's queue is bounded, so a long outage cannot
  grow the database without limit.

Credentials are never stored: the queue holds only what was said, and the caller
resolves the Bearer identity again at drain time.
"""

from __future__ import annotations

import json
import time
from typing import Any, Awaitable, Callable

from ._log import logger

DEFAULT_MAX_PENDING = 200
DEFAULT_TTL_SECONDS = 24 * 3600.0


class Outbox:
    """Persisted, ordered, per-venue queue of undelivered capture writes."""

    def __init__(
        self,
        kv_get: Callable[[str, Any], Awaitable[Any]],
        kv_put: Callable[[str, Any], Awaitable[Any]],
        client: Any,
        *,
        prefix: str = "",
        auth_resolver: Callable[[str], dict] | None = None,
        max_pending: int = DEFAULT_MAX_PENDING,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._kv_get = kv_get
        self._kv_put = kv_put
        self._client = client
        self._prefix = prefix
        self._auth_for = auth_resolver or (lambda _venue: {})
        self._max = max(1, int(max_pending))
        self._ttl = max(0.0, float(ttl_seconds))
        self._queues: dict[str, list[dict[str, Any]]] = {}
        self._loaded = False
        self._sent = 0
        self._dropped = 0

    # -- lifecycle ------------------------------------------------------------

    async def load(self) -> None:
        """Load persisted queues and drop anything past its TTL."""
        if self._loaded:
            return
        self._loaded = True
        venues = await self._kv_get(self._index_key, None)
        venues = venues if isinstance(venues, list) else _parse_json(venues, [])
        dropped = 0
        now = time.time()
        for venue in venues:
            if not isinstance(venue, str) or not venue:
                continue
            entries = await self._read(venue)
            kept = [e for e in entries if not self._expired(e, now)]
            dropped += len(entries) - len(kept)
            if kept:
                self._queues[venue] = kept
        if dropped:
            self._dropped += dropped
            logger.warning("[OV] outbox dropped %d expired message(s) on load", dropped)
        if self._queues:
            logger.info(
                "[OV] outbox restored %d message(s) across %d venue(s)",
                sum(len(v) for v in self._queues.values()),
                len(self._queues),
            )

    # -- writing --------------------------------------------------------------

    async def send(
        self,
        venue: str,
        session_id: str,
        message: dict[str, Any],
        *,
        peer_id: str | None = None,
    ) -> bool:
        """Deliver a capture write, queueing it when that cannot happen now.

        Returns:
            True when the message reached OV (or was already queued behind an
            earlier failure), False when the caller must assume it is pending.
        """
        await self.load()

        if self._queues.get(venue):
            # Never overtake an older undelivered message: the session would read
            # out of order and the extracted memory would be wrong.
            await self._enqueue(venue, session_id, message, peer_id)
            return False

        ok, retryable, detail = await self._deliver(session_id, message, peer_id, venue)
        if ok:
            return True
        if retryable:
            logger.warning("[OV] capture write failed, queueing for retry: %s", detail)
            await self._enqueue(venue, session_id, message, peer_id)
            return False
        logger.error("[OV] capture write rejected, dropping: %s", detail)
        self._dropped += 1
        return False

    async def _deliver(
        self,
        session_id: str,
        message: dict[str, Any],
        peer_id: str | None,
        venue: str,
    ) -> tuple[bool, bool, str]:
        auth = self._auth_for(venue) or {}
        try:
            return await self._client.add_message_verbose(
                session_id, message, peer_id=peer_id, **auth
            )
        except TypeError:  # pragma: no cover - client without the verbose variant
            ok = await self._client.add_message(session_id, message, peer_id=peer_id, **auth)
            return ok, not ok, "" if ok else "add_message failed"

    async def _enqueue(
        self,
        venue: str,
        session_id: str,
        message: dict[str, Any],
        peer_id: str | None,
    ) -> None:
        queue = self._queues.setdefault(venue, [])
        queue.append(
            {
                "ts": time.time(),
                "s": session_id,
                "p": peer_id or "",
                "m": message,
            }
        )
        if len(queue) > self._max:
            overflow = len(queue) - self._max
            del queue[:overflow]
            self._dropped += overflow
            logger.warning("[OV] outbox full for %s, dropped %d oldest message(s)", venue, overflow)
        await self._write(venue, queue)

    # -- draining -------------------------------------------------------------

    async def flush(self, venue: str) -> int:
        """Try to deliver a venue's queued messages, oldest first.

        Stops at the first failure so ordering survives across drains.
        """
        await self.load()
        queue = self._queues.get(venue)
        if not queue:
            return 0

        now = time.time()
        delivered = 0
        while queue:
            entry = queue[0]
            if self._expired(entry, now):
                queue.pop(0)
                self._dropped += 1
                logger.warning("[OV] outbox entry expired for %s, dropping", venue)
                continue
            session_id = str(entry.get("s") or "")
            peer_id = str(entry.get("p") or "") or None
            message = entry.get("m")
            if not session_id or not isinstance(message, dict):
                queue.pop(0)
                self._dropped += 1
                continue
            ok, retryable, detail = await self._deliver(session_id, message, peer_id, venue)
            if ok:
                queue.pop(0)
                delivered += 1
                self._sent += 1
                continue
            if retryable:
                logger.debug("[OV] outbox replay still failing for %s: %s", venue, detail)
                break
            queue.pop(0)
            self._dropped += 1
            logger.error("[OV] outbox replay rejected, dropping: %s", detail)

        if delivered:
            logger.info("[OV] outbox replayed %d message(s) for %s", delivered, venue)
        await self._write(venue, queue)
        return delivered

    async def flush_all(self) -> int:
        """Drain every venue that has something queued."""
        await self.load()
        total = 0
        for venue in list(self._queues):
            total += await self.flush(venue)
        return total

    async def pending(self, venue: str) -> int:
        await self.load()
        return len(self._queues.get(venue) or [])

    # -- storage --------------------------------------------------------------

    @property
    def _index_key(self) -> str:
        return f"{self._prefix}outbox_index"

    def _queue_key(self, venue: str) -> str:
        return f"{self._prefix}outbox::{venue}"

    async def _read(self, venue: str) -> list[dict[str, Any]]:
        raw = await self._kv_get(self._queue_key(venue), None)
        data = _parse_json(raw, [])
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []

    async def _write(self, venue: str, queue: list[dict[str, Any]]) -> None:
        if queue:
            self._queues[venue] = queue
            await self._kv_put(self._queue_key(venue), json.dumps(queue))
        else:
            self._queues.pop(venue, None)
            await self._kv_put(self._queue_key(venue), json.dumps([]))
        await self._persist_index()

    async def _persist_index(self) -> None:
        await self._kv_put(self._index_key, json.dumps(sorted(self._queues)))

    def _expired(self, entry: dict[str, Any], now: float) -> bool:
        if self._ttl <= 0:
            return False
        return (now - float(entry.get("ts") or 0.0)) > self._ttl

    # -- diagnostics ----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "venues": {venue: len(queue) for venue, queue in self._queues.items() if queue},
            "sent": self._sent,
            "dropped": self._dropped,
        }


def _parse_json(raw: Any, default: Any) -> Any:
    if isinstance(raw, (list, dict)):
        return raw
    if not isinstance(raw, str):
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default
