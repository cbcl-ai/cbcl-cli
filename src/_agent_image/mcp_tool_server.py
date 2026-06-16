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
    get_manager_tools as _get_manager_tools,
    get_planner_tools as _get_planner_tools,
    get_worker_subcatalog as _get_worker_subcatalog,
    get_worker_tools as _get_worker_tools,  # noqa: F401 — re-exported for tests/test_mcp_tool_filter
    project_response as _project_response,
    transform_params as _transform_params,
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


# Script-execution path extracted to ``_mcp_script_exec`` (the heaviest
# concern in this module — manifest parsing + subprocess spawn +
# completion monitor). ``compute_output_dir`` lives with it because
# the only runtime caller is ``_execute_script``; re-exported here so
# ``tests/test_mcp_tool_filter.py`` (which loads this module via
# importlib) keeps finding it as ``mcp_tool_server.compute_output_dir``.
from _mcp_script_exec import (  # noqa: E402
    _execute_script,
    _get_script_status,
    compute_output_dir,  # noqa: F401 — re-exported for tests/test_mcp_tool_filter
)

TASK_MODE = os.environ.get("TASK_MODE", "execute")  # "execute" | "review" | "triage" | "manager"

# T5.1.4 (06/I-9): the per-turn session lock fires on these terminal
# ``move_task`` transitions. ``blocked`` is DELIBERATELY excluded — a move
# to ``blocked`` must be followed by the mandatory blocking-cause comment
# (task-spec "Blocking discussion contract"), so locking after it would be
# wrong. The Manager prompt (``_manager.py`` "Per-Turn Session Lock") states
# the SAME set; ``test_session_lock_pin`` fails if either side drifts.
SESSION_LOCK_MOVE_STATUSES: tuple[str, ...] = ("done", "ready")
# The worker terminal set (``task_status_update``) is separate.
SESSION_LOCK_STATUS_UPDATE_STATUSES: tuple[str, ...] = ("review", "blocked")


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
# refuses ``update_status`` / ``move_task`` on the CURRENT blocked
# task (matched by ``TASK_ID``, defined at module top). Tools acting
# on OTHER tasks — ``create_task`` for a helper, ``update_task`` to
# set ``depends_on`` — stay available so the MA can run its three
# legitimate resolution paths (B answer-and-stop / C helper-task /
# D propose_action) without being able to silently un-block the task
# the playbook tells it never to un-block.
# Context of the current Manager chat turn. "general_chat" when the user
# is chatting without a workstream; "workstream:{uuid}" when inside a
# workstream. Empty for non-Manager (worker) sessions. Controls whether
# board-mutating tools are exposed.
CONTEXT_KEY = os.environ.get("CONTEXT_KEY", "")

