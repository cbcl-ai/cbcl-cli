"""`cbcl status` — daemon-version line + `get_daemon_version` helper.

The status header must print the installed cbcl version (resolved via
importlib metadata of the ``cubicle-communicator`` package — there is
no ``__version__`` attribute anywhere in this codebase).
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from src import cli_commands
from src.config import Config
from src.utils import get_daemon_version


class TestGetDaemonVersion:
    def test_returns_nonempty_string(self):
        version = get_daemon_version()
        assert isinstance(version, str)
        assert version  # "unknown" fallback at minimum — never empty

    def test_matches_installed_metadata_when_available(self):
        try:
            from importlib.metadata import version

            expected = version("cubicle-communicator")
        except Exception:
            expected = "unknown"
        assert get_daemon_version() == expected


@pytest.fixture
def status_env(tmp_path, monkeypatch):
    """Neutralise every external touchpoint of `cbcl status`: config
    present, no daemon PID, no offices, logs under tmp."""
    monkeypatch.setattr(cli_commands, "config_exists", lambda: True)
    monkeypatch.setattr(
        cli_commands, "load_config",
        lambda: Config(
            platform_url="https://app.cbcl.ai",
            security_token="",
        ),
    )
    monkeypatch.setattr(
        cli_commands, "get_pid_path", lambda: tmp_path / "cbcl.pid",
    )
    monkeypatch.setattr(
        cli_commands, "find_running_daemon_pid", lambda: None,
    )
    monkeypatch.setattr(
        cli_commands, "fetch_offices_sync", lambda url, token: [],
    )
    monkeypatch.setattr(cli_commands, "get_logs_path", lambda: tmp_path)


class TestStatusPrintsVersion:
    def test_version_line_in_header(self, status_env, monkeypatch):
        monkeypatch.setattr(
            cli_commands, "get_daemon_version", lambda: "9.9.9-test",
        )
        result = CliRunner().invoke(cli_commands.status)
        assert result.exit_code == 0
        assert "Cubicle Communicator" in result.output
        assert "Version:  9.9.9-test" in result.output

    def test_real_version_resolves_without_crash(self, status_env):
        result = CliRunner().invoke(cli_commands.status)
        assert result.exit_code == 0
        assert "Version:" in result.output


OFFICE_ID = "5c0f3e2a-6d0b-4c4e-9a53-2b1c6f7d8e90"


@pytest.fixture
def runtime_db(tmp_path, monkeypatch):
    path = tmp_path / "runtime" / "control.sqlite3"
    monkeypatch.setattr("src.paths.get_runtime_state_path", lambda: path)
    return path


@pytest.fixture
def one_office(status_env, monkeypatch):
    """`cbcl status` with one office the platform assigns to this cbcl."""
    from unittest.mock import AsyncMock, MagicMock

    from src.config import OfficeConfig

    manager = MagicMock()
    manager.get_status_by_name = AsyncMock(return_value={"status": "running"})
    manager.inspect_office_runtime = AsyncMock(
        return_value={"status": "ready", "message": ""},
    )
    monkeypatch.setattr(cli_commands, "ContainerManager", lambda **kwargs: manager)
    monkeypatch.setattr(
        cli_commands, "fetch_offices_sync",
        lambda url, token: [OfficeConfig(id=OFFICE_ID, name="Sales")],
    )
    monkeypatch.setattr(
        cli_commands, "get_workspace_path", lambda slug, create=False: f"/home/x/{slug}",
    )


class TestStatusShowsMaintenancePause:
    """A pause left on after an upgrade must be visible where operators look."""

    def test_no_runtime_database_prints_nothing_and_creates_nothing(
        self, status_env, runtime_db,
    ):
        result = CliRunner().invoke(cli_commands.status)
        assert result.exit_code == 0
        assert "Maintenance" not in result.output
        assert not runtime_db.exists(), "status only reports"

    def test_open_offices_print_no_maintenance_line(self, one_office, runtime_db):
        from src.runtime_state import RuntimeState

        RuntimeState(runtime_db, "*").set_maintenance(False)
        result = CliRunner().invoke(cli_commands.status)
        assert result.exit_code == 0
        assert "Maintenance" not in result.output

    def test_global_pause_is_shown_with_the_command_that_reopens(
        self, one_office, runtime_db,
    ):
        from src.runtime_state import RuntimeState

        RuntimeState(runtime_db, "*").set_maintenance(True)
        result = CliRunner().invoke(cli_commands.status)
        assert result.exit_code == 0
        assert "Maintenance: ON" in result.output
        assert "all offices, since " in result.output
        assert "reopen with: cbcl maintenance disable\n" in result.output
        assert "    Maintenance: paused" in result.output, "the office row says so too"

    def test_office_pause_names_the_office_and_its_own_command(
        self, one_office, runtime_db,
    ):
        from src.runtime_state import RuntimeState

        RuntimeState(runtime_db, OFFICE_ID).set_maintenance(True)
        RuntimeState(runtime_db, "another-office").set_maintenance(False)
        result = CliRunner().invoke(cli_commands.status)
        assert result.exit_code == 0
        assert f"office {OFFICE_ID}, since " in result.output
        assert f"cbcl maintenance disable --office-id {OFFICE_ID}" in result.output
        assert "another-office" not in result.output
        assert "    Maintenance: paused" in result.output

    def test_unreadable_state_is_unknown_not_open(self, status_env, runtime_db):
        runtime_db.parent.mkdir(parents=True)
        runtime_db.write_bytes(b"this is not a database file at all" * 64)
        result = CliRunner().invoke(cli_commands.status)
        assert result.exit_code == 0
        assert "Maintenance: unknown" in result.output
        assert "cbcl maintenance status" in result.output
