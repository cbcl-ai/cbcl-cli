"""Process-state regression coverage for office execution cleanup.

The container programs run here against a fake ``/proc``: every process has
a full stat line (state, parent, flags, start time), optional threads, and a
clock that the programs' own sleeps advance, so a deadline is reached without
waiting for it.
"""

import builtins
import io
import json
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.docker import task_process_cleanup

MARKER = "a" * 64
OURS = f"CUBICLE_WORKER_EXECUTION_ID={MARKER}".encode()
OTHER = f"CUBICLE_WORKER_EXECUTION_ID={'b' * 64}".encode()
PF_EXITING = 0x4
CLOCK_TICKS = 100


def stat_line(pid, comm, state, ppid, flags, start):
    # Fields 3 to 22 of /proc/<pid>/stat; field 9 is flags, 22 the start time.
    fields = [state, ppid, pid, pid, 0, -1, flags, *([0] * 8), 20, 0, 1, 0, start]
    return f"{pid} ({comm}) {' '.join(str(field) for field in fields)}".encode()


class FakeProcfs:
    """The processes of one fake container and the effects of a program run.

    Each process is a dict: ``states``/``flags``/``unreadable`` are consumed
    one per read of the main thread (the last value stays), ``gone_at`` is the
    clock time it disappears, ``threads`` lists its threads (default: one that
    mirrors the main thread), ``ignores_term`` keeps it alive until SIGKILL,
    and ``lookup_fails_on_stat`` makes that stat read raise ESRCH.
    """

    def __init__(self, processes, *, uptime=None):
        self.processes = processes
        self.now = 0.0
        self.uptime = uptime
        self.signals = []
        self.opened = []
        self.open_handles = set()
        self.peak_open_handles = 0
        self.output = []
        self.stderr = io.StringIO()
        self.stat_reads = {}

    def alive(self, name):
        process = self.processes.get(name)
        return process is not None and self.now < process.get("gone_at", float("inf"))

    def process(self, name):
        if not self.alive(name):
            raise FileNotFoundError(name)
        return self.processes[name]

    @staticmethod
    def take(process, key, default):
        values = process.get(key, default)
        if not isinstance(values, list):
            return values
        return values.pop(0) if len(values) > 1 else values[0]

    def entries(self):
        return [Entry(self, name) for name in list(self.processes) if self.alive(name)]

    def read(self, name, thread, filename):
        process = self.process(name)
        if filename == "stat":
            if thread is None:
                count = self.stat_reads[name] = self.stat_reads.get(name, 0) + 1
                if process.get("lookup_fails_on_stat") == count:
                    raise ProcessLookupError(name)
                state = self.take(process, "states", "S")
                flags = self.take(process, "flags", 0)
            else:
                state = thread.get("state", "S")
                flags = thread.get("flags", 0)
            parent = process.get("ppid", 1)
            if str(parent) in self.processes and not self.alive(str(parent)):
                # Linux reparents an orphan to the container init.
                parent = 1
            return stat_line(
                name, process.get("comm", "worker (nested) name"), state,
                parent, flags, process.get("start", 1000),
            )
        if filename == "environ":
            source = process if thread is None else thread
            if self.take(source, "unreadable", False):
                raise PermissionError("protected")
            return source.get("environment", b"")
        assert filename == "cmdline"
        return process.get("command", b"sleep\0")

    def threads(self, name):
        process = self.process(name)
        threads = process.get("threads")
        if threads is None:
            states = process.get("states", "S")
            flags = process.get("flags", 0)
            threads = [{
                "state": states[0] if isinstance(states, list) else states,
                "flags": flags[0] if isinstance(flags, list) else flags,
                "environment": process.get("environment", b""),
                "unreadable": process.get("unreadable", False),
            }]
        return [Entry(self, name, thread=thread) for thread in threads]

    def pidfd_open(self, process_id):
        if not self.alive(str(process_id)):
            raise ProcessLookupError(process_id)
        self.opened.append(process_id)
        self.open_handles.add(process_id)
        self.peak_open_handles = max(self.peak_open_handles, len(self.open_handles))
        return process_id

    def close(self, handle):
        self.open_handles.discard(handle)

    def send_signal(self, handle, signum):
        self.signals.append((handle, signum))
        process = self.processes.get(str(handle))
        if process is None or not self.alive(str(handle)):
            raise ProcessLookupError(handle)
        if signum == signal.SIGKILL or not process.get("ignores_term"):
            process["gone_at"] = self.now

    def sleep(self, seconds):
        self.now += seconds

    def path(self, value):
        if value == "/proc/uptime":
            if self.uptime is None:
                raise FileNotFoundError(value)
            return SimpleNamespace(read_bytes=lambda: f"{self.uptime} 0.0".encode())
        return SimpleNamespace(iterdir=self.entries)

    def run(self, program, *argv):
        modules = {
            "pathlib": SimpleNamespace(Path=self.path),
            "sys": SimpleNamespace(argv=["cleanup", *argv], stderr=self.stderr),
            "os": SimpleNamespace(
                pidfd_open=self.pidfd_open,
                close=self.close,
                getpid=lambda: 999,
                geteuid=lambda: 1000,
                sysconf=lambda name: CLOCK_TICKS,
            ),
            "signal": SimpleNamespace(
                pidfd_send_signal=self.send_signal,
                SIGTERM=signal.SIGTERM,
                SIGKILL=signal.SIGKILL,
            ),
            "time": SimpleNamespace(monotonic=lambda: self.now, sleep=self.sleep),
            "json": json,
            "re": __import__("re"),
        }
        controlled_builtins = dict(
            vars(builtins),
            __import__=lambda name, *args, **kwargs: modules[name],
            print=lambda value: self.output.append(value),
        )
        exec(program, {"__builtins__": controlled_builtins})

    def diagnostic(self):
        lines = [
            line for line in self.stderr.getvalue().splitlines()
            if line.startswith("CUBICLE_CLEANUP_DIAGNOSTIC ")
        ]
        assert len(lines) == 1
        return json.loads(lines[0].split(" ", 1)[1])


