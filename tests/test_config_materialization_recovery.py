"""A transient sync failure cannot strand admission or replay an older revision."""

import asyncio
from functools import partial
from threading import Event
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config_sync.retry import ConfigSyncRetry
from src.handlers import _register_process_model_handlers
from src.orchestrator.agent_supervisor import AgentSupervisor
from src.runtime_state import RuntimeState


def register(monkeypatch, tmp_path, writer, *, supervisor=None):
    import src.config_sync.retry as retry_module

    monkeypatch.setattr(
        retry_module,
        "ConfigSyncRetry",
        partial(
            ConfigSyncRetry,
            retry_delay=0.001,
            max_retry_delay=0.002,
        ),
    )
    monkeypatch.setattr(
        "src.handlers._spawn_background", lambda coro, **_: coro.close()
    )
    supervisor = supervisor or AgentSupervisor(str(tmp_path), "office")
    applied = asyncio.Event()
    dispatcher = MagicMock()
    dispatcher.wake.side_effect = applied.set
    router = MagicMock()
    config = AsyncMock()
    _register_process_model_handlers(
        router,
        config,
        AsyncMock(),
        writer,
        MagicMock(),
        supervisor,
        dispatcher,
        MagicMock(),
        MagicMock(),
        MagicMock(),
    )
    handler = next(
        call.args[1]
        for call in router.on.call_args_list
        if call.args[0] == "sync_config"
    )
    return handler, supervisor, applied, config


async def test_transient_materialization_failure_recovers_without_another_sync(
    monkeypatch,
    tmp_path,
    caplog,
):
    writer = MagicMock()
    writer.sync_all.side_effect = [OSError("private-content-must-not-leak"), None]
    handler, supervisor, applied, config = register(monkeypatch, tmp_path, writer)
    policy = {"enabled": True, "max_workers": 2, "max_workers_per_profile": 2}
    await handler(
        {
            "config": {
                "agent_execution_policy": policy,
                "office_tool_secret": "current-owner",
            }
        }
    )
    assert not supervisor.config_ready
    assert "OSError" in supervisor.config_sync_error
    assert "private-content-must-not-leak" not in caplog.text
    await asyncio.wait_for(applied.wait(), timeout=1)
    assert supervisor.config_ready and supervisor.execution_policy == policy
    assert supervisor._office_tool_secret == "current-owner"
    assert supervisor.config_sync_error is None
    assert config.update_from_sync.await_count == 2
    await supervisor._config_reconciler.close()


async def test_new_revision_waits_for_old_writer_without_opening_stale_admission(
    monkeypatch,
    tmp_path,
):
    entered = asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()
    written = []

    def write(config):
        if config["revision"] == "old":
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(timeout=2)
        written.append(config["revision"])

    writer = MagicMock()
    writer.sync_all.side_effect = write
    handler, supervisor, _, _ = register(monkeypatch, tmp_path, writer)
    original_set_policy = supervisor.set_execution_policy
    readiness = []

    def set_policy(policy, *, ready=True):
        readiness.append((policy, ready))
        original_set_policy(policy, ready=ready)

    supervisor.set_execution_policy = set_policy
    old = {"enabled": False, "max_workers": 2, "max_workers_per_profile": 1}
    new = {"enabled": True, "max_workers": 4, "max_workers_per_profile": 2}
    first = asyncio.create_task(
        handler({"config": {"revision": "old", "agent_execution_policy": old}})
    )
    await entered.wait()
    second = asyncio.create_task(
        handler({"config": {"revision": "new", "agent_execution_policy": new}})
    )
    await asyncio.sleep(0)
    assert not supervisor.config_ready
    release.set()
    await asyncio.gather(first, second)
    assert written == ["old", "new"]
    assert [policy for policy, ready in readiness if ready] == [new]
    await supervisor._config_reconciler.close()


async def test_new_sync_interrupts_old_retry_delay_and_never_retries_old_revision():
    calls = []
    pauses = []

    async def apply(message, current):
        calls.append(message["revision"])
        if message["revision"] == "old":
            raise OSError("unavailable")
        assert current()

    retry = ConfigSyncRetry(
        apply, lambda: pauses.append(True), lambda _: None, retry_delay=30
    )
    await retry.submit({"revision": "old"})
    await asyncio.wait_for(retry.submit({"revision": "new"}), timeout=1)
    assert calls == ["old", "new"]
    await retry.close()


@pytest.mark.parametrize("reservation", ["script", "uncertain_claim"])
async def test_disabled_policy_waits_for_private_cleanup_then_applies_without_save(
    monkeypatch, tmp_path, reservation,
):
    writer = MagicMock()
    handler, supervisor, applied, config = register(monkeypatch, tmp_path, writer)
    state = RuntimeState(tmp_path / "private.sqlite", "office")
    if reservation == "script":
        state.begin_script_resource_lease(
            lease_id="prior", script_name="one", task_id="", parent_attempt_id="",
            resources=["shared-workspace"], execution_id="execution", marker="a" * 64,
            container_id="b" * 64,
        )
    else:
        state.begin_worker_claim("reader", "task", {"attempt_id": "uncertain"})
    supervisor.set_runtime_state(state)
    await handler({"config": {"agent_execution_policy": {"enabled": False},
                              "office_tool_secret": "fresh-owner"}})
    assert not supervisor.config_ready
    assert supervisor.execution_policy["enabled"]
    assert "Waiting for execution cleanup" in supervisor.config_sync_error
    assert "materialization failed" not in supervisor.config_sync_error
    assert supervisor._office_tool_secret == "fresh-owner"
    config.update_from_sync.assert_not_awaited()
    writer.sync_all.assert_not_called()
    if reservation == "script":
        state.set_script_resource_state("prior", "released")
    else:
        state.forget_worker_execution("uncertain")
    await asyncio.wait_for(applied.wait(), timeout=1)
    assert supervisor.config_ready and not supervisor.execution_policy["enabled"]
    assert supervisor.config_sync_error is None
    assert config.update_from_sync.await_count == 1
    await supervisor._config_reconciler.close()


async def test_shutdown_drains_writer_and_never_reopens_admission():
    entered = asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()
    ready = []

    def write():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=2)

    async def apply(message, current):
        await asyncio.to_thread(write)
        if current():
            ready.append(True)

    retry = ConfigSyncRetry(apply, lambda: None, lambda _: None)
    submit = asyncio.create_task(retry.submit({}))
    await entered.wait()
    closing = asyncio.create_task(retry.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await asyncio.gather(submit, closing)
    assert ready == []
    await retry.submit({"revision": "too-late"})
    assert retry._task is None
