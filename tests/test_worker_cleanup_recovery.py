"""Process-state regression coverage for office execution cleanup."""

import builtins
import json
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.docker import task_process_cleanup


def run_program(program, processes):
    marker = "a" * 64
    signals = []
    opened = []
    output = []

    class Entry:
        def __init__(self, name):
            self.name = name

        def stat(self):
            return SimpleNamespace(st_uid=processes[self.name].get("uid", 1000))

        def __truediv__(self, filename):
            def read_stat():
                process = processes[self.name]
                states = process.get("states", ["S"])
                state = states.pop(0) if len(states) > 1 else states[0]
                return f"{self.name} (worker (nested) name) {state} 1 1".encode()

            def read_bytes():
                if filename == "stat":
                    return read_stat()
                process = processes[self.name]
                if filename == "environ":
                    if process.get("unreadable"):
                        raise PermissionError("protected")
                    return process.get("environment", b"")
                assert filename == "cmdline"
                return process.get("command", b"sleep\0")

            return SimpleNamespace(read_bytes=read_bytes)

    def pidfd_open(process_id):
        opened.append(process_id)
        return process_id

    def send_signal(handle, signum):
        signals.append((handle, signum))
        processes.pop(str(handle))

    modules = {
        "pathlib": SimpleNamespace(
            Path=lambda value: SimpleNamespace(
                iterdir=lambda: [Entry(name) for name in list(processes)]
            )
        ),
        "sys": SimpleNamespace(argv=["cleanup", marker]),
        "os": SimpleNamespace(
            pidfd_open=pidfd_open,
            close=lambda handle: None,
            getpid=lambda: 999,
            geteuid=lambda: 1000,
        ),
        "signal": SimpleNamespace(
            pidfd_send_signal=send_signal,
            SIGTERM=signal.SIGTERM,
            SIGKILL=signal.SIGKILL,
        ),
        "time": SimpleNamespace(monotonic=lambda: 0, sleep=lambda seconds: None),
        "json": json,
        "re": __import__("re"),
    }
    controlled_builtins = dict(
        vars(builtins),
        __import__=lambda name, *args, **kwargs: modules[name],
        print=lambda value: output.append(value),
    )
    exec(program, {"__builtins__": controlled_builtins})
    return signals, opened, output


@pytest.mark.parametrize("state", ["Z", "X"])
@pytest.mark.parametrize(
    "program",
    [task_process_cleanup._CLEANUP_PROGRAM, task_process_cleanup._DISCOVER_PROGRAM],
)
def test_exited_unreadable_process_cannot_block_office_cleanup(program, state):
    processes = {"101": {"states": [state], "unreadable": True}}
    signals, opened, output = run_program(program, processes)
    assert signals == []
    assert opened == []
    assert set(processes) == {"101"}
    if program == task_process_cleanup._DISCOVER_PROGRAM:
        assert json.loads(output[0]) == []


@pytest.mark.parametrize(
    "program",
    [task_process_cleanup._CLEANUP_PROGRAM, task_process_cleanup._DISCOVER_PROGRAM],
)
def test_process_becoming_zombie_during_scan_is_rechecked(program):
    processes = {"101": {"states": ["S", "Z"], "unreadable": True}}
    signals, _, _ = run_program(program, processes)
    assert signals == []


@pytest.mark.parametrize(
    "program",
    [task_process_cleanup._CLEANUP_PROGRAM, task_process_cleanup._DISCOVER_PROGRAM],
)
def test_live_unreadable_process_still_fails_closed(program):
    processes = {"101": {"states": ["S"], "unreadable": True}}
    with pytest.raises(RuntimeError, match="Cannot inspect|Cannot verify"):
        run_program(program, processes)


def test_zombie_parent_does_not_hide_live_marked_child_or_kill_sibling():
    processes = {
        "101": {"states": ["Z"], "unreadable": True},
        "102": {"environment": f"CUBICLE_WORKER_EXECUTION_ID={'a' * 64}".encode()},
        "103": {"environment": f"CUBICLE_WORKER_EXECUTION_ID={'b' * 64}".encode()},
    }
    signals, _, _ = run_program(task_process_cleanup._CLEANUP_PROGRAM, processes)
    assert signals == [(102, signal.SIGTERM)]
    assert set(processes) == {"101", "103"}


def test_startup_discovers_live_markers_despite_unreaped_zombies():
    marker = "a" * 64
    processes = {
        "101": {"states": ["Z"], "unreadable": True},
        "102": {"environment": f"CUBICLE_WORKER_EXECUTION_ID={marker}".encode()},
    }
    signals, _, output = run_program(task_process_cleanup._DISCOVER_PROGRAM, processes)
    assert signals == []
    assert json.loads(output[0]) == [marker]


def test_startup_still_rejects_live_legacy_cli_beside_zombies():
    processes = {
        "101": {"states": ["Z"], "unreadable": True},
        "102": {"command": b"/usr/local/bin/claude\0--print\0"},
    }
    with pytest.raises(RuntimeError, match="Untracked legacy"):
        run_program(task_process_cleanup._DISCOVER_PROGRAM, processes)


@pytest.mark.parametrize(
    ("stderr", "reason"),
    [
        (b"RuntimeError: Cannot inspect a worker-owned container process", "live_process_unreadable"),
        (b"OSError: [Errno 38] Function not implemented", "pidfd_unavailable"),
        (b"private credentials or arbitrary Docker error", "helper_failed"),
    ],
)
async def test_cleanup_errors_expose_only_allowlisted_diagnostic(monkeypatch, stderr, reason):
    process = SimpleNamespace(
        returncode=1,
        communicate=AsyncMock(return_value=(b"", stderr)),
    )
    monkeypatch.setattr(
        task_process_cleanup.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    with pytest.raises(RuntimeError) as caught:
        await task_process_cleanup.terminate_worker_execution("office", "a" * 64)
    assert str(caught.value) == f"Task-scoped container cancellation failed ({reason})"
