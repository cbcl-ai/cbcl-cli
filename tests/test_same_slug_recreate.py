"""Regression tests for the same-name office delete→recreate race.

Incident (cbcl-stg, 2026-09-02 12:03): an office was deleted and a new
one with the SAME NAME created moments later. Container names
(``cbcl-office-{slug}``) and workspace paths are slug-derived, so:

  * the new office's connect ADOPTED the old office's still-running
    container by name;
  * the old office's in-flight teardown then removed that container
    (Phase 5) and rmtree'd the shared workspace (Phase 6);
  * the new office's Claude sign-in failed ("The office container is
    not running") and ``force_restart_office`` 404-looped on the stale
    container id "until an operator intervenes".

The fix has three layers, each pinned here:

  1. ``src.office_slug_lock`` — teardown and connect serialize on a
     per-slug lifecycle lock.
  2. Ownership by label — ``start_office`` never adopts a running
     container whose ``cbcl.office_id`` label names another office;
     ``stop_office`` never removes one; teardown's destructive
     host-state cleanup skips slugs a live office claims.
  3. Self-heal — ``force_restart_office`` recreates from the stored
     ``OfficeConfig`` when the tracked container no longer exists.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import OfficeConfig
from src.daemon import ProcessModelComponents
from src.docker.container_manager import ContainerManager
from src.office_slug_lock import slug_lifecycle_lock


# ---------------------------------------------------------------------------
# Layer 1 — the slug lock registry + serialization
# ---------------------------------------------------------------------------


class TestSlugLockRegistry:
    def test_same_slug_same_lock(self) -> None:
        assert slug_lifecycle_lock("alpha") is slug_lifecycle_lock("alpha")

    def test_different_slug_different_lock(self) -> None:
        assert slug_lifecycle_lock("alpha") is not slug_lifecycle_lock("beta")


def _components(name: str) -> ProcessModelComponents:
    sr = MagicMock()
    sr._cron_scheduler = None
    sr.shutdown = AsyncMock()
    return ProcessModelComponents(
        supervisor=MagicMock(shutdown=AsyncMock()),
        dispatcher=MagicMock(stop=AsyncMock()),
        router=MagicMock(stop=AsyncMock()),
        reporter=MagicMock(),
        script_runner=sr,
        watchdog_task=None,
        queue_manager=None,
        tool_proxy=None,
        office_name=name,
    )


def _workspace_fixture(tmp_path, monkeypatch, slug: str):
    """Point CUBICLE_HOME at a tmp dir with a populated slug workspace
    + office-secrets file; return (workspace_dir, secrets_file)."""
    from src import paths

    cubicle_home = tmp_path / ".cubicle"
    monkeypatch.setattr(paths, "CUBICLE_HOME", cubicle_home)
    ws = cubicle_home / "workspaces" / slug
    (ws / ".claude-auth").mkdir(parents=True)
    (ws / ".claude-auth" / ".credentials.json").write_text("{}")
    secrets_dir = cubicle_home / "office-secrets"
    secrets_dir.mkdir(parents=True)
    secrets_file = secrets_dir / f"{slug}.json"
    secrets_file.write_text('{"KEY": "v"}')
    return ws, secrets_file


class TestTeardownHoldsSlugLock:
    @pytest.mark.asyncio
    async def test_teardown_waits_for_held_slug_lock(
        self, tmp_path, monkeypatch,
    ) -> None:
        """A teardown must not start its body (pop from ``connected``,
        remove the container, wipe the workspace) while another
        lifecycle operation holds the office slug's lock."""
        from fakeredis.aioredis import FakeRedis
        from src import daemon

        slug = "race-office"
        ws, secrets_file = _workspace_fixture(tmp_path, monkeypatch, slug)
        oid = "old-office"
        connected = {oid: _components("Race Office")}
        containers = MagicMock(stop_office=AsyncMock())
        redis = FakeRedis()

        lock = slug_lifecycle_lock(slug)
        await lock.acquire()
        try:
            task = asyncio.create_task(
                daemon._disconnect_office_process_model(
                    oid, connected, containers, redis,
                    delete_workspace=True,
                )
            )
            await asyncio.sleep(0.1)
            assert oid in connected, (
                "teardown body ran while the slug lifecycle lock was held"
            )
            assert ws.exists()
        finally:
            lock.release()
        await asyncio.wait_for(task, timeout=5)
        assert oid not in connected
        assert not ws.exists()
        assert not secrets_file.exists()

    @pytest.mark.asyncio
    async def test_connect_waits_for_held_slug_lock(self) -> None:
        """A connect must not touch the container (``ensure_container``)
        while the slug's lifecycle lock is held — e.g. by the teardown
        of a just-deleted office with the same name."""
        from src import daemon

        office = OfficeConfig(id="new-office", name="Race Office Two")
        lock = slug_lifecycle_lock(office.slug)
        # ensure_container raising ends the connect early (caught +
        # logged inside), which keeps this test independent of the
        # heavy init path — the contract under test is only the
        # ordering: no container work until the lock is free.
        containers = MagicMock(
            ensure_container=AsyncMock(side_effect=RuntimeError("stop here")),
            get_container_name=MagicMock(return_value=""),
        )
        connecting: set[str] = set()

        await lock.acquire()
        try:
            task = asyncio.create_task(
                daemon._connect_office_process_model(
                    office, config=MagicMock(platform_url="http://x"),
                    containers=containers, redis_client=None,
                    connected={}, background_tasks=[],
                    connecting=connecting,
                )
            )
            await asyncio.sleep(0.1)
            containers.ensure_container.assert_not_awaited()
        finally:
            lock.release()
        await asyncio.wait_for(task, timeout=5)
        containers.ensure_container.assert_awaited_once()
        assert not lock.locked(), "connect must release the slug lock"
        assert "new-office" not in connecting


