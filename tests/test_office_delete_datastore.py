"""Deleting an office removes its collection rows; nothing else does.

The collections datastore (``~/.cubicle/data/<slug>.sqlite``) is slug-keyed
host state like the workspace and the Office Secrets file. A deletion used
to leave it behind, so a new office with the same name found the deleted
office's rows under any collection it created with a matching name.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src import daemon, paths
from src.daemon import ProcessModelComponents

SIDECARS = ("", "-wal", "-shm", "-journal")


def _components(name: str, slug: str = "") -> ProcessModelComponents:
    script_runner = MagicMock()
    script_runner._cron_scheduler = None
    script_runner.shutdown = AsyncMock()
    return ProcessModelComponents(
        supervisor=MagicMock(shutdown=AsyncMock()),
        dispatcher=MagicMock(stop=AsyncMock()),
        router=MagicMock(stop=AsyncMock()),
        reporter=MagicMock(),
        script_runner=script_runner,
        watchdog_task=None,
        queue_manager=None,
        tool_proxy=None,
        office_name=name,
        office_slug=slug,
    )


@pytest.fixture
def home(tmp_path, monkeypatch):
    cubicle_home = tmp_path / ".cubicle"
    monkeypatch.setattr(paths, "CUBICLE_HOME", cubicle_home)
    return cubicle_home


def _host_state(home, slug: str) -> dict:
    """Create every slug-keyed file cbcl keeps for an office."""
    workspace = home / "workspaces" / slug
    (workspace / "outputs").mkdir(parents=True)
    (workspace / "outputs" / "report.md").write_text("work")
    secrets_file = home / "office-secrets" / f"{slug}.json"
    secrets_file.parent.mkdir(parents=True, exist_ok=True)
    secrets_file.write_text("{}")
    data_dir = home / "data"
    data_dir.mkdir(exist_ok=True)
    datastore = [data_dir / f"{slug}.sqlite{suffix}" for suffix in SIDECARS]
    for path in datastore:
        path.write_bytes(b"rows")
    return {"workspace": workspace, "secrets": secrets_file, "datastore": datastore}


async def _teardown(connected: dict, office_id: str, *, delete_workspace: bool) -> None:
    from fakeredis.aioredis import FakeRedis

    containers = MagicMock(stop_office=AsyncMock(), delete_private_storage=AsyncMock())
    await daemon._disconnect_office_process_model(
        office_id, connected, containers, FakeRedis(),
        delete_workspace=delete_workspace,
    )


async def test_deletion_removes_the_datastore_and_its_journal_files(home) -> None:
    deleted = _host_state(home, "sales")
    neighbour = _host_state(home, "support")

    await _teardown(
        {"office-1": _components("Sales", "sales")}, "office-1", delete_workspace=True,
    )

    assert not any(path.exists() for path in deleted["datastore"])
    assert all(path.exists() for path in neighbour["datastore"])
    assert (home / "data").is_dir()


async def test_released_office_keeps_its_datastore(home) -> None:
    """Moved, parked or merely missing from discovery: the rows stay."""
    kept = _host_state(home, "sales")

    await _teardown(
        {"office-1": _components("Sales", "sales")}, "office-1", delete_workspace=False,
    )

    assert all(path.exists() for path in kept["datastore"])


async def test_deletion_without_a_datastore_changes_nothing(home) -> None:
    """Most offices never wrote a row: there is no ``data`` directory."""
    await _teardown(
        {"office-1": _components("Sales", "sales")}, "office-1", delete_workspace=True,
    )
    assert not (home / "data").exists()


async def test_live_office_with_the_same_name_keeps_its_datastore(home) -> None:
    """The replacement office connected before the old one's delete arrived."""
    shared = _host_state(home, "sales")
    connected = {
        "old": _components("Sales", "sales"),
        "replacement": _components("Sales", "sales"),
    }

    await _teardown(connected, "old", delete_workspace=True)

    assert all(path.exists() for path in shared["datastore"])
    assert shared["workspace"].exists()
    assert shared["secrets"].exists()


async def test_renamed_live_office_keeps_the_paths_its_pinned_slug_owns(home) -> None:
    """A rename leaves the pinned slug, so the name no longer names the paths.

    The deleted office is called "Ops" (its own files are pinned at
    ``sales``). The live office was created as "Ops", so its files are
    pinned at ``ops``, and was renamed since. The deleted office's
    name-derived cleanup candidate is ``ops``: those paths belong to the
    live office.
    """
    own = _host_state(home, "sales")
    live = _host_state(home, "ops")
    connected = {
        "deleted": _components("Ops", "sales"),
        "live": _components("Operations", "ops"),
    }

    await _teardown(connected, "deleted", delete_workspace=True)

    assert all(path.exists() for path in live["datastore"])
    assert (live["workspace"] / "outputs" / "report.md").exists()
    assert live["secrets"].exists()
    assert not own["workspace"].exists()
    assert not own["secrets"].exists()
    assert not any(path.exists() for path in own["datastore"])
