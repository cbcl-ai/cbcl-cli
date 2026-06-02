"""Tests for process-per-agent handler init (P2-T10 handlers.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.handlers import ProcessModelOfficeComponents, _register_process_model_handlers


# ---------------------------------------------------------------------------
# NamedTuple
# ---------------------------------------------------------------------------


class TestProcessModelOfficeComponents:
    """Tests for the ProcessModelOfficeComponents NamedTuple."""

    def test_has_expected_fields(self):
        # tool_proxy was added to support the WS-based tool-proxy mode
        # (non-legacy MCP call path). It's `None` when the office runs
        # in the default HTTP /tool-call mode.
        pmc = ProcessModelOfficeComponents(
            supervisor="sup",
            dispatcher="disp",
            router="rtr",
            reporter="rep",
            script_runner="sr",
            manager="mgr",
            watchdog="wd",
            queue_manager="qm",
            tool_proxy=None,
        )
        assert pmc.supervisor == "sup"
        assert pmc.dispatcher == "disp"
        assert pmc.router == "rtr"
        assert pmc.reporter == "rep"
        assert pmc.script_runner == "sr"
        assert pmc.manager == "mgr"
        assert pmc.watchdog == "wd"
        assert pmc.queue_manager == "qm"
        assert pmc.tool_proxy is None

    def test_has_expected_field_count(self):
        assert len(ProcessModelOfficeComponents._fields) == 9


# ---------------------------------------------------------------------------
# _register_process_model_handlers
# ---------------------------------------------------------------------------


class TestRegisterProcessModelHandlers:
    """Tests for _register_process_model_handlers."""

    def _make_components(self):
        """Create mock components for handler registration.

        Keys mirror the keyword args accepted by
        _register_process_model_handlers. The former `skill_assembler`
        positional slot was removed when skill-sync moved into
        workspace_setup; tests historically passed it and silently
        shifted every subsequent argument by one, causing the
        supervisor / queue_manager mocks to land in the wrong slots.
        """
        return dict(
            router=MagicMock(),
            config_store=MagicMock(),
            script_syncer=MagicMock(),
            claude_md_writer=MagicMock(),
            mgr=MagicMock(),
            supervisor=MagicMock(),
            dispatcher=MagicMock(),
            script_runner=MagicMock(),
            secrets_store=MagicMock(),
            queue_manager=MagicMock(),
        )

    def _register(self, **overrides):
        comps = self._make_components()
        comps.update(overrides)
        router = comps.pop("router")
        _register_process_model_handlers(router, **comps)
        return router, comps

    def test_registers_all_expected_message_types(self):
        router, _ = self._register()
        registered_types = {call.args[0] for call in router.on.call_args_list}
        # The set below is the CORE subset that must always be
        # registered for the process model to function. Tests assert
        # subset — not equality — so new message types (mcp_*, setup
        # handlers) don't force a test update on every feature addition.
        required = {
            "chat_message", "switch_context",
            "task_ready", "task_rework", "task_updated", "task_moved",
            "sync_config", "script_execute", "script_secret_update",
            "skill_secret_update", "task_kill",
        }
        missing = required - registered_types
        assert not missing, f"Missing required handlers: {missing}"

    def test_chat_message_handler_is_manager_method(self):
        mgr = MagicMock()
        router, _ = self._register(mgr=mgr)
        chat_call = [c for c in router.on.call_args_list if c.args[0] == "chat_message"]
        assert chat_call[0].args[1] is mgr.handle_chat_message

    def test_switch_context_handler_is_manager_method(self):
        mgr = MagicMock()
        router, _ = self._register(mgr=mgr)
        calls = [c for c in router.on.call_args_list if c.args[0] == "switch_context"]
        assert calls[0].args[1] is mgr.handle_switch_context


# ---------------------------------------------------------------------------
# Individual handler function behavior
# ---------------------------------------------------------------------------


class TestHandleSyncConfig:
    """Tests for the sync_config handler registered by process model."""

    @pytest.mark.asyncio
    async def test_sync_config_calls_all_sync_steps(self):
        # Skill sync was merged into workspace_setup.sync_agent_workspaces,
        # so the `skill_assembler` step is no longer part of the handler
        # contract. We keep the rest of the pipeline (config store,
        # script syncer, claude_md_writer, dispatcher.wake) as invariants.
        config_store = AsyncMock()
        script_syncer = AsyncMock()
        claude_md_writer = MagicMock()
        dispatcher = MagicMock()
        workspace_setup = MagicMock()

        router = MagicMock()

        _register_process_model_handlers(
            router,
            config_store,
            script_syncer,
            claude_md_writer,
            MagicMock(),        # mgr
            MagicMock(),        # supervisor
            dispatcher,
            MagicMock(),        # script_runner
            MagicMock(),        # secrets_store
            MagicMock(),        # queue_manager
            workspace_setup=workspace_setup,
        )

        # Extract the sync_config handler
        handler = next(
            c.args[1] for c in router.on.call_args_list
            if c.args[0] == "sync_config"
        )

        msg = {"config": {"agents": [{"name": "analyst"}]}}
        await handler(msg)

        config_store.update_from_sync.assert_awaited_once_with(msg)
        script_syncer.sync_from_config.assert_awaited_once_with(msg)
        claude_md_writer.sync_all.assert_called_once_with({"agents": [{"name": "analyst"}]})
        workspace_setup.sync_agent_workspaces.assert_called_once_with(
            [{"name": "analyst"}]
        )
        dispatcher.wake.assert_called_once()


class TestHandleTaskReady:
    """Tests for the task_ready handler registered by process model."""

    @pytest.mark.asyncio
    async def test_task_ready_adds_to_dispatcher(self):
        dispatcher = AsyncMock()
        router = MagicMock()

        _register_process_model_handlers(
            router,
            MagicMock(), MagicMock(), MagicMock(),  # config_store, script_syncer, claude_md_writer
            MagicMock(),                             # mgr
            MagicMock(),                             # supervisor
            dispatcher,                              # dispatcher (pos 7)
            MagicMock(), MagicMock(), MagicMock(),  # script_runner, secrets_store, queue_manager
        )

        handler = next(
            c.args[1] for c in router.on.call_args_list
            if c.args[0] == "task_ready"
        )

        msg = {"task_data": {"task_id": "t1", "assigned_agent": "analyst"}}
        await handler(msg)

        dispatcher.add_task.assert_awaited_once_with(
            {"task_id": "t1", "assigned_agent": "analyst"},
        )

    @pytest.mark.asyncio
    async def test_task_ready_falls_back_to_msg_itself(self):
        """When task_data key is absent, uses the msg dict as task_data."""
        dispatcher = AsyncMock()
        router = MagicMock()

        _register_process_model_handlers(
            router,
            MagicMock(), MagicMock(), MagicMock(),
            MagicMock(), MagicMock(), dispatcher,
            MagicMock(), MagicMock(), MagicMock(),
        )

        handler = next(
            c.args[1] for c in router.on.call_args_list
            if c.args[0] == "task_ready"
        )

        msg = {"task_id": "t1", "assigned_agent": "analyst"}
        await handler(msg)

        dispatcher.add_task.assert_awaited_once_with(msg)


class TestHandleTaskRework:
    """Tests for the task_rework handler registered by process model."""

    @pytest.mark.asyncio
    async def test_task_rework_constructs_correct_data(self):
        dispatcher = AsyncMock()
        router = MagicMock()

        _register_process_model_handlers(
            router,
            MagicMock(), MagicMock(), MagicMock(),  # config_store, script_syncer, claude_md_writer
            MagicMock(),                             # mgr
            MagicMock(),                             # supervisor
            dispatcher,                              # dispatcher
            MagicMock(), MagicMock(), MagicMock(),  # script_runner, secrets_store, queue_manager
        )

        handler = next(
            c.args[1] for c in router.on.call_args_list
            if c.args[0] == "task_rework"
        )

        msg = {
            "task_id": "t99",
            "readable_id": "WR-001.T05",
            "assigned_agent": "developer",
            "priority": "high",
            "feedback": "Fix the tests",
            "rework_count": 2,
        }
        await handler(msg)

        expected = {
            "task_id": "t99",
            "readable_id": "WR-001.T05",
            "title": "",
            "assigned_agent": "developer",
            # `reviewer` and `prior_session_id` were added to the rework
            # dispatch payload to support designated-reviewer routing
            # and session continuity across rework cycles. Both default
            # to empty when the caller doesn't supply them.
            "reviewer": "",
            "priority": "high",
            "brief": {},
            "rework_feedback": "Fix the tests",
            "rework_count": 2,
            "workstream_name": "",
            # Added so workers can build the per-workstream output path
            # `/workspace/outputs/{short_code}/...` directly from the
            # rework dispatch payload without an extra round-trip.
            "workstream_short_code": "",
            # Scope context propagated so the worker's CUBICLE_OUTPUT_DIR
            # stays consistent across review→rework cycles. QA round 5 H1.
            "scope_id": None,
            "scope_readable_id": None,
            "status": "ready",
            "prior_session_id": "",
        }
        dispatcher.add_task.assert_awaited_once_with(expected)


class TestHandleTaskKill:
    """Tests for the task_kill handler registered by process model."""

    @pytest.mark.asyncio
    async def test_task_kill_kills_agent_and_removes_task(self):
        supervisor = MagicMock()
        supervisor._kill_process = AsyncMock()
        queue_manager = AsyncMock()
        router = MagicMock()

        _register_process_model_handlers(
            router,
            MagicMock(), MagicMock(), MagicMock(),  # config_store, script_syncer, claude_md_writer
            MagicMock(),                             # mgr
            supervisor,                              # supervisor (pos 6)
            MagicMock(),                             # dispatcher
            MagicMock(),                             # script_runner
            MagicMock(),                             # secrets_store
            queue_manager,                           # queue_manager (pos 10)
        )

        handler = next(
            c.args[1] for c in router.on.call_args_list
            if c.args[0] == "task_kill"
        )

        msg = {"task_id": "t1", "agent_name": "developer"}
        await handler(msg)

        supervisor._kill_process.assert_awaited_once_with("developer")
        # ADD-A3: with an agent_name, removal is SCOPED to that agent's
        # queue (so a reviewer's just-routed entry for the same task isn't
        # clobbered) — NOT the broad remove_task_from_all sweep.
        queue_manager.remove_task.assert_awaited_once_with("developer", "t1")
        queue_manager.remove_task_from_all.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_task_kill_without_agent_name_only_removes(self):
        supervisor = MagicMock()
        supervisor._kill_process = AsyncMock()
        queue_manager = AsyncMock()
        router = MagicMock()

        _register_process_model_handlers(
            router,
            MagicMock(), MagicMock(), MagicMock(),  # config_store, script_syncer, claude_md_writer
            MagicMock(),                             # mgr
            supervisor,                              # supervisor (pos 6)
            MagicMock(),                             # dispatcher
            MagicMock(),                             # script_runner
            MagicMock(),                             # secrets_store
            queue_manager,                           # queue_manager (pos 10)
        )

        handler = next(
            c.args[1] for c in router.on.call_args_list
            if c.args[0] == "task_kill"
        )

        msg = {"task_id": "t1", "agent_name": ""}
        await handler(msg)

        supervisor._kill_process.assert_not_awaited()
        queue_manager.remove_task_from_all.assert_awaited_once_with("t1")

    @pytest.mark.asyncio
    async def test_task_kill_handles_supervisor_error(self):
        supervisor = MagicMock()
        supervisor._kill_process = AsyncMock(side_effect=RuntimeError("no process"))
        queue_manager = AsyncMock()
        router = MagicMock()

        _register_process_model_handlers(
            router,
            MagicMock(), MagicMock(), MagicMock(),  # config_store, script_syncer, claude_md_writer
            MagicMock(),                             # mgr
            supervisor,                              # supervisor (pos 6)
            MagicMock(),                             # dispatcher
            MagicMock(),                             # script_runner
            MagicMock(),                             # secrets_store
            queue_manager,                           # queue_manager (pos 10)
        )

        handler = next(
            c.args[1] for c in router.on.call_args_list
            if c.args[0] == "task_kill"
        )

        msg = {"task_id": "t1", "agent_name": "developer"}
        # Should not raise
        await handler(msg)

        # ADD-A3: scoped removal even when the kill itself errored.
        queue_manager.remove_task.assert_awaited_once_with("developer", "t1")


# ---------------------------------------------------------------------------
# init_office_process_model
# ---------------------------------------------------------------------------


class TestInitOfficeProcessModel:
    """Tests for init_office_process_model."""

    @pytest.mark.asyncio
    async def test_creates_all_components(self):
        from src.handlers import init_office_process_model

        office = MagicMock()
        office.id = "test-office"
        office.workspace_path = "/tmp/test-workspace"

        mock_redis = AsyncMock()
        mock_supervisor = MagicMock()
        mock_dispatcher = MagicMock()
        mock_router = MagicMock()
        mock_manager = MagicMock()
        mock_reporter = MagicMock()
        mock_watchdog = MagicMock()

        mock_sm = MagicMock()
        mock_sm.init_from_disk = AsyncMock()
        mock_sr = MagicMock()
        mock_sr.cleanup_orphaned_run_files.return_value = 0

        # SkillAssembler was folded into WorkspaceSetup (skill-sync via
        # symlinks). The patch list is therefore reduced — skipping a
        # patch for a symbol that no longer exists on the module would
        # raise AttributeError at fixture entry.
        with (
            patch("src.handlers.WorkspaceSetup"),
            patch("src.handlers.ConfigStore"),
            patch("src.handlers.ScriptSyncer"),
            patch("src.handlers.ClaudeMdWriter"),
            patch("src.handlers.SessionManager", return_value=mock_sm),
            patch("src.handlers.VariableManager"),
            patch("src.handlers.SecretsStore"),
            patch("src.handlers.ScriptRunner", return_value=mock_sr),
            patch("src.handlers.mark_stale_script_executions", return_value=0),
            patch("src.handlers.asyncio.create_task"),
            patch("src.connection.ws_client.PlatformWSClient"),
            patch(
                "src.orchestrator.agent_supervisor.AgentSupervisor",
                return_value=mock_supervisor,
            ),
            patch(
                "src.orchestrator.task_dispatcher.TaskDispatcher",
                return_value=mock_dispatcher,
            ),
            # MessageRouter was replaced by WsTransport (unified WS
            # client). Patch that instead so init_office_process_model
            # gets the mock router.
            patch(
                "src.transport.ws_transport.WsTransport",
                return_value=mock_router,
            ),
            patch(
                "src.handlers.ManagerController",
                return_value=mock_manager,
            ),
            patch("src.handlers.HealthReporter", return_value=mock_reporter),
            patch("src.watchdog.TaskWatchdog", return_value=mock_watchdog),
        ):
            result = await init_office_process_model(
                office, "http://localhost:8000",
                container_name="cbcl-office-test",
                redis_client=mock_redis,
            )

        assert isinstance(result, ProcessModelOfficeComponents)
        assert result.supervisor is mock_supervisor
        assert result.dispatcher is mock_dispatcher
        assert result.router is mock_router
        assert result.reporter is mock_reporter
        assert result.manager is mock_manager
        assert result.watchdog is mock_watchdog
