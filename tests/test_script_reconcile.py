"""ADD-C1: startup reconciliation of executions a previous daemon left
``running`` must check the REAL in-container process state instead of
blindly rewriting running→failed.

The office container is reused across daemon restarts, so an orphaned
script keeps running. The old code reported it failed (a lie) → the
Manager reworked runs that actually succeeded. ``reconcile_orphaned_
executions`` now: alive → kill + honest message; dead/no-pid → honest
failed.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.scripts import script_execution as se


def _make_running_exec(workspace: Path, name: str, exec_id: str, *, pid: str | None):
    exec_dir = workspace / ".scripts" / name / "executions" / exec_id
    exec_dir.mkdir(parents=True)
    (exec_dir / "status.json").write_text(json.dumps({
        "status": "running", "started_at": "2026-06-02T10:00:00+00:00",
        "completed_at": None, "duration_seconds": None, "exit_code": None,
        "task_id": None, "triggered_by": "agent", "error_message": None,
    }))
    if pid is not None:
        (exec_dir / "in_container.pid").write_text(pid)
    return exec_dir


def _status(exec_dir: Path) -> dict:
    return json.loads((exec_dir / "status.json").read_text())


@pytest.mark.asyncio
async def test_no_scripts_dir_returns_zero(tmp_path) -> None:
    assert await se.reconcile_orphaned_executions(str(tmp_path), "c") == 0


@pytest.mark.asyncio
async def test_alive_orphan_is_killed_and_marked_failed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(se, "_IN_CONTAINER_KILL_GRACE_SECONDS", 0)
    exec_dir = _make_running_exec(tmp_path, "s1", "exec-1", pid="4242")

    kill_calls: list[list[str]] = []

    async def _fake_alive(container, pid):
        return True

    async def _fake_kill(container, pid, sig):
        kill_calls.append([container, str(pid), sig])

    monkeypatch.setattr(se, "_docker_pid_alive", _fake_alive)
    monkeypatch.setattr(se, "_docker_exec_kill", _fake_kill)

    n = await se.reconcile_orphaned_executions(str(tmp_path), "cbcl-office-foo")
    assert n == 1
    data = _status(exec_dir)
    assert data["status"] == "failed"
    assert "orphaned" in data["error_message"]
    assert data["completed_at"] is not None
    # TERM then KILL on the recorded pid.
    assert [c[2] for c in kill_calls] == ["TERM", "KILL"]
    assert all(c[1] == "4242" for c in kill_calls)


@pytest.mark.asyncio
async def test_dead_orphan_marked_failed_without_kill(tmp_path) -> None:
    exec_dir = _make_running_exec(tmp_path, "s2", "exec-2", pid="999")

    killed = False

    async def _fake_alive(container, pid):
        return False  # process already gone

    async def _fake_kill(container, pid, sig):
        nonlocal killed
        killed = True

    with patch.object(se, "_docker_pid_alive", _fake_alive), \
         patch.object(se, "_docker_exec_kill", _fake_kill):
        n = await se.reconcile_orphaned_executions(str(tmp_path), "cbcl-office-foo")

    assert n == 1
    assert killed is False
    data = _status(exec_dir)
    assert data["status"] == "failed"
    assert "restarted" in data["error_message"]


@pytest.mark.asyncio
async def test_no_pidfile_marked_failed(tmp_path) -> None:
    exec_dir = _make_running_exec(tmp_path, "s3", "exec-3", pid=None)
    n = await se.reconcile_orphaned_executions(str(tmp_path), "cbcl-office-foo")
    assert n == 1
    assert _status(exec_dir)["status"] == "failed"


@pytest.mark.asyncio
async def test_no_container_marked_failed(tmp_path) -> None:
    """Host-fallback (container_name=None): never probes docker, just
    marks failed."""
    exec_dir = _make_running_exec(tmp_path, "s4", "exec-4", pid="5")
    n = await se.reconcile_orphaned_executions(str(tmp_path), None)
    assert n == 1
    assert _status(exec_dir)["status"] == "failed"


@pytest.mark.asyncio
async def test_terminal_rows_untouched(tmp_path) -> None:
    """Already-completed/failed rows must NOT be rewritten."""
    exec_dir = tmp_path / ".scripts" / "s5" / "executions" / "exec-5"
    exec_dir.mkdir(parents=True)
    (exec_dir / "status.json").write_text(json.dumps({
        "status": "completed", "exit_code": 0, "completed_at": "x",
    }))
    n = await se.reconcile_orphaned_executions(str(tmp_path), "cbcl-office-foo")
    assert n == 0
    assert _status(exec_dir)["status"] == "completed"


@pytest.mark.asyncio
async def test_unverifiable_alive_check_marks_failed(tmp_path, monkeypatch) -> None:
    """If docker is unreachable, _docker_pid_alive returns False
    (fail-closed) → the run is honestly marked failed, not left
    running forever."""
    exec_dir = _make_running_exec(tmp_path, "s6", "exec-6", pid="77")

    async def _boom_spawn(*a, **k):
        raise OSError("docker not found")

    # Exercise the real _docker_pid_alive with a failing spawn.
    with patch(
        "src.scripts.script_execution.asyncio.create_subprocess_exec",
        side_effect=_boom_spawn,
    ):
        n = await se.reconcile_orphaned_executions(str(tmp_path), "cbcl-office-foo")

    assert n == 1
    assert _status(exec_dir)["status"] == "failed"
