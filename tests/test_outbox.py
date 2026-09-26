"""
Outbox behaviour: durable, ordered, bounded capture writes.

The property that matters is that nothing is lost quietly. A rate-limited or
restarted OV must cost a delay, not a hole in the session, and a replayed
message must never overtake the one before it.
"""

from __future__ import annotations

import asyncio
import json
import pathlib

from ov_client.outbox import Outbox

VENUE = "aiocqhttp-group-123"
SESSION = "astrbot-sess-aiocqhttp-group-123"


def _run(coro):
    return asyncio.run(coro)


class FakeKv:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key, default=None):
        return self.store.get(key, default)

    async def put(self, key, value):
        self.store[key] = value


class FakeOV:
    """Records every attempt; the test decides how each one turns out."""

    def __init__(self, *, ok=True, retryable=True) -> None:
        self.ok = ok
        self.retryable = retryable
        self.attempts: list[tuple[str, dict, str | None]] = []

    async def add_message_verbose(self, session_id, payload, **kwargs):
        self.attempts.append((session_id, payload, kwargs.get("peer_id")))
        if self.ok:
            return True, False, ""
        return False, self.retryable, "HTTP 429: rate limited"

    def delivered(self) -> list[str]:
        return [str(payload.get("content")) for _sid, payload, _pid in self.attempts]


def make_outbox(kv: FakeKv, client: FakeOV, **kw) -> Outbox:
    return Outbox(
        kv_get=kv.get,
        kv_put=kv.put,
        client=client,
        prefix="ov_test_",
        auth_resolver=lambda venue: {"api_key": f"key-{venue}"},
        **kw,
    )


def msg(text: str) -> dict:
    return {"role": "user", "content": text}


# -- happy path ------------------------------------------------------------


def test_successful_write_is_not_queued():
    kv, client = FakeKv(), FakeOV()
    box = make_outbox(kv, client)

    async def run():
        ok = await box.send(VENUE, SESSION, msg("hi"))
        return ok, await box.pending(VENUE), box.snapshot()

    ok, pending, snap = _run(run())
    assert ok is True
    assert pending == 0
    assert snap["sent"] == 0  # delivered on the first try, never replayed


def test_auth_is_resolved_at_send_time_and_never_stored():
    kv, client = FakeKv(), FakeOV(ok=False)
    seen: list[dict] = []
    box = Outbox(
        kv_get=kv.get,
        kv_put=kv.put,
        client=client,
        prefix="ov_test_",
        auth_resolver=lambda venue: seen.append({"venue": venue}) or {"api_key": "secret-key"},
    )

    _run(box.send(VENUE, SESSION, msg("hi")))
    assert seen == [{"venue": VENUE}]
    # The persisted queue must not carry the credential with it.
    persisted = "\n".join(kv.store.values())
    assert "secret-key" not in persisted


# -- failure handling ------------------------------------------------------


def test_retryable_failure_is_queued_and_replayed():
    kv, client = FakeKv(), FakeOV(ok=False)
    box = make_outbox(kv, client)

    async def run():
        first = await box.send(VENUE, SESSION, msg("one"))
        pending_while_down = await box.pending(VENUE)
        client.ok = True
        replayed = await box.flush(VENUE)
        return first, pending_while_down, replayed, await box.pending(VENUE)

    first, pending_while_down, replayed, after = _run(run())
    assert first is False
    assert pending_while_down == 1
    assert replayed == 1
    assert after == 0


def test_deterministic_failure_is_dropped_not_queued():
    kv, client = FakeKv(), FakeOV(ok=False, retryable=False)
    box = make_outbox(kv, client)

    async def run():
        ok = await box.send(VENUE, SESSION, msg("bad payload"))
        return ok, await box.pending(VENUE), box.snapshot()

    ok, pending, snap = _run(run())
    assert ok is False
    assert pending == 0  # retrying would never help
    assert snap["dropped"] == 1


