"""X32 — cancelling a generation run stops the in-container process.

``_run_claude_cli`` runs ``docker exec … generation_runner.py`` in a worker
thread; cancelling the awaiting task could not interrupt it, so "cancelled"
wizard siblings kept spending until their own timeout. Each run now carries a
marker in the runner's environment and a cancelled caller schedules an
in-container kill of the runner and its descendants.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import src._generation_cancel as gc
import src._setup_cli as cli

CONTAINER_ID = "a" * 64


def test_marker_shape_and_env_args():
    marker = gc.new_generation_marker()
    assert marker.startswith("cbcl-gen-") and len(marker) == len("cbcl-gen-") + 32
    assert gc.marker_env_args(marker) == ["-e", f"CBCL_GENERATION_MARKER={marker}"]
    assert gc.marker_env_args(None) == []
    with pytest.raises(ValueError):
        gc.marker_env_args("cbcl-gen-$(rm -rf /)")


def test_generation_command_tags_runner_without_moving_the_tail():
    marker = gc.new_generation_marker()
    command = cli._generation_command(CONTAINER_ID, marker)
    assert f"CBCL_GENERATION_MARKER={marker}" in command
    # The environment flag precedes the container; the helper invocation tail
    # (pinned elsewhere) is unchanged.
    assert command.index(f"CBCL_GENERATION_MARKER={marker}") < command.index(
        CONTAINER_ID
    )
    assert command[-3:] == ["-I", "-S", cli._GENERATION_RUNNER]
    assert "CBCL_GENERATION_MARKER" not in " ".join(
        cli._generation_command(CONTAINER_ID)
    )


def test_kill_command_requires_immutable_container_and_valid_marker():
    marker = gc.new_generation_marker()
    assert gc.kill_command("office-name", marker) is None
    assert gc.kill_command(CONTAINER_ID, "not-a-marker") is None
    command = gc.kill_command(CONTAINER_ID, marker)
    assert command[:5] == ["docker", "exec", "-u", "agent", CONTAINER_ID]
    assert command[-1] == marker
    assert command[5:9] == ["/usr/local/bin/python3", "-I", "-S", "-c"]


def test_kill_generation_never_raises(monkeypatch):
    marker = gc.new_generation_marker()

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 20)

    monkeypatch.setattr(gc.subprocess, "run", boom)
    assert gc.kill_generation(CONTAINER_ID, marker) is None
    assert gc.kill_generation("bad", marker) is None


@pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="needs /proc")
@pytest.mark.parametrize("pidfd", [True, False], ids=["pidfd", "no-pidfd"])
def test_kill_script_stops_marked_runner_and_descendants(pidfd):
    """The in-container scan kills the marked process tree and nothing else.

    With pidfd support every signal goes through a pidfd opened before the
    process was inspected; without it (``os.pidfd_open`` removed) the scan
    re-checks each process's start time before the delayed SIGKILL.
    """
    if pidfd and not hasattr(os, "pidfd_open"):
        pytest.skip("this kernel/Python has no pidfd_open")
    script = gc._KILL_SCRIPT if pidfd else "import os\ndel os.pidfd_open\n" + (
        gc._KILL_SCRIPT
    )
    marker = gc.new_generation_marker()
    other = gc.new_generation_marker()
    child_code = "import time; time.sleep(60)"
    runner_code = (
        "import subprocess, sys, os, time\n"
        f"env = {{'PATH': os.environ.get('PATH', '')}}\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}], env=env)\n"
        "time.sleep(60)\n"
    )
    marked = subprocess.Popen(
        [sys.executable, "-c", runner_code],
        env={**os.environ, "CBCL_GENERATION_MARKER": marker},
    )
    bystander = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env={**os.environ, "CBCL_GENERATION_MARKER": other},
    )
    child_pid = None
    child_fd = None
    try:
        # Wait for the marked runner to spawn its (unmarked) child.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and child_pid is None:
            for name in os.listdir("/proc"):
                if not name.isdigit():
                    continue
                try:
                    stat = Path(f"/proc/{name}/stat").read_bytes()
                except OSError:
                    continue
                ppid = int(stat[stat.rindex(b")") + 2 :].split()[1])
                if ppid == marked.pid:
                    child_pid = int(name)
            time.sleep(0.05)
        assert child_pid is not None
        if hasattr(os, "pidfd_open"):
            # Pinned so the cleanup below can never signal a recycled PID.
            with contextlib.suppress(ProcessLookupError):
                child_fd = os.pidfd_open(child_pid)
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", script, marker],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert int(result.stdout.strip()) == 2
        marked.wait(timeout=10)
        assert marked.returncode is not None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                stat = Path(f"/proc/{child_pid}/stat").read_bytes()
            except OSError:
                break
            state = stat[stat.rindex(b")") + 2 :].split()[0]
            if state == b"Z":
                break
            time.sleep(0.05)
        else:
            pytest.fail("marked runner's child survived the kill")
        assert bystander.poll() is None
    finally:
        for process in (marked, bystander):
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        # A failed kill must not orphan the runner's unmarked child: SIGKILL
        # on the runner does not reach it.
        _kill_leftover_child(child_pid, child_fd, child_code)


def _kill_leftover_child(
    child_pid: int | None, child_fd: int | None, child_code: str
) -> None:
    if child_fd is not None:
        with contextlib.suppress(ProcessLookupError):
            signal.pidfd_send_signal(child_fd, signal.SIGKILL)
        os.close(child_fd)
        return
    if child_pid is None:
        return
    try:
        cmdline = Path(f"/proc/{child_pid}/cmdline").read_bytes()
    except OSError:
        return
    # Without a pidfd, confirm the PID still runs the test's child.
    if child_code.encode() in cmdline:
        with contextlib.suppress(ProcessLookupError):
            os.kill(child_pid, signal.SIGKILL)


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected"),
    [(0, "2\n", 2), (0, "", 0), (1, "2", None), (0, "signalled", None)],
    ids=["count", "none-found", "nonzero-rc", "unparsable"],
)
def test_kill_generation_interprets_the_scan_result(
    monkeypatch, returncode, stdout, expected
):
    """B7b-tests-07: the scan's exit code is checked before its count."""
    marker = gc.new_generation_marker()
    seen = []

    def fake_run(command, **kwargs):
        seen.append(command)
        return subprocess.CompletedProcess(command, returncode, stdout, "boom")

    monkeypatch.setattr(gc.subprocess, "run", fake_run)
    assert gc.kill_generation(CONTAINER_ID, marker) == expected
    assert seen == [gc.kill_command(CONTAINER_ID, marker)]


