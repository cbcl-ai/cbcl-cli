"""Maintenance changes admission only; it never implies a safe restart."""

import json

from click.testing import CliRunner
import pytest

from src.cli_commands import cli
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
