"""Regression tests for the WNOHANG watcher-miss fallback in
``script_execution._resolve_exit_code_via_waitpid`` AND for the
import-at-call-time SyntaxError that hid in ``script_execution.py``
from April 2026 (commit 22a8efb) until May 2026 — see
``TestScriptExecutionParsesCleanly``.

User report 2026-05-29: manual script runs sat at ``status=running``
indefinitely even though the in-container python process exited
cleanly and ``docker exec`` was gone from ``ps``. The asyncio
child watcher had dropped the SIGCHLD callback so
``Process.returncode`` never flipped from ``None`` and the
polling monitor never called ``on_complete``.

These tests cover:
* WNOHANG probe returns ``None`` for a running child (no false-fires)
* WNOHANG probe returns the exit code for an exited child
* ChildProcessError → ``-1`` sentinel so the monitor falls back
  to the log-content heuristic
* Log-content heuristic infers 0 for non-empty logs, 1 for empty
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from src.scripts.script_execution import (
    _infer_exit_code_from_log,
    _resolve_exit_code_via_waitpid,
)


SKIP_NON_UNIX = pytest.mark.skipif(
    sys.platform == "win32",
    reason="waitpid + WNOHANG are POSIX-only",
)


@SKIP_NON_UNIX
class TestResolveExitCodeViaWaitpid:

    def test_returns_none_for_running_child(self) -> None:
        """A child that's still running must return ``None`` so the
        monitor keeps polling. False-fires would prematurely mark
        live scripts as completed."""
        # sleep 60 — definitely still running 100ms after spawn.
        pid = os.spawnvp(os.P_NOWAIT, "sleep", ["sleep", "60"])
        try:
            time.sleep(0.1)
            result = _resolve_exit_code_via_waitpid(pid)
            assert result is None
        finally:
            # Cleanup: kill + reap so the test doesn't leak a zombie.
            try:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
            except OSError:
                pass

    def test_returns_exit_code_for_exited_child(self) -> None:
        """Once the child has exited but BEFORE anything reaps it,
        the WNOHANG probe must return the real exit code."""
        # ``true`` exits 0; ``false`` exits 1. Pick true for clarity.
        pid = os.spawnvp(os.P_NOWAIT, "true", ["true"])
        # Let the child actually exit. The kernel marks it zombie
        # until we waitpid().
        time.sleep(0.2)
        result = _resolve_exit_code_via_waitpid(pid)
        assert result == 0, (
            f"Expected 0 from waitpid on a successfully-exited child, "
            f"got {result}"
        )

    def test_returns_nonzero_for_failed_child(self) -> None:
        pid = os.spawnvp(os.P_NOWAIT, "false", ["false"])
        time.sleep(0.2)
        result = _resolve_exit_code_via_waitpid(pid)
        assert result == 1, (
            f"Expected 1 from waitpid on a failed child, got {result}"
        )

    def test_returns_negative_one_when_already_reaped(self) -> None:
        """If some other code path reaped the child first,
        ``waitpid`` raises ``ChildProcessError``. The helper must
        return ``-1`` so the monitor falls back to the log-content
        heuristic."""
        pid = os.spawnvp(os.P_NOWAIT, "true", ["true"])
        # Reap directly so the next waitpid sees ECHILD.
        os.waitpid(pid, 0)
        result = _resolve_exit_code_via_waitpid(pid)
        assert result == -1

    def test_returns_none_for_invalid_pid(self) -> None:
        """Defensive: a 0 or negative pid (e.g. a test fixture with
        a mock subprocess) must not crash the monitor loop."""
        assert _resolve_exit_code_via_waitpid(0) is None
        assert _resolve_exit_code_via_waitpid(-1) is None


class TestScriptExecutionParsesCleanly:
    """Module-level smoke test. The pre-existing dangling-``try``
    SyntaxError in this file was invisible because every caller
    used INLINE ``from src.scripts.script_execution import …``
    inside function bodies. ``ast.parse`` would have failed CI on
    day one of 22a8efb. This test plus the project-wide ruff /
    flake8 sweep prevents the same class of bug from shipping
    silently again.
    """

    def test_module_parses(self) -> None:
        import ast
        import importlib.util
        from pathlib import Path

        path = (
            Path(__file__).parent.parent
            / "src" / "scripts" / "script_execution.py"
        )
        ast.parse(path.read_text())
        # Belt + suspenders: real import (forces resolution of every
        # transitive top-level import too).
        spec = importlib.util.spec_from_file_location(
            "script_execution_smoke", path,
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert hasattr(module, "monitor_all")
        assert hasattr(module, "on_complete")


class TestInferExitCodeFromLog:

    def test_empty_log_infers_failure(self, tmp_path: Path) -> None:
        log = tmp_path / "log.txt"
        log.write_text("")
        assert _infer_exit_code_from_log(log) == 1

    def test_nonempty_log_infers_success(self, tmp_path: Path) -> None:
        log = tmp_path / "log.txt"
        log.write_text("notify_manager returned: ok\n")
        assert _infer_exit_code_from_log(log) == 0

    def test_missing_log_infers_failure(self, tmp_path: Path) -> None:
        # File doesn't exist — heuristic returns 1.
        assert _infer_exit_code_from_log(tmp_path / "missing.txt") == 1
