#!/usr/bin/env python3
"""Standalone MCP Tool Server for Cubicle agent containers.

Runs as a child process of the Claude CLI (standard MCP pattern).
Communicates via JSON-RPC over stdin/stdout.

Usage (spawned by Claude CLI via --mcp-config):
    python3 /opt/cubicle/mcp_tool_server.py --role manager
    python3 /opt/cubicle/mcp_tool_server.py --role worker

Environment variables:
    BACKEND_URL  — Platform backend URL (e.g. http://host.docker.internal:8000)
    OFFICE_ID    — Office UUID
    TASK_ID      — Current task UUID (worker only, optional)
    AGENT_NAME   — Agent name (worker only, optional)
    TASK_CLASS   — Current task's class (worker only, optional; ``ask``
                   unlocks the close-own-task-to-done move)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# P3-F: tool-definition lists + parameter transforms live in the
# sibling ``_mcp`` package. Both are pure functions; the imports are
# cheap and stay at module-load time so the JSON-RPC startup
# round-trip isn't slowed.
#
# P3.5-B: guard the sys.path insert so a host-side test that loads
# this module via ``importlib.util.spec_from_file_location`` doesn't
# accumulate duplicate path entries (or worse, shadow a future
# top-level ``_mcp`` module on the global path). Idempotent.
_OWN_DIR = str(Path(__file__).parent)
if _OWN_DIR not in sys.path:
    sys.path.insert(0, _OWN_DIR)
from _mcp import (  # noqa: E402
    get_data_curator_tools as _get_data_curator_tools,
    get_flow_architect_tools as _get_flow_architect_tools,
    get_manager_tools as _get_manager_tools,
    get_planner_tools as _get_planner_tools,
    get_worker_subcatalog as _get_worker_subcatalog,
    get_worker_tools as _get_worker_tools,  # noqa: F401 — re-exported for tests/test_mcp_tool_filter
    project_response as _project_response,
    transform_params as _transform_params,
)
from _mcp.read_receipts import (
    GUARDED_WRITES as _RECEIPT_GUARDED_WRITES,
    READ_RECEIPT_KEY as _READ_RECEIPT_KEY,
    ReadReceipts as _ReadReceipts,
    without_unserved_receipt_guidance as _without_unserved_receipt_guidance,
)
from _mcp.result_text import (  # noqa: E402
    SectionReadError as _SectionReadError,
    pop_section_request as _pop_section_request,
    render_result as _render_result,
    render_section as _render_section,
    tool_meta as _tool_meta,
)

logger = logging.getLogger("mcp_tool_server")

# ── Configuration ──────────────────────────────────────────────────

BACKEND_URL = os.environ.get("BACKEND_URL", "http://host.docker.internal:8000")
TOOL_PROXY_URL = os.environ.get("TOOL_PROXY_URL", "")  # Local proxy on communicator host
# Bearer token for ``TOOL_PROXY_URL``. Plumbed via the agent_worker's
# CLI env. When unset (older cbcl that didn't mint the token), the
# proxy responds 401 and the caller falls back to the direct
# /api/offices/{oid}/tool-call path (only meaningful for the
# ``/tool-call`` endpoint; ``/script-execute-host`` has no fallback).
TOOL_PROXY_TOKEN = os.environ.get("TOOL_PROXY_TOKEN", "")
OFFICE_ID = os.environ.get("OFFICE_ID", "")
TASK_ID = os.environ.get("TASK_ID", "")
# WRK-09: the human-readable id (e.g. RC-001.T14) for the current task, so the
# triage guard can match a move_task/archive target passed in EITHER form.
TASK_READABLE_ID = os.environ.get("TASK_READABLE_ID", "")
AGENT_NAME = os.environ.get("AGENT_NAME", "")
# Per-task output dir context, set by ``agent_worker._build_mcp_config``
# when the worker is assigned a task. Used to inject ``CUBICLE_OUTPUT_DIR``
# into script subprocesses spawned via the local ``execute_script`` MCP
# tool — keeps the in-container path consistent with the host-side
# ScriptRunner when an agent triggers execution.
WORKSTREAM_SHORT_CODE = os.environ.get("CUBICLE_WORKSTREAM_SHORT_CODE", "")
SCOPE_READABLE_ID = os.environ.get("CUBICLE_SCOPE_READABLE_ID", "")


# Tools only the Automation Script Developer may call. Stripped from
# every other worker's tool list at registration time so non-script-
# authoring agents physically cannot author scripts. register_script
# is idempotent (create OR update) so this single name covers both
# creation and edits. ``bind_script_variable`` shipped in 0.2.22 to
# let the ASD wire its own credentials — it's gated to the same agent
# because random workers shouldn't be moving wiring decisions on a
# script they don't own.
_SCRIPT_AUTHOR_ONLY = frozenset({
    "register_script",
    "clone_script",
    "install_script_from_template",
    "bind_script_variable",
    # W6-A5-HIGH-6: ``tools_worker.py`` exposes the cron-mutation tools
    # to EVERY worker. Without this gate any non-ASD agent could
    # schedule the ASD's scripts to run hourly with arbitrary
    # ``variable_overrides``. Restricted to the ASD per the same
    # rationale as the authoring tools above — a non-author shouldn't
    # be making scheduling decisions on a script they don't own.
    "schedule_script",
    "update_script_cron",
    "delete_script_cron",
})


def filter_script_author_tools(
    tools: list[dict], agent_name: str
) -> list[dict]:
    """Return ``tools`` minus script-authoring tools for non-author agents.

    Pure function so the subprocess wiring stays trivial and the
    filter is unit-testable without spawning the MCP server. The
    automation-script-developer keeps everything; every other agent
    (including a worker spawned with an empty AGENT_NAME, which is
    a spawn-time bug — see caller) loses the authoring tools.
    """
    if agent_name == "automation-script-developer":
        return tools
    return [t for t in tools if t.get("name") not in _SCRIPT_AUTHOR_ONLY]


# The General-Chat registration strip lives in the pure ``_mcp/general_chat.py``
# so the host-side Manager procedure module can render from the same filter
# without importing this entry script (which edits ``sys.path``).
from _mcp.general_chat import (  # noqa: E402
    BOARD_WRITE_ACTIONS as _BOARD_WRITE_ACTIONS,
    filter_general_chat_tools,
)


# Script-execution path extracted to ``_mcp_script_exec`` (the heaviest
# concern in this module — manifest parsing + subprocess spawn +
# completion monitor). ``compute_output_dir`` lives with it because
# the only runtime caller is ``_execute_script``; re-exported here so
# ``tests/test_mcp_tool_filter.py`` (which loads this module via
# importlib) keeps finding it as ``mcp_tool_server.compute_output_dir``.
from _mcp_script_exec import (  # noqa: E402
    _execute_script,
    _get_script_status,
    _operation_call,
    compute_output_dir,  # noqa: F401 — re-exported for tests/test_mcp_tool_filter
)

TASK_MODE = os.environ.get("TASK_MODE", "execute")  # "execute" | "review" | "triage" | "manager"
# Pivot-1 T5 (C-3): the current task's class (``ask`` | ``assignment`` |
# ``program`` | ``op``), threaded from the dispatch payload by
# ``_agent_worker_mcp.build_mcp_config``. ``ask`` lets an executor keep
# ``move_task`` (registration) and close its OWN task straight to ``done``
# (runtime-guard exemption) — ask tasks skip Review. Empty (older daemons /
# payloads without task_class) = today's plain executor behaviour.
TASK_CLASS = os.environ.get("TASK_CLASS", "")

# T5.1.4 (06/I-9): the per-turn session lock fires on these terminal
# ``move_task`` transitions. ``blocked`` is DELIBERATELY excluded — a move
# to ``blocked`` must be followed by the mandatory blocking-cause comment
# (task-spec "Blocking discussion contract"), so locking after it would be
# wrong. The Manager prompt (``_manager.py`` "Per-Turn Session Lock") states
# the SAME set; ``test_session_lock_pin`` fails if either side drifts.
SESSION_LOCK_MOVE_STATUSES: tuple[str, ...] = ("done", "ready")
# The worker terminal set (``task_status_update``) is separate.
SESSION_LOCK_STATUS_UPDATE_STATUSES: tuple[str, ...] = ("review", "blocked")
# X45: Manager-mode actions that END the turn on success (PRE-LOCK, released
# if the backend call fails) — besides the move_task statuses above. This is
# the single source for the Manager's full lock-trigger set (the playbook's
# "Per-Turn Session Lock" list should name every entry);
# tests/test_session_lock_pin.py pins the set and its lock/unlock behavior.
MANAGER_TURN_ENDING_ACTIONS: tuple[str, ...] = (
    "ask_user_choice",
    "propose_configuration",
)
_MANAGER_TURN_END_REASONS: dict[str, str] = {
    "ask_user_choice": (
        "You asked the user a question — the answer arrives "
        "as the user's next message in a NEW turn. STOP."
    ),
    "propose_configuration": (
        "Configuration proposal posted for human review. End your turn "
        "now; no settings have changed."
    ),
}


# X60: structured recovery fields a failed tool result may carry. They are
# the model's ONLY retry guidance (e.g. a 409 capacity refusal's
# retry_after_seconds + retry_same_operation_key, or a backend ``code``
# such as stale_execution / input_read_required), so the error formatter
# must render them instead of dropping every key but the message.
ERROR_RECOVERY_FIELDS: tuple[str, ...] = (
    "code",
    "retryable",
    "retry_after_seconds",
    "retry_same_operation_key",
)
# Bounded validation detail (e.g. a Pydantic error list) is useful to a
# model that must correct its call; cap it so it can't flood the context.
_ERROR_DETAILS_MAX_CHARS = 2000


def format_error_text(result: dict) -> str:
    """Render a failed tool result for the model (X60).

    Keeps the human message and appends the machine-readable recovery
    fields plus bounded ``details`` when present.
    """
    message = result.get("message") or result.get("error") or "Unknown error"
    text = f"Error: {message}"
    recovery = {
        field: result[field]
        for field in ERROR_RECOVERY_FIELDS
        if field in result and result[field] not in (None, "", False)
    }
    if recovery:
        text += "\nRecovery: " + json.dumps(
            recovery, sort_keys=True, default=str, ensure_ascii=False
        )
    details = result.get("details")
    if details:
        rendered = json.dumps(details, sort_keys=True, default=str, ensure_ascii=False)
        if len(rendered) > _ERROR_DETAILS_MAX_CHARS:
            rendered = rendered[:_ERROR_DETAILS_MAX_CHARS] + "…(truncated)"
        text += "\nDetails: " + rendered
    return text


def _ma_tool_budget() -> int:
    """Generous tool-call ceiling for the MA's quick triage/review turns
    (ADD-A6). Env-tunable; default 20 — high enough not to break a thorough
    triage, low enough to stop a runaway comment/read loop."""
    try:
        return max(1, int(os.environ.get("CUBICLE_MA_TRIAGE_TOOL_BUDGET", "20")))
    except (TypeError, ValueError):
        return 20


def _is_terminal_verdict(bare_name: str, new_status: str) -> bool:
    """True for a session-ending MA verdict (L2/F2): ``move_task→done/ready``
    or ``update_status→review/blocked``. These are EXEMPT from the tool budget
    (the decision must always get through). A NON-terminal verdict call
    (``move_task→blocked``, ``update_status→in_progress``) is NOT exempt, so a
    runaway MA can't bypass the budget by spraying non-terminal moves."""
    if bare_name == "move_task":
        return new_status in ("done", "ready")
    if bare_name == "update_status":
        return new_status in ("review", "blocked")
    return False
