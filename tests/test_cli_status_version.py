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
            anthropic_api_key="",
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
