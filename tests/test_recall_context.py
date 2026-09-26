"""
Recall wiring: context face, degradation chain, and client-side dedup.

These lock in the behaviour that the recall rework exists for:
the server-side context face is tried first, every deterministic refusal is
remembered instead of re-paid on every turn, ``peer_scope`` may only be
narrowed, and what was injected is not injected again on the next turn.
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib

import httpx

from ov_client.client import (
    CONTEXT_MAX_EXCLUDE_URIS,
    CONTEXT_MAX_TOKENS,
    ContextSearchUnsupported,
    OVClient,
)
from ov_client.config import PluginConfig
from ov_client.recall import recall_and_format
from ov_client.recall_ledger import RecallLedger

VENUE = "aiocqhttp-group-123"
SESSION = "astrbot-sess-aiocqhttp-group-123"


def _run(coro):
    return asyncio.run(coro)


def _run_with_mock(handler, coro_factory):
    transport = httpx.MockTransport(handler)

    async def run():
        c = OVClient("http://x", api_key="k")
        c._http = httpx.AsyncClient(transport=transport)
        try:
            return await coro_factory(c)
        finally:
            await c.close()

    return asyncio.run(run())


# -- KV stub --------------------------------------------------------------


class FakeKv:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.writes = 0

    async def get(self, key, default=None):
        return self.store.get(key, default)

    async def put(self, key, value):
        self.writes += 1
        self.store[key] = value


def make_ledger(kv: FakeKv, **kw) -> RecallLedger:
    return RecallLedger(kv_get=kv.get, kv_put=kv.put, prefix="ov_test_", **kw)


# -- search_context body --------------------------------------------------


def test_search_context_body_is_context_shaped():
    captured: dict = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"result": {"entries": [], "stats": {}}})

    result = _run_with_mock(
        handler,
        lambda c: c.search_context(
            "q",
            session_id=SESSION,
            peer_scope="actor",
            max_tokens=3000,
            dedup_turns=3,
            exclude_uris=["viking://a", "viking://b"],
        ),
    )

    assert result == {"entries": [], "stats": {}}
    body = captured["body"]
    assert captured["url"].endswith("/api/v1/search/search")
    assert body["mode"] == "context"
    assert body["session_id"] == SESSION
    assert body["peer_scope"] == "actor"
    assert body["max_tokens"] == 3000
    assert body["dedup_turns"] == 3
    assert body["exclude_uris"] == ["viking://a", "viking://b"]
    # target_uri is list-mode only: sending it earns a hard 400.
    assert "target_uri" not in body


def test_search_context_clamps_out_of_range_values():
    captured: dict = {}

    def handler(request):
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"result": {"entries": [], "stats": {}}})

    _run_with_mock(
        handler,
        lambda c: c.search_context(
            "q",
            max_tokens=10**9,
            dedup_turns=10**6,
            exclude_uris=[f"viking://{i}" for i in range(CONTEXT_MAX_EXCLUDE_URIS + 50)],
        ),
    )

    body = captured["body"]
    assert body["max_tokens"] == CONTEXT_MAX_TOKENS
    assert body["dedup_turns"] == 100
    assert len(body["exclude_uris"]) == CONTEXT_MAX_EXCLUDE_URIS


def test_search_context_omits_budget_when_unset():
    captured: dict = {}

    def handler(request):
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"result": {}})

    _run_with_mock(handler, lambda c: c.search_context("q", max_tokens=0, dedup_turns=0))
    assert "max_tokens" not in captured["body"]
    assert "dedup_turns" not in captured["body"]


def test_search_context_classifies_peer_scope_refusal():
    def handler(request):
        return httpx.Response(
            400,
            json={
                "status": "error",
                "error": {
                    "code": "INVALID_ARGUMENT",
                    "message": "Value error, peer_scope require mode='context'; ...",
                },
            },
        )

    try:
        _run_with_mock(handler, lambda c: c.search_context("q", peer_scope="all"))
    except ContextSearchUnsupported as exc:
        assert exc.field == "peer_scope"
    else:  # pragma: no cover - the refusal must not be swallowed
        raise AssertionError("expected ContextSearchUnsupported")


def test_search_context_classifies_missing_context_face():
    def handler(request):
        return httpx.Response(
            400,
            json={
                "status": "error",
                "error": {"code": "INVALID_ARGUMENT", "message": "unknown field mode"},
            },
        )

    try:
        _run_with_mock(handler, lambda c: c.search_context("q"))
    except ContextSearchUnsupported as exc:
        assert exc.field == "mode"
    else:  # pragma: no cover
        raise AssertionError("expected ContextSearchUnsupported")


def test_search_context_returns_none_on_transient_failures():
    def server_error(request):
        return httpx.Response(503, text="unavailable")

    def timeout(request):
        raise httpx.TimeoutException("too slow")

    assert _run_with_mock(server_error, lambda c: c.search_context("q")) is None
    assert _run_with_mock(timeout, lambda c: c.search_context("q")) is None


# -- search_list ----------------------------------------------------------


def test_search_list_keeps_target_uri_and_inlines_content():
    captured: dict = {}

    def handler(request):
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "result": {
                    "memories": [{"uri": "viking://m/1", "abstract": "a", "content": "c"}],
                    "skills": [{"uri": "viking://s/1", "abstract": "b"}],
                }
            },
        )

    hits = _run_with_mock(
        handler,
        lambda c: c.search_list("q", target_uri=["viking://user/s/memories"], session_id=SESSION),
    )

    assert [h["uri"] for h in hits] == ["viking://m/1", "viking://s/1"]
    body = captured["body"]
    assert body["mode"] == "list"
    assert body["read_content"] is True
    assert body["target_uri"] == ["viking://user/s/memories"]
    assert body["session_id"] == SESSION


def test_search_list_distinguishes_empty_from_failed():
    def empty(request):
        return httpx.Response(200, json={"result": {"memories": []}})

    def broken(request):
        return httpx.Response(500, text="boom")

    assert _run_with_mock(empty, lambda c: c.search_list("q")) == []
    assert _run_with_mock(broken, lambda c: c.search_list("q")) is None


# -- ledger ---------------------------------------------------------------


def test_ledger_ring_is_bounded_and_newest_last():
    kv = FakeKv()
    ledger = make_ledger(kv, max_uris=3)

    async def run():
        for uri in ("a", "b", "c", "d"):
            await ledger.record(SESSION, [uri])
        return await ledger.recent_uris(SESSION)

    assert _run(run()) == ["b", "c", "d"]


def test_ledger_persists_across_instances():
    kv = FakeKv()

    async def run():
        first = make_ledger(kv)
        await first.record(SESSION, ["viking://m/1"])
        second = make_ledger(kv)
        return await second.recent_uris(SESSION)

    assert _run(run()) == ["viking://m/1"]


def test_ledger_memo_expires():
    kv = FakeKv()

    async def run():
        ledger = make_ledger(kv, memo_ttl=3600)
        await ledger.mark_context_unsupported("mode", "old server")
        assert not ledger.context_face_ok
        # Age the memo past its TTL: a retest is due.
        ledger._state["context_face"]["ts"] = 0.0
        return ledger.context_face_ok

    assert _run(run()) is True


def test_peer_scope_never_widens():
    kv = FakeKv()
    ledger = make_ledger(kv)
    assert ledger.resolve_peer_scope("auto", self_scope="global") == "actor"
    assert ledger.resolve_peer_scope("auto", self_scope="venue") == "all"

    _run(ledger.mark_peer_scope_unsupported("rejected"))
    # Even an explicit `all` must collapse after a refusal.
    assert ledger.resolve_peer_scope("all", self_scope="venue") == "actor"


# -- degradation chain ----------------------------------------------------


class StubClient:
    """Records which tier was used; each tier is scripted per test."""

    def __init__(self, *, context=None, context_exc=None, hits=(), find_items=()):
        self.context = context
        self.context_exc = context_exc
        self.hits = None if hits is None else list(hits)
        self.find_items = list(find_items)
        # Optional per-call script for search_list (supplement tests need two
        # different answers from the same tier).
        self.list_queue: list | None = None
        self.calls: list[tuple] = []

    async def resolve_user_space(self, api_key=None, user_id=None):
        return "space"

    async def search_context(self, query, session_id="", **kwargs):
        self.calls.append(("context", session_id, kwargs))
        if self.context_exc:
            raise self.context_exc
        return self.context

    async def search_list(self, query, target_uri="", limit=8, **kwargs):
        self.calls.append(("list", kwargs.get("session_id", ""), target_uri))
        if self.list_queue is not None:
            return self.list_queue.pop(0) if self.list_queue else []
        return self.hits

    async def find(self, query, target_uri="", limit=8, **kwargs):
        self.calls.append(("find", target_uri))
        return self.find_items

    def tiers(self) -> list[str]:
        return [c[0] for c in self.calls]


def entry(uri: str, text: str, score: float = 0.9) -> dict:
    return {"uri": uri, "category": "preferences", "score": score, "detail": "full", "text": text}


def hit(uri: str, abstract: str = "abstract", score: float = 0.9) -> dict:
    return {"uri": uri, "abstract": abstract, "level": 1, "score": score}


def recall(client, ledger=None, cfg=None, query="what does alice like", self_scope="global", **kw):
    return _run(
        recall_and_format(
            client,
            cfg or PluginConfig({}),
            query,
            VENUE,
            "astrbot-global",
            session_id=SESSION,
            self_scope=self_scope,
            ledger=ledger,
            **kw,
        )
    )


def test_context_entries_are_rendered_and_remembered():
    kv = FakeKv()
    ledger = make_ledger(kv)
    peer_uri = "viking://user/space/peers/alice/memories/preferences/tea"
    client = StubClient(
        context={"entries": [entry(peer_uri, "Alice prefers tea")], "stats": {}},
    )

    block = recall(client, ledger)

    assert client.tiers() == ["context"]
    assert "Alice prefers tea" in block
    assert "about:alice" in block
    assert block.startswith("<openviking-context>")
    assert "viking://" not in block.split("\n")[2]  # content line, not a URI stub
    assert _run(ledger.recent_uris(SESSION)) == [peer_uri]


def test_remembered_uri_is_excluded_next_turn():
    kv = FakeKv()
    ledger = make_ledger(kv)
    uri = "viking://user/space/peers/alice/memories/preferences/tea"

    async def run():
        await ledger.record(SESSION, [uri])
        client = StubClient(context={"entries": [entry(uri, "Alice prefers tea")], "stats": {}})
        return await recall_and_format(
            client,
            PluginConfig({}),
            "what does alice like",
            VENUE,
            "astrbot-global",
            session_id=SESSION,
            self_scope="global",
            ledger=ledger,
        )

    assert _run(run()) is None


def test_missing_context_face_is_remembered_and_skipped_next_turn():
    kv = FakeKv()
    ledger = make_ledger(kv)
    client = StubClient(
        context_exc=ContextSearchUnsupported("mode", "unknown field mode"),
        hits=[hit("viking://user/space/memories/notes")],
    )

    block = recall(client, ledger)

    assert client.tiers() == ["context", "list"]
    assert "abstract" in block
    assert not ledger.context_face_ok

    second = StubClient(hits=[hit("viking://user/space/memories/notes")])
    recall(second, ledger)
    assert second.tiers() == ["list"]  # no second failed context probe


def test_peer_scope_refusal_pins_actor_for_next_turn():
    kv = FakeKv()
    ledger = make_ledger(kv)
    client = StubClient(
        context_exc=ContextSearchUnsupported("peer_scope", "peer_scope require context"),
        hits=[hit("viking://user/space/memories/notes")],
    )

    recall(client, ledger)
    assert client.calls[0][2]["peer_scope"] == "actor"  # global scope → actor
    assert ledger.peer_scope_downgraded

    venue_client = StubClient(context={"entries": [], "text": "", "stats": {}})
    _run(
        recall_and_format(
            venue_client,
            PluginConfig({"self_scope": "venue"}),
            "hi there",
            VENUE,
            "astrbot-global",
            session_id=SESSION,
            self_scope="venue",
            ledger=ledger,
        )
    )
    assert venue_client.calls[0][2]["peer_scope"] == "actor"  # `all` was refused before


def test_no_relevant_suppresses_injection_without_second_search():
    kv = FakeKv()
    ledger = make_ledger(kv)
    client = StubClient(
        context={"entries": [], "text": "", "stats": {"rewrite": "no_relevant"}},
        hits=[hit("viking://user/space/memories/notes")],
    )

    assert recall(client, ledger) is None
    assert client.tiers() == ["context"]


def test_transient_context_failure_still_uses_ranked_path():
    kv = FakeKv()
    ledger = make_ledger(kv)
    client = StubClient(context=None, hits=[hit("viking://user/space/memories/notes")])

    block = recall(client, ledger)

    assert client.tiers() == ["context", "list"]
    assert block is not None
    assert ledger.context_face_ok  # transient, so nothing is memoized


def test_list_failure_falls_back_to_find():
    kv = FakeKv()
    ledger = make_ledger(kv)
    client = StubClient(context=None, hits=None, find_items=[hit("viking://user/space/memories/x")])

    async def search_list(*a, **kw):
        client.calls.append(("list", kw.get("session_id", ""), kw.get("target_uri", "")))
        return None

    client.search_list = search_list
    block = recall(client, ledger)

    assert client.tiers() == ["context", "list", "find"]
    assert block is not None


def test_recall_disabled_and_short_queries_skip_everything():
    client = StubClient(context={"entries": [], "text": "", "stats": {}})
    assert recall(client, None, cfg=PluginConfig({"auto_recall_enabled": False})) is None
    for empty in ("   ", "@bot", "@bot  ", "/ov_status"):
        assert recall(client, None, query=empty) is None
    assert client.tiers() == []


def test_context_face_can_be_switched_off():
    client = StubClient(hits=[hit("viking://user/space/memories/x")])
    cfg = PluginConfig({"recall_context_enabled": False})
    block = recall(client, None, cfg=cfg)
    assert client.tiers() == ["list"]
    assert block is not None


# -- active-peer supplement ------------------------------------------------


def peer_uri(peer: str, leaf: str = "preferences/tea") -> str:
    return f"viking://user/space/peers/{peer}/memories/{leaf}"


def self_uri() -> str:
    return "viking://user/space/memories/self"


def context_with_self_note() -> dict:
    return {"entries": [entry(self_uri(), "self note")], "stats": {}}


def test_supplement_is_off_by_default():
    client = StubClient(context=context_with_self_note(), hits=[hit(peer_uri("bob"))])
    block = recall(client, make_ledger(FakeKv()), active_member_ids=["bob"])

    assert client.tiers() == ["context"]  # no second search
    assert "self note" in block
    assert "preferences/tea" not in block


def test_supplement_adds_active_peer_rows():
    ledger = make_ledger(FakeKv())
    client = StubClient(context=context_with_self_note())
    client.list_queue = [[hit(peer_uri("bob"), "Bob prefers tea", 0.8)]]

    block = recall(
        client,
        ledger,
        cfg=PluginConfig({"recall_include_active_peers": True}),
        active_member_ids=["bob"],
    )

    assert client.tiers() == ["context", "list"]
    assert "self note" in block
    assert "Bob prefers tea" in block
    assert "about:bob" in block
    # The extra search is scoped to the peer's own space, not the whole user.
    assert client.calls[1][2] == [peer_uri("bob").rsplit("/memories", 1)[0] + "/memories"]
    # Both parts are remembered, so the next turn excludes them.
    assert sorted(_run(ledger.recent_uris(SESSION))) == sorted([self_uri(), peer_uri("bob")])


def test_supplement_skipped_when_context_already_covers_all_peers():
    client = StubClient(context=context_with_self_note(), hits=[hit(peer_uri("bob"))])
    recall(
        client,
        make_ledger(FakeKv()),
        cfg=PluginConfig({"recall_include_active_peers": True, "self_scope": "venue"}),
        self_scope="venue",
        active_member_ids=["bob"],
    )

    assert client.tiers() == ["context"]  # peer_scope=all already covers peers


def test_supplement_requires_active_member_scope():
    client = StubClient(context=context_with_self_note(), hits=[hit(peer_uri("bob"))])
    recall(
        client,
        make_ledger(FakeKv()),
        cfg=PluginConfig({"recall_include_active_peers": True, "peer_recall_scope": "speaker"}),
        active_member_ids=["bob"],
    )

    assert client.tiers() == ["context"]


def test_supplement_drops_uris_already_in_the_block():
    duplicate = peer_uri("bob")
    client = StubClient(context={"entries": [entry(duplicate, "Bob prefers tea")], "stats": {}})
    client.list_queue = [[hit(duplicate, "Bob prefers tea", 0.99)]]

    block = recall(
        client,
        make_ledger(FakeKv()),
        cfg=PluginConfig({"recall_include_active_peers": True}),
        active_member_ids=["bob"],
    )

    assert client.tiers() == ["context", "list"]
    assert block.count("Bob prefers tea") == 1


# -- wiring guard ---------------------------------------------------------


def test_every_recall_call_site_passes_the_session_id():
    """A recall without session_id silently loses the cross-turn ledger."""
    source = pathlib.Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "recall_and_format"
    ]
    assert calls, "recall_and_format is no longer called from main.py"
    for call in calls:
        passed = {kw.arg for kw in call.keywords}
        assert "session_id" in passed, f"missing session_id at line {call.lineno}"
        assert "ledger" in passed, f"missing ledger at line {call.lineno}"
