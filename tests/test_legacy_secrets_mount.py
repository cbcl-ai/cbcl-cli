"""SEC2 — office containers no longer mount the retired ``~/.cubicle/secrets``.

The pre-D2 daemon kept every office's skill secret values under
``~/.cubicle/secrets/skills/<skill>/secrets.json`` and mounted that whole
tree read-only at ``/secrets`` in EVERY office container, so any agent could
read another office's legacy values. Nothing reads ``/secrets`` any more.
New containers are created without it, and a running container from an
older daemon is recreated without it on the next start (the stale-image
path). Docker and AI are never invoked.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import office_runtime as runtime
from src import paths
from src.docker import container_manager as cm_module
from src.docker.container_manager import ContainerManager, mounts_retired_secrets_tree

OFFICE_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CUBICLE_HOME", tmp_path / "home")
    legacy = tmp_path / "home" / "secrets" / "skills" / "slack"
    legacy.mkdir(parents=True)
    (legacy / "secrets.json").write_text('{"TOKEN": "another-office-value"}')
    directory = tmp_path / "workspace"
    directory.mkdir()
    with runtime.runtime_lock(OFFICE_ID):
        runtime.prepare_runtime(OFFICE_ID, directory)
    monkeypatch.setattr(cm_module, "_ensure_bind_mount_ownership", lambda *a: None)
    return directory


def _bind(source, destination, rw=True):
    return {"Type": "bind", "Source": str(source), "Destination": destination, "RW": rw}


def _office_container(workspace, *, legacy_mount: bool, source=None):
    container = MagicMock()
    container.id = "immutable-container-id"
    container.short_id = "container"
    container.status = "running"
    container.labels = {"cbcl.office_id": OFFICE_ID}
    container.image.id = "image-id"
    mounts = [
        _bind(workspace, "/workspace"),
        _bind(runtime.claude_auth_dir(OFFICE_ID), "/home/agent/.claude"),
        _bind(runtime.ssh_keys_dir(OFFICE_ID), "/home/agent/.ssh"),
    ]
    if legacy_mount:
        mounts.append(
            _bind(source or paths.CUBICLE_HOME / "secrets", "/secrets", rw=False)
        )
    container.attrs = {
        "Mounts": mounts,
        "Config": {"Env": []},
        "HostConfig": {"Init": True},
    }
    return container


def _client(workspace, existing):
    import docker.errors

    state = {"current": existing}
    created: list[dict] = []

    def get(name):
        if state["current"] is None:
            raise docker.errors.NotFound("absent")
        return state["current"]

    def remove(**kwargs):
        state["current"] = None

    def run(image, **kwargs):
        created.append(kwargs)
        fresh = _office_container(workspace, legacy_mount=False)
        state["current"] = fresh
        return fresh

    if existing is not None:
        existing.remove.side_effect = remove
    client = SimpleNamespace(
        containers=SimpleNamespace(get=get, list=lambda **kwargs: [], run=run),
        images=SimpleNamespace(get=lambda tag: SimpleNamespace(id="image-id")),
    )
    return client, created


def _destinations(volumes: dict) -> set[str]:
    return {entry["bind"] for entry in volumes.values()}


@pytest.mark.asyncio
async def test_new_container_never_mounts_the_legacy_tree(workspace, monkeypatch):
    client, created = _client(workspace, None)
    manager = ContainerManager(use_docker=True)
    monkeypatch.setattr(manager, "_get_client", lambda: client)
    await manager.start_office("office", OFFICE_ID, str(workspace))
    assert len(created) == 1
    volumes = created[0]["volumes"]
    assert "/secrets" not in _destinations(volumes)
    assert str(paths.CUBICLE_HOME / "secrets") not in volumes


@pytest.mark.asyncio
async def test_running_container_with_the_legacy_mount_is_recreated(
    workspace, monkeypatch
):
    existing = _office_container(workspace, legacy_mount=True)
    client, created = _client(workspace, existing)
    manager = ContainerManager(use_docker=True)
    monkeypatch.setattr(manager, "_get_client", lambda: client)
    await manager.start_office("office", OFFICE_ID, str(workspace))
    existing.remove.assert_called_once_with(force=True)
    assert len(created) == 1
    assert "/secrets" not in _destinations(created[0]["volumes"])


@pytest.mark.asyncio
async def test_running_container_without_it_is_reused(workspace, monkeypatch):
    existing = _office_container(workspace, legacy_mount=False)
    client, created = _client(workspace, existing)
    manager = ContainerManager(use_docker=True)
    monkeypatch.setattr(manager, "_get_client", lambda: client)
    result = await manager.start_office("office", OFFICE_ID, str(workspace))
    assert result == "immutable-container-id"
    existing.remove.assert_not_called()
    assert created == []


def test_only_the_legacy_source_counts(workspace, tmp_path):
    assert mounts_retired_secrets_tree(_office_container(workspace, legacy_mount=True))
    elsewhere = tmp_path / "operator-data"
    elsewhere.mkdir()
    operator_mount = _office_container(workspace, legacy_mount=True, source=elsewhere)
    assert not mounts_retired_secrets_tree(operator_mount)
    assert not mounts_retired_secrets_tree(
        _office_container(workspace, legacy_mount=False)
    )
    assert not mounts_retired_secrets_tree(SimpleNamespace(attrs={}))


def test_nothing_in_the_daemon_or_image_reads_the_mount():
    """Pin the premise: no source file names the ``/secrets`` container path."""
    source_root = Path(__file__).resolve().parents[1] / "src"
    offenders = []
    for path in source_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in ('"/secrets"', "'/secrets'", '"/secrets/', "'/secrets/"):
            if needle in text:
                offenders.append(f"{path.relative_to(source_root)}: {needle}")
    assert offenders == ["docker/container_manager.py: \"/secrets\""], offenders
