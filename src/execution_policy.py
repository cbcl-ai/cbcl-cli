"""Explicit local opt-in for an additional, bounded isolated execution pool."""

from __future__ import annotations

import asyncio
import math
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

import yaml

from src.docker.execution_ledger import ExecutionBudget, ExecutionResources, read_execution_inventory


@dataclass(frozen=True)
class WorkerExecutionPolicy:
    resources: ExecutionResources
    budget: ExecutionBudget


def load_worker_execution_policy(office_id: str, config_path: Path) -> WorkerExecutionPolicy | None:
    if not config_path.exists():
        return None
    data = yaml.safe_load(config_path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError("Communicator configuration must be a mapping")
    policy = data.get("execution_containers")
    if policy is None:
        return None
    if not isinstance(policy, dict):
        raise ValueError("execution_containers must be a mapping")
    offices = policy.get("offices", [])
    if not isinstance(offices, list):
        raise ValueError("execution_containers.offices must list immutable office UUIDs")
    selected = {str(uuid.UUID(value)) for value in offices if isinstance(value, str)}
    if len(selected) != len(offices):
        raise ValueError("execution_containers.offices contains invalid or duplicate identities")
    if str(office_id) not in selected:
        return None
    if policy.get("acknowledge_shared_auth") is not True:
        raise ValueError("Isolated execution requires acknowledge_shared_auth: true; CLI auth/cache remains shared")
    supported = {
        "offices", "acknowledge_shared_auth", "max_workers_per_office",
        "worker_cpus", "worker_memory", "worker_pids",
    }
    if set(policy) - supported:
        raise ValueError("Unknown execution_containers setting; refusing silent fallback")
    workers = policy.get("max_workers_per_office", 2)
    cpus = policy.get("worker_cpus", 1)
    memory = policy.get("worker_memory", "2g")
    pids = policy.get("worker_pids", 256)
    if type(workers) is not int or not 1 <= workers <= 20:
        raise ValueError("max_workers_per_office must be an integer from 1 to 20")
    if isinstance(cpus, bool) or not isinstance(cpus, (int, float)) or not math.isfinite(cpus) or not 0.1 <= cpus <= 64:
        raise ValueError("worker_cpus must be finite and between 0.1 and 64")
    if type(pids) is not int or not 64 <= pids <= 4096:
        raise ValueError("worker_pids must be an integer from 64 to 4096")
    match = re.fullmatch(r"([1-9][0-9]*)([mg])", memory) if isinstance(memory, str) else None
    if not match:
        raise ValueError("worker_memory must use positive m/g units, for example 2g")
    memory_bytes = int(match[1]) * 1024 ** (2 if match[2] == "m" else 3)
    if not 256 * 1024 ** 2 <= memory_bytes <= 128 * 1024 ** 3:
        raise ValueError("worker_memory must be between 256m and 128g")
    resources = ExecutionResources(int(cpus * 1000), memory_bytes, pids)
    budget = ExecutionBudget(workers, resources.cpu_millis * workers, memory_bytes * workers, pids * workers)
    return WorkerExecutionPolicy(resources, budget)


async def configure_worker_execution(office, container_id: str):
    from src.docker.execution_launcher import ExecutionContainerManager, OfficeExecutionContext
    from src.office_runtime import claude_auth_dir, ssh_keys_dir
    from src.paths import get_config_path, get_runtime_state_path

    policy = load_worker_execution_policy(str(office.id), get_config_path())
    ledger_path = get_runtime_state_path().with_name("execution-containers.sqlite")
    if policy is None:
        retained = await asyncio.to_thread(read_execution_inventory, ledger_path, str(office.id))
        if retained:
            raise ValueError("Retained isolated executions prevent disabling containment; restore the policy and reconcile before changing modes")
        return None
    import docker

    client = docker.from_env(timeout=15)
    try:
        container = await asyncio.to_thread(client.containers.get, container_id)
        context = await asyncio.to_thread(
            OfficeExecutionContext.from_office_container,
            container, str(office.id), Path(office.workspace_path),
            claude_auth_dir(str(office.id)), ssh_keys_dir(str(office.id)),
            tuple(mount["container_path"] for mount in office.extra_mounts),
        )
        manager = ExecutionContainerManager(
            client, ledger_path,
            context, policy.resources, policy.budget,
        )
        await manager.reconcile()
        return manager
    except BaseException:
        client.close()
        raise
