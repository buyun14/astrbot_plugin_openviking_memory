"""
Semantic recall from OV and context injection formatting.

Mirrors the CC memory plugin's auto-recall.mjs: multi-source find,
client-side ranking with boosts, token-budgeted injection block.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from ._log import logger
from .client import ContextSearchUnsupported, OVClient
from .config import PluginConfig
from .identity import parse_venue_origin, safe_peer_id, venue_is_group
from .parts import estimate_tokens
from .recall_ledger import RecallLedger

_PEER_URI_RE = re.compile(r"/peers/([^/]+)/")

_BLOCK_OPEN = "<openviking-context>"
_BLOCK_HINT = "Relevant context from OpenViking. Use the read MCP tool to expand URIs."
_BLOCK_CLOSE = "</openviking-context>"

_PREFERENCE_RE = re.compile(
    r"prefer|preference|favorite|favourite|like|偏好|喜欢|爱好|更倾向", re.I
)
_TEMPORAL_RE = re.compile(
    r"when|what time|date|day|month|year|yesterday|today|tomorrow|last|next"
    r"|什么时候|何时|哪天|几月|几年|昨天|今天|明天",
    re.I,
)
_TOKEN_RE = re.compile(r"[a-z0-9一-鿿]{2,}", re.I)
_STOPWORDS = {
    "what",
    "when",
    "where",
    "which",
    "who",
    "whom",
    "whose",
    "why",
    "how",
    "did",
    "does",
    "is",
    "are",
    "was",
    "were",
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "your",
    "you",
}

# (api_key fingerprint, user_id) -> OV user space. Bounded: it exists only to
# avoid re-resolving the same identity on every single recall.
_SPACE_CACHE_MAX = 64
_space_cache: dict[tuple[str, str], str] = {}


async def _resolve_user_space(
    client: OVClient,
    api_key: str | None,
    user_id: str | None,
) -> str:
    # Fingerprint the key rather than using it directly: this dict is long-lived.
    fingerprint = hashlib.sha256(api_key.encode()).hexdigest()[:16] if api_key else ""
    cache_key = (fingerprint, user_id or "")
    cached = _space_cache.get(cache_key)
    if cached is not None:
        return cached
    space = await client.resolve_user_space(api_key=api_key, user_id=user_id)
    if len(_space_cache) >= _SPACE_CACHE_MAX:
        _space_cache.clear()
    _space_cache[cache_key] = space
    return space


def _build_query_profile(query: str) -> dict:
    tokens = [t for t in _TOKEN_RE.findall(query.lower()) if t not in _STOPWORDS]
    return {
        "tokens": tokens,
        "wants_preference": bool(_PREFERENCE_RE.search(query)),
        "wants_temporal": bool(_TEMPORAL_RE.search(query)),
    }


def _lexical_overlap_boost(tokens: list[str], text: str) -> float:
    if not tokens or not text:
        return 0.0
    haystack = f" {text.lower()} "
    matched = sum(1 for t in tokens[:8] if t in haystack)
    return min(0.2, (matched / min(len(tokens), 4)) * 0.2)


def _rank_item(item: dict, profile: dict) -> float:
    base = max(0.0, min(1.0, item.get("score", 0)))
    abstract = (item.get("abstract") or item.get("overview") or "").strip()
    uri = (item.get("uri") or "").lower()

    leaf_boost = 0.12 if (item.get("level") == 2 or uri.endswith(".md")) else 0.0
    event_boost = 0.1 if profile["wants_temporal"] and "/events/" in uri else 0.0
    pref_boost = 0.08 if profile["wants_preference"] and "/preferences/" in uri else 0.0
    overlap = _lexical_overlap_boost(profile["tokens"], f"{uri} {abstract}")
    return base + leaf_boost + event_boost + pref_boost + overlap


def _dedup(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for it in items:
        uri = it.get("uri", "")
        cat = (it.get("category") or "").lower()
        if cat in ("events", "cases") or "/events/" in uri or "/cases/" in uri:
            key = f"uri:{uri}"
        else:
            key = (it.get("abstract") or it.get("overview") or "").strip().lower() or f"uri:{uri}"
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


def _build_recall_targets(
    cfg: PluginConfig,
    space: str,
    speaker_id: str | None,
    active_member_ids: list[str] | None,
) -> tuple[list[str], list[str]]:
    """Explicit (target_uri list, peer_id list) for a recall.

    Self memories are always included. When peer recall is on, each peer is named
    explicitly under viking://user/<space>/peers/<peer>/memories — OV resolves
    each target independently (see openviking/core/retrieval_targets.py), and we
    deliberately do NOT pass a peer_id param (which would reject other peers).
    """
    targets = [f"viking://user/{space}/memories"]
    peer_ids: list[str] = []
    if cfg.peer_enabled and cfg.peer_recall_scope != "none":
        sp = safe_peer_id(speaker_id)
        if sp:
            peer_ids.append(sp)
        if cfg.peer_recall_scope == "speaker_plus_active":
            for member in active_member_ids or []:
                pid = safe_peer_id(member)
                if pid and pid not in peer_ids:
                    peer_ids.append(pid)
    targets.extend(f"viking://user/{space}/peers/{pid}/memories" for pid in peer_ids)
    return targets, peer_ids


_MIN_QUERY_CHARS = 2
_AT_PREFIX_RE = re.compile(r"^(?:@\S+\s*)+")
_COMMAND_PREFIX_RE = re.compile(r"^/\S+\s*")


def _clean_query(query: str) -> str:
    """Strip leading @mentions and a command prefix; truncate very long input.

    A message that is nothing but a mention or a command cleans down to nothing
    and is not worth an embed call.
    """
    text = str(query or "").strip()
    text = _AT_PREFIX_RE.sub("", text).strip()
    text = _COMMAND_PREFIX_RE.sub("", text).strip()
    return text[:4000]


def _resolve_peer_scope(cfg: PluginConfig, ledger: RecallLedger | None, self_scope: str) -> str:
    """Effective ``actor``/``all`` for this venue.

    ``all`` is only ever returned under venue scope, where the OV user is the
    group and ``all`` therefore means "the people in this group". Under global
    scope every venue shares one OV user, so ``all`` would scan other venues'
    peers and is narrowed to ``actor`` even when configured explicitly. Kept in
    step with ``RecallLedger.resolve_peer_scope``, which applies the same rule
    for callers that have a ledger.
    """
    requested = str(getattr(cfg, "recall_peer_scope", "auto") or "auto").lower()
    if ledger is not None:
        return ledger.resolve_peer_scope(requested, self_scope=self_scope)
    if requested == "actor":
        return "actor"
    return "all" if self_scope == "venue" else "actor"


async def _context_recall(
    client: OVClient,
    cfg: PluginConfig,
    query: str,
    session_id: str,
    self_scope: str,
    ledger: RecallLedger | None,
    exclude: list[str],
    api_key: str | None,
    user_id: str | None,
) -> dict[str, Any] | None:
    """Ask for server-side context assembly.

    Returns:
        ``None`` when the context face could not be used (caller should fall
        back to ranked hits); otherwise a dict with ``entries``, ``text`` (the
        server's digest/rendered fallback) and ``stats``. An empty ``entries``
        with empty ``text`` means "the server had nothing relevant" — that is a
        terminal answer, not a reason to run another search.
    """
    budget = int(getattr(cfg, "recall_token_budget", 0) or 0)
    timeout_ms = int(getattr(cfg, "recall_timeout_ms", 0) or 0)
    try:
        result = await client.search_context(
            query,
            session_id=session_id,
            peer_scope=_resolve_peer_scope(cfg, ledger, self_scope),
            max_tokens=budget,
            dedup_turns=int(getattr(cfg, "recall_dedup_turns", 0) or 0),
            query_expansion=("auto" if getattr(cfg, "recall_query_expansion", True) else "off"),
            rewrite=bool(getattr(cfg, "recall_rewrite", False)),
            min_score=cfg.recall_min_score,
            exclude_uris=exclude,
            timeout=max(1.0, timeout_ms / 1000.0) if timeout_ms else None,
            api_key=api_key,
            user_id=user_id,
        )
    except ContextSearchUnsupported as exc:
        if ledger is not None:
            if exc.field == "peer_scope":
                # Narrow from the next turn on. Do not widen, and do not spend a
                # second slow request inside this turn retrying.
                await ledger.mark_peer_scope_unsupported(exc.detail)
            else:
                await ledger.mark_context_unsupported(exc.field, exc.detail)
        return None

    if result is None:
        return None

    stats = result.get("stats") or {}
    if str(stats.get("rewrite") or "") == "no_relevant":
        return {"entries": [], "text": "", "stats": stats}

    entries = [
        entry
        for entry in (result.get("entries") or [])
        if isinstance(entry, dict)
        and float(entry.get("score") or 0) >= cfg.recall_min_score
        and entry.get("uri") not in exclude
    ]
    text = str(result.get("digest") or result.get("rendered") or "").strip()
    return {"entries": entries, "text": text, "stats": stats}


async def recall_and_format(
    client: OVClient,
    cfg: PluginConfig,
    query: str,
    venue_id: str,
    ov_user_id: str,
    api_key: str | None = None,
    user_id: str | None = None,
    speaker_id: str | None = None,
    active_member_ids: list[str] | None = None,
    session_id: str = "",
    self_scope: str = "global",
    ledger: RecallLedger | None = None,
) -> str | None:
    """Recall memories and render the injection block.

    Three tiers, best first:

    1. ``/search`` in context mode — server-side assembly, which is the only
       face that runs the cross-turn dedup ledger, query expansion and token
       budgeting. Needs ``session_id`` to be the session the capture path writes.
    2. ``/search`` in list mode — keeps the explicit ``target_uri`` narrowing
       and accepts ``session_id``, but no server-side dedup.
    3. ``/find`` — the original path, kept for servers predating ``/search``.

    Args:
        client: OV HTTP client.
        cfg: Plugin config snapshot.
        query: Raw user message text.
        venue_id: Venue identifier (drives labels and group detection).
        ov_user_id: OV user id, used for the space in the degraded paths.
        api_key: Bearer override for this venue.
        user_id: Identity assertion (trusted mode only).
        speaker_id: Current speaker, for peer narrowing.
        active_member_ids: Recently-active peers, for peer narrowing.
        session_id: OV session id; the context face's dedup ledger needs it.
        self_scope: Effective ``global``/``venue`` scope for this venue.
        ledger: Client-side recall state, when available.

    Returns:
        The rendered block, or None when there is nothing to inject.
    """
    text = _clean_query(query)
    if not cfg.auto_recall_enabled or len(text) < _MIN_QUERY_CHARS:
        return None

    exclude: list[str] = []
    if session_id and ledger is not None:
        exclude = await ledger.recent_uris(session_id)

    # -- tier 1: server-side context assembly --------------------------------
    if getattr(cfg, "recall_context_enabled", True) and (ledger is None or ledger.context_face_ok):
        context = await _context_recall(
            client, cfg, text, session_id, self_scope, ledger, exclude, api_key, user_id
        )
        if context is not None:
            rows = _context_rows(context["entries"], venue_id)
            served = _uris_of(context["entries"])
            if _supplement_enabled(cfg, self_scope, ledger, active_member_ids):
                extra_rows, extra_uris = await _supplement_rows(
                    client,
                    cfg,
                    text,
                    venue_id,
                    session_id,
                    exclude,
                    served,
                    active_member_ids,
                    api_key,
                    user_id,
                )
                rows.extend(extra_rows)
                served.extend(extra_uris)
            block = _assemble_block(rows, cfg.recall_token_budget) or _wrap(context["text"])
            if block:
                await _remember(ledger, session_id, served)
            return block

    # -- tier 2 / 3: ranked hits, then client-side ranking -------------------
    space = await _resolve_user_space(client, api_key, user_id)
    targets, peer_ids = _build_recall_targets(cfg, space, speaker_id, active_member_ids)
    per_source_limit = max(cfg.recall_limit * 2, 8) + 4 * len(peer_ids)

    items = await client.search_list(
        query=text,
        target_uri=targets,
        limit=per_source_limit,
        min_score=cfg.recall_min_score,
        session_id=session_id,
        api_key=api_key,
        user_id=user_id,
    )
    if items is None:  # endpoint missing or request failed → last resort
        items = await client.find(
            query=text,
            target_uri=targets,
            limit=per_source_limit,
            api_key=api_key,
            user_id=user_id,
        )
    if not items:
        return None

    profile = _build_query_profile(text)
    filtered = [
        it
        for it in items
        if it.get("score", 0) >= cfg.recall_min_score and it.get("uri") not in exclude
    ]
    filtered.sort(key=lambda it: _rank_item(it, profile), reverse=True)
    picked = _dedup(filtered)[: cfg.recall_limit]
    if not picked:
        return None

    block = await _build_injection_block(client, cfg, picked, venue_id, api_key, user_id)
    if block:
        await _remember(ledger, session_id, _uris_of(picked))
    return block


def _uris_of(items: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("uri")) for item in items if item.get("uri")]


async def _remember(ledger: RecallLedger | None, session_id: str, uris: list[str]) -> None:
    """Persist what we just injected, so the next turn can exclude it."""
    if ledger is not None and session_id and uris:
        await ledger.record(session_id, uris)


def _wrap(text: str) -> str | None:
    """Wrap a server-rendered digest as the injection block."""
    body = (text or "").strip()
    return f"{_BLOCK_OPEN}\n{body}\n{_BLOCK_CLOSE}" if body else None


def _entry_header(
    score_pct: int,
    origin_label: str,
    uri: str,
    *,
    is_group: bool,
    abstract: str,
) -> str:
    """``[memory 62% · origin · about:<peer>]`` for one entry."""
    header = f"[memory {score_pct}% · {origin_label}"
    peer = _peer_from_uri(uri)
    if peer:
        header += f" · about:{peer}"
    else:
        sender = _extract_sender(abstract)
        if is_group and sender:
            header += f" · from:{sender}"
    return header + "]"


def _assemble_block(rows: list[tuple[str, str, str]], budget: int) -> str | None:
    """Budget a list of ``(header, uri, content)`` rows into the block format.

    Rows whose content does not fit keep a URI line instead, so the model always
    has something to expand.
    """
    lines = [_BLOCK_OPEN, _BLOCK_HINT]
    content_count = 0
    for header, uri, content in rows:
        uri_line = f"- {header} {uri}"
        body = (content or "").strip()
        if not body or budget <= 0:
            lines.append(uri_line)
            continue
        content_line = f"- {header} {body}"
        cost = estimate_tokens(content_line)
        if cost > budget and content_count > 0:
            lines.append(uri_line)
            continue
        lines.append(content_line)
        budget -= cost
        content_count += 1
    if content_count == 0 and len(lines) == 2:
        return None
    lines.append(_BLOCK_CLOSE)
    return "\n".join(lines)


def _context_rows(
    entries: list[dict[str, Any]],
    venue_id: str,
) -> list[tuple[str, str, str]]:
    """Build rows from context-face entries.

    These entries differ from ranked hits: their body lives in ``text`` (already
    fetched at whatever detail tier the server chose) and there is no
    ``abstract``, so the ``from:<sender>`` label that the ranked path derives
    from an abstract is unavailable here. ``about:<peer>`` still works, since
    the URI is always present.
    """
    origin_label = parse_venue_origin(venue_id)
    rows: list[tuple[str, str, str]] = []
    for entry in entries:
        score_pct = max(0, min(100, int(float(entry.get("score") or 0) * 100)))
        uri = str(entry.get("uri") or "")
        header = _entry_header(score_pct, origin_label, uri, is_group=False, abstract="")
        rows.append((header, uri, str(entry.get("text") or "")))
    return rows


async def _hit_rows(
    client: OVClient,
    cfg: PluginConfig,
    items: list[dict[str, Any]],
    venue_id: str,
    api_key: str | None,
    user_id: str | None = None,
) -> list[tuple[str, str, str]]:
    """Build rows from ranked hits, fetching any body they came without."""
    is_group = venue_is_group(venue_id)
    origin_label = parse_venue_origin(venue_id)
    rows: list[tuple[str, str, str]] = []
    for item in items:
        score_pct = max(0, min(100, int(item.get("score", 0) * 100)))
        uri = item.get("uri", "")
        abstract = (item.get("abstract") or item.get("overview") or "").strip()
        header = _entry_header(score_pct, origin_label, uri, is_group=is_group, abstract=abstract)
        rows.append((header, uri, await _resolve_content(client, item, cfg, api_key, user_id)))
    return rows


async def _build_injection_block(
    client: OVClient,
    cfg: PluginConfig,
    items: list[dict[str, Any]],
    venue_id: str,
    api_key: str | None,
    user_id: str | None = None,
) -> str | None:
    rows = await _hit_rows(client, cfg, items, venue_id, api_key, user_id)
    return _assemble_block(rows, cfg.recall_token_budget)


def _active_peer_targets(space: str, active_member_ids: list[str] | None) -> list[str]:
    """Memory roots of the recently-active members.

    Only used to supplement the context face, which addresses at most one peer
    through ``peer_scope`` and therefore cannot reach the other active members.
    """
    targets: list[str] = []
    for member in active_member_ids or []:
        pid = safe_peer_id(member)
        if pid:
            targets.append(f"viking://user/{space}/peers/{pid}/memories")
    return targets


def _supplement_enabled(
    cfg: PluginConfig,
    self_scope: str,
    ledger: RecallLedger | None,
    active_member_ids: list[str] | None,
) -> bool:
    """Whether the active-peer supplement applies to this venue.

    Only when it can add something: the caller asked for active members, there
    are some, and the context face is limited to a single peer. When
    ``peer_scope`` is already ``all`` the server has covered those peers.
    """
    if not getattr(cfg, "recall_include_active_peers", False):
        return False
    if not cfg.peer_enabled or cfg.peer_recall_scope != "speaker_plus_active":
        return False
    if not active_member_ids:
        return False
    return _resolve_peer_scope(cfg, ledger, self_scope) == "actor"


async def _supplement_rows(
    client: OVClient,
    cfg: PluginConfig,
    query: str,
    venue_id: str,
    session_id: str,
    exclude: list[str],
    already: list[str],
    active_member_ids: list[str] | None,
    api_key: str | None,
    user_id: str | None,
) -> tuple[list[tuple[str, str, str]], list[str]]:
    """One extra list-mode search over the active members' own spaces.

    Kept deliberately narrow: it reuses the ranked path so the result set is
    bounded by ``recall_limit``, and anything already in the block (``already``)
    or recently served (``exclude``) is dropped so a peer profile cannot be
    injected twice in one turn.
    """
    space = await _resolve_user_space(client, api_key, user_id)
    targets = _active_peer_targets(space, active_member_ids)
    if not targets:
        return [], []

    hits = await client.search_list(
        query=query,
        target_uri=targets,
        limit=max(cfg.recall_limit, 4),
        min_score=cfg.recall_min_score,
        session_id=session_id,
        api_key=api_key,
        user_id=user_id,
    )
    if not hits:
        return [], []

    blocked = set(exclude) | set(already)
    profile = _build_query_profile(query)
    filtered = [
        hit
        for hit in hits
        if hit.get("score", 0) >= cfg.recall_min_score and hit.get("uri") not in blocked
    ]
    filtered.sort(key=lambda hit: _rank_item(hit, profile), reverse=True)
    picked = _dedup(filtered)[: cfg.recall_limit]
    if not picked:
        return [], []

    logger.debug(
        "[OV] active-peer supplement added %d hit(s) from %d peer(s)",
        len(picked),
        len(targets),
    )
    rows = await _hit_rows(client, cfg, picked, venue_id, api_key, user_id)
    return rows, _uris_of(picked)


async def _resolve_content(
    client: OVClient,
    item: dict[str, Any],
    cfg: PluginConfig,
    api_key: str | None,
    user_id: str | None = None,
) -> str:
    uri = item.get("uri", "")
    abstract = (item.get("abstract") or item.get("overview") or "").strip()

    # List-mode search can inline the body (read_content=true); prefer it, as it
    # saves one content/read round-trip per hit.
    inline = item.get("content")
    if isinstance(inline, str) and inline.strip():
        return inline.strip()

    if item.get("level") == 2 and uri:
        full = await client.read_content(uri, api_key=api_key, user_id=user_id)
        if full and full.strip():
            return full.strip()

    return abstract or uri


def _extract_sender(abstract: str) -> str:
    if abstract.startswith("[") and "]" in abstract:
        bracket_end = abstract.index("]")
        return abstract[1:bracket_end]
    return ""


def _peer_from_uri(uri: str) -> str:
    """Return the peer id if uri is a peer-memory URI, else ''."""
    m = _PEER_URI_RE.search(uri or "")
    return m.group(1) if m else ""
