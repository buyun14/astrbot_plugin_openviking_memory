"""
Client-side recall bookkeeping.

The context search face keeps its own cross-turn ledger on the server, under
the session's ``.recall_log.json``. That ledger is the good one — but it only
exists when the server serves the context face and the session is materialized.
This module covers the gaps:

1. **URI ring** — a bounded tail of recently served URIs, sent as
   ``exclude_uris`` so a memory is not re-injected turn after turn. It is the
   only dedup that exists once recall degrades to list mode.
2. **Capability memos** — remember that the server refused the context face (or
   refused ``peer_scope``) so we stop paying a failed request every turn.

State is persisted through the plugin's KV store, so it survives restarts.

Security note: the ``peer_scope`` memo only ever *narrows* scope. A server that
rejects ``peer_scope=all`` makes us fall back to ``actor``; nothing here may
ever widen scope, because ``all`` reads across every peer of the same OV user.
"""

from __future__ import annotations

import json
import time
from typing import Any, Awaitable, Callable

from ._log import logger

# One more than the server's own cap, so the truncation it applies is visible.
DEFAULT_MAX_URIS = 200
# How long a "the server cannot do this" verdict is trusted before we retest.
DEFAULT_MEMO_TTL = 6 * 3600.0


class RecallLedger:
    """Bounded, persisted recall state for one plugin instance."""

    def __init__(
        self,
        kv_get: Callable[[str, Any], Awaitable[Any]],
        kv_put: Callable[[str, Any], Awaitable[Any]],
        prefix: str = "",
        *,
        max_uris: int = DEFAULT_MAX_URIS,
        memo_ttl: float = DEFAULT_MEMO_TTL,
    ) -> None:
        self._kv_get = kv_get
        self._kv_put = kv_put
        self._prefix = prefix
        self._max_uris = max(1, max_uris)
        self._memo_ttl = max(0.0, memo_ttl)

        self._state_key = f"{prefix}recall_state"
        self._state: dict[str, Any] = {}
        self._loaded = False
        self._rings: dict[str, list[str]] = {}
        self._ring_dirty: set[str] = set()

    # -- lifecycle ------------------------------------------------------------

    async def load(self) -> None:
        """Read persisted capability memos once. Safe to call repeatedly."""
        if self._loaded:
            return
        self._loaded = True
        raw = await self._kv_get(self._state_key, None)
        data = raw
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                data = None
        if isinstance(data, dict):
            self._state = data

    def _memo(self, name: str) -> dict[str, Any] | None:
        entry = self._state.get(name)
        if not isinstance(entry, dict):
            return None
        ts = float(entry.get("ts") or 0.0)
        if self._memo_ttl and (time.time() - ts) > self._memo_ttl:
            return None
        return entry

    async def _store_memo(self, name: str, payload: dict[str, Any]) -> None:
        self._state[name] = {**payload, "ts": time.time()}
        await self._kv_put(self._state_key, json.dumps(self._state))

    # -- context face availability -------------------------------------------

    @property
    def context_face_ok(self) -> bool:
        """False once the server has told us it cannot serve the context face."""
        return self._memo("context_face") is None

    @property
    def context_face_reason(self) -> str:
        entry = self._memo("context_face") or {}
        return str(entry.get("reason") or "")

    async def mark_context_unsupported(self, field: str, detail: str = "") -> None:
        """Record a deterministic refusal of the context face."""
        if self._memo("context_face") is None:
            logger.warning(
                "[OV] context-mode recall unavailable (field=%s): %s — "
                "falling back to list mode for %s",
                field,
                (detail or "no detail")[:200],
                _ttl_label(self._memo_ttl),
            )
        await self._store_memo("context_face", {"field": field, "reason": detail[:300]})

    # -- peer scope -----------------------------------------------------------

    @property
    def peer_scope_downgraded(self) -> bool:
        """True when the server rejected ``all`` and we pinned ``actor``."""
        return self._memo("peer_scope") is not None

    async def mark_peer_scope_unsupported(self, detail: str = "") -> None:
        """Pin the scope to ``actor`` after the server rejected a wider one.

        This is the only direction allowed: never widen a rejected scope.
        """
        if self._memo("peer_scope") is None:
            logger.warning(
                "[OV] server rejected peer_scope, pinning to 'actor': %s",
                (detail or "no detail")[:200],
            )
        await self._store_memo("peer_scope", {"reason": detail[:300]})

    def resolve_peer_scope(self, requested: str, *, self_scope: str) -> str:
        """Resolve the effective scope, honoring the downgrade memo.

        Args:
            requested: ``actor``, ``all``, or ``auto``.
            self_scope: Effective ``global``/``venue`` scope for this venue.

        Returns:
            ``actor`` or ``all``. ``auto`` maps to ``all`` only when the venue
            owns its own OV user (venue scope), where ``all`` means "the people
            in this group" rather than "every peer of a shared user".
        """
        if self.peer_scope_downgraded:
            return "actor"
        if requested in ("actor", "all"):
            return requested
        return "all" if self_scope == "venue" else "actor"

    # -- recently served URIs -------------------------------------------------

    async def recent_uris(self, session_id: str) -> list[str]:
        """The bounded tail of URIs already injected for this session."""
        if session_id not in self._rings:
            raw = await self._kv_get(self._ring_key(session_id), None)
            uris = raw
            if isinstance(raw, str):
                try:
                    uris = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    uris = None
            self._rings[session_id] = (
                [str(u) for u in uris if isinstance(u, str)] if isinstance(uris, list) else []
            )
        return list(self._rings[session_id])

    async def record(self, session_id: str, uris: list[str]) -> None:
        """Append served URIs to the ring, keeping the most recent ones."""
        fresh = [u for u in uris if u]
        if not fresh:
            return
        ring = await self.recent_uris(session_id)
        for uri in fresh:
            if uri in ring:
                ring.remove(uri)
            ring.append(uri)
        del ring[: max(0, len(ring) - self._max_uris)]
        self._rings[session_id] = ring
        await self._kv_put(self._ring_key(session_id), json.dumps(ring))

    def _ring_key(self, session_id: str) -> str:
        return f"{self._prefix}recall_uris::{session_id}"

    async def ring_size(self, session_id: str) -> int:
        """How many URIs are currently excluded for this session.

        Reads the ring on demand: ``snapshot()`` only knows about rings this
        process has already touched, so reporting from it alone shows 0 for a
        session that has a persisted ring but has not recalled yet.
        """
        return len(await self.recent_uris(session_id))

    # -- diagnostics ----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Small dict for ``/ov_status``."""
        return {
            "context_face": "ok" if self.context_face_ok else "unsupported",
            "context_face_reason": self.context_face_reason,
            "peer_scope_downgraded": self.peer_scope_downgraded,
            "rings": {k: len(v) for k, v in self._rings.items()},
        }


def _ttl_label(seconds: float) -> str:
    return f"{seconds / 3600:.0f}h" if seconds else "forever"
