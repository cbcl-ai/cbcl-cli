"""CLI startup and shutdown must preserve credential ownership boundaries."""

from __future__ import annotations

import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from src import cli_commands, office_runtime, paths
from src.config import Config, OfficeConfig
from src.docker import container_manager

OFFICE_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def forbid_host_docker_access(monkeypatch):
    """Unit lifecycle tests must supply a synthetic client, never use host Docker."""
    import docker

    monkeypatch.setattr(
        docker, "from_env",
        MagicMock(side_effect=AssertionError("Test must install a synthetic Docker client")),
    )


def test_maintenance_status_uses_runtime_registry_not_removed_config_offices(tmp_path, monkeypatch):
    from src.runtime_state import RuntimeState

    state_path = tmp_path / "runtime.sqlite3"
    monkeypatch.setattr(paths, "get_runtime_state_path", lambda: state_path)
    monkeypatch.setattr(cli_commands, "config_exists", lambda: True)
    monkeypatch.setattr(cli_commands, "load_config", lambda: Config())
    RuntimeState(state_path, OFFICE_ID).set_maintenance(True)
    RuntimeState(state_path, OFFICE_ID).snapshot(0, 0)
    result = CliRunner().invoke(cli_commands.maintenance, ["status"])
    assert result.exit_code == 0, result.output
    assert OFFICE_ID in result.output


@pytest.fixture
def cli_environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(paths, "CUBICLE_HOME", home)
    monkeypatch.setattr(cli_commands, "CUBICLE_HOME", home)
    office = OfficeConfig(
        id=OFFICE_ID, name="Renamed Office", workspace_slug="original-office"
    )
    manager = MagicMock(spec=container_manager.ContainerManager)
    manager.inspect_office_runtime = AsyncMock(
        return_value={"status": "fresh", "can_start": True, "message": ""}
    )
    manager.get_status_by_name = AsyncMock(return_value={"status": "not_running"})
    monkeypatch.setattr(cli_commands, "ContainerManager", lambda **kwargs: manager)
    monkeypatch.setattr(cli_commands, "config_exists", lambda: True)
    monkeypatch.setattr(
        cli_commands,
        "load_config",
        lambda: Config(platform_url="https://platform.invalid", security_token=""),
    )
    monkeypatch.setattr(cli_commands, "fetch_offices_sync", lambda *args: [office])
    monkeypatch.setattr(cli_commands, "_ufw_preflight", MagicMock())
    start_daemon = MagicMock()
    start_foreground = MagicMock()
    monkeypatch.setattr(cli_commands, "_start_daemon", start_daemon)
    monkeypatch.setattr(cli_commands, "_start_foreground", start_foreground)
    pid_path = tmp_path / "communicator.pid"
    monkeypatch.setattr(cli_commands, "get_pid_path", lambda: pid_path)
    monkeypatch.setattr(cli_commands, "get_logs_path", lambda: tmp_path)
    monkeypatch.setattr(cli_commands, "find_running_daemon_pid", lambda: None)
    monkeypatch.setattr(cli_commands, "_is_process_running", lambda pid: False)
    return SimpleNamespace(
        office=office,
        home=home,
        workspace=home / "workspaces" / office.slug,
        manager=manager,
        pid_path=pid_path,
        start_daemon=start_daemon,
        start_foreground=start_foreground,
    )


@pytest.mark.parametrize("arguments", [[], ["--daemon"]])
def test_blocked_start_never_forks_or_builds(cli_environment, arguments):
    environment = cli_environment
    command = (
        f"cbcl migrate-credentials --office-id {OFFICE_ID} "
        f"--workspace '{environment.workspace}' --approve-legacy-owner"
    )
    environment.manager.inspect_office_runtime.return_value = {
        "status": "blocked",
        "can_start": False,
        "message": f"Legacy credential ownership is unverified. Run {command}",
    }

    result = CliRunner().invoke(cli_commands.start, arguments)

    assert result.exit_code != 0
    assert OFFICE_ID in result.output
    assert str(environment.workspace) in result.output
    assert command in result.output
    environment.start_daemon.assert_not_called()
    environment.start_foreground.assert_not_called()
    environment.manager.ensure_image.assert_not_called()
    assert not environment.home.exists()


@pytest.mark.parametrize(
    "runtime_status", ["fresh", "ready", "migration_pending", "migration_in_progress"]
)
def test_valid_runtime_states_allow_background_initialization(
    cli_environment, runtime_status
):
    environment = cli_environment
    environment.manager.inspect_office_runtime.return_value = {
        "status": runtime_status,
        "can_start": True,
        "message": "",
    }

    result = CliRunner().invoke(cli_commands.start, ["--daemon"])

    assert result.exit_code == 0, result.output
    environment.start_daemon.assert_called_once()
    environment.manager.inspect_office_runtime.assert_awaited_once_with(
        environment.office
    )
    assert "office initialization continues" in result.output
    assert not environment.home.exists()


