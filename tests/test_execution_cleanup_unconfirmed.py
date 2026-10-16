"""A session whose processes cannot be confirmed stopped (ID-001.T469).

The session bridge retries the container cleanup twice and then raises
``ExecutionCleanupUnconfirmed``. The worker never starts another CLI attempt
after it: a session that finished or escalated still reports that outcome
(the supervisor delivers it once the stop is confirmed), anything else sends
a fatal error, and the worker process exits. Any other unexpected exception
on a board task is fatal too, so a live idle worker can never sit behind a
WORKING supervisor record.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src._agent_worker_task import handle_assign_task, run_sdk_session
from src.agent_protocol import MessageType
from src.agent_worker import AgentErrorEscalation, AgentWorker
from src.docker import session_bridge, task_process_cleanup
from src.docker.session_bridge import SessionMessage
from src.docker.task_process_cleanup import (
    ExecutionCleanupUnconfirmed,
    WorkerCleanupError,
)

AGENT_CONFIG = {"_container_name": "cbcl-office-test", "model": "claude-opus-4-7"}
DIAGNOSTIC = "live_process_unreadable: ssh-agent (pid 4321, running 2 h)"
WALL_CAP = "Session timed out: exceeded the per-attempt wall-clock cap of 14400s"


# ---------------------------------------------------------------------------
# Session bridge
# ---------------------------------------------------------------------------


def test_retry_delays_are_two_and_ten_seconds():
    assert session_bridge._CLEANUP_RETRY_DELAYS_SECONDS == (2.0, 10.0)


async def test_cleanup_is_retried_and_a_later_success_continues(monkeypatch):
    cleanup = AsyncMock(side_effect=[WorkerCleanupError("processes_remain"), None])
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    monkeypatch.setattr(session_bridge, "_CLEANUP_RETRY_DELAYS_SECONDS", (0.0, 0.0))
    await session_bridge._confirm_session_stopped("office", "a" * 64)
    assert cleanup.await_count == 2


async def test_cleanup_failing_every_attempt_raises_the_typed_exception(monkeypatch):
    slept: list[float] = []
    original_sleep = asyncio.sleep

    async def record_sleep(seconds):
        slept.append(seconds)
        await original_sleep(0)

    last = WorkerCleanupError("live_process_unreadable", DIAGNOSTIC)
    cleanup = AsyncMock(side_effect=[
        WorkerCleanupError("live_process_unreadable"), TimeoutError(), last,
    ])
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    monkeypatch.setattr(session_bridge.asyncio, "sleep", record_sleep)
    with pytest.raises(ExecutionCleanupUnconfirmed) as caught:
        await session_bridge._confirm_session_stopped("office", "a" * 64)
    assert slept == [2.0, 10.0]
    assert cleanup.await_count == 3
    failure = caught.value
    assert (failure.reason, failure.diagnostic, failure.attempts) == (
        "live_process_unreadable", DIAGNOSTIC, 3,
    )
    assert failure.__cause__ is last
    # The Manager's error copy recognises the message as a cleanup failure.
    assert "task-scoped container cancellation failed" in str(failure).lower()
    assert "execution cleanup unconfirmed" in str(failure).lower()


async def test_cancellation_is_never_retried_or_wrapped(monkeypatch):
    cleanup = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    with pytest.raises(asyncio.CancelledError):
        await session_bridge._confirm_session_stopped("office", "a" * 64)
    assert cleanup.await_count == 1


class _FinishedProcess:
    """A ``docker exec`` client whose CLI printed nothing and exited."""

    def __init__(self) -> None:
        self.pid = 4242
        self.stdin = MagicMock()
        self.stdin.drain = AsyncMock()
        self.stdout = MagicMock()
        self.stdout.readline = AsyncMock(return_value=b"")
        self.stderr = MagicMock()
        self.stderr.read = AsyncMock(return_value=b"")
        self.returncode = 0

    async def wait(self) -> int:
        return 0

    async def communicate(
        self, input: bytes | None = None,  # noqa: A002
    ) -> tuple[bytes, bytes]:
        return b"", b""

    def kill(self) -> None:
        return None


async def test_unconfirmed_cleanup_still_removes_session_files(monkeypatch):
    # The stream itself: every cleanup attempt fails, the typed exception
    # replaces the stream's end, and the prompt and MCP files (which carry the
    # session's secrets) are removed from the container all the same.
    secret = "ots_SECRET_office_tool_value"
    failure = WorkerCleanupError("live_process_unreadable", DIAGNOSTIC)
    cleanup = AsyncMock(side_effect=failure)
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    monkeypatch.setattr(session_bridge, "_CLEANUP_RETRY_DELAYS_SECONDS", (0.0, 0.0))

    async def fake_exec(*args, **kwargs):
        return _FinishedProcess()

    mcp_config = {
        "mcpServers": {"cubicle-tools": {"env": {"OFFICE_TOOL_SECRET": secret}}},
    }
    with patch.object(asyncio, "create_subprocess_exec", fake_exec), patch(
        "subprocess.run"
    ) as remove_files:
        with pytest.raises(ExecutionCleanupUnconfirmed) as caught:
            async for _ in session_bridge.stream_cli_session(
                container_name="cbcl-office-test",
                model="claude-opus-4-7",
                system_prompt="Private context",
                prompt="Check the board",
                mcp_config=mcp_config,
            ):
                pass
    assert caught.value.__cause__ is failure
    assert caught.value.attempts == cleanup.await_count == 3
    assert caught.value.diagnostic == DIAGNOSTIC
    removal = remove_files.call_args.args[0]
    assert removal[:7] == [
        "docker", "exec", "-u", "agent", "cbcl-office-test", "rm", "-f",
    ]
    assert any(".prompt-" in path for path in removal[7:])
    assert any(".mcp-" in path for path in removal[7:])
    assert secret not in " ".join(removal)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def _worker() -> MagicMock:
    worker = MagicMock()
    worker.backend_url = ""  # skip the get_task_detail fetch
    worker.office_id = "office-1"
    worker.agent_name = "analyst"
    worker.workspace_path = "/tmp/cbcl-test-workspace"
    worker._send = MagicMock()
    worker._build_mcp_config = MagicMock(return_value={})

    async def run(**kwargs):
        return await run_sdk_session(worker, **kwargs)

    worker._run_sdk_session = run
    return worker


def _task(status: str = "ready", **extra) -> dict:
    return {
        "task_id": "task-469",
        "readable_id": "ID-001.T469",
        "status": status,
        "brief": {"goal": "Ship the thing"},
        "agent_config": AGENT_CONFIG,
        **extra,
    }


def _failure() -> ExecutionCleanupUnconfirmed:
    return ExecutionCleanupUnconfirmed("live_process_unreadable", DIAGNOSTIC, 3)


def _stream(monkeypatch, messages: list[SessionMessage]) -> dict:
    """Every attempt yields ``messages``; closing it cannot confirm the stop."""
    calls = {"attempts": 0}

    def factory(**kwargs):
        calls["attempts"] += 1

        async def stream():
            try:
                for message in messages:
                    yield message
            finally:
                raise _failure()

        return stream()

    monkeypatch.setattr(session_bridge, "stream_cli_session", factory)
    return calls


def _frames(worker, kind):
    return [
        frame for frame in (call.args[0] for call in worker._send.call_args_list)
        if frame.get("type") == kind
    ]


def _init(session_id: str) -> SessionMessage:
    return SessionMessage(type="system", data={"subtype": "init", "session_id": session_id})


async def test_failed_session_sends_a_fatal_error_with_its_session_and_exits(monkeypatch):
    worker = _worker()
    calls = _stream(monkeypatch, [
        _init("sess-469"),
        SessionMessage(type="error", data={"error": WALL_CAP, "stderr": ""}),
    ])
    await handle_assign_task(worker, _task())

    # No retry: another CLI attempt may not start beside the old processes.
    assert calls["attempts"] == 1
    assert not _frames(worker, MessageType.TASK_COMPLETE)
    [fatal] = _frames(worker, MessageType.ERROR)
    assert fatal["fatal"] is True
    assert fatal["reason"] == "execution_cleanup_unconfirmed"
    assert fatal["session_id"] == "sess-469"
    assert fatal["task_id"] == "task-469"
    assert fatal["cleanup_error"] == DIAGNOSTIC
    [note] = [
        frame for frame in _frames(worker, MessageType.PROGRESS)
        if frame.get("details", {}).get("error_class") == "execution_cleanup_unconfirmed"
    ]
    assert DIAGNOSTIC in note["content"]
    assert "restarted automatically" in note["content"]
    worker.exit_after_fatal_error.assert_called_once()


async def test_finished_session_still_reports_its_completion_and_exits(monkeypatch):
    worker = _worker()
    _stream(monkeypatch, [
        _init("sess-1"),
        SessionMessage(
            type="result", data={"session_id": "sess-1", "cost_usd": 0.5, "subtype": "success"},
        ),
    ])
    await handle_assign_task(worker, _task())

    [completion] = _frames(worker, MessageType.TASK_COMPLETE)
    assert completion["status"] == "review"
    assert completion["session_id"] == "sess-1"
    assert completion["token_cost"] == 0.5
    assert not _frames(worker, MessageType.ERROR)
    worker.exit_after_fatal_error.assert_called_once()


async def test_escalated_session_still_reports_its_escalation_and_exits():
    worker = _worker()
    failure = _failure()
    failure.interrupted_by = AgentErrorEscalation(
        error_class="TIMEOUT", original_error="Wall-clock budget exhausted",
        escalation_message="Task exceeded the 6-hour wall-clock budget.",
        session_id="sess-2",
    )
    worker._run_sdk_session = AsyncMock(side_effect=failure)
    await handle_assign_task(worker, _task())

    [completion] = _frames(worker, MessageType.TASK_COMPLETE)
    assert completion["status"] == "blocked"
    assert completion["comment"].startswith("ESCALATED (TIMEOUT)")
    assert completion["session_id"] == "sess-2"
    assert not _frames(worker, MessageType.ERROR)
    worker.exit_after_fatal_error.assert_called_once()


async def test_in_loop_exception_is_recorded_as_what_ended_the_stream():
    async def stream():
        try:
            yield SessionMessage(type="assistant", data={})
        finally:
            raise _failure()

    escalation = AgentErrorEscalation(
        error_class="TIMEOUT", original_error="", escalation_message="",
    )
    from src._agent_worker_task import _ClosingSession

    with pytest.raises(ExecutionCleanupUnconfirmed) as caught:
        session = stream()
        async with _ClosingSession(session, lambda: ("sess", 1.0, None)):
            async for _message in session:
                raise escalation
    assert caught.value.interrupted_by is escalation
    assert (caught.value.session_id, caught.value.total_cost) == ("sess", 1.0)


async def test_cancelled_session_with_an_unconfirmed_cleanup_is_fatal():
    worker = _worker()
    started = asyncio.Event()

    async def run(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # The bridge's cleanup replaced the cancellation.
            raise _failure() from None

    worker._run_sdk_session = run
    assignment = asyncio.create_task(handle_assign_task(worker, _task()))
    await started.wait()
    assignment.cancel()
    await asyncio.gather(assignment, return_exceptions=True)

    assert not _frames(worker, MessageType.TASK_COMPLETE)
    [fatal] = _frames(worker, MessageType.ERROR)
    assert fatal["fatal"] is True and fatal["reason"] == "execution_cleanup_unconfirmed"
    worker.exit_after_fatal_error.assert_called_once()


@pytest.mark.parametrize("status", ["ready", "review", "blocked"])
async def test_unexpected_board_task_error_is_fatal_and_exits(status):
    worker = _worker()
    worker._run_sdk_session = AsyncMock(side_effect=ValueError("bad workspace"))
    await handle_assign_task(worker, _task(status))

    [fatal] = _frames(worker, MessageType.ERROR)
    assert fatal["fatal"] is True
    assert fatal["reason"] == "worker_session_failed"
    notes = [
        frame for frame in _frames(worker, MessageType.PROGRESS)
        if frame.get("event_type") == "error"
    ]
    # A Manager Assistant note on a blocked task would start its triage
    # cooldown, so triage posts none.
    assert len(notes) == (0 if status == "blocked" else 1)
    if notes:
        assert "ValueError" in notes[0]["content"]
        assert "bad workspace" not in notes[0]["content"]
    worker.exit_after_fatal_error.assert_called_once()


@pytest.mark.parametrize("consult", ["planner_consult", "flow_consult"])
async def test_unexpected_consult_error_stays_non_fatal(consult):
    worker = _worker()
    worker._run_sdk_session = AsyncMock(side_effect=ValueError("boom"))
    await handle_assign_task(worker, _task("planning", **{consult: {"mode": "specify"}}))

    [error] = _frames(worker, MessageType.ERROR)
    assert error["fatal"] is False
    worker.exit_after_fatal_error.assert_not_called()


async def test_handler_escape_on_a_board_task_is_fatal_and_stops_the_process():
    worker = AgentWorker("worker", "analyst", "/tmp", "office-1")
    worker._send = MagicMock()

    async def failing(msg):
        raise RuntimeError("handler bug")

    await worker._run_session_handler(
        MessageType.ASSIGN_TASK, {"task_id": "task-469"}, failing,
    )
    [frame] = [call.args[0] for call in worker._send.call_args_list]
    assert frame["fatal"] is True
    assert frame["task_id"] == "task-469"
    assert frame["reason"] == "worker_session_failed"
    assert worker._shutdown_event.is_set()


async def test_handler_escape_on_a_chat_turn_stays_non_fatal():
    worker = AgentWorker("manager", "manager", "/tmp", "office-1")
    worker._send = MagicMock()

    async def failing(msg):
        raise RuntimeError("handler bug")

    await worker._run_session_handler(
        MessageType.CHAT_MESSAGE, {"conversation_id": "turn"}, failing,
    )
    [frame] = [call.args[0] for call in worker._send.call_args_list]
    assert frame["fatal"] is False
    assert not worker._shutdown_event.is_set()
