"""The maintenance drain waits for the offices this cbcl serves, not for ghosts.

An office that was deleted, moved to another Company Token or parked used
to stay in the drain inventory forever (its last snapshot was never
removed), so a global ``cbcl maintenance enable --wait`` could never be
confirmed again. Offices that still own work keep blocking the drain.
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import AsyncMock, MagicMock

from click.testing import CliRunner
import pytest

from src import cli_commands, daemon
from src.cli_commands import cli
from src.config import Config, OfficeConfig
from src.operations.maintenance import annotate, read_annotation
from src.runtime_state import AdmissionPaused, RuntimeState


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "runtime" / "control.sqlite3"
    monkeypatch.setattr("src.paths.get_runtime_state_path", lambda: path)
    return path


def _serve(database, office_id: str) -> RuntimeState:
    """What the daemon does when it brings an office up."""
    state = RuntimeState(database, office_id)
    state.mark_served()
    state.snapshot(0, 0)
    return state


def _control(database) -> RuntimeState:
    return RuntimeState(database, "*")


def _annotations(database):
    return database.with_name("maintenance-annotations.sqlite3")


# ── the inventory ───────────────────────────────────────────────────


def test_inventory_lists_served_offices_not_old_snapshots(database):
    _serve(database, "live")
    _serve(database, "gone")
    control = _control(database)
    assert control.known_office_ids() == {"live", "gone"}

    control.forget_office("gone")

    assert control.known_office_ids() == {"live"}
    assert control.served_office_ids() == {"live"}


def test_served_record_survives_a_daemon_stop(database):
    """A stopped cbcl still owns its offices: nothing may drop them."""
    _serve(database, "live")
    assert RuntimeState(database, "*").known_office_ids() == {"live"}
    assert RuntimeState(database, "live").maintenance_status()["served"] is True


def test_upgrade_keeps_every_office_the_old_version_reported(database):
    """cbcl 0.5.36 had no served-office table: its snapshots seed it once."""
    old = RuntimeState(database, "reported-before-upgrade")
    old.snapshot(0, 0)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE served_offices")

    upgraded = RuntimeState(database, "*")

    assert upgraded.known_office_ids() == {"reported-before-upgrade"}
    # Only the first run seeds: a forgotten office does not come back.
    upgraded.forget_office("reported-before-upgrade")
    RuntimeState(database, "reported-before-upgrade").snapshot(0, 0)
    assert RuntimeState(database, "*").known_office_ids() == set()


def test_forgetting_removes_observations_and_keeps_records_of_work(database):
    gone = _serve(database, "gone")
    reservation = gone.reserve("worker", "task-1")
    gone.set_maintenance(True)
    annotate(_annotations(database), scope="gone", owner="ops", reason="moving", duration=600)
    control = _control(database)
    control.set_maintenance(True)

    control.forget_office("gone")

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT scope FROM maintenance").fetchall() == [("*",)]
        for table in ("served_offices", "runtime_snapshots", "snapshot_owners"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert read_annotation(_annotations(database), "gone") is None
    assert gone.owns_reservation(reservation), "an admission is a record of work"


def test_the_global_scope_is_not_an_office(database):
    control = _control(database)
    control.set_maintenance(True)
    with pytest.raises(ValueError):
        control.forget_office("*")
    with pytest.raises(ValueError):
        control.mark_served()
    assert not RuntimeState(database, "any").admission_open()


def test_office_that_still_owns_work_keeps_blocking_the_drain(database):
    gone = _serve(database, "gone")
    gone.reserve("script", "task-1")
    control = _control(database)
    control.set_maintenance(True)

    control.forget_office("gone")

    assert control.known_office_ids() == {"gone"}
    status = RuntimeState(database, "gone").maintenance_status()
    assert status["served"] is False
    assert status["state"] == "unknown"
    assert status["pending_admissions"] == 1


def test_pause_of_an_office_that_left_does_not_stall_it_when_it_returns(database):
    office = _serve(database, "moved")
    office.set_maintenance(True)
    with pytest.raises(AdmissionPaused):
        office.reserve("worker")

    _control(database).forget_office("moved")

    returned = _serve(database, "moved")
    assert returned.admission_open()
    returned.release(returned.reserve("worker"))


def test_missing_annotation_store_does_not_stop_forgetting(database):
    _serve(database, "gone")
    assert not _annotations(database).exists()
    _control(database).forget_office("gone")
    assert _control(database).known_office_ids() == set()


# ── the documented drain ────────────────────────────────────────────


def test_global_drain_is_confirmed_when_a_torn_down_office_exists(database, monkeypatch):
    live = _serve(database, "live")
    _serve(database, "gone")
    _control(database).forget_office("gone")
    # The live office's daemon acknowledges the pause on its next heartbeat.
    monkeypatch.setattr(cli_commands.time, "sleep", lambda _seconds: live.snapshot(0, 0))

    result = CliRunner().invoke(cli, ["maintenance", "enable", "--wait", "5"])

    assert result.exit_code == 0, result.output
    assert "Drain not confirmed" not in result.output
    assert not live.admission_open()


def test_global_drain_is_not_confirmed_by_an_unserved_office_with_work(database, monkeypatch):
    live = _serve(database, "live")
    gone = _serve(database, "gone")
    gone.reserve("worker", "task-1")
    _control(database).forget_office("gone")
    monkeypatch.setattr(cli_commands.time, "sleep", lambda _seconds: live.snapshot(0, 0))

    result = CliRunner().invoke(cli, ["maintenance", "enable", "--wait", "0.05"])

    assert result.exit_code == 1
    assert "Drain not confirmed" in result.output
    status = CliRunner().invoke(cli, ["maintenance", "status"])
    states = {state["office_id"]: state for state in json.loads(status.output)}
    assert set(states) == {"live", "gone"}
    assert states["gone"]["served"] is False
    assert states["gone"]["state"] == "unknown"
    assert states["live"]["served"] is True


def test_drain_without_any_office_is_refused_with_the_reason(database):
    """An empty inventory is also what the wrong account's home looks like."""
    result = CliRunner().invoke(cli, ["maintenance", "enable", "--wait", "0.01"])
    assert result.exit_code == 1
    assert "Drain not confirmed: no office is served" in result.output
    assert not RuntimeState(database, "any").admission_open()


