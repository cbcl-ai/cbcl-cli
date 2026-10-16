"""The container cleanup programs against a real Linux ``/proc``, without Docker.

Every process here, the helper included, runs as an unprivileged uid, so a
non-dumpable process (``prctl(PR_SET_DUMPABLE, 0)``, as ssh-agent does) is
really unreadable to the helper, as in an office container. Needs Linux
pidfds and root to switch uid; skipped elsewhere.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import uuid

import pytest

from src.docker.task_process_cleanup import (
    _CLEANUP_PROGRAM,
    _DISCOVER_PROGRAM,
    cleanup_failure_details,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not hasattr(os, "pidfd_open") or os.geteuid() != 0,
    reason="requires Linux pidfds and root to run processes as another user",
)

# A uid nothing else in the test container uses.
AGENT_UID = 43210

# A worker whose child turns non-dumpable; prints the child's pid.
WORKER_TREE = (
    "import ctypes, os, time\n"
    "child = os.fork()\n"
    "if child == 0:\n"
    "    assert ctypes.CDLL(None).prctl(4, 0) == 0\n"
    "    time.sleep(60)\n"
    "    os._exit(0)\n"
    "print(child, flush=True)\n"
    "time.sleep(60)\n"
)
# A worker whose non-dumpable child ignores SIGTERM; the child prints its own
# pid once both are set up.
STUBBORN_TREE = (
    "import ctypes, os, signal, time\n"
    "if os.fork() == 0:\n"
    "    assert ctypes.CDLL(None).prctl(4, 0) == 0\n"
    "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "    print(os.getpid(), flush=True)\n"
    "    time.sleep(60)\n"
    "    os._exit(0)\n"
    "time.sleep(60)\n"
)
# Exits at once, leaving its non-dumpable child to the container's init.
ORPHAN = (
    "import ctypes, os, time\n"
    "child = os.fork()\n"
    "if child == 0:\n"
    "    assert ctypes.CDLL(None).prctl(4, 0) == 0\n"
    "    time.sleep(60)\n"
    "    os._exit(0)\n"
    "print(child, flush=True)\n"
)


def as_agent() -> None:
    os.setgroups([])
    os.setgid(AGENT_UID)
    os.setuid(AGENT_UID)


def environment(marker: str | None = None) -> dict[str, str]:
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin"}
    if marker:
        env["CUBICLE_WORKER_EXECUTION_ID"] = marker
    return env


def as_agent_run(arguments: list[str], **options) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-S", *arguments],
        env=environment(),
        preexec_fn=as_agent,
        capture_output=True,
        text=True,
        timeout=60,
        cwd="/",
        **options,
    )


@pytest.fixture
def start():
    pids: list[int] = []

    def started(code: str, marker: str | None = None) -> tuple[subprocess.Popen, int]:
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", code],
            env=environment(marker),
            preexec_fn=as_agent,
            stdout=subprocess.PIPE,
            text=True,
            cwd="/",
        )
        child = int(process.stdout.readline())
        pids.extend([process.pid, child])
        return process, child

    yield started
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_helper(program: str, *arguments: str, deadline: int = 15):
    return as_agent_run(
        ["-", *arguments],
        input=program.replace("DEADLINE_SECONDS = 15", f"DEADLINE_SECONDS = {deadline}"),
    )


def unreadable_to_agent(pid: int) -> bool:
    result = as_agent_run(["-c", f"open('/proc/{pid}/environ', 'rb').read()"])
    return "PermissionError" in result.stderr


def running(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", "rb") as stat:
            return stat.read().rpartition(b")")[2].split()[0] not in (b"Z", b"X")
    except FileNotFoundError:
        return False


def test_unreadable_children_are_attributed_to_their_worker(start):
    marker, other = uuid.uuid4().hex * 2, uuid.uuid4().hex * 2
    ours, ours_child = start(WORKER_TREE, marker)
    theirs, theirs_child = start(WORKER_TREE, other)
    assert unreadable_to_agent(ours_child) and unreadable_to_agent(theirs_child)

    cleanup = run_helper(_CLEANUP_PROGRAM, marker)

    assert cleanup.returncode == 0, cleanup.stderr
    ours.wait(timeout=5)
    assert not running(ours_child)
    assert running(theirs.pid) and running(theirs_child)
    discovery = run_helper(_DISCOVER_PROGRAM)
    assert discovery.returncode == 0, discovery.stderr
    markers = json.loads(discovery.stdout)
    assert other in markers and marker not in markers


def test_unreadable_child_is_killed_after_its_parent_exits(start):
    # The parent dies on the first SIGTERM, which reparents the child; the
    # child, signalled as ours on that first scan, still gets the SIGKILL.
    marker = uuid.uuid4().hex * 2
    ours, ours_child = start(STUBBORN_TREE, marker)
    assert unreadable_to_agent(ours_child)

    cleanup = run_helper(_CLEANUP_PROGRAM, marker)

    assert cleanup.returncode == 0, cleanup.stderr
    ours.wait(timeout=5)
    assert not running(ours_child)


def test_orphaned_unreadable_process_fails_closed_after_ours_are_killed(start):
    marker = uuid.uuid4().hex * 2
    ours, ours_child = start(WORKER_TREE, marker)
    orphan_parent, orphan = start(ORPHAN)
    orphan_parent.wait(timeout=5)
    assert unreadable_to_agent(orphan)

    cleanup = run_helper(_CLEANUP_PROGRAM, marker, deadline=2)

    assert cleanup.returncode != 0
    ours.wait(timeout=5)
    assert not running(ours_child)
    assert running(orphan)
    reason, text = cleanup_failure_details(cleanup.stderr.encode())
    assert reason == "live_process_unreadable"
    assert f"(pid {orphan}" in text
    discovery = run_helper(_DISCOVER_PROGRAM, deadline=2)
    assert discovery.returncode != 0
    assert "Cannot verify orphan worker cleanup" in discovery.stderr


def limited_agent(open_files: int):
    def apply() -> None:
        import resource

        resource.setrlimit(resource.RLIMIT_NOFILE, (open_files, open_files))
        as_agent()

    return apply


@pytest.mark.parametrize("program", ["cleanup", "discover"])
def test_a_busy_container_does_not_exhaust_the_helpers_open_files(program):
    # A scan used to keep a pidfd for every readable process of the agent
    # user until it ended, so more processes than the helper's open-file
    # limit failed every cleanup closed as "live_process_unreadable".
    marker = uuid.uuid4().hex * 2
    idle = "import time; print(1, flush=True); time.sleep(60)"
    processes = []
    try:
        for index in range(151):
            process = subprocess.Popen(
                [sys.executable, "-I", "-S", "-c", idle],
                env=environment(marker if index == 150 else None),
                preexec_fn=as_agent,
                stdout=subprocess.PIPE,
                text=True,
                cwd="/",
            )
            processes.append(process)
        for process in processes:
            process.stdout.readline()
        ours = processes[-1]
        source, arguments = (
            (_CLEANUP_PROGRAM, [marker])
            if program == "cleanup"
            else (_DISCOVER_PROGRAM, [])
        )
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-", *arguments],
            input=source,
            env=environment(),
            preexec_fn=limited_agent(64),
            capture_output=True,
            text=True,
            timeout=60,
            cwd="/",
        )
        assert result.returncode == 0, cleanup_failure_details(result.stderr.encode())
        if program == "cleanup":
            assert ours.wait(timeout=5) == -signal.SIGTERM
        else:
            assert json.loads(result.stdout) == [marker]
            assert ours.poll() is None
    finally:
        for process in processes:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        for process in processes:
            process.wait(timeout=5)
