"""
Self-driven session commit scheduler.

AstrBot does not expose a PreCompact hook, so we drive commit cadence ourselves
with four complementary triggers:
1. Message count threshold
2. Token estimate threshold
3. Idle timeout
4. Explicit flush (on terminate)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ._log import logger

if TYPE_CHECKING:
    from .client import OVClient
    from .config import PluginConfig


@dataclass
class SessionState:
    pending_messages: int = 0
    pending_tokens: int = 0
    last_message_ts: float = field(default_factory=time.time)
    last_commit_ts: float = 0.0
    committing: bool = False
    idle_handle: asyncio.TimerHandle | None = None
    # Commit is two-phase server side: archiving finishes before the call
    # returns, memory extraction keeps running and is only observable through
    # the task API. Keeping both apart is what tells "accepted" from "extracted".
    commit_state: str = ""
    commit_detail: str = ""
    extract_task_id: str = ""


# States a commit task cannot leave, so polling can stop.
TERMINAL_COMMIT_STATES = {"extracted", "extract_failed", "extract_unknown"}

# Task statuses that mean "still working".
PENDING_TASK_STATUSES = {"", "pending", "running", "cancelling"}


class CommitScheduler:
    def __init__(self, client: OVClient, cfg: PluginConfig):
        from .client import PEER_MEMORY_POLICY

        self._client = client
        self._cfg = cfg
        self._sessions: dict[str, SessionState] = {}
        self._auth: dict[str, dict] = {}
        self._memory_policy = PEER_MEMORY_POLICY if cfg.peer_enabled else None
        self._lock = asyncio.Lock()
        # Commits spawned in the background. Tracked so flush_all() can wait for
        # them instead of racing with them.
        self._inflight: set[asyncio.Task] = set()

    def set_auth(self, session_id: str, auth: dict):
        self._auth[session_id] = auth

    def _get_state(self, session_id: str) -> SessionState:
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionState()
        return self._sessions[session_id]

    async def record_message(self, session_id: str, token_estimate: int):
        state = self._get_state(session_id)
        state.pending_messages += 1
        state.pending_tokens += token_estimate
        state.last_message_ts = time.time()

        self._reset_idle_timer(session_id, state)

        if await self._should_commit(state):
            self._spawn_commit(session_id)

    def _spawn_commit(self, session_id: str) -> None:
        """Fire a background commit, keeping a handle on it for flush_all()."""
        task = asyncio.create_task(self._do_commit(session_id))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _should_commit(self, state: SessionState) -> bool:
        if state.committing:
            return False
        if state.pending_messages >= self._cfg.commit_message_threshold:
            return True
        if state.pending_tokens >= self._cfg.commit_token_threshold:
            return True
        return False

    def _reset_idle_timer(self, session_id: str, state: SessionState):
        if state.idle_handle is not None:
            state.idle_handle.cancel()
        loop = asyncio.get_running_loop()
        state.idle_handle = loop.call_later(
            self._cfg.commit_idle_seconds,
            lambda: self._spawn_commit(session_id),
        )

    async def evaluate(self, session_id: str):
        state = self._get_state(session_id)
        if await self._should_commit(state):
            await self._do_commit(session_id)

    async def _do_commit(self, session_id: str):
        async with self._lock:
            state = self._get_state(session_id)
            if state.committing or state.pending_messages == 0:
                return
            state.committing = True

        auth = self._auth.get(session_id, {})
        try:
            result = await self._client.commit_session(
                session_id, memory_policy=self._memory_policy, **auth
            )
            if result is not None:
                logger.info(
                    "committed session %s (%d msgs, ~%d tokens)",
                    session_id,
                    state.pending_messages,
                    state.pending_tokens,
                )
                state.pending_messages = 0
                state.pending_tokens = 0
                state.last_commit_ts = time.time()
                task_id = ""
                if isinstance(result, dict):
                    task_id = str(result.get("task_id") or "")
                state.extract_task_id = task_id
                # Archive done; extraction is a background task now.
                state.commit_state = "extracting" if task_id else "archived"
                state.commit_detail = ""
            else:
                state.commit_state = "commit_failed"
                state.commit_detail = "commit request failed"
                logger.warning(
                    "commit rejected for session %s; pending kept for the next try",
                    session_id,
                )
        except Exception as exc:
            state.commit_state = "commit_failed"
            state.commit_detail = f"{type(exc).__name__}"
            logger.exception("commit failed for session %s", session_id)
        finally:
            state.committing = False

    async def poll_commit_tasks(self) -> int:
        """Refresh the extraction state of recent commits.

        A successful commit only means the archive phase finished. Whether the
        memories were actually extracted shows up on the task, so without this
        the plugin would report "committed" forever and the operator could never
        tell the two apart.

        Returns:
            How many sessions changed state.
        """
        changed = 0
        for session_id, state in list(self._sessions.items()):
            if not state.extract_task_id or state.commit_state in TERMINAL_COMMIT_STATES:
                continue
            task = await self._client.get_task(
                state.extract_task_id, **self._auth.get(session_id, {})
            )
            if not isinstance(task, dict):
                continue
            status = str(task.get("status") or "")
            if status in PENDING_TASK_STATUSES:
                continue
            if status == "completed":
                new_state = "extracted"
            elif status in ("failed", "cancelled"):
                new_state = "extract_failed"
            else:
                new_state = "extract_unknown"
            detail = str(task.get("error") or task.get("stage") or "")
            if new_state != state.commit_state:
                changed += 1
                logger.info(
                    "session %s extraction %s%s",
                    session_id,
                    new_state,
                    f" ({detail[:120]})" if detail else "",
                )
            state.commit_state = new_state
            state.commit_detail = detail
        return changed

    async def flush_all(self):
        """Drain every session, including commits already in flight.

        Without waiting for in-flight commits first, a session that is mid-commit
        would be skipped by ``_do_commit``'s ``committing`` guard and its pending
        messages would be dropped on shutdown.
        """
        for state in self._sessions.values():
            if state.idle_handle is not None:
                state.idle_handle.cancel()
                state.idle_handle = None

        if self._inflight:
            await asyncio.gather(*list(self._inflight), return_exceptions=True)

        tasks = [
            self._do_commit(session_id)
            for session_id, state in list(self._sessions.items())
            if state.pending_messages > 0
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def get_status(self, session_id: str) -> dict:
        state = self._get_state(session_id)
        return {
            "pending_messages": state.pending_messages,
            "pending_tokens": state.pending_tokens,
            "last_commit_ts": state.last_commit_ts,
            "committing": state.committing,
            "commit_state": state.commit_state,
            "commit_detail": state.commit_detail,
            "extract_task_id": state.extract_task_id,
        }