# Triage mode = MA dispatch on a still-blocked task. The MCP server
# refuses ``update_status``, and ``move_task`` / ``archive_task`` /
# ``retry_blocked_task`` on the CURRENT blocked task (matched by
# ``TASK_ID``, defined at module top). Tools acting
# on OTHER tasks — ``create_task`` for a helper, ``update_task`` to
# set ``depends_on`` — stay available so the MA can run the triage
# paths of the MA playbook's "Blocked Task Resolution" section (A
# answer / B helper task / C escalate_blocker or request_clarification)
# without being able to silently un-block the task the playbook tells
# it never to un-block. retry_blocked_task is NOT a triage path, so the
# triage sub-catalog does not serve it (``tools_worker._MA_TRIAGE_DROPS``).
# TRIAGE_PATHS_TEXT is the one runtime rendering of that letter map;
# tests/test_tool_refusal_messages.py pins it against the catalogs.
TRIAGE_PATHS_TEXT = (
    "(A) answer it: `add_activity` with event_type='answer', then "
    "`escalate_blocker` (blocker_class='ambiguous_spec', 'Answered in-thread; "
    "approve to resume') so approval resumes the task; "
    "(B) helper task: `create_task`, then `update_task` on THIS task to "
    "set depends_on=[<helper readable_id>]; "
    "(C) only the user can resolve it: `escalate_blocker` with the right "
    "blocker_class (or `request_clarification` for a brief question). "
    "`retry_blocked_task` is NOT a triage path: an approved escalation "
    "returns the task to ready when its gates allow (brief, scope, "
    "dependencies, and the bounce cap for agent-decided approvals); "
    'otherwise escalate the remaining gate — except after an "Auto-unblock '
    'refused" posted since the task last entered blocked (the bounce cap): '
    "with nothing pending, a person has decided, so comment only."
)
# Context of the current Manager chat turn. "general_chat" when the user
# is chatting without a workstream; "workstream:{uuid}" when inside a
# workstream. Empty for non-Manager (worker) sessions. Controls whether
# board-mutating tools are exposed.
CONTEXT_KEY = os.environ.get("CONTEXT_KEY", "")

