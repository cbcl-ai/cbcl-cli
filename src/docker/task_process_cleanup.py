"""Bounded container cleanup scoped to one supervisor-created worker."""

from __future__ import annotations

import asyncio
import json
import re

WORKER_EXECUTION_ENV = "CUBICLE_WORKER_EXECUTION_ID"

# How long one helper run may take on the host. The container programs below
# give up after 15 seconds of rescans; the rest covers ``docker exec`` itself.
_HELPER_TIMEOUT_SECONDS = 25

# The one stderr line a helper prints when it gives up, followed by a JSON
# object with allowlisted fields only (never a command line or environment).
_DIAGNOSTIC_PREFIX = b"CUBICLE_CLEANUP_DIAGNOSTIC "
_DIAGNOSTIC_REASONS = frozenset({
    "processes_remain",
    "live_process_unreadable",
    "operation_not_permitted",
})
# Characters a process name keeps in a diagnostic; anything else becomes "?".
_PROCESS_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9 ._:/@+-]")
_CLEANUP_ERROR_LENGTH = 300


class WorkerCleanupError(RuntimeError):
    """One helper run could not prove that an execution's processes stopped.

    ``reason`` is an allowlisted code such as ``live_process_unreadable``;
    ``diagnostic`` is short allowlisted text naming the process that blocked
    the cleanup, for example
    ``live_process_unreadable: ssh-agent (pid 4321, running 2 h)``.
    """

    def __init__(self, reason: str, diagnostic: str = "") -> None:
        self.reason = reason
        self.diagnostic = diagnostic or reason
        super().__init__(
            f"Task-scoped container cancellation failed ({self.diagnostic})"
        )


class ExecutionCleanupUnconfirmed(RuntimeError):
    """A session ended, but its processes could not be confirmed stopped.

    Raised by the session bridge after its retries. The caller must not start
    another session for this execution: the supervisor keeps its slot, marker,
    resources and backend receipt until it confirms the stop itself. The
    worker fills in the session state when the stream ended: its session id
    and cost, the CLI error the session reported (``session_error``, None for
    a clean end) and the exception that ended the stream early
    (``interrupted_by``, None when the stream finished on its own).
    """

    def __init__(self, reason: str, diagnostic: str, attempts: int) -> None:
        self.reason = reason
        self.diagnostic = diagnostic
        self.attempts = attempts
        self.session_id: str | None = None
        self.total_cost: float | None = None
        self.session_error: str | None = None
        self.interrupted_by: BaseException | None = None
        super().__init__(
            f"Execution cleanup unconfirmed after {attempts} attempt(s): "
            f"Task-scoped container cancellation failed ({diagnostic})"
        )


def cleanup_failure_text(error: BaseException) -> str:
    """Short allowlisted text for a cleanup failure (logs, health reports).

    Never includes an exception message: those can carry paths or command
    output. Helper failures carry their own allowlisted diagnostic.
    """
    if isinstance(error, (WorkerCleanupError, ExecutionCleanupUnconfirmed)):
        text = error.diagnostic
    elif isinstance(error, TimeoutError):
        text = "helper_timeout"
    elif isinstance(error, asyncio.CancelledError):
        text = "cleanup_interrupted"
    elif isinstance(error, FileNotFoundError):
        text = "docker_unavailable"
    else:
        text = f"cleanup_failed ({type(error).__name__})"
    return text[:_CLEANUP_ERROR_LENGTH]


def is_helper_failure(error: BaseException) -> bool:
    """The helper ran in the container and could not confirm the stop.

    Such a failure lasts while the blocking process lives, and each attempt
    scans the container for up to 15 seconds, so the supervisor retries it on
    a backoff instead of on every reconcile pass.
    """
    return isinstance(error, (WorkerCleanupError, TimeoutError))


