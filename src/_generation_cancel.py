"""Real cancellation for in-container generation runs (X32).

A generation call is ``docker exec … generation_runner.py`` driven through
``asyncio.to_thread(subprocess.run, …)``. Cancelling the awaiting asyncio task
cannot interrupt that thread, and killing the host ``docker exec`` client does
not stop the process inside the container (no TTY, no signal forwarding). The
wizard's "cancel the failed phase's siblings" therefore used to leave every
sibling burning model spend until its own timeout.

Each run is a :class:`GenerationRun` with a unique, non-secret marker in the
runner's environment (``docker exec -e CBCL_GENERATION_MARKER=<marker>``). The
runner strips its environment before spawning the CLI, so the marker lives on
the runner process only; the CLI and its helpers are found as the runner's
descendants. When the owning task is cancelled,
:func:`schedule_generation_kill`:

* marks the run cancelled, so a run that has not launched ``docker exec`` yet
  (still waiting for admission or a worker thread) never launches it (CM7);
* while a launched run has not returned, repeats a small isolated Python scan
  inside the same container (as the same ``agent`` user) a bounded number of
  times, so a runner that was still starting when the first scan ran is
  caught by a later one. The scan signals the runner and every descendant:
  SIGTERM, a short grace, then SIGKILL for survivors. It holds a pidfd for
  every process it signals, so a PID reused during the grace period is never
  hit; without pidfd support it re-checks the process start time before the
  SIGKILL instead.

This is best-effort cleanup, never an authority. Runtime admission is still
held until the bounded host call returns, so a failed kill only means the run
ends at its normal deadline, exactly as before.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import threading
import uuid
from typing import Any

logger = logging.getLogger(__name__)

GENERATION_MARKER_ENV = "CBCL_GENERATION_MARKER"
_MARKER_RE = re.compile(r"cbcl-gen-[0-9a-f]{32}")
_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}")
_KILL_TIMEOUT_SECONDS = 20
# While a launched run has not returned, the scan repeats this many times,
# waiting this long for the run to end after each one.
_KILL_MAX_ATTEMPTS = 8
_KILL_RETRY_SECONDS = 2.0

# Runs inside the office container as ``agent``. Finds the runner whose
# environment carries the marker, then its descendants via the ppid map, and
# signals them. A pidfd is opened BEFORE each process is inspected, so the fd
# names the inspected process (a PID reused afterwards only makes the signal
# fail with ESRCH). Without pidfd support the SIGKILL is sent only while the
# PID still has the start time the scan recorded. Prints the number of
# processes it signalled.
_KILL_SCRIPT = r"""
import os, select, signal, sys, time
needle = b"CBCL_GENERATION_MARKER=" + sys.argv[1].encode()
me = os.getpid()
use_pidfd = hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")
def stat_of(pid):
    with open("/proc/%d/stat" % pid, "rb") as handle:
        stat = handle.read()
    fields = stat[stat.rindex(b")") + 2:].split()
    return int(fields[1]), fields[19]
procs = {}
roots = []
for name in os.listdir("/proc"):
    if not name.isdigit() or int(name) == me:
        continue
    pid = int(name)
    fd = None
    try:
        if use_pidfd:
            try:
                fd = os.pidfd_open(pid)
            except ProcessLookupError:
                continue
            except OSError:
                use_pidfd = False
        ppid, start = stat_of(pid)
        with open("/proc/%d/environ" % pid, "rb") as handle:
            if needle in handle.read().split(b"\0"):
                roots.append(pid)
        procs[pid] = (ppid, start, fd)
        fd = None
    except (OSError, ValueError, IndexError):
        continue
    finally:
        if fd is not None:
            os.close(fd)
targets = set(roots)
frontier = list(roots)
while frontier:
    parent = frontier.pop()
    for pid, entry in procs.items():
        if entry[0] == parent and pid not in targets:
            targets.add(pid)
            frontier.append(pid)
for pid, entry in procs.items():
    if pid not in targets and entry[2] is not None:
        os.close(entry[2])
def send(pid, sig):
    ppid, start, fd = procs[pid]
    try:
        if fd is not None:
            signal.pidfd_send_signal(fd, sig)
        elif stat_of(pid)[1] == start:
            os.kill(pid, sig)
    except (OSError, ValueError, IndexError):
        pass
for pid in targets:
    send(pid, signal.SIGTERM)
if targets:
    deadline = time.monotonic() + 2
    waiting = {procs[pid][2]: pid for pid in targets if procs[pid][2] is not None}
    while waiting and time.monotonic() < deadline:
        left = max(0.0, deadline - time.monotonic())
        for fd in select.select(list(waiting), [], [], left)[0]:
            waiting.pop(fd, None)
    untracked = [pid for pid in targets if procs[pid][2] is None]
    if untracked:
        time.sleep(max(0.0, deadline - time.monotonic()))
    for pid in untracked + list(waiting.values()):
        send(pid, signal.SIGKILL)
