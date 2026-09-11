"""Bounded container cleanup scoped to one supervisor-created worker."""

from __future__ import annotations

import asyncio
import json
import re

WORKER_EXECUTION_ENV = "CUBICLE_WORKER_EXECUTION_ID"

_CLEANUP_PROGRAM = """
import os
import signal
import sys
import time
from pathlib import Path

marker = ("CUBICLE_WORKER_EXECUTION_ID=" + sys.argv[1]).encode()
deadline = time.monotonic() + 3

def matching_handles():
    handles = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        handle = None
        try:
            handle = os.pidfd_open(int(entry.name))
            if marker in (entry / "environ").read_bytes().split(b"\\0"):
                handles.append(handle)
                handle = None
        except (ProcessLookupError, FileNotFoundError):
            pass
        except PermissionError:
            try:
                if entry.stat().st_uid == os.geteuid():
                    raise RuntimeError("Cannot inspect a worker-owned container process")
            except FileNotFoundError:
                pass
        finally:
            if handle is not None:
                os.close(handle)
    return handles

first = True
while True:
    handles = matching_handles()
    if not handles:
        break
    for handle in handles:
        try:
            signal.pidfd_send_signal(handle, signal.SIGTERM if first else signal.SIGKILL)
        except ProcessLookupError:
            pass
        finally:
            os.close(handle)
    if time.monotonic() >= deadline:
        raise RuntimeError("Worker container processes remain after cancellation")
    time.sleep(0.1 if first else 0.05)
    first = False
"""


async def terminate_worker_execution(container_name: str, marker: str) -> None:
    """Signal only an exact random marker; never use agent-name process matches.

    Linux pidfds bind each signal to the inspected process, not a reused PID.
    Unsupported pidfds, Docker failure and timeout remain explicit failures.
    This is cooperative execution scoping, not a hostile-process sandbox:
    children that discard the marker or change UID may evade enumeration.
    """
    if not container_name or not re.fullmatch(r"[0-9a-f]{64}", marker):
        raise ValueError("A container and valid worker execution marker are required")
    process = await asyncio.create_subprocess_exec(
        "docker",
        "exec",
        "-i",
        "-u",
        "1000:1000",
        container_name,
        "/usr/local/bin/python3",
        "-I",
        "-S",
        "-",
        marker,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(
            process.communicate(_CLEANUP_PROGRAM.encode()), timeout=5
        )
        if process.returncode != 0:
            raise RuntimeError("Task-scoped container cancellation failed")
    except BaseException:
        if process.returncode is None:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=1)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
        raise


_DISCOVER_PROGRAM = """
import json
import os
import re
from pathlib import Path

markers = set()
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit() or int(entry.name) == os.getpid():
        continue
    try:
        if entry.stat().st_uid != os.geteuid():
            continue
        environment = (entry / 'environ').read_bytes().split(b'\\0')
        found = False
        for value in environment:
            if value.startswith(b'CUBICLE_WORKER_EXECUTION_ID='):
                marker = value.split(b'=', 1)[1].decode('ascii')
                if not re.fullmatch('[0-9a-f]{64}', marker):
                    raise RuntimeError('An orphan worker has an invalid execution marker')
                markers.add(marker)
                found = True
        command = (entry / 'cmdline').read_bytes().split(b'\\0')
        if not found and b'--print' in command and any(
            argument.rsplit(b'/', 1)[-1] == b'claude' for argument in command[:2]
        ):
            raise RuntimeError('Untracked legacy CLI session requires controlled container restart')
    except (FileNotFoundError, ProcessLookupError):
        pass
    except PermissionError:
        raise RuntimeError('Cannot verify orphan worker cleanup')
print(json.dumps(sorted(markers)))
"""


async def reap_worker_executions(container_id: str) -> int:
    if not container_id:
        raise ValueError("A verified office container is required for worker recovery")
    process = await asyncio.create_subprocess_exec(
        "docker",
        "exec",
        "-i",
        "-u",
        "1000:1000",
        container_id,
        "/usr/local/bin/python3",
        "-I",
        "-S",
        "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(_DISCOVER_PROGRAM.encode()), timeout=5
        )
        if process.returncode != 0:
            raise RuntimeError(
                "Office worker recovery could not prove previous sessions stopped. "
                "Check container access; legacy untracked sessions require a controlled "
                "container restart before task dispatch resumes."
            )
        if len(stdout) > 65536:
            raise RuntimeError("Office worker recovery response is too large")
        markers = json.loads(stdout)
        if not isinstance(markers, list) or len(markers) > 256:
            raise RuntimeError("Office worker recovery response is invalid")
        for marker in markers:
            await terminate_worker_execution(container_id, marker)
        return len(markers)
    except BaseException:
        if process.returncode is None:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=1)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
        raise