# Shared by both container programs below. Every process is classified on
# every scan, and a scan never raises for the state a process is in:
#
# * gone: it disappeared (ENOENT/ESRCH), or every thread is a zombie;
# * dying: it is exiting (PF_EXITING, 0x4 in the flags field of its stat) and
#   its memory, which holds its environment, is already released;
# * readable: its environment can be read, through a live thread when its
#   main thread has exited;
# * foreign: another user's process;
# * unreadable: one of ours whose environment cannot be read although it is
#   alive (non-dumpable, between a setuid and an exec, or after a gid change).
#
# An unreadable or dying process is attributed through its parent chain from
# world-readable stat files: the nearest readable ancestor decides. Reaching
# the container init (pid 1) or a ``docker exec`` root (pid 0), an ancestor
# that is gone, or one that started after its child (its pid was reused),
# leaves it unresolved; an unresolved process is read again on every scan and
# fails closed once the 15 seconds run out.
#
# A scan keeps a pidfd only for a process it may signal: an unreadable one,
# or a readable one the program needs (``keeps_handle``). Holding one for
# every process until the scan ends ran out of open files in a busy
# container, which failed every cleanup closed.
_PROCESS_SCAN_PROGRAM = r'''
import json
import os
import signal
import sys
import time
from pathlib import Path

DEAD_STATES = (b"Z", b"X")
PF_EXITING = 0x4
ENOSYS = 38
ENFILE = 23
EMFILE = 24
MAX_ANCESTRY = 32
DEADLINE_SECONDS = 15
GONE = "gone"
DYING = "dying"
READABLE = "readable"
UNREADABLE = "unreadable"
FOREIGN = "foreign"
BROKEN = "broken"
own_pid = os.getpid()
own_uid = os.geteuid()


def raise_open_file_limit():
    """Use the hard open-file limit; the soft one can be as low as 1024."""
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft != hard:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    except Exception:
        pass


raise_open_file_limit()


def read_stat(directory):
    """Return (comm, state, ppid, flags, start) from a stat file.

    Raises FileNotFoundError or ProcessLookupError once the process is gone.
    A missing field is unknown (None; no flags) instead of an error.
    """
    data = (directory / "stat").read_bytes()
    head, _, tail = data.rpartition(b")")
    fields = tail.split()

    def number(index):
        try:
            return int(fields[index])
        except (IndexError, ValueError):
            return None

    return (
        head.partition(b"(")[2],
        fields[0] if fields else b"?",
        number(1),
        number(6) or 0,
        number(19),
    )


def process_view(entry, state, flags):
    """Return (directory, exiting) to read a process through.

    The directory is None once every thread is dead. A process whose main
    thread has exited while other threads run is read through a live thread.
    """
    if state not in DEAD_STATES and not flags & PF_EXITING:
        return entry, False
    found = None if state in DEAD_STATES else entry
    try:
        threads = list((entry / "task").iterdir())
    except OSError:
        threads = []
    for thread in threads:
        try:
            _, thread_state, _, thread_flags, _ = read_stat(thread)
        except OSError:
            continue
        if thread_state in DEAD_STATES:
            continue
        if not thread_flags & PF_EXITING:
            return thread, False
        found = found or thread
    return found, True


def owned(entry):
    """True for a process of the helper's user, None once it is gone."""
    try:
        return entry.stat().st_uid == own_uid
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError:
        return True


def out_of_files(error):
    """True for an error that says the helper ran out of open files: no
    process can be judged then, so the scan fails instead of guessing."""
    return getattr(error, "errno", None) in (EMFILE, ENFILE)


def close_handle(record):
    if record["handle"] is not None:
        try:
            os.close(record["handle"])
        except OSError:
            pass
        record["handle"] = None
    return record


def keeps_handle(record):
    """Whether a readable process keeps its pidfd; each program redefines it."""
    return True


def keeps_unreadable_handle():
    """Whether an unreadable process keeps its pidfd; discovery redefines it."""
    return True


def unreadable(record):
    record["kind"] = UNREADABLE
    if not keeps_unreadable_handle():
        close_handle(record)
    return record


def same_process(entry, start):
    """True while the pid still names the process first read; False once it is
    gone or reused; None when its stat cannot be read again."""
    try:
        return read_stat(entry)[4] == start
    except (FileNotFoundError, ProcessLookupError):
        return False
    except OSError as error:
        if out_of_files(error):
            raise
        return None


def readable(record, view):
    record.update(kind=READABLE, view=view)
    if not keeps_handle(record):
        close_handle(record)
    return record


def inspect(entry, pid):
    """Classify one process; never raises for the state the process is in."""
    record = {
        "pid": pid, "kind": GONE, "handle": None, "ppid": None, "comm": b"",
        "state": b"?", "exiting": False, "start": None, "environment": None,
        "view": None, "error": None,
    }
    try:
        comm, state, ppid, flags, start = read_stat(entry)
    except (FileNotFoundError, ProcessLookupError):
        return record
    except OSError as error:
        if out_of_files(error):
            record.update(kind=BROKEN, error=error)
        elif owned(entry):
            record["kind"] = UNREADABLE
        return record
    record.update(comm=comm, state=state, ppid=ppid, start=start)
    view, exiting = process_view(entry, state, flags)
    if view is None:
        return record
    record["exiting"] = exiting
    try:
        # Opened before the environment is read, so a signal can only reach
        # the process that was inspected, never a later owner of its pid.
        record["handle"] = os.pidfd_open(pid)
    except ProcessLookupError:
        return record
    except OSError as error:
        record.update(kind=BROKEN, error=error)
        return record
    try:
        record["environment"] = (view / "environ").read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return close_handle(record)
    except OSError as error:
        if out_of_files(error):
            record.update(kind=BROKEN, error=error)
            return record
    else:
        # A pid reused since the first stat read would pair the old process's
        # parent with the new one's environment.
        same = same_process(entry, start)
        if same:
            return readable(record, view)
        # Reused or gone: the parent read first was the old process's, so it
        # links no process to an ancestor.
        record.update(environment=None, ppid=None)
        if same is None:
            # Not confirmed this scan: unresolved, never signalled.
            record["kind"] = UNREADABLE
        return close_handle(record)
    # The kernel makes an environment root-owned once the memory holding it
    # is released, which happens before the exiting state shows in stat.
    mine = owned(entry)
    if not mine:
        if mine is False:
            record["kind"] = FOREIGN
        return close_handle(record)
    try:
        comm, state, ppid, flags, again = read_stat(entry)
    except (FileNotFoundError, ProcessLookupError):
        return close_handle(record)
    except OSError as error:
        if out_of_files(error):
            record.update(kind=BROKEN, error=error)
            return record
        return unreadable(record)
    if again != start:
        # The pid was reused; the next scan reads the new process. The parent
        # read first was the old process's.
        record["ppid"] = None
        return close_handle(record)
    record.update(state=state, ppid=ppid)
    later, exiting = process_view(entry, state, flags)
    if later is None:
        return close_handle(record)
    if exiting:
        record.update(kind=DYING, exiting=True)
        return close_handle(record)
    if later is not view:
        try:
            record["environment"] = (later / "environ").read_bytes()
        except (FileNotFoundError, ProcessLookupError):
            return close_handle(record)
        except OSError as error:
            if out_of_files(error):
                record.update(kind=BROKEN, error=error)
                return record
        else:
            return readable(record, later)
    return unreadable(record)


def scan():
    """Classify every process in the container's pid namespace."""
    table = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid != own_pid:
            table[pid] = inspect(entry, pid)
    broken = [record for record in table.values() if record["kind"] == BROKEN]
    if broken:
        # A pidfd that cannot be opened cannot be waited out: no process
        # could be signalled safely.
        for record in table.values():
            close_handle(record)
        raise broken[0]["error"]
    return table


def owner(table, record, decide):
    """Decide for an unreadable or dying process from its nearest readable
    ancestor; None when no readable ancestor is found below pid 1, or when an
    ancestor started after its child (its pid was reused)."""
    parent = record["ppid"]
    child_start = record["start"]
    for _ in range(MAX_ANCESTRY):
        if parent is None or parent <= 1:
            return None
        ancestor = table.get(parent)
        if ancestor is None:
            return None
        start = ancestor["start"]
        if start is not None and child_start is not None and start > child_start:
            return None
        if ancestor["kind"] == READABLE:
            return decide(ancestor)
        child_start = start
        parent = ancestor["ppid"]
    return None


def age_seconds(start):
    try:
        uptime = float(Path("/proc/uptime").read_bytes().split()[0])
        return max(0, int(uptime - start / os.sysconf("SC_CLK_TCK")))
    except Exception:
        return None


def report(reason, records):
    """Print one allowlisted diagnostic line about the oldest blocker."""
    oldest = min(
        records,
        key=lambda record: (
            record["start"] if record["start"] is not None else float("inf")
        ),
    )
    diagnostic = {
        "reason": reason,
        "count": len(records),
        "pid": oldest["pid"],
        "comm": oldest["comm"].decode("utf-8", "replace")[:32],
        "state": oldest["state"].decode("ascii", "replace")[:4],
        "exiting": bool(oldest["exiting"]),
        "age_seconds": age_seconds(oldest["start"]),
    }
    try:
        line = json.dumps(diagnostic, sort_keys=True)
        sys.stderr.write("CUBICLE_CLEANUP_DIAGNOSTIC " + line + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def pause(started):
    time.sleep(0.05 if time.monotonic() - started < 1 else 0.2)
'''