class Entry:
    def __init__(self, procfs, name, thread=None):
        self.procfs = procfs
        self.name = name
        self.thread = thread

    def stat(self):
        return SimpleNamespace(st_uid=self.procfs.process(self.name).get("uid", 1000))

    def __truediv__(self, filename):
        if filename == "task":
            return SimpleNamespace(iterdir=lambda: self.procfs.threads(self.name))
        return SimpleNamespace(
            read_bytes=lambda: self.procfs.read(self.name, self.thread, filename)
        )


def run_program(program, processes, **options):
    procfs = FakeProcfs(processes, **options)
    procfs.run(program, MARKER)
    return procfs.signals, procfs.opened, procfs.output


def cleanup(processes, **options):
    procfs = FakeProcfs(processes, **options)
    procfs.run(task_process_cleanup._CLEANUP_PROGRAM, MARKER)
    return procfs


BOTH_PROGRAMS = pytest.mark.parametrize(
    "program",
    [task_process_cleanup._CLEANUP_PROGRAM, task_process_cleanup._DISCOVER_PROGRAM],
    ids=["cleanup", "discover"],
)


@pytest.mark.parametrize("state", ["Z", "X"])
@BOTH_PROGRAMS
def test_exited_unreadable_process_cannot_block_office_cleanup(program, state):
    processes = {"101": {"states": [state], "unreadable": True}}
    signals, opened, output = run_program(program, processes)
    assert signals == []
    assert opened == []
    assert set(processes) == {"101"}
    if program == task_process_cleanup._DISCOVER_PROGRAM:
        assert json.loads(output[0]) == []


@BOTH_PROGRAMS
def test_process_becoming_zombie_during_scan_is_rechecked(program):
    processes = {"101": {"states": ["S", "Z"], "unreadable": True}}
    signals, _, _ = run_program(program, processes)
    assert signals == []


@BOTH_PROGRAMS
def test_live_unreadable_process_still_fails_closed(program):
    # Its parent is the container init: nothing attributes it to an owner.
    processes = {"101": {"states": ["S"], "unreadable": True, "ppid": 1}}
    with pytest.raises(RuntimeError, match="Cannot inspect|Cannot verify"):
        run_program(program, processes)


