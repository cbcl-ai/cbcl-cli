"""Shared operation budgets retain unknown work across processes/restarts."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import pytest

from src.operations.host_capacity import (
    HostCapacity,
    HostCapacityUnavailable,
    load_host_capacity,
)


POLICY = {
    "enabled": True,
    "budgets": {
        "host-local": {"limit": 2, "per_office": 1},
        "provider": {"limit": 1, "per_office": 1},
    },
    "default_costs": {"host-local": 1},
    "resource_costs": {"external:provider": {"provider": 1}},
}


def test_wait_probe_is_read_only_and_never_reserves(tmp_path):
    path = tmp_path / "capacity.sqlite3"
    ledger = HostCapacity(path, POLICY)
    ledger.reserve("holder-a", "a", [])
    ledger.reserve("holder-b", "b", [])
    with pytest.raises(HostCapacityUnavailable):
        ledger.reserve("wait", "c", [])
    before = path.read_bytes()
    assert ledger.wait_eligibility("wait", "c") == {
        "state": "waiting",
        "eligible": False,
    }
    assert ledger.wait_eligibility("missing", "c") == {
        "state": "missing",
        "eligible": False,
    }
    assert path.read_bytes() == before
    ledger.release("holder-a", "a", cleanup_confirmed=True)
    before = path.read_bytes()
    assert ledger.wait_eligibility("wait", "c") == {
        "state": "waiting",
        "eligible": True,
    }
    assert path.read_bytes() == before
    assert ledger.wait_eligibility("holder-a", "a")["state"] == "released"
    assert ledger.wait_eligibility("holder-b", "b") == {
        "state": "reserved",
        "eligible": False,
    }
    assert ledger.reserve("wait", "c", [])["state"] == "reserved"


def test_durable_wait_renewal_retains_identity_without_acquiring_or_releasing(
    tmp_path, monkeypatch
):
    now = 100.0
    monkeypatch.setattr("src.operations.host_capacity.time.time", lambda: now)
    ledger = HostCapacity(tmp_path / "capacity.sqlite3", POLICY)
    ledger.reserve("holder-a", "a", [])
    ledger.reserve("holder-b", "b", [])
    with pytest.raises(HostCapacityUnavailable):
        ledger.reserve("old", "c", [])
    now += 301
    with pytest.raises(HostCapacityUnavailable):
        ledger.reserve("fresh", "d", [])
    assert ledger.wait_eligibility("old", "c")["eligible"] is False
    assert ledger.renew_wait("old", "c") is True
    assert ledger.renew_wait("missing", "c") is False
    assert ledger.renew_wait("holder-a", "a") is False
    assert ledger.status()["counts"] == {"reserved": 2, "waiting": 2}
    ledger.release("holder-a", "a", cleanup_confirmed=True)
    assert ledger.wait_eligibility("fresh", "d")["eligible"] is True
    assert ledger.wait_eligibility("old", "c")["eligible"] is False
    assert ledger.reserve("fresh", "d", [])["state"] == "reserved"
    ledger.release("holder-b", "b", cleanup_confirmed=True)
    assert ledger.wait_eligibility("old", "c")["eligible"] is True
    assert ledger.renew_wait("holder-b", "b") is False


def test_wait_probe_and_renewal_reject_wrong_owner_and_changed_policy(tmp_path):
    path = tmp_path / "capacity.sqlite3"
    ledger = HostCapacity(path, POLICY)
    ledger.reserve("holder", "a", [])
    with pytest.raises(HostCapacityUnavailable):
        ledger.reserve("wait", "a", [])
    for method in (ledger.wait_eligibility, ledger.renew_wait):
        with pytest.raises(ValueError, match="identity"):
            method("wait", "other-office")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE capacity_policy SET policy='{}'")
    for method in (ledger.wait_eligibility, ledger.renew_wait):
        with pytest.raises(ValueError, match="policy changed"):
            method("wait", "a")


def test_ledger_rejects_symbolic_parent_before_creating_directories(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        HostCapacity(alias / "created" / "capacity.sqlite3", POLICY)
    assert list(target.iterdir()) == []


def test_budget_spans_offices_and_preserves_waiter_fairness(tmp_path):
    path = tmp_path / "capacity.sqlite3"
    first = HostCapacity(path, POLICY)
    second = HostCapacity(path, POLICY)
    first.reserve("a1", "a", None)
    second.reserve("b1", "b", None)
    with pytest.raises(HostCapacityUnavailable):
        first.reserve("c1", "c", None)
    first.release("a1", "a", cleanup_confirmed=True)
    with pytest.raises(HostCapacityUnavailable):
        second.reserve("d1", "d", None)
    first.reserve("c1", "c", None)
    assert first.status()["counts"]["reserved"] == 2


def test_concurrent_reservations_cannot_overbook(tmp_path):
    path = tmp_path / "capacity.sqlite3"
    ledgers = [HostCapacity(path, POLICY) for _ in range(8)]

    def attempt(index):
        try:
            ledgers[index].reserve(str(index), str(index), None)
            return True
        except HostCapacityUnavailable:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(8))) == 2


def test_restart_retains_unknown_resources_and_release_requires_cleanup(tmp_path):
    path = tmp_path / "capacity.sqlite3"
    ledger = HostCapacity(path, POLICY)
    ledger.reserve("a1", "a", None)
    reopened = HostCapacity(path, POLICY)
    assert reopened.reserve("a1", "a", None)["state"] == "reserved"
    with pytest.raises(ValueError, match="cleanup"):
        reopened.release("a1", "a", cleanup_confirmed=False)
    with pytest.raises(HostCapacityUnavailable):
        reopened.reserve("a2", "a", None)
    reopened.abandon_wait("a1", "a")
    assert reopened.status()["counts"]["reserved"] == 1
    with pytest.raises(ValueError, match="identity"):
        reopened.reserve("a1", "b", None)
    reopened.release("a1", "a", cleanup_confirmed=True)
    with pytest.raises(ValueError, match="relaunched"):
        reopened.reserve("a1", "a", None)


def test_worker_resource_mapping_cannot_waive_local_baseline(tmp_path):
    ledger = HostCapacity(tmp_path / "capacity.sqlite3", POLICY)
    ledger.reserve("a1", "a", None)
    ledger.reserve("b1", "b", None)
    with pytest.raises(HostCapacityUnavailable):
        ledger.reserve("external", "c", ["external:provider"])
    ledger.release("a1", "a", cleanup_confirmed=True)
    assert ledger.reserve("external", "c", ["external:provider"])["costs"] == {
        "provider": 1,
        "host-local": 1,
    }


def test_config_defaults_off_but_cannot_disable_retained_capacity(tmp_path):
    config = tmp_path / "config.yaml"
    path = tmp_path / "capacity.sqlite3"
    assert load_host_capacity(config, path) is None
    ledger = HostCapacity(path, POLICY)
    ledger.reserve("a1", "a", None)
    with pytest.raises(ValueError, match="prevents disabling"):
        load_host_capacity(config, path)
    changed = {**POLICY, "default_costs": {"provider": 1}}
    with pytest.raises(ValueError, match="Settle"):
        HostCapacity(path, changed)


@pytest.mark.parametrize("explicit_disable", [False, True])
def test_disabling_waiting_only_policy_requires_settling_intent_before_transition(
    tmp_path, monkeypatch, explicit_disable
):
    now = 100.0
    monkeypatch.setattr("src.operations.host_capacity.time.time", lambda: now)
    config = tmp_path / "config.yaml"
    if explicit_disable:
        config.write_text("host_capacity:\n  enabled: false\n")
    path = tmp_path / "capacity.sqlite3"
    ledger = HostCapacity(path, POLICY)
    ledger.reserve("holder", "a", [])
    with pytest.raises(HostCapacityUnavailable):
        ledger.reserve("queued", "a", [])
    ledger.release("holder", "a", cleanup_confirmed=True)
    # No process holds capacity, but bypassing this queued intent would strand
    # its tombstone and prevent a later policy change after completion.
    now += 3600
    before = path.read_bytes()
    with pytest.raises(ValueError, match="prevents disabling"):
        load_host_capacity(config, path)
    assert path.read_bytes() == before
    assert ledger.wait_eligibility("queued", "a")["state"] == "waiting"
    ledger.abandon_wait("queued", "a")
    assert load_host_capacity(config, path) is None
    changed = {
        **POLICY,
        "budgets": {**POLICY["budgets"], "host-local": {"limit": 3, "per_office": 1}},
    }
    assert HostCapacity(path, changed).policy == changed


def test_office_at_own_limit_does_not_head_of_line_block_other_offices(tmp_path):
    ledger = HostCapacity(tmp_path / "capacity.sqlite3", POLICY)
    ledger.reserve("a1", "a", None)
    with pytest.raises(HostCapacityUnavailable):
        ledger.reserve("a2", "a", None)
    assert ledger.reserve("b1", "b", None)["state"] == "reserved"


def test_provider_blocked_waiter_does_not_idle_available_local_capacity(tmp_path):
    ledger = HostCapacity(tmp_path / "capacity.sqlite3", POLICY)
    ledger.reserve("remote-a", "a", ["external:provider"])
    with pytest.raises(HostCapacityUnavailable):
        ledger.reserve("remote-b", "b", ["external:provider"])
    assert ledger.reserve("local-c", "c", None)["state"] == "reserved"
    ledger.release("remote-a", "a", cleanup_confirmed=True)
    # Once the earlier waiter is eligible, its priority is still preserved.
    with pytest.raises(HostCapacityUnavailable):
        ledger.reserve("local-d", "d", None)
    assert ledger.reserve("remote-b", "b", ["external:provider"])["state"] == "reserved"


def test_reserved_recovery_paging_is_office_scoped_and_stable_after_release(tmp_path):
    policy = {**POLICY, "budgets": {"host-local": {"limit": 8, "per_office": 5}}}
    policy["resource_costs"] = {}
    ledger = HostCapacity(tmp_path / "capacity.sqlite3", policy)
    for operation, office in (("z", "a"), ("b", "a"), ("a", "a"), ("secret", "other")):
        ledger.reserve(operation, office, [])
    assert ledger.list_reserved_operations("a", limit=2) == ["a", "b"]
    ledger.release("a", "a", cleanup_confirmed=True)
    assert ledger.list_reserved_operations("a", after_id="b", limit=2) == ["z"]
    assert ledger.list_reserved_operations("unknown") == []
    with pytest.raises(ValueError, match="Bounded"):
        ledger.list_reserved_operations("a", limit=101)


def test_expired_never_started_waiter_loses_priority_but_can_reenter(
    tmp_path, monkeypatch
):
    now = 100.0
    monkeypatch.setattr("src.operations.host_capacity.time.time", lambda: now)
    policy = {
        **POLICY,
        "budgets": {"host-local": {"limit": 1, "per_office": 1}},
        "resource_costs": {},
    }
    ledger = HostCapacity(tmp_path / "capacity.sqlite3", policy)
    ledger.reserve("held", "a", [])
    with pytest.raises(HostCapacityUnavailable):
        ledger.reserve("old-wait", "b", [])
    now += 301
    # Elapsed time never releases actual reserved ownership.
    with pytest.raises(HostCapacityUnavailable):
        ledger.reserve("fresh-wait", "c", [])
    assert ledger.list_reserved_operations("a") == ["held"]
    ledger.release("held", "a", cleanup_confirmed=True)
    # Renewing the old intent joins behind the fresh waiter, retaining its ID.
    with pytest.raises(HostCapacityUnavailable):
        ledger.reserve("old-wait", "b", [])
    ledger.reserve("fresh-wait", "c", [])
    ledger.release("fresh-wait", "c", cleanup_confirmed=True)
    assert ledger.reserve("old-wait", "b", [])["state"] == "reserved"
