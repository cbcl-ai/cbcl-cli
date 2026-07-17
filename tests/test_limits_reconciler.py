"""ResourceLimitReconciler — sync-driven recreate-when-idle decision logic.

Covers the per-office container resource-limit reconciliation
(``src.docker.limits_reconciler``): drift detection against the
ConfigStore's applied snapshot, the busy/idle decision (defer while
agents work / the Manager is mid-turn / scripts run; recreate when
idle), the health-tick recheck of a deferred change, and failure
retry semantics.
"""

from __future__ import annotations

import logging

import pytest

from src import config as config_mod
from src.config import OfficeConfig, OfficeResourceLimits
from src.config_sync.sync_service import ConfigStore
from src.docker.limits_reconciler import ResourceLimitReconciler


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    """Isolate the host-global limits chain: no config.yaml, no env —
    the host chain resolves to the built-in defaults (4.0 / '8g')."""
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config_mod, "get_config_path", lambda: path)
    monkeypatch.delenv("CBCL_OFFICE_CPUS", raising=False)
    monkeypatch.delenv("CBCL_OFFICE_MEMORY", raising=False)
    return path


class FakeContainers:
    def __init__(self) -> None:
        self.recreates: list[str] = []
        self.fail = False

    async def recreate_office(self, office: OfficeConfig) -> str:
        if self.fail:
            raise RuntimeError("docker down")
        self.recreates.append(office.id)
        return "new-cid"


class FakeSupervisor:
    def __init__(self, statuses: dict | None = None) -> None:
        self.statuses = statuses or {}

    def get_all_statuses(self) -> dict:
        return self.statuses


class FakeManager:
    def __init__(self, busy: bool = False) -> None:
        self.is_busy = busy


class FakeScriptRunner:
    def __init__(self, running: list | None = None) -> None:
        self.running = running or []
        self.raise_on_check = False

    async def get_running_scripts(self) -> list:
        if self.raise_on_check:
            raise RuntimeError("boom")
        return self.running


def _idle_statuses() -> dict:
    return {
        "analyst": {"status": "idle", "current_task": None},
        "manager": {"status": "ready", "current_task": None},
    }


def _busy_statuses() -> dict:
    statuses = _idle_statuses()
    statuses["python-developer"] = {
        "status": "working", "current_task": "t-1",
    }
    return statuses


@pytest.fixture
def parts(config_path):
    """Default assembly: applied snapshot = host defaults (4.0/'8g'),
    idle supervisor, idle manager, no scripts."""
    office = OfficeConfig(id="oid-1", name="Test Office")
    store = ConfigStore()
    store.mark_resource_limits_applied(
        OfficeResourceLimits(cpus=4.0, memory="8g")
    )
    containers = FakeContainers()
    supervisor = FakeSupervisor(_idle_statuses())
    manager = FakeManager(busy=False)
    runner = FakeScriptRunner()
    reconciler = ResourceLimitReconciler(
        containers=containers,
        office=office,
        config_store=store,
        supervisor=supervisor,
        script_runner=runner,
        manager=manager,
    )
    return office, store, containers, supervisor, manager, runner, reconciler


class TestNoDrift:
    @pytest.mark.asyncio
    async def test_same_limits_is_in_sync(self, parts):
        _, _, containers, _, _, _, reconciler = parts
        outcome = await reconciler.on_sync_config({})
        assert outcome == "in_sync"
        assert containers.recreates == []
        assert reconciler.pending is False

    @pytest.mark.asyncio
    async def test_no_applied_baseline_is_in_sync(self, config_path):
        """Before bring-up stamps the applied snapshot there is
        nothing to reconcile against — never recreate blind."""
        office = OfficeConfig(id="oid-1", name="Test Office")
        store = ConfigStore()  # no mark_resource_limits_applied
        containers = FakeContainers()
        reconciler = ResourceLimitReconciler(
            containers=containers, office=office, config_store=store,
        )
        outcome = await reconciler.on_sync_config(
            {"container_cpus": 8, "container_memory": "16g"}
        )
        assert outcome == "in_sync"
        assert containers.recreates == []

    @pytest.mark.asyncio
    async def test_recheck_without_pending_is_noop(self, parts):
        _, _, containers, _, _, _, reconciler = parts
        assert await reconciler.recheck_pending() == "in_sync"
        assert containers.recreates == []