@BOTH_PROGRAMS
def test_unreadable_process_under_a_docker_exec_root_fails_closed(program):
    processes = {"101": {"unreadable": True, "ppid": 0}}
    with pytest.raises(RuntimeError, match="Cannot inspect|Cannot verify"):
        run_program(program, processes)


def test_zombie_parent_does_not_hide_live_marked_child_or_kill_sibling():
    processes = {
        "101": {"states": ["Z"], "unreadable": True},
        "102": {"environment": OURS},
        "103": {"environment": OTHER},
    }
    procfs = cleanup(processes)
    assert procfs.signals == [(102, signal.SIGTERM)]
    assert procfs.alive("101") and procfs.alive("103")
    assert not procfs.alive("102")


def test_startup_discovers_live_markers_despite_unreaped_zombies():
    processes = {
        "101": {"states": ["Z"], "unreadable": True},
        "102": {"environment": OURS},
    }
    signals, _, output = run_program(task_process_cleanup._DISCOVER_PROGRAM, processes)
    assert signals == []
    assert json.loads(output[0]) == [MARKER]


def test_startup_still_rejects_live_legacy_cli_beside_zombies():
    processes = {
        "101": {"states": ["Z"], "unreadable": True},
        "102": {"command": b"/usr/local/bin/claude\0--print\0"},
    }
    with pytest.raises(RuntimeError, match="Untracked legacy"):
        run_program(task_process_cleanup._DISCOVER_PROGRAM, processes)


def test_exiting_process_that_disappears_is_waited_for_not_failed():
    # PF_EXITING with its memory already released: the environment is
    # root-owned, the state still running.
    processes = {
        "101": {"environment": OURS},
        "102": {"flags": PF_EXITING, "unreadable": True, "gone_at": 0.4},
    }
    procfs = cleanup(processes)
    assert procfs.signals == [(101, signal.SIGTERM)]
    assert 0.4 <= procfs.now < 1


def test_process_that_stays_exiting_reports_processes_remain():
    processes = {"102": {"flags": PF_EXITING, "unreadable": True, "comm": "node"}}
    procfs = FakeProcfs(processes)
    with pytest.raises(RuntimeError, match="processes remain"):
        procfs.run(task_process_cleanup._CLEANUP_PROGRAM, MARKER)
    assert procfs.signals == []
    assert procfs.now >= 15
    diagnostic = procfs.diagnostic()
    assert diagnostic["reason"] == "processes_remain"
    assert diagnostic["exiting"] is True
    assert diagnostic["comm"] == "node"


def test_transient_unreadable_process_is_read_again():
    # Between a setuid and an exec a process is briefly unreadable; once it
    # is readable again it is judged by its environment.
    processes = {
        "101": {"environment": OURS},
        "102": {"unreadable": [True, True, False], "environment": OTHER},
    }
    procfs = cleanup(processes)
    assert procfs.signals == [(101, signal.SIGTERM)]
    assert "102" in processes and procfs.alive("102")


def test_long_lived_unreadable_process_does_not_stop_ours_being_killed():
    start = (10_000 - 7_200) * CLOCK_TICKS
    processes = {
        "101": {"environment": OURS, "ignores_term": True},
        "4321": {"unreadable": True, "ppid": 1, "comm": "ssh-agent", "start": start},
    }
    procfs = FakeProcfs(processes, uptime=10_000.0)
    with pytest.raises(RuntimeError, match="Cannot inspect"):
        procfs.run(task_process_cleanup._CLEANUP_PROGRAM, MARKER)
    # SIGTERM at once, SIGKILL after the grace, whatever 4321 does.
    assert procfs.signals == [(101, signal.SIGTERM), (101, signal.SIGKILL)]
    assert not procfs.alive("101")
    assert procfs.alive("4321")
    diagnostic = procfs.diagnostic()
    assert diagnostic == {
        "reason": "live_process_unreadable", "count": 1, "pid": 4321,
        "comm": "ssh-agent", "state": "S", "exiting": False, "age_seconds": 7200,
    }
    reason, text = task_process_cleanup.cleanup_failure_details(
        procfs.stderr.getvalue().encode()
        + b"RuntimeError: Cannot inspect a worker-owned container process\n"
    )
    assert (reason, text) == (
        "live_process_unreadable",
        "live_process_unreadable: ssh-agent (pid 4321, running 2 h)",
    )


