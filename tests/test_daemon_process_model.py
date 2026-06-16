"""Tests for daemon process-per-agent model (P2-T10)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.daemon import (
    ProcessModelComponents,
    _connect_redis,
    _connect_office_process_model,
    _run_process_model,
)


# ---------------------------------------------------------------------------
# ProcessModelComponents NamedTuple
# ---------------------------------------------------------------------------


class TestProcessModelComponents:
    """Tests for the ProcessModelComponents NamedTuple."""

    def test_has_expected_fields(self):
        # `tool_proxy` was added when the WS-based MCP tool proxy landed
        # as a parallel lifecycle object the daemon must close on
        # shutdown. `office_name` was added in 0.2.45 so the disconnect
        # path can derive the workspace slug even when the orchestrator's
        # sync_config never arrived.
        pmc = ProcessModelComponents(
            supervisor="sup",
            dispatcher="disp",
            router="rtr",
            reporter="rep",
            script_runner="sr",
            watchdog_task=None,
            queue_manager="qm",
            tool_proxy=None,
            office_name="Office Name",
        )
        assert pmc.supervisor == "sup"
        assert pmc.dispatcher == "disp"
        assert pmc.router == "rtr"
        assert pmc.reporter == "rep"
        assert pmc.script_runner == "sr"
        assert pmc.watchdog_task is None
        assert pmc.queue_manager == "qm"
        assert pmc.tool_proxy is None
        assert pmc.office_name == "Office Name"
        # monitor_task defaults to None (T8.2.1 re-review — cancelled on
        # office teardown so the supervised script-monitor doesn't leak).
        assert pmc.monitor_task is None

    def test_has_expected_field_count(self):
        """Daemon lifecycle components: 10 fields (8 legacy + office_name +
        monitor_task)."""
        assert len(ProcessModelComponents._fields) == 10

    def test_does_not_contain_manager_or_watchdog_object(self):
        """Daemon only needs lifecycle components, not manager/watchdog refs."""
        assert "manager" not in ProcessModelComponents._fields
        assert "watchdog" not in ProcessModelComponents._fields

    def test_replace_watchdog_task(self):
        pmc = ProcessModelComponents(
            supervisor="sup", dispatcher="disp", router="rtr",
            reporter="rep", script_runner="sr", watchdog_task=None,
            queue_manager="qm", tool_proxy=None, office_name="o",
        )
        task = MagicMock()
        pmc2 = pmc._replace(watchdog_task=task)
        assert pmc2.watchdog_task is task
        assert pmc.watchdog_task is None  # original unchanged


# ---------------------------------------------------------------------------
# _connect_redis
# ---------------------------------------------------------------------------


class TestConnectRedis:
    """Tests for _connect_redis helper."""

    @pytest.mark.asyncio
    async def test_connects_to_redis(self):
        import redis.asyncio as aioredis

        mock_client = AsyncMock()
        mock_client.ping = AsyncMock()

        mock_config = MagicMock()
        mock_config.redis_url = "redis://testhost:6379/1"

        with patch.object(aioredis, "from_url", return_value=mock_client) as mock_from_url:
            result = await _connect_redis(mock_config)

        mock_from_url.assert_called_once_with(
            "redis://testhost:6379/1", decode_responses=True,
        )
        mock_client.ping.assert_awaited_once()
        assert result is mock_client

    @pytest.mark.asyncio
    async def test_uses_in_process_when_not_configured(self):
        """When ``config.redis_url`` is missing or empty, the daemon
        uses an in-process FakeRedis instead of trying to reach
        an external Redis server. Validates the architectural
        constraint that cbcl spawns NO host services beyond office
        containers.
        """
        from src.local_redis import reset_for_tests

        reset_for_tests()  # drop any cached singleton from prior tests

        mock_config = MagicMock(spec=[])  # no redis_url attr

        client = await _connect_redis(mock_config)

        # FakeRedis is a real async-Redis-shaped client we can ping.
        assert await client.ping()
        # And it's actually FakeRedis, not a connection to a host port.
        from fakeredis.aioredis import FakeRedis

        assert isinstance(client, FakeRedis)

    async def test_uses_in_process_when_redis_url_empty_string(self):
        """An explicit empty string also routes through in-process."""
        from src.local_redis import reset_for_tests

        reset_for_tests()

        mock_config = MagicMock()
        mock_config.redis_url = ""

        client = await _connect_redis(mock_config)
        assert await client.ping()
        from fakeredis.aioredis import FakeRedis

        assert isinstance(client, FakeRedis)

    @pytest.mark.asyncio
    async def test_raises_on_ping_failure(self):
        import redis.asyncio as aioredis

        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(side_effect=ConnectionError("refused"))

        mock_config = MagicMock()
        mock_config.redis_url = "redis://badhost:6379/0"

        with patch.object(aioredis, "from_url", return_value=mock_client):
            with pytest.raises(ConnectionError):
                await _connect_redis(mock_config)


# ---------------------------------------------------------------------------
# _start_foreground branching
# ---------------------------------------------------------------------------


class TestStartForeground:
    """Tests that _start_foreground calls _run_process_model."""

    @patch("src.daemon.asyncio")
    @patch("src.daemon._setup_logging_foreground")
    def test_calls_run_process_model(self, mock_logging, mock_asyncio):
        from src.daemon import _start_foreground

        config = MagicMock()
        _start_foreground(config)

        mock_asyncio.run.assert_called_once()


# ---------------------------------------------------------------------------
# _connect_office_process_model
# ---------------------------------------------------------------------------


class TestConnectOfficeProcessModel:
    """Tests for _connect_office_process_model."""

    @pytest.mark.asyncio
    async def test_creates_components_and_starts_them(self):
        office = MagicMock()
        office.id = "off1"
        office.name = "Test Office"
        config = MagicMock()
        containers = AsyncMock()
        containers.get_container_name.return_value = "cbcl-office-test"
        redis_client = AsyncMock()
        connected: dict = {}
        background_tasks: list = []

        mock_oc = MagicMock()
        mock_oc.supervisor = MagicMock()
        mock_oc.dispatcher = MagicMock()
        mock_oc.router = MagicMock()
        mock_oc.reporter = MagicMock()
        mock_oc.script_runner = MagicMock()
        mock_oc.watchdog = MagicMock()
        mock_oc.router.start = AsyncMock()
        mock_oc.dispatcher.run = AsyncMock()
        mock_oc.script_runner.monitor_all = AsyncMock()
        mock_oc.watchdog.run = AsyncMock()
        mock_oc.manager = AsyncMock()
        mock_oc.manager.start = AsyncMock()

        with patch(
            "src.handlers.init_office_process_model",
            new_callable=AsyncMock,
            return_value=mock_oc,
        ):
            await _connect_office_process_model(
                office, config, containers, redis_client,
                connected, background_tasks,
            )

        assert "off1" in connected
        pmc = connected["off1"]
        assert isinstance(pmc, ProcessModelComponents)
        assert pmc.supervisor is mock_oc.supervisor
        assert pmc.dispatcher is mock_oc.dispatcher
        assert pmc.router is mock_oc.router
        assert pmc.reporter is mock_oc.reporter
        assert pmc.script_runner is mock_oc.script_runner
        assert pmc.watchdog_task is not None  # asyncio.Task created
        mock_oc.reporter.start.assert_called_once()
        # 3 background tasks: router.start, dispatcher.run,
        # script_runner.monitor_all. The 0.2.48 ``global_sweep``
        # fallback was removed in 0.2.49 — its job (deliver
        # notify_manager + execution-status from in-container MCP
        # runs) is now done by the primary path itself, which calls
        # the backend via the standard ``_call_backend`` helper with
        # proxy → direct-backend fallback + 3-retry behaviour.
        assert len(background_tasks) == 3

    @pytest.mark.asyncio
    async def test_handles_container_failure(self):
        """Office connection failure should log error, not crash."""
        office = MagicMock()
        office.id = "off1"
        office.name = "Failing Office"
        config = MagicMock()
        containers = AsyncMock()
        containers.ensure_container = AsyncMock(side_effect=RuntimeError("docker down"))
        connected: dict = {}
        background_tasks: list = []

        # Should not raise
        await _connect_office_process_model(
            office, config, containers, MagicMock(),
            connected, background_tasks,
        )

        assert "off1" not in connected


# ---------------------------------------------------------------------------
# _run_process_model shutdown
# ---------------------------------------------------------------------------


class TestRunProcessModelShutdown:
    """Tests for the shutdown sequence in _run_process_model."""

    @pytest.mark.asyncio
    async def test_shutdown_closes_redis_and_containers(self):
        """Run _run_process_model with immediate shutdown; verify cleanup."""
        import redis.asyncio as aioredis

        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock()
        mock_redis.aclose = AsyncMock()

        config = MagicMock()
        config.anthropic_api_key = "test-key"

        with (
            patch.object(aioredis, "from_url", return_value=mock_redis),
            patch("src.daemon.set_api_key"),
            patch("src.daemon.ContainerManager") as mock_cm_cls,
            patch("src.daemon._discover_offices", return_value=[]),
            patch("src.daemon.get_pid_path") as mock_pid,
        ):
            mock_cm = mock_cm_cls.return_value
            mock_cm.ensure_image = AsyncMock()
            mock_cm.ensure_redis = AsyncMock()
            mock_cm.stop_all = AsyncMock()
            mock_cm.health_check_all = AsyncMock()
            mock_pid.return_value = MagicMock()
            mock_pid.return_value.unlink = MagicMock()

            # Capture the signal handler and call it to trigger shutdown
            captured_handlers = []
            loop = asyncio.get_running_loop()

            def spy_add_signal_handler(sig, handler):
                captured_handlers.append(handler)

            with patch.object(loop, "add_signal_handler", side_effect=spy_add_signal_handler):
                task = asyncio.create_task(_run_process_model(config))
                await asyncio.sleep(0.05)  # Let startup complete

                # Trigger first signal → graceful shutdown
                assert len(captured_handlers) >= 1
                captured_handlers[0]()

                # Wait for shutdown to complete
                await asyncio.sleep(0.1)
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

        mock_redis.aclose.assert_awaited_once()
        mock_cm.stop_all.assert_awaited_once()


class TestDoubleSignalHandler:
    """Test that a second signal forces immediate exit."""

    @pytest.mark.asyncio
    async def test_second_signal_exits(self):
        """Simulate two signal handler invocations — second should sys.exit(1)."""
        import redis.asyncio as aioredis

        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock()
        mock_redis.aclose = AsyncMock()

        config = MagicMock()
        config.anthropic_api_key = "key"

        signal_handler_ref = []

        original_add = asyncio.get_event_loop().add_signal_handler

        def capture_handler(sig, handler):
            signal_handler_ref.append(handler)

        with (
            patch.object(aioredis, "from_url", return_value=mock_redis),
            patch("src.daemon.set_api_key"),
            patch("src.daemon.ContainerManager") as mock_cm_cls,
            patch("src.daemon._discover_offices", return_value=[]),
            patch("src.daemon.get_pid_path") as mock_pid,
        ):
            mock_cm = mock_cm_cls.return_value
            mock_cm.ensure_image = AsyncMock()
            mock_cm.ensure_redis = AsyncMock()
            mock_cm.stop_all = AsyncMock()
            mock_cm.health_check_all = AsyncMock()
            mock_pid.return_value = MagicMock()
            mock_pid.return_value.unlink = MagicMock()

            loop = asyncio.get_running_loop()
            original_add_handler = loop.add_signal_handler

            # Capture the signal handler
            captured_handlers = []

            def spy_add_signal_handler(sig, handler):
                captured_handlers.append(handler)
                # Don't actually register — we'll call it manually

            with patch.object(loop, "add_signal_handler", side_effect=spy_add_signal_handler):
                # Start _run_process_model in a task
                task = asyncio.create_task(_run_process_model(config))
                await asyncio.sleep(0.05)  # Let it start

                # Should have registered 2 handlers (SIGINT, SIGTERM) — same fn
                assert len(captured_handlers) >= 1
                handler = captured_handlers[0]

                # First call: sets shutdown_event
                handler()
                await asyncio.sleep(0.05)

                # Second call: forces exit
                with pytest.raises(SystemExit) as exc_info:
                    handler()
                assert exc_info.value.code == 1

                # Clean up the task
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass


# ---------------------------------------------------------------------------
# Office-deletion host-state cleanup (workspace + secrets)
# ---------------------------------------------------------------------------


class TestDisconnectWorkspaceCleanup:
    """A true office DELETE (delete_workspace=True, the office_deleted push)
    wipes the per-office host state; a reconcile-disconnect (office merely
    missing from discovery — parked/reassigned, delete_workspace=False)
    must PRESERVE it."""

    def _components(self):
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
            office_name="Teardown Test Office",
        )

    async def _run(self, tmp_path, monkeypatch, *, delete_workspace):
        from fakeredis.aioredis import FakeRedis
        from src import daemon, paths

        cubicle_home = tmp_path / ".cubicle"
        monkeypatch.setattr(paths, "CUBICLE_HOME", cubicle_home)
        slug = "teardown-test-office"  # slugify("Teardown Test Office")
        ws = cubicle_home / "workspaces" / slug
        (ws / "outputs").mkdir(parents=True)
        (ws / "outputs" / "stale.txt").write_text("old task output")
        (ws / ".claude-auth").mkdir()
        (ws / ".claude-auth" / ".credentials.json").write_text("{}")
        secrets_dir = cubicle_home / "office-secrets"
        secrets_dir.mkdir(parents=True)
        secrets_file = secrets_dir / f"{slug}.json"
        secrets_file.write_text('{"OPENAI_API_KEY": "sk-x"}')

        oid = "office-1"
        connected = {oid: self._components()}
        containers = MagicMock(stop_office=AsyncMock())
        redis = FakeRedis()
        await daemon._disconnect_office_process_model(
            oid, connected, containers, redis,
            delete_workspace=delete_workspace,
        )
        containers.stop_office.assert_awaited_once_with(oid)
        assert oid not in connected  # always dropped from the registry
        return ws, secrets_file

    async def test_true_delete_wipes_workspace_and_secrets(
        self, tmp_path, monkeypatch,
    ):
        ws, secrets_file = await self._run(
            tmp_path, monkeypatch, delete_workspace=True,
        )
        assert not ws.exists(), "workspace dir must be removed on true delete"
        assert not secrets_file.exists(), (
            "office-secrets file must be removed on true delete"
        )

    async def test_reconcile_preserves_workspace_and_secrets(
        self, tmp_path, monkeypatch,
    ):
        ws, secrets_file = await self._run(
            tmp_path, monkeypatch, delete_workspace=False,
        )
        assert ws.exists() and (ws / "outputs" / "stale.txt").exists(), (
            "a parked/reassigned office (reconcile path) must keep its workspace"
        )
        assert secrets_file.exists(), (
            "a parked/reassigned office must keep its office-secrets"
        )
