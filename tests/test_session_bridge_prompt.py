"""Prompt-delivery contract for ``stream_cli_session``.

History:

* 2026-06-04 — a user pasted a chat message whose first line was a
  markdown bullet (``- SMTP_CREDENTIALS …``). The prompt was passed to
  ``claude --print`` as a POSITIONAL argv element and the CLI's
  commander parser rejected it (``error: unknown option '- …'``). The
  first fix terminated option parsing with ``--``.
* T2.2.2 (03/#25) — the positional form ALSO exposed the full task
  brief / activity history / user-pasted text in host ``ps`` /
  ``/proc/*/cmdline`` for the whole session and re-opened the ARG_MAX
  ceiling for long prompts. The prompt now rides STDIN: ``claude
  --print`` reads it from stdin when no positional is given. That makes
  the dash-leading bug structurally impossible (stdin never reaches the
  option parser) and keeps the argv secret-free.

This suite pins the stdin contract: the prompt is NEVER an argv
element, the subprocess is spawned with a stdin pipe, the prompt bytes
are written to it, and stdin is closed (EOF) afterwards.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from src.docker import session_bridge


class _StdinRecorder:
    """Records writes/close like an ``asyncio.StreamWriter``."""

    def __init__(self) -> None:
        self.written = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _Reader:
    async def readline(self) -> bytes:
        return b""  # immediate EOF


class _ErrReader:
    async def read(self, _n: int) -> bytes:
        return b""


class _FakeProc:
    """Subprocess stand-in: immediate stdout EOF, recordable stdin."""

    def __init__(self) -> None:
        self.pid = 12345
        self.returncode = 0
        self.stdin = _StdinRecorder()
        self.stdout = _Reader()
        self.stderr = _ErrReader()

    async def wait(self) -> int:
        return 0

    def terminate(self) -> None:  # pragma: no cover — not exercised
        self.returncode = 143

    def kill(self) -> None:  # pragma: no cover — not exercised
        self.returncode = 137


async def _run_session(prompt: str, **kwargs) -> tuple[list[str], _FakeProc]:
    """Run ``stream_cli_session`` to completion against a fake process;
    return (argv, fake_proc)."""
    captured: list[list[str]] = []
    proc = _FakeProc()

    async def _fake_exec(*args: str, **_kw: Any) -> _FakeProc:
        captured.append(list(args))
        return proc

    with patch.object(asyncio, "create_subprocess_exec", _fake_exec), \
            patch("subprocess.run"):
        async for _ in session_bridge.stream_cli_session(
            container_name="cbcl-office-test",
            model="claude-opus-4-7",
            system_prompt="",  # skip the prompt-file tee path
            prompt=prompt,
            **kwargs,
        ):
            pass

    assert captured, "create_subprocess_exec was never reached"
    return captured[0], proc


@pytest.mark.asyncio
async def test_prompt_not_in_argv_delivered_via_stdin():
    cmd, proc = await _run_session("hello world")
    assert "hello world" not in cmd
    assert not any("hello world" in part for part in cmd)
    # The prompt arrived over stdin, then stdin was closed (EOF).
    assert proc.stdin.written == b"hello world"
    assert proc.stdin.closed is True


@pytest.mark.asyncio
async def test_dash_leading_prompt_safe_via_stdin():
    dash_prompt = "- SMTP_CREDENTIALS is in the Office secrets\n\nSkip Sentry"
    cmd, proc = await _run_session(dash_prompt)
    # Nothing prompt-shaped on the argv — the option parser never sees it.
    assert dash_prompt not in cmd
    assert proc.stdin.written.decode() == dash_prompt
    # The old, broken forms passed ``["-p", prompt]`` / ``["--", prompt]``.
    assert "-p" not in cmd
    assert cmd[-1] != dash_prompt


@pytest.mark.asyncio
async def test_long_prompt_does_not_ride_argv():
    """ARG_MAX regression: a prompt far past typical argv comfort rides
    stdin whole, with zero argv growth."""
    long_prompt = "x" * 600_000  # > typical ARG_MAX budget for one element
    cmd, proc = await _run_session(long_prompt)
    assert all(len(part) < 10_000 for part in cmd)
    assert len(proc.stdin.written) == 600_000


@pytest.mark.asyncio
async def test_print_mode_still_enabled_and_stdin_attached():
    cmd, _ = await _run_session("hi")
    assert "--print" in cmd
    # ``docker exec -i`` keeps the container-side stdin open for the feed.
    assert cmd[:3] == ["docker", "exec", "-i"]


class _OneLineThenBlockReader:
    """Yields a single NDJSON line, then would block forever (returns nothing
    more). Lets us abandon the generator after the first yielded message."""

    def __init__(self) -> None:
        self._sent = False

    async def readline(self) -> bytes:
        if not self._sent:
            self._sent = True
            return b'{"type":"system","subtype":"init","session_id":"s1"}\n'
        await asyncio.sleep(3600)  # block — simulates a still-running CLI
        return b""


class _LiveProc:
    """A proc that stays running (returncode None) until terminate()/kill()."""

    def __init__(self) -> None:
        self.pid = 999
        self.returncode = None
        self.stdin = _StdinRecorder()
        self.stdout = _OneLineThenBlockReader()
        self.stderr = _ErrReader()
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        # Resolves once terminated/killed; otherwise blocks.
        while self.returncode is None:
            await asyncio.sleep(0.01)
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 143

    def kill(self) -> None:
        self.killed = True
        self.returncode = 137


@pytest.mark.asyncio
async def test_abandoning_generator_terminates_live_proc():
    """T8.1.1 — if the consumer abandons the stream mid-flight (GeneratorExit
    at the yield), the finally must terminate the still-running CLI."""
    proc = _LiveProc()

    async def _fake_exec(*args: str, **_kw: Any) -> _LiveProc:
        return proc

    with patch.object(asyncio, "create_subprocess_exec", _fake_exec), \
            patch("subprocess.run"):
        agen = session_bridge.stream_cli_session(
            container_name="cbcl-office-test",
            model="claude-opus-4-7",
            system_prompt="",
            prompt="do work",
        )
        async for _msg in agen:
            break  # abandon after the first message → GeneratorExit
        await agen.aclose()  # ensure finally runs deterministically

    assert proc.returncode is not None, "proc was left running (leak)"
    assert proc.terminated or proc.killed


@pytest.mark.asyncio
async def test_raise_out_of_loop_reaps_proc_via_finalizer():
    """T8.1.1 (production path) — the real consumer abandons the generator by
    RAISING out of the `async for` across frames (no explicit aclose). The
    async-gen finalizer then runs the finally and terminates the proc. Mirrors
    _agent_worker_task's AgentErrorEscalation path."""
    import gc

    proc = _LiveProc()

    async def _fake_exec(*args: str, **_kw: Any) -> _LiveProc:
        return proc

    class _Boom(Exception):
        pass

    async def _consume() -> None:
        async for _msg in session_bridge.stream_cli_session(
            container_name="cbcl-office-test",
            model="claude-opus-4-7",
            system_prompt="",
            prompt="do work",
        ):
            raise _Boom()  # abandon via raise — no aclose(), like production

    with patch.object(asyncio, "create_subprocess_exec", _fake_exec), \
            patch("subprocess.run"):
        with pytest.raises(_Boom):
            await _consume()
        # The generator is now unreferenced; let the finalizer hook run.
        gc.collect()
        for _ in range(5):
            await asyncio.sleep(0)

    assert proc.returncode is not None, "proc leaked after raise-abandon"
    assert proc.terminated or proc.killed


