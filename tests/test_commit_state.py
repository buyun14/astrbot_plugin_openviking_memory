"""
Observability of the two things that used to be invisible.

1. Commit is two-phase: archiving finishes before the call returns, memory
   extraction runs on afterwards. A plugin that stops at "HTTP 200" cannot tell
   "accepted" from "extracted", which is exactly the confusion that made a
   successful-looking commit look like a lost memory.
2. Recall injection has a last-resort path that rewrites the system prompt and
   costs prefix-cache hits. It used to be silent, so a quality regression and a
   healthy run looked the same in the logs.
"""

from __future__ import annotations

import asyncio

from ov_client.commit_scheduler import CommitScheduler
from ov_client.config import PluginConfig
from ov_client.injection import inject_recall_block, injection_fallback_count

SESSION = "astrbot-sess-aiocqhttp-group-123"


def _run(coro):
    return asyncio.run(coro)


class FakeClient:
    def __init__(self, *, commit_result=None, task=None):
        self.commit_result = commit_result
        self.task = task
        self.commit_calls = 0
        self.task_calls: list[str] = []

    async def commit_session(self, session_id, **kwargs):
        self.commit_calls += 1
        return self.commit_result

    async def get_task(self, task_id, **kwargs):
        self.task_calls.append(task_id)
        return self.task


def make_scheduler(client) -> CommitScheduler:
    return CommitScheduler(client, PluginConfig({}))


async def commit_once(sched, client):
    """Register some pending work and drive one commit through.

    The counts sit above the default thresholds (20 messages / 4096 tokens) so
    that ``evaluate`` actually decides to commit.
    """
    sched.set_auth(SESSION, {"api_key": "k"})
    state = sched._get_state(SESSION)
    state.pending_messages = 25
    state.pending_tokens = 5000
    await sched.evaluate(SESSION)
    return state


# -- commit states ---------------------------------------------------------


def test_commit_records_that_extraction_is_still_running():
    client = FakeClient(commit_result={"task_id": "task-abc", "status": "ok"})

    async def run():
        sched = make_scheduler(client)
        state = await commit_once(sched, client)
        return state, sched.get_status(SESSION)

    state, status = _run(run())
    assert status["commit_state"] == "extracting"
    assert status["extract_task_id"] == "task-abc"
    assert state.pending_messages == 0  # archived, so nothing left to resend
    assert state.last_commit_ts > 0


def test_commit_without_task_id_is_archived_only():
    client = FakeClient(commit_result={"status": "ok"})

    async def run():
        sched = make_scheduler(client)
        await commit_once(sched, client)
        return sched.get_status(SESSION)["commit_state"]

    assert _run(run()) == "archived"


def test_failed_commit_keeps_pending_messages():
    client = FakeClient(commit_result=None)

    async def run():
        sched = make_scheduler(client)
        state = await commit_once(sched, client)
        return state, sched.get_status(SESSION)

    state, status = _run(run())
    assert status["commit_state"] == "commit_failed"
    # The messages are still only local, so they must stay counted for the next try.
    assert state.pending_messages == 25
    assert state.pending_tokens == 5000


def test_commit_exception_is_reported_not_swallowed():
    class Boom(FakeClient):
        async def commit_session(self, session_id, **kwargs):
            raise RuntimeError("socket closed")

    client = Boom()

    async def run():
        sched = make_scheduler(client)
        state = await commit_once(sched, client)
        return state, sched.get_status(SESSION)

    state, status = _run(run())
    assert status["commit_state"] == "commit_failed"
    assert "RuntimeError" in status["commit_detail"]
    assert state.pending_messages == 25


def test_failed_commit_drops_the_previous_extraction_task():
    """A stale task id would let an old result overwrite the new failure."""
    client = FakeClient(commit_result={"task_id": "task-old"}, task={"status": "completed"})

    async def run():
        sched = make_scheduler(client)
        await commit_once(sched, client)
        first_task = sched.get_status(SESSION)["extract_task_id"]

        client.commit_result = None  # the next commit is rejected
        state = sched._get_state(SESSION)
        state.pending_messages = 25
        state.pending_tokens = 5000
        await sched.evaluate(SESSION)
        status = sched.get_status(SESSION)

        client.task_calls.clear()
        await sched.poll_commit_tasks()
        return first_task, status, client.task_calls

    first_task, status, later_calls = _run(run())
    assert first_task == "task-old"
    assert status["commit_state"] == "commit_failed"
    assert status["extract_task_id"] == ""
    assert later_calls == []


