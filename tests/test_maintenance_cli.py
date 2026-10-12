"""Maintenance changes admission only; it never implies a safe restart."""

import asyncio
import json
from unittest.mock import MagicMock

from click.testing import CliRunner
import pytest

from src import cli_commands, daemon
from src.cli_commands import cli
from src.config import Config, OfficeConfig
from src.runtime_state import RuntimeState


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    database = tmp_path / "runtime.sqlite3"
    monkeypatch.setattr("src.paths.get_runtime_state_path", lambda: database)
    return RuntimeState(database, "office")


def test_cli_pause_and_resume_are_persistent_without_stopping_work(runtime):
    runner = CliRunner()
    paused = runner.invoke(cli, ["maintenance", "enable", "--office-id", "office"])
    assert paused.exit_code == 0
    assert "not restart authorization" in paused.output
    assert not RuntimeState(runtime.database_path, "office").admission_open()
    resumed = runner.invoke(cli, ["maintenance", "disable", "--office-id", "office"])
    assert resumed.exit_code == 0
    assert RuntimeState(runtime.database_path, "office").admission_open()


def test_cli_wait_timeout_keeps_admission_paused(runtime):
    result = CliRunner().invoke(cli, ["maintenance", "enable", "--office-id", "office", "--wait", "0.01"])
    assert result.exit_code == 1
    assert "maintenance remains enabled" in result.output
    assert not runtime.admission_open()


def test_cli_reports_retained_old_daemon_admission_explicitly(runtime):
    runtime.reserve("generation", "task")
    runtime.set_maintenance(True)
    RuntimeState(runtime.database_path, "office").snapshot(0, 0)
    result = CliRunner().invoke(cli, ["maintenance", "status", "--office-id", "office"])
    assert result.exit_code == 0
    report = json.loads(result.output)[0]
    assert report["state"] == "reconciliation_required"
    assert report["retained_admissions"][0]["task_id"] == "task"


# ── a pause left on must be visible, not a silent stall ─────────────


@pytest.fixture
def startable(monkeypatch):
    """`cbcl start` with every external step replaced."""
    monkeypatch.setattr(
        cli_commands, "load_config",
        lambda: Config(platform_url="https://platform.example", security_token=""),
    )
    monkeypatch.setattr(
        cli_commands, "fetch_offices_sync",
        lambda *args: [OfficeConfig(id="office", name="Office")],
    )
    monkeypatch.setattr(cli_commands, "_credential_preflight", lambda offices: None)
    monkeypatch.setattr(cli_commands, "_ufw_preflight", lambda: None)
    started = MagicMock()
    monkeypatch.setattr(cli_commands, "_start_daemon", started)
    return started


def test_start_says_that_a_stored_pause_keeps_the_offices_closed(runtime, startable):
    RuntimeState(runtime.database_path, "*").set_maintenance(True)
    result = CliRunner().invoke(cli, ["start", "--daemon"])
    assert result.exit_code == 0, result.output
    assert "Maintenance: ON" in result.output
    assert "all offices, since " in result.output
    assert "cbcl maintenance disable" in result.output
    startable.assert_called_once()


def test_start_is_quiet_about_maintenance_when_nothing_is_paused(runtime, startable):
    result = CliRunner().invoke(cli, ["start", "--daemon"])
    assert result.exit_code == 0, result.output
    assert "Maintenance" not in result.output
    startable.assert_called_once()


async def test_daemon_warns_at_start_and_keeps_reminding_while_paused(
    runtime, monkeypatch, caplog,
):
    runtime.set_maintenance(True)
    RuntimeState(runtime.database_path, "*").set_maintenance(True)
    monkeypatch.setattr(daemon, "MAINTENANCE_REMINDER_INTERVAL", 0.01)
    shutdown = asyncio.Event()
    caplog.set_level("WARNING", logger="cbcl")

    reminder = asyncio.create_task(
        daemon._remind_of_maintenance_pause(runtime.database_path, shutdown),
    )
    await asyncio.sleep(0.1)
    shutdown.set()
    await reminder

    messages = [
        record.getMessage() for record in caplog.records
        if "Maintenance pause is ON" in record.getMessage()
    ]
    assert len(messages) >= 4, "at start and again on every interval, for both pauses"
    assert any("all offices" in message and message.endswith("cbcl maintenance disable") for message in messages)
    assert any("cbcl maintenance disable --office-id office" in message for message in messages)


async def test_daemon_is_silent_while_admission_is_open(runtime, monkeypatch, caplog):
    runtime.set_maintenance(False)
    monkeypatch.setattr(daemon, "MAINTENANCE_REMINDER_INTERVAL", 0.01)
    shutdown = asyncio.Event()
    caplog.set_level("WARNING", logger="cbcl")

    reminder = asyncio.create_task(
        daemon._remind_of_maintenance_pause(runtime.database_path, shutdown),
    )
    await asyncio.sleep(0.05)
    shutdown.set()
    await reminder

    assert "Maintenance" not in caplog.text