class TestTeardownOwnershipGuard:
    @pytest.mark.asyncio
    async def test_wipe_skipped_when_live_office_claims_slug(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Reverse ordering: the same-name replacement office connected
        FIRST, then the old office's delete arrived. The teardown must
        NOT wipe the slug-keyed workspace/secrets the live office now
        owns."""
        from fakeredis.aioredis import FakeRedis
        from src import daemon

        slug = "shared-name-office"
        ws, secrets_file = _workspace_fixture(tmp_path, monkeypatch, slug)
        old_oid, new_oid = "old-office", "new-office"
        connected = {
            old_oid: _components("Shared Name Office"),
            new_oid: _components("Shared Name Office"),
        }
        containers = MagicMock(stop_office=AsyncMock())
        redis = FakeRedis()

        await daemon._disconnect_office_process_model(
            old_oid, connected, containers, redis, delete_workspace=True,
        )
        assert old_oid not in connected
        assert new_oid in connected
        assert ws.exists(), (
            "workspace wiped although a live same-name office owns it"
        )
        assert secrets_file.exists()


# ---------------------------------------------------------------------------
# Layer 2 — container ownership by label
# ---------------------------------------------------------------------------


class TestStartOfficeOwnership:
    def _make_cm(self, monkeypatch, existing):
        cm = ContainerManager(use_docker=True)
        run_calls: list[dict] = []
        fresh = MagicMock()
        fresh.id = "fresh-cid"
        fresh.short_id = "fresh"

        class _FakeContainers:
            def get(self, name):
                return existing

            def run(self, image, **kwargs):
                run_calls.append(kwargs)
                return fresh

        class _FakeImages:
            def get(self, tag):
                img = MagicMock()
                img.id = "img-current"
                return img

        class _FakeClient:
            containers = _FakeContainers()
            images = _FakeImages()

        monkeypatch.setattr(cm, "_get_client", lambda: _FakeClient())
        return cm, run_calls, fresh

    @pytest.mark.asyncio
    async def test_foreign_labeled_container_is_replaced_not_adopted(
        self, monkeypatch, tmp_path,
    ) -> None:
        existing = MagicMock()
        existing.status = "running"
        existing.labels = {"cbcl.office_id": "old-office"}
        cm, run_calls, fresh = self._make_cm(monkeypatch, existing)

        cid = await cm.start_office(
            office_slug="same-slug", office_id="new-office",
            workspace_path=str(tmp_path / "ws"),
        )
        existing.remove.assert_called_once_with(force=True)
        assert run_calls, "a fresh container must be created"
        assert cid == "fresh-cid"
        assert cm._containers["new-office"] is fresh

    @pytest.mark.asyncio
    async def test_own_labeled_container_is_adopted(
        self, monkeypatch, tmp_path,
    ) -> None:
        existing = MagicMock()
        existing.status = "running"
        existing.id = "existing-cid"
        existing.labels = {"cbcl.office_id": "same-office"}
        existing.image.id = "img-current"
        cm, run_calls, _fresh = self._make_cm(monkeypatch, existing)

        cid = await cm.start_office(
            office_slug="same-slug", office_id="same-office",
            workspace_path=str(tmp_path / "ws"),
        )
        existing.remove.assert_not_called()
        assert not run_calls
        assert cid == "existing-cid"

    @pytest.mark.asyncio
    async def test_unlabeled_container_stays_adoptable(
        self, monkeypatch, tmp_path,
    ) -> None:
        """Back-compat: containers created by a pre-label cbcl carry no
        ``cbcl.office_id`` — they must adopt exactly as before."""
        existing = MagicMock()
        existing.status = "running"
        existing.id = "existing-cid"
        existing.labels = {}
        existing.image.id = "img-current"
        cm, run_calls, _fresh = self._make_cm(monkeypatch, existing)

        cid = await cm.start_office(
            office_slug="same-slug", office_id="any-office",
            workspace_path=str(tmp_path / "ws"),
        )
        existing.remove.assert_not_called()
        assert not run_calls
        assert cid == "existing-cid"


class TestStopOfficeOwnership:
    @pytest.mark.asyncio
    async def test_foreign_labeled_container_left_running(self) -> None:
        cm = ContainerManager(use_docker=True)
        container = MagicMock()
        container.labels = {"cbcl.office_id": "new-office"}
        container.name = "cbcl-office-same-slug"
        cm._containers["old-office"] = container

        await cm.stop_office("old-office")
        container.stop.assert_not_called()
        container.remove.assert_not_called()
        assert "old-office" not in cm._containers

    @pytest.mark.asyncio
    async def test_gone_container_is_quiet_noop(self) -> None:
        import docker.errors

        cm = ContainerManager(use_docker=True)
        container = MagicMock()
        container.reload.side_effect = docker.errors.NotFound("gone")
        cm._containers["old-office"] = container

        await cm.stop_office("old-office")
        container.stop.assert_not_called()
        container.remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_own_container_still_stopped_and_removed(self) -> None:
        cm = ContainerManager(use_docker=True)
        container = MagicMock()
        container.labels = {"cbcl.office_id": "office-1"}
        cm._containers["office-1"] = container
        cm._office_configs["office-1"] = OfficeConfig(
            id="office-1", name="Office One",
        )

        await cm.stop_office("office-1")
        container.stop.assert_called_once()
        container.remove.assert_called_once()
        assert "office-1" not in cm._office_configs


# ---------------------------------------------------------------------------
# Layer 3 — force_restart_office self-heal
# ---------------------------------------------------------------------------


class TestForceRestartSelfHeal:
    @pytest.mark.asyncio
    async def test_gone_container_recreates_from_stored_config(self) -> None:
        import docker.errors

        cm = ContainerManager(use_docker=True)
        container = MagicMock()
        container.reload.side_effect = docker.errors.NotFound("gone")
        cm._containers["office-1"] = container
        cfg = OfficeConfig(id="office-1", name="Office One")
        cm._office_configs["office-1"] = cfg
        recreate = AsyncMock()
        cm.recreate_office = recreate  # type: ignore[method-assign]

        await cm.force_restart_office("office-1")
        recreate.assert_awaited_once_with(cfg)

    @pytest.mark.asyncio
    async def test_gone_container_without_config_drops_tracking(self) -> None:
        import docker.errors

        cm = ContainerManager(use_docker=True)
        container = MagicMock()
        container.reload.side_effect = docker.errors.NotFound("gone")
        cm._containers["office-1"] = container

        await cm.force_restart_office("office-1")
        assert "office-1" not in cm._containers
