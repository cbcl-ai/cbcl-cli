"""Script notification, progress reporting, and completion handling.

Contains the completion handler, progress checker, orphaned file cleanup,
and notification logic extracted from ScriptRunner.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.connection.ws_client import PlatformWSClient

logger = logging.getLogger(__name__)


def format_duration(seconds: int) -> str:
    """Format a duration in seconds to a human-readable string like '1h 30m'."""
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    if remaining_minutes == 0:
        return f"{hours}h"
    return f"{hours}h {remaining_minutes}m"


async def _publish(
    router: object | None,
    ws: PlatformWSClient | None,
    event_type: str,
    payload: dict,
    context: str = "",
) -> None:
    """Publish an event via the WebSocket transport.

    Tries the transport router first. If the router is not available or
    raises an exception, falls back to the WebSocket client directly.
    """
    if router:
        try:
            await router.publish_event({"type": event_type, **payload})
            return
        except Exception as exc:
            logger.warning(
                "Transport publish failed for %s (%s), falling back to WS: %s",
                event_type, context, exc,
            )

    if ws:
        try:
            await ws.send({"type": event_type, **payload})
        except (OSError, ConnectionError, RuntimeError) as exc:
            logger.warning(
                "WS send failed for %s (%s): %s", event_type, context, exc,
            )


async def notify_completion(
    ws: PlatformWSClient | None,
    script_name: str,
    exec_id: str,
    task_id: str | None,
    triggered_by: str,
    started_at_iso: str,
    process_returncode: int | None,
    status: str,
    duration: float,
    error_message: str | None,
    progress: dict,
    router: object | None = None,
    cron_id: str | None = None,
) -> None:
    """Send completion notifications to the platform via the WebSocket transport."""
    duration_int = int(duration)
    duration_str = format_duration(duration_int)

    # 1. Script status event
    await _publish(router, ws, "script_status", {
        "script_name": script_name,
        "execution_id": exec_id, "status": status,
        "progress": progress, "task_id": task_id,
        "cron_id": cron_id,
        "error_message": error_message,
        "duration_seconds": duration_int,
        "started_at": started_at_iso, "triggered_by": triggered_by,
    }, context=f"script_status for {exec_id}")

    # 2. Task activity (if linked to a task)
    if task_id:
        status_label = (
            "completed successfully" if status == "completed"
            else f"failed (exit code {process_returncode})"
        )
        await _publish(router, ws, "task_activity", {
            "task_id": task_id,
            "event_type": "script_completed", "actor": "system",
            "content": (
                f"Script '{script_name}' {status_label}. "
                f"Duration: {duration_str}."
            ),
            "details": {
                "script_name": script_name, "execution_id": exec_id,
                "status": status, "duration_seconds": duration_int,
                "exit_code": process_returncode,
            },
        }, context=f"script_completed activity for {exec_id}")

    # 3. Manager notification
    # Note: scripts write outputs to whatever path they choose. New scripts
    # are taught (in the Automation Script Developer prompt) to use the
    # per-workstream convention `/workspace/outputs/{ws}/{scope?}/...`;
    # legacy scripts still write to the flat root. The Manager sees the
    # exact path via the script's logged output, not from this hint.
    output_hint = (
        "Results in /workspace/outputs/"
        if status == "completed"
        else f"Error: {(error_message or 'unknown')[:200]}"
    )
    payload: dict = {
        "script_name": script_name, "status": status,
        "duration_seconds": duration_int,
        "duration_display": duration_str, "output_hint": output_hint,
    }
    if task_id:
        payload["task_id"] = task_id
    # Scripts are tied to tasks, not chat contexts, so we route the
    # notification into general_chat. The backend's persist helper
    # writes it as a role=system row scoped to that context, which is
    # the visible chat for cross-workstream events like script
    # completions. Omitting context_key would cause the backend to
    # default to general_chat anyway, but sending it explicitly keeps
    # the invariant "every manager_action carries a context_key"
    # enforced at the emission site.
    await _publish(router, ws, "manager_action", {
        "action": "script_completed",
        "context_key": "general_chat",
        "payload": payload,
    }, context=f"manager_action for {exec_id}")


async def report_progress(
    ws: PlatformWSClient | None,
    script_name: str,
    exec_id: str,
    task_id: str | None,
    progress: dict,
    router: object | None = None,
) -> None:
    """Report script progress to the platform via the WebSocket transport."""
    await _publish(router, ws, "script_status", {
        "script_name": script_name,
        "execution_id": exec_id, "status": "running",
        "progress": progress, "task_id": task_id,
        "error_message": None,
    }, context=f"script progress for {exec_id}")

    if task_id:
        done = progress.get("done", 0)
        total = progress.get("total", 0)
        current = progress.get("current_item", "")
        content = f"Script progress: {done}/{total}"
        if current:
            content += f" — {current}"
        await _publish(router, ws, "task_activity", {
            "task_id": task_id,
            "event_type": "script_progress", "actor": "system",
            "content": content, "details": progress,
        }, context=f"script_progress activity for {exec_id}")


def cleanup_orphaned_run_files(workspace: Path) -> int:
    """Legacy cleanup: delete any leftover ``_run.py`` files.

    Current mini-project scripts run ``python -m main`` directly
    and never write ``_run.py``. This scrubber exists only for
    workspaces migrated from an older build that DID write
    materialised, secret-bearing ``_run.py`` files — leaving them
    on disk would leak injected secrets via ``find`` / a curious
    editor. Safe to call unconditionally at boot; a no-op once a
    workspace has been cleaned once. Returns the number of files
    deleted (0 in production after the first boot).
    """
    scripts_dir = workspace / ".scripts"
    if not scripts_dir.exists():
        return 0

    deleted = 0
    for script_dir in scripts_dir.iterdir():
        if not script_dir.is_dir():
            continue
        executions_dir = script_dir / "executions"
        if not executions_dir.exists():
            continue
        for exec_dir in executions_dir.iterdir():
            if not exec_dir.is_dir():
                continue
            run_file = exec_dir / "_run.py"
            if run_file.exists():
                try:
                    run_file.unlink()
                    deleted += 1
                    logger.info("Deleted orphaned _run.py: %s", run_file)
                except OSError as exc:
                    logger.error(
                        "Failed to delete orphaned _run.py %s: %s", run_file, exc
                    )
    return deleted


def _find_status_on_disk_sync(workspace: Path, execution_id: str) -> dict | None:
    """Search for a status.json matching an execution_id (sync helper).

    LINEAR in the number of script directories — every script gets
    one ``Path.exists()`` stat. For an office with 50 scripts that's
    50 stats per call. Bounded but not free.

    This shape is REQUIRED on the caller path where the agent only
    has an ``execution_id`` (no script_name): ``execute_script``
    returns the id, the agent saves it, later calls
    ``get_script_status(execution_id)`` from a fresh session that
    has no in-memory mapping. The host-side runner's ``_active``
    dict is the fast path; this scan is the cold fallback when the
    execution is no longer in memory (completed + reaped).

    Optimization opportunities (not done here — would require new
    plumbing): persist an exec_id → script_name index on disk so
    the lookup is O(1) instead of O(N). Not worth the complexity
    for the realistic N (tens of scripts per office, infrequent
    cold lookups).
    """
    scripts_dir = workspace / ".scripts"
    if not scripts_dir.exists():
        return None

    for script_dir in scripts_dir.iterdir():
        if not script_dir.is_dir():
            continue
        status_file = script_dir / "executions" / execution_id / "status.json"
        if status_file.exists():
            try:
                return json.loads(status_file.read_text())
            except (json.JSONDecodeError, OSError):
                pass
    return None


async def find_status_on_disk(workspace: Path, execution_id: str) -> dict | None:
    """Search for a status.json matching an execution_id (async-safe)."""
    return await asyncio.to_thread(_find_status_on_disk_sync, workspace, execution_id)


def _read_progress_sync(workspace: Path, script_name: str) -> dict:
    """Read .progress.json from a script directory (sync helper)."""
    progress_file = workspace / ".scripts" / script_name / ".progress.json"
    if not progress_file.exists():
        return {}
    try:
        return json.loads(progress_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


async def read_progress(workspace: Path, script_name: str) -> dict:
    """Read .progress.json from a script directory (async-safe)."""
    return await asyncio.to_thread(_read_progress_sync, workspace, script_name)


def write_status(exec_dir: Path, status: dict) -> None:
    """Write status.json to an execution directory.

    Chowns the file after writing so the in-container script
    subprocess (uid 1000) can OVERWRITE its own status on the
    next state transition. Pre-0.2.25 the host daemon wrote
    status.json root-owned and the script's update attempt
    (e.g. running → completed) hit EACCES, leaving the file
    stuck on the initial "running" status.
    """
    from src._chown import chown_to_agent

    try:
        status_path = exec_dir / "status.json"
        status_path.write_text(json.dumps(status, indent=2))
        chown_to_agent(status_path)
    except OSError as exc:
        logger.error("Failed to write status.json in %s: %s", exec_dir, exc)