@pytest.mark.asyncio
async def test_cancelled_caller_schedules_in_container_kill(monkeypatch):
    """No-runtime path: the cancelled caller kills the marked run."""
    started = asyncio.Event()
    seen: dict = {}
    kills: list[tuple[str, str]] = []

    async def generation(**kwargs):
        seen.update(kwargs)
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(cli, "_run_claude_cli_admitted", generation)
    monkeypatch.setattr(
        cli, "schedule_generation_kill", lambda c, m: kills.append((c, m))
    )
    monkeypatch.setattr("src.runtime_state.generation_runtime", lambda _container: None)
    caller = asyncio.create_task(cli._run_claude_cli(CONTAINER_ID, "sys", "usr"))
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert kills == [(CONTAINER_ID, seen["run"])]
    # The run is the one carrier of its marker (B5-hygiene-12).
    assert "marker" not in seen


@pytest.mark.asyncio
async def test_completed_run_is_never_killed(monkeypatch):
    kills: list = []

    async def generation(**kwargs):
        return "ok"

    monkeypatch.setattr(cli, "_run_claude_cli_admitted", generation)
    monkeypatch.setattr(
        cli, "schedule_generation_kill", lambda c, m: kills.append((c, m))
    )
    monkeypatch.setattr("src.runtime_state.generation_runtime", lambda _container: None)
    assert await cli._run_claude_cli(CONTAINER_ID, "sys", "usr") == "ok"
    assert kills == []


@pytest.mark.asyncio
async def test_admitted_run_receives_marker_on_the_docker_command(monkeypatch):
    runs: list = []

    def fake_run(command, **kwargs):
        runs.append(command)
        return subprocess.CompletedProcess(command, 0, "fine", "")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr("src.runtime_state.generation_runtime", lambda _container: None)
    assert await cli._run_claude_cli(CONTAINER_ID, "sys", "usr") == "fine"
    tagged = [part for part in runs[0] if part.startswith("CBCL_GENERATION_MARKER=")]
    assert len(tagged) == 1


class _FakeRuntime:
    def __init__(self) -> None:
        self.held = 0

    def admission(self, _kind):
        runtime = self

        class _Admission:
            def __enter__(self_inner):
                runtime.held += 1

            def __exit__(self_inner, *exc):
                runtime.held -= 1
                return False

        return _Admission()


