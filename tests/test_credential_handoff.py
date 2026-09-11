"""Synthetic upgrade handoff tests: no live Docker, AI, or credentials."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import docker
import pytest

from src import office_runtime as runtime, paths
from src.config import OfficeConfig
from src.docker import container_manager

OFFICE_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def legacy_office(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CUBICLE_HOME", tmp_path / "home")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in (".claude-auth", "ssh-keys"):
        (workspace / name).mkdir()
        (workspace / name / "synthetic.txt").write_text(f"original {name}")
    container = MagicMock()
    container.id = "synthetic-legacy-container"
    container.name = "cbcl-office-original"
    container.status = "running"
    container.labels = {"cbcl.office_id": OFFICE_ID, "cbcl.managed": "true"}
    container.attrs = {
        "Mounts": [
            {"Type": "bind", "Source": str(source), "Destination": target, "RW": True}
            for source, target in (
                (workspace, "/workspace"),
                (workspace / ".claude-auth", "/home/agent/.claude"),
                (workspace / "ssh-keys", "/home/agent/.ssh"),
            )
        ],
        "Config": {"Env": []},
    }
    return SimpleNamespace(workspace=workspace, container=container)


def prepare(workspace):
    with runtime.runtime_lock(OFFICE_ID):
        runtime.prepare_runtime(OFFICE_ID, workspace)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_path", ["daemon", "sweep", "old-daemon"])
async def test_stop_then_fresh_manager_start_preserves_login_and_rollback(
    legacy_office, monkeypatch, stop_path
):
    workspace, container = legacy_office.workspace, legacy_office.container
    current = container
    events = []

    def remove(**kwargs):
        nonlocal current
        assert runtime._read_record(
            runtime.office_runtime_dir(OFFICE_ID) / "approval.json"
        )
        assert (
            workspace / ".claude-auth" / "synthetic.txt"
        ).read_text() == "original .claude-auth"
        events.append("remove")
        current = None

    def get(name):
        if current is None:
            raise docker.errors.NotFound("synthetic container removed")
        return current

    def run(image, **kwargs):
        runtime.require_ready(OFFICE_ID)
        assert str(runtime.claude_auth_dir(OFFICE_ID)) in kwargs["volumes"]
        assert str(runtime.ssh_keys_dir(OFFICE_ID)) in kwargs["volumes"]
        created = MagicMock()
        created.id = "synthetic-private-container"
        created.labels = kwargs["labels"]
        created.attrs = {
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": source,
                    "Destination": options["bind"],
                    "RW": options["mode"] == "rw",
                }
                for source, options in kwargs["volumes"].items()
            ],
            "Config": {
                "Env": [
                    f"{name}={value}" for name, value in kwargs["environment"].items()
                ]
            },
        }
        return created

    container.remove.side_effect = remove
    client = SimpleNamespace(
        containers=SimpleNamespace(
            get=get,
            list=lambda **kwargs: [current] if current is not None else [],
            run=run,
        ),
        close=MagicMock(),
    )
    monkeypatch.setattr(docker, "from_env", lambda **kwargs: client)
    before = {
        name: runtime._identity(workspace / name)
        for name in (".claude-auth", "ssh-keys")
    }
    with monkeypatch.context() as capture:
        capture.setattr(
            runtime,
            "_tree_digest",
            MagicMock(side_effect=AssertionError("handoff read credentials")),
        )
        if stop_path == "daemon":
            old_manager = container_manager.ContainerManager(use_docker=True)
            old_manager._containers[OFFICE_ID] = container
            await old_manager.stop_office(OFFICE_ID)
            container.stop.assert_called_once_with(timeout=30)
        elif stop_path == "sweep":
            assert container_manager.stop_and_remove_managed_containers() == 1
        else:
            assert container_manager.preserve_managed_credential_ownership() == 1
            container.remove()
    assert events == ["remove"]
    assert before == {name: runtime._identity(workspace / name) for name in before}
    fresh_manager = container_manager.ContainerManager(use_docker=True)
    monkeypatch.setattr(fresh_manager, "_get_client", lambda: client)
    assert (
        await fresh_manager.start_office("original", OFFICE_ID, str(workspace))
        == "synthetic-private-container"
    )
    root = runtime.require_ready(OFFICE_ID)
    for kind, name in (("claude-auth", ".claude-auth"), ("ssh-keys", "ssh-keys")):
        assert not (workspace / name).exists()
        assert (root / kind / "synthetic.txt").read_text() == f"original {name}"
        assert (
            root / "rollback" / kind / "synthetic.txt"
        ).read_text() == f"original {name}"
        assert runtime._identity(root / "rollback" / kind) == before[name]


@pytest.mark.parametrize(
    "fault",
    [
        "missing-label",
        "invalid-uuid",
        "other-uuid",
        "auth-mount",
        "workspace-mount",
        "duplicate-mount",
        "symlink-workspace",
    ],
)
def test_handoff_rejects_unproven_ownership(legacy_office, tmp_path, fault):
    container = legacy_office.container
    if fault == "missing-label":
        container.labels.pop("cbcl.office_id")
    elif fault == "invalid-uuid":
        container.labels["cbcl.office_id"] = "office-name"
    elif fault == "other-uuid":
        container.labels["cbcl.office_id"] = OTHER_ID
    elif fault == "auth-mount":
        container.attrs["Mounts"][1]["Source"] = str(tmp_path / "other-auth")
    elif fault == "workspace-mount":
        container.attrs["Mounts"][0]["Destination"] = "/not-workspace"
    elif fault == "duplicate-mount":
        container.attrs["Mounts"].append(dict(container.attrs["Mounts"][1]))
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(legacy_office.workspace, target_is_directory=True)
        container.attrs["Mounts"][0]["Source"] = str(alias)
    with pytest.raises(runtime.RuntimeStorageError):
        runtime.preserve_legacy_ownership(container, expected_office_id=OFFICE_ID)
    assert not paths.CUBICLE_HOME.exists()


def test_changed_directory_identity_never_inherits_saved_approval(legacy_office):
    runtime.preserve_legacy_ownership(legacy_office.container)
    original = legacy_office.workspace / ".claude-auth"
    original.rename(legacy_office.workspace / "old-auth")
    original.mkdir()
    inspection = runtime.inspect_runtime(OFFICE_ID, legacy_office.workspace)
    assert inspection["can_start"] is False
    with pytest.raises(runtime.RuntimeStorageError, match="unverified"):
        prepare(legacy_office.workspace)
    assert (
        legacy_office.workspace / "old-auth" / "synthetic.txt"
    ).read_text() == "original .claude-auth"
    with pytest.raises(runtime.RuntimeStorageError, match="different legacy ownership"):
        runtime.preserve_legacy_ownership(legacy_office.container)


def test_handoff_noop_after_migration_keeps_journal_unchanged(legacy_office):
    runtime.preserve_legacy_ownership(legacy_office.container)
    prepare(legacy_office.workspace)
    root = runtime.office_runtime_dir(OFFICE_ID)
    records = {
        name: (root / name).read_bytes() for name in ("approval.json", "state.json")
    }
    assert runtime.preserve_legacy_ownership(legacy_office.container) is False
    assert records == {name: (root / name).read_bytes() for name in records}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["busy-lock", "write-failed"])
async def test_failed_handoff_warns_without_blocking_worker_stop(
    legacy_office, monkeypatch, caplog, failure
):
    manager = container_manager.ContainerManager(use_docker=True)
    manager._containers[OFFICE_ID] = legacy_office.container
    if failure == "busy-lock":
        with runtime.runtime_lock(OFFICE_ID):
            await asyncio.wait_for(manager.stop_office(OFFICE_ID), timeout=2)
    else:
        monkeypatch.setattr(
            runtime,
            "_write_record",
            MagicMock(side_effect=OSError("synthetic full disk")),
        )
        await manager.stop_office(OFFICE_ID)
    legacy_office.container.stop.assert_called_once_with(timeout=30)
    legacy_office.container.remove.assert_called_once()
    assert "Stopping continues" in caplog.text
    assert not (runtime.office_runtime_dir(OFFICE_ID) / "approval.json").exists()


@pytest.mark.parametrize(
    "fault",
    [
        "missing-fields",
        "missing-kind",
        "bad-identity",
        "bad-fingerprint",
        "conflicting-source",
        "missing-source",
    ],
)
def test_readonly_inspection_rejects_malformed_or_conflicting_journal(
    legacy_office, monkeypatch, fault
):
    workspace = legacy_office.workspace
    runtime.preserve_legacy_ownership(legacy_office.container)
    root = runtime.office_runtime_dir(OFFICE_ID)
    state = {
        "version": runtime.RUNTIME_VERSION,
        "office_id": OFFICE_ID,
        "workspace": str(workspace),
        "phase": "migrating",
        "legacy": {
            kind: runtime._identity(workspace / name)
            for kind, name in runtime._KINDS.items()
        },
        "fingerprints": {kind: "a" * 64 for kind in runtime._KINDS},
    }
    if fault == "missing-fields":
        state.pop("legacy")
    elif fault == "missing-kind":
        state["fingerprints"].pop("ssh-keys")
    elif fault == "bad-identity":
        state["legacy"]["claude-auth"] = [True, 123]
    elif fault == "bad-fingerprint":
        state["fingerprints"]["claude-auth"] = "not-a-digest"
    elif fault == "conflicting-source":
        state["legacy"]["claude-auth"] = None
        state["fingerprints"]["claude-auth"] = None
    else:
        (workspace / ".claude-auth").rename(workspace / "lost-auth")
    runtime._write_record(root / "state.json", state)
    before = (root / "state.json").read_bytes()
    with monkeypatch.context() as readonly:
        readonly.setattr(
            runtime,
            "_tree_digest",
            MagicMock(side_effect=AssertionError("read credentials")),
        )
        readonly.setattr(
            Path, "mkdir", MagicMock(side_effect=AssertionError("created path"))
        )
        readonly.setattr(
            Path, "chmod", MagicMock(side_effect=AssertionError("changed permissions"))
        )
        result = runtime.inspect_runtime(OFFICE_ID, workspace)
    assert result["can_start"] is False
    assert "migrate-credentials" not in result["message"]
    assert (root / "state.json").read_bytes() == before


@pytest.mark.parametrize("interruption", ["before-copy", "after-original-move"])
def test_readonly_inspection_accepts_real_resumable_journal(
    legacy_office, monkeypatch, interruption
):
    runtime.preserve_legacy_ownership(legacy_office.container)
    real_digest = runtime._tree_digest
    real_rename = runtime.os.rename

    def interrupt_copy(source, destination=None):
        if destination is not None:
            raise RuntimeError("synthetic interruption")
        return real_digest(source, destination)

    def interrupt_rename(source, destination):
        real_rename(source, destination)
        if Path(source) == legacy_office.workspace / ".claude-auth":
            raise RuntimeError("synthetic interruption")

    with monkeypatch.context() as interrupted:
        if interruption == "before-copy":
            interrupted.setattr(runtime, "_tree_digest", interrupt_copy)
        else:
            interrupted.setattr(runtime.os, "rename", interrupt_rename)
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            prepare(legacy_office.workspace)
    with monkeypatch.context() as readonly:
        readonly.setattr(
            runtime,
            "_tree_digest",
            MagicMock(side_effect=AssertionError("read credentials")),
        )
        result = runtime.inspect_runtime(OFFICE_ID, legacy_office.workspace)
    assert result["status"] == "migration_in_progress"
    assert result["can_start"] is True
    prepare(legacy_office.workspace)
    runtime.require_ready(OFFICE_ID)


@pytest.mark.parametrize(
    "parent", ["home", "private-runtime", "offices", "office-root"]
)
def test_readonly_inspection_refuses_symlinked_private_parents(
    legacy_office, tmp_path, parent
):
    runtime.preserve_legacy_ownership(legacy_office.container)
    selected = {
        "home": paths.CUBICLE_HOME,
        "private-runtime": runtime.runtime_base(),
        "offices": runtime.runtime_base() / "offices",
        "office-root": runtime.office_runtime_dir(OFFICE_ID),
    }[parent]
    moved = tmp_path / "synthetic-moved-storage"
    selected.rename(moved)
    selected.symlink_to(moved, target_is_directory=True)
    result = runtime.inspect_runtime(OFFICE_ID, legacy_office.workspace)
    assert result["can_start"] is False
    assert "migrate-credentials" not in result["message"]


@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("owner", [None, OTHER_ID])
def test_inspection_rejects_conflicting_container_even_without_legacy_files(
    legacy_office, ready, owner
):
    runtime.preserve_legacy_ownership(legacy_office.container)
    prepare(legacy_office.workspace)
    if not ready:
        (runtime.office_runtime_dir(OFFICE_ID) / "state.json").unlink()
    legacy_office.container.labels = {"cbcl.office_id": owner}
    result = runtime.inspect_runtime(
        OFFICE_ID, legacy_office.workspace, container=legacy_office.container
    )
    assert result["can_start"] is False
    assert "ownership" in result["message"]


def test_workspace_path_inspection_does_not_create_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CUBICLE_HOME", tmp_path / "missing-home")
    path = paths.get_workspace_path("office", create=False)
    assert path == paths.CUBICLE_HOME / "workspaces" / "office"
    assert not paths.CUBICLE_HOME.exists()
    assert runtime.inspect_runtime(OFFICE_ID, path)["status"] == "fresh"
    assert not paths.CUBICLE_HOME.exists()
    assert paths.get_workspace_path("office") == path
    assert path.is_dir()


@pytest.mark.asyncio
async def test_inspection_wrapper_uses_pinned_slug_and_never_creates_workspace(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(paths, "CUBICLE_HOME", tmp_path / "missing-home")
    office = OfficeConfig(id=OFFICE_ID, name="Renamed", workspace_slug="original")
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("synthetic absent")
    manager = container_manager.ContainerManager(use_docker=True)
    monkeypatch.setattr(manager, "_get_client", lambda: client)
    assert (await manager.inspect_office_runtime(office))["status"] == "fresh"
    client.containers.get.assert_called_once_with("cbcl-office-original")
    assert not paths.CUBICLE_HOME.exists()
    client.containers.get.side_effect = RuntimeError("synthetic Docker unavailable")
    with pytest.raises(RuntimeError, match="Docker unavailable"):
        await manager.inspect_office_runtime(office)


def test_private_ready_agent_owned_children_remain_valid(legacy_office):
    runtime.preserve_legacy_ownership(legacy_office.container)
    prepare(legacy_office.workspace)
    root = runtime.office_runtime_dir(OFFICE_ID)
    if os.geteuid() == 0:
        os.chown(root / "claude-auth", 1000, 1000)
        os.chown(root / "ssh-keys", 1000, 1000)
    result = runtime.inspect_runtime(OFFICE_ID, legacy_office.workspace)
    assert result["status"] == "ready"
    assert result["can_start"] is True
    assert json.loads((root / "state.json").read_text())["phase"] == "ready"


@pytest.mark.parametrize("parent", ["office-root", "rollback"])
def test_inspection_rejects_wrong_owner_private_parents(legacy_office, parent):
    if os.geteuid() != 0:
        pytest.skip("Synthetic chown test requires isolated container root")
    runtime.preserve_legacy_ownership(legacy_office.container)
    prepare(legacy_office.workspace)
    root = runtime.office_runtime_dir(OFFICE_ID)
    if parent == "rollback":
        state = runtime._read_record(root / "state.json")
        state["phase"] = "migrating"
        runtime._write_record(root / "state.json", state)
    os.chown(root if parent == "office-root" else root / "rollback", 1000, 1000)
    result = runtime.inspect_runtime(OFFICE_ID, legacy_office.workspace)
    assert result["can_start"] is False
    assert "ownership" in result["message"]


def test_inspection_rejects_cross_device_legacy_metadata(legacy_office, monkeypatch):
    real_identity = runtime._identity

    def cross_device(path):
        identity = real_identity(path)
        if Path(path) == legacy_office.workspace / ".claude-auth":
            return [identity[0] + 1, identity[1]]
        return identity

    monkeypatch.setattr(runtime, "_identity", cross_device)
    result = runtime.inspect_runtime(
        OFFICE_ID, legacy_office.workspace, container=legacy_office.container
    )
    assert result["can_start"] is False
    assert "Cross-filesystem" in result["message"]
    assert not paths.CUBICLE_HOME.exists()


def test_replaced_workspace_identity_does_not_inherit_approval(legacy_office):
    workspace = legacy_office.workspace
    runtime.preserve_legacy_ownership(legacy_office.container)
    moved = workspace.with_name("original-workspace")
    workspace.rename(moved)
    workspace.mkdir()
    for name in (".claude-auth", "ssh-keys"):
        (moved / name).rename(workspace / name)
    assert runtime.inspect_runtime(OFFICE_ID, workspace)["can_start"] is False
    with pytest.raises(runtime.RuntimeStorageError, match="unverified"):
        prepare(workspace)
    with pytest.raises(runtime.RuntimeStorageError, match="different legacy ownership"):
        runtime.preserve_legacy_ownership(legacy_office.container)


def test_separate_offices_preserve_independent_ownership(legacy_office, tmp_path):
    workspace = tmp_path / "second-office"
    workspace.mkdir()
    (workspace / ".claude-auth").mkdir()
    (workspace / ".claude-auth" / "synthetic.txt").write_text("other office")
    second = MagicMock()
    second.labels = {"cbcl.office_id": OTHER_ID, "cbcl.managed": "true"}
    second.attrs = {
        "Mounts": [
            {
                **mount,
                "Source": mount["Source"].replace(
                    str(legacy_office.workspace), str(workspace)
                ),
            }
            for mount in legacy_office.container.attrs["Mounts"]
        ]
    }
    assert runtime.preserve_legacy_ownership(legacy_office.container)
    assert runtime.preserve_legacy_ownership(second)
    prepare(legacy_office.workspace)
    with runtime.runtime_lock(OTHER_ID):
        runtime.prepare_runtime(OTHER_ID, workspace)
    assert (
        runtime.claude_auth_dir(OFFICE_ID) / "synthetic.txt"
    ).read_text() == "original .claude-auth"
    assert (
        runtime.claude_auth_dir(OTHER_ID) / "synthetic.txt"
    ).read_text() == "other office"