class TestRecreateWhenIdle:
    @pytest.mark.asyncio
    async def test_drift_and_idle_recreates(self, parts, caplog):
        office, store, containers, _, _, _, reconciler = parts
        with caplog.at_level(
            logging.INFO, logger="src.docker.limits_reconciler",
        ):
            outcome = await reconciler.on_sync_config(
                {"container_cpus": 8, "container_memory": "16g"}
            )
        assert outcome == "recreated"
        assert containers.recreates == ["oid-1"]
        # Desired values landed on the office dataclass…
        assert office.container_cpus == 8.0
        assert office.container_memory == "16g"
        # …and the applied snapshot advanced.
        assert store.resource_limits_applied == OfficeResourceLimits(
            cpus=8.0, memory="16g",
        )
        assert reconciler.pending is False
        assert "recreating" in caplog.text

    @pytest.mark.asyncio
    async def test_clearing_override_recreates_back_to_defaults(
        self, config_path,
    ):
        """A sync_config WITHOUT the override (null / cleared in the
        UI) drifts an overridden container back to the host chain."""
        office = OfficeConfig(
            id="oid-1", name="Test Office",
            container_cpus=8.0, container_memory="16g",
        )
        store = ConfigStore()
        store.mark_resource_limits_applied(
            OfficeResourceLimits(cpus=8.0, memory="16g")
        )
        containers = FakeContainers()
        reconciler = ResourceLimitReconciler(
            containers=containers, office=office, config_store=store,
            supervisor=FakeSupervisor(_idle_statuses()),
        )
        outcome = await reconciler.on_sync_config({"container_cpus": None})
        assert outcome == "recreated"
        assert office.container_cpus is None
        assert office.container_memory is None
        assert store.resource_limits_applied == OfficeResourceLimits(
            cpus=4.0, memory="8g",
        )

    @pytest.mark.asyncio
    async def test_invalid_sync_values_treated_as_no_override(
        self, parts, caplog,
    ):
        """Garbage from the wire degrades to 'no override' → desired
        equals the applied defaults → no recreate."""
        _, _, containers, _, _, _, reconciler = parts
        with caplog.at_level(logging.WARNING, logger="src.config"):
            outcome = await reconciler.on_sync_config(
                {"container_cpus": "lots", "container_memory": "8gb"}
            )
        assert outcome == "in_sync"
        assert containers.recreates == []
        assert "office_cpus" in caplog.text
        assert "office_memory" in caplog.text