_CLEANUP_PROGRAM = _PROCESS_SCAN_PROGRAM + r'''
marker = ("CUBICLE_WORKER_EXECUTION_ID=" + sys.argv[1]).encode()
KILL_AFTER_SECONDS = 1


def carries_marker(record):
    return marker in record["environment"].split(b"\0")


# Only the execution's own readable processes are signalled.
keeps_handle = carries_marker
started = time.monotonic()
deadline = started + DEADLINE_SECONDS
terminated = {}
refused = None
while True:
    table = scan()
    now = time.monotonic()
    remaining = []
    unresolved = []
    for record in table.values():
        kind = record["kind"]
        if kind == READABLE:
            if not carries_marker(record):
                continue
        elif kind in (UNREADABLE, DYING):
            mine = owner(table, record, carries_marker)
            if mine is None and kind == UNREADABLE and record["start"] is not None and (
                (record["pid"], record["start"]) in terminated
            ):
                # Signalled as ours on an earlier scan (its start time was
                # checked after its pidfd was opened). Its parent may have
                # exited since, which leaves no ancestor to attribute it by.
                # A readable ancestor that says otherwise now is believed.
                mine = True
            if mine is False:
                continue
            if mine is None and kind == UNREADABLE:
                unresolved.append(record)
                continue
        else:
            continue
        remaining.append(record)
        if record["exiting"] or record["handle"] is None:
            # An exiting process runs no more code; it is only waited for.
            continue
        # Ours are signalled on every scan, whatever unresolved processes do:
        # SIGTERM first, SIGKILL once the grace has passed.
        key = (record["pid"], record["start"])
        if key not in terminated:
            terminated[key] = now
            signum = signal.SIGTERM
        elif now - terminated[key] >= KILL_AFTER_SECONDS:
            signum = signal.SIGKILL
        else:
            continue
        try:
            signal.pidfd_send_signal(record["handle"], signum)
        except ProcessLookupError:
            pass
        except OSError as error:
            refused = error
    for record in table.values():
        close_handle(record)
    if not remaining and not unresolved:
        break
    if now >= deadline:
        if remaining:
            report(
                "operation_not_permitted"
                if getattr(refused, "errno", None) == 1
                else "processes_remain",
                remaining,
            )
            raise RuntimeError("Worker container processes remain after cancellation")
        report("live_process_unreadable", unresolved)
        raise RuntimeError("Cannot inspect a worker-owned container process")
    pause(started)
'''


