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
import time

from ov_client.outbox import Outbox

VENUE = "aiocqhttp-group-123"
SESSION = "astrbot-sess-aiocqhttp-group-123"


def _run(coro):
    return asyncio.run(coro)


class FakeKv:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key, default=None):
        # Yielding keeps the store from being silently atomic for the
        # concurrency test below.
        await asyncio.sleep(0)
        return self.store.get(key, default)

    async def put(self, key, value):
        await asyncio.sleep(0)
        self.store[key] = value


class RecordingKv(FakeKv):
    """FakeKv that remembers the order keys were written in."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[str] = []

    async def put(self, key, value):
        self.writes.append(key)
        await super().put(key, value)

    def wrote_before(self, first: str, second: str) -> bool:
        return self.writes.index(first) < self.writes.index(second)


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


# -- storage integrity -----------------------------------------------------


def test_corrupt_timestamp_does_not_break_loading():
    """A record we cannot reason about must not take down the whole load."""
    kv = FakeKv()
    kv.store["ov_test_outbox_index"] = json.dumps([VENUE])
    kv.store[f"ov_test_outbox::{VENUE}"] = json.dumps(
        [
            {"ts": "not-a-number", "s": SESSION, "m": msg("corrupt")},
            {"ts": {"nested": True}, "s": SESSION, "m": msg("also corrupt")},
            {"s": SESSION, "m": msg("no timestamp at all")},
            {"ts": time.time(), "s": SESSION, "m": msg("good")},
        ]
    )
    box = make_outbox(kv, FakeOV(ok=False))

    async def run():
        await box.load()  # must not raise
        return await box.pending(VENUE), box.snapshot()["dropped"]

    pending, dropped = _run(run())
    assert pending == 1
    assert dropped == 3


def test_index_is_persisted_before_the_queue():
    """A crash between the two writes must not orphan a queue.

    Index-first degrades to "an index entry whose queue reads back empty";
    queue-first would leave a full queue that load() can never discover.
    """
    kv, client = RecordingKv(), FakeOV(ok=False)
    box = make_outbox(kv, client)

    _run(box.send(VENUE, SESSION, msg("hi")))

    assert kv.wrote_before("ov_test_outbox_index", f"ov_test_outbox::{VENUE}")


def test_concurrent_sends_are_serialised_and_ordered():
    """Capture hooks run concurrently; the queue must not lose or reorder."""
    kv, client = FakeKv(), FakeOV(ok=False)
    box = make_outbox(kv, client)

    async def run():
        await asyncio.gather(*(box.send(VENUE, SESSION, msg(f"m{i}")) for i in range(5)))
        stored = json.loads(kv.store[f"ov_test_outbox::{VENUE}"])
        return await box.pending(VENUE), [e["m"]["content"] for e in stored]

    pending, persisted = _run(run())
    assert pending == 5
    assert persisted == ["m0", "m1", "m2", "m3", "m4"]


def test_replay_reports_each_delivery():
    """Replayed writes must be countable, or a drained queue never commits."""
    kv, client = FakeKv(), FakeOV(ok=False)
    seen: list[tuple] = []

    async def on_delivered(venue, session_id, message):
        seen.append((venue, session_id, message.get("content")))

    box = make_outbox(kv, client, on_delivered=on_delivered)

    async def run():
        await box.send(VENUE, SESSION, msg("late"))
        client.ok = True
        return await box.flush(VENUE)

    assert _run(run()) == 1
    assert seen == [(VENUE, SESSION, "late")]


def test_a_failing_delivery_callback_does_not_break_the_drain():
    kv, client = FakeKv(), FakeOV(ok=False)

    async def boom(*_args):
        raise RuntimeError("accounting blew up")

    box = make_outbox(kv, client, on_delivered=boom)

    async def run():
        await box.send(VENUE, SESSION, msg("late"))
        client.ok = True
        return await box.flush(VENUE)

    assert _run(run()) == 1


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


def test_private_method_calls_match_their_signatures():
    """Call/definition arity must agree.

    _caption_images was called with six arguments while its definition still took
    seven. Python only complains at runtime, on a path that is off by default, so
    nothing else catches it: ruff does not, and the test suite does not exercise
    that path. This guard covers the class instead of the instance.
    """
    import ast

    source = pathlib.Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    bounds: dict[str, tuple[int, int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        if args.vararg or args.kwarg:
            continue  # variadic: there is no fixed arity to compare against
        positional = args.posonlyargs + args.args
        if positional and positional[0].arg == "self":
            positional = positional[1:]
        required = len(positional) - len(args.defaults)
        required += sum(1 for default in args.kw_defaults if default is None)
        bounds[node.name] = (required, len(positional) + len(args.kwonlyargs))

    mismatches: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            continue
        if func.value.id != "self" or func.attr not in bounds:
            continue
        if any(isinstance(arg, ast.Starred) for arg in node.args):
            continue
        if any(kw.arg is None for kw in node.keywords):  # **kwargs unpacking
            continue
        supplied = len(node.args) + len(node.keywords)
        low, high = bounds[func.attr]
        if not low <= supplied <= high:
            mismatches.append(
                f"{func.attr} at line {node.lineno}: {supplied} argument(s) "
                f"for a signature taking {low}..{high}"
            )

    assert not mismatches, "call/definition arity mismatch: " + "; ".join(mismatches)


def test_commit_accounting_only_happens_after_delivery():
    """record_message must stay behind a delivery check.

    It belongs in exactly two places: _capture_message (gated on the write
    succeeding) and the replay callback. Anywhere else risks committing a session
    that is missing messages, or counting a message that was dropped.
    """
    source = pathlib.Path(__file__).resolve().parents[1] / "main.py"
    text = source.read_text(encoding="utf-8")
    assert text.count("self.scheduler.record_message(") == 2


def test_background_loop_is_started_beyond_the_capture_path():
    """The drainer also polls commit tasks, so it cannot depend on the outbox.

    If it is only started from a capture write, then outbox_enabled=false leaves
    commit states stuck at "extracting" forever, and a queue restored after a
    restart is never drained while the bot is idle.
    """
    import ast

    source = pathlib.Path(__file__).resolve().parents[1] / "main.py"
    raw = source.read_text(encoding="utf-8")
    tree = ast.parse(raw)

    starts = 0
    loop_source = ""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = ast.get_source_segment(raw, node) or ""
        if node.name == "_drain_loop":
            loop_source = body
        if "self._ensure_drainer()" in body and node.name != "_ensure_drainer":
            starts += 1

    assert starts >= 3, "expected _ensure_drainer() in capture, on_loaded and on_llm_request"
    assert "poll_commit_tasks" in loop_source
    # Polling must not sit behind the outbox switch.
    assert loop_source.index("poll_commit_tasks") < loop_source.index("outbox_enabled")
