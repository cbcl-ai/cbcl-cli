"""Script execution monitoring and completion handling.

Contains the monitoring loop, completion handler, and progress checking
logic extracted from ScriptRunner.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from src.scripts.outbox_watcher import scan_and_dispatch
from src.scripts.script_notifier import (
    notify_completion,
    read_progress,
    report_progress,
    write_status,
)

if TYPE_CHECKING:
    from src.scripts.script_runner import _Execution

logger = logging.getLogger(__name__)

# Grace between SIGTERM and SIGKILL when stopping an in-container script.
_IN_CONTAINER_KILL_GRACE_SECONDS = 5.0
# Cap each ``docker exec ... kill`` so a wedged daemon can't block the
# monitor loop / shutdown indefinitely.
_DOCKER_KILL_TIMEOUT_SECONDS = 10.0


def _read_in_container_pid(exec_dir: Path) -> int | None:
    """Read the in-container PID the launch wrapper recorded for this
    run, or ``None`` if the pidfile is absent / empty / malformed (the
    common pre-start race or a host-fallback run that wrote none)."""
    # Local import to avoid any import-order coupling with script_runner.
    from src.scripts.script_runner import _IN_CONTAINER_PID_FILE

    try:
        raw = (exec_dir / _IN_CONTAINER_PID_FILE).read_text().strip()
    except (OSError, ValueError):
        return None
    if not raw:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 1 else None


async def _docker_exec_kill(container: str, pid: int, signal_name: str) -> None:
    """``docker exec <container>`` a shell ``kill`` of ``pid`` (and its
    process group, best-effort) with ``signal_name`` (TERM / KILL).

    Routed through ``sh -c`` so we rely only on the shell's ``kill``
    builtin — the slim agent image ships ``/bin/sh`` (dash) but not a
    standalone ``kill``/``pkill`` binary. The group form (``-pid``) is
    a best-effort reap of children the script forked; it harmlessly
    no-ops when the process isn't a group leader, and the bare ``pid``
    form always covers the script process itself.

    Best-effort + PID-based: there is a narrow TOCTOU window where a
    just-exited PID is reused by an unrelated in-container process
    between the probe and the signal. Accepted — the window is
    sub-second, container-scoped, and the alternative (a start-time /
    cmdline cross-check on every kill) is disproportionate for a
    shutdown/timeout/reconcile path.
    """
    argv = [
        "docker", "exec", container, "sh", "-c",
        f'kill -{signal_name} -"$1" 2>/dev/null; '
        f'kill -{signal_name} "$1" 2>/dev/null; true',
        "cubicle-kill", str(pid),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(
            proc.wait(), timeout=_DOCKER_KILL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "in-container kill timed out (pid=%s, container=%s, sig=%s)",
            pid, container, signal_name,
        )
    except OSError as exc:
        logger.warning(
            "in-container kill failed (pid=%s, container=%s): %s",
            pid, container, exc,
        )


async def _docker_pid_alive(container: str, pid: int) -> bool:
    """Return True iff ``pid`` is alive inside ``container``.

    Uses the shell ``kill -0`` builtin (signal 0 = existence probe, no
    signal delivered). Fail-CLOSED: any docker error / timeout returns
    False so reconciliation treats an unverifiable run as finished and
    stops reporting it ``running`` forever.
    """
    argv = [
        "docker", "exec", container, "sh", "-c",
        'kill -0 "$1" 2>/dev/null', "cubicle-probe", str(pid),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        rc = await asyncio.wait_for(
            proc.wait(), timeout=_DOCKER_KILL_TIMEOUT_SECONDS,
        )
        return rc == 0
    except (OSError, asyncio.TimeoutError):
        return False


async def reconcile_orphaned_executions(
    workspace_path: str,
    container_name: str | None,
) -> int:
    """Startup reconciliation of executions a previous daemon left
    ``running`` (ADD-C1).

    The office container is REUSED across daemon restarts, so a script
    the old daemon launched keeps running inside it even though the
    daemon (and its host-side ``docker exec`` client) is gone. The old
    behaviour blindly rewrote every ``running`` → ``failed`` — a lie:
    a job that is still running (or already succeeded) was reported
    failed, and the Manager could rework a run that actually worked.

    For each ``running`` execution we check the REAL in-container
    process via the recorded PID:

    * **alive** — the run was orphaned by the restart and can no longer
      be monitored (its exit code is unrecoverable once container PID 1
      reaps it). Kill it cleanly and mark ``failed`` with an honest,
      specific message so the Manager knows to re-run rather than
      misreading a generic failure.
    * **dead / no pidfile / host-fallback** — the run was interrupted;
      mark ``failed`` honestly.

    Returns the number of executions reconciled. Safe to call before the
    ScriptRunner starts tracking anything (it touches only on-disk
    status + the container).
    """
    scripts_dir = Path(workspace_path) / ".scripts"
    if not scripts_dir.exists():
        return 0

    count = 0
    for status_file in scripts_dir.glob("*/executions/*/status.json"):
        try:
            data = json.loads(status_file.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Could not read stale script status file %s: %s",
                status_file, exc,
            )
            continue
        if data.get("status") != "running":
            continue

        exec_dir = status_file.parent
        pid = _read_in_container_pid(exec_dir)
        message = "Communicator restarted while script was running"
        if container_name and pid and await _docker_pid_alive(
            container_name, pid
        ):
            # Orphaned but still alive — kill it (we can't recover its
            # exit code, and leaving it leaks an unmonitorable process).
            await _docker_exec_kill(container_name, pid, "TERM")
            await asyncio.sleep(_IN_CONTAINER_KILL_GRACE_SECONDS)
            await _docker_exec_kill(container_name, pid, "KILL")
            message = (
                "Communicator restarted; the previous run was orphaned "
                "and has been terminated. Re-run if needed."
            )

        data["status"] = "failed"
        data["error_message"] = message
        if data.get("completed_at") is None:
            data["completed_at"] = (
                datetime.now(timezone.utc).isoformat()
            )
        try:
            status_file.write_text(json.dumps(data, indent=2))
            count += 1
        except OSError as exc:
            logger.warning(
                "Could not update stale script status file %s: %s",
                status_file, exc,
            )
    return count


async def terminate_execution(execution: "_Execution") -> None:
    """Stop a running script INSIDE the container, not just the host
    ``docker exec`` client.

    NEW-2: ``docker exec`` without a TTY does NOT forward signals, so
    ``execution.process.terminate()`` only kills the host-side client
    while the in-container python keeps running (reparented to PID 1) —
    a 4-hour run reported "terminated" is in fact still writing outputs.
    We read the PID the launch wrapper recorded and ``docker exec …
    kill`` it (TERM → grace → KILL), then terminate the host client so
    the host-side ``proc.wait()`` returns and the monitor reaps the row.

    Best-effort and idempotent: a missing pidfile (pre-start race or a
    host-fallback run) falls back to terminating the client alone.
    """
    container = getattr(execution, "container_name", None)
    pid = _read_in_container_pid(execution.exec_dir)
    if container and pid:
        await _docker_exec_kill(container, pid, "TERM")
        await asyncio.sleep(_IN_CONTAINER_KILL_GRACE_SECONDS)
        # Re-check: if the host client already saw the child exit there
        # is nothing left to SIGKILL, but the group form is harmless.
        if execution.process.returncode is None:
            await _docker_exec_kill(container, pid, "KILL")
    # Always stop the host-side docker-exec client so the monitor's
    # proc.wait() unblocks and the execution is untracked.
    try:
        execution.process.terminate()
    except ProcessLookupError:
        pass


def _resolve_exit_code_via_waitpid(pid: int) -> int | None:
    """Direct-syscall fallback for asyncio child-watcher misses.

    User report 2026-05-29: manual script runs sat at ``status=running``
    indefinitely even though the in-container python process exited
    cleanly and the docker exec wrapper was gone from ``ps``. The
    ``asyncio.subprocess.Process.returncode`` property stayed at
    ``None`` so the polling monitor's ``if exit_code is not None``
    branch never fired and ``on_complete`` was never called.

    Root cause: Python 3.12's ``ThreadedChildWatcher`` (the default
    on Linux) spawns a thread per child that calls ``os.waitpid``
    BLOCKING. The thread relies on the parent never having reaped
    the child via another path. Under heavy spawn concurrency
    (manager + worker agents + claude CLI subprocesses + docker
    exec wrappers all alive at once), the watcher occasionally
    loses track and the loop's transport callback is never invoked.

    This helper short-circuits the watcher with a non-blocking
    ``WNOHANG`` check. Returns:

    * ``None`` — child still running (or ``waitpid`` not supported).
    * ``exit_code`` (int, can be negative for signal termination) —
      child has exited; the caller should run ``on_complete``.
    """
    if not hasattr(os, "waitpid") or pid <= 0:
        return None
    try:
        pid_seen, raw_status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        # Already reaped by some other path (the asyncio watcher's
        # thread DID see the exit but never delivered the
        # transport callback; OR a signal handler reaped first).
        # We can't recover the real exit code from /proc at this
        # point — assume success unless the log indicates otherwise.
        # Caller checks log content via ``_infer_exit_code_from_log``.
        return -1
    except OSError:
        # Defensive: any other waitpid failure means we shouldn't
        # second-guess the asyncio watcher. Try again next tick.
        return None
    if pid_seen == 0:
        return None  # Child still running
    # WIFEXITED / WIFSIGNALED translation. Python 3.9+ ships
    # waitstatus_to_exitcode; the manual fallback handles older.
    if hasattr(os, "waitstatus_to_exitcode"):
        return os.waitstatus_to_exitcode(raw_status)
    return raw_status >> 8


def _infer_exit_code_from_log(log_path: Path) -> int:
    """Heuristic exit code when waitpid returned ChildProcessError.

    Returns 0 if the log has content (script printed something →
    likely ran successfully and exited cleanly), 1 otherwise.
    """
    try:
        return 0 if log_path.stat().st_size > 0 else 1
    except OSError:
        return 1


async def monitor_all(
    active: dict[str, _Execution],
    workspace_path: str,
    max_duration: int,
    ws,
    router: object | None = None,
    *,
    office_id: str = "",
    config_store: object | None = None,
    manager: object | None = None,
    active_by_task: dict[str, set[str]] | None = None,
) -> None:
    """Background loop: check active executions and scan their
    ``.outbox/`` for Manager-callback notifications.

    Cadence is 2 s while any script is running so Manager callbacks
    feel responsive, 10 s when idle to avoid unnecessary filesystem
    churn. Enforces a per-execution maximum duration as a safety
    net — scripts exceeding it are terminated.

    ``config_store`` + ``manager`` + ``office_id`` are keyword-only
    so existing callers that don't wire them (tests, older code
    paths) still work — outbox dispatch is a no-op without them.
    """
    workspace = Path(workspace_path)
    try:
        while True:
            # Tight cadence while work is in flight so a
            # `cubicle.notify_manager(...)` call feels snappy to the
            # user watching the chat. Falls back to the old 10 s
            # idle cadence when no scripts are running so we don't
            # burn CPU on empty scans.
            tick = 2 if active else 10
            await asyncio.sleep(tick)
            now = datetime.now(timezone.utc)
            for exec_id in list(active):
                try:
                    execution = active.get(exec_id)
                    if execution is None:
                        continue
                    exit_code = execution.process.returncode
                    # Watcher-miss fallback (user report 2026-05-29):
                    # manual runs stuck at status=running because the
                    # asyncio child watcher occasionally drops the
                    # SIGCHLD callback under heavy concurrency. A
                    # WNOHANG probe catches the exit and lets the
                    # completion path run. ChildProcessError means
                    # some other path reaped first — infer exit code
                    # from log content so the user isn't permanently
                    # stuck on a successful run.
                    if exit_code is None:
                        probed = _resolve_exit_code_via_waitpid(
                            execution.process.pid,
                        )
                        if probed is not None:
                            if probed == -1:
                                # ChildProcessError → already reaped
                                exit_code = _infer_exit_code_from_log(
                                    execution.exec_dir / "log.txt",
                                )
                                logger.warning(
                                    "Script '%s' exit not observed by "
                                    "asyncio watcher; recovered via log "
                                    "heuristic (exit_code=%d). exec=%s",
                                    execution.script_name, exit_code,
                                    execution.exec_id,
                                )
                            else:
                                exit_code = probed
                                logger.warning(
                                    "Script '%s' exit not observed by "
                                    "asyncio watcher; recovered via "
                                    "WNOHANG (exit_code=%d). exec=%s",
                                    execution.script_name, exit_code,
                                    execution.exec_id,
                                )
                    if exit_code is not None:
                        await on_complete(
                            execution, exit_code, active, workspace, ws,
                            router=router,
                            office_id=office_id,
                            config_store=config_store,
                            manager=manager,
                            active_by_task=active_by_task,
                        )
                    else:
                        elapsed = (now - execution.started_at).total_seconds()
                        if elapsed > max_duration:
                            logger.warning(
                                "Script '%s' (exec_id=%s) exceeded max "
                                "duration of %ds — terminating",
                                execution.script_name,
                                execution.exec_id,
                                max_duration,
                            )
                            await terminate_execution(execution)
                            await on_complete(
                                execution, exit_code=-15,
                                active=active, workspace=workspace,
                                ws=ws, router=router,
                                timed_out=True, max_duration=max_duration,
                                office_id=office_id,
                                config_store=config_store,
                                manager=manager,
                                active_by_task=active_by_task,
                            )
                        else:
                            await check_progress(
                                execution, workspace, ws, router=router,
                            )
                            # Scan the script's outbox for any
                            # Manager callbacks the running script
                            # dropped since the last tick. Defensive
                            # no-op if the wiring isn't in place.
                            if config_store is not None and manager is not None:
                                await _scan_outbox(
                                    execution=execution,
                                    workspace=workspace,
                                    office_id=office_id,
                                    config_store=config_store,
                                    manager=manager,
                                )
                except Exception as exc:
                    logger.exception(
                        "Error monitoring execution %s: %s", exec_id, exc,
                    )
    except asyncio.CancelledError:
        pass


async def _scan_outbox(
    *,
    execution: _Execution,
    workspace,
    office_id: str,
    config_store,
    manager,
) -> None:
    """Delegate to ``outbox_watcher.scan_and_dispatch`` for one
    execution. Exists as a thin wrapper so ``on_complete`` can call
    the same path on its last-before-pop sweep and so call sites
    stay symmetrical."""
    script_dir = workspace / ".scripts" / execution.script_name
    await scan_and_dispatch(
        script_dir=script_dir,
        script_name=execution.script_name,
        office_id=office_id,
        config_store=config_store,
        manager=manager,
        workspace_root=workspace,
    )


async def on_complete(
    execution: _Execution,
    exit_code: int,
    active: dict[str, _Execution],
    workspace,
    ws,
    router: object | None = None,
    timed_out: bool = False,
    max_duration: int = 0,
    *,
    office_id: str = "",
    config_store: object | None = None,
    manager: object | None = None,
    active_by_task: dict[str, set[str]] | None = None,
) -> None:
    """Handle script completion: write status, close the log, notify.

    (Historical note: v2 mini-projects run ``python -m`` with variables
    injected via ``docker exec -e`` and never materialise a ``_run.py``,
    so there is no secret-bearing temp file to clean up here.)

    Also does one final outbox scan BEFORE popping the execution
    from ``active`` — a script that drops a notification in its
    last few milliseconds before exit would otherwise be missed
    (the monitor loop only scans while the execution is still
    tracked).
    """
    # Final-scan: pick up any notify files the script dropped
    # between the last monitor tick and its exit. No-op when the
    # outbox wiring isn't plumbed (tests / host fallback).
    if config_store is not None and manager is not None:
        try:
            await _scan_outbox(
                execution=execution,
                workspace=workspace,
                office_id=office_id,
                config_store=config_store,
                manager=manager,
            )
        except Exception:
            logger.exception(
                "Final outbox scan failed for %s — continuing with "
                "completion handling",
                execution.exec_id,
            )
    now = datetime.now(timezone.utc)
    duration = (now - execution.started_at).total_seconds()

    # Determine status
    if timed_out:
        status = "timed_out"
    elif exit_code == 0:
        status = "completed"
    else:
        status = "failed"

    error_message = None
    if status != "completed":
        if timed_out:
            error_message = (
                f"Script exceeded maximum duration of "
                f"{max_duration}s and was terminated."
            )
        elif exit_code < 0:
            error_message = (
                f"Script killed by signal {-exit_code} "
                f"(possibly OOM killer or external termination)."
            )
        else:
            # SECURITY: do NOT ship the log tail to the backend. log.txt
            # is the script's captured stdout/stderr and can contain
            # injected secret values (a traceback printing os.environ, an
            # API error echoing a key, etc.). Sending it would violate
            # "credentials never leave the user's machine". Send only a
            # generic message; the full log stays host-local at
            # ``exec_dir/log.txt`` for debugging.
            error_message = (
                f"Script failed (exit code {exit_code}). "
                "See the local execution log for details."
            )

    # No try/finally wrapper here: commit 22a8efb (v1→v2 refactor)
    # deleted the matching ``finally:`` block that used to clean up
    # ``_run.py`` (v1 wrote it with inlined secrets), but left the
    # bare ``try:`` dangling. The file has been syntactically broken
    # since 22a8efb — the parser raised a SyntaxError on import-at-
    # call-time (both ``monitor_all`` and ``on_complete`` were
    # imported INLINE from inside function bodies, so the failure
    # only fired the moment ``script_runner.monitor_all`` was
    # actually invoked). The asyncio.create_task wrapper swallowed
    # the exception and the host-side monitor loop never ran. Net
    # symptom: manual script runs sat at ``status=running`` forever
    # because nothing checked ``process.returncode`` or wrote
    # ``status.json``. Agent-triggered runs worked because they go
    # through the in-container MCP server's own monitor, not this
    # host-side path.
    write_status(execution.exec_dir, {
        "status": status,
        "started_at": execution.started_at.isoformat(),
        "completed_at": now.isoformat(),
        "duration_seconds": int(duration), "exit_code": exit_code,
        "task_id": execution.task_id,
        "triggered_by": execution.triggered_by,
        "error_message": error_message,
    })

    try:
        execution.log_handle.close()  # type: ignore[union-attr]
    except (OSError, AttributeError):
        pass

    # Per-execution collections token (script-lane completion #2,
    # 2026-08-21): revoke on EVERY terminal path — natural exit,
    # timeout kill, UI ``script_kill``, daemon shutdown — they all
    # funnel through ``on_complete``. Idempotent proxy-side (set
    # discard), so a double completion is safe; the spawn-failure
    # path revokes at its raise site since no ``_Execution`` exists
    # there. getattr-guarded for test stand-ins built without the
    # dataclass field.
    revoke = getattr(execution, "collections_token_revoke", None)
    if callable(revoke):
        try:
            revoke()
        except Exception:  # noqa: BLE001
            logger.warning(
                "Per-execution collections token revoke failed for %s",
                execution.exec_id, exc_info=True,
            )

    active.pop(execution.exec_id, None)
    # Keep the task-id index in sync so :meth:`has_active_scripts`
    # stays O(1). ``active_by_task`` is None in test paths that
    # pass a raw dict — the runner always passes its index.
    if active_by_task is not None and execution.task_id:
        bucket = active_by_task.get(execution.task_id)
        if bucket is not None:
            bucket.discard(execution.exec_id)
            if not bucket:
                del active_by_task[execution.task_id]

    logger.info(
        "Script '%s' %s: exec_id=%s exit_code=%d duration=%ds",
        execution.script_name, status, execution.exec_id,
        exit_code, int(duration),
    )

    # T8.3.2 (03/#19): the ws-protocol script_status enum is
    # {running, completed, failed} — ``timed_out`` is off-contract and the
    # frontend can't render it. Map it to ``failed`` ON THE WIRE (the timeout
    # detail is preserved in error_message); status.json above keeps the
    # richer ``timed_out`` for host-side consumers, and the history backfill
    # in handlers.py also maps it to ``failed`` on the wire (same posture).
    wire_status = "failed" if status == "timed_out" else status
    await notify_completion(
        ws=ws,
        router=router,
        script_name=execution.script_name,
        exec_id=execution.exec_id,
        task_id=execution.task_id,
        cron_id=execution.cron_id,
        triggered_by=execution.triggered_by,
        started_at_iso=execution.started_at.isoformat(),
        process_returncode=execution.process.returncode,
        status=wire_status, duration=duration,
        error_message=error_message,
        progress=await read_progress(workspace, execution.script_name),
    )
    # No per-execution cleanup needed: the runner launches
    # ``python -m main`` directly against the mini-project entry
    # module, so there's no materialised ``_run.py`` with inlined
    # secrets to delete. Secrets live in ``.secrets.json`` for the
    # lifetime of the workspace and are injected via ``docker exec
    # -e`` on each run.


async def check_progress(
    execution: _Execution,
    workspace,
    ws,
    router: object | None = None,
) -> None:
    """Read .progress.json and report if changed."""
    progress = await read_progress(workspace, execution.script_name)
    if not progress or progress == execution.last_progress:
        return
    execution.last_progress = progress
    await report_progress(
        ws, execution.script_name, execution.exec_id,
        execution.task_id, progress,
        router=router,
    )