def _cleanup_failure_reason(stderr: bytes) -> str:
    reasons = {
        b"Cannot inspect a worker-owned container process": "live_process_unreadable",
        b"Cannot verify orphan worker cleanup": "live_process_unreadable",
        b"Worker container processes remain after cancellation": "processes_remain",
        b"Untracked legacy CLI session requires controlled container restart": "legacy_execution",
        b"An orphan worker has an invalid execution marker": "invalid_execution_marker",
        b"[Errno 38]": "pidfd_unavailable",
        b"[Errno 1]": "operation_not_permitted",
        b"[Errno 24]": "too_many_open_files",
        b"[Errno 23]": "too_many_open_files",
        b"has no attribute 'pidfd_": "pidfd_unavailable",
    }
    for diagnostic, reason in reasons.items():
        if diagnostic in stderr:
            return reason
    return "helper_failed"


def _helper_diagnostic(stderr: bytes) -> dict | None:
    """The JSON object of a helper's diagnostic line, or ``None``."""
    for line in stderr.splitlines():
        if line.startswith(_DIAGNOSTIC_PREFIX):
            try:
                data = json.loads(line[len(_DIAGNOSTIC_PREFIX):][:4096])
            except ValueError:
                return None
            return data if isinstance(data, dict) else None
    return None


