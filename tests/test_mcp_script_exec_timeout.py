"""ADD-C4 / NEW-2 (in-container analogue): the agent ``execute_script``
path MUST bound a run's wall-clock and kill the WHOLE process tree on
timeout (and on session cancel), not just the direct subprocess.

The host path enforces a 4h max duration (``script_execution.py:179``);
the in-container monitor used a bare ``await proc.wait()`` — an
agent-triggered infinite loop ran forever. And the kill killed only the
direct child, orphaning anything the script forked inside the container.

These tests spawn REAL subprocesses (in their own session, exactly as
``_execute_script`` now does) and verify ``_kill_proc_tree`` reaps the
group, plus that the monitor's timeout path stamps a ``failed`` status.

Module import uses the ``_mcp_backend`` stub + sys.path shim (see
``test_mcp_script_exec_bindings.py``).
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import pathlib
import signal
import sys
import types

import pytest


_AGENT_IMAGE_DIR = (
    pathlib.Path(__file__).resolve().parent.parent / "src" / "_agent_image"
)


@pytest.fixture(scope="module")
def mod():
    # Complete, order-independent stub: the module imports both
    # _get_session and _call_backend (C3 gate), so both must exist.
    stub = sys.modules.get("_mcp_backend")
    if stub is None:
        stub = types.ModuleType("_mcp_backend")
        sys.modules["_mcp_backend"] = stub
    if not hasattr(stub, "_get_session"):
        stub._get_session = lambda *a, **k: None
    if not hasattr(stub, "_call_backend"):
        async def _call_backend(action, params):
            return {}
        stub._call_backend = _call_backend
    added = False
    if str(_AGENT_IMAGE_DIR) not in sys.path:
        sys.path.insert(0, str(_AGENT_IMAGE_DIR))
        added = True
    try:
        module = importlib.import_module("_mcp_script_exec")
    finally:
        if added:
            sys.path.remove(str(_AGENT_IMAGE_DIR))
    return module


# ── _max_script_duration_seconds ───────────────────────────────────

def test_max_duration_default(mod, monkeypatch) -> None:
    monkeypatch.delenv("CUBICLE_SCRIPT_MAX_DURATION_SECONDS", raising=False)
    assert mod._max_script_duration_seconds() == 4 * 60 * 60


def test_max_duration_env_override(mod, monkeypatch) -> None:
    monkeypatch.setenv("CUBICLE_SCRIPT_MAX_DURATION_SECONDS", "120")
    assert mod._max_script_duration_seconds() == 120


def test_max_duration_typo_falls_back_to_default(mod, monkeypatch) -> None:
    monkeypatch.setenv("CUBICLE_SCRIPT_MAX_DURATION_SECONDS", "not-a-number")
    assert mod._max_script_duration_seconds() == 4 * 60 * 60


def test_max_duration_too_small_falls_back(mod, monkeypatch) -> None:
    # A typo'd "5" must not make every script die in 5s — floor at 60.
    monkeypatch.setenv("CUBICLE_SCRIPT_MAX_DURATION_SECONDS", "5")
    assert mod._max_script_duration_seconds() == 4 * 60 * 60


# ── _kill_proc_tree ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_kill_proc_tree_kills_direct_process(mod) -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)",
        start_new_session=True,
    )
    assert proc.returncode is None
    await mod._kill_proc_tree(proc, "test-script")
    assert proc.returncode is not None  # reaped


@pytest.mark.asyncio
async def test_kill_proc_tree_reaps_forked_child(mod, tmp_path) -> None:
    """The whole group dies — a child the script forked must not be
    orphaned (the in-container leak class)."""
    pid_file = tmp_path / "child.pid"
    # Parent forks a long-lived child, records its PID, then sleeps.
    code = (
        "import subprocess, sys, time\n"
        f"c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(c.pid))\n"
        "time.sleep(60)\n"
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, start_new_session=True,
    )
    # Wait for the child PID to be written.
    for _ in range(50):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.1)
    child_pid = int(pid_file.read_text().strip())

    await mod._kill_proc_tree(proc, "test-script")
    assert proc.returncode is not None

    # Give the group signal a beat to land on the child.
    await asyncio.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)  # raises iff the child is gone


@pytest.mark.asyncio
async def test_kill_proc_tree_noop_on_finished_proc(mod) -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "pass", start_new_session=True,
    )
    await proc.wait()
    # Already dead — must not raise.
    await mod._kill_proc_tree(proc, "test-script")


# ── _monitor_script timeout path ───────────────────────────────────

@pytest.mark.asyncio
async def test_monitor_times_out_and_marks_failed(mod, tmp_path, monkeypatch) -> None:
    """End-to-end: a script that outlives max_duration is killed and its
    status.json is stamped failed with a clear timeout message."""
    # Tiny max-duration so the test is fast.
    monkeypatch.setenv("CUBICLE_SCRIPT_MAX_DURATION_SECONDS", "60")
    monkeypatch.setattr(mod, "_max_script_duration_seconds", lambda: 1)
    # Stub the two network side effects the monitor performs.
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(mod, "_report_status_to_backend", _noop)
    monkeypatch.setattr(mod, "_trigger_outbox_scan", _noop)

    exec_dir = tmp_path / "executions" / "exec-test"
    exec_dir.mkdir(parents=True)
    started = mod.datetime.now(mod.timezone.utc).isoformat()
    (exec_dir / "status.json").write_text(json.dumps({
        "status": "running", "started_at": started, "completed_at": None,
        "duration_seconds": None, "exit_code": None,
        "task_id": None, "triggered_by": "agent", "error_message": None,
    }))

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)",
        start_new_session=True,
    )
    await mod._monitor_script(proc, exec_dir, None, "test-script", "exec-test")

    assert proc.returncode is not None  # killed by the timeout path
    data = json.loads((exec_dir / "status.json").read_text())
    assert data["status"] == "failed"
    assert "max duration" in (data.get("error_message") or "")