class _OversizedThenValidReader:
    """readline() raises ValueError (oversized line, buffer cleared) once,
    then returns a valid NDJSON line, then EOF — the real CPython behavior."""

    def __init__(self) -> None:
        self._calls = 0

    async def readline(self) -> bytes:
        self._calls += 1
        if self._calls == 1:
            raise ValueError("Separator is not found, and chunk exceed the limit")
        if self._calls == 2:
            return b'{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n'
        return b""  # EOF


@pytest.mark.asyncio
async def test_oversized_line_skipped_not_session_abort():
    """T8.1.2 — an oversized line raises ValueError from readline(); the
    session must SKIP it and keep streaming, not emit a 'Session error'."""
    proc = _LiveProc()
    proc.stdout = _OversizedThenValidReader()
    proc.returncode = 0  # so EOF path completes cleanly

    async def _fake_exec(*args: str, **_kw: Any) -> _LiveProc:
        return proc

    msgs = []
    with patch.object(asyncio, "create_subprocess_exec", _fake_exec), \
            patch("subprocess.run"):
        async for m in session_bridge.stream_cli_session(
            container_name="cbcl-office-test", model="claude-opus-4-7",
            system_prompt="", prompt="x",
        ):
            msgs.append(m)

    # No "Session error" message; the valid line after the oversized one was processed.
    assert not any(m.type == "error" for m in msgs), [m.data for m in msgs]