for pid in targets:
    if procs[pid][2] is not None:
        os.close(procs[pid][2])
print(len(targets))
"""


class GenerationCancelledError(RuntimeError):
    """The run was cancelled before ``docker exec`` launched; nothing ran."""


class GenerationRun:
    """One generation call: its marker and its cancel/launch/finish state.

    :meth:`launch` runs in the worker thread that owns the ``docker exec``
    client. It publishes ``launched`` BEFORE checking ``cancelled``, and
    :meth:`cancel` sets ``cancelled`` BEFORE reading ``launched`` (both are
    lock-backed events), so either the launch is refused or the canceller
    sees that it may have started — never neither.
    """

    def __init__(self) -> None:
        self.marker = new_generation_marker()
        self._cancelled = threading.Event()
        self._launched = threading.Event()
        self._finished = threading.Event()

    @property
    def finished(self) -> bool:
        return self._finished.is_set()

    def launch(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        """``subprocess.run(command, **kwargs)`` unless already cancelled."""
        self._launched.set()
        try:
            if self._cancelled.is_set():
                raise GenerationCancelledError(
                    "Generation was cancelled before it started; nothing ran."
                )
            return subprocess.run(command, **kwargs)
        finally:
            self._finished.set()

    def cancel(self) -> bool:
        """Mark cancelled; return True when the runner may still be running."""
        self._cancelled.set()
        return self._launched.is_set() and not self._finished.is_set()

    def wait_finished(self, timeout: float) -> bool:
        return self._finished.wait(timeout)


_pending_kills: set[asyncio.Task] = set()


def new_generation_marker() -> str:
    """Return a fresh, non-secret identifier for one generation run."""
    return f"cbcl-gen-{uuid.uuid4().hex}"


def marker_env_args(marker: str | None) -> list[str]:
    """``docker exec`` arguments that tag the runner with ``marker``."""
    if marker is None:
        return []
    if not _MARKER_RE.fullmatch(marker):
        raise ValueError("invalid generation marker")
    return ["-e", f"{GENERATION_MARKER_ENV}={marker}"]


def kill_command(container_id: str, marker: str) -> list[str] | None:
    """Build the in-container kill command, or ``None`` when inputs are unsafe.

    Only an immutable 64-hex container id and a well-formed marker are
    accepted: a name could be re-bound to another container, and the marker
    reaches the scan as a single argv element.
    """
    if not isinstance(container_id, str) or not _CONTAINER_ID_RE.fullmatch(
        container_id
    ):
        return None
    if not isinstance(marker, str) or not _MARKER_RE.fullmatch(marker):
        return None
    return [
        "docker",
        "exec",
        "-u",
        "agent",
        container_id,
        "/usr/local/bin/python3",
        "-I",
        "-S",
        "-c",
        _KILL_SCRIPT,
        marker,
    ]


def kill_generation(container_id: str, marker: str) -> int | None:
    """Signal the marked runner and its descendants; return how many.

    Returns ``None`` when the kill could not be attempted or its outcome is
    unknown (unsafe inputs, docker failure, timeout). Never raises.
    """
    command = kill_command(container_id, marker)
    if command is None:
        return None
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_KILL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Generation cancel: kill command failed: %s", exc)
        return None
    if result.returncode != 0:
        logger.warning(
            "Generation cancel: kill exited rc=%s: %s",
            result.returncode,
            (result.stderr or "").strip()[:300],
        )
        return None
    try:
        return int((result.stdout or "").strip() or "0")
    except ValueError:
        return None


async def _kill_until_stopped(container_id: str, run: GenerationRun) -> None:
    """Repeat the in-container kill until the launched run returns (bounded)."""
    signalled_total = 0
    for _attempt in range(_KILL_MAX_ATTEMPTS):
        if run.finished:
            break
        signalled = await asyncio.to_thread(kill_generation, container_id, run.marker)
        signalled_total += signalled or 0
        if await asyncio.to_thread(run.wait_finished, _KILL_RETRY_SECONDS):
            break
    if run.finished:
        logger.info(
            "Generation cancel: the run stopped (signalled %d process(es))",
            signalled_total,
        )
    else:
        logger.warning(
            "Generation cancel: could not confirm the in-container run stopped; "
            "it will end at its own deadline."
        )


def schedule_generation_kill(container_id: str, run: GenerationRun | None) -> None:
    """Cancel ``run`` and start best-effort in-container cleanup.

    Never blocks the canceller. A run that has not launched is refused at
    launch; a launched one is killed repeatedly until it returns (bounded).
    """
    if run is None or not run.cancel():
        return
    if kill_command(container_id, run.marker) is None:
        return
    try:
        task = asyncio.get_running_loop().create_task(
            _kill_until_stopped(container_id, run)
        )
    except RuntimeError:
        return
    _pending_kills.add(task)
    task.add_done_callback(_pending_kills.discard)
