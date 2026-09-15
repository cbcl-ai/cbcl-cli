"""Exact Docker ownership boundary for future per-attempt execution containers."""

from __future__ import annotations

from dataclasses import dataclass
import re
import uuid


class ExecutionBoundaryError(RuntimeError):
    pass


def execution_labels(office_id: str, task_id: str, attempt_id: str) -> dict[str, str]:
    return {
        "cbcl.execution.managed": "true",
        "cbcl.execution.office": str(uuid.UUID(office_id)),
        "cbcl.execution.task": str(uuid.UUID(task_id)),
        "cbcl.execution.attempt": str(uuid.UUID(attempt_id)),
    }


@dataclass(frozen=True)
class ExecutionContainerIdentity:
    container_id: str
    office_id: str
    task_id: str
    attempt_id: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-f0-9]{64}", self.container_id):
            raise ValueError("An immutable full Docker container ID is required")
        execution_labels(self.office_id, self.task_id, self.attempt_id)


def verify_execution_container(container, identity: ExecutionContainerIdentity) -> dict:
    container.reload()
    attributes = container.attrs
    labels = (attributes.get("Config") or {}).get("Labels") or {}
    expected = execution_labels(identity.office_id, identity.task_id, identity.attempt_id)
    host = attributes.get("HostConfig") or {}
    if attributes.get("Id") != identity.container_id or any(labels.get(key) != value for key, value in expected.items()):
        raise ExecutionBoundaryError("Execution container identity does not match its retained owner")
    if (
        host.get("Privileged") is not False
        or host.get("PidMode") not in ("", "private")
        or host.get("NetworkMode") in ("host", "container")
        or str(host.get("NetworkMode", "")).startswith("container:")
        or host.get("Init") is not True
    ):
        raise ExecutionBoundaryError("Execution container does not have the required private process boundary")
    return attributes


def stop_execution_container(client, identity: ExecutionContainerIdentity, *, timeout: int = 15) -> None:
    """Stop only the verified exact container; retain it for inspection.

    The caller must configure a bounded Docker API transport timeout. Missing
    containers or unavailable Docker state are unconfirmed, not success.
    """
    container = client.containers.get(identity.container_id)
    attributes = verify_execution_container(container, identity)
    if (attributes.get("State") or {}).get("Running") is True:
        container.kill(signal="SIGKILL")
        container.wait(condition="not-running", timeout=timeout)
    attributes = verify_execution_container(container, identity)
    state = attributes.get("State") or {}
    if state.get("Running") is not False or state.get("Pid") != 0 or state.get("Status") not in ("exited", "dead"):
        raise ExecutionBoundaryError("Execution container termination is not confirmed")