@pytest.mark.asyncio
async def test_shielded_run_keeps_admission_and_schedules_kill(monkeypatch):
    """Admission stays held until the (killed) run returns; the kill fires."""
    runtime = _FakeRuntime()
    started = asyncio.Event()
    release = asyncio.Event()
    kills: list = []

    async def generation(**kwargs):
        started.set()
        await release.wait()
        raise RuntimeError("Claude CLI failed (rc=143): killed")

    monkeypatch.setattr(cli, "_run_claude_cli_admitted", generation)
    monkeypatch.setattr(
        cli, "schedule_generation_kill", lambda c, m: kills.append((c, m))
    )
    monkeypatch.setattr(
        "src.runtime_state.generation_runtime", lambda _container: runtime
    )
    caller = asyncio.create_task(cli._run_claude_cli(CONTAINER_ID, "sys", "usr"))
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert len(kills) == 1 and kills[0][0] == CONTAINER_ID
    assert isinstance(kills[0][1], gc.GenerationRun)
    assert runtime.held == 1  # still admitted while the run winds down
    release.set()
    await asyncio.gather(*list(cli._admitted_generation_tasks), return_exceptions=True)
    assert runtime.held == 0


# ---------------------------------------------------------------------------
# CM7 — a cancellation that arrives before the launch refuses it, and a
# launched run is killed repeatedly (bounded) until it returns.
# ---------------------------------------------------------------------------


def test_cancelled_run_never_launches(monkeypatch):
    calls: list = []
    monkeypatch.setattr(gc.subprocess, "run", lambda *a, **k: calls.append(a))
    run = gc.GenerationRun()
    assert run.cancel() is False  # nothing launched: no kill needed
    with pytest.raises(gc.GenerationCancelledError):
        run.launch(["docker", "exec"])
    assert calls == []
    assert run.finished


def test_cancel_reports_a_launched_unfinished_run(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def slow_run(*args, **kwargs):
        started.set()
        release.wait(10)
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr(gc.subprocess, "run", slow_run)
    run = gc.GenerationRun()
    worker = threading.Thread(target=run.launch, args=(["docker", "exec"],))
    worker.start()
    assert started.wait(10)
    assert run.cancel() is True
    release.set()
    worker.join(10)
    assert run.finished and run.cancel() is False


@pytest.mark.asyncio
async def test_run_cancelled_while_awaiting_admission_never_launches(monkeypatch):
    """CM7: the shielded run must not launch after its caller was cancelled.

    The single kill scan used to run before the runner existed; the run then
    launched and spent its full timeout.
    """
    runtime = _FakeRuntime()
    launches: list = []
    gate = asyncio.Event()
    real_admitted = cli._run_claude_cli_admitted

    async def gated(**kwargs):
        await gate.wait()  # e.g. waiting for admission or a worker thread
        return await real_admitted(**kwargs)

    def fake_run(command, **kwargs):
        launches.append(command)
        return subprocess.CompletedProcess(command, 0, "late", "")

    monkeypatch.setattr(cli, "_run_claude_cli_admitted", gated)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(
        "src.runtime_state.generation_runtime", lambda _container: runtime
    )
    caller = asyncio.create_task(cli._run_claude_cli(CONTAINER_ID, "sys", "usr"))
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    gate.set()
    results = await asyncio.gather(
        *list(cli._admitted_generation_tasks), return_exceptions=True
    )
    assert launches == []
    assert any(isinstance(r, gc.GenerationCancelledError) for r in results)
    assert runtime.held == 0


class _LaunchedRun(gc.GenerationRun):
    """A run that is 'launched' and ends after ``stop_after`` kill scans."""

    def __init__(self, stop_after: int | None) -> None:
        super().__init__()
        self._launched.set()
        self.stop_after = stop_after
        self.scans = 0

    def scanned(self) -> None:
        self.scans += 1
        if self.stop_after is not None and self.scans >= self.stop_after:
            self._finished.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_after", [3, None], ids=["caught-later", "bounded"])
async def test_kill_repeats_until_the_run_returns(monkeypatch, stop_after):
    run = _LaunchedRun(stop_after)

    def fake_kill(container_id, marker):
        assert (container_id, marker) == (CONTAINER_ID, run.marker)
        run.scanned()
        # The first scans find nothing: the runner had not started yet.
        return 1 if run.finished else 0

    monkeypatch.setattr(gc, "kill_generation", fake_kill)
    monkeypatch.setattr(gc, "_KILL_RETRY_SECONDS", 0.01)
    gc.schedule_generation_kill(CONTAINER_ID, run)
    await asyncio.gather(*list(gc._pending_kills))
    expected = stop_after if stop_after is not None else gc._KILL_MAX_ATTEMPTS
    assert run.scans == expected


@pytest.mark.asyncio
async def test_unlaunched_or_unsafe_runs_schedule_no_scan(monkeypatch):
    scans: list = []
    monkeypatch.setattr(gc, "kill_generation", lambda *a: scans.append(a))
    gc.schedule_generation_kill(CONTAINER_ID, None)
    gc.schedule_generation_kill(CONTAINER_ID, gc.GenerationRun())  # not launched
    gc.schedule_generation_kill("office-name", _LaunchedRun(1))  # mutable name
    await asyncio.sleep(0)
    assert scans == [] and not gc._pending_kills