# Actions / bare tool names blocked in General Chat mode live in the pure
# ``_mcp/general_chat.py`` (BOARD_WRITE_ACTIONS, imported above as
# ``_BOARD_WRITE_ACTIONS``). The guard at ``_execute_tool`` checks BOTH
# ``tool["action"]`` and the bare tool name against it; read its header
# before adding a board/planning write.


def _is_general_chat() -> bool:
    return CONTEXT_KEY == "general_chat"


def _GENERAL_CHAT_REDIRECT(attempted: str) -> str:
    return (
        f"Board-mutating tool '{attempted}' is DISABLED in General Chat. "
        "Task and Scope manipulation is only available inside a workstream. "
        "Tell the user: \"I can't create or modify tasks from General Chat. "
        "Please switch to the appropriate workstream from the sidebar and "
        "ask me there.\" Do not retry this tool."
    )

# ── HTTP client ────────────────────────────────────────────────────
# Extracted to ``_mcp_backend`` so the JSON-RPC dispatch path stays
# focused on tool routing. Re-exported here because the rest of this
# module (and ``_execute_script`` in ``_mcp_script_exec``) still call
# these names directly.
from _mcp_backend import (  # noqa: E402
    _call_backend,
    _close_session,
)


# ── MCP Protocol (JSON-RPC over stdio) ────────────────────────────

