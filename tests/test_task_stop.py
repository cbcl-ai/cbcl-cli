"""Task-scoped cancellation without subprocesses or a live container."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src._handlers._tasks import route_task_kill
from src.docker import task_process_cleanup
from src.orchestrator.agent_supervisor import AgentProcess, AgentState, AgentSupervisor


def _supervisor():
    return AgentSupervisor(
        workspace_path=".", office_id="office", container_name="office-test"
    )


@pytest.mark.parametrize("state", ["removed", "stopped", "running", "unknown", "unavailable", "wrong_identity"])
async def test_cleanup_after_shutdown_requires_exact_container_evidence(monkeypatch, state):
    import docker

    container_id = "b" * 64
    client = MagicMock()
    if state == "removed":
        client.containers.get.side_effect = docker.errors.NotFound("missing")
    elif state == "unavailable":
        client.containers.get.side_effect = docker.errors.DockerException("offline")
    else:
        client.containers.get.return_value = SimpleNamespace(
            id="c" * 64 if state == "wrong_identity" else container_id,
            attrs={"State": {} if state == "unknown" else {"Running": state == "running"}},
        )
    monkeypatch.setattr(docker, "from_env", lambda **kwargs: client)
    process = MagicMock(returncode=1)
    process.communicate = AsyncMock(return_value=(b"", b"exec failed"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    if state in {"removed", "stopped"}:
        await task_process_cleanup.terminate_worker_execution(container_id, "a" * 64)
    else:
        with pytest.raises(RuntimeError, match="cancellation failed"):
            await task_process_cleanup.terminate_worker_execution(container_id, "a" * 64)
    client.containers.get.assert_called_once_with(container_id)
    client.close.assert_called_once()


def test_mutable_container_name_cannot_prove_shutdown(monkeypatch):
    import docker

    factory = MagicMock()
    monkeypatch.setattr(docker, "from_env", factory)
    assert not task_process_cleanup._confirmed_container_stopped("cbcl-office-project")
    factory.assert_not_called()


def _worker(task_id="task-old", marker="a" * 64):
    process = MagicMock()
    process.wait = AsyncMock(return_value=0)
    process.returncode = None
    return AgentProcess(
        agent_name="analyst",
        role="worker",
        state=AgentState.WORKING,
        current_task_id=task_id,
        process=process,
        execution_marker=marker,
    )


async def test_stale_stop_cannot_kill_or_clear_successor():
    supervisor = _supervisor()
    worker = _worker(task_id="task-new")
    supervisor._agents["analyst"] = worker
    queue = AsyncMock()
    queue.get_active.return_value = {"task_id": "task-new"}
    await route_task_kill(
        {"task_id": "task-old", "agent_name": "analyst", "stop_request_id": "request"},
        supervisor=supervisor,
        queue_manager=queue,
        dispatcher=MagicMock(),
    )
    worker.process.terminate.assert_not_called()
    queue.clear_active.assert_not_awaited()
    queue.remove_task.assert_awaited_once_with("analyst", "task-old")


async def test_cleanup_failure_holds_slot_and_repeat_stop_recovers(monkeypatch):
    supervisor = _supervisor()
    worker = _worker()
    supervisor._agents["analyst"] = worker
    cleanup = AsyncMock(side_effect=RuntimeError("Docker unavailable"))
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    with pytest.raises(RuntimeError, match="Docker unavailable"):
        await supervisor.stop_task("analyst", "task-old")
    assert supervisor.is_agent_busy("analyst")
    assert supervisor.reconcile_stuck_agents() == []
    assert worker.cleanup_pending
    worker.state = AgentState.IDLE
    assert supervisor.active_count == 1
    assert supervisor.get_all_statuses()["analyst"]["status"] == "working"
    worker.process = None
    cleanup.side_effect = None
    assert await supervisor.stop_task("analyst", "task-old")
    assert not supervisor.is_agent_busy("analyst")
    assert not worker.cleanup_pending
    assert worker.execution_marker == ""
    assert cleanup.await_count == 2
    assert not await supervisor.stop_task("analyst", "task-old")


async def test_all_agents_cancellation_targets_only_this_task(monkeypatch):
    supervisor = _supervisor()
    supervisor._agents = {
        "analyst": _worker(),
        "reviewer": _worker(),
        "other": _worker(task_id="task-unrelated", marker="b" * 64),
    }
    cleanup = AsyncMock()
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    queue = AsyncMock()
    queue.get_active.return_value = {"task_id": "task-old"}
    await route_task_kill(
        {"task_id": "task-old", "agent_name": "analyst", "all_agents": True,
         "stop_request_id": "request"},
        supervisor=supervisor,
        queue_manager=queue,
        dispatcher=MagicMock(),
    )
    assert cleanup.await_count == 2
    supervisor._agents["other"].process.terminate.assert_not_called()
    queue.remove_task_from_all.assert_awaited_once_with("task-old")
    assert queue.clear_active.await_count == 2


async def test_stop_holds_spawn_lock_until_exact_marker_cleanup_finishes(monkeypatch):
    supervisor = _supervisor()
    worker = _worker()
    supervisor._agents["analyst"] = worker
    started = asyncio.Event()
    release = asyncio.Event()

    async def cleanup(container, marker):
        assert container == "office-test"
        assert marker == "a" * 64
        worker.process.terminate.assert_called_once()
        started.set()
        await release.wait()

    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    stopping = asyncio.create_task(supervisor.stop_task("analyst", "task-old"))
    await started.wait()
    assert supervisor.is_agent_busy("analyst")
    acquired = asyncio.Event()

    async def successor():
        async with supervisor._get_lock("analyst"):
            acquired.set()

    next_worker = asyncio.create_task(successor())
    await asyncio.sleep(0)
    assert not acquired.is_set()
    release.set()
    assert await stopping
    await next_worker


async def test_every_spawn_gets_fresh_marker_and_manager_env_does_not_inherit(
    monkeypatch,
):
    supervisor = _supervisor()
    monkeypatch.setenv(task_process_cleanup.WORKER_EXECUTION_ENV, "stale-parent-marker")
    assert (
        task_process_cleanup.WORKER_EXECUTION_ENV
        not in supervisor._build_subprocess_env()
    )
    process = MagicMock()
    process.stdin.drain = AsyncMock()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    for name in (
        "_reader_loop",
        "_wait_for_ready",
        "_monitor_exit",
        "_heartbeat_loop",
        "_send_to_agent",
    ):
        monkeypatch.setattr(supervisor, name, AsyncMock())
    assert await supervisor.spawn_worker("first", {}, {"task_id": "one"})
    assert await supervisor.spawn_worker("second", {}, {"task_id": "two"})
    markers = [
        call.kwargs["env"][task_process_cleanup.WORKER_EXECUTION_ENV]
        for call in spawn.await_args_list
    ]
    assert len(set(markers)) == 2
    assert all(len(marker) == 64 for marker in markers)
    await asyncio.gather(
        *[
            task
            for worker in supervisor._agents.values()
            for task in (worker.reader_task, worker.monitor_task, worker.heartbeat_task)
            if task is not None
        ]
    )


async def test_container_cleanup_has_exact_argv_and_bounded_failure(monkeypatch):
    process = MagicMock()
    process.returncode = 1
    process.communicate = AsyncMock(return_value=(b"", b"failure"))
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(RuntimeError, match="cancellation failed"):
        await task_process_cleanup.terminate_worker_execution("office-test", "a" * 64)
    argv = spawn.await_args.args
    assert argv == (
        "docker",
        "exec",
        "-i",
        "-u",
        "1000:1000",
        "office-test",
        "/usr/local/bin/python3",
        "-I",
        "-S",
        "-",
        "a" * 64,
    )
    assert process.communicate.await_args.args == (
        task_process_cleanup._CLEANUP_PROGRAM.encode(),
    )
    process.returncode = None
    process.communicate.side_effect = asyncio.TimeoutError
    process.wait = AsyncMock(return_value=0)
    with pytest.raises(asyncio.TimeoutError):
        await task_process_cleanup.terminate_worker_execution("office-test", "a" * 64)
    process.kill.assert_called_once()


@pytest.mark.parametrize("unreadable_owned", [False, True])
def test_container_cleanup_signals_exact_marker_only(unreadable_owned):
    import builtins
    import signal

    marker = "a" * 64
    environments = {
        "101": f"CUBICLE_WORKER_EXECUTION_ID={marker}".encode(),
        "102": f"CUBICLE_WORKER_EXECUTION_ID={marker}x".encode(),
        "103": b"CUBICLE_WORKER_EXECUTION_ID=other-worker",
    }
    if unreadable_owned:
        environments["104"] = b""
    signals = []

    class Entry:
        def __init__(self, name):
            self.name = name

        def __truediv__(self, value):
            if value == "stat":
                return SimpleNamespace(read_bytes=lambda: f"{self.name} (worker) S 1".encode())
            assert value == "environ"

            def read_bytes():
                if self.name == "104":
                    raise PermissionError("protected process")
                return environments.get(self.name, b"")

            return SimpleNamespace(read_bytes=read_bytes)

        def stat(self):
            return SimpleNamespace(st_uid=99)

    def send_signal(handle, signum):
        signals.append((handle, signum))
        environments.pop(str(handle), None)

    modules = {
        "pathlib": SimpleNamespace(
            Path=lambda value: SimpleNamespace(
                iterdir=lambda: [Entry(name) for name in list(environments)]
            )
        ),
        "sys": SimpleNamespace(argv=["cleanup", marker]),
        "os": SimpleNamespace(
            pidfd_open=lambda process_id: process_id,
            close=lambda handle: None,
            getpid=lambda: 999,
            geteuid=lambda: 99,
        ),
        "signal": SimpleNamespace(
            pidfd_send_signal=send_signal,
            SIGTERM=signal.SIGTERM,
            SIGKILL=signal.SIGKILL,
        ),
        "time": SimpleNamespace(monotonic=lambda: 0, sleep=lambda seconds: None),
    }
    controlled_builtins = dict(
        vars(builtins), __import__=lambda name, *args, **kwargs: modules[name]
    )
    if unreadable_owned:
        with pytest.raises(RuntimeError, match="Cannot inspect"):
            exec(
                task_process_cleanup._CLEANUP_PROGRAM,
                {"__builtins__": controlled_builtins},
            )
    else:
        exec(
            task_process_cleanup._CLEANUP_PROGRAM, {"__builtins__": controlled_builtins}
        )
        assert signals == [(101, signal.SIGTERM)]
        assert set(environments) == {"102", "103"}
