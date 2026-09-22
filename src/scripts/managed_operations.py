"""Optional operations on the managed script lane, independent of any provider.

An external adapter writes a bounded operation-result.json to its run directory.
The recorded context is supplied to a separate reconciliation entry point after
observer loss. A wrapper's zero exit is not proof that an external job succeeded.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import stat
import uuid
from datetime import datetime, timezone

from src.operation_state import OperationConflict, operation_result, operation_spec
from src.scripts.manifest import load_manifest
from src.scripts.operation_fingerprint import fingerprint as _fingerprint


def capacity(runner):
    from src.operations.host_capacity import load_host_capacity
    from src.paths import get_config_path

    if not hasattr(runner, "_operation_host_capacity"):
        runner._operation_host_capacity = load_host_capacity(
            get_config_path(), runner._runtime_state.database_path.with_name("host-capacity.sqlite3"),
        )
    return runner._operation_host_capacity


def release_capacity(runner, record: dict) -> None:
    budget = capacity(runner)
    if budget and record["cleanup_confirmed"] and (
        record["state"] in {"succeeded", "failed", "cancelled"}
        or (record["mechanism"] == "local" and record["state"] == "unknown")
    ):
        budget.release(record["operation_id"], runner._runtime_state.office_id, cleanup_confirmed=True)


def begin(runner, script_name: str, spec: dict, task: dict, caller: dict,
          variable_overrides: dict | None) -> tuple[dict, bool]:
    if runner._runtime_state is None or not caller or caller.get("role") != "worker":
        raise ValueError("Tracked operations require a current task worker/reviewer and durable runtime")
    validated = operation_spec(spec)
    script_dir = runner._workspace / ".scripts" / script_name
    manifest = load_manifest(script_dir)
    from src.agent_execution_policy import execution_resources

    resources = sorted(set(execution_resources(task)) | set(validated["resources"]) | {f"script:{script_name}"})
    return runner._runtime_state.begin_operation(
        task_id=caller["task_id"], cycle=caller["execution_cycle"], phase=caller["task_mode"],
        key=validated["key"], fingerprint=_fingerprint(script_dir, validated, variable_overrides),
        input_fingerprint=validated["input_fingerprint"],
        script_name=script_name, attempt_id=caller["attempt_id"], mechanism=manifest.operation_mode,
        resources=resources, stage=validated["stage"],
    )


def prepare_context(runner, record: dict, exec_dir: Path, action: str) -> dict:
    context_path = exec_dir / "operation-context.json"
    result_path = exec_dir / "operation-result.json"
    context = {key: record.get(key) for key in (
        "operation_id", "task_id", "cycle", "phase", "operation_key", "fingerprint", "external_ref",
        "input_fingerprint", "stage",
    )}
    context["action"] = action
    context_path.write_text(json.dumps(context))
    from src._chown import chown_to_agent

    chown_to_agent(context_path)
    convert = runner._to_container_path if runner._use_docker() else str
    return {
        "CUBICLE_OPERATION_ID": record["operation_id"],
        "CUBICLE_OPERATION_CONTEXT": convert(context_path),
        "CUBICLE_OPERATION_RESULT": convert(result_path),
    }


def prepare_launch(runner, record: dict, exec_dir: Path, action: str, caller: dict | None,
                   variable_overrides: dict | None) -> dict:
    record = runner._runtime_state.update_operation(
        record["operation_id"], execution_id=exec_dir.name, state="preparing", cleanup_confirmed=False,
        observer_attempt_id=(caller or {}).get("attempt_id"),
        observer_fingerprint=_fingerprint(exec_dir.parent.parent, {
            "input_fingerprint": record["input_fingerprint"], "action": action,
        }, variable_overrides),
    )
    return prepare_context(runner, record, exec_dir, action)


def attach_completion_observer(runner, execution) -> None:
    runner._runtime_state.update_operation(
        execution.operation_id, execution_id=execution.exec_id, state="running", cleanup_confirmed=False,
    )
    wait = runner._runtime_state.capacity_wait(execution.task_id)
    if wait and wait["operation_id"] == execution.operation_id:
        runner._runtime_state.retire_capacity_wait(wait["wait_id"])

    async def persist(state: str, exit_code: int | None) -> None:
        record = settle(
            runner, execution.operation_id, execution.exec_id,
            state="cancelled" if execution.operation_cancel_requested else state, exit_code=exit_code,
            cleanup_confirmed=bool(execution.resource_lease and execution.resource_lease.record["state"] in {"stopped", "released"}),
        )
        await publish(runner, record)

    execution.operation_observer = persist


def read_receipt(exec_dir: Path) -> dict:
    path = exec_dir / "operation-result.json"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    with os.fdopen(descriptor, "rb") as receipt:
        info = os.fstat(receipt.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024:
            raise ValueError("Operation adapter receipt must be a bounded regular file")
        data = receipt.read(16 * 1024 + 1)
        if len(data) > 16 * 1024:
            raise ValueError("Operation adapter receipt must be a bounded regular file")
    return operation_result(json.loads(data))


def settle(runner, operation_id: str, execution_id: str, *, state: str,
           exit_code: int | None, cleanup_confirmed: bool) -> dict:
    record = runner._runtime_state.get_operation(operation_id)
    if record is None or record["execution_id"] != execution_id:
        raise OperationConflict("A stale observer cannot complete the current operation")
    exec_dir = runner._workspace / ".scripts" / record["script_name"] / "executions" / execution_id
    try:
        receipt = read_receipt(exec_dir)
    except (ValueError, OSError):
        receipt = {}
        state = "unknown"
    if record["mechanism"] == "external":
        # Keep external identity even when the observer fails; cancellation of
        # the local observer makes no assertion about a remote side effect.
        reported = receipt.get("state", "unknown")
        identity = receipt.get("external_ref") or record.get("external_ref")
        if (record.get("external_ref") and receipt.get("external_ref")
                and record["external_ref"] != receipt["external_ref"]):
            # A corrupt/changed adapter must not complete a different job or
            # make inspection unusable. Preserve the original remote identity.
            receipt.pop("external_ref")
            reported = "unknown"
        state = reported if identity and exit_code == 0 and reported in {"succeeded", "failed", "cancelled"} else "unknown"
    elif state == "completed":
        state = "succeeded" if exit_code == 0 else "unknown"
    elif state not in {"failed", "cancelled", "unknown"}:
        state = "failed"
    receipt["artifact_refs"] = [
        str((exec_dir / "log.txt").relative_to(runner._workspace)),
        *receipt.get("artifact_refs", []),
    ][:32]
    record = runner._runtime_state.update_operation(
        operation_id, state=state, cleanup_confirmed=cleanup_confirmed,
        exit_code=exit_code, receipt=receipt,
    )
    release_capacity(runner, record)
    return record


async def publish(runner, record: dict) -> None:
    """Existing task-activity channel; replay ID is stable for this snapshot."""
    if runner._router is None:
        return
    occurred = datetime.fromtimestamp(record["updated_at"], timezone.utc).isoformat()
    event = {
        "version": 1,
        "event_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"operation:{record['operation_id']}:{record['updated_at']}")),
        "kind": "operation", "operation_id": record["operation_id"],
        "input_fingerprint": record["input_fingerprint"],
        "mechanism_fingerprint": record.get("observer_fingerprint") or record["fingerprint"],
        "origin_phase": record["phase"],
        "execution_cycle": record["cycle"], "execution_attempt_id": record["observer_attempt_id"],
        "origin_attempt_id": record["origin_attempt_id"],
        "phase": "external_wait" if record["mechanism"] == "external" else record["stage"],
        "state": "queued" if record["state"] == "preparing" else record["state"],
        "mechanism": "script", "occurred_at": occurred,
        "started_at": datetime.fromtimestamp(record["created_at"], timezone.utc).isoformat(),
        "artifact_refs": record["artifact_refs"] or [],
        "cleanup_confirmed": record["cleanup_confirmed"],
    }
    if record.get("external_ref"):
        event["external_ref"] = record["external_ref"]
    try:
        await runner._router.publish_event({
            "type": "task_activity", "task_id": record["task_id"],
            "event_type": "execution_progress", "actor": "script-runner",
            "content": f"Managed operation {record['operation_key']}: {record['state']}",
            "details": {"execution_event": event},
        })
    except Exception:
        logging.getLogger(__name__).warning("Operation telemetry delivery deferred; durable receipt remains available", exc_info=True)
