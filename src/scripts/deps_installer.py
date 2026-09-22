"""Managed dependency preparation with in-container lock ownership.

Pip and its wrapper hold a stable kernel file lock inside the office. The
host client never unlinks that inode or breaks a lock by age. An interrupted
launch stays uncertain unless its owned container cleanup is confirmed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from src.scripts.deps_install_runtime import LOCK_FORMAT, cache_valid

logger = logging.getLogger(__name__)

_INSTALL_TIMEOUT_SECONDS = 600
_LOCK_WAIT_TIMEOUT = 660
_CLIENT_GRACE_SECONDS = 20


class DepsInstallError(RuntimeError):
    def __init__(self, message: str, stderr_tail: str = "") -> None:
        super().__init__(message)
        self.stderr_tail = stderr_tail


class DepsCleanupUnconfirmed(DepsInstallError):
    """Dependency processes may remain; retain admission until reconciliation."""


@dataclass(frozen=True)
class DepsInstallPlan:
    needed: bool
    deps_dir: Path
    requirements_file: Path


def plan_install(script_dir: Path) -> DepsInstallPlan:
    requirements_file = script_dir / "requirements.txt"
    deps_dir = script_dir / ".deps"
    lock = deps_dir / ".installing.lock"
    receipt = {}
    if lock.exists():
        try:
            receipt = json.loads(lock.read_text())
        except (OSError, ValueError):
            return DepsInstallPlan(True, deps_dir, requirements_file)
        if (
            not isinstance(receipt, dict)
            or receipt.get("format") != LOCK_FORMAT
            or receipt.get("state") != "complete"
        ):
            return DepsInstallPlan(True, deps_dir, requirements_file)
    if not requirements_file.is_file():
        return DepsInstallPlan(False, deps_dir, requirements_file)
    valid = receipt.get("state") == "complete" and cache_valid(
        requirements_file, deps_dir / ".installed_at",
    )
    return DepsInstallPlan(not valid, deps_dir, requirements_file)


async def ensure_deps_installed(
    *,
    script_dir: Path,
    container_name: str | None,
    workspace_to_container: Callable[[Path], str] = str,
    execution_marker: str | None = None,
) -> Path:
    plan = plan_install(script_dir)
    if not plan.needed:
        return plan.deps_dir
    from src._chown import chown_to_agent

    plan.deps_dir.mkdir(parents=True, exist_ok=True)
    chown_to_agent(plan.deps_dir)
    await _run_pip_install(
        container_name=container_name,
        script_dir=script_dir,
        deps_dir=plan.deps_dir,
        requirements_file=plan.requirements_file,
        workspace_to_container=workspace_to_container,
        **({"execution_marker": execution_marker} if execution_marker else {}),
    )
    if plan_install(script_dir).needed:
        raise DepsInstallError("Dependency cache was not confirmed; requirements may have changed")
    return plan.deps_dir


async def _container_id(container_name: str) -> str:
    def inspect() -> str:
        import docker

        client = docker.from_env(timeout=5)
        try:
            container = client.containers.get(container_name)
            if container.status != "running":
                raise DepsInstallError("Office container is not running")
            return container.id
        finally:
            client.close()

    try:
        return await asyncio.to_thread(inspect)
    except Exception as exc:
        raise DepsInstallError("Cannot verify office container for dependency preparation") from exc


async def _cleanup_launcher(process, container_id: str | None, marker: str) -> None:
    try:
        if container_id:
            if process.returncode is None:
                process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError as exc:
        raise DepsCleanupUnconfirmed("Dependency launcher termination is unconfirmed") from exc
    if container_id:
        from src.docker.task_process_cleanup import terminate_worker_execution

        try:
            await terminate_worker_execution(container_id, marker)
        except Exception as exc:
            raise DepsCleanupUnconfirmed(
                "Dependency container cleanup is unconfirmed; "
                "new work must wait for reconciliation"
            ) from exc


def _launch_recorded(deps_dir: Path, marker: str) -> bool:
    try:
        receipt = json.loads((deps_dir / ".installing.lock").read_text())
        return (
            isinstance(receipt, dict) and receipt.get("format") == LOCK_FORMAT
            and receipt.get("marker") == marker
        )
    except (OSError, ValueError):
        return False


async def _run_pip_install(
    *,
    container_name: str | None,
    script_dir: Path,
    deps_dir: Path,
    requirements_file: Path,
    workspace_to_container: Callable[[Path], str],
    execution_marker: str | None = None,
) -> None:
    from src.docker.task_process_cleanup import WORKER_EXECUTION_ENV

    marker = execution_marker or secrets.token_hex(32)
    container_id = await _container_id(container_name) if container_name else None
    arguments = [
        "--target", workspace_to_container(deps_dir) if container_id else str(deps_dir),
        "--requirements", workspace_to_container(requirements_file) if container_id else str(requirements_file),
        "--timeout", str(_INSTALL_TIMEOUT_SECONDS), "--lock-timeout", str(_LOCK_WAIT_TIMEOUT),
    ]
    program = Path(__file__).with_name("deps_install_runtime.py").read_bytes()
    argv = (
        ["docker", "exec", "-i", "-e", WORKER_EXECUTION_ENV,
         container_id, "python3", "-I", "-S", "-"]
        if container_id else [sys.executable, "-I", "-S", "-"]
    )
    environment = {**os.environ, WORKER_EXECUTION_ENV: marker}
    launch = asyncio.create_task(asyncio.create_subprocess_exec(
        *argv, *arguments, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=environment, start_new_session=True,
    ))
    process = None
    try:
        process = await asyncio.shield(launch)
        stdout, stderr = await asyncio.wait_for(
            process.communicate(program),
            timeout=_INSTALL_TIMEOUT_SECONDS + _LOCK_WAIT_TIMEOUT + _CLIENT_GRACE_SECONDS,
        )
    except BaseException as exc:
        if launch.done() and not launch.cancelled() and launch.exception() is not None:
            raise DepsInstallError(f"Dependency launcher unavailable for {script_dir.name}") from exc
        try:
            if process is None:
                process = await asyncio.wait_for(asyncio.shield(launch), timeout=10)
            await asyncio.shield(_cleanup_launcher(process, container_id, marker))
            if not container_id:
                raise DepsCleanupUnconfirmed(
                    "Interrupted host fallback cannot verify detached pip cleanup"
                )
            if container_id and not _launch_recorded(deps_dir, marker):
                raise DepsCleanupUnconfirmed(
                    "Dependency launch was not acknowledged; "
                    "delayed execution requires reconciliation"
                )
        except Exception as cleanup_error:
            raise DepsCleanupUnconfirmed("Dependency launch or cleanup is unconfirmed; admission is retained") from cleanup_error
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise DepsInstallError(f"Dependency preparation interrupted for {script_dir.name}") from exc
    if process.returncode != 0:
        await _cleanup_launcher(process, container_id, marker)
        tail = (stderr.decode(errors="replace") or stdout.decode(errors="replace"))[-2000:]
        if not container_id or not _launch_recorded(deps_dir, marker):
            raise DepsCleanupUnconfirmed(
                "Failed dependency launch lacks verified cleanup evidence; "
                "admission is retained for reconciliation",
                stderr_tail=tail,
            )
        raise DepsInstallError(f"Dependency preparation failed for {script_dir.name}", stderr_tail=tail)
    logger.info("Dependency cache confirmed for %s", script_dir.name)
