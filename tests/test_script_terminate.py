"""NEW-2: ``terminate_execution`` must kill the REAL in-container
process, not just the host-side ``docker exec`` client.

Docker (no TTY) does not forward signals, so terminating the client
orphans the in-container python. ``terminate_execution`` reads the PID
the launch wrapper recorded and ``docker exec … kill``s it (TERM →
grace → KILL), then terminates the client.

These tests patch ``create_subprocess_exec`` to capture the
``docker exec … kill`` argv and assert the PID + signals, and verify
the fallback (no pidfile / host-fallback run) still terminates the
client.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from src.scripts import script_execution as se


class _FakeProc:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15


class _FakeExecution:
    def __init__(self, exec_dir: Path, container_name, process) -> None:
        self.exec_dir = exec_dir
        self.container_name = container_name
        self.process = process


def _make_exec(tmp_path: Path, *, container, pid: str | None):
    exec_dir = tmp_path / "executions" / "exec-x"
    exec_dir.mkdir(parents=True)
    if pid is not None:
        (exec_dir / "in_container.pid").write_text(pid)
    return _FakeExecution(exec_dir, container, _FakeProc())


@pytest.mark.asyncio
async def test_terminate_kills_in_container_pid(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(se, "_IN_CONTAINER_KILL_GRACE_SECONDS", 0)
    execution = _make_exec(tmp_path, container="cbcl-office-foo", pid="4242")

    kill_calls: list[list[str]] = []

    async def _fake_spawn(*args, **kwargs):
        kill_calls.append(list(args))

        class _Stub:
            returncode = 0

            async def wait(self):
                return 0
        return _Stub()

    with patch(
        "src.scripts.script_execution.asyncio.create_subprocess_exec",
        side_effect=_fake_spawn,
    ):
        await se.terminate_execution(execution)

    # Two docker-exec kills: TERM then KILL (process never exited).
    assert len(kill_calls) == 2
    for argv in kill_calls:
        assert argv[:3] == ["docker", "exec", "cbcl-office-foo"]
        assert "4242" in argv  # the recorded in-container PID
    joined_term = " ".join(kill_calls[0])
    joined_kill = " ".join(kill_calls[1])
    assert "kill -TERM" in joined_term
    assert "kill -KILL" in joined_kill
    # The host-side client is ALSO terminated so proc.wait() unblocks.
    assert execution.process.terminated is True


@pytest.mark.asyncio
async def test_terminate_skips_kill_when_client_already_exited(
    tmp_path, monkeypatch
) -> None:
    """If the host client sees the child exit during the grace window,
    no SIGKILL is sent (only the TERM)."""
    monkeypatch.setattr(se, "_IN_CONTAINER_KILL_GRACE_SECONDS", 0)
    execution = _make_exec(tmp_path, container="cbcl-office-foo", pid="999")

    kill_calls: list[list[str]] = []

    async def _fake_spawn(*args, **kwargs):
        kill_calls.append(list(args))
        # Simulate the in-container process dying after the TERM.
        execution.process.returncode = 0

        class _Stub:
            returncode = 0

            async def wait(self):
                return 0
        return _Stub()

    with patch(
        "src.scripts.script_execution.asyncio.create_subprocess_exec",
        side_effect=_fake_spawn,
    ):
        await se.terminate_execution(execution)

    assert len(kill_calls) == 1  # only TERM; KILL skipped
    assert "kill -TERM" in " ".join(kill_calls[0])


@pytest.mark.asyncio
async def test_terminate_fallback_no_pidfile(tmp_path) -> None:
    """No pidfile (pre-start race / host-fallback run) → just terminate
    the client; never attempt a docker-exec kill."""
    execution = _make_exec(tmp_path, container="cbcl-office-foo", pid=None)

    called = False

    async def _fake_spawn(*args, **kwargs):
        nonlocal called
        called = True

        class _Stub:
            returncode = 0

            async def wait(self):
                return 0
        return _Stub()

    with patch(
        "src.scripts.script_execution.asyncio.create_subprocess_exec",
        side_effect=_fake_spawn,
    ):
        await se.terminate_execution(execution)

    assert called is False  # no docker-exec kill
    assert execution.process.terminated is True


@pytest.mark.asyncio
async def test_terminate_host_fallback_no_container(tmp_path) -> None:
    """A host-fallback run (container_name=None) just terminates the
    client even if a stray pidfile exists."""
    execution = _make_exec(tmp_path, container=None, pid="123")

    called = False

    async def _fake_spawn(*args, **kwargs):
        nonlocal called
        called = True

        class _Stub:
            returncode = 0

            async def wait(self):
                return 0
        return _Stub()

    with patch(
        "src.scripts.script_execution.asyncio.create_subprocess_exec",
        side_effect=_fake_spawn,
    ):
        await se.terminate_execution(execution)

    assert called is False
    assert execution.process.terminated is True


def test_read_in_container_pid_rejects_garbage(tmp_path) -> None:
    exec_dir = tmp_path / "e"
    exec_dir.mkdir()
    # Missing file
    assert se._read_in_container_pid(exec_dir) is None
    # Empty
    (exec_dir / "in_container.pid").write_text("   ")
    assert se._read_in_container_pid(exec_dir) is None
    # Non-numeric
    (exec_dir / "in_container.pid").write_text("not-a-pid")
    assert se._read_in_container_pid(exec_dir) is None
    # PID 1 (container init) rejected — never kill init
    (exec_dir / "in_container.pid").write_text("1")
    assert se._read_in_container_pid(exec_dir) is None
    # Valid
    (exec_dir / "in_container.pid").write_text("4242\n")
    assert se._read_in_container_pid(exec_dir) == 4242
