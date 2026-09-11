"""Synthetic private-storage and migration tests; Docker/AI are never invoked."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src import office_runtime as runtime
from src import paths
from src.docker.container_manager import ContainerManager

OFFICE_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CUBICLE_HOME", tmp_path / "home")
    directory = tmp_path / "workspace"
    directory.mkdir()
    return directory


def legacy_files(workspace):
    auth = workspace / ".claude-auth"
    ssh = workspace / "ssh-keys"
    auth.mkdir()
    ssh.mkdir()
    (auth / ".credentials.json").write_text('{"sentinel":"current"}')
    (auth / ".credentials.json.backup").write_text('{"sentinel":"backup"}')
    (auth / ".claude.json").write_text('{"sentinel":"configuration"}')
    (auth / "sessions").mkdir()
    (auth / "sessions" / "history.json").write_text("synthetic history")
    (ssh / "test-key").write_text("synthetic key")
    return auth, ssh


def prepare(workspace, *, authorized=False, office_id=OFFICE_ID):
    with runtime.runtime_lock(office_id):
        runtime.prepare_runtime(office_id, workspace, legacy_authorized=authorized)


def bind(source, destination):
    return {
        "Type": "bind",
        "Source": str(source),
        "Destination": destination,
        "RW": True,
    }


def office_container(workspace, *, private=False):
    container = MagicMock()
    container.id = "immutable-container-id"
    container.short_id = "container"
    container.status = "running"
    container.labels = {"cbcl.office_id": OFFICE_ID}
    container.image.id = "image-id"
    container.attrs = {
        "Mounts": [
            bind(workspace, "/workspace"),
            bind(
                (
                    runtime.claude_auth_dir(OFFICE_ID)
                    if private
                    else workspace / ".claude-auth"
                ),
                "/home/agent/.claude",
            ),
            bind(
                runtime.ssh_keys_dir(OFFICE_ID) if private else workspace / "ssh-keys",
                "/home/agent/.ssh",
            ),
        ],
        "Config": {"Env": []},
    }
    return container


def test_fresh_offices_have_distinct_private_paths_even_for_same_workspace(workspace):
    prepare(workspace)
    prepare(workspace, office_id=OTHER_ID)
    assert runtime.claude_auth_dir(OFFICE_ID) != runtime.claude_auth_dir(OTHER_ID)
    assert not (workspace / ".claude-auth").exists()
    assert not (workspace / "ssh-keys").exists()
    assert runtime.office_runtime_dir(OFFICE_ID).stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("office_id", ["../other", "Office Name", "", "office-1"])
def test_storage_requires_an_immutable_uuid(workspace, office_id):
    with pytest.raises(runtime.RuntimeStorageError, match="UUID"):
        runtime.office_runtime_dir(office_id)


def test_unknown_legacy_owner_preserves_originals_and_refuses_start(workspace):
    auth, ssh = legacy_files(workspace)
    with pytest.raises(runtime.RuntimeStorageError, match="migrate-credentials"):
        prepare(workspace)
    assert (auth / ".credentials.json").read_text() == '{"sentinel":"current"}'
    assert (ssh / "test-key").exists()
    assert not runtime.claude_auth_dir(OFFICE_ID).exists()


def test_explicit_mapping_preserves_full_auth_and_ssh_without_workspace_aliases(
    workspace,
):
    auth, ssh = legacy_files(workspace)
    runtime.approve_legacy_ownership(OFFICE_ID, workspace)
    prepare(workspace)
    root = runtime.require_ready(OFFICE_ID)
    assert not auth.exists() and not ssh.exists()
    assert (
        root / "claude-auth" / ".credentials.json"
    ).read_text() == '{"sentinel":"current"}'
    assert (
        root / "claude-auth" / ".credentials.json.backup"
    ).read_text() == '{"sentinel":"backup"}'
    assert (
        root / "claude-auth" / ".claude.json"
    ).read_text() == '{"sentinel":"configuration"}'
    assert (
        root / "claude-auth" / "sessions" / "history.json"
    ).read_text() == "synthetic history"
    assert (root / "ssh-keys" / "test-key").read_text() == "synthetic key"
    assert (root / "rollback" / "claude-auth" / ".credentials.json").exists()
    assert (root / "ssh-keys" / "test-key").stat().st_mode & 0o777 == 0o600
    prepare(workspace)


def test_approval_does_not_follow_a_replaced_legacy_directory(workspace):
    auth, _ = legacy_files(workspace)
    runtime.approve_legacy_ownership(OFFICE_ID, workspace)
    auth.rename(workspace / "old-synthetic-auth")
    auth.mkdir()
    with pytest.raises(runtime.RuntimeStorageError, match="unverified"):
        prepare(workspace)


def test_existing_private_data_is_never_overwritten(workspace):
    legacy_files(workspace)
    with runtime.runtime_lock(OFFICE_ID):
        private = runtime.office_runtime_dir(OFFICE_ID)
        private.mkdir()
        runtime.claude_auth_dir(OFFICE_ID).mkdir()
        (runtime.claude_auth_dir(OFFICE_ID) / ".credentials.json").write_text(
            "different"
        )
    with pytest.raises(runtime.RuntimeStorageError, match="overwrite"):
        prepare(workspace, authorized=True)
    assert (
        runtime.claude_auth_dir(OFFICE_ID) / ".credentials.json"
    ).read_text() == "different"


@pytest.mark.parametrize("link_kind", ["file", "directory", "hardlink"])
def test_migration_refuses_legacy_links_without_reading_targets(
    workspace, tmp_path, link_kind
):
    auth, _ = legacy_files(workspace)
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    target = sibling / "sentinel"
    target.write_text("must remain private")
    if link_kind == "file":
        (auth / "escape").symlink_to(target)
    elif link_kind == "directory":
        (auth / "escape").symlink_to(sibling, target_is_directory=True)
    else:
        os.link(target, auth / "escape")
    with pytest.raises(runtime.RuntimeStorageError, match="links"):
        prepare(workspace, authorized=True)
    assert target.read_text() == "must remain private"
    assert auth.exists()


@pytest.mark.parametrize("phase", ["staged", "original_moved"])
def test_restart_resumes_from_durable_migration_journal(workspace, monkeypatch, phase):
    legacy_files(workspace)
    real_rename = runtime.os.rename
    fired = False

    def interrupt(source, destination):
        nonlocal fired
        source = Path(source)
        destination = Path(destination)
        if phase == "staged" and source.name == "staging-claude-auth" and not fired:
            fired = True
            raise RuntimeError("synthetic crash")
        real_rename(source, destination)
        if (
            phase == "original_moved"
            and destination.parent.name == "rollback"
            and not fired
        ):
            fired = True
            raise RuntimeError("synthetic crash")

    with monkeypatch.context() as context:
        context.setattr(runtime.os, "rename", interrupt)
        with pytest.raises(RuntimeError, match="synthetic crash"):
            prepare(workspace, authorized=True)
    prepare(workspace)
    assert runtime.require_ready(OFFICE_ID)
    assert (
        runtime.claude_auth_dir(OFFICE_ID) / ".credentials.json"
    ).read_text() == '{"sentinel":"current"}'
    assert not (workspace / ".claude-auth").exists()


def test_partial_stage_requires_recovery_instead_of_overwriting(workspace, monkeypatch):
    legacy_files(workspace)
    original = runtime._tree_digest

    def interrupted(source, destination=None):
        if destination is not None:
            destination.mkdir()
            (destination / "partial").write_text("partial synthetic copy")
            raise RuntimeError("synthetic crash")
        return original(source)

    with monkeypatch.context() as context:
        context.setattr(runtime, "_tree_digest", interrupted)
        with pytest.raises(RuntimeError, match="synthetic crash"):
            prepare(workspace, authorized=True)
    with pytest.raises(runtime.RuntimeStorageError, match="staging"):
        prepare(workspace)
    assert (workspace / ".claude-auth" / ".credentials.json").exists()


def test_reappearing_legacy_alias_blocks_public_runtime_readiness(workspace):
    prepare(workspace)
    (workspace / ".claude-auth").symlink_to(runtime.claude_auth_dir(OFFICE_ID))
    with pytest.raises(runtime.RuntimeStorageError, match="alias"):
        runtime.require_ready(OFFICE_ID)


def test_legacy_authority_requires_office_label_and_exact_bind_sources(workspace):
    legacy_files(workspace)
    container = office_container(workspace)
    assert runtime.legacy_mounts_authorize(container, OFFICE_ID, str(workspace))
    container.labels = {"cbcl.office_id": OTHER_ID}
    assert not runtime.legacy_mounts_authorize(container, OFFICE_ID, str(workspace))
    container.labels = {"cbcl.office_id": OFFICE_ID}
    container.attrs["Mounts"][1]["Source"] = str(workspace / "wrong")
    assert not runtime.legacy_mounts_authorize(container, OFFICE_ID, str(workspace))


def test_private_mounts_reject_global_api_key_and_wrong_office(workspace):
    prepare(workspace)
    container = office_container(workspace, private=True)
    assert runtime.private_mounts_match(container, OFFICE_ID)
    container.attrs["Config"]["Env"] = ["ANTHROPIC_API_KEY=synthetic"]
    assert not runtime.private_mounts_match(container, OFFICE_ID)
    container.attrs["Config"]["Env"] = []
    container.labels = {"cbcl.office_id": OTHER_ID}
    assert not runtime.private_mounts_match(container, OFFICE_ID)


def test_any_live_container_using_backing_prevents_migration(workspace):
    client = SimpleNamespace(
        containers=SimpleNamespace(list=lambda **kwargs: [office_container(workspace)])
    )
    with pytest.raises(runtime.RuntimeStorageError, match="Stop every container"):
        runtime.assert_no_running_credential_users(client, OFFICE_ID, str(workspace))


def test_private_credentials_cannot_be_read_through_symlink(workspace, tmp_path):
    prepare(workspace)
    target = tmp_path / "sentinel"
    target.write_text("synthetic")
    (runtime.claude_auth_dir(OFFICE_ID) / ".credentials.json").symlink_to(target)
    with pytest.raises(OSError):
        runtime.read_auth_file(OFFICE_ID, ".credentials.json")
    runtime.write_auth_file(OFFICE_ID, ".credentials.json", "replacement")
    assert target.read_text() == "synthetic"
    assert runtime.read_auth_file(OFFICE_ID, ".credentials.json") == "replacement"


def test_office_delete_preserves_sibling_and_refuses_live_container(workspace):
    prepare(workspace)
    prepare(workspace, office_id=OTHER_ID)
    client = SimpleNamespace(
        containers=SimpleNamespace(
            list=lambda **kwargs: [office_container(workspace, private=True)]
        )
    )
    with pytest.raises(
        runtime.RuntimeStorageError, match="Remove the office container"
    ):
        runtime.remove_private_runtime(OFFICE_ID, client=client)
    client.containers.list = lambda **kwargs: []
    runtime.remove_private_runtime(OFFICE_ID, client=client)
    assert not runtime.office_runtime_dir(OFFICE_ID).exists()
    assert runtime.require_ready(OTHER_ID)


@pytest.mark.asyncio
async def test_runtime_lock_serializes_credential_writes_and_startup(workspace):
    acquired = asyncio.Event()

    async def second():
        async with runtime.async_runtime_lock(OFFICE_ID):
            acquired.set()

    async with runtime.async_runtime_lock(OFFICE_ID):
        contender = asyncio.create_task(second())
        await asyncio.sleep(0.08)
        assert not acquired.is_set()
    await asyncio.wait_for(contender, 1)
    assert acquired.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [True, False])
async def test_container_recreated_for_legacy_mounts_or_global_api_key(
    workspace, monkeypatch, legacy
):
    import docker.errors

    if legacy:
        legacy_files(workspace)
    else:
        prepare(workspace)
    existing = office_container(workspace, private=not legacy)
    existing.attrs["Config"]["Env"] = ["ANTHROPIC_API_KEY=synthetic"]
    current = existing
    created = []

    def get(name):
        if current is None:
            raise docker.errors.NotFound("absent")
        return current

    def stop(**kwargs):
        existing.status = "exited"

    def remove(**kwargs):
        nonlocal current
        assert runtime.require_ready(OFFICE_ID)
        current = None

    def run(image, **kwargs):
        created.append(kwargs)
        return office_container(workspace, private=True)

    existing.stop.side_effect = stop
    existing.remove.side_effect = remove
    client = SimpleNamespace(
        containers=SimpleNamespace(get=get, list=lambda **kwargs: [], run=run)
    )
    manager = ContainerManager(use_docker=True)
    monkeypatch.setattr(manager, "_get_client", lambda: client)
    result = await manager.start_office("office", OFFICE_ID, str(workspace))
    assert result == "immutable-container-id"
    existing.stop.assert_called_once_with(timeout=30)
    assert created[0]["environment"] == {"OFFICE_ID": OFFICE_ID}
    assert (
        created[0]["volumes"][str(runtime.claude_auth_dir(OFFICE_ID))]["bind"]
        == "/home/agent/.claude"
    )
    assert (
        created[0]["volumes"][str(runtime.ssh_keys_dir(OFFICE_ID))]["bind"]
        == "/home/agent/.ssh"
    )


def test_auth_session_cannot_be_completed_from_a_different_office(
    workspace, monkeypatch
):
    from src import auth_service

    prepare(workspace)
    monkeypatch.setattr(
        runtime, "validated_container_id", lambda office_id, name: "fixed-container-id"
    )
    started = auth_service.start_auth_flow(
        "mutable-container-name", office_id=OFFICE_ID
    )
    result = auth_service.complete_auth_flow(
        started["session_id"], "synthetic-code", office_id=OTHER_ID
    )
    assert result["authenticated"] is False
    assert "different office" in result["error"]
    assert (
        auth_service._SESSIONS[started["session_id"]].container_name
        == "fixed-container-id"
    )


def test_stale_container_prevents_auth_exchange(workspace, monkeypatch):
    from src import auth_service

    prepare(workspace)
    monkeypatch.setattr(
        runtime, "validated_container_id", lambda office_id, name: "fixed-container-id"
    )
    started = auth_service.start_auth_flow("office-name", office_id=OFFICE_ID)

    def unavailable(*args):
        raise runtime.RuntimeStorageError("old container is gone")

    monkeypatch.setattr(runtime, "validated_container_id", unavailable)
    exchange = MagicMock()
    monkeypatch.setattr(auth_service, "_exchange_code_for_tokens", exchange)
    result = auth_service.complete_auth_flow(
        started["session_id"], "synthetic-code", office_id=OFFICE_ID
    )
    assert result["authenticated"] is False
    exchange.assert_not_called()


def test_private_mounts_reject_workspace_substitution_and_readonly_auth(workspace):
    prepare(workspace)
    container = office_container(workspace, private=True)
    container.attrs["Mounts"][0]["Source"] = str(workspace.parent / "sibling")
    assert not runtime.private_mounts_match(container, OFFICE_ID)
    container.attrs["Mounts"][0]["Source"] = str(workspace)
    container.attrs["Mounts"][1]["RW"] = False
    assert not runtime.private_mounts_match(container, OFFICE_ID)


@pytest.mark.asyncio
async def test_cancelled_startup_keeps_lock_until_critical_operation_finishes(
    workspace, monkeypatch
):
    entered = asyncio.Event()
    finish = asyncio.Event()
    reader = MagicMock(return_value="verified-id")
    manager = ContainerManager(use_docker=True)

    async def start(*args):
        entered.set()
        await finish.wait()
        return "created-id"

    monkeypatch.setattr(manager, "_start_office_locked", start)
    monkeypatch.setattr(runtime, "validated_container_id", reader)
    startup = asyncio.create_task(
        manager.start_office("office", OFFICE_ID, str(workspace))
    )
    await entered.wait()
    startup.cancel()
    waiter = asyncio.create_task(
        runtime.resolve_office_container_id(OFFICE_ID, "office")
    )
    await asyncio.sleep(0.08)
    reader.assert_not_called()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await startup
    assert await asyncio.wait_for(waiter, 1) == "verified-id"


def test_offline_approval_cli_records_mapping_without_reading_credentials(
    workspace, monkeypatch
):
    from click.testing import CliRunner
    from src import cli_commands
    from src.main import cli
    import docker

    legacy_files(workspace)
    client = MagicMock()
    client.containers.list.return_value = []
    monkeypatch.setattr(docker, "from_env", lambda: client)
    monkeypatch.setattr(cli_commands, "find_running_daemon_pid", lambda: None)
    snapshot = MagicMock(
        side_effect=AssertionError("approval must not read credential contents")
    )
    monkeypatch.setattr(runtime, "_tree_digest", snapshot)
    result = CliRunner().invoke(
        cli,
        [
            "migrate-credentials",
            "--office-id",
            OFFICE_ID,
            "--workspace",
            str(workspace),
            "--approve-legacy-owner",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "sentinel" not in result.output
    assert (workspace / ".claude-auth" / ".credentials.json").exists()
    snapshot.assert_not_called()


def test_approval_cli_refuses_running_daemon_before_docker_access(
    workspace, monkeypatch
):
    from click.testing import CliRunner
    from src import cli_commands
    from src.main import cli
    import docker

    monkeypatch.setattr(cli_commands, "find_running_daemon_pid", lambda: 12345)
    client_factory = MagicMock()
    monkeypatch.setattr(docker, "from_env", client_factory)
    result = CliRunner().invoke(
        cli,
        [
            "migrate-credentials",
            "--office-id",
            OFFICE_ID,
            "--workspace",
            str(workspace),
            "--approve-legacy-owner",
        ],
    )
    assert result.exit_code != 0
    assert "Stop cbcl" in result.output
    client_factory.assert_not_called()


@pytest.mark.asyncio
async def test_async_identity_resolution_waits_for_runtime_lock(workspace, monkeypatch):
    prepare(workspace)
    resolver = MagicMock(return_value="fixed-container-id")
    monkeypatch.setattr(runtime, "validated_container_id", resolver)
    async with runtime.async_runtime_lock(OFFICE_ID):
        pending = asyncio.create_task(
            runtime.resolve_office_container_id(OFFICE_ID, "office-name")
        )
        await asyncio.sleep(0.08)
        resolver.assert_not_called()
    assert await asyncio.wait_for(pending, 1) == "fixed-container-id"
    resolver.assert_called_once_with(OFFICE_ID, "office-name")


@pytest.mark.asyncio
@pytest.mark.parametrize("container_id", ["a" * 64, None])
async def test_daemon_cli_upgrade_uses_ensured_immutable_id(monkeypatch, container_id):
    from src import daemon

    office = SimpleNamespace(id=OFFICE_ID, name="Synthetic office", slug=None)
    containers = MagicMock()
    containers.ensure_container = AsyncMock(return_value=container_id)
    containers.get_container_name.return_value = "cbcl-office-mutable-name"
    upgrade = AsyncMock(return_value={"ok": True})
    initialize = AsyncMock(side_effect=RuntimeError("synthetic test boundary"))
    monkeypatch.setenv("CUBICLE_AUTO_UPGRADE_CLI", "1")
    monkeypatch.setattr("src.docker.session_bridge.upgrade_cli", upgrade)
    monkeypatch.setattr("src.handlers.init_office_process_model", initialize)

    await daemon._connect_office_process_model(
        office, MagicMock(), containers, MagicMock(), {}, []
    )

    containers.ensure_container.assert_awaited_once_with(office)
    if container_id:
        upgrade.assert_awaited_once_with(container_id)
    else:
        upgrade.assert_not_awaited()
    initialize.assert_awaited_once()
    assert initialize.await_args.kwargs["container_id"] == (container_id or "")