class MCPServer:
    """Minimal MCP server implementing the JSON-RPC protocol over stdio."""

    def __init__(self, tools: list[dict]):
        self._tools = {t["name"]: t for t in tools}
        # Session lock: set after a terminal action (update_status→review,
        # move_task→done/ready). ALL subsequent tool calls return an error.
        # This is the ONLY reliable way to stop Claude from continuing —
        # prompt instructions and kill signals have latency and can be ignored.
        self._session_locked = False
        self._lock_reason = ""
        # ADD-A6: tool-call budget for the Manager Assistant's quick-decision
        # modes (triage of a blocked task, or MA-review). These are meant to
        # be FAST — read state, decide, post one synthesis/verdict — not deep
        # work. The "≤2 tool calls" guidance was prompt-only; this is a
        # generous code ceiling that only catches a runaway loop (an MA
        # spraying many comments / reads) without breaking a thorough triage.
        # Designated reviewers (TASK_MODE=review, custom agent) are NOT
        # budgeted here — they legitimately read many deliverables.
        self._tool_call_count = 0
        self._ma_budget_applies = TASK_MODE == "triage" or (
            TASK_MODE == "review" and AGENT_NAME == "manager-assistant"
        )
        # C4c-G1/R12: the latest whole read of each flow graph, spec and
        # execution plan in THIS session. Their write-backs replace the target
        # wholesale, so one is refused after a shortened read, and after a
        # complete read it must echo that read's receipt: the CLI may have
        # shown the model only a preview of it (see _mcp/read_receipts.py).
        self._read_receipts = _ReadReceipts()

    async def run(self):
        """Main loop: read JSON-RPC requests from stdin, write responses to stdout.

        Claude CLI sends messages as NDJSON (one JSON object per line),
        NOT Content-Length framed. Uses thread-based stdin reading to avoid
        asyncio connect_read_pipe PermissionError in Docker containers.
        """
        loop = asyncio.get_running_loop()

        def _read_stdin_line() -> str:
            """Read one line from stdin (blocking, runs in thread)."""
            line = sys.stdin.buffer.readline()
            if not line:
                raise EOFError("stdin closed")
            return line.decode().strip()

        while True:
            try:
                line = await loop.run_in_executor(None, _read_stdin_line)

                if not line:
                    continue  # Skip empty lines

                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Non-JSON line from stdin: %s", line[:100])
                    continue

                # Dispatch
                response = await self._handle_message(msg)
                if response is not None:
                    self._write_response(response)

            except EOFError:
                break
            except Exception as exc:
                logger.exception("Error in MCP server loop: %s", exc)

        await _close_session()

    async def _handle_message(self, msg: dict) -> dict | None:
        """Handle a JSON-RPC message."""
        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params", {})

        if method == "initialize":
            return self._make_response(msg_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "cubicle-tools",
                    "version": "1.0.0",
                },
            })

        elif method == "notifications/initialized":
            return None  # No response for notifications

        elif method == "tools/list":
            tool_list = []
            for tool in self._tools.values():
                tool_list.append({
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "inputSchema": tool.get("inputSchema", {"type": "object", "properties": {}}),
                    # Declares the result size this server renders to, so the
                    # CLI inlines results up to it instead of replacing them
                    # with a file (see _mcp/result_text.py).
                    "_meta": _tool_meta(tool.get("action", "")),
                })
            return self._make_response(msg_id, {"tools": tool_list})

        elif method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            result = await self._execute_tool(tool_name, arguments)
            return self._make_response(msg_id, result)

        elif method == "ping":
            return self._make_response(msg_id, {})

        else:
            # Unknown method — return error
            if msg_id is not None:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": -32601,
                        "message": f"Unknown method: {method}",
                    },
                }
            return None

    async def _execute_tool(self, tool_name: str, arguments: dict) -> dict:
        """Execute a tool call and return MCP-formatted result."""
        # SESSION LOCK: reject ALL tool calls after terminal action.
        # Note: this is a secondary guard. The primary enforcement is in the
        # backend's action_add_activity. This lock works when tool calls
        # go through the local MCP server (not all calls do).
        if self._session_locked:
            return {
                "content": [{"type": "text", "text": (
                    f"SESSION TERMINATED: {self._lock_reason} "
                    "No further tool calls are allowed. STOP IMMEDIATELY."
                )}],
                "isError": True,
            }

        # GENERAL CHAT GUARD (Manager): board/scope-mutating tools are
        # forbidden in General Chat context. Task manipulation must
        # happen inside a workstream.
        if TASK_MODE == "manager" and _is_general_chat():
            tool = self._tools.get(tool_name)
            action = tool["action"] if tool else ""
            # Also check by tool_name suffix in case we didn't build this tool
            bare_name = tool_name.replace("mcp__cubicle-tools__", "")
            if action in _BOARD_WRITE_ACTIONS or bare_name in _BOARD_WRITE_ACTIONS:
                return {
                    "content": [{"type": "text", "text": _GENERAL_CHAT_REDIRECT(bare_name or tool_name)}],
                    "isError": True,
                }

        # TRIAGE GUARD: triage mode = MA dispatched to a still-blocked
        # task. Refuse any tool call that would un-block (or terminally
        # move) the CURRENT task. Tools targeting OTHER tasks are still
        # allowed so the MA can create a helper task, set depends_on on
        # the blocked task, or propose an action for the user.
        if TASK_MODE == "triage":
            bare_name = tool_name.replace("mcp__cubicle-tools__", "")
            tool_def = self._tools.get(tool_name)
            action_name = tool_def["action"] if tool_def else ""
            # WRK-09: match the current task whether the MA passes the UUID or
            # the readable_id (case-insensitive, trimmed) — otherwise the
            # readable form silently bypasses the blocked-task triage lock.
            current_task = str(arguments.get("task_id", "")).strip().lower()
            _current_forms = {
                v.strip().lower() for v in (TASK_ID, TASK_READABLE_ID) if v
            }
            targets_current = bool(current_task) and current_task in _current_forms

            if bare_name in ("update_status",) or action_name == "task_status_update":
                return {
                    "content": [{"type": "text", "text": (
                        "update_status is disabled while triaging a blocked "
                        "task. Post ONE synthesis comment via `add_activity`, "
                        "then pick exactly one path — "
                        + TRIAGE_PATHS_TEXT
                        + " Then STOP."
                    )}],
                    "isError": True,
                }
            if (
                bare_name in ("move_task", "archive_task", "retry_blocked_task")
                or action_name in ("move_task", "retry_blocked_task")
            ) and targets_current:
                return {
                    "content": [{"type": "text", "text": (
                        f"{bare_name} on the current blocked task is "
                        "disabled in triage mode — the cooldown lock + "
                        "bounce cap rely on this. Leave the task in "
                        "blocked and resolve it through one path: "
                        + TRIAGE_PATHS_TEXT
                    )}],
                    "isError": True,
                }

        # EXECUTOR GUARD: executors (TASK_MODE=execute) have restricted tools.
        # The Planner is spawned as a worker (TASK_MODE=execute) but
        # legitimately needs create_task / create_scope / move-equivalents to
        # materialize a planned scope — exempt it here. Its plan-write tools
        # already gate on AGENT_NAME=="planner" in the toolset. The Manager
        # Assistant is likewise a Board Operator that legitimately keeps the
        # board-write set in every mode (T5.1.1/T5.1.3); its triage-mode
        # lockout on the *current* blocked task is enforced separately above.
        if TASK_MODE == "execute" and AGENT_NAME not in ("planner", "manager-assistant"):
            # Executors cannot call move_task (only reviewers/MA can).
            # ONE exception (pivot-1 T5 / C-3): an ask-class executor closes
            # its OWN task straight to done — ask tasks skip Review, so
            # ``move_task(new_status="done")`` targeting the CURRENT task is
            # allowed when TASK_CLASS == "ask". Every other move_task use
            # (other tasks, other statuses) is still refused.
            if tool_name in ("move_task", "mcp__cubicle-tools__move_task"):
                _mt_target = str(arguments.get("task_id", "")).strip().lower()
                _own_forms = {
                    v.strip().lower()
                    for v in (TASK_ID, TASK_READABLE_ID) if v
                }
                _ask_own_done = (
                    TASK_CLASS == "ask"
                    and arguments.get("new_status") == "done"
                    and _mt_target in _own_forms
                )
                if not _ask_own_done:
                    return {
                        "content": [{"type": "text", "text": (
                            "move_task is not available. Use update_status."
                            + (
                                " (ask-class exception: move_task is allowed "
                                "ONLY to close YOUR OWN task to done)"
                                if TASK_CLASS == "ask" else ""
                            )
                        )}],
                        "isError": True,
                    }
            # Executors cannot create tasks (only Manager can)
            if tool_name in ("create_task", "mcp__cubicle-tools__create_task"):
                return {
                    "content": [{"type": "text", "text": "create_task is not available to executors. Use propose_task (or a typed proposal: propose_subtask / propose_split_into_scope) — proposals route through the Action Request inbox for the Manager to decide."}],
                    "isError": True,
                }
            # After session lock (submitted for review), block ALL MCP tools.
            # This catches add_activity calls that arrive after update_status.
            if self._session_locked:
                return {
                    "content": [{"type": "text", "text": (
                        f"SESSION TERMINATED: {self._lock_reason} "
                        "No further tool calls allowed."
                    )}],
                    "isError": True,
                }

        tool = self._tools.get(tool_name)
        if not tool:
            return {
                "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                "isError": True,
            }

        if tool.get("action") in _RECEIPT_GUARDED_WRITES:
            refused = self._read_receipts.refusal(tool["action"], arguments or {})
            if refused:
                return {
                    "content": [{"type": "text", "text": refused}],
                    "isError": True,
                }

        # ADD-A6 (+M1 + L2 + F2 fixes): enforce the MA quick-decision tool
        # budget (triage / MA review). Counted here — AFTER the general-chat /
        # triage / executor guards and the unknown-tool check — so only tool
        # calls that actually proceed to dispatch consume budget (a
        # guard-refused no-op shouldn't). Only a TERMINAL verdict is EXEMPT
        # (F2): exempting move_task / update_status by name alone let a runaway
        # MA spray non-terminal moves (move_task→blocked, update_status→
        # in_progress) forever without consuming budget. Exempt ONLY the
        # session-ending verdicts (move_task→done/ready, update_status→
        # review/blocked) — the decision must always get through; everything
        # else, including non-terminal verdict calls, consumes budget.
        _bare_budget_name = tool_name.replace("mcp__cubicle-tools__", "")
        _budget_ns = (arguments or {}).get("new_status", "")
        if self._ma_budget_applies and not _is_terminal_verdict(
            _bare_budget_name, _budget_ns
        ):
            self._tool_call_count += 1
            if self._tool_call_count > _ma_tool_budget():
                self._session_locked = True
                self._lock_reason = (
                    f"Manager Assistant exceeded its {_ma_tool_budget()}-call "
                    "budget for a quick triage/review turn."
                )
                return {
                    "content": [{"type": "text", "text": (
                        f"SESSION TERMINATED: {self._lock_reason} "
                        "Triage/review must be fast — read state, decide, post "
                        "ONE synthesis/verdict, and stop. No further tool calls "
                        "are allowed. STOP IMMEDIATELY."
                    )}],
                    "isError": True,
                }

        # Initialized OUTSIDE the try so the except handler below can
        # always read it (L-4) — even when the failure happens before
        # the PRE-LOCK section (e.g. a transform raising).
        is_terminal = False
        try:
            action = tool["action"]
            transform = tool.get("transform")
            is_local = tool.get("local", False)

            # Apply parameter transforms
            params = _transform_params(action, transform, arguments)
            # A large read's ``section``/``offset`` select part of the
            # rendered result, and a write-back's ``read_receipt`` was checked
            # above; none of them reaches the backend.
            section_request = _pop_section_request(action, params)
            if action in _RECEIPT_GUARDED_WRITES:
                params.pop(_READ_RECEIPT_KEY, None)

            # PRE-LOCK: Set session lock BEFORE the backend call for
            # terminal actions. This blocks same-turn tool calls —
            # Claude sometimes sends add_activity + update_status in
            # one turn, and the lock must be set before add_activity
            # can execute.
            if action == "task_status_update":
                ns = params.get("new_status", "")
                if ns in SESSION_LOCK_STATUS_UPDATE_STATUSES:
                    is_terminal = True
                    self._session_locked = True
                    self._lock_reason = f"Task submitted for {ns}."
                    logger.debug("PRE-LOCK SET: action=%s, new_status=%s", action, ns)
            elif action == "move_task":
                ns = params.get("new_status", "")
                if ns in SESSION_LOCK_MOVE_STATUSES:
                    is_terminal = True
                    self._session_locked = True
                    self._lock_reason = f"Task moved to {ns}."
            elif action == "request_user_action":
                is_terminal = True
                self._session_locked = True
                self._lock_reason = "Human response requested in chat and Inbox. Stop now; the platform resumes after a correlated response."
            elif action in MANAGER_TURN_ENDING_ACTIONS and TASK_MODE == "manager":
                # Pivot-2 P1 (D2): asking the user ENDS the Manager turn —
                # the answer arrives as the user's next message in a NEW
                # turn (the consult_planner async posture; a one-shot
                # ``claude --print`` session cannot wait). A configuration
                # proposal likewise waits for a human decision. Same
                # PRE-LOCK mechanism as the terminal board actions;
                # unlocked below if the backend call fails. Manager
                # sessions only — neither tool exists in any worker or
                # Planner catalog.
                is_terminal = True
                self._session_locked = True
                self._lock_reason = _MANAGER_TURN_END_REASONS[action]

            # Execute locally or via backend
            if is_local:
                if action == "script_execute":
                    result = await _execute_script(params)
                elif action == "script_get_status":
                    result = await _get_script_status(params)
                elif action.startswith("operation_"):
                    result = await _operation_call(action.removeprefix("operation_"), params)
                else:
                    result = {"error": True, "message": f"Unknown local action: {action}"}
            else:
                result = await _call_backend(action, params)
                # Lean projection for board/task READS — strip the
                # fields the agent never reasons over (description in
                # listings, UUIDs, timestamps, display metadata, verbose
                # activity blobs) BEFORE the result enters the (resumed,
                # accumulating) conversation context. No-op for every
                # other action. This is the single biggest lever against
                # long-session context bloat — see project_response.
                result = _project_response(action, result)

            # If terminal action failed, unlock (allow retry)
            if is_terminal and isinstance(result, dict) and result.get("error"):
                self._session_locked = False
                self._lock_reason = ""

            # Format response
            if isinstance(result, dict) and result.get("error"):
                return {
                    "content": [{"type": "text", "text": format_error_text(result)}],
                    "isError": True,
                }

            # For terminal actions, return a clean completion message
            if (
                is_local
                and action in {"script_execute", "operation_reconcile", "operation_cancel"}
                and isinstance(result, dict)
                and result.get("accepted_wait") is True
            ):
                self._session_locked = True
                self._lock_reason = result["message"]

            # Preserve the accepted capacity receipt rather than a task verdict.
            if is_terminal:
                if action in {"request_user_action", "propose_configuration"}:
                    result = {**result, "message": self._lock_reason}
                elif action == "ask_user_choice":
                    # Keep the minted choice_id visible (debuggability) but
                    # make the end-turn instruction the headline.
                    _choice_id = (
                        result.get("choice_id")
                        if isinstance(result, dict)
                        else None
                    )
                    result = {
                        "status": "asked",
                        "choice_id": _choice_id,
                        "message": (
                            "Question posted to the user. "
                            f"{self._lock_reason} End your turn now."
                        ),
                    }
                else:
                    # A block whose office_secret_names reached no escalation
                    # keeps the backend's warning: saving them resumes nothing.
                    names_warning = (
                        result.get("office_secret_names_warning")
                        if isinstance(result, dict)
                        else None
                    )
                    result = {
                        "status": "complete",
                        "message": f"Session complete. {self._lock_reason}",
                    }
                    if names_warning:
                        result["office_secret_names_warning"] = names_warning

            # Compact JSON within the action's result limit; a result over it
            # is shortened field by field with an explicit _truncated notice
            # (see _mcp/result_text.py). A complete whole read of a guarded
            # target ends with a fresh read receipt; a section read renders
            # one part of the result and neither issues nor clears one.
            if section_request is not None:
                rendered = _render_section(result, action, section_request)
            else:
                receipt = self._read_receipts.new_receipt(action)
                rendered = _render_result(result, action, receipt=receipt)
                if receipt is not None:
                    self._read_receipts.record(
                        action, arguments or {}, result, rendered.receipt
                    )

            return {
                "content": [{"type": "text", "text": rendered.text}],
            }

        except _SectionReadError as exc:
            refused = format_error_text({"error": str(exc)})
            return {"content": [{"type": "text", "text": refused}], "isError": True}
        except Exception as exc:
            # L-4: a terminal(-locking) call that RAISED never completed —
            # release the PRE-LOCK exactly like the error-dict path above.
            # Without this, an ask/submit that blows up (transport bug,
            # serialization failure) wedges the session: "Tool error"
            # followed by "SESSION TERMINATED" on every retry, with
            # nothing actually posted. The lock was set by THIS call
            # (a previously-locked session never reaches the try — the
            # top-of-method guard returns first), so resetting is safe.
            if is_terminal and self._session_locked:
                self._session_locked = False
                self._lock_reason = ""
            logger.exception("Tool %s failed: %s", tool_name, exc)
            return {
                "content": [{"type": "text", "text": f"Tool error: {exc}"}],
                "isError": True,
            }

    def _make_response(self, msg_id: Any, result: dict) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": result,
        }

    def _write_response(self, response: dict):
        """Write a JSON-RPC response to stdout as NDJSON (one line)."""
        line = json.dumps(response) + "\n"
        sys.stdout.buffer.write(line.encode())
        sys.stdout.buffer.flush()