def test_unreadable_descendant_of_ours_is_attributed_and_killed():
    processes = {
        "101": {"environment": OURS},
        "102": {"unreadable": True, "ppid": 101},
        "103": {"unreadable": True, "ppid": 102},
    }
    procfs = cleanup(processes)
    assert sorted(procfs.signals) == [
        (101, signal.SIGTERM), (102, signal.SIGTERM), (103, signal.SIGTERM),
    ]


def test_unreadable_descendant_stays_ours_after_its_parent_exits():
    # The parent exits on SIGTERM; the child ignores it and is reparented to
    # the container init, so no ancestor attributes it any more. It was
    # signalled as ours on the first scan, so it still gets the SIGKILL.
    processes = {
        "101": {"environment": OURS},
        "102": {"unreadable": True, "ppid": 101, "ignores_term": True},
    }
    procfs = cleanup(processes)
    assert procfs.signals == [
        (101, signal.SIGTERM), (102, signal.SIGTERM), (102, signal.SIGKILL),
    ]
    assert not procfs.alive("102")
    assert procfs.now < 2


def test_unreadable_child_of_another_worker_is_ignored():
    processes = {
        "101": {"environment": OURS},
        "201": {"environment": OTHER},
        "202": {"unreadable": True, "ppid": 201},
        # A dying process under another worker is not waited for either.
        "203": {"unreadable": True, "flags": PF_EXITING, "ppid": 201},
    }
    procfs = cleanup(processes)
    assert procfs.signals == [(101, signal.SIGTERM)]
    assert procfs.now < 1


def test_another_users_process_is_ignored():
    processes = {"101": {"environment": OURS}, "1": {"unreadable": True, "uid": 0}}
    procfs = cleanup(processes)
    assert procfs.signals == [(101, signal.SIGTERM)]


def test_zombie_main_thread_with_a_live_thread_is_read_through_the_thread():
    processes = {
        "101": {
            "states": ["Z"],
            "unreadable": True,
            "threads": [
                {"state": "Z", "unreadable": True},
                {"state": "S", "environment": OURS},
            ],
        },
        "102": {
            "states": ["Z"],
            "unreadable": True,
            "threads": [{"state": "Z", "unreadable": True}, {"environment": OTHER}],
        },
    }
    procfs = cleanup(processes)
    assert procfs.signals == [(101, signal.SIGTERM)]
    assert procfs.alive("102")


def test_lookup_failure_on_the_recheck_counts_as_gone():
    processes = {
        "101": {"environment": OURS},
        "102": {"unreadable": True, "lookup_fails_on_stat": 2, "gone_at": 0.01},
    }
    procfs = cleanup(processes)
    assert procfs.signals == [(101, signal.SIGTERM)]


def test_reused_pid_during_the_recheck_is_read_again_next_scan():
    processes = {"102": {"unreadable": True, "ppid": 1, "gone_at": 0.3}}
    starts = iter([1000, 2000])

    class Reused(FakeProcfs):
        def read(self, name, thread, filename):
            if filename == "stat" and thread is None and self.stat_reads.get(name, 0) < 2:
                self.stat_reads[name] = self.stat_reads.get(name, 0) + 1
                return stat_line(name, "worker", "S", 1, 0, next(starts))
            return super().read(name, thread, filename)

    procfs = Reused(processes)
    procfs.run(task_process_cleanup._CLEANUP_PROGRAM, MARKER)
    assert procfs.signals == []


@BOTH_PROGRAMS
def test_scan_keeps_no_pidfd_for_processes_it_will_not_signal(program):
    # A busy container: many readable processes of the agent user that are
    # not ours. Holding a pidfd for each until the scan ended exhausted the
    # helper's open files and failed every cleanup closed.
    processes = {str(100 + index): {"environment": OTHER} for index in range(200)}
    processes["999"] = {"environment": OURS}
    procfs = FakeProcfs(processes)
    procfs.run(program, MARKER)
    assert procfs.peak_open_handles <= 2
    assert not procfs.open_handles