def test_inspection_failure_is_not_reported_as_ready(cli_environment):
    environment = cli_environment
    environment.manager.inspect_office_runtime.side_effect = RuntimeError(
        "Docker is unavailable"
    )

    result = CliRunner().invoke(cli_commands.start, ["--daemon"])

    assert result.exit_code != 0
    assert "Credential inspection failed: Docker is unavailable" in result.output
    environment.start_daemon.assert_not_called()


def test_status_uses_pinned_slug_after_office_rename(cli_environment):
    environment = cli_environment

    result = CliRunner().invoke(cli_commands.status)

    assert result.exit_code == 0, result.output
    environment.manager.get_status_by_name.assert_awaited_once_with(
        "cbcl-office-original-office"
    )
    assert "Renamed Office" in result.output
    assert str(environment.workspace) in result.output
    assert not environment.home.exists()


def test_running_daemon_still_reports_blocked_credentials(cli_environment, monkeypatch):
    environment = cli_environment
    environment.pid_path.write_text("12345")
    monkeypatch.setattr(cli_commands, "_is_process_running", lambda pid: True)
    environment.manager.inspect_office_runtime.return_value = {
        "status": "blocked",
        "can_start": False,
        "message": "Legacy credential ownership is unverified; approve the exact mapping.",
    }

    result = CliRunner().invoke(cli_commands.status)

    assert result.exit_code == 0, result.output
    assert "Running (PID 12345)" in result.output
    assert "daemon process only" in result.output
    assert "Container: not_running" in result.output
    assert "Credentials: blocked" in result.output
    assert "Legacy credential ownership is unverified" in result.output


@pytest.mark.parametrize("daemon_running", [False, True])
def test_stop_preserves_ownership_before_signal_and_sweep(
    cli_environment, monkeypatch, daemon_running
):
    environment = cli_environment
    events = []
    monkeypatch.setattr(
        container_manager,
        "preserve_managed_credential_ownership",
        lambda: events.append("preserve") or 1,
    )
    monkeypatch.setattr(
        container_manager,
        "stop_and_remove_managed_containers",
        lambda: events.append("remove") or 1,
    )
    if daemon_running:
        environment.pid_path.write_text("12345")
        monkeypatch.setattr(
            cli_commands, "_is_process_running", MagicMock(side_effect=[True, False])
        )
    monkeypatch.setattr(
        cli_commands.os,
        "kill",
        lambda pid, requested_signal: events.append(("signal", requested_signal)),
    )
    monkeypatch.setattr(cli_commands.time, "sleep", lambda seconds: None)

    result = CliRunner().invoke(cli_commands.stop)

    assert result.exit_code == 0, result.output
    expected = ["preserve"]
    if daemon_running:
        expected.append(("signal", signal.SIGTERM))
    expected.append("remove")
    assert events == expected
    assert not environment.pid_path.exists()


def test_ownership_capture_failure_never_prevents_stop(cli_environment, monkeypatch):
    environment = cli_environment
    environment.pid_path.write_text("12345")
    monkeypatch.setattr(
        container_manager,
        "preserve_managed_credential_ownership",
        MagicMock(side_effect=OSError("Disk is full")),
    )
    remove = MagicMock(return_value=1)
    monkeypatch.setattr(container_manager, "stop_and_remove_managed_containers", remove)
    monkeypatch.setattr(
        cli_commands, "_is_process_running", MagicMock(side_effect=[True, False])
    )
    kill = MagicMock()
    monkeypatch.setattr(cli_commands.os, "kill", kill)
    monkeypatch.setattr(cli_commands.time, "sleep", lambda seconds: None)

    result = CliRunner().invoke(cli_commands.stop)

    assert result.exit_code == 0, result.output
    assert "could not preserve credential ownership" in result.output
    assert "Stopping continues" in result.output
    kill.assert_called_once_with(12345, signal.SIGTERM)
    remove.assert_called_once()


def _use_synthetic_docker(environment, monkeypatch, container=None):
    import docker

    client = MagicMock()
    present = {"container": container}

    def get_container(name):
        if present["container"] is None:
            raise docker.errors.NotFound("Container was removed")
        assert name == f"cbcl-office-{environment.office.slug}"
        return present["container"]

    client.containers.get.side_effect = get_container
    client.containers.list.side_effect = lambda **kwargs: (
        [present["container"]] if present["container"] is not None else []
    )
    monkeypatch.setattr(docker, "from_env", lambda **kwargs: client)
    manager = container_manager.ContainerManager(use_docker=True)
    manager._client = client
    monkeypatch.setattr(cli_commands, "ContainerManager", lambda **kwargs: manager)
    return present


