"""Startup recovery helpers.

Handles cleanup of stale state from previous communicator sessions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

logger = logging.getLogger("cbcl.recovery")

# T4.3.3: pattern matching orphan agent CLI sessions left running inside a
# REUSED office container by a crashed/SIGKILLed previous daemon. The pattern
# is deliberately specific to the Claude CLI invocation (`claude --print`) so
# it can NEVER match script subprocesses (those run as `python … main.py` via
# the ScriptRunner, reconciled separately). Without this reap, a daemon
# crash-restart re-queues + re-spawns the same tasks while the orphan CLIs keep
# executing → concurrent double execution in the same workspace (07/G12, S6).
_AGENT_CLI_REAP_PATTERN = "claude --print"


async def reap_orphan_agent_sessions(container_name: str) -> int:
    """`docker exec <container> pkill -f 'claude --print'` — kill orphan agent
    CLI sessions before the dispatcher's ``full_sync`` re-spawns their tasks.

    Returns the pkill exit code (0 = processes matched + signalled, 1 = none
    matched, which is the healthy steady-state). Best-effort: a docker/exec
    failure is logged and swallowed (never blocks office bring-up). Graceful
    ``cbcl stop`` already stops containers first, so this only matters on
    crash/SIGKILL restarts where the container is reused.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", container_name,
            "pkill", "-f", _AGENT_CLI_REAP_PATTERN,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        rc = proc.returncode if proc.returncode is not None else -1
    except Exception as exc:  # docker missing, container gone, etc.
        logger.warning(
            "Orphan agent-session reap failed for %s: %s (continuing)",
            container_name, exc,
        )
        return -1
    if rc == 0:
        logger.warning(
            "Reaped orphan agent CLI session(s) in %s (matched %r) before "
            "full_sync — a prior daemon left them running.",
            container_name, _AGENT_CLI_REAP_PATTERN,
        )
    elif rc == 1:
        # pkill rc=1 == no processes matched — the healthy steady state.
        logger.info(
            "No orphan agent CLI sessions to reap in %s (pkill rc=1).",
            container_name,
        )
    else:
        # rc >= 2 (incl. docker exec 125/126/127 = container down / docker
        # error / command not found) means the reap did NOT actually run —
        # NOT a clean "no match". Surface it at WARNING so a missed reap is
        # debuggable instead of masquerading as healthy.
        logger.warning(
            "Orphan agent-session reap could not run in %s (docker exec / "
            "pkill rc=%d — container likely not running or a docker error); "
            "continuing. If a prior daemon left CLI sessions, full_sync may "
            "double-execute until they exit.",
            container_name, rc,
        )
    return rc


def mark_stale_script_executions(workspace_path: str) -> int:
    """Blindly mark any 'running' script executions as failed (legacy).

    DEPRECATED (ADD-C1): this rewrote status running→failed without
    checking whether the in-container process was still alive. Because
    the office container is reused across daemon restarts, a job that
    was still running (or had already succeeded) got reported failed,
    which made the Manager rework runs that actually worked. The office
    init path now calls the container-aware
    :func:`src.scripts.script_execution.reconcile_orphaned_executions`
    instead, which checks the real PID and kills orphans cleanly.

    Kept only as a synchronous, container-blind fallback for callers
    that have no container handle (host-only test contexts). Returns the
    number of stale executions found and marked.
    """
    scripts_dir = Path(workspace_path) / ".scripts"
    if not scripts_dir.exists():
        return 0

    count = 0
    for status_file in scripts_dir.glob("*/executions/*/status.json"):
        try:
            data = json.loads(status_file.read_text())
            if data.get("status") == "running":
                data["status"] = "failed"
                data["error_message"] = "Communicator restarted while script was running"
                status_file.write_text(json.dumps(data, indent=2))
                count += 1
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Could not read/update stale script status file %s: %s",
                status_file, exc,
            )
    return count
