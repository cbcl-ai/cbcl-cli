"""T3.2.3 (07/G5, 03/#4) — worker output-liveness + per-attempt wall cap.

``stream_cli_session`` previously had NO detector between "CLI alive"
and the (dead) per-attempt cap: a CLI process that stayed alive but
emitted nothing ran unbounded. This suite pins:

(a) a silent-but-alive CLI is terminated once the inactivity window
    (CUBICLE_WORKER_INACTIVITY_SECONDS, default 1200s) elapses, and the
    emitted error classifies as TIMEOUT (retryable) so the existing
    worker retry ladder — resume, 3 attempts, blocked escalation —
    takes over;
(b) an actively-emitting stream past the threshold is NOT terminated;
(c) the per-attempt wall cap (_SESSION_TIMEOUT_SECONDS, previously a
    dead constant) fires on an endlessly-chatty session.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.docker import session_bridge
from src.orchestrator.error_classifier import ErrorClass, classify_error


class FakeProc:
    """Minimal asyncio-subprocess stand-in with controllable stdout."""

    def __init__(self, readline_fn):
        self.pid = 4242
        self.returncode: int | None = None
        self._exit_evt = asyncio.Event()
        self.stdout = SimpleNamespace(readline=readline_fn)
        self.stderr = SimpleNamespace(read=self._stderr_read)
        self.terminated = False
        self.killed = False

    async def _stderr_read(self, _n: int) -> bytes:
        return b""  # immediate EOF — stderr reader exits cleanly

    async def wait(self) -> int | None:
        await self._exit_evt.wait()
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15
        self._exit_evt.set()

    def kill(self) -> None:
        self.killed = True
        if self.returncode is None:
            self.returncode = -9
        self._exit_evt.set()

    def exit_clean(self) -> None:
        self.returncode = 0
        self._exit_evt.set()


async def _collect(proc: FakeProc, **kwargs) -> list[session_bridge.SessionMessage]:
    """Run stream_cli_session against the fake proc; return messages."""

    async def _fake_exec(*_args, **_kw):
        return proc

    messages: list[session_bridge.SessionMessage] = []
    with patch.object(asyncio, "create_subprocess_exec", _fake_exec):
        async for msg in session_bridge.stream_cli_session(
            container_name="cbcl-office-test",
            model="claude-opus-4-7",
            system_prompt="",  # skip the tee prompt-file path
            prompt="do the task",
            **kwargs,
        ):
            messages.append(msg)
    return messages


@pytest.fixture
def fast_read_timeout(monkeypatch):
    """Shrink the readline poll so liveness checks run sub-second."""
    monkeypatch.setattr(session_bridge, "_READ_TIMEOUT_SECONDS", 0.05)


class TestInactivityTimeout:

    async def test_silent_alive_cli_terminated_and_classified_timeout(
        self, monkeypatch, fast_read_timeout,
    ):
        monkeypatch.setenv("CUBICLE_WORKER_INACTIVITY_SECONDS", "0.2")

        async def _silent_readline() -> bytes:
            await asyncio.Event().wait()  # never returns
            return b""  # pragma: no cover

        proc = FakeProc(_silent_readline)
        messages = await _collect(proc)

        # The attempt was terminated…
        assert proc.terminated is True
        # …and exactly one TIMEOUT-classifiable error was emitted.
        errors = [m for m in messages if m.type == "error"]
        assert len(errors) == 1
        error_text = errors[0].data["error"]
        assert "no output" in error_text
        remedy = classify_error(error_text)
        assert remedy.error_class is ErrorClass.TIMEOUT
        # Retry ladder engagement: the worker loop retries retryable
        # remedies (resume, 3 attempts) before the blocked escalation.
        assert remedy.retryable is True

    async def test_active_stream_past_threshold_not_terminated(
        self, monkeypatch,
    ):
        # Threshold 0.3s; lines arrive every ~0.02s for ~0.5s+ total —
        # well past the threshold in wall time, but never silent. The
        # read-timeout poll stays comfortably ABOVE the line cadence
        # so the read never races the emit (a readline cancelled by
        # wait_for would drop the in-flight line and fake silence).
        monkeypatch.setattr(session_bridge, "_READ_TIMEOUT_SECONDS", 1.0)
        monkeypatch.setenv("CUBICLE_WORKER_INACTIVITY_SECONDS", "0.3")

        emitted = {"count": 0}
        proc_holder: dict[str, FakeProc] = {}

        async def _chatty_readline() -> bytes:
            if emitted["count"] >= 25:
                proc_holder["proc"].exit_clean()
                return b""  # EOF
            emitted["count"] += 1
            await asyncio.sleep(0.02)
            return (
                json.dumps({"type": "assistant", "n": emitted["count"]})
                + "\n"
            ).encode()

        proc = FakeProc(_chatty_readline)
        proc_holder["proc"] = proc
        messages = await _collect(proc)

        assert proc.terminated is False
        assert proc.killed is False
        assert [m for m in messages if m.type == "error"] == []
        assert len([m for m in messages if m.type == "assistant"]) == 25

    async def test_env_override_invalid_falls_back_to_default(
        self, monkeypatch,
    ):
        monkeypatch.setenv("CUBICLE_WORKER_INACTIVITY_SECONDS", "not-a-number")
        assert session_bridge._inactivity_timeout_seconds() == (
            session_bridge._DEFAULT_INACTIVITY_SECONDS
        )
        monkeypatch.setenv("CUBICLE_WORKER_INACTIVITY_SECONDS", "-5")
        assert session_bridge._inactivity_timeout_seconds() == (
            session_bridge._DEFAULT_INACTIVITY_SECONDS
        )
        monkeypatch.setenv("CUBICLE_WORKER_INACTIVITY_SECONDS", "900")
        assert session_bridge._inactivity_timeout_seconds() == 900.0
        monkeypatch.delenv("CUBICLE_WORKER_INACTIVITY_SECONDS")
        assert session_bridge._inactivity_timeout_seconds() == 1200.0


class TestWallClockCap:

    async def test_endlessly_chatty_session_hits_wall_cap(
        self, monkeypatch, fast_read_timeout,
    ):
        # An endlessly-EMITTING CLI never trips the read timeout, so
        # the inactivity timer can't see it — the wall cap must.
        monkeypatch.setenv("CUBICLE_WORKER_INACTIVITY_SECONDS", "60")
        monkeypatch.setattr(session_bridge, "_SESSION_TIMEOUT_SECONDS", 0.3)

        async def _endless_readline() -> bytes:
            await asyncio.sleep(0.02)
            return (json.dumps({"type": "assistant"}) + "\n").encode()

        proc = FakeProc(_endless_readline)
        messages = await _collect(proc)

        assert proc.terminated is True
        errors = [m for m in messages if m.type == "error"]
        assert len(errors) == 1
        error_text = errors[0].data["error"]
        assert "wall-clock cap" in error_text
        remedy = classify_error(error_text)
        assert remedy.error_class is ErrorClass.TIMEOUT
        assert remedy.retryable is True
        # Output kept streaming until the cap.
        assert [m for m in messages if m.type == "assistant"]