class TestDeferWhileBusy:
    @pytest.mark.asyncio
    async def test_working_agent_defers(self, parts, caplog):
        _, store, containers, supervisor, _, _, reconciler = parts
        supervisor.statuses = _busy_statuses()
        with caplog.at_level(
            logging.INFO, logger="src.docker.limits_reconciler",
        ):
            outcome = await reconciler.on_sync_config(
                {"container_cpus": 8}
            )
        assert outcome == "deferred"
        assert containers.recreates == []
        assert reconciler.pending is True
        # Applied snapshot untouched — drift persists until applied.
        assert store.resource_limits_applied == OfficeResourceLimits(
            cpus=4.0, memory="8g",
        )
        assert "deferring" in caplog.text

    @pytest.mark.asyncio
    async def test_spawning_agent_defers(self, parts):
        _, _, _, supervisor, _, _, reconciler = parts
        supervisor.statuses = {
            "analyst": {"status": "spawning", "current_task": None},
        }
        assert await reconciler.on_sync_config(
            {"container_cpus": 8}
        ) == "deferred"

    @pytest.mark.asyncio
    async def test_ready_agent_is_not_busy(self, parts):
        """READY = process alive, no in-flight docker exec — safe to
        recreate (the next exec lands in the fresh container)."""
        _, _, containers, supervisor, _, _, reconciler = parts
        supervisor.statuses = {
            "analyst": {"status": "ready", "current_task": None},
        }
        assert await reconciler.on_sync_config(
            {"container_cpus": 8}
        ) == "recreated"
        assert containers.recreates == ["oid-1"]

    @pytest.mark.asyncio
    async def test_manager_mid_turn_defers(self, parts):
        _, _, containers, _, manager, _, reconciler = parts
        manager.is_busy = True
        assert await reconciler.on_sync_config(
            {"container_cpus": 8}
        ) == "deferred"
        assert containers.recreates == []

    @pytest.mark.asyncio
    async def test_running_script_defers(self, parts):
        _, _, containers, _, _, runner, reconciler = parts
        runner.running = [{"script_name": "s", "execution_id": "e-1"}]
        assert await reconciler.on_sync_config(
            {"container_cpus": 8}
        ) == "deferred"
        assert containers.recreates == []

    @pytest.mark.asyncio
    async def test_unreadable_script_state_fails_busy(self, parts):
        """When in doubt, don't kill — an erroring script check
        counts as busy."""
        _, _, containers, _, _, runner, reconciler = parts
        runner.raise_on_check = True
        assert await reconciler.on_sync_config(
            {"container_cpus": 8}
        ) == "deferred"
        assert containers.recreates == []

    @pytest.mark.asyncio
    async def test_recheck_applies_once_idle(self, parts):
        """The deferred change lands via the health-tick recheck when
        the office goes idle."""
        office, store, containers, supervisor, _, _, reconciler = parts
        supervisor.statuses = _busy_statuses()
        assert await reconciler.on_sync_config(
            {"container_cpus": 8}
        ) == "deferred"

        # Still busy → still deferred.
        assert await reconciler.recheck_pending() == "deferred"
        assert containers.recreates == []

        # Office goes idle → the recheck recreates.
        supervisor.statuses = _idle_statuses()
        assert await reconciler.recheck_pending() == "recreated"
        assert containers.recreates == ["oid-1"]
        assert reconciler.pending is False
        assert store.resource_limits_applied.cpus == 8.0

    @pytest.mark.asyncio
    async def test_reverted_change_cancels_pending(self, parts, caplog):
        """User reverts the limits before the office goes idle —
        the pending recreate is cancelled, nothing recreated."""
        _, _, containers, supervisor, _, _, reconciler = parts
        supervisor.statuses = _busy_statuses()
        assert await reconciler.on_sync_config(
            {"container_cpus": 8}
        ) == "deferred"
        with caplog.at_level(
            logging.INFO, logger="src.docker.limits_reconciler",
        ):
            assert await reconciler.on_sync_config({}) == "in_sync"
        assert reconciler.pending is False
        supervisor.statuses = _idle_statuses()
        assert await reconciler.recheck_pending() == "in_sync"
        assert containers.recreates == []
        assert "back in sync" in caplog.text


class TestFailureRetry:
    @pytest.mark.asyncio
    async def test_recreate_failure_stays_pending_and_retries(
        self, parts, caplog,
    ):
        _, store, containers, _, _, _, reconciler = parts
        containers.fail = True
        with caplog.at_level(
            logging.ERROR, logger="src.docker.limits_reconciler",
        ):
            outcome = await reconciler.on_sync_config(
                {"container_cpus": 8}
            )
        assert outcome == "failed"
        assert reconciler.pending is True
        # Snapshot NOT advanced — the drift is still real.
        assert store.resource_limits_applied.cpus == 4.0
        assert "failed" in caplog.text

        # Docker recovers → the next health tick applies it.
        containers.fail = False
        assert await reconciler.recheck_pending() == "recreated"
        assert containers.recreates == ["oid-1"]
        assert store.resource_limits_applied.cpus == 8.0