def test_replay_preserves_order_and_stops_at_the_first_failure():
    kv, client = FakeKv(), FakeOV(ok=False)
    box = make_outbox(kv, client)

    async def run():
        for text in ("one", "two", "three"):
            await box.send(VENUE, SESSION, msg(text))
        client.ok = False
        assert await box.flush(VENUE) == 0  # still down
        client.ok = True
        client.attempts.clear()
        delivered = await box.flush(VENUE)
        return delivered, client.delivered()

    delivered, order = _run(run())
    assert delivered == 3
    assert order == ["one", "two", "three"]


def test_new_message_does_not_overtake_a_queued_one():
    kv, client = FakeKv(), FakeOV(ok=False)
    box = make_outbox(kv, client)

    async def run():
        await box.send(VENUE, SESSION, msg("first"))
        # While the queue is non-empty nothing may be delivered directly, or the
        # session would read out of order.
        ok = await box.send(VENUE, SESSION, msg("second"))
        return ok, await box.pending(VENUE)

    ok, pending = _run(run())
    assert ok is False
    assert pending == 2


def test_flush_all_covers_every_venue():
    kv, client = FakeKv(), FakeOV(ok=False)
    box = make_outbox(kv, client)
    other = "telegram-dm-7"

    async def run():
        await box.send(VENUE, SESSION, msg("a"))
        await box.send(other, "astrbot-sess-" + other, msg("b"))
        client.ok = True
        return await box.flush_all()

    assert _run(run()) == 2


# -- durability ------------------------------------------------------------


def test_queue_survives_a_restart():
    kv, client = FakeKv(), FakeOV(ok=False)

    async def run():
        await make_outbox(kv, client).send(VENUE, SESSION, msg("across restart"))
        restarted = make_outbox(kv, client)
        await restarted.load()
        return await restarted.pending(VENUE)

    assert _run(run()) == 1


def test_expired_entries_are_dropped_on_load():
    kv, client = FakeKv(), FakeOV(ok=False)
    box = make_outbox(kv, client, ttl_seconds=3600)

    async def run():
        await box.send(VENUE, SESSION, msg("stale"))
        stored = json.loads(kv.store[f"ov_test_outbox::{VENUE}"])
        stored[0]["ts"] -= 7200  # older than the TTL
        kv.store[f"ov_test_outbox::{VENUE}"] = json.dumps(stored)
        fresh = make_outbox(kv, client, ttl_seconds=3600)
        await fresh.load()
        return await fresh.pending(VENUE), fresh.snapshot()["dropped"]

    pending, dropped = _run(run())
    assert pending == 0
    assert dropped == 1


def test_queue_is_bounded_per_venue():
    kv, client = FakeKv(), FakeOV(ok=False)
    box = make_outbox(kv, client, max_pending=2)

    async def run():
        for text in ("one", "two", "three"):
            await box.send(VENUE, SESSION, msg(text))
        fresh = make_outbox(kv, client, max_pending=2)
        await fresh.load()
        stored = json.loads(kv.store[f"ov_test_outbox::{VENUE}"])
        return await fresh.pending(VENUE), [e["m"].get("content") for e in stored]

    pending, contents = _run(run())
    assert pending == 2
    assert contents == ["two", "three"]  # oldest dropped


def test_disabled_outbox_is_not_consulted():
    """The switch lives in main.py, but the queue must stay empty either way."""
    kv, client = FakeKv(), FakeOV()
    box = make_outbox(kv, client)
    assert _run(box.pending(VENUE)) == 0
    assert kv.store == {}


# -- wiring guard ----------------------------------------------------------


def test_capture_writes_go_through_the_outbox():
    """A new capture write that bypasses _capture() would lose messages again."""
    source = pathlib.Path(__file__).resolve().parents[1] / "main.py"
    text = source.read_text(encoding="utf-8")
    direct = text.count("self.ov.add_message")
    assert direct == 1, (
        f"found {direct} direct add_message calls; every capture write must go "
        "through self._capture() so failures can be replayed"
    )
