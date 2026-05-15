"""Tests for daemon process-per-agent model (P2-T10)."""

from __future__ import annotations

import asyncio
import os
import sys
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
        # shutdown. Default to None for offices in HTTP /tool-call mode.
        pmc = ProcessModelComponents(
            supervisor="sup",
            dispatcher="disp",
            router="rtr",
            reporter="rep",
            script_runner="sr",
            watchdog_task=None,
            queue_manager="qm",
            tool_proxy=None,
        )
        assert pmc.supervisor == "sup"
        assert pmc.dispatcher == "disp"
        assert pmc.router == "rtr"
        assert pmc.reporter == "rep"
        assert pmc.script_runner == "sr"
        assert pmc.watchdog_task is None
        assert pmc.queue_manager == "qm"
        assert pmc.tool_proxy is None

    def test_has_expected_field_count(self):
        """Daemon lifecycle components: 8 fields (7 legacy + tool_proxy)."""
        assert len(ProcessModelComponents._fields) == 8

    def test_does_not_contain_manager_or_watchdog_object(self):
        """Daemon only needs lifecycle components, not manager/watchdog refs."""
        assert "manager" not in ProcessModelComponents._fields
        assert "watchdog" not in ProcessModelComponents._fields

    def test_replace_watchdog_task(self):
        pmc = ProcessModelComponents(
            supervisor="sup", dispatcher="disp", router="rtr",
            reporter="rep", script_runner="sr", watchdog_task=None,
            queue_manager="qm", tool_proxy=None,
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
    async def test_uses_default_url_when_not_configured(self):
        import redis.asyncio as aioredis

        mock_client = AsyncMock()
        mock_client.ping = AsyncMock()

        mock_config = MagicMock(spec=[])  # no redis_url attr

        with patch.object(aioredis, "from_url", return_value=mock_client) as mock_from_url:
            await _connect_redis(mock_config)

        mock_from_url.assert_called_once_with(
            "redis://localhost:6379/0", decode_responses=True,
        )

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
        # 3 background tasks: router.start, dispatcher.run, script_runner.monitor_all
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
