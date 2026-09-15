"""Task cancellation never overclaims independent Office script cleanup."""

import asyncio
import importlib
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src._handlers._tasks import route_task_kill, route_task_moved, route_task_updated
from src.scripts.script_runner import ScriptRunner


@pytest.fixture
def runner(tmp_path):
    return ScriptRunner(str(tmp_path), MagicMock(), MagicMock())


@pytest.fixture
def script_module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "src" / "_agent_image"))
    return importlib.import_module("_mcp_script_exec")


@pytest.mark.parametrize("source", ["active", "starting", "uncertain"])
async def test_linked_script_withholds_stop_confirmation(runner, source):
    if source == "active":
        runner._active_by_task["task"] = {"execution"}
    elif source == "starting":
        runner._starting_by_task["task"] = 1
    else:
        runner._uncertain_tasks.add("task")
    supervisor = MagicMock()
    supervisor.get_all_statuses.return_value = {}
    router = AsyncMock()
    await route_task_kill(
        {"task_id": "task", "stop_request_id": "request", "all_agents": True},
        queue_manager=AsyncMock(),
        dispatcher=MagicMock(),
        supervisor=supervisor,
        router=router,
        script_runner=runner,
    )
    receipt = router.publish_event.call_args.args[0]
    assert receipt["status"] == "unconfirmed"
    assert receipt["errors"][0]["agent_name"] == "office-scripts"
    with pytest.raises(RuntimeError, match="cancellation"):
        await runner.execute("synthetic", task_id="task")


@pytest.mark.parametrize("handler", [route_task_moved, route_task_updated])
async def test_terminal_events_suppress_script_admission(runner, handler):
    supervisor = MagicMock()
    supervisor.get_all_statuses.return_value = {}
    await handler(
        {
            "task_id": "task",
            "new_status": "archived",
            "task_data": {"status": "archived"},
        },
        queue_manager=AsyncMock(),
        dispatcher=MagicMock(),
        supervisor=supervisor,
        router=AsyncMock(),
        script_runner=runner,
    )
    assert "task" in runner._suppressed_tasks


@pytest.mark.parametrize(
    "task",
    [
        {"status": "done"},
        {"status": "archived"},
        {},
        {"status": "in_progress", "execution_blocked": True},
    ],
)
async def test_host_runner_checks_authoritative_task_before_launch(
    runner, monkeypatch, task
):
    import httpx

    runner._platform_url = "https://synthetic.invalid"
    runner._office_id = "office"
    client = AsyncMock()
    client.__aenter__.return_value = client
    response = MagicMock()
    response.json.return_value = task
    client.get.return_value = response
    monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=client))
    runner._execute_v2 = AsyncMock()
    with pytest.raises(RuntimeError, match="refused"):
        await runner.execute("synthetic", task_id="task")
    runner._execute_v2.assert_not_awaited()
    assert not runner.has_active_scripts("task")


