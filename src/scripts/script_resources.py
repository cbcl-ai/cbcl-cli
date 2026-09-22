"""Script leases share worker admission and survive the parent worker's exit."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import signal
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from src.agent_execution_policy import execution_resources
from src.script_resource_state import ScriptResourceConflict


def dynamic_scripts_enabled(runner) -> bool:
    supervisor = runner._resource_supervisor
    policy = getattr(supervisor, "execution_policy", None)
    if not isinstance(policy, dict):
        config = getattr(runner._config_store, "office_config", None)
        policy = (
            config.get("agent_execution_policy") if isinstance(config, dict) else None
        )
    return isinstance(policy, dict) and policy.get("enabled") is True


@dataclass
class ScriptResourceLease:
    record: dict
    runtime: object
    process: object | None = None
    launch_started: bool = False
    workspace: object | None = None

    def mark_launch(self, *, preparation: bool = False, started: bool = True) -> None:
        field = "preparation_started" if preparation else "launch_started"
        self.record[field] = started
        if not preparation:
            self.launch_started = started
        self.runtime.mark_script_resource_launch(
            self.record["lease_id"], preparation=preparation, started=started
        )

    async def confirm_stopped(self) -> None:
        """Retain the lease unless every marked container descendant is stopped."""
        if self.record.get("state") in {"stopped", "released"}:
            return
        try:
            if self.record["container_id"]:
                from pathlib import Path
                from src.docker.task_process_cleanup import (
                    _confirmed_container_stopped,
                    terminate_worker_execution,
                )
                from src.scripts.deps_installer import _launch_recorded
                from src.scripts.script_execution import _read_in_container_pid

                # Stop the docker client before scanning, then require an
                # acknowledgement for every started launch. Otherwise Docker
                # may accept a delayed exec AFTER a marker-absence scan.
                if self.process is not None and self.process.returncode is None:
                    try:
                        self.process.terminate()
                    except ProcessLookupError:
                        pass
                    await asyncio.wait_for(self.process.wait(), timeout=5)
                await terminate_worker_execution(
                    self.record["container_id"], self.record["marker"]
                )
                script_dir = (
                    Path(self.workspace) / ".scripts" / self.record["script_name"]
                    if self.workspace
                    else None
                )
                acknowledged = (
                    bool(script_dir)
                    and (
                        not self.record.get("preparation_started")
                        or _launch_recorded(script_dir / ".deps", self.record["marker"])
                    )
                    and (
                        not self.record.get("launch_started")
                        or _read_in_container_pid(
                            script_dir / "executions" / self.record["execution_id"]
                        )
                        is not None
                    )
                )
                if not acknowledged and not await asyncio.to_thread(
                    _confirmed_container_stopped, self.record["container_id"]
                ):
                    raise RuntimeError(
                        "Script launch acknowledgement is missing; resource reservation is retained until container cleanup is proven"
                    )
            elif self.process is not None:
                # Dynamic host fallback is test-only; a fresh process group keeps
                # its descendants out of the daemon's own process group.
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await asyncio.wait_for(self.process.wait(), timeout=5)
            elif self.launch_started:
                raise RuntimeError(
                    "Script process ownership is uncertain; resource reservation is retained"
                )
            if self.process is not None and self.process.returncode is None:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass
                await asyncio.wait_for(self.process.wait(), timeout=5)
            self.runtime.set_script_resource_state(self.record["lease_id"], "stopped")
            self.record["state"] = "stopped"
        except BaseException:
            self.runtime.set_script_resource_state(self.record["lease_id"], "uncertain")
            self.record["state"] = "uncertain"
            raise

    def release(self) -> None:
        if self.record.get("state") not in {"stopped", "released"}:
            raise RuntimeError(
                "Cannot release script resources before physical cleanup"
            )
        self.runtime.set_script_resource_state(self.record["lease_id"], "released")
        self.record["state"] = "released"


async def reserve_script_resources(
    runner, task: dict | None, caller: dict | None
) -> ScriptResourceLease:
    supervisor = runner._resource_supervisor
    runtime = runner._runtime_state
    if supervisor is None or runtime is None:
        raise RuntimeError(
            "Script launch deferred: dynamic execution resource coordination is unavailable"
        )
    # Null task declarations reserve shared workspace for every caller.
    resources = execution_resources(
        {"execution_resources": (task or {}).get("execution_resources")},
    )
    parent_attempt_id = (caller or {}).get("attempt_id") or ""
    if (caller or {}).get("role") == "manager":
        parent_attempt_id = ""
    container_id = runner._container_name or ""
    if container_id and not re.fullmatch(r"[0-9a-f]{64}", container_id):
        from src.scripts.deps_installer import _container_id

        container_id = await _container_id(container_id)
    record = {
        "lease_id": str(uuid4()),
        "script_name": "",
        "task_id": "",
        "parent_attempt_id": parent_attempt_id,
        "resources": resources,
        "execution_id": f"exec-{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%S')}-{uuid4().hex[:6]}",
        "marker": secrets.token_hex(32),
        "container_id": container_id,
    }
    # The caller fills script/task identity before the durable insertion below.
    return ScriptResourceLease(record, runtime, workspace=runner._workspace)


async def admit_script_resources(
    runner, lease: ScriptResourceLease, script_name: str, task_id: str | None,
    execution_caller: dict | None = None,
) -> None:
    supervisor = runner._resource_supervisor
    record = lease.record
    record.update(script_name=script_name, task_id=task_id or "")
    async with supervisor.admission_lock:
        finishing_parent = (
            isinstance(execution_caller, dict)
            and record["parent_attempt_id"] == execution_caller.get("attempt_id")
            and supervisor.can_continue_script_during_policy_drain(
                execution_caller, task_id
            )
        )
        if supervisor.config_ready is not True and not finishing_parent:
            raise ScriptResourceConflict(
                "Script launch deferred: the current office configuration is not fully applied. "
                "Wait for configuration synchronization, then retry."
            )
        if not supervisor.resources_available(
            {"allowed_tools": ["Bash"]},
            {"execution_resources": record["resources"]},
            parent_attempt_id=record["parent_attempt_id"],
        ):
            raise ScriptResourceConflict(
                "Script launch deferred: a worker or script still owns a shared resource. "
                "Wait for confirmed completion or stop, then retry; do not bypass the reservation."
            )
        lease.runtime.begin_script_resource_lease(**record)


async def terminate_legacy_script_execution(
    container_id: str, execution_id: str
) -> None:
    """Reap an older script by its exact runner-owned exec ID, never by a PID."""
    from src.docker.task_process_cleanup import (
        _CLEANUP_PROGRAM,
        _cleanup_failure_reason,
        _confirmed_container_stopped,
    )

    if not re.fullmatch(r"[0-9a-f]{64}", container_id) or not re.fullmatch(
        r"exec-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-[0-9a-f]{6}",
        execution_id,
    ):
        raise RuntimeError(
            "Legacy script cleanup requires an immutable container and a valid execution ID"
        )
    program = _CLEANUP_PROGRAM.replace(
        "CUBICLE_WORKER_EXECUTION_ID=", "CUBICLE_EXECUTION_ID="
    )
    process = await asyncio.create_subprocess_exec(
        "docker",
        "exec",
        "-i",
        "-u",
        "1000:1000",
        container_id,
        "/usr/local/bin/python3",
        "-I",
        "-S",
        "-",
        execution_id,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(
            process.communicate(program.encode()), timeout=5
        )
        if process.returncode != 0:
            if await asyncio.to_thread(_confirmed_container_stopped, container_id):
                return
            raise RuntimeError(
                f"Legacy script cleanup is unconfirmed ({_cleanup_failure_reason(stderr)})"
            )
    except BaseException:
        if process.returncode is None:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=1)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
        raise