def test_actual_preflight_never_creates_fresh_workspace(cli_environment, monkeypatch):
    environment = cli_environment
    _use_synthetic_docker(environment, monkeypatch)

    result = CliRunner().invoke(cli_commands.start, ["--daemon"])

    assert result.exit_code == 0, result.output
    environment.start_daemon.assert_called_once()
    assert not environment.home.exists()


def test_actual_preflight_refuses_unproven_legacy_without_writes(
    cli_environment, monkeypatch
):
    environment = cli_environment
    legacy_auth = environment.workspace / ".claude-auth"
    legacy_auth.mkdir(parents=True)
    credential = legacy_auth / ".credentials.json"
    credential.write_text("synthetic credential")
    before = credential.stat()
    _use_synthetic_docker(environment, monkeypatch)

    result = CliRunner().invoke(cli_commands.start, ["--daemon"])

    assert result.exit_code != 0
    assert f"--office-id {OFFICE_ID}" in result.output
    assert "--approve-legacy-owner" in result.output
    assert str(environment.workspace) in result.output
    environment.start_daemon.assert_not_called()
    assert not office_runtime.runtime_base().exists()
    assert credential.read_text() == "synthetic credential"
    assert credential.stat().st_mtime_ns == before.st_mtime_ns
    assert credential.stat().st_ino == before.st_ino


@pytest.mark.parametrize("older_daemon_running", [False, True])
def test_actual_stop_then_start_preserves_verified_legacy_ownership(
    cli_environment, monkeypatch, older_daemon_running
):
    environment = cli_environment
    legacy_auth = environment.workspace / ".claude-auth"
    legacy_ssh = environment.workspace / "ssh-keys"
    legacy_auth.mkdir(parents=True)
    legacy_ssh.mkdir()
    (legacy_auth / ".credentials.json").write_text("synthetic credential")
    (legacy_ssh / "test-key").write_text("synthetic ssh key")
    container = MagicMock()
    container.id = "immutable-container-id"
    container.name = f"cbcl-office-{environment.office.slug}"
    container.status = "running"
    container.labels = {"cbcl.managed": "true", "cbcl.office_id": OFFICE_ID}
    container.attrs = {
        "Mounts": [
            {"Type": "bind", "Source": str(source), "Destination": target, "RW": True}
            for source, target in [
                (environment.workspace, "/workspace"),
                (legacy_auth, "/home/agent/.claude"),
                (legacy_ssh, "/home/agent/.ssh"),
            ]
        ],
        "Config": {"Env": []},
    }
    present = _use_synthetic_docker(environment, monkeypatch, container)
    container.remove.side_effect = lambda **kwargs: present.update(container=None)

    def older_daemon_stops(pid, requested_signal):
        assert requested_signal == signal.SIGTERM
        assert (office_runtime.office_runtime_dir(OFFICE_ID) / "approval.json").exists()
        present["container"] = None

    if older_daemon_running:
        environment.pid_path.write_text("12345")
        monkeypatch.setattr(
            cli_commands, "_is_process_running", MagicMock(side_effect=[True, False])
        )
    monkeypatch.setattr(cli_commands.os, "kill", older_daemon_stops)
    monkeypatch.setattr(cli_commands.time, "sleep", lambda seconds: None)

    stopped = CliRunner().invoke(cli_commands.stop)

    assert stopped.exit_code == 0, stopped.output
    assert present["container"] is None
    assert (office_runtime.office_runtime_dir(OFFICE_ID) / "approval.json").exists()

    def initialize_runtime(config):
        with office_runtime.runtime_lock(OFFICE_ID):
            office_runtime.prepare_runtime(OFFICE_ID, environment.workspace)

    environment.start_daemon.side_effect = initialize_runtime
    started = CliRunner().invoke(cli_commands.start, ["--daemon"])

    assert started.exit_code == 0, started.output
    environment.start_daemon.assert_called_once()
    assert office_runtime.require_ready(OFFICE_ID)
    assert (
        office_runtime.claude_auth_dir(OFFICE_ID) / ".credentials.json"
    ).read_text() == "synthetic credential"
    assert (
        office_runtime.ssh_keys_dir(OFFICE_ID) / "test-key"
    ).read_text() == "synthetic ssh key"
    assert not legacy_auth.exists()
    assert not legacy_ssh.exists()