async def test_script_spawn_cancellation_stays_uncertain(runner):
    entered = asyncio.Event()

    async def pending(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    runner._execute_v2 = pending
    pending_task = asyncio.create_task(runner.execute("synthetic", task_id="task"))
    await entered.wait()
    assert runner.has_active_scripts("task")
    runner.suppress_task("task")
    pending_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending_task
    assert runner.has_active_scripts("task")


@pytest.mark.parametrize("task_id", [None, "task"])
@pytest.mark.parametrize("error_type", [asyncio.CancelledError, "deps"])
async def test_uncertain_preparation_retains_durable_admission(runner, tmp_path, task_id, error_type):
    from src.runtime_state import RuntimeState
    from src.scripts.deps_installer import DepsCleanupUnconfirmed

    state = RuntimeState(tmp_path / "runtime.sqlite", "office")
    runner.set_runtime_state(state)
    error = DepsCleanupUnconfirmed if error_type == "deps" else error_type
    runner._execute_v2 = AsyncMock(side_effect=error("interrupted"))
    with pytest.raises(error):
        await runner.execute("synthetic", task_id=task_id)
    state.snapshot(0, 0)
    assert state.maintenance_status()["pending_admissions"] == 1


async def test_confirmed_dependency_failure_releases_admission(runner, tmp_path):
    from src.runtime_state import RuntimeState
    from src.scripts.deps_installer import DepsInstallError

    state = RuntimeState(tmp_path / "runtime.sqlite", "office")
    runner.set_runtime_state(state)
    runner._execute_v2 = AsyncMock(side_effect=DepsInstallError("pip rejected input"))
    with pytest.raises(DepsInstallError):
        await runner.execute("synthetic")
    state.snapshot(0, 0)
    assert state.maintenance_status()["pending_admissions"] == 0


@pytest.mark.parametrize("response_status", [200, 409, None])
async def test_managed_mcp_never_installs_dependencies_locally(script_module, monkeypatch, tmp_path, response_status):
    import json
    import sys

    monkeypatch.setattr(script_module, "TASK_MODE", "execute")
    monkeypatch.setattr(script_module, "TASK_ID", "task")
    monkeypatch.setattr(script_module, "TOOL_PROXY_URL", "http://synthetic.invalid" if response_status else "")
    monkeypatch.setattr(script_module, "_task_launch_refusal", AsyncMock(return_value=None))
    monkeypatch.setattr(script_module, "_check_bootstrap_status", AsyncMock(return_value=None))
    monkeypatch.setattr(script_module, "_parse_manifest", lambda _directory: {})
    monkeypatch.setattr(script_module, "Path", lambda _path: tmp_path)
    installer = AsyncMock(side_effect=AssertionError("Managed MCP must never launch pip"))
    monkeypatch.setattr(script_module, "_ensure_deps_installed", installer)
    monkeypatch.setattr(sys.modules["_mcp_backend"], "_caller_envelope", dict, raising=False)
    response = MagicMock(status=response_status)
    response.text = AsyncMock(return_value=json.dumps({"execution_id": "owned"} if response_status == 200 else {"error": "not_ready"}))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post.return_value = context
    monkeypatch.setattr(script_module, "_get_session", AsyncMock(return_value=session))
    result = await script_module._execute_script({"script_name": "synthetic"})
    if response_status == 200:
        assert result["delegated_to"] == "host_runner"
    else:
        assert result["error"]
    installer.assert_not_awaited()


@pytest.mark.parametrize(
    "task",
    [
        {"status": "done"},
        {"status": "archived"},
        {},
        {"error": True},
        {"status": "in_progress", "execution_blocked": True},
    ],
)
async def test_mcp_script_task_gate_fails_closed(script_module, monkeypatch, task):
    monkeypatch.setattr(script_module, "TASK_ID", "task")
    backend = AsyncMock(return_value=task)
    monkeypatch.setattr(script_module, "_call_backend", backend)
    assert (await script_module._task_launch_refusal())["error"]
    backend.assert_awaited_once_with("get_task_detail", {"task_id": "task"})


async def test_mcp_script_lookup_failure_refuses(script_module, monkeypatch):
    monkeypatch.setattr(script_module, "TASK_ID", "task")
    monkeypatch.setattr(
        script_module, "_call_backend", AsyncMock(side_effect=RuntimeError("offline"))
    )
    assert (await script_module._task_launch_refusal())["error"]


async def test_mcp_script_authorized_live_task_proceeds(script_module, monkeypatch):
    monkeypatch.setattr(script_module, "TASK_ID", "task")
    monkeypatch.setattr(
        script_module,
        "_call_backend",
        AsyncMock(return_value={"status": "in_progress"}),
    )
    assert await script_module._task_launch_refusal() is None


def test_detached_mcp_script_excludes_worker_marker_and_auth(
    script_module, monkeypatch
):
    monkeypatch.setenv("CUBICLE_WORKER_EXECUTION_ID", "a" * 64)
    monkeypatch.setenv("TOOL_PROXY_TOKEN", "synthetic-sensitive-token")
    environment = script_module._script_base_env(
        {"CUBICLE_WORKER_EXECUTION_ID": "b" * 64}
    )
    assert "CUBICLE_WORKER_EXECUTION_ID" not in environment
    assert "TOOL_PROXY_TOKEN" not in environment


def test_mcp_script_manifest_cannot_forge_marker(script_module, monkeypatch):
    monkeypatch.delenv("CUBICLE_WORKER_EXECUTION_ID", raising=False)
    assert "CUBICLE_WORKER_EXECUTION_ID" not in script_module._script_base_env(
        {"CUBICLE_WORKER_EXECUTION_ID": "b" * 64}
    )


def test_mcp_config_explicitly_passes_worker_execution_marker(monkeypatch):
    from src._agent_worker_mcp import build_mcp_config

    monkeypatch.setenv("CUBICLE_WORKER_EXECUTION_ID", "a" * 64)
    worker = MagicMock(backend_url="https://synthetic.invalid", office_id="office")
    config = build_mcp_config(worker, "worker", task_id="task")
    assert (
        config["mcpServers"]["cubicle-tools"]["env"]["CUBICLE_WORKER_EXECUTION_ID"]
        == "a" * 64
    )


async def test_best_effort_script_kill_cannot_erase_uncertainty(
    runner, monkeypatch, tmp_path
):
    from datetime import datetime, timezone

    from src.scripts import script_execution
    from src.scripts.script_runner import _Execution

    execution = _Execution(
        exec_id="execution",
        script_name="synthetic",
        task_id="task",
        triggered_by="synthetic",
        process=MagicMock(),
        exec_dir=tmp_path,
        log_handle=MagicMock(),
        started_at=datetime.now(timezone.utc),
        container_name="synthetic-container",
    )
    runner._track_execution(execution)
    monkeypatch.setattr(
        script_execution, "_read_in_container_pid", lambda directory: None
    )
    await script_execution.terminate_execution(execution)
    runner._active_by_task.clear()
    runner._active.clear()
    assert runner.has_active_scripts("task")
    assert "task" in runner._uncertain_tasks


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="Exact process cleanup requires Linux pidfd support")
def test_normal_worker_cleanup_preserves_detached_script(
    script_module, monkeypatch, tmp_path
):
    import os
    import pathlib
    import subprocess
    import sys
    from types import SimpleNamespace
    from uuid import uuid4

    from src.docker.task_process_cleanup import _CLEANUP_PROGRAM

    marker = uuid4().hex + uuid4().hex
    monkeypatch.setenv("CUBICLE_WORKER_EXECUTION_ID", marker)
    worker_environment = {"CUBICLE_WORKER_EXECUTION_ID": marker}
    script_environment = script_module._script_base_env({})
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    worker = subprocess.Popen(command, env=worker_environment, cwd=tmp_path)
    script = subprocess.Popen(command, env=script_environment, cwd=tmp_path)

    class SyntheticEntry:
        def __init__(self, process, environment):
            self.process = process
            self.name = str(process.pid)
            self.environment = environment

        def __truediv__(self, suffix):
            if suffix == "stat":
                return SimpleNamespace(
                    read_bytes=lambda: f"{self.name} (worker) S 1".encode()
                )
            assert suffix == "environ"
            return SimpleNamespace(
                read_bytes=lambda: b"\0".join(
                    f"{name}={value}".encode()
                    for name, value in self.environment.items()
                )
            )

    entries = [
        SyntheticEntry(worker, worker_environment),
        SyntheticEntry(script, script_environment),
    ]
    inventory = SimpleNamespace(
        iterdir=lambda: [entry for entry in entries if entry.process.poll() is None]
    )
    original_path = pathlib.Path
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                pathlib,
                "Path",
                lambda path: inventory if path == "/proc" else original_path(path),
            )
            scoped.setattr(sys, "argv", ["synthetic-cleanup", marker])
            exec(compile(_CLEANUP_PROGRAM, "synthetic-cleanup", "exec"), {})
        assert worker.wait(timeout=2) < 0
        assert script.poll() is None
        assert os.getpid() not in {worker.pid, script.pid}
    finally:
        for process in (worker, script):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