# ── Session tool selection ─────────────────────────────────────────

def select_session_tools(
    role: str,
    agent_name: str,
    task_mode: str,
    task_class: str | None = None,
    context_key: str = "",
) -> list[dict]:
    """Return the exact tool catalog one MCP session registers.

    Pure (no environment reads) so ``main()`` and the behavioral evals share
    ONE selection: an eval cannot hand-pick a friendlier subset than the
    session the model really gets. Order of operations is load-bearing:

    1. Catalog by role — the Manager catalog for ``role == "manager"``; the
       Planner / Flow Architect / Data Curator consult catalogs keyed on
       ``agent_name`` (they are spawned as worker processes); otherwise the
       worker role sub-catalog for ``task_mode`` (T5.1.1/T5.1.3 —
       executors lose board writes, reviewers keep ``move_task``, the
       Manager Assistant keeps the Board-Operator set, an ask-class executor
       keeps own-task ``move_task``).
    2. Workers: only the Automation Script Developer keeps the
       script-authoring tools (``filter_script_author_tools``). An empty
       ``agent_name`` is a spawn bug and falls back to the stripped set.
    3. A ``general_chat`` Manager loses board writes
       (``filter_general_chat_tools``) — the primary General-Chat defense;
       ``_execute_tool`` keeps the secondary runtime guard.
    4. A receipt-carrying read keeps its "which <write> must pass" sentence
       only when the session serves that write.
    """
    if role == "manager":
        tools = _get_manager_tools()
    elif agent_name == "planner":
        # The Planner is spawned as a worker process but needs a
        # manager-like board toolset + the plan-write/verify tools.
        # Keyed on AGENT_NAME so no new --role threading is required.
        tools = _get_planner_tools()
    elif agent_name == "flow-architect":
        # Flow Studio (FS-P3.T3): consult-only flow-design surface —
        # graph/template authoring + the collection tools (minus
        # delete_row) + KB reads. Same AGENT_NAME selection pattern
        # as the Planner.
        tools = _get_flow_architect_tools()
    elif agent_name == "data-curator":
        # Flow Studio (FS-P3.T3): consult-only collections surface —
        # schema + row stewardship + KB reads.
        tools = _get_data_curator_tools()
    else:
        tools = _get_worker_subcatalog(task_mode, agent_name, task_class or None)

    # Workers: only the Automation Script Developer may author scripts.
    # Stripping the script-authoring tools (``register_script`` for
    # create/update, ``clone_script`` for marketplace-Phase-1
    # duplicate-and-adapt) at registration time means non-script-
    # authoring agents physically cannot author scripts — closing the
    # routing gap that produced orphan .py files when other custom agents
    # tried to "help" with automation.
    if role == "worker":
        before = len(tools)
        tools = filter_script_author_tools(tools, agent_name)
        removed = before - len(tools)
        if removed:
            logger.info(
                "Worker '%s' is not the Automation Script Developer: "
                "stripped %d script-authoring tool(s)",
                agent_name or "?", removed,
            )

    # General Chat mode: strip board-mutating tools so the Manager cannot
    # even attempt to create/modify tasks or scopes.
    if role == "manager" and context_key == "general_chat":
        filtered = filter_general_chat_tools(tools)
        logger.info(
            "General Chat mode: stripped %d write tools (kept %d read-only)",
            len(tools) - len(filtered), len(filtered),
        )
        tools = filtered
    # A read's receipt sentence names its write; keep it only where the
    # session also serves that write (the Manager has no update_spec, and
    # General Chat has no update_execution_plan).
    return _without_unserved_receipt_guidance(tools)


# ── Entry point ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Cubicle MCP Tool Server")
    parser.add_argument(
        "--role",
        choices=["manager", "worker"],
        required=True,
        help="Agent role determines available tools",
    )
    args = parser.parse_args()

    # Configure logging to stderr (stdout is for MCP protocol)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    if not OFFICE_ID:
        logger.error("OFFICE_ID environment variable is required")
        sys.exit(1)

    if args.role == "worker" and not AGENT_NAME:
        logger.critical(
            "Worker MCP server started with empty AGENT_NAME — "
            "this is a spawn-time bug. Falling back to "
            "non-script-author behaviour (register_script + "
            "clone_script will be stripped). Investigate the "
            "orchestrator/agent spawn path."
        )
    tools = select_session_tools(
        args.role, AGENT_NAME, TASK_MODE, TASK_CLASS or None, CONTEXT_KEY,
    )

    logger.info(
        "Starting MCP tool server: role=%s, tools=%d, backend=%s, office=%s, context=%s",
        args.role, len(tools), BACKEND_URL, OFFICE_ID[:8], CONTEXT_KEY or "-",
    )

    server = MCPServer(tools)
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