def _plain_count(value: object, *, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if low <= value <= high else None


def _duration(seconds: int) -> str:
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h"


def _describe_blocker(diagnostic: dict) -> str:
    """``ssh-agent (pid 4321, running 2 h)`` from an allowlisted diagnostic."""
    name = _PROCESS_NAME_UNSAFE.sub("?", str(diagnostic.get("comm") or ""))[:32]
    pid = _plain_count(diagnostic.get("pid"), low=1, high=4_194_304)
    age = _plain_count(diagnostic.get("age_seconds"), low=0, high=10**9)
    count = _plain_count(diagnostic.get("count"), low=1, high=100_000)
    facts = []
    if pid is not None:
        facts.append(f"pid {pid}")
    if diagnostic.get("exiting") is True:
        facts.append("exiting")
    if age is not None:
        facts.append(f"running {_duration(age)}")
    text = (name.strip() or "a process") + (f" ({', '.join(facts)})" if facts else "")
    if count and count > 1:
        text += f" and {count - 1} more"
    return text


def cleanup_failure_details(stderr: bytes) -> tuple[str, str]:
    """``(reason, short text)`` for a failed helper run, both allowlisted."""
    diagnostic = _helper_diagnostic(stderr)
    reason = _cleanup_failure_reason(stderr)
    if diagnostic is None:
        return reason, reason
    if diagnostic.get("reason") in _DIAGNOSTIC_REASONS:
        reason = diagnostic["reason"]
    text = f"{reason}: {_describe_blocker(diagnostic)}"
    return reason, text[:_CLEANUP_ERROR_LENGTH]


def _confirmed_container_stopped(container_id: str) -> bool:
    """Accept shutdown evidence only for an immutable, full Docker identity."""
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        return False
    import docker

    try:
        client = docker.from_env(timeout=3)
        try:
            container = client.containers.get(container_id)
            return (
                container.id == container_id
                and container.attrs.get("State", {}).get("Running") is False
            )
        except docker.errors.NotFound:
            return True
        finally:
            client.close()
    except (docker.errors.DockerException, OSError):
        return False


async def terminate_worker_execution(container_name: str, marker: str) -> None:
    """Signal only an exact random marker; never use agent-name process matches.

    Linux pidfds bind each signal to the inspected process, not a reused PID.
    Unsupported pidfds, Docker failure and timeout remain explicit failures;
    a run that cannot prove the stop raises ``WorkerCleanupError`` naming the
    process that blocked it. This is cooperative execution scoping, not a
    hostile-process sandbox: children that discard the marker or change UID
    may evade enumeration.
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
        _, stderr = await asyncio.wait_for(
            process.communicate(_CLEANUP_PROGRAM.encode()),
            timeout=_HELPER_TIMEOUT_SECONDS,
        )
        if process.returncode != 0:
            # Daemon shutdown removes office containers before stopping the host
            # Manager. Its exec helper cannot run then, but a verified stopped
            # or removed immutable container already proves its processes ended.
            if await asyncio.to_thread(_confirmed_container_stopped, container_name):
                return
            reason, diagnostic = cleanup_failure_details(stderr)
            raise WorkerCleanupError(reason, diagnostic)
    except BaseException:
        if process.returncode is None:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=1)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
        raise


_DISCOVER_PROGRAM = _PROCESS_SCAN_PROGRAM + r'''
import re

PREFIX = b"CUBICLE_WORKER_EXECUTION_ID="


def worker_markers(record):
    return [
        value[len(PREFIX):]
        for value in record["environment"].split(b"\0")
        if value.startswith(PREFIX)
    ]


def has_marker(record):
    return bool(worker_markers(record))


# Discovery signals nothing.
def keeps_handle(record):
    return False


def keeps_unreadable_handle():
    return False


def legacy_session(record):
    try:
        command = (record["view"] / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return b"--print" in command and any(
        argument.rsplit(b"/", 1)[-1] == b"claude" for argument in command[:2]
    )


markers = set()
started = time.monotonic()
deadline = started + DEADLINE_SECONDS
while True:
    table = scan()
    problems = []
    unresolved = []
    for record in table.values():
        if record["kind"] == READABLE:
            found = worker_markers(record)
            for value in found:
                text = value.decode("ascii", "replace")
                if re.fullmatch("[0-9a-f]{64}", text):
                    markers.add(text)
                else:
                    problems.append("An orphan worker has an invalid execution marker")
            if not found and not record["exiting"] and legacy_session(record):
                problems.append(
                    "Untracked legacy CLI session requires controlled container restart"
                )
        elif record["kind"] == UNREADABLE and owner(table, record, has_marker) is None:
            # A process under a marked worker is stopped by that marker's
            # cleanup; one under an unmarked process belongs to someone else.
            # A dying process runs no more code.
            unresolved.append(record)
    for record in table.values():
        close_handle(record)
    if problems:
        raise RuntimeError(problems[0])
    if not unresolved:
        break
    if time.monotonic() >= deadline:
        report("live_process_unreadable", unresolved)
        raise RuntimeError("Cannot verify orphan worker cleanup")
    pause(started)
print(json.dumps(sorted(markers)))
'''


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
        stdout, stderr = await asyncio.wait_for(
            process.communicate(_DISCOVER_PROGRAM.encode()),
            timeout=_HELPER_TIMEOUT_SECONDS,
        )
        if process.returncode != 0:
            _reason, diagnostic = cleanup_failure_details(stderr)
            raise RuntimeError(
                "Office worker recovery could not prove previous sessions stopped. "
                f"Cleanup diagnostic: {diagnostic}. "
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