# Actions / bare tool names that mutate the board, scopes, or
# workspace. Blocked in General Chat mode. The guard at
# ``_execute_tool`` checks BOTH ``tool["action"]`` and the bare tool
# name against this set, so the set legitimately mixes "actions" and
# "names" that are not 1-to-1 (e.g. the ``archive_task`` tool dispatches
# to action ``move_task`` with a transform).
#
# Several entries are belt-and-suspenders for actions that ONLY exist
# on the worker side today. They are kept here so a future change that
# accidentally exposes one to the Manager (e.g. by promoting a worker
# tool into the manager_tools list) still gets blocked in General Chat
# rather than silently letting the Manager mutate while in
# "general_chat" context. If you add a worker-only mutation, ALSO add
# its action / bare name here — that's cheaper than a runtime audit.
_BOARD_WRITE_ACTIONS = {
    # Manager tool actions (from ``_get_manager_tools``).
    "create_task",
    "update_task",
    "move_task",
    "add_activity",
    "delete_task",
    "create_scope",
    "update_scope",
    "activate_scope",
    "archive_scope",
    # TS-M1: engaging the Planner is a workstream-planning write — strip it in
    # General Chat (which has no workstream context) so the Manager can't
    # consult the Planner against an arbitrary workstream from general chat.
    "consult_planner",
    # Closing a scope's verification is a scope state change — strip it in
    # General Chat (no scope context there), same as the other scope writes.
    # Plan READS (get_workstream_plan / get_execution_plan) stay available;
    # they're harmless and the Manager has no scope to read in General Chat
    # anyway.
    "complete_scope_verification",
    "office_save_file",
    # Bare tool names — Manager tools whose ``action`` aliases a less
    # specific verb (the bare-name check still trips the guard).
    "archive_task",  # tool name; action is move_task + transform
    # Escape hatch for the blocked-bounce-cap deadlock. Manager / MA
    # only; the General-Chat guard still blocks it (Manager in chat
    # has no business unblocking a stuck task without context).
    "retry_blocked_task",
    # Action-request decisions are workstream state changes — blocked
    # in General Chat so the Manager doesn't accidentally approve a
    # request without the workstream-context that informs the call.
    "decide_action_request",
    # Worker-only actions/names (defense-in-depth — see header above).
    "office_attach_to_task",
    "register_script",
    "clone_script",
    "install_script_from_template",
    "task_status_update",
    # F21 (audit): ``kb_save`` removed — no such tool is registered on
    # either Manager or Worker. Was a dead defense-in-depth entry.
}


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
            current_task = arguments.get("task_id", "")
            targets_current = bool(TASK_ID) and current_task == TASK_ID

            if bare_name in ("update_status",) or action_name == "task_status_update":
                return {
                    "content": [{"type": "text", "text": (
                        "update_status is disabled while triaging a blocked "
                        "task. Post a synthesis comment via add_activity, "
                        "then either (B) answer-and-stop, (C) create a "
                        "helper task and stamp depends_on, or (D) call "
                        "propose_action for the user — and STOP."
                    )}],
                    "isError": True,
                }
            if (bare_name in ("move_task", "archive_task")
                    or action_name == "move_task") and targets_current:
                return {
                    "content": [{"type": "text", "text": (
                        f"{bare_name} on the current blocked task is "
                        "disabled in triage mode — the cooldown lock + "
                        "bounce cap rely on this. Use propose_action or "
                        "leave the task in blocked for the user to resolve."
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
            # Executors cannot call move_task (only reviewers/MA can)
            if tool_name in ("move_task", "mcp__cubicle-tools__move_task"):
                return {
                    "content": [{"type": "text", "text": "move_task is not available. Use update_status."}],
                    "isError": True,
                }
            # Executors cannot create tasks (only Manager can)
            if tool_name in ("create_task", "mcp__cubicle-tools__create_task"):
                return {
                    "content": [{"type": "text", "text": "create_task is not available. Use add_activity with event_type 'task_proposed' to suggest a new task to the Manager."}],
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

        try:
            action = tool["action"]
            transform = tool.get("transform")
            is_local = tool.get("local", False)

            # Apply parameter transforms
            params = _transform_params(action, transform, arguments)

            # PRE-LOCK: Set session lock BEFORE the backend call for
            # terminal actions. This blocks same-turn tool calls —
            # Claude sometimes sends add_activity + update_status in
            # one turn, and the lock must be set before add_activity
            # can execute.
            is_terminal = False
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

            # Execute locally or via backend
            if is_local:
                if action == "script_execute":
                    result = await _execute_script(params)
                elif action == "script_get_status":
                    result = await _get_script_status(params)
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
                    "content": [{"type": "text", "text": f"Error: {result.get('message') or result.get('error') or 'Unknown error'}"}],
                    "isError": True,
                }

            # For terminal actions, return a clean completion message
            if is_terminal:
                result = {
                    "status": "complete",
                    "message": f"Session complete. {self._lock_reason}",
                }

            # Truncate large responses to prevent buffer overflow
            text = json.dumps(result, indent=2, default=str)
            if len(text) > 50_000:
                text = text[:50_000] + "\n\n... (truncated, response too large)"

            return {
                "content": [{"type": "text", "text": text}],
            }

        except Exception as exc:
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

    if args.role == "manager":
        tools = _get_manager_tools()
    elif AGENT_NAME == "planner":
        # The Planner is spawned as a worker process but needs a
        # manager-like board toolset + the plan-write/verify tools.
        # Keyed on AGENT_NAME so no new --role threading is required.
        tools = _get_planner_tools()
    else:
        # T5.1.1/T5.1.3: registration-time role filtering. Executors lose the
        # board-write tools (create/move/update_task); reviewers keep
        # move_task; the Manager Assistant keeps the full set + the
        # Board-Operator reads/recovery. Replaces the old
        # description-as-refusal + runtime-guard-only posture.
        tools = _get_worker_subcatalog(TASK_MODE, AGENT_NAME)

    # Workers: only the Automation Script Developer may author scripts.
    # Stripping the script-authoring tools (``register_script`` for
    # create/update, ``clone_script`` for marketplace-Phase-1
    # duplicate-and-adapt) at registration time means non-script-
    # authoring agents (research, copywriting, code review, etc.)
    # physically cannot author scripts — closing the routing gap that
    # produced orphan .py files when other custom agents tried to
    # "help" with automation. ``register_script`` is idempotent
    # (create OR update); ``clone_script`` is the duplicate path.
    if args.role == "worker":
        if not AGENT_NAME:
            logger.critical(
                "Worker MCP server started with empty AGENT_NAME — "
                "this is a spawn-time bug. Falling back to "
                "non-script-author behaviour (register_script + "
                "clone_script will be stripped). Investigate the "
                "orchestrator/agent spawn path."
            )
        before = len(tools)
        tools = filter_script_author_tools(tools, AGENT_NAME)
        removed = before - len(tools)
        if removed:
            logger.info(
                "Worker '%s' is not the Automation Script Developer: "
                "stripped %d script-authoring tool(s)",
                AGENT_NAME or "?", removed,
            )

    # General Chat mode: strip board-mutating tools so the Manager cannot
    # even attempt to create/modify tasks or scopes. This is the primary
    # defense; the _execute_tool guard is the secondary defense.
    if args.role == "manager" and _is_general_chat():
        filtered = [
            t for t in tools
            if t.get("action") not in _BOARD_WRITE_ACTIONS
        ]
        removed = len(tools) - len(filtered)
        tools = filtered
        logger.info(
            "General Chat mode: stripped %d write tools (kept %d read-only)",
            removed, len(tools),
        )

    logger.info(
        "Starting MCP tool server: role=%s, tools=%d, backend=%s, office=%s, context=%s",
        args.role, len(tools), BACKEND_URL, OFFICE_ID[:8], CONTEXT_KEY or "-",
    )

    server = MCPServer(tools)
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
