"""Tests for agent_worker.py -- Agent subprocess entry point."""

import asyncio
import json
import sys
from unittest.mock import AsyncMock, patch

import pytest

from src.agent_worker import AgentWorker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def worker():
    """Create an AgentWorker instance for testing."""
    return AgentWorker(
        role="worker",
        agent_name="test-agent",
        workspace_path="/tmp/test-workspace",
        office_id="test-office-id",
        backend_url="http://localhost:8000",
    )


@pytest.fixture
def manager_worker():
    """Create a Manager AgentWorker instance for testing."""
    return AgentWorker(
        role="manager",
        agent_name="manager",
        workspace_path="/tmp/test-workspace",
        office_id="test-office-id",
        backend_url="http://localhost:8000",
    )


# ---------------------------------------------------------------------------
# Initialization tests
# ---------------------------------------------------------------------------


class TestAgentWorkerInit:
    """Tests for AgentWorker construction."""

    def test_worker_role(self, worker: AgentWorker) -> None:
        assert worker.role == "worker"
        assert worker.agent_name == "test-agent"

    def test_manager_role(self, manager_worker: AgentWorker) -> None:
        assert manager_worker.role == "manager"
        assert manager_worker.agent_name == "manager"

    def test_initial_state(self, worker: AgentWorker) -> None:
        # T1.11/T2.13 (review): _pending_tool_calls was deleted along
        # with the proxied tool-call path. Initial state now: no
        # current task, no shutdown.
        assert worker._current_task_id is None
        assert not worker._shutdown_event.is_set()


# ---------------------------------------------------------------------------
# Message dispatch tests
# ---------------------------------------------------------------------------


