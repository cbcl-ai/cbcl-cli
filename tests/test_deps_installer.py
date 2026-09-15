"""Dependency preparation ownership, cache receipts and failure classification."""

import asyncio
import json
import os
import secrets
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.scripts import deps_install_runtime as runtime
from src.scripts import deps_installer as installer


def complete_cache(script_dir, marker="a" * 64):
    deps_dir = script_dir / ".deps"
    deps_dir.mkdir(exist_ok=True)
    digest = runtime.requirements_digest(script_dir / "requirements.txt")
    (deps_dir / ".installed_at").write_text(json.dumps({"requirements_sha256": digest}))
    (deps_dir / ".installing.lock").write_text(json.dumps({"format": runtime.LOCK_FORMAT, "state": "complete", "marker": marker}))


def test_without_requirements_no_install(tmp_path):
    assert not installer.plan_install(tmp_path).needed


def test_first_use_or_legacy_stamp_requires_receipt(tmp_path):
    (tmp_path / "requirements.txt").write_text("example==1")
    assert installer.plan_install(tmp_path).needed
    (tmp_path / ".deps").mkdir()
    (tmp_path / ".deps/.installed_at").write_text("ok\n")
    assert installer.plan_install(tmp_path).needed


def test_content_receipt_not_clock_skew_controls_cache(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("example==1")
    complete_cache(tmp_path)
    os.utime(requirements, (100000, 100000))
    assert not installer.plan_install(tmp_path).needed
    requirements.write_text("example==2")
    os.utime(requirements, (100000, 100000))
    assert installer.plan_install(tmp_path).needed


@pytest.mark.parametrize("state", ["running", "uncertain", "failed"])
def test_incomplete_install_never_uses_prior_success_stamp(tmp_path, state):
    (tmp_path / "requirements.txt").write_text("example==1")
    complete_cache(tmp_path)
    (tmp_path / ".deps/.installing.lock").write_text(json.dumps({"format": runtime.LOCK_FORMAT, "state": state}))
    assert installer.plan_install(tmp_path).needed


@pytest.fixture
def mocked_launch(tmp_path, monkeypatch):
    captured = []
    process = SimpleNamespace(returncode=0, pid=987654, wait=AsyncMock(return_value=0), kill=lambda: None)

    async def communicate(program):
        assert b"flock" in program
        if process.returncode == 0:
            complete_cache(tmp_path)
        return b"", b"synthetic failure"

    process.communicate = communicate

    async def launch(*arguments, **options):
        captured.append((arguments, options))
        if process.returncode:
            (tmp_path / ".deps/.installing.lock").write_text(json.dumps({
                "format": runtime.LOCK_FORMAT, "state": "uncertain",
                "marker": options["env"][runtime.MARKER_ENV],
            }))
        return process

    monkeypatch.setattr(installer, "_container_id", AsyncMock(return_value="c" * 64))
    monkeypatch.setattr(installer.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(installer.os, "killpg", lambda *_arguments: None)
    cleanup = AsyncMock()
    monkeypatch.setattr("src.docker.task_process_cleanup.terminate_worker_execution", cleanup)
    monkeypatch.setattr("src._chown.chown_to_agent", lambda *_arguments: None)
    return captured, process, cleanup


async def test_cache_hit_does_not_launch(tmp_path, mocked_launch):
    (tmp_path / "requirements.txt").write_text("example==1")
    complete_cache(tmp_path)
    assert await installer.ensure_deps_installed(script_dir=tmp_path, container_name="office") == tmp_path / ".deps"
    assert not mocked_launch[0]


async def test_docker_runtime_uses_exact_container_and_private_marker(tmp_path, mocked_launch):
    (tmp_path / "requirements.txt").write_text("example==1")
    await installer.ensure_deps_installed(script_dir=tmp_path, container_name="office", workspace_to_container=lambda target: str(target).replace(str(tmp_path), "/workspace"))
    arguments, options = mocked_launch[0][0]
    assert arguments[:5] == ("docker", "exec", "-i", "-e", runtime.MARKER_ENV)
    assert "c" * 64 in arguments
    assert "office" not in arguments
    assert arguments[arguments.index("--target") + 1] == "/workspace/.deps"
    assert arguments[arguments.index("--requirements") + 1] == "/workspace/requirements.txt"
    marker = options["env"][runtime.MARKER_ENV]
    assert len(marker) == 64 and marker not in arguments
    assert (tmp_path / ".deps/.installing.lock").exists()


async def test_host_fallback_uses_same_locking_runtime(tmp_path, mocked_launch):
    (tmp_path / "requirements.txt").write_text("example==1")
    await installer.ensure_deps_installed(script_dir=tmp_path, container_name=None)
    arguments, _options = mocked_launch[0][0]
    assert arguments[0] == installer.sys.executable
    assert "--lock-timeout" in arguments
    assert arguments[arguments.index("--target") + 1] == str(tmp_path / ".deps")


async def test_confirmed_failure_cleans_owned_container_and_never_stamps(tmp_path, mocked_launch):
    (tmp_path / "requirements.txt").write_text("example==1")
    mocked_launch[1].returncode = 1
    with pytest.raises(installer.DepsInstallError) as error:
        await installer.ensure_deps_installed(script_dir=tmp_path, container_name="office")
    assert error.value.stderr_tail == "synthetic failure"
    assert not (tmp_path / ".deps/.installed_at").exists()
    mocked_launch[2].assert_awaited_once()
    assert mocked_launch[2].await_args.args[0] == "c" * 64


@pytest.mark.parametrize("interrupt", [TimeoutError, asyncio.CancelledError])
async def test_unacknowledged_interrupted_launch_stays_uncertain(tmp_path, mocked_launch, interrupt):
    (tmp_path / "requirements.txt").write_text("example==1")
    mocked_launch[1].communicate = AsyncMock(side_effect=interrupt())
    with pytest.raises(installer.DepsCleanupUnconfirmed):
        await installer.ensure_deps_installed(script_dir=tmp_path, container_name="office")
    mocked_launch[2].assert_awaited_once()
    assert not (tmp_path / ".deps/.installed_at").exists()


async def test_failed_container_cleanup_is_not_an_ordinary_install_failure(tmp_path, mocked_launch):
    (tmp_path / "requirements.txt").write_text("example==1")
    mocked_launch[1].returncode = 1
    mocked_launch[2].side_effect = RuntimeError("unconfirmed")
    with pytest.raises(installer.DepsCleanupUnconfirmed):
        await installer.ensure_deps_installed(script_dir=tmp_path, container_name="office")


async def test_interrupted_host_fallback_never_claims_detached_pip_is_gone(tmp_path, mocked_launch):
    (tmp_path / "requirements.txt").write_text("example==1")
    mocked_launch[1].communicate = AsyncMock(side_effect=TimeoutError())
    with pytest.raises(installer.DepsCleanupUnconfirmed):
        await installer.ensure_deps_installed(script_dir=tmp_path, container_name=None)
    mocked_launch[2].assert_not_awaited()


async def test_failed_host_fallback_retains_admission(tmp_path, mocked_launch):
    (tmp_path / "requirements.txt").write_text("example==1")
    mocked_launch[1].returncode = 1
    with pytest.raises(installer.DepsCleanupUnconfirmed):
        await installer.ensure_deps_installed(script_dir=tmp_path, container_name=None)


async def test_completed_docker_client_without_launch_receipt_is_not_remote_exit_proof(tmp_path, mocked_launch):
    (tmp_path / "requirements.txt").write_text("example==1")
    mocked_launch[1].returncode = 1

    async def failed_without_receipt(_program):
        (tmp_path / ".deps/.installing.lock").unlink()
        return b"", b"connection lost"

    mocked_launch[1].communicate = failed_without_receipt
    with pytest.raises(installer.DepsCleanupUnconfirmed) as error:
        await installer.ensure_deps_installed(script_dir=tmp_path, container_name="office")
    assert error.value.stderr_tail == "connection lost"


def test_aged_legacy_lock_is_preserved_not_broken(tmp_path):
    (tmp_path / "requirements.txt").write_text("example==1")
    deps_dir = tmp_path / ".deps"
    deps_dir.mkdir()
    lock = deps_dir / ".installing.lock"
    lock.write_text("99999 1\n")
    os.utime(lock, (1, 1))
    with pytest.raises(RuntimeError, match="reconciliation"):
        runtime.install(tmp_path / "requirements.txt", deps_dir, marker=secrets.token_hex(32), install_timeout=1, lock_timeout=1)
    assert lock.read_text() == "99999 1\n"
