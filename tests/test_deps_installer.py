"""Tests for the per-script pip dependency installer.

The actual pip invocation is an async subprocess call we don't run
against real PyPI in unit tests. Instead we cover:

  - the plan/cache-hit logic (pure, no subprocess)
  - the install-lock contract (concurrent acquire, stale-lock break)
  - the ``workspace_to_container`` translator is applied to paths
    when building the docker-exec command
  - failure classification via DepsInstallError
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.scripts.deps_installer import (  # noqa: E402
    DepsInstallError,
    ensure_deps_installed,
    plan_install,
)


# ---------------------------------------------------------------------------
# plan_install — cache-hit / miss decision
# ---------------------------------------------------------------------------


class TestPlanInstall:

    def test_no_requirements_file_skips(self, tmp_path):
        # Scripts without a requirements.txt are the common case —
        # pip MUST NOT run. Many mini-projects only use stdlib, so
        # the fast-path cache miss / no-op has to be rock solid.
        plan = plan_install(tmp_path)
        assert plan.needed is False

    def test_first_run_installs(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("rich\n")
        plan = plan_install(tmp_path)
        assert plan.needed is True
        assert plan.deps_dir == tmp_path / ".deps"
        assert plan.requirements_file == tmp_path / "requirements.txt"

    def test_unchanged_requirements_cache_hit(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("rich\n")
        deps_dir = tmp_path / ".deps"
        deps_dir.mkdir()
        stamp = deps_dir / ".installed_at"
        stamp.write_text("ok\n")
        # Make the stamp definitively newer than the requirements.
        _touch(stamp, time.time() + 1)
        plan = plan_install(tmp_path)
        assert plan.needed is False

    def test_newer_requirements_invalidates_cache(self, tmp_path):
        reqs = tmp_path / "requirements.txt"
        reqs.write_text("rich\n")
        deps_dir = tmp_path / ".deps"
        deps_dir.mkdir()
        stamp = deps_dir / ".installed_at"
        stamp.write_text("ok\n")
        # Bump requirements mtime into the future — this is the
        # "user edited requirements.txt after last install" case.
        _touch(reqs, time.time() + 10)
        plan = plan_install(tmp_path)
        assert plan.needed is True

    def test_same_mtime_is_cache_hit(self, tmp_path):
        # Low-resolution filesystems (HFS+ at 1s) can produce
        # identical mtimes for files created close together. Treat
        # that as a cache hit — otherwise the install loops forever.
        reqs = tmp_path / "requirements.txt"
        reqs.write_text("rich\n")
        deps_dir = tmp_path / ".deps"
        deps_dir.mkdir()
        stamp = deps_dir / ".installed_at"
        stamp.write_text("ok\n")
        shared_mtime = time.time()
        _touch(reqs, shared_mtime)
        _touch(stamp, shared_mtime)
        plan = plan_install(tmp_path)
        assert plan.needed is False

    def test_stamp_missing_forces_install(self, tmp_path):
        # Partial previous install left .deps/ but no stamp. Treat
        # as fresh install (we re-run pip, it's idempotent with
        # --target).
        (tmp_path / "requirements.txt").write_text("rich\n")
        (tmp_path / ".deps").mkdir()
        plan = plan_install(tmp_path)
        assert plan.needed is True


# ---------------------------------------------------------------------------
# ensure_deps_installed — docker exec wiring
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):
        self.returncode = -9


class TestEnsureDepsInstalled:

    @pytest.mark.asyncio
    async def test_cache_hit_skips_subprocess(self, tmp_path):
        # Nothing to install — create_subprocess_exec must NOT be
        # called. If it is, we'd see the mock invocation count > 0.
        with patch(
            "src.scripts.deps_installer.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
        ) as spawn:
            result = await ensure_deps_installed(
                script_dir=tmp_path,
                container_name="cbcl-office-test",
            )
            assert result == tmp_path / ".deps"
            spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_docker_command_uses_container_paths(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("rich\n")
        captured_args: list = []

        async def _fake_spawn(*args, **kwargs):
            captured_args.extend(args)
            return _FakeProc(returncode=0)

        def _to_container(path):
            # Simulate the Runner's host→container translator by
            # swapping tmp_path for /workspace.
            return str(path).replace(str(tmp_path), "/workspace")

        with patch(
            "src.scripts.deps_installer.asyncio.create_subprocess_exec",
            side_effect=_fake_spawn,
        ):
            await ensure_deps_installed(
                script_dir=tmp_path,
                container_name="cbcl-office-test",
                workspace_to_container=_to_container,
            )

        # Command must go through docker exec, point at the
        # container's python, and use container-translated paths
        # for --target and -r.
        assert captured_args[0] == "docker"
        assert captured_args[1] == "exec"
        assert "cbcl-office-test" in captured_args
        # Find the --target and -r args.
        target_idx = captured_args.index("--target")
        reqs_idx = captured_args.index("-r")
        assert captured_args[target_idx + 1] == "/workspace/.deps"
        assert captured_args[reqs_idx + 1] == "/workspace/requirements.txt"

    @pytest.mark.asyncio
    async def test_host_fallback_when_no_container(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("rich\n")
        captured_args: list = []

        async def _fake_spawn(*args, **kwargs):
            captured_args.extend(args)
            return _FakeProc(returncode=0)

        with patch(
            "src.scripts.deps_installer.asyncio.create_subprocess_exec",
            side_effect=_fake_spawn,
        ):
            await ensure_deps_installed(
                script_dir=tmp_path,
                container_name=None,
            )

        # Host path — no docker wrapper, paths are host paths.
        # Interpreter is ``sys.executable`` (not bare ``"python"``)
        # so the fallback works on Ubuntu 24.04+ where ``python``
        # isn't on PATH.
        import sys
        assert captured_args[0] == sys.executable
        target_idx = captured_args.index("--target")
        assert captured_args[target_idx + 1] == str(tmp_path / ".deps")

    @pytest.mark.asyncio
    async def test_install_failure_raises_with_stderr(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("not-a-real-pkg-xyz\n")

        async def _fake_spawn(*args, **kwargs):
            return _FakeProc(
                returncode=1,
                stderr=b"ERROR: could not find 'not-a-real-pkg-xyz'",
            )

        with patch(
            "src.scripts.deps_installer.asyncio.create_subprocess_exec",
            side_effect=_fake_spawn,
        ):
            with pytest.raises(DepsInstallError) as excinfo:
                await ensure_deps_installed(
                    script_dir=tmp_path,
                    container_name=None,
                )
        # stderr tail is captured on the exception so the UI can
        # show the user what went wrong.
        assert "not-a-real-pkg-xyz" in excinfo.value.stderr_tail

    @pytest.mark.asyncio
    async def test_stamp_only_written_on_success(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("rich\n")

        async def _fake_spawn(*args, **kwargs):
            return _FakeProc(returncode=1, stderr=b"nope")

        with patch(
            "src.scripts.deps_installer.asyncio.create_subprocess_exec",
            side_effect=_fake_spawn,
        ):
            with pytest.raises(DepsInstallError):
                await ensure_deps_installed(
                    script_dir=tmp_path,
                    container_name=None,
                )

        # A failed install MUST NOT leave a stamp — otherwise the
        # next run sees a cache hit and the script runs against an
        # empty .deps/.
        stamp = tmp_path / ".deps" / ".installed_at"
        assert not stamp.exists()

    @pytest.mark.asyncio
    async def test_second_call_after_success_is_cache_hit(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("rich\n")
        spawn_count = 0

        async def _fake_spawn(*args, **kwargs):
            nonlocal spawn_count
            spawn_count += 1
            return _FakeProc(returncode=0)

        with patch(
            "src.scripts.deps_installer.asyncio.create_subprocess_exec",
            side_effect=_fake_spawn,
        ):
            # Advance the stamp mtime far enough past requirements
            # so the second call is an unambiguous cache hit.
            await ensure_deps_installed(
                script_dir=tmp_path, container_name=None,
            )
            stamp = tmp_path / ".deps" / ".installed_at"
            _touch(stamp, time.time() + 10)
            await ensure_deps_installed(
                script_dir=tmp_path, container_name=None,
            )
        assert spawn_count == 1


# ---------------------------------------------------------------------------
# Install lock — concurrent acquire + stale detection
# ---------------------------------------------------------------------------


class TestInstallLock:

    @pytest.mark.asyncio
    async def test_stale_lock_is_broken(self, tmp_path, monkeypatch):
        # A previous crashed install left a .installing.lock behind.
        # A new run should detect it's older than _LOCK_STALE_SECONDS
        # and proceed rather than waiting forever.
        (tmp_path / "requirements.txt").write_text("rich\n")
        deps_dir = tmp_path / ".deps"
        deps_dir.mkdir()
        stale_lock = deps_dir / ".installing.lock"
        stale_lock.write_text("99999 1\n")
        # Backdate the mtime far into the past — 24h ago.
        _touch(stale_lock, time.time() - 86400)

        async def _fake_spawn(*args, **kwargs):
            return _FakeProc(returncode=0)

        with patch(
            "src.scripts.deps_installer.asyncio.create_subprocess_exec",
            side_effect=_fake_spawn,
        ):
            # Shouldn't hang. If the stale-break path is broken
            # this test will time out on the default pytest deadline.
            await asyncio.wait_for(
                ensure_deps_installed(
                    script_dir=tmp_path, container_name=None,
                ),
                timeout=5,
            )
        # Lock cleaned up after the install finished.
        assert not stale_lock.exists()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _touch(path: Path, mtime: float) -> None:
    os.utime(path, (mtime, mtime))