def test_commit_exception_also_drops_the_previous_task():
    class Flaky(FakeClient):
        def __init__(self):
            super().__init__(commit_result={"task_id": "task-old"})
            self.fail = False

        async def commit_session(self, session_id, **kwargs):
            if self.fail:
                raise RuntimeError("socket closed")
            return self.commit_result

    client = Flaky()

    async def run():
        sched = make_scheduler(client)
        await commit_once(sched, client)
        client.fail = True
        state = sched._get_state(SESSION)
        state.pending_messages = 25
        state.pending_tokens = 5000
        await sched.evaluate(SESSION)
        return sched.get_status(SESSION)

    status = _run(run())
    assert status["commit_state"] == "commit_failed"
    assert status["extract_task_id"] == ""


# -- extraction polling ----------------------------------------------------


def test_poll_marks_extraction_completed():
    client = FakeClient(
        commit_result={"task_id": "task-1"},
        task={"status": "completed", "stage": "extract"},
    )

    async def run():
        sched = make_scheduler(client)
        await commit_once(sched, client)
        changed = await sched.poll_commit_tasks()
        return changed, sched.get_status(SESSION)["commit_state"]

    changed, state = _run(run())
    assert changed == 1
    assert state == "extracted"


def test_poll_reports_extraction_failure_with_reason():
    client = FakeClient(
        commit_result={"task_id": "task-2"},
        task={"status": "failed", "error": "embedding provider down"},
    )

    async def run():
        sched = make_scheduler(client)
        await commit_once(sched, client)
        await sched.poll_commit_tasks()
        return sched.get_status(SESSION)

    status = _run(run())
    assert status["commit_state"] == "extract_failed"
    assert "embedding provider down" in status["commit_detail"]


def test_running_task_is_left_alone():
    client = FakeClient(commit_result={"task_id": "task-3"}, task={"status": "running"})

    async def run():
        sched = make_scheduler(client)
        await commit_once(sched, client)
        changed = await sched.poll_commit_tasks()
        return changed, sched.get_status(SESSION)["commit_state"]

    changed, state = _run(run())
    assert changed == 0
    assert state == "extracting"


def test_expired_task_stops_the_polling():
    client = FakeClient(commit_result={"task_id": "task-4"}, task={"status": "gone"})

    async def run():
        sched = make_scheduler(client)
        await commit_once(sched, client)
        await sched.poll_commit_tasks()
        client.task_calls.clear()
        await sched.poll_commit_tasks()  # terminal → must not ask again
        return sched.get_status(SESSION)["commit_state"], client.task_calls

    state, later_calls = _run(run())
    assert state == "extract_unknown"
    assert later_calls == []


def test_sessions_without_a_task_are_not_polled():
    client = FakeClient(commit_result={"status": "ok"})

    async def run():
        sched = make_scheduler(client)
        await commit_once(sched, client)
        return await sched.poll_commit_tasks()

    assert _run(run()) == 0
    assert client.task_calls == []


# -- injection fallback ----------------------------------------------------


class FakePart:
    def __init__(self, text: str) -> None:
        self.text = text

    def mark_as_temp(self) -> None:
        return None


class FakeRequest:
    def __init__(self, with_parts: bool) -> None:
        self.system_prompt = "SYS"
        self.extra_user_content_parts = [] if with_parts else None


def test_tail_injection_does_not_touch_the_system_prompt():
    before = injection_fallback_count()
    req = FakeRequest(with_parts=True)

    inject_recall_block(req, "BLOCK", text_part_cls=FakePart)

    assert req.system_prompt == "SYS"
    assert [p.text for p in req.extra_user_content_parts] == ["BLOCK"]
    assert injection_fallback_count() == before


def test_system_prompt_fallback_is_counted():
    before = injection_fallback_count()
    req = FakeRequest(with_parts=False)

    inject_recall_block(req, "BLOCK")

    assert req.system_prompt.endswith("BLOCK")
    assert injection_fallback_count() == before + 1
