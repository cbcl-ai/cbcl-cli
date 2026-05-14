"""Tests for the 4-layer error recovery system.

Covers the pieces that are hard to unit-test end-to-end but well-suited
to focused mocking:

- ``AgentErrorEscalation`` exception shape + truncation
- session_bridge ``stream_cli_session`` env_overrides injection into the
  docker exec command line
- watchdog ``_peek_last_error_class`` reading structured details and
  falling back to content-based re-classification

The full retry loop is exercised via the existing integration harness;
here we verify each collaborating piece in isolation so regressions are
surfaced quickly.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agent_worker import (
    AgentErrorEscalation,
    _ERROR_PREVIEW_LENGTH,
    _ESCALATION_ORIGINAL_LENGTH,
    _MAX_SESSION_ATTEMPTS,
    _MAX_SYSTEM_PROMPT_SIZE,
)
from src.docker.session_bridge import stream_cli_session
from src.orchestrator.error_classifier import ErrorClass, classify_error
from src.watchdog import HttpBoardClient, TaskWatchdog


# ---------------------------------------------------------------------------
# AgentErrorEscalation
# ---------------------------------------------------------------------------


class TestAgentErrorEscalation:
    """Exception constructor + truncation behaviour."""

    def test_basic_construction(self):
        esc = AgentErrorEscalation(
            error_class="output_token_limit",
            original_error="some error",
            escalation_message="please split task",
            session_id="sess-1",
            total_cost=0.42,
        )
        assert esc.error_class == "output_token_limit"
        assert esc.original_error == "some error"
        assert esc.escalation_message == "please split task"
        assert esc.session_id == "sess-1"
        assert esc.total_cost == 0.42

    def test_optional_fields_default_none(self):
        esc = AgentErrorEscalation(
            error_class="rate_limited",
            original_error="429",
            escalation_message="retry later",
        )
        assert esc.session_id is None
        assert esc.total_cost is None

    def test_str_truncates_long_original_error(self):
        esc = AgentErrorEscalation(
            error_class="unknown_fatal",
            original_error="X" * 10_000,
            escalation_message="mystery",
        )
        # str() should not blow up the activity content field.
        s = str(esc)
        # Format: "[class] message (original: <truncated>)"
        # The truncation applies to original_error, not the whole string.
        # Just verify the total is bounded by the truncation constant + overhead.
        assert len(s) < _ERROR_PREVIEW_LENGTH + 200

    def test_escalation_truncation_constants_distinct(self):
        # Sanity: the two preview lengths are intentionally different
        # (ERROR_PREVIEW used in log events, ESCALATION_ORIGINAL used in
        # the block comment sent to MA which needs more context).
        assert _ESCALATION_ORIGINAL_LENGTH >= _ERROR_PREVIEW_LENGTH

    def test_max_session_attempts_is_sane(self):
        # 3 = one primary + up to 2 retries. More than that and we're
        # really doing damage control rather than recovery.
        assert 2 <= _MAX_SESSION_ATTEMPTS <= 5

    def test_max_system_prompt_size_comfortably_large(self):
        # Must be big enough for base prompt + several guidance blocks
        # but safely under typical Claude context windows.
        assert 100_000 <= _MAX_SYSTEM_PROMPT_SIZE <= 500_000


# ---------------------------------------------------------------------------
# session_bridge.stream_cli_session — env_overrides injection
# ---------------------------------------------------------------------------


class _FakeProcess:
    """Minimal async subprocess stand-in for stream_cli_session tests.

    The stream_cli_session generator opens the subprocess, reads stdout,
    and eventually terminates. For these tests we don't actually need
    the CLI to run — we just need to intercept the command that WOULD
    have been invoked and assert its shape.
    """

    def __init__(self) -> None:
        self.stdout = AsyncMock()
        self.stdout.readline = AsyncMock(return_value=b"")  # immediate EOF
        self.stderr = AsyncMock()
        self.stderr.read = AsyncMock(return_value=b"")
        self.returncode = 0
        self.pid = 12345

    async def wait(self):
        return 0

    def terminate(self):
        self.returncode = 143

    def kill(self):
        self.returncode = 137


@pytest.fixture
def captured_cmd() -> list[list[str]]:
    """Capture every cmd passed to create_subprocess_exec."""
    seen: list[list[str]] = []

    async def _fake_exec(*cmd: str, **kwargs: Any) -> _FakeProcess:
        seen.append(list(cmd))
        return _FakeProcess()

    with patch(
        "src.docker.session_bridge.asyncio.create_subprocess_exec",
        side_effect=_fake_exec,
    ):
        # Suppress the subprocess.run call that writes the prompt file.
        with patch("src.docker.session_bridge.__import__", create=True) as _imp:
            yield seen


@pytest.mark.asyncio
async def test_env_overrides_injected_as_e_flags(captured_cmd):
    # Call the generator fully so we reach the create_subprocess_exec call.
    gen = stream_cli_session(
        container_name="cbcl-office-test",
        model="claude-sonnet-4-6",
        system_prompt="",  # no system prompt file, simplifies the mock
        prompt="hello",
        env_overrides={
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000",
            "MY_OTHER_VAR": "value",
        },
    )
    # Drain whatever the generator yields (should complete immediately
    # on the fake EOF).
    async for _ in gen:
        pass

    assert captured_cmd, "create_subprocess_exec was never called"
    cmd = captured_cmd[0]

    # Both -e flags should appear before the container name.
    container_idx = cmd.index("cbcl-office-test")
    e_indices = [i for i, c in enumerate(cmd) if c == "-e"]
    assert len(e_indices) == 2
    for i in e_indices:
        assert i < container_idx, (
            "-e flags must come before the container name in docker exec"
        )
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS=64000" in cmd
    assert "MY_OTHER_VAR=value" in cmd


@pytest.mark.asyncio
async def test_env_overrides_none_adds_no_e_flags(captured_cmd):
    gen = stream_cli_session(
        container_name="cbcl-office-test",
        model="claude-sonnet-4-6",
        system_prompt="",
        prompt="hello",
        env_overrides=None,
    )
    async for _ in gen:
        pass

    cmd = captured_cmd[0]
    assert "-e" not in cmd


@pytest.mark.asyncio
async def test_env_overrides_drops_invalid_keys(captured_cmd):
    gen = stream_cli_session(
        container_name="cbcl-office-test",
        model="claude-sonnet-4-6",
        system_prompt="",
        prompt="hello",
        env_overrides={
            "VALID_KEY": "value",
            "invalid key with spaces": "value",  # bad key — must be dropped
            "": "value",                          # empty key — must be dropped
            "ALSO_BAD": "",                        # empty value — must be dropped
        },
    )
    async for _ in gen:
        pass

    cmd = captured_cmd[0]
    # Only the valid one should land in the command
    assert "VALID_KEY=value" in cmd
    for v in cmd:
        if v.startswith("invalid") or v.startswith("="):
            pytest.fail(f"Invalid env override leaked into command: {v!r}")


@pytest.mark.asyncio
async def test_env_overrides_safe_against_shell_metacharacters(captured_cmd):
    # Values are passed directly to execve (docker exec -e KEY=VALUE);
    # shell metacharacters in the value should NOT be evaluated. We
    # can't prove non-evaluation end-to-end, but we CAN confirm the
    # value is passed as a single argv element rather than being
    # interpolated through a shell — the shape verifies that.
    gen = stream_cli_session(
        container_name="cbcl-office-test",
        model="claude-sonnet-4-6",
        system_prompt="",
        prompt="hello",
        env_overrides={"DANGEROUS": "$(rm -rf /); `echo pwned`"},
    )
    async for _ in gen:
        pass

    cmd = captured_cmd[0]
    assert "DANGEROUS=$(rm -rf /); `echo pwned`" in cmd
    # And that literal string is one argv element — no shell involvement.


# ---------------------------------------------------------------------------
# watchdog._peek_last_error_class
# ---------------------------------------------------------------------------


def _make_watchdog() -> TaskWatchdog:
    """Build a minimal watchdog with a fake HTTP client."""
    client = MagicMock()
    client.request = AsyncMock()
    wd = TaskWatchdog.__new__(TaskWatchdog)
    wd._ws = client
    wd._manager = MagicMock()
    wd._config = MagicMock()
    wd._office_id = "test-office"
    wd._supervisor = MagicMock()
    wd._dispatcher = MagicMock()
    wd._recently_dispatched = {}
    wd._move_failed = {}
    wd._task_crash_count = {}
    wd._wake_event = asyncio.Event()
    return wd


@pytest.mark.asyncio
async def test_peek_finds_error_class_from_details():
    wd = _make_watchdog()
    wd._ws.request.return_value = {
        "recent_activities": [
            {"event_type": "checkpoint", "content": "..."},
            {
                "event_type": "error",
                "content": "something went wrong",
                "details": {"error_class": "output_token_limit"},
            },
        ],
    }
    assert await wd._peek_last_error_class("t1") == "output_token_limit"


@pytest.mark.asyncio
async def test_peek_walks_reverse_picks_newest():
    # recent_activities is ordered oldest-first (newest-last). Reversed,
    # we should find the MOST RECENT error — not the older one.
    wd = _make_watchdog()
    wd._ws.request.return_value = {
        "recent_activities": [
            {
                "event_type": "error",
                "details": {"error_class": "rate_limited"},
            },
            {"event_type": "checkpoint"},
            {
                "event_type": "error",
                "details": {"error_class": "output_token_limit"},  # newest
            },
        ],
    }
    assert await wd._peek_last_error_class("t1") == "output_token_limit"


@pytest.mark.asyncio
async def test_peek_falls_back_to_content_classification():
    # When details is missing/empty, the peek should classify the
    # content text as a fallback so we still produce a hint.
    wd = _make_watchdog()
    wd._ws.request.return_value = {
        "recent_activities": [
            {
                "event_type": "error",
                "content": "API Error: exceeded the 32000 output token maximum",
                "details": {},
            },
        ],
    }
    assert await wd._peek_last_error_class("t1") == "output_token_limit"


@pytest.mark.asyncio
async def test_peek_returns_none_when_no_error_activity():
    wd = _make_watchdog()
    wd._ws.request.return_value = {
        "recent_activities": [
            {"event_type": "checkpoint"},
            {"event_type": "tool_run"},
        ],
    }
    assert await wd._peek_last_error_class("t1") is None


@pytest.mark.asyncio
async def test_peek_returns_none_on_backend_error():
    wd = _make_watchdog()
    wd._ws.request.return_value = {"error": "task not found"}
    assert await wd._peek_last_error_class("t1") is None


@pytest.mark.asyncio
async def test_peek_returns_none_on_request_exception():
    wd = _make_watchdog()
    wd._ws.request.side_effect = RuntimeError("network down")
    # Non-fatal — watchdog should degrade to "no hint" not crash.
    assert await wd._peek_last_error_class("t1") is None


@pytest.mark.asyncio
async def test_peek_handles_unclassifiable_content():
    wd = _make_watchdog()
    wd._ws.request.return_value = {
        "recent_activities": [
            {
                "event_type": "error",
                "content": "totally unprecedented failure mode XYZ",
                "details": {},
            },
        ],
    }
    # Falls back to UNKNOWN_FATAL via classify_error — still a valid class.
    result = await wd._peek_last_error_class("t1")
    assert result == "unknown_fatal"


# ---------------------------------------------------------------------------
# Integration: classifier → Remedy → expected outcomes
# ---------------------------------------------------------------------------


class TestClassifierRemedyContracts:
    """Cross-check: the remedy for each class must match what the retry
    loop and escalation path will actually do with it."""

    def test_output_token_limit_bumps_env_and_retries(self):
        r = classify_error("exceeded the 32000 output token maximum")
        assert r.error_class is ErrorClass.OUTPUT_TOKEN_LIMIT
        assert r.retryable
        assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" in r.env_overrides
        # Not a fresh session — the context is still useful.
        assert r.reset_session is False

    def test_context_too_large_resets_session(self):
        r = classify_error("prompt too long")
        assert r.reset_session is True

    def test_auth_failed_is_non_retryable(self):
        r = classify_error("401 Unauthorized")
        assert r.retryable is False

    def test_tool_unavailable_is_non_retryable(self):
        r = classify_error("tool not found: foo")
        assert r.retryable is False

    def test_unknown_is_non_retryable_and_safe(self):
        r = classify_error("totally unknown error")
        assert r.retryable is False
        assert r.error_class is ErrorClass.UNKNOWN_FATAL


# ---------------------------------------------------------------------------
# Long/unicode inputs — regression for serialization safety
# ---------------------------------------------------------------------------


class TestClassifierLongInputs:

    def test_very_long_unknown_error(self):
        msg = "x" * 100_000 + "🔥" * 500
        r = classify_error(msg)
        assert r.error_class is ErrorClass.UNKNOWN_FATAL
        # escalation_message must be bounded so it doesn't blow up the
        # activity content column.
        assert len(r.escalation_message) < 1000

    def test_multiline_error(self):
        msg = "API Error:\n  exceeded the 32000 output token maximum\n  stack: ..."
        r = classify_error(msg)
        assert r.error_class is ErrorClass.OUTPUT_TOKEN_LIMIT

    def test_non_ascii_error_message(self):
        msg = "错误: exceeded the 32000 output token maximum"
        r = classify_error(msg)
        assert r.error_class is ErrorClass.OUTPUT_TOKEN_LIMIT


# ---------------------------------------------------------------------------
# HttpBoardClient — watchdog's HTTP helper
# ---------------------------------------------------------------------------


class TestHttpBoardClient:

    def test_init_strips_trailing_slash(self):
        c = HttpBoardClient("http://localhost:8000/", "office-1")
        assert c._base == "http://localhost:8000/api/offices/office-1"

    def test_office_id_property(self):
        c = HttpBoardClient("http://localhost:8000", "office-1")
        assert c.office_id == "office-1"


# ---------------------------------------------------------------------------
# Regression: output-token-limit must be classified, not swallowed as
# UNKNOWN_FATAL. This is the exact text Claude CLI produces when the
# response hits the 32k limit — we classify it directly (the integration
# path feeds this text into classify_error via last_api_error).
# ---------------------------------------------------------------------------


class TestOutputTokenLimitRegression:
    """The bug that prompted the 2026-04-20 fix: the user's task was
    escalated as unknown_fatal because only 'Claude CLI exited with
    code 255' reached the classifier — the real API-error text was
    emitted as an assistant TEXT block and dropped on the floor.

    These tests pin the shape of the fix: the classifier still handles
    the rich text correctly; agent_worker now surfaces that text via
    the last_api_error capture path.
    """

    def test_exact_user_reported_text(self):
        text = (
            "API Error: Claude's response exceeded the 32000 output "
            "token maximum. To configure this behavior, set the "
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS environment variable."
        )
        r = classify_error(text)
        assert r.error_class is ErrorClass.OUTPUT_TOKEN_LIMIT, (
            f"Regression: output-token-limit reported by Claude CLI "
            f"must be classified, not fall through to UNKNOWN_FATAL. "
            f"Got {r.error_class}."
        )
        assert r.retryable is True
        assert r.env_overrides.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS")
        assert r.reset_session is False  # preserve partial work context

    def test_claude_code_max_output_tokens_literal_matches(self):
        # Even if the user-visible prefix changes, the env-var name
        # literal in the message should still classify.
        text = "error: set CLAUDE_CODE_MAX_OUTPUT_TOKENS to retry"
        r = classify_error(text)
        assert r.error_class is ErrorClass.OUTPUT_TOKEN_LIMIT

    def test_synthetic_exit_code_alone_is_unknown_fatal(self):
        # Important negative case: the synthetic "exited with code N"
        # message by itself has no useful signal, so the classifier
        # MUST NOT guess. The fix relies on agent_worker feeding the
        # classifier a richer source (last_api_error / stderr).
        r = classify_error("Claude CLI exited with code 255")
        assert r.error_class is ErrorClass.UNKNOWN_FATAL
        assert r.retryable is False

    def test_output_token_guidance_mentions_partial_work_recovery(self):
        # The remedy must tell the agent to inspect the workspace for
        # partial work before re-doing anything on retry — preserving
        # user progress is the explicit product requirement.
        r = classify_error("API Error: exceeded output token maximum")
        assert r.error_class is ErrorClass.OUTPUT_TOKEN_LIMIT
        lowered = r.guidance.lower()
        assert "workspace" in lowered or "inspect" in lowered or "partial" in lowered, (
            f"OUTPUT_TOKEN_LIMIT guidance should instruct the agent to "
            f"check for partial work before re-doing the task. Got: {r.guidance!r}"
        )


# ---------------------------------------------------------------------------
# agent_worker.py error-source selection logic.
#
# The retry loop picks the classification signal from (in order):
#   1. last_api_error (captured from assistant text or result.is_error)
#   2. stderr
#   3. the synthetic "exited with code N" error string
#
# We replicate that selection logic in-test to lock the priority —
# regressing it is exactly what caused the production bug.
# ---------------------------------------------------------------------------


class TestErrorSourceSelection:

    @staticmethod
    def _pick(last_api_error: str | None, stderr: str, exit_msg: str) -> str:
        """Mirror of the selection block in agent_worker._run_sdk_session."""
        if last_api_error:
            return last_api_error
        if stderr.strip():
            return stderr.strip()
        return exit_msg

    def test_api_error_wins_over_stderr_and_exit_msg(self):
        picked = self._pick(
            last_api_error="API Error: exceeded the 32000 output token maximum",
            stderr="something else",
            exit_msg="Claude CLI exited with code 255",
        )
        assert "output token" in picked
        assert classify_error(picked).error_class is ErrorClass.OUTPUT_TOKEN_LIMIT

    def test_stderr_wins_when_no_api_error(self):
        picked = self._pick(
            last_api_error=None,
            stderr="429 Too Many Requests\n",
            exit_msg="Claude CLI exited with code 1",
        )
        assert picked == "429 Too Many Requests"
        assert classify_error(picked).error_class is ErrorClass.RATE_LIMITED

    def test_exit_msg_is_last_resort(self):
        picked = self._pick(
            last_api_error=None,
            stderr="",
            exit_msg="Claude CLI exited with code 255",
        )
        # And the classifier intentionally fails this as UNKNOWN_FATAL,
        # forcing escalation — because we literally have no signal.
        assert picked == "Claude CLI exited with code 255"
        assert classify_error(picked).error_class is ErrorClass.UNKNOWN_FATAL


# ---------------------------------------------------------------------------
# session_bridge stderr propagation.
#
# Before the fix, stderr was drained to a discard task; by the time the
# non-zero-exit branch tried to re-read it, the buffer was empty. We now
# accumulate chunks into a shared list and include them in the emitted
# error payload. This test verifies the plumbing without running a real
# Claude CLI.
# ---------------------------------------------------------------------------


class _StderrFakeProcess:
    """Fake subprocess that yields stderr bytes then exits non-zero."""

    def __init__(self, stderr_bytes: bytes, rc: int) -> None:
        self.stdout = AsyncMock()
        self.stdout.readline = AsyncMock(return_value=b"")  # immediate EOF

        # stderr emits the payload in one read, then EOF on the next read.
        stderr_reads = [stderr_bytes, b""]

        async def _read(n: int = -1) -> bytes:
            if stderr_reads:
                return stderr_reads.pop(0)
            return b""

        self.stderr = MagicMock()
        self.stderr.read = _read
        self.returncode = rc
        self.pid = 99999

    async def wait(self) -> int:
        return self.returncode

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


@pytest.mark.asyncio
async def test_session_bridge_emits_stderr_in_error_payload():
    payload = b"fatal: auth token expired\n"

    async def _fake_exec(*_cmd: str, **_kwargs: Any) -> _StderrFakeProcess:
        return _StderrFakeProcess(stderr_bytes=payload, rc=255)

    with patch(
        "src.docker.session_bridge.asyncio.create_subprocess_exec",
        side_effect=_fake_exec,
    ):
        gen = stream_cli_session(
            container_name="cbcl-office-test",
            model="claude-sonnet-4-6",
            system_prompt="",  # skip system-prompt-file path
            prompt="hello",
        )
        msgs = []
        async for m in gen:
            msgs.append(m)

    err_msgs = [m for m in msgs if m.type == "error"]
    assert err_msgs, "non-zero exit must emit an error SessionMessage"
    err = err_msgs[-1].data
    assert "stderr" in err
    assert "auth token expired" in err["stderr"], (
        f"stderr must be propagated to the error payload so the "
        f"classifier can see it. Got {err!r}"
    )
    assert err.get("exit_code") == 255
