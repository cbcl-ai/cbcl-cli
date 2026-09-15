"""Opt-in isolated worker containers with durable, immutable launch ownership."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import uuid

from src.docker.execution_boundary import ExecutionContainerIdentity, execution_labels
from src.docker.execution_boundary import stop_execution_container, verify_execution_container
from src.docker.execution_ledger import ExecutionAdmissionError, ExecutionBudget, ExecutionCapacityUnavailable, ExecutionLedger, ExecutionResources
from src.docker.session_files import SESSION_FILE_DIRECTORY


RUNTIME_PATH = SESSION_FILE_DIRECTORY


class ExecutionReconciliationRequired(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionMount:
    source: str
    target: str
    read_only: bool = False

    def __post_init__(self):
        source, target = PurePosixPath(self.source), PurePosixPath(self.target)
        if not source.is_absolute() or not target.is_absolute() or ".." in source.parts or ".." in target.parts:
            raise ValueError("Execution mounts must be absolute, traversal-free paths")
        if str(source) in {"/", "/home", "/root", "/Users", "/var", "/etc", "/run", "/var/run"} or any(source.is_relative_to(prefix) for prefix in ("/proc", "/sys", "/dev")) or source.name.endswith(".sock"):
            raise ValueError("Host system roots and sockets cannot be execution mounts")
        if str(target) in {"/", "/home", "/home/agent", "/run", "/tmp"} or any(target.is_relative_to(prefix) for prefix in ("/proc", "/sys", "/dev", "/opt", "/usr", "/etc", RUNTIME_PATH)):
            raise ValueError("Execution mounts cannot replace runtime or system paths")


@dataclass(frozen=True)
class OfficeExecutionContext:
    office_id: str
    office_container_id: str
    image_id: str
    mounts: tuple[ExecutionMount, ...]
    network_mode: str = "bridge"

    def __post_init__(self):
        uuid.UUID(self.office_id)
        if not re.fullmatch(r"[a-f0-9]{64}", self.office_container_id) or not re.fullmatch(r"sha256:[a-f0-9]{64}", self.image_id):
            raise ValueError("Office execution context requires immutable container and image IDs")
        if self.network_mode not in {"bridge", "none"}:
            raise ValueError("Execution networking must be private bridge or disabled")
        targets = [mount.target for mount in self.mounts]
        if len(set(targets)) != len(targets):
            raise ValueError("Execution mount targets must be unique")

    @classmethod
    def from_office_container(cls, container, office_id: str, workspace_path: Path,
                              auth_path: Path, ssh_path: Path,
                              approved_extra_targets: tuple[str, ...] = ()):
        container.reload()
        attributes = container.attrs
        office_id = str(uuid.UUID(office_id))
        labels = (attributes.get("Config") or {}).get("Labels") or {}
        host = attributes.get("HostConfig") or {}
        if labels.get("cbcl.managed") != "true" or labels.get("cbcl.office_id") != office_id or (attributes.get("State") or {}).get("Running") is not True or host.get("Privileged") is not False or host.get("PidMode") not in ("", "private"):
            raise ExecutionAdmissionError("A verified live, nonprivileged office is required for isolated execution")
        expected = {
            "/workspace": str(workspace_path.resolve()),
            "/home/agent/.claude": str(auth_path.resolve()),
            "/home/agent/.ssh": str(ssh_path.resolve()),
        }
        selected = set(expected) | set(approved_extra_targets)
        mounts = []
        for mount in attributes.get("Mounts", []):
            target = mount.get("Destination")
            if target not in selected:
                continue
            if mount.get("Type") != "bind" or (target in expected and (mount.get("Source") != expected[target] or mount.get("RW") is not True)):
                raise ExecutionAdmissionError("Office execution mounts do not match their authorized paths")
            if target not in expected and any(PurePosixPath(target).is_relative_to(required) or PurePosixPath(required).is_relative_to(target) for required in expected):
                raise ExecutionAdmissionError("Extra execution mounts cannot overlap workspace or credentials")
            mounts.append(ExecutionMount(mount["Source"], target, not mount.get("RW", False)))
        if {mount.target for mount in mounts} != selected:
            raise ExecutionAdmissionError("An approved execution mount is unavailable on the office")
        return cls(office_id, attributes["Id"], attributes["Image"], tuple(sorted(mounts, key=lambda mount: mount.target)))


class ExecutionContainerManager:
    def __init__(self, client, ledger_path: Path, context: OfficeExecutionContext,
                 resources: ExecutionResources, budget: ExecutionBudget):
        self.client = client
        self.ledger = ExecutionLedger(ledger_path)
        self.context = context
        self.resources = resources
        self.budget = budget
        self._preparations: dict[str, asyncio.Task] = {}
        self._preparation_tasks: dict[str, str] = {}
        self._no_launch_attempts: dict[str, str] = {}

    def _fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self._launch_spec(), sort_keys=True).encode()).hexdigest()

    def _launch_spec(self) -> dict:
        return {"context": asdict(self.context), "resources": asdict(self.resources), "version": 1}

    @staticmethod
    def _identity(row: dict) -> ExecutionContainerIdentity:
        if not row["container_id"]:
            raise ExecutionReconciliationRequired("Execution creation has no confirmed container identity")
        return ExecutionContainerIdentity(row["container_id"], row["office_id"], row["task_id"], row["attempt_id"])

    def _labels(self, row: dict) -> dict:
        return {
            **execution_labels(row["office_id"], row["task_id"], row["attempt_id"]),
            "cbcl.execution.reservation": row["reservation_id"],
            "cbcl.execution.spec": row["fingerprint"],
            "cbcl.execution.office_container": row["office_container_id"],
        }

    def _verify(self, container, row: dict) -> dict:
        attributes = verify_execution_container(container, self._identity(row))
        labels = (attributes.get("Config") or {}).get("Labels") or {}
        host = attributes.get("HostConfig") or {}
        if any(labels.get(key) != value for key, value in self._labels(row).items()) or attributes.get("Image") != row["image_id"]:
            raise ExecutionReconciliationRequired("Execution container does not match its durable launch receipt")
        if (attributes.get("Config") or {}).get("User") != "1000:1000" or host.get("Memory") != row["memory_bytes"] or host.get("NanoCpus") != row["cpu_millis"] * 1_000_000 or host.get("PidsLimit") != row["pids"] or (host.get("RestartPolicy") or {}).get("Name") not in ("", "no"):
            raise ExecutionReconciliationRequired("Execution isolation or resource limits changed")
        security = host.get("SecurityOpt") or []
        private_runtime = (host.get("Tmpfs") or {}).get(RUNTIME_PATH, "")
        if "ALL" not in (host.get("CapDrop") or []) or host.get("CapAdd") or set(security) not in ({"no-new-privileges"}, {"no-new-privileges:true"}) or not {"nosuid", "nodev", "noexec", "mode=0700", "uid=1000", "gid=1000"}.issubset(private_runtime.split(",")):
            raise ExecutionReconciliationRequired("Execution capability or private runtime isolation is unavailable")
        encoded_spec = row.get("spec_json") or ""
        if hashlib.sha256(encoded_spec.encode()).hexdigest() != row["fingerprint"]:
            raise ExecutionReconciliationRequired("Execution launch properties lack a durable verified specification")
        context = json.loads(encoded_spec)["context"]
        expected_mounts = {(mount["source"], mount["target"], not mount["read_only"]) for mount in context["mounts"]}
        actual_mounts = {(mount.get("Source"), mount.get("Destination"), mount.get("RW")) for mount in attributes.get("Mounts", []) if mount.get("Type") == "bind"}
        invalid_mounts = any(mount.get("Type") != "bind" and not (mount.get("Type") == "tmpfs" and mount.get("Destination") == RUNTIME_PATH) for mount in attributes.get("Mounts", []))
        if expected_mounts != actual_mounts or invalid_mounts or host.get("NetworkMode") != context["network_mode"] or host.get("PortBindings"):
            raise ExecutionReconciliationRequired("Execution mounts or network exposure differ from their authorized launch")
        return attributes

    async def prepare(self, task_id: str, attempt_id: str) -> ExecutionContainerIdentity:
        task_id, attempt_id = str(uuid.UUID(task_id)), str(uuid.UUID(attempt_id))
        previous = self._preparations.get(attempt_id)
        if previous is not None:
            identity = await asyncio.shield(previous)
            if identity.task_id != task_id:
                raise ExecutionAdmissionError("An attempt cannot be reused for another task")
            return identity
        operation = asyncio.create_task(self._prepare(task_id, attempt_id))
        self._preparations[attempt_id] = operation
        self._preparation_tasks[attempt_id] = task_id
        def finished(completed):
            if self._preparations.get(attempt_id) is completed:
                self._preparations.pop(attempt_id, None)
                self._preparation_tasks.pop(attempt_id, None)
            if not completed.cancelled():
                completed.exception()

        operation.add_done_callback(finished)
        return await asyncio.shield(operation)

    async def _prepare(self, task_id: str, attempt_id: str) -> ExecutionContainerIdentity:
        try:
            row, created = self.ledger.reserve(
                office_id=self.context.office_id, task_id=task_id, attempt_id=attempt_id,
                fingerprint=self._fingerprint(), office_container_id=self.context.office_container_id,
                image_id=self.context.image_id, resources=self.resources, budget=self.budget,
                launch_spec=self._launch_spec(),
            )
        except ExecutionCapacityUnavailable:
            if self.ledger.get(attempt_id) is None:
                self._no_launch_attempts[attempt_id] = task_id
            raise
        self._no_launch_attempts.pop(attempt_id, None)
        if not created:
            if row["state"] != "running":
                raise ExecutionReconciliationRequired("Retained execution must be reconciled, never automatically relaunched")
            container = await asyncio.to_thread(self.client.containers.get, row["container_id"])
            attributes = await asyncio.to_thread(self._verify, container, row)
            if (attributes.get("State") or {}).get("Running") is not True:
                raise ExecutionReconciliationRequired("Retained execution is no longer running")
            return self._identity(row)
        try:
            from docker.types import Mount

            host_config = self.client.api.create_host_config(
                init=True,
                network_mode=self.context.network_mode, extra_hosts={"host.docker.internal": "host-gateway"},
                privileged=False, cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"], restart_policy={"Name": "no"},
                mem_limit=self.resources.memory_bytes, nano_cpus=self.resources.cpu_millis * 1_000_000,
                pids_limit=self.resources.pids,
                tmpfs={RUNTIME_PATH: "rw,nosuid,nodev,noexec,size=16m,mode=0700,uid=1000,gid=1000"},
                mounts=[Mount(target=mount.target, source=mount.source, type="bind", read_only=mount.read_only) for mount in self.context.mounts],
            )
            created_response = await asyncio.to_thread(
                self.client.api.create_container, self.context.image_id,
                command=["-f", "/dev/null"], entrypoint="tail",
                name=f"cbcl-execution-{attempt_id}", detach=True, user="1000:1000",
                working_dir="/workspace", labels=self._labels(row), host_config=host_config,
                environment={"OFFICE_ID": self.context.office_id, "CUBICLE_EXECUTION_RUNTIME_DIR": RUNTIME_PATH},
            )
            row = self.ledger.bind_container(attempt_id, row["reservation_id"], created_response["Id"])
            container = await asyncio.to_thread(self.client.containers.get, row["container_id"])
            await asyncio.to_thread(self._verify, container, row)
            self.ledger.transition(attempt_id, container.id, "starting")
            await asyncio.to_thread(container.start)
            attributes = await asyncio.to_thread(self._verify, container, row)
            if (attributes.get("State") or {}).get("Running") is not True:
                raise ExecutionReconciliationRequired("Execution container startup was not confirmed")
            self.ledger.transition(attempt_id, container.id, "running_unready")
            result = await asyncio.to_thread(
                container.exec_run,
                ["python3", "-I", "-S", "-c", "import os; target='/home/agent/.claude/.claude.json'; path='/home/agent/.claude.json'; exists=os.path.lexists(path); assert not exists or (os.path.islink(path) and os.readlink(path)==target), 'Claude config ownership mismatch'; os.symlink(target,path) if not exists else None"],
                user="1000:1000",
            )
            if result.exit_code != 0:
                raise ExecutionReconciliationRequired("Claude configuration link preparation was not confirmed")
            self.ledger.transition(attempt_id, container.id, "running")
            return self._identity(row)
        except BaseException:
            current = self.ledger.get(attempt_id)
            if current is not None and current["state"] not in {"stopped", "running_unready"}:
                self.ledger.transition(attempt_id, current["container_id"], "uncertain")
            raise

    async def stop_attempt(self, task_id: str, attempt_id: str) -> None:
        task_id, attempt_id = str(uuid.UUID(task_id)), str(uuid.UUID(attempt_id))
        operation = self._preparations.get(attempt_id)
        if operation is not None:
            try:
                await asyncio.wait_for(asyncio.shield(operation), timeout=30)
            except asyncio.TimeoutError as exc:
                raise ExecutionReconciliationRequired("Execution creation is still unsettled; admission remains retained") from exc
            except Exception:
                pass
        row = self.ledger.get(attempt_id)
        if row is None and self._no_launch_attempts.get(attempt_id) == task_id:
            return
        if row is None or row["office_id"] != self.context.office_id or row["task_id"] != task_id:
            raise ExecutionAdmissionError("Stop does not own a retained execution attempt")
        await self.stop(self._identity(row))

    async def task_available(self, task_id: str) -> bool:
        return await asyncio.to_thread(self.ledger.task_available, self.context.office_id, str(uuid.UUID(task_id)))

    async def available(self) -> bool:
        return await asyncio.to_thread(self.ledger.available, self.context.office_id, self.resources, self.budget)

    async def stop(self, identity: ExecutionContainerIdentity) -> None:
        row = self.ledger.get(identity.attempt_id)
        if row is None or identity != self._identity(row) or identity.office_id != self.context.office_id:
            raise ExecutionAdmissionError("Stop does not match the retained execution container")
        if row["state"] not in {"running", "running_unready", "stopped"}:
            raise ExecutionReconciliationRequired("Creation or startup is unconfirmed; do not release its reservation")
        container = await asyncio.to_thread(self.client.containers.get, identity.container_id)
        await asyncio.to_thread(self._verify, container, row)
        await asyncio.to_thread(stop_execution_container, self.client, identity)
        self.ledger.transition(identity.attempt_id, identity.container_id, "stopped")

    async def stop_task(self, task_id: str) -> bool:
        task_id = str(uuid.UUID(task_id))
        rows = [row for row in self.ledger.unresolved(self.context.office_id) if row["task_id"] == task_id]
        attempts = {row["attempt_id"] for row in rows} | {attempt for attempt, task in self._preparation_tasks.items() if task == task_id}
        for attempt_id in attempts:
            await self.stop_attempt(task_id, attempt_id)
        return bool(attempts)

    async def stop_all(self) -> None:
        tasks = {row["task_id"] for row in self.ledger.unresolved(self.context.office_id)} | set(self._preparation_tasks.values())
        errors = []
        for task_id in tasks:
            try:
                await self.stop_task(task_id)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExecutionReconciliationRequired("Some execution containers require verified reconciliation; shutdown is not confirmed") from errors[0]

    async def reconcile(self) -> list[dict]:
        reports = []
        for row in self.ledger.unresolved(self.context.office_id):
            try:
                if not row["container_id"]:
                    candidates = await asyncio.to_thread(self.client.containers.list, all=True, filters={"label": [f"{key}={value}" for key, value in self._labels(row).items()]})
                    if len(candidates) != 1:
                        raise ExecutionReconciliationRequired("Creation has no unique acknowledged container; do not recreate")
                    await asyncio.to_thread(self._verify, candidates[0], {**row, "container_id": candidates[0].id})
                    row = self.ledger.bind_container(row["attempt_id"], row["reservation_id"], candidates[0].id)
                container = await asyncio.to_thread(self.client.containers.get, row["container_id"])
                attributes = await asyncio.to_thread(self._verify, container, row)
                state = attributes.get("State") or {}
                if state.get("Running") is False and state.get("Pid") == 0 and state.get("Status") in {"exited", "dead"} and row["state"] in {"running", "running_unready"}:
                    self.ledger.transition(row["attempt_id"], row["container_id"], "stopped")
                    disposition = "stopped"
                else:
                    disposition = "running_reconciliation_required" if state.get("Running") else "created_reconciliation_required"
                reports.append({"task_id": row["task_id"], "attempt_id": row["attempt_id"], "container_id": row["container_id"], "state": disposition})
            except Exception:
                reports.append({"task_id": row["task_id"], "attempt_id": row["attempt_id"], "container_id": row["container_id"], "state": "unconfirmed"})
        return reports

    async def close(self) -> None:
        if any(not operation.done() for operation in self._preparations.values()):
            raise ExecutionReconciliationRequired("Execution creation is unsettled; keep its Docker client available")
        await asyncio.to_thread(self.client.close)
