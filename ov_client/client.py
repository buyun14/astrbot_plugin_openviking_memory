"""
Async HTTP client for the OpenViking server API.

Targets peer-contract OpenViking servers (PR #2236+). Identity is derived from
the Bearer key: in standard api_key mode the server 403s on X-OpenViking-Account
/ X-OpenViking-User and ignores X-OpenViking-Agent (the agent identity layer was
removed). Those headers are only sent when trusted_mode is set (auth_mode=trusted
behind a gateway).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from ._log import logger

DEFAULT_TIMEOUT = 15.0

# Context-mode search bounds, mirrored from the server's request model so we
# clamp locally instead of earning a 422.
CONTEXT_MIN_TOKENS = 64
CONTEXT_MAX_TOKENS = 32000
CONTEXT_MAX_DEDUP_TURNS = 100
CONTEXT_MAX_EXCLUDE_URIS = 200

# Sent at commit when peer memory is enabled: extract both the bot's own (self)
# memory and a per-person (peer) profile for each peer_id seen in the batch.
PEER_MEMORY_POLICY: dict[str, dict[str, bool]] = {
    "self": {"enabled": True},
    "peer": {"enabled": True},
}


class ContextSearchUnsupported(Exception):
    """The server cannot serve the context face as asked.

    Deterministic: the same request will fail the same way, so callers should
    fall back to list mode and remember it rather than retry.

    Attributes:
        field: ``"mode"`` when the context face itself is unavailable,
            ``"peer_scope"`` when only that argument was rejected. The caller
            must narrow the scope in the latter case, never widen it.
        detail: Short server-supplied explanation, for logs.
    """

    def __init__(self, field: str, detail: str = "") -> None:
        super().__init__(f"context search unsupported ({field}): {detail}")
        self.field = field
        self.detail = detail


def _error_message(response: Any) -> str:
    """Pull the human-readable message out of an OV error envelope."""
    try:
        error = response.json().get("error") or {}
    except Exception:
        return response.text[:200]
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return str(error)[:300]


def _unsupported_field(response: Any) -> str:
    """Decide whether a 4xx means "no context face" or "peer_scope rejected"."""
    message = _error_message(response).lower()
    if "peer_scope" in message:
        return "peer_scope"
    return "mode"


def _search_hits(result: Any) -> list[dict[str, Any]]:
    """Flatten a list-mode search payload into a flat hit list."""
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if not isinstance(result, dict):
        return []
    hits: list[dict[str, Any]] = []
    for bucket in ("memories", "skills"):
        entries = result.get(bucket)
        if isinstance(entries, list):
            hits.extend(item for item in entries if isinstance(item, dict))
    return hits


class OVClient:
    """Thin wrapper over OV REST endpoints needed by the plugin."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        account_id: str = "",
        trusted_mode: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.account_id = account_id
        self.trusted_mode = trusted_mode
        self._http = httpx.AsyncClient(timeout=timeout)

    def _headers(
        self,
        api_key: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, str]:
        key = api_key or self.api_key
        h: dict[str, str] = {"Content-Type": "application/json"}
        if key:
            h["Authorization"] = f"Bearer {key}"
        # Identity is derived from the Bearer key. Only assert it via headers in
        # trusted mode; in api_key mode these 403 (and -Agent was removed).
        if self.trusted_mode:
            if self.account_id:
                h["X-OpenViking-Account"] = self.account_id
            if user_id:
                h["X-OpenViking-User"] = user_id
        return h

    async def close(self):
        await self._http.aclose()

    # -- health ---------------------------------------------------------------

    async def health(self) -> bool:
        try:
            r = await self._http.get(
                f"{self.base_url}/health",
                headers=self._headers(),
            )
            return r.status_code == 200
        except Exception:
            return False

    # -- admin: create user ---------------------------------------------------

    async def create_user(
        self,
        user_id: str,
        admin_api_key: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """Create OV user. Returns (result_dict, error_msg). error_msg is "" on success."""
        url = f"{self.base_url}/api/v1/admin/accounts/{quote(self.account_id)}/users"
        try:
            r = await self._http.post(
                url,
                headers=self._headers(api_key=admin_api_key),
                json={"user_id": user_id, "role": "user"},
            )
        except Exception as e:
            return None, f"HTTP error: {e}"
        if r.status_code == 200:
            body = r.json()
            return body.get("result", body), ""
        return None, f"HTTP {r.status_code}: {r.text[:300]}"

    # -- sessions -------------------------------------------------------------

    async def add_message(
        self,
        session_id: str,
        payload: dict[str, Any],
        api_key: str | None = None,
        user_id: str | None = None,
        peer_id: str | None = None,
    ) -> bool:
        import json as _json

        # peer_id (peer contract) tags the message with the stable id of "the
        # other party"; set on incoming messages so commit extracts peer memory.
        if peer_id:
            payload = {**payload, "peer_id": peer_id}
        body = _json.dumps(payload, ensure_ascii=False, default=str)
        r = await self._http.post(
            f"{self.base_url}/api/v1/sessions/{quote(session_id)}/messages",
            headers=self._headers(api_key=api_key, user_id=user_id),
            content=body,
        )
        if r.status_code != 200:
            logger.warning("add_message %s failed: %d", session_id, r.status_code)
        return r.status_code == 200

    async def commit_session(
        self,
        session_id: str,
        api_key: str | None = None,
        user_id: str | None = None,
        memory_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        # memory_policy (peer contract) overrides the session default for this
        # commit, e.g. {"self": {"enabled": True}, "peer": {"enabled": True}}.
        # Empty body still forces a commit regardless of pending threshold.
        body = {"memory_policy": memory_policy} if memory_policy else {}
        r = await self._http.post(
            f"{self.base_url}/api/v1/sessions/{quote(session_id)}/commit",
            headers=self._headers(api_key=api_key, user_id=user_id),
            json=body,
        )
        if r.status_code == 200:
            return r.json().get("result")
        logger.warning("commit_session %s failed: %d", session_id, r.status_code)
        return None

    async def get_session(
        self,
        session_id: str,
        api_key: str | None = None,
        user_id: str | None = None,
        auto_create: bool = False,
    ) -> dict[str, Any] | None:
        q = "?auto_create=true" if auto_create else ""
        r = await self._http.get(
            f"{self.base_url}/api/v1/sessions/{quote(session_id)}{q}",
            headers=self._headers(api_key=api_key, user_id=user_id),
        )
        if r.status_code == 200:
            return r.json().get("result")
        return None

    # -- search ---------------------------------------------------------------

    async def find(
        self,
        query: str,
        target_uri: str | list[str] = "",
        limit: int = 8,
        api_key: str | None = None,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "score_threshold": 0,
        }
        if target_uri:
            body["target_uri"] = target_uri
        r = await self._http.post(
            f"{self.base_url}/api/v1/search/find",
            headers=self._headers(api_key=api_key, user_id=user_id),
            json=body,
        )
        if r.status_code != 200:
            logger.warning("find failed: %d", r.status_code)
            return []
        result = r.json().get("result", {})
        if isinstance(result, list):
            return result
        items: list[dict[str, Any]] = []
        for bucket in ("memories", "skills"):
            items.extend(result.get(bucket, []))
        return items

    async def resolve_user_space(
        self,
        api_key: str | None = None,
        user_id: str | None = None,
    ) -> str:
        r = await self._http.get(
            f"{self.base_url}/api/v1/system/status",
            headers=self._headers(api_key=api_key, user_id=user_id),
        )
        if r.status_code == 200:
            user = r.json().get("result", {}).get("user", "")
            if user and isinstance(user, str):
                return user.strip()
        return "default"

    async def search_list(
        self,
        query: str,
        target_uri: str | list[str] = "",
        limit: int = 8,
        min_score: float = 0.35,
        session_id: str = "",
        api_key: str | None = None,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """``POST /search`` in list mode: ranked hits, session-aware.

        List mode keeps ``target_uri`` (so the caller's multi-peer narrowing
        still works) while accepting ``session_id``. ``read_content`` asks the
        server to inline each hit's body, which removes one ``content/read``
        round-trip per hit.

        Returns:
            Hits with ``uri``/``abstract``/``level``/``content``; an empty list
            when the server found nothing, or None when the request itself
            failed (so the caller can try an older endpoint).
        """
        body: dict[str, Any] = {
            "query": query,
            "mode": "list",
            "limit": limit,
            "score_threshold": min_score,
            "read_content": True,
        }
        if target_uri:
            body["target_uri"] = target_uri
        if session_id:
            body["session_id"] = session_id
        try:
            r = await self._http.post(
                f"{self.base_url}/api/v1/search/search",
                headers=self._headers(api_key=api_key, user_id=user_id),
                json=body,
            )
        except httpx.HTTPError as e:
            logger.warning("search(list) transport error: %s", type(e).__name__)
            return None
        if r.status_code == 200:
            return _search_hits(r.json().get("result"))
        logger.warning("search(list) failed: %d", r.status_code)
        return None

    async def search_context(
        self,
        query: str,
        session_id: str = "",
        *,
        peer_scope: str = "actor",
        max_tokens: int = 0,
        dedup_turns: int = 0,
        query_expansion: str = "auto",
        rewrite: bool = False,
        purpose: str = "chat",
        min_score: float = 0.35,
        exclude_uris: list[str] | None = None,
        timeout: float | None = None,
        api_key: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        """``POST /search`` in context mode: server-side assembly.

        Context mode is the only face that runs the cross-turn dedup ledger,
        query expansion and token budgeting; it takes ``peer_scope`` instead of
        ``target_uri`` (the server rejects the latter outright).

        Args:
            query: User query.
            session_id: The same OV session the capture path writes to; the
                dedup ledger lives under that session and is skipped without it.
            peer_scope: ``actor`` (this peer only) or ``all``. Passed explicitly
                because the server defaults to ``all``.
            max_tokens: Injection budget; clamped to the server's 64..32000.
            dedup_turns: Cross-turn cooldown; clamped to 0..100.
            query_expansion: ``auto`` or ``off``.
            rewrite: Ask the server for a rewritten digest (slower).
            purpose: Quota preset, ``chat`` or ``coding``.
            min_score: Score threshold.
            exclude_uris: Client-side dedup ring; truncated to the server's 200.
            timeout: Per-request timeout in seconds (this call may be slow).
            api_key: Bearer override.
            user_id: Identity assertion (trusted mode only).

        Returns:
            ``{entries, rendered, digest, stats}`` on success, or None when the
            failure looks transient (timeout, connection error, 5xx).

        Raises:
            ContextSearchUnsupported: The server has no context face, or rejects
                one of its arguments (deterministic — do not retry).
        """
        body: dict[str, Any] = {
            "query": query,
            "mode": "context",
            "peer_scope": peer_scope,
            "query_expansion": query_expansion,
            "purpose": purpose,
            "score_threshold": min_score,
            "rewrite": rewrite,
        }
        if session_id:
            body["session_id"] = session_id
        if max_tokens > 0:
            body["max_tokens"] = max(CONTEXT_MIN_TOKENS, min(max_tokens, CONTEXT_MAX_TOKENS))
        if dedup_turns > 0:
            body["dedup_turns"] = max(1, min(dedup_turns, CONTEXT_MAX_DEDUP_TURNS))
        if exclude_uris:
            body["exclude_uris"] = list(exclude_uris)[:CONTEXT_MAX_EXCLUDE_URIS]

        kwargs: dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            r = await self._http.post(
                f"{self.base_url}/api/v1/search/search",
                headers=self._headers(api_key=api_key, user_id=user_id),
                json=body,
                **kwargs,
            )
        except httpx.TimeoutException:
            logger.warning("search(context) timed out after %ss", timeout)
            return None
        except httpx.HTTPError as e:
            logger.warning("search(context) transport error: %s", type(e).__name__)
            return None

        if r.status_code == 200:
            result = r.json().get("result")
            return result if isinstance(result, dict) else None
        if r.status_code in (404, 405):
            # A server old enough to have no /search at all.
            raise ContextSearchUnsupported("mode", f"HTTP {r.status_code}")
        if r.status_code >= 500:
            logger.warning("search(context) server error: %d", r.status_code)
            return None
        # 400/422: the request itself is wrong. Split "old server" from
        # "peer_scope rejected" so the caller can degrade only the former.
        raise ContextSearchUnsupported(_unsupported_field(r), _error_message(r))

    async def read_content(
        self,
        uri: str,
        api_key: str | None = None,
        user_id: str | None = None,
    ) -> str | None:
        r = await self._http.get(
            f"{self.base_url}/api/v1/content/read",
            params={"uri": uri},
            headers=self._headers(api_key=api_key, user_id=user_id),
        )
        if r.status_code == 200:
            result = r.json().get("result")
            return result if isinstance(result, str) else None
        return None

    # -- resources ------------------------------------------------------------

    async def add_resource(
        self,
        path: str,
        to_uri: str,
        api_key: str | None = None,
        user_id: str | None = None,
        wait: bool = False,
    ) -> dict[str, Any] | None:
        r = await self._http.post(
            f"{self.base_url}/api/v1/resources",
            headers=self._headers(api_key=api_key, user_id=user_id),
            json={"path": path, "to": to_uri, "wait": wait},
        )
        if r.status_code == 200:
            return r.json().get("result")
        logger.warning("add_resource failed: %d", r.status_code)
        return None