# ── the daemon keeps the record in line with discovery ──────────────


def test_retire_forgets_only_offices_outside_the_snapshot(database):
    for office_id in ("kept", "connecting", "gone"):
        _serve(database, office_id)
    control = _control(database)

    daemon._retire_unserved_offices(control, {"kept", "connecting"})

    assert control.served_office_ids() == {"kept", "connecting"}


def test_retire_without_a_control_database_does_nothing():
    daemon._retire_unserved_offices(None, set())


def test_retire_never_raises_into_the_poll_loop(database, caplog):
    control = _control(database)
    control.served_office_ids = MagicMock(side_effect=sqlite3.OperationalError("locked"))
    daemon._retire_unserved_offices(control, set())
    assert "served-office record" in caplog.text


def test_unopenable_control_database_does_not_stop_the_daemon(tmp_path, caplog):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("file where the runtime directory should be")
    assert daemon._open_control_state(blocked / "control.sqlite3") is None
    assert "Runtime control database unavailable" in caplog.text
    assert daemon._open_control_state(None) is None


class _PollWindow:
    """Give the real poll loop a finite number of ticks without waiting."""

    def __init__(self, ticks: int) -> None:
        self.ticks = ticks

    def is_set(self) -> bool:
        return False

    async def wait(self) -> None:
        if self.ticks:
            self.ticks -= 1
            raise TimeoutError


async def _poll_once(monkeypatch, control, discover, *, connected=None, connecting=None):
    monkeypatch.setattr(daemon, "_discover_offices", discover)
    monkeypatch.setattr(daemon, "_spawn_office_connect", MagicMock())

    async def _disconnect(office_id, current, *_args, **_kwargs):
        current.pop(office_id)

    monkeypatch.setattr(daemon, "_disconnect_office_process_model", _disconnect)
    await daemon._poll_for_new_offices_process_model(
        Config(platform_url="https://platform.example", security_token="synthetic"),
        None, None,
        {} if connected is None else connected,
        set() if connecting is None else connecting,
        [], _PollWindow(1), control=control,
    )


async def test_poll_retires_an_office_missing_from_discovery(database, monkeypatch):
    for office_id in ("kept", "moved-away", "mid-connect", "pushed-ahead"):
        _serve(database, office_id)
    control = _control(database)
    connected = {"moved-away": object()}

    async def _discover(*_args):
        # A create push connects this office while the snapshot is in flight.
        connected["pushed-ahead"] = object()
        return [OfficeConfig(id="kept", name="Kept")]

    await _poll_once(
        monkeypatch, control, _discover,
        connected=connected, connecting={"mid-connect"},
    )

    assert set(connected) == {"pushed-ahead"}
    assert control.served_office_ids() == {"kept", "mid-connect", "pushed-ahead"}


async def test_poll_without_an_answer_keeps_every_office(database, monkeypatch):
    _serve(database, "kept")
    control = _control(database)

    await _poll_once(monkeypatch, control, AsyncMock(return_value=None))

    assert control.served_office_ids() == {"kept"}


@pytest.mark.parametrize(
    ("discovered", "expected"),
    [([], set()), (None, {"removed-while-stopped"})],
    ids=["authoritative-empty-list", "discovery-unavailable"],
)
async def test_startup_retires_offices_removed_while_cbcl_was_stopped(
    database, monkeypatch, discovered, expected,
):
    """Covers the office that was deleted while the daemon was not running."""
    import asyncio

    import redis.asyncio as aioredis

    _serve(database, "removed-while-stopped")
    monkeypatch.setattr(aioredis, "from_url", lambda *args, **kwargs: AsyncMock())
    containers = MagicMock(
        ensure_image=AsyncMock(), stop_all=AsyncMock(), health_check_all=AsyncMock(),
    )
    monkeypatch.setattr(daemon, "ContainerManager", lambda **kwargs: containers)
    monkeypatch.setattr(daemon, "_discover_offices", AsyncMock(return_value=discovered))
    monkeypatch.setattr(daemon, "get_pid_path", MagicMock())
    monkeypatch.setattr(
        "src.docker.container_manager.stop_and_remove_managed_containers", lambda: 0,
    )
    stop: list = []
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda _signal, handler: stop.append(handler))

    task = asyncio.create_task(
        daemon._run_process_model(MagicMock(), runtime_state_path=database),
    )
    async with asyncio.timeout(10):
        while not stop:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        stop[0]()
        await task

    assert _control(database).served_office_ids() == expected
