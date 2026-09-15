"""Retained containers cannot disappear behind idle telemetry or a mode change."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.docker.execution_ledger import ExecutionBudget, ExecutionLedger, ExecutionResources
from src.execution_policy import configure_worker_execution
from src.runtime_state import RuntimeState


@pytest.fixture
def inventory(tmp_path, monkeypatch):
    runtime_path = tmp_path / "private" / "runtime.sqlite3"
    office_id = str(uuid4())
    runtime = RuntimeState(runtime_path, office_id)
    ledger = ExecutionLedger(runtime_path.with_name("execution-containers.sqlite"))
    row, _created = ledger.reserve(
        office_id=office_id, task_id=str(uuid4()), attempt_id=str(uuid4()),
        fingerprint="synthetic", office_container_id="a" * 64, image_id="sha256:" + "b" * 64,
        resources=ExecutionResources(1000, 1024, 64), budget=ExecutionBudget(2, 2000, 2048, 128),
    )
    monkeypatch.setattr("src.paths.get_runtime_state_path", lambda: runtime_path)
    monkeypatch.setattr("src.paths.get_config_path", lambda: tmp_path / "absent.yaml")
    return runtime, ledger, row


def test_zero_host_workers_cannot_hide_reserved_or_running_container(inventory):
    runtime, ledger, row = inventory
    runtime.set_maintenance(True)
    runtime.snapshot(0, 0)
    assert runtime.maintenance_status()["state"] == "reconciliation_required"
    ledger.bind_container(row["attempt_id"], row["reservation_id"], "c" * 64)
    ledger.transition(row["attempt_id"], "c" * 64, "running")
    status = runtime.maintenance_status()
    assert status["state"] == "reconciliation_required"
    assert len(status["isolated_executions"]) == 1
    assert status["isolated_inventory_confirmed"]
    runtime.snapshot(1, 0)
    assert runtime.maintenance_status()["state"] == "draining"
    ledger.transition(row["attempt_id"], "c" * 64, "stopped")
    runtime.snapshot(0, 0)
    assert runtime.maintenance_status()["state"] == "drained"


def test_office_discovery_includes_launch_before_first_snapshot(inventory):
    runtime, _ledger, row = inventory
    assert row["office_id"] in runtime.known_office_ids()


async def test_disabling_mode_refuses_retained_attempt_but_allows_verified_drain(inventory):
    runtime, ledger, row = inventory
    office = SimpleNamespace(id=runtime.office_id)
    with pytest.raises(ValueError, match="prevent disabling containment"):
        await configure_worker_execution(office, "unused")
    ledger.bind_container(row["attempt_id"], row["reservation_id"], "c" * 64)
    ledger.transition(row["attempt_id"], "c" * 64, "stopped")
    assert await configure_worker_execution(office, "unused") is None


async def test_corrupt_ledger_never_means_drained_or_default_fallback(inventory):
    runtime, ledger, _row = inventory
    runtime.set_maintenance(True)
    runtime.snapshot(0, 0)
    ledger.database_path.write_bytes(b"synthetic invalid database")
    report = runtime.maintenance_status()
    assert report["state"] == "unknown"
    assert report["isolated_inventory_confirmed"] is False
    with pytest.raises(Exception):
        await configure_worker_execution(SimpleNamespace(id=runtime.office_id), "unused")