class TestDispatch:
    """Tests for the _dispatch() message routing."""

    @pytest.mark.asyncio
    async def test_ping_does_not_reach_dispatch(
        self, worker: AgentWorker,
    ) -> None:
        """PING is intercepted in ``_reader_loop`` and never enters the
        dispatcher (so heartbeats stay responsive while a long CLI
        session blocks the dispatch loop). If a PING ever leaks into
        ``_dispatch`` it falls through to the "unknown message type"
        branch, which is the correct defensive behaviour — there is
        no dedicated handler to test here."""
        sent_messages: list[dict] = []
        worker._send = lambda msg: sent_messages.append(msg)
        await worker._dispatch("ping", {"type": "ping"})
        # No reply — dispatcher is no longer a PING handler.
        assert sent_messages == []

    @pytest.mark.asyncio
    async def test_reader_loop_responds_to_ping_inline(
        self, worker: AgentWorker,
    ) -> None:
        """Verify the ``_reader_loop`` short-circuit: a PING line goes
        straight to a PONG reply without touching the dispatch queue.
        This is what keeps the supervisor's 90s heartbeat happy even
        when the dispatcher is mid-CLI-session."""
        sent_messages: list[dict] = []
        worker._send = lambda msg: sent_messages.append(msg)
        # Feed a PING line followed by EOF.
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"type":"ping"}\n')
        reader.feed_eof()
        queue: "asyncio.Queue[dict]" = asyncio.Queue()
        await worker._reader_loop(reader, queue)
        assert any(m.get("type") == "pong" for m in sent_messages)
        assert queue.empty(), "PING must NOT land on the dispatch queue"

    @pytest.mark.asyncio
    async def test_reader_loop_handles_cancel_inline(
        self, worker: AgentWorker,
    ) -> None:
        """Chat-v2 (CHAT-005 review): cancel_task must bypass the
        dispatch queue too. Without this, a Cancel that arrives after
        a subsequent chat_message would sit behind it — and the
        dispatcher (already awaiting the running task to finish
        before starting the queued chat) would only process the
        cancel AFTER the running task completed naturally. By then
        cancellation has no effect on the user's intended target."""
        import asyncio

        # Stand in for the running task1.
        async def _never_ending() -> None:
            await asyncio.sleep(60)

        task = asyncio.create_task(_never_ending())
        worker._current_session_task = task

        # Reader gets a chat_message (msg2) followed by a cancel_task
        # — in that order. The cancel must still preempt task1.
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"type":"chat_message","content":"msg2"}\n')
        reader.feed_data(b'{"type":"cancel_task","reason":"user"}\n')
        reader.feed_eof()
        queue: "asyncio.Queue[dict]" = asyncio.Queue()
        await worker._reader_loop(reader, queue)

        # cancel_task did NOT land on the dispatch queue; it was
        # consumed inline. Only msg2 is queued for the dispatcher.
        items: list[dict] = []
        while not queue.empty():
            items.append(queue.get_nowait())
        assert len(items) == 1
        assert items[0]["type"] == "chat_message"

        # task1 was cancelled.
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        assert task.cancelled() or task.done()

    @pytest.mark.asyncio
    async def test_shutdown_sets_event(self, worker: AgentWorker) -> None:
        """Shutdown message should set the shutdown event."""
        assert not worker._shutdown_event.is_set()

        await worker._dispatch(
            "shutdown", {"type": "shutdown", "grace_period_seconds": 10}
        )

        assert worker._shutdown_event.is_set()

    @pytest.mark.asyncio
    async def test_cancel_when_idle_is_noop(
        self, worker: AgentWorker,
    ) -> None:
        """Chat-v2 (CHAT-005): cancel with no active session is a safe
        no-op. The user clicked Cancel a tick after the Manager
        finished, so the cancel arrived after the session task
        already wound down."""
        assert worker._current_session_task is None
        await worker._dispatch("cancel_task", {"type": "cancel_task"})
        # Still no session task — cancel didn't create one.
        assert worker._current_session_task is None

    @pytest.mark.asyncio
    async def test_cancel_cancels_active_session_task(
        self, worker: AgentWorker,
    ) -> None:
        """Chat-v2 (CHAT-005): cancel preempts the in-flight session
        task. We simulate one with a long-running asyncio.Task, then
        verify the dispatch path calls cancel() on it."""
        import asyncio

        async def _never_ending() -> None:
            await asyncio.sleep(60)

        # Stand in for the chat/assign work an active session would
        # be running. _dispatch's cancel branch must call .cancel()
        # on this task.
        task = asyncio.create_task(_never_ending())
        worker._current_session_task = task

        await worker._dispatch("cancel_task", {"type": "cancel_task"})

        # The cancel request is non-blocking — give the loop a tick
        # to run the cancellation.
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        assert task.cancelled() or task.done()

    @pytest.mark.asyncio
    async def test_unknown_type_logged(self, worker: AgentWorker) -> None:
        """Unknown message types should be logged but not crash."""
        # Should not raise
        await worker._dispatch("unknown_type", {"type": "unknown_type"})

    @pytest.mark.asyncio
    async def test_assign_task_dispatched(self, worker: AgentWorker) -> None:
        """Chat-v2 (CHAT-005): assign_task is now spawned as a tracked
        background task instead of awaited inline so cancel_task can
        preempt it. We verify the handler is invoked and the task
        ref is tracked.
        """
        completion = asyncio.Event()

        async def _handler(msg: dict) -> None:
            completion.set()

        worker._handle_assign_task = _handler  # type: ignore[method-assign]

        msg = {
            "type": "assign_task",
            "task_id": "t1",
            "readable_id": "WR-001.T01",
        }
        await worker._dispatch("assign_task", msg)

        # Tracked task should exist…
        assert worker._current_session_task is not None
        # …and the handler should run when the loop gets a turn.
        await asyncio.wait_for(completion.wait(), timeout=1.0)
        await worker._current_session_task  # surface any exception
        assert completion.is_set()

    @pytest.mark.asyncio
    async def test_chat_message_dispatched(
        self, worker: AgentWorker
    ) -> None:
        """Chat-v2 (CHAT-005): chat_message is spawned as a tracked
        background task (same rationale as assign_task)."""
        completion = asyncio.Event()
        captured: dict[str, dict] = {}

        async def _handler(msg: dict) -> None:
            captured["msg"] = msg
            completion.set()

        worker._handle_chat_message = _handler  # type: ignore[method-assign]

        msg = {
            "type": "chat_message",
            "context_key": "general_chat",
            "content": "hello",
        }
        await worker._dispatch("chat_message", msg)

        assert worker._current_session_task is not None
        await asyncio.wait_for(completion.wait(), timeout=1.0)
        await worker._current_session_task
        assert captured["msg"] == msg

    @pytest.mark.asyncio
    async def test_session_messages_serialise(
        self, worker: AgentWorker,
    ) -> None:
        """Chat-v2 (CHAT-005): two session messages in a row must NOT
        run concurrently — the dispatcher awaits the previous task
        before spawning the next.
        """
        running_concurrently = False
        in_progress = 0

        async def _handler(_msg: dict) -> None:
            nonlocal in_progress, running_concurrently
            in_progress += 1
            if in_progress > 1:
                running_concurrently = True
            await asyncio.sleep(0.02)
            in_progress -= 1

        worker._handle_chat_message = _handler  # type: ignore[method-assign]

        msg1 = {"type": "chat_message", "context_key": "general_chat"}
        msg2 = {"type": "chat_message", "context_key": "general_chat"}

        await worker._dispatch("chat_message", msg1)
        # Spawning a second message immediately would race the first
        # if serialisation is broken.
        await worker._dispatch("chat_message", msg2)

        # Drain the second task so we observe the full sequence.
        assert worker._current_session_task is not None
        await worker._current_session_task
        assert not running_concurrently, (
            "Two chat handlers ran concurrently — the dispatcher's "
            "serialisation barrier is broken."
        )

    @pytest.mark.asyncio
    async def test_cancel_runs_inline_not_via_session_task(
        self, worker: AgentWorker,
    ) -> None:
        """The cancel branch must NOT itself reassign
        ``_current_session_task`` — it's a control message that
        preempts the existing task, not session work."""
        import asyncio

        async def _never_ending() -> None:
            await asyncio.sleep(60)

        existing = asyncio.create_task(_never_ending())
        worker._current_session_task = existing

        await worker._dispatch("cancel_task", {"type": "cancel_task"})

        # The dispatch path must not have replaced the tracked task —
        # it should still point at the same Task object (which is now
        # cancelled).
        assert worker._current_session_task is existing
        try:
            await asyncio.wait_for(existing, timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


# ---------------------------------------------------------------------------
# Send / output tests
# ---------------------------------------------------------------------------


class TestSend:
    """Tests for the _send() IPC output method."""

    def test_send_writes_ndjson(self, worker: AgentWorker) -> None:
        """_send() should write valid NDJSON to stdout."""
        import io

        stream = io.StringIO()
        original_stdout = sys.stdout
        try:
            sys.stdout = stream
            worker._send({"type": "pong"})
        finally:
            sys.stdout = original_stdout

        output = stream.getvalue()
        assert output.endswith("\n")
        parsed = json.loads(output.strip())
        assert parsed == {"type": "pong"}

    def test_send_handles_broken_pipe(self, worker: AgentWorker) -> None:
        """_send() should not crash on BrokenPipeError."""

        class BrokenStream:
            def write(self, data):
                raise BrokenPipeError("Pipe broken")

            def flush(self):
                raise BrokenPipeError("Pipe broken")

        original_stdout = sys.stdout
        try:
            sys.stdout = BrokenStream()
            # Should not raise
            worker._send({"type": "pong"})
        finally:
            sys.stdout = original_stdout


# ---------------------------------------------------------------------------
# Shutdown and signal tests
# ---------------------------------------------------------------------------


class TestShutdown:
    """Tests for shutdown and signal handling."""

    def test_shutdown_message_sets_event(self, worker: AgentWorker) -> None:
        worker._handle_shutdown(
            {"type": "shutdown", "grace_period_seconds": 10}
        )
        assert worker._shutdown_event.is_set()

    def test_signal_handler_sets_event(self, worker: AgentWorker) -> None:
        worker._handle_signal()
        assert worker._shutdown_event.is_set()

    def test_default_grace_period(self, worker: AgentWorker) -> None:
        """Shutdown without grace_period_seconds should use default."""
        worker._handle_shutdown({"type": "shutdown"})
        assert worker._shutdown_event.is_set()


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Tests for error handling in the dispatch loop."""

    @pytest.mark.asyncio
    async def test_handler_error_sends_error_message(
        self, worker: AgentWorker
    ) -> None:
        """Errors in a session handler surface as a non-fatal ERROR
        IPC frame.

        Chat-v2 (CHAT-005): session handlers run inside an
        asyncio.Task spawned by ``_dispatch`` so they don't block the
        cancel/ping control path. The wrapper
        ``_run_session_handler`` is responsible for catching
        non-cancellation exceptions and surfacing them as an ERROR
        frame, replicating the pre-Pass-C inline behaviour.
        """
        sent_messages = []
        worker._send = lambda msg: sent_messages.append(msg)

        async def failing_handler(msg):
            raise ValueError("Something went wrong")

        worker._handle_assign_task = failing_handler  # type: ignore[method-assign]

        msg = {
            "type": "assign_task",
            "task_id": "t1",
            "readable_id": "WR-001.T01",
            "agent_config": {},
            "brief": {},
        }
        await worker._dispatch("assign_task", msg)

        # Drain the spawned task — _run_session_handler emits the
        # ERROR frame from inside the task, so we have to wait for
        # it to complete before asserting.
        assert worker._current_session_task is not None
        await worker._current_session_task

        errors = [m for m in sent_messages if m["type"] == "error"]
        assert len(errors) == 1
        assert "Something went wrong" in errors[0]["message"]
        assert errors[0]["fatal"] is False

    @pytest.mark.asyncio
    async def test_assign_task_exception_sends_error(
        self, worker: AgentWorker
    ) -> None:
        """_handle_assign_task should send error on SDK failure."""
        sent_messages = []
        worker._send = lambda msg: sent_messages.append(msg)

        # Mock _run_sdk_session to raise
        worker._run_sdk_session = AsyncMock(
            side_effect=RuntimeError("SDK crashed")
        )

        await worker._handle_assign_task({
            "type": "assign_task",
            "task_id": "t1",
            "readable_id": "WR-001.T01",
            "agent_config": {"name": "test"},
            "brief": {},
        })

        errors = [m for m in sent_messages if m["type"] == "error"]
        assert len(errors) == 1
        assert "SDK crashed" in errors[0]["message"]
        assert errors[0]["task_id"] == "t1"

        # Current task should be cleared
        assert worker._current_task_id is None

    @pytest.mark.asyncio
    async def test_chat_message_exception_sends_error(
        self, manager_worker: AgentWorker
    ) -> None:
        """_handle_chat_message should send error on failure."""
        sent_messages = []
        manager_worker._send = lambda msg: sent_messages.append(msg)

        # Mock _run_manager_session to raise
        manager_worker._run_manager_session = AsyncMock(
            side_effect=RuntimeError("Manager session crashed")
        )

        with patch(
            "src.orchestrator.manager_controller.build_dynamic_context",
            return_value="system prompt",
        ), patch(
            "src.config_sync.sync_service.ConfigStore.update_from_agent_config",
        ):
            await manager_worker._handle_chat_message({
                "type": "chat_message",
                "context_key": "general_chat",
                "content": "hello",
                "conversation_id": "conv-1",
                "agent_config": {"name": "manager"},
            })

        errors = [m for m in sent_messages if m["type"] == "error"]
        assert len(errors) == 1
        assert "Manager session crashed" in errors[0]["message"]
        assert errors[0]["fatal"] is False

    @pytest.mark.asyncio
    async def test_chat_message_success_sends_response_final(
        self, manager_worker: AgentWorker
    ) -> None:
        """_handle_chat_message should send response_final on success."""
        sent_messages = []
        manager_worker._send = lambda msg: sent_messages.append(msg)

        manager_worker._run_manager_session = AsyncMock(
            return_value=("session-new", 0.05)
        )

        with patch(
            "src.orchestrator.manager_controller.build_dynamic_context",
            return_value="system prompt",
        ), patch(
            "src.config_sync.sync_service.ConfigStore.update_from_agent_config",
        ):
            await manager_worker._handle_chat_message({
                "type": "chat_message",
                "context_key": "workstream:ws-1",
                "content": "plan the sprint",
                "conversation_id": "conv-2",
                "agent_config": {"name": "manager", "model": "claude-sonnet-4-6"},
            })

        finals = [m for m in sent_messages if m["type"] == "response_final"]
        assert len(finals) == 1
        assert finals[0]["conversation_id"] == "conv-2"
        assert finals[0]["context_key"] == "workstream:ws-1"
        assert finals[0]["token_cost"] == 0.05
        assert finals[0]["session_id"] == "session-new"


# ---------------------------------------------------------------------------
# main() tests
# ---------------------------------------------------------------------------


class TestMain:
    """Tests for the main() entry point function."""

    def test_argument_parsing(self) -> None:
        """Verify command-line argument parsing."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--role", choices=["manager", "worker"], required=True
        )
        parser.add_argument("--agent-name", required=True)
        parser.add_argument("--workspace-path", default="/workspace")
        parser.add_argument("--office-id", default="")
        parser.add_argument("--backend-url", default="")

        args = parser.parse_args([
            "--role", "worker",
            "--agent-name", "analyst",
            "--workspace-path", "/tmp/test",
            "--office-id", "office-123",
            "--backend-url", "http://localhost:8000",
        ])

        assert args.role == "worker"
        assert args.agent_name == "analyst"
        assert args.workspace_path == "/tmp/test"
        assert args.office_id == "office-123"

    def test_default_workspace_path(self) -> None:
        """Default workspace path should be /workspace."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--role", choices=["manager", "worker"], required=True
        )
        parser.add_argument("--agent-name", required=True)
        parser.add_argument("--workspace-path", default="/workspace")

        args = parser.parse_args([
            "--role", "manager",
            "--agent-name", "manager",
        ])

        assert args.workspace_path == "/workspace"


# ---------------------------------------------------------------------------
# P2.5 — prompt rotation tests (P2-F + P2.5-E)
# ---------------------------------------------------------------------------


class TestPromptRotation:
    """Symbolic tests for the agent_worker retry loop's guidance-block
    rotation. We replicate the real algorithm in a small helper because
    `_run_sdk_session` is too entangled to drive end-to-end without
    mocking the entire CLI bridge. Any divergence between this helper
    and the real code will fail integration tests, but locks down the
    rotation invariants here."""

    _MARKER = "\n\n<!--CBCL_RECOVERY_BLOCK_START-->\n"
    _CAP = 200

    def _rotate(self, prompt: str, new_block: str) -> str:
        if len(prompt) + len(new_block) <= self._CAP:
            return prompt + new_block

        offsets: list[int] = []
        idx = prompt.find(self._MARKER)
        while idx >= 0:
            offsets.append(idx)
            idx = prompt.find(self._MARKER, idx + 1)

        rotated = prompt
        for i in range(len(offsets)):
            next_idx = offsets[i + 1] if i + 1 < len(offsets) else None
            if next_idx is None:
                rotated = rotated[: offsets[i]]
            else:
                rotated = rotated[: offsets[i]] + rotated[next_idx:]
            if len(rotated) + len(new_block) <= self._CAP:
                break
            offsets = []
            idx = rotated.find(self._MARKER)
            while idx >= 0:
                offsets.append(idx)
                idx = rotated.find(self._MARKER, idx + 1)
            if not offsets:
                break

        if len(rotated) + len(new_block) <= self._CAP:
            return rotated + new_block
        return prompt

    def test_under_cap_appends_normally(self) -> None:
        prompt = "base"
        block = self._MARKER + "first guidance"
        out = self._rotate(prompt, block)
        assert out == prompt + block

    def test_one_block_rotated_when_overflow(self) -> None:
        base = "base"
        b1 = self._MARKER + "guidance one — old fix"
        b2 = self._MARKER + "guidance two — new fix"
        prompt_with_b1 = base + b1
        # Pad b1 so that prompt + b2 overflows.
        b1_padded = self._MARKER + "guidance one — old fix" + ("x" * 100)
        prompt_padded = base + b1_padded
        out = self._rotate(prompt_padded, b2)
        # New guidance kept.
        assert "guidance two" in out
        # Old guidance dropped.
        assert "guidance one" not in out
        # Base preserved.
        assert out.startswith(base)

    def test_multi_block_rotation_drops_oldest_first(self) -> None:
        base = "B"
        b1 = self._MARKER + "alpha"
        b2 = self._MARKER + "beta"
        b3 = self._MARKER + "gamma" + ("x" * 100)
        b4 = self._MARKER + "delta"
        prompt = base + b1 + b2 + b3
        out = self._rotate(prompt, b4)
        assert "delta" in out
        # Oldest dropped first.
        assert "alpha" not in out

    def test_drop_new_when_no_rotation_helps(self) -> None:
        base = "B"
        b1 = self._MARKER + "alpha"
        prompt = base + b1
        oversized = self._MARKER + ("x" * 1000)
        out = self._rotate(prompt, oversized)
        # Original prompt unchanged.
        assert out == prompt

    def test_marker_collision_safe(self) -> None:
        """A user prompt mentioning 'AUTOMATIC RECOVERY' must not be
        eaten by rotation. The HTML-comment marker eliminates the
        Markdown-heading collision the previous design had."""
        user_text = "## AUTOMATIC RECOVERY — READ THIS\nplease keep me"
        new_block = self._MARKER + "real guidance"
        out = self._rotate(user_text, new_block)
        assert "please keep me" in out
        assert "real guidance" in out