def test_unreadable_process_is_not_attributed_to_a_parent_that_started_after_it():
    # 102's parent pid now belongs to a process of ours that started later,
    # so it cannot be 102's parent: 102 stays unresolved and is not signalled.
    processes = {
        "101": {"environment": OURS, "start": 5000},
        "102": {"unreadable": True, "ppid": 101, "start": 2000, "comm": "ssh-agent"},
    }
    procfs = FakeProcfs(processes)
    with pytest.raises(RuntimeError, match="Cannot inspect a worker-owned container process"):
        procfs.run(task_process_cleanup._CLEANUP_PROGRAM, MARKER)
    assert procfs.signals == [(101, signal.SIGTERM)]
    assert procfs.diagnostic()["comm"] == "ssh-agent"


def test_reused_pid_of_a_readable_process_is_not_read_as_the_parent():
    # 101 is first read as an unrelated process (started at 1000, child of
    # init); its pid is reused by a process of ours before its environment
    # is read. That environment must not make 101's old child 102 ours.
    processes = {
        "101": {"environment": OURS, "start": 3000},
        "102": {"unreadable": True, "ppid": 101, "start": 2000, "comm": "ssh-agent"},
    }

    class Reused(FakeProcfs):
        def read(self, name, thread, filename):
            if name == "101" and filename == "stat" and thread is None and not self.stat_reads.get(name):
                self.stat_reads[name] = 1
                return stat_line(name, "other", "S", 1, 0, 1000)
            return super().read(name, thread, filename)

    procfs = Reused(processes)
    with pytest.raises(RuntimeError, match="Cannot inspect a worker-owned container process"):
        procfs.run(task_process_cleanup._CLEANUP_PROGRAM, MARKER)
    assert all(handle != 102 for handle, _signal in procfs.signals)


def test_a_reused_pid_links_no_process_to_the_old_parent():
    """101 is first read as a child of our worker 100; its pid is then reused
    by a process of another execution, whose unreadable child 102 must not be
    attributed through the old parent link: neither SIGTERM on that scan nor,
    once 101 reads as foreign, SIGKILL on a later one."""
    processes = {
        "100": {"environment": OURS, "start": 1000},
        "101": {"environment": OTHER, "ppid": 200, "start": 3000},
        "102": {
            "unreadable": True, "ppid": 101, "start": 3100, "ignores_term": True,
        },
        "200": {"environment": OTHER, "start": 2900},
    }

    class Reused(FakeProcfs):
        def read(self, name, thread, filename):
            if (
                name == "101" and filename == "stat" and thread is None
                and not self.stat_reads.get(name)
            ):
                self.stat_reads[name] = 1
                return stat_line(name, "worker", "S", 100, 0, 1500)
            return super().read(name, thread, filename)

    procfs = Reused(processes)
    procfs.run(task_process_cleanup._CLEANUP_PROGRAM, MARKER)
    assert procfs.signals == [(100, signal.SIGTERM)]
    assert not procfs.open_handles


def test_discovery_keeps_no_pidfd_at_all():
    """Discovery signals nothing, so not even an unreadable process keeps its
    pidfd until the scan ends."""
    processes = {"100": {"environment": b"PATH=/usr/bin", "start": 1000}}
    processes.update({
        str(200 + index): {"unreadable": True, "ppid": 100, "start": 2000}
        for index in range(60)
    })
    procfs = FakeProcfs(processes)
    procfs.run(task_process_cleanup._DISCOVER_PROGRAM, MARKER)
    assert procfs.peak_open_handles <= 1
    assert not procfs.open_handles


@BOTH_PROGRAMS
def test_running_out_of_open_files_fails_the_scan_plainly(program):
    """Out of open files, no process can be judged: the scan fails with that
    error instead of reporting the process as unreadable."""

    class Exhausted(FakeProcfs):
        def read(self, name, thread, filename):
            if name == "101" and filename == "stat":
                raise OSError(24, "Too many open files")
            return super().read(name, thread, filename)

    procfs = Exhausted({
        "100": {"environment": OURS, "start": 1000},
        "101": {"environment": OURS, "ppid": 100, "start": 1100},
    })
    with pytest.raises(OSError) as failure:
        procfs.run(program, MARKER)
    assert failure.value.errno == 24
    assert not procfs.open_handles
    assert task_process_cleanup._cleanup_failure_reason(
        b"OSError: [Errno 24] Too many open files"
    ) == "too_many_open_files"


