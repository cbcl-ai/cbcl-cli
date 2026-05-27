"""Worker task-session runner for ``agent_worker`` (Wave 10 decomposition).

Extracted from ``agent_worker.py`` — the biggest single concern in
the daemon: the assign-task IPC handler + the streaming Claude CLI
runner that drives each worker task. ~750 lines of behaviour
preserved verbatim, with ``self`` → ``worker`` parameter renaming
and one-level dedent.

The handler is the IPC entry point the Orchestrator drops an
``assign_task`` IPC frame at. It builds the worker system prompt
(including subagent definitions, retry guidance, and rework
feedback), then calls ``run_sdk_session`` to drive the Claude CLI
subprocess that does the actual work.

The runner streams per-token output, handles retries, manages
session lock state, and emits ``task_complete`` /
``response_chunk`` / ``activity`` IPC frames back. It's the
hottest path in the daemon — every worker task goes through here.

Adapters in ``agent_worker.py`` keep the class-method call shape
stable for every existing caller.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
import uuid
from typing import TYPE_CHECKING

from src.agent_protocol import MessageType
from ._agent_worker_mcp import _CLAUDE_CLI_BUILTIN_DISALLOW

if TYPE_CHECKING:
    from src.agent_worker import AgentWorker

# Runtime imports for symbols this module RAISES / CALLS — not just
# type hints. ``AgentErrorEscalation`` stays defined in
# ``agent_worker`` (its module-public home); we re-import it here so
# the ``raise`` + ``except`` sites work after the Wave 10 extract.
# The import is at module top — the load order is OK because
# ``agent_worker.py`` only imports THIS module lazily (inside the
# adapter method bodies), so no circular import on first load.
from src.agent_worker import AgentErrorEscalation  # noqa: E402
from src.orchestrator.error_classifier import classify_error  # noqa: E402


logger = logging.getLogger(__name__)


async def handle_assign_task(worker: "AgentWorker", msg: dict) -> None:
    """Execute a worker task using the Claude Agent SDK.

    This is the main work handler. It receives the full task data
    (brief, agent config, workspace path), builds the worker prompt,
    and runs an SDK session via the container's agent runner.

    On success: sends task_complete with status="review".
    On cancellation: sends task_complete with status="blocked".
    On error: sends error message (non-fatal, agent stays alive).

    Args:
        msg: The assign_task message dict.
    """
    task_id = msg.get("task_id", "")
    readable_id = msg.get("readable_id", "")
    task_status = msg.get("status", "ready")
    worker._current_task_id = task_id
    # Fresh task → forget any cancellation source recorded for a
    # prior assignment, so an external_cancel that fires here
    # isn't misattributed to a stale shutdown / explicit_cancel
    # signal from an earlier life of this subprocess.
    worker._cancellation_source = None

    is_review = task_status == "review"
    is_triage = task_status == "blocked"
    if is_review:
        mode = "REVIEW"
    elif is_triage:
        mode = "TRIAGE"
    else:
        mode = "EXECUTE"
    logger.info("Assigned task %s (%s) — mode: %s", readable_id, task_id, mode)

    try:
        agent_config = msg.get("agent_config", {})
        worker._agent_config = agent_config

        # Run the SDK session
        session_id, total_cost = await worker._run_sdk_session(
            agent_config=agent_config,
            task_data=msg,
        )

        # Check if task was skipped (already done or reassigned)
        if session_id is None and total_cost is None:
            logger.info("Task %s skipped (state changed)", readable_id)
            worker._send({
                "type": MessageType.TASK_COMPLETE,
                "task_id": task_id,
                "status": task_status,  # Keep current status
                "comment": "Task skipped — state changed since dispatch.",
                "token_cost": 0.0,
                "session_id": "",
                "is_review_completion": True,  # Don't trigger auto-unassign
            })
            return

        # Report completion — behavior depends on task state
        if is_review:
            # Reviewer: task stays in Review. The reviewer should have
            # posted findings and unassigned the task during execution.
            # We report completion but DON'T trigger status change or
            # auto-unassign — the reviewer handles that explicitly.
            worker._send({
                "type": MessageType.TASK_COMPLETE,
                "task_id": task_id,
                "status": "review",  # Stay in review
                "comment": "Review complete.",
                "token_cost": total_cost or 0.0,
                "session_id": session_id or "",
                "is_review_completion": True,  # Flag: don't auto-unassign
            })
        elif is_triage:
            # Triage dispatch on a blocked task. The MA (or whoever
            # was dispatched here) ran its playbook, posted a
            # synthesis comment, and exited — the task STAYS in
            # blocked. We MUST flag this as a non-status-changing
            # completion so the orchestrator's task_complete handler
            # doesn't try ``move_task(blocked → review)`` (which
            # would fail board-validation and ALSO incorrectly
            # imply the agent finished work). Without this flag
            # the handler kept attempting an invalid transition
            # every triage exit, generating spurious error logs
            # and feeding the reconciler→re-dispatch loop on
            # blocked tasks. (TO-007.T40 regression, 2026-05-14.)
            worker._send({
                "type": MessageType.TASK_COMPLETE,
                "task_id": task_id,
                "status": "blocked",  # Stay in blocked
                "comment": "Triage complete.",
                "token_cost": total_cost or 0.0,
                "session_id": session_id or "",
                "is_review_completion": True,  # Re-use the "don't move" flag
            })
        else:
            # Executor: move to review for Manager
            worker._send({
                "type": MessageType.TASK_COMPLETE,
                "task_id": task_id,
                "status": "review",
                "comment": "Task execution complete.",
                "token_cost": total_cost or 0.0,
                "session_id": session_id or "",
                "is_review_completion": False,
            })

    except asyncio.CancelledError:
        # Cancellation source is whatever the cancel/shutdown/
        # signal handlers stamped earlier. None means nothing in
        # THIS process initiated the cancel — supervisor reap,
        # heartbeat timeout, daemon restart, container kill, etc.
        # "external_cancel" is the catch-all; the backend's
        # task_errors row makes the distinction queryable.
        cancellation_source = (
            worker._cancellation_source or "external_cancel"
        )
        logger.info(
            "Task %s cancelled (source=%s)",
            readable_id,
            cancellation_source,
        )
        # Telemetry event FIRST — the backend's task_activity
        # handler picks up error rows with details.error_class
        # and writes a queryable task_errors entry. The
        # task_complete frame below changes status and is
        # consumed by a different handler path that DOESN'T
        # carry structured error context, which is exactly why
        # the user sees a bare "Task was cancelled." today.
        try:
            worker._send({
                "type": MessageType.PROGRESS,
                "task_id": task_id,
                "event_type": "error",
                "content": (
                    f"Worker session for {readable_id} was "
                    f"cancelled ({cancellation_source})."
                ),
                "details": {
                    "error_class": "cancelled",
                    "cancellation_source": cancellation_source,
                    "retryable": False,
                },
            })
        except Exception:
            # ``_send`` writes NDJSON to stdout; the only way this
            # raises is if stdout is closed, in which case the
            # process is already dying. Swallow so we still emit
            # the task_complete below if we can.
            logger.exception(
                "Failed to emit cancellation telemetry for task %s",
                task_id,
            )
        worker._send({
            "type": MessageType.TASK_COMPLETE,
            "task_id": task_id,
            "status": "blocked",
            "comment": "Task was cancelled.",
            "token_cost": 0.0,
            "session_id": "",
        })
    except AgentErrorEscalation as esc:
        # Error-recovery retries exhausted OR non-retryable error.
        # Move the task to blocked with a structured comment so the
        # Manager Assistant (Board Operator) can pick it up, read
        # the classification, and decide next steps (split task,
        # refresh auth, etc.). Do NOT send ERROR — that would be
        # treated as a fatal agent crash by the supervisor.
        logger.warning(
            "Task %s escalated to MA: class=%s msg=%s",
            readable_id, esc.error_class, esc.escalation_message,
        )
        comment = (
            f"ESCALATED ({esc.error_class}): {esc.escalation_message}\n\n"
            f"Original error: {esc.original_error[:_ESCALATION_ORIGINAL_LENGTH]}\n\n"
            "Manager Assistant: please investigate. Options typically "
            "include splitting this task into smaller pieces, reducing "
            "scope, or (for config/auth errors) asking the user to "
            "resolve the underlying issue."
        )
        worker._send({
            "type": MessageType.TASK_COMPLETE,
            "task_id": task_id,
            "status": "blocked",
            "comment": comment,
            "token_cost": esc.total_cost or 0.0,
            "session_id": esc.session_id or "",
            "details": {
                "error_class": esc.error_class,
                "escalation_message": esc.escalation_message,
            },
        })
    except Exception as exc:
        logger.exception("Task %s failed: %s", readable_id, exc)
        worker._send({
            "type": MessageType.ERROR,
            "message": str(exc)[:1000],
            "task_id": task_id,
            "fatal": False,
        })
    finally:
        worker._current_task_id = None


async def _run_sdk_session(
    self,
    agent_config: dict,
    task_data: dict,
) -> tuple[str | None, float | None]:
    """Run a Claude CLI session via docker exec and stream events.

    Invokes the Claude CLI directly inside the Docker container using
    ``docker exec``. Events are streamed to the Orchestrator via
    stdout NDJSON messages.

    Returns:
        Tuple of (session_id, total_cost).
    """
    from src.docker.session_bridge import stream_cli_session
    from src.orchestrator.worker_prompt import build_worker_prompt

    task_id = task_data.get("task_id", "")

    # Always fetch fresh task details from the backend to ensure
    # we have the latest state, brief, activities, and artifacts
    if worker.backend_url and task_id:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{worker.backend_url}/api/offices/{worker.office_id}/tool-call",
                    json={"action": "get_task_detail", "params": {"task_id": task_id}},
                )
                if resp.status_code == 200:
                    detail = resp.json()
                    task_data["brief"] = detail.get("brief", task_data.get("brief", {}))
                    task_data["title"] = detail.get("title", task_data.get("title", ""))
                    task_data["reviewer"] = detail.get("reviewer") or task_data.get("reviewer", "")
                    task_data["rework_count"] = detail.get("rework_count", task_data.get("rework_count", 0))
                    task_data["recent_activities"] = detail.get("recent_activities", [])
                    task_data["artifacts"] = detail.get("artifacts", [])

                    # Check if task state has changed since dispatch
                    current_status = detail.get("status", "")
                    current_agent = detail.get("assigned_agent") or ""

                    if current_status in ("done", "archived"):
                        logger.info("Task %s already %s — skipping", task_id, current_status)
                        return None, None

                    # Check authorization: agent must be either the
                    # assigned executor OR the designated reviewer.
                    current_reviewer = detail.get("reviewer") or ""
                    is_authorized = (
                        not current_agent
                        or current_agent == worker.agent_name
                        or current_reviewer == worker.agent_name
                    )
                    if not is_authorized:
                        logger.info(
                            "Task %s not assigned to us (%s) — agent=%s reviewer=%s — skipping",
                            task_id, worker.agent_name, current_agent, current_reviewer,
                        )
                        return None, None

                    # Task is assigned to us (as executor or reviewer).
                    # The agent's prompt (review vs execute mode) tells
                    # it what to do based on the task state.

                    # Update status and agent from fresh data
                    task_data["status"] = current_status
                    task_data["assigned_agent"] = worker.agent_name
                    logger.info("Fetched fresh task details for %s (status=%s)", task_id, current_status)
        except Exception as exc:
            logger.warning("Failed to fetch task details: %s", exc)
    container_name = agent_config.get("_container_name", "")
    # F5/R2-F9 (audit): explicit fallback via the central constant.
    # Fires only when the orchestrator dispatched a malformed config;
    # log loudly so the gap surfaces.
    from src.orchestrator._model_defaults import FALLBACK_WORKER_MODEL
    model = agent_config.get("model") or FALLBACK_WORKER_MODEL
    if not agent_config.get("model"):
        logger.warning(
            "Worker agent_config missing 'model' for task %s — "
            "falling back to %s. Investigate the orchestrator "
            "dispatch path.",
            task_id, FALLBACK_WORKER_MODEL,
        )
    # NOTE: We intentionally do NOT pass --allowed-tools to the Claude CLI.
    # The agent's allowed tools are documented in their CLAUDE.md, and the
    # MCP tool server defines which cubicle-tools are available. Passing
    # --allowed-tools would block MCP connector tools (Notion, Figma, etc.)
    # that are configured in the container via `claude mcp add`.
    allowed_tools: list[str] | None = None

    # Build the system prompt from the task brief only.
    # The agent's CLAUDE.md is auto-discovered by Claude CLI from the
    # per-agent working directory (/workspace/agents/{name}/CLAUDE.md).
    # The office-level CLAUDE.md (/workspace/CLAUDE.md) is also
    # auto-discovered via directory hierarchy.
    system_prompt = build_worker_prompt(task_data)
    prompt = f"Execute the task as described in the system prompt. Task ID: {task_id}"

    # Per-agent working directory for Claude CLI
    agent_cwd = f"/workspace/agents/{worker.agent_name}"

    # Build MCP config for worker tools. ``triage`` is the new
    # mode for MA dispatch on a blocked task — the MCP server
    # uses it to refuse ``update_status`` / ``move_task`` on the
    # current blocked task, enforcing the playbook rule that the
    # MA never moves blocked → ready itworker.
    status_now = task_data.get("status")
    if status_now == "review":
        task_mode = "review"
    elif status_now == "blocked":
        task_mode = "triage"
    else:
        task_mode = "execute"
    mcp_config = worker._build_mcp_config(
        "worker",
        task_id,
        task_mode=task_mode,
        workstream_short_code=task_data.get("workstream_short_code") or None,
        scope_readable_id=task_data.get("scope_readable_id") or None,
    )

    total_cost: float | None = None
    session_id: str | None = None

    # MCP tool prefixes to skip in progress reporting (internal tools)
    _skip_prefixes = (
        "mcp__cubicle",
    )

    # Preserve session across rework cycles for context continuity.
    # On rework the backend passes the prior executor/reviewer session_id;
    # we resume it here so the agent sees its own prior work + feedback.
    prior_session_id = task_data.get("prior_session_id") or None
    if prior_session_id:
        logger.info(
            "Resuming prior session %s for task %s (rework cycle)",
            prior_session_id, task_id,
        )

    # Error-aware retry loop. If the CLI returns an error we classify
    # it and either retry with a remedy (raised token limits, adjusted
    # prompt, fresh session) or escalate by raising.
    #
    # Context preserved across retries:
    #   - session_id (most recent non-null from `result` messages)
    #   - total_cost (most recent from `result` messages)
    #   - _output_locked (once a terminal tool is seen, stays locked
    #     for the rest of the worker lifetime — not reset on retry
    #     because the task is already submitted and further output
    #     is spurious)
    _output_locked = False
    current_prompt = prompt
    current_system_prompt = system_prompt
    current_resume = prior_session_id
    current_env: dict[str, str] = {}
    attempt = 0
    max_attempts = _MAX_SESSION_ATTEMPTS
    # P2-E + P2.5-F: track wall-clock so we can fail-fast on
    # slow-burn retries even if the per-attempt CLI timeout
    # never fires. ``time.monotonic`` is the right clock here:
    # immune to wall-clock jumps and doesn't depend on the
    # event-loop instance (avoids the ``get_event_loop()``
    # deprecation surface).
    wallclock_start = time.monotonic()

    while attempt < max_attempts:
        elapsed = time.monotonic() - wallclock_start
        if elapsed > _MAX_SESSION_WALLCLOCK_SECONDS:
            logger.warning(
                "task %s exceeded wall-clock budget (%.0fs > %ds); escalating",
                task_id, elapsed, _MAX_SESSION_WALLCLOCK_SECONDS,
            )
            raise AgentErrorEscalation(
                error_class="TIMEOUT",
                original_error=(
                    f"Wall-clock budget exhausted after "
                    f"{int(elapsed)}s across {attempt} attempt(s)."
                ),
                escalation_message=(
                    "Task exceeded the 6-hour wall-clock budget across "
                    "retries. Investigate why the CLI is taking so long "
                    "(model rate limits, slow tool calls, infinite "
                    "loops in the prompt) before re-queuing."
                ),
                session_id=session_id,
                total_cost=total_cost,
            )
        attempt += 1
        # All three signals live at loop-scope so the classification
        # block below can read them unconditionally (no NameError
        # games across branches).
        #
        # - last_error_text: populated ONLY when session_bridge emits
        #   an `error` stream event (non-zero exit or timeout). This
        #   is the retry trigger — if it stays None, the CLI
        #   succeeded and we return.
        # - last_api_error: captured opportunistically from assistant
        #   text prefixed "API Error:" OR from result.is_error. Used
        #   to ENRICH classification when an error does fire, since
        #   the raw "exited with code N" string matches no pattern.
        # - last_stderr_text: stderr captured by session_bridge and
        #   piggy-backed on the error event. Second-best enrichment.
        last_error_text: str | None = None
        last_api_error: str | None = None
        last_stderr_text: str = ""

        async for msg in stream_cli_session(
            container_name=container_name,
            model=model,
            system_prompt=current_system_prompt,
            prompt=current_prompt,
            cwd=agent_cwd,
            mcp_config=mcp_config,
            allowed_tools=allowed_tools,
            # Always exclude Claude CLI's built-in TaskCreate
            # family — see ``_CLAUDE_CLI_BUILTIN_DISALLOW``. The
            # ``allowed_tools`` whitelist passed above does NOT
            # cover these (Claude CLI built-ins land in the
            # model's tool catalog regardless), so explicit
            # ``--disallowed-tools`` is what keeps them out.
            disallowed_tools=_CLAUDE_CLI_BUILTIN_DISALLOW,
            resume_session=current_resume,
            env_overrides=current_env or None,
        ):
            # P2.5-F: per-message wall-clock check. The
            # between-attempts check at the top of the outer
            # while-loop only fires AFTER an attempt fully
            # finishes. Without this inline check, a slow-burn
            # attempt could individually run past the 6-hour
            # budget (the per-attempt CLI timeout is 4 h) before
            # we even look at the clock. The async generator
            # yields many messages, so this fires roughly once
            # per CLI line — cheap.
            elapsed = time.monotonic() - wallclock_start
            if elapsed > _MAX_SESSION_WALLCLOCK_SECONDS:
                logger.warning(
                    "task %s exceeded wall-clock budget mid-attempt "
                    "(%.0fs > %ds); aborting attempt %d/%d",
                    task_id, elapsed, _MAX_SESSION_WALLCLOCK_SECONDS,
                    attempt, max_attempts,
                )
                raise AgentErrorEscalation(
                    error_class="TIMEOUT",
                    original_error=(
                        f"Wall-clock budget exhausted mid-attempt "
                        f"after {int(elapsed)}s "
                        f"(attempt {attempt}/{max_attempts})."
                    ),
                    escalation_message=(
                        "Task exceeded the 6-hour wall-clock budget. "
                        "Check why the CLI is running so long."
                    ),
                    session_id=session_id,
                    total_cost=total_cost,
                )

            if msg.type == "result":
                session_id = msg.data.get("session_id") or session_id
                total_cost = (
                    msg.data.get("cost_usd")
                    or msg.data.get("total_cost_usd")
                    or total_cost
                )
                # Claude CLI reports terminal API errors via the final
                # result message: is_error=true with the error text in
                # `result`, or subtype=="error_during_execution". Both
                # paths must feed the classifier so we don't fall back
                # to the contentless exit-code string.
                if (
                    msg.data.get("is_error")
                    or msg.data.get("subtype") == "error_during_execution"
                ):
                    result_err = (
                        msg.data.get("result")
                        or msg.data.get("error")
                        or ""
                    )
                    if isinstance(result_err, str) and result_err.strip():
                        last_api_error = result_err.strip()
            elif msg.type == "assistant":
                # Claude CLI stream-json: content blocks may contain
                # text + tool_use mixed in one message.
                blocks = msg.data.get("message", {}).get("content", [])

                # PRE-SCAN: if ANY block is a terminal tool call,
                # lock output BEFORE processing any block. This
                # prevents same-turn leaks (e.g., text + update_status
                # in one message — the text would leak without pre-scan).
                if not _output_locked:
                    _terminal_tools = (
                        "update_status", "mcp__cubicle-tools__update_status",
                        "move_task", "mcp__cubicle-tools__move_task",
                    )
                    for block in blocks:
                        if block.get("type") == "tool_use":
                            if block.get("name", "") in _terminal_tools:
                                _output_locked = True
                                logger.info(
                                    "Output locked — terminal tool detected: %s",
                                    block.get("name"),
                                )
                                break

                if _output_locked:
                    continue  # Skip entire message

                for block in blocks:
                    if block.get("type") == "text" and block.get("text"):
                        text = block["text"]
                        # Claude CLI surfaces API errors as assistant
                        # text prefixed with "API Error:". Capture the
                        # full text so classify_error receives the
                        # specific diagnostic (e.g. output-token-limit)
                        # and can pick the right remedy instead of
                        # falling through to UNKNOWN_FATAL.
                        stripped = text.lstrip()
                        if stripped.startswith("API Error"):
                            last_api_error = stripped.strip()
                        worker._send({
                            "type": MessageType.PROGRESS,
                            "task_id": task_id,
                            "event_type": "checkpoint",
                            "content": text[:500],
                        })
                    elif block.get("type") == "tool_use":
                        tool_name = block.get("name", "unknown")
                        if not any(
                            tool_name.startswith(p) for p in _skip_prefixes
                        ):
                            worker._send({
                                "type": MessageType.PROGRESS,
                                "task_id": task_id,
                                "event_type": "tool_run",
                                "content": f"Using {tool_name}",
                                "details": {"tool": tool_name},
                            })
            elif msg.type == "error":
                # Capture and break out of the stream loop so the retry
                # handler below can decide whether to retry or escalate.
                last_error_text = msg.data.get("error") or ""
                last_stderr_text = msg.data.get("stderr") or ""
                logger.warning(
                    "CLI stream error on attempt %d/%d for task %s: "
                    "err=%s; api_err=%s; stderr=%s",
                    attempt, max_attempts, task_id,
                    last_error_text[:200],
                    (last_api_error or "")[:200],
                    last_stderr_text[:200],
                )
                break

        # No error stream event means the CLI finished cleanly — the
        # `assistant` may have mentioned an API error in passing (e.g.
        # quoting documentation), but the process exited 0 so the
        # session is a success. Ignore last_api_error in that case.
        if last_error_text is None:
            return session_id, total_cost

        # Pick the richest classification signal available, in order:
        # 1. API error text surfaced via assistant/result (specific
        #    diagnostic produced by the model/API).
        # 2. Stderr from the CLI subprocess (often contains the
        #    underlying auth/connection failure).
        # 3. The synthetic "Claude CLI exited with code N" string —
        #    last resort; matches no pattern but keeps the loop safe.
        if last_api_error:
            error_for_classify = last_api_error
        elif last_stderr_text.strip():
            error_for_classify = last_stderr_text.strip()
        else:
            error_for_classify = last_error_text

        # Classify and decide what to do.
        remedy = classify_error(error_for_classify)

        # Post a structured `error` activity so the task's feed shows
        # exactly what happened + what the system decided.
        worker._send({
            "type": MessageType.PROGRESS,
            "task_id": task_id,
            "event_type": "error",
            "content": (
                f"CLI error ({remedy.error_class.value}) on attempt "
                f"{attempt}/{max_attempts}: {last_error_text[:_ERROR_PREVIEW_LENGTH]}"
            ),
            "details": {
                "error_class": remedy.error_class.value,
                "retryable": remedy.retryable,
                "attempt": attempt,
                "max_attempts": max_attempts,
            },
        })

        if not remedy.retryable or attempt >= max_attempts:
            # Non-retryable OR exhausted — escalate. Raise with a
            # structured message so the enclosing _handle_assign_task
            # can move the task to blocked.
            raise AgentErrorEscalation(
                error_class=remedy.error_class.value,
                original_error=last_error_text,
                escalation_message=remedy.escalation_message,
                session_id=session_id,
                total_cost=total_cost,
            )

        # Apply remedy and loop. Log a recovery checkpoint so the
        # activity feed tells the user what's happening.
        worker._send({
            "type": MessageType.PROGRESS,
            "task_id": task_id,
            "event_type": "checkpoint",
            "content": (
                f"Recovering from {remedy.error_class.value} "
                f"(attempt {attempt + 1}/{max_attempts}). Applying remedy: "
                f"{'fresh session; ' if remedy.reset_session else ''}"
                f"{'env=' + ','.join(remedy.env_overrides) + '; ' if remedy.env_overrides else ''}"
                f"appending guidance hint to system prompt."
            ),
            "details": {
                "error_class": remedy.error_class.value,
                "env_overrides": list(remedy.env_overrides),
                "reset_session": remedy.reset_session,
                "backoff_seconds": remedy.backoff_seconds,
            },
        })

        if remedy.backoff_seconds > 0:
            await asyncio.sleep(remedy.backoff_seconds)

        # Fold the remedy's env into the next call. Later remedies
        # override earlier ones (e.g. repeated token-limit errors
        # keep the bumped var, not stack).
        current_env.update(remedy.env_overrides)

        # Append guidance to the system prompt. We keep previous
        # guidance (if any) so the agent sees the full history of
        # what went wrong — capped to a few entries to avoid bloat.
        #
        # P2.5-E: use a sentinel-style delimiter unlikely to
        # appear in the user's task brief (the previous Markdown
        # heading "## AUTOMATIC RECOVERY — READ THIS" could
        # collide with a brief that copy-pastes that exact
        # phrase, and rotation would corrupt the base prompt).
        # The HTML-comment form is invisible to most renderings
        # but still readable in the model's literal prompt.
        _MARKER = "\n\n<!--CBCL_RECOVERY_BLOCK_START-->\n"
        guidance_block = (
            f"{_MARKER}"
            f"## AUTOMATIC RECOVERY — READ THIS\n"
            f"Attempt {attempt} failed: {remedy.error_class.value}. "
            f"{remedy.guidance}"
        )

        if (
            len(current_system_prompt) + len(guidance_block)
            <= _MAX_SYSTEM_PROMPT_SIZE
        ):
            current_system_prompt = current_system_prompt + guidance_block
        else:
            # P2-F + P2.5-E: rotate oldest blocks out until the
            # new block fits. Previous behaviour rotated AT MOST
            # ONE block, so a prompt that hit the cap with N>=2
            # blocks would simply drop the new guidance — and on
            # the next attempt drop it again, leaving the agent
            # without the latest remedy on every single retry.
            # We now drop blocks oldest-first until the new
            # guidance fits or no blocks remain. If even an
            # empty-block prompt + guidance overflows, we drop
            # the new guidance as a last resort.
            offsets: list[int] = []
            idx = current_system_prompt.find(_MARKER)
            while idx >= 0:
                offsets.append(idx)
                idx = current_system_prompt.find(_MARKER, idx + 1)

            rotated = current_system_prompt
            rotated_count = 0
            for i in range(len(offsets)):
                next_block_idx = (
                    offsets[i + 1] if i + 1 < len(offsets) else None
                )
                if next_block_idx is None:
                    rotated = rotated[: offsets[i]]
                else:
                    # Drop everything from this block's start to
                    # the next block's start.
                    rotated = (
                        rotated[: offsets[i]]
                        + rotated[next_block_idx:]
                    )
                    # Recompute offsets after drop — easiest is
                    # to break and re-scan, but the loop math
                    # above shifts subsequent offsets. Simpler:
                    # break and rebuild offsets each pass.
                rotated_count += 1
                if (
                    len(rotated) + len(guidance_block)
                    <= _MAX_SYSTEM_PROMPT_SIZE
                ):
                    break
                # Re-scan offsets relative to rotated for next pass.
                offsets = []
                idx = rotated.find(_MARKER)
                while idx >= 0:
                    offsets.append(idx)
                    idx = rotated.find(_MARKER, idx + 1)
                if not offsets:
                    break

            if (
                len(rotated) + len(guidance_block)
                <= _MAX_SYSTEM_PROMPT_SIZE
            ):
                logger.warning(
                    "Prompt size cap hit; rotated %d guidance "
                    "block(s) to keep attempt %d's remedy",
                    rotated_count, attempt,
                )
                current_system_prompt = rotated + guidance_block
            else:
                logger.warning(
                    "Prompt size cap hit and rotation could not free "
                    "enough room (%d chars after dropping %d blocks); "
                    "dropping new guidance for attempt %d",
                    len(rotated), rotated_count, attempt,
                )

        if remedy.reset_session:
            current_resume = None  # start a fresh session
        else:
            # Resume the session the CLI just used (if any) so the
            # next attempt sees the partial work — tool calls made,
            # files written, observations recorded — instead of
            # starting a blank conversation. Without this, the
            # retry starts a new session and the agent has no
            # visibility into what it already did on disk, forcing
            # it to redo discovery work and risking duplicate
            # writes / divergent output.
            if session_id:
                current_resume = session_id

    # Unreachable by construction: the loop body returns on success
    # and raises AgentErrorEscalation on the final failure. This
    # raise only fires if someone refactors the loop incorrectly —
    # it fails loud rather than silently returning None, None.
    raise RuntimeError(
        "agent_worker retry loop exited without return or raise — "
        "this indicates a logic bug. Please file it."
    )
