"""Fail-closed startup recovery; no daemon or real office processes are used."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src import recovery
from src.docker import task_process_cleanup


@pytest.mark.parametrize("markers", [[], ["a" * 64, "b" * 64]])
async def test_recovery_cleans_only_discovered_exact_markers(monkeypatch, markers):
    import json

    process = MagicMock()
    process.returncode = 0
    process.communicate = AsyncMock(return_value=(json.dumps(markers).encode(), b""))
    spawn = AsyncMock(return_value=process)
    cleanup = AsyncMock()
    monkeypatch.setattr(task_process_cleanup.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    assert await recovery.reap_orphan_agent_sessions("immutable-container") == len(
        markers
    )
    assert [call.args for call in cleanup.await_args_list] == [
        ("immutable-container", marker) for marker in markers
    ]
    argv = spawn.await_args.args
    assert argv == (
        "docker",
        "exec",
        "-i",
        "-u",
        "1000:1000",
        "immutable-container",
        "/usr/local/bin/python3",
        "-I",
        "-S",
        "-",
    )
    assert process.communicate.await_args.args == (
        task_process_cleanup._DISCOVER_PROGRAM.encode(),
    )


@pytest.mark.parametrize("returncode", [1, 126])
async def test_startup_refuses_unverified_orphan_cleanup(monkeypatch, returncode):
    process = MagicMock()
    process.returncode = returncode
    process.communicate = AsyncMock(
        return_value=(b"", b"unavailable or legacy process")
    )
    monkeypatch.setattr(
        task_process_cleanup.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    with pytest.raises(RuntimeError, match="could not prove"):
        await recovery.reap_orphan_agent_sessions("immutable-container")


async def test_startup_docker_unavailable_never_continues(monkeypatch):
    monkeypatch.setattr(
        task_process_cleanup.asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError("Docker unavailable")),
    )
    with pytest.raises(FileNotFoundError):
        await recovery.reap_orphan_agent_sessions("immutable-container")


async def test_startup_marker_cleanup_failure_never_continues(monkeypatch):
    process = MagicMock()
    process.returncode = 0
    process.communicate = AsyncMock(return_value=((f'["{"a" * 64}"]').encode(), b""))
    monkeypatch.setattr(
        task_process_cleanup.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    monkeypatch.setattr(
        task_process_cleanup,
        "terminate_worker_execution",
        AsyncMock(side_effect=RuntimeError("process remains")),
    )
    with pytest.raises(RuntimeError, match="process remains"):
        await recovery.reap_orphan_agent_sessions("immutable-container")