def test_discovery_tolerates_dying_and_attributed_unreadable_processes():
    processes = {
        "101": {"environment": OURS},
        "102": {"unreadable": True, "ppid": 101},
        "201": {"environment": b"HOME=/home/agent"},
        "202": {"unreadable": True, "ppid": 201},
        "301": {"unreadable": True, "flags": PF_EXITING, "ppid": 1},
    }
    procfs = FakeProcfs(processes)
    procfs.run(task_process_cleanup._DISCOVER_PROGRAM)
    assert json.loads(procfs.output[0]) == [MARKER]
    assert procfs.signals == []
    assert procfs.now < 1


def test_discovery_waits_out_a_transient_unreadable_process():
    processes = {"101": {"unreadable": [True, True, True, False]}}
    procfs = FakeProcfs(processes)
    procfs.run(task_process_cleanup._DISCOVER_PROGRAM)
    assert json.loads(procfs.output[0]) == []


def test_discovery_fails_closed_with_a_diagnostic_after_its_rescans():
    processes = {"101": {"unreadable": True, "ppid": 1, "comm": "ssh-agent"}}
    procfs = FakeProcfs(processes)
    with pytest.raises(RuntimeError, match="Cannot verify orphan worker cleanup"):
        procfs.run(task_process_cleanup._DISCOVER_PROGRAM)
    assert procfs.now >= 15
    assert procfs.diagnostic()["comm"] == "ssh-agent"


def test_legacy_script_variant_uses_the_same_rules():
    program = task_process_cleanup._CLEANUP_PROGRAM.replace(
        "CUBICLE_WORKER_EXECUTION_ID=", "CUBICLE_EXECUTION_ID="
    )
    execution = "exec-2026-09-21T12-00-00-123abc"
    processes = {
        "101": {"environment": f"CUBICLE_EXECUTION_ID={execution}".encode()},
        "102": {"unreadable": True, "ppid": 101},
        "103": {"environment": OURS},
    }
    procfs = FakeProcfs(processes)
    procfs.run(program, execution)
    assert sorted(procfs.signals) == [(101, signal.SIGTERM), (102, signal.SIGTERM)]


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
    assert isinstance(caught.value, task_process_cleanup.WorkerCleanupError)
    assert caught.value.reason == reason


async def test_cleanup_error_names_the_blocking_process_without_its_command(monkeypatch):
    stderr = (
        b'CUBICLE_CLEANUP_DIAGNOSTIC {"reason": "live_process_unreadable", "count": 2, '
        b'"pid": 4321, "comm": "ssh-agent\\n$(secret)", "state": "S", "exiting": false, '
        b'"age_seconds": 5400, "cmdline": "ssh-agent -a token"}\n'
        b"RuntimeError: Cannot inspect a worker-owned container process\n"
    )
    process = SimpleNamespace(returncode=1, communicate=AsyncMock(return_value=(b"", stderr)))
    monkeypatch.setattr(
        task_process_cleanup.asyncio, "create_subprocess_exec", AsyncMock(return_value=process),
    )
    with pytest.raises(task_process_cleanup.WorkerCleanupError) as caught:
        await task_process_cleanup.terminate_worker_execution("office", "a" * 64)
    assert caught.value.diagnostic == (
        "live_process_unreadable: ssh-agent???secret? (pid 4321, running 90 min) and 1 more"
    )
    assert "token" not in str(caught.value)


def test_cleanup_failure_text_never_carries_an_exception_message():
    text = task_process_cleanup.cleanup_failure_text
    assert text(RuntimeError("/home/agent/.ssh/private")) == "cleanup_failed (RuntimeError)"
    assert text(TimeoutError()) == "helper_timeout"
    assert text(FileNotFoundError("docker")) == "docker_unavailable"
    assert task_process_cleanup.is_helper_failure(
        task_process_cleanup.WorkerCleanupError("processes_remain")
    )
    assert not task_process_cleanup.is_helper_failure(RuntimeError("Docker unavailable"))
