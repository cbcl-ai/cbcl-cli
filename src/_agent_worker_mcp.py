"""MCP config builder for ``agent_worker`` Claude CLI invocations.

Extracted from ``agent_worker.py`` (Wave 10 decomposition). Pure
config-dict builder — no async, no streaming, no IPC state. The
adapter ``AgentWorker._build_mcp_config`` in ``agent_worker.py``
is a one-liner that calls :func:`build_mcp_config` here.

The CLI-builtin disallow list (``_CLAUDE_CLI_BUILTIN_DISALLOW``)
also lives here since it's a static catalog scrubbed from every
Claude session — same lifecycle as the MCP config itself.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agent_worker import AgentWorker


# Names of Claude CLI built-in tools we ALWAYS scrub from the
# model's catalog. These either overlap with Cubicle primitives
# (TaskCreate/CronCreate/Skill) or hand the agent capabilities it
# shouldn't have (Config, MCP, RemoteTrigger). See agent_worker.py
# module docstring for the original rationale paragraph and the
# group-A/B/C split.
_CLAUDE_CLI_BUILTIN_DISALLOW: list[str] = [
    # Group A — task / todo / team management. These overlap directly
    # with Cubicle's Kanban + Task Brief lifecycle. Source of the
    # original "TaskCreate harness" noise (TO-007.T184).
    "TaskCreate",
    "TaskList",
    "TaskUpdate",
    "TaskGet",
    "TaskOutput",
    "TaskStop",
    "TodoWrite",
    "TeamCreate",
    "TeamDelete",
    # Group B — domain overlap with Cubicle primitives. Cubicle has
    # its own equivalents (``schedule_script`` for cron, the office
    # Skills surface for skills, the workstream chat for "ask user").
    "CronCreate",
    "CronDelete",
    "CronList",
    "Skill",
    "AskUserQuestion",
    "SendMessage",
    "Brief",
    "SendUserMessage",  # rename of Brief in newer CLI builds — same tool.
    # Group C — agent autonomy risks. These let the model touch
    # config / auth / external triggers / interactive plan-mode
    # affordances Cubicle doesn't use. Disallow defensively even if
    # the model has never tried to call them — a future model
    # behaviour change shouldn't get free reign over CLI internals.
    "Config",
    "MCP",
    "McpAuth",
    "RemoteTrigger",
    "Sleep",
    # Monitor is the CLI's cross-turn background-task watcher ("watch
    # this and re-invoke me when it fires") — a contract that is VOID
    # under one-shot ``claude --print``: the process exits at turn end,
    # so there is no later turn to re-invoke (verify turn-end incident
    # 2026-07-17). Worse, it is a DEFERRED tool (schema loaded via
    # ToolSearch first); a model reaching for it raw gets an
    # InputValidationError and is funneled into ending its turn to
    # "wait" — killing any still-running workflow. Scrub it so the
    # broken affordance is never advertised. Bash
    # ``run_in_background`` is deliberately NOT denied: in-turn
    # background work + timeout-bounded polling is legitimate.
    "Monitor",
    "REPL",
    "PowerShell",
    "EnterPlanMode",
    "ExitPlanMode",
]


def build_mcp_config(
    worker: "AgentWorker",
    role: str,
    task_id: str | None = None,
    task_mode: str | None = None,
    context_key: str | None = None,
    workstream_short_code: str | None = None,
    scope_readable_id: str | None = None,
    task_readable_id: str | None = None,
) -> dict:
    """Build the MCP server configuration for the Claude CLI.

    Returns a dict suitable for the ``--mcp-config`` flag. Contains
    only our custom cubicle-tools MCP server.

    Other MCP servers (Notion, Slack, etc.) are managed natively by
    Claude via ``claude mcp add`` and stored in ``~/.claude.json``
    inside the container. They are available automatically to all
    sessions — no need to pass them via ``--mcp-config``.

    Per-agent tool filtering is handled by the ``--allowed-tools``
    flag, which restricts which MCP tools each agent can call.

    For Manager sessions, ``context_key`` controls whether board-
    mutating tools are available. In General Chat mode, only read /
    discovery tools are offered; write tools are suppressed so the
    Manager cannot create/modify tasks or scopes without first
    switching to a workstream.
    """
    env: dict[str, str] = {
        "BACKEND_URL": (
            worker.backend_url or "http://host.docker.internal:8000"
        ),
        "OFFICE_ID": worker.office_id,
        "TASK_MODE": task_mode or "execute",
    }
    if context_key:
        env["CONTEXT_KEY"] = context_key
    # Route tool calls through local proxy when WS transport is active.
    # The bearer token must travel with the URL — the in-container
    # MCP needs it to authenticate against /tool-call AND
    # /script-execute-host (the latter spawns docker exec with
    # caller-controlled env, so confining it to authenticated
    # callers prevents office-secret exfil from other local procs).
    tool_proxy_url = os.environ.get("CUBICLE_TOOL_PROXY_URL", "")
    if tool_proxy_url:
        env["TOOL_PROXY_URL"] = tool_proxy_url
        tool_proxy_token = os.environ.get(
            "CUBICLE_TOOL_PROXY_TOKEN", "",
        )
        if tool_proxy_token:
            env["TOOL_PROXY_TOKEN"] = tool_proxy_token
    # SEC3-01: per-office /tool-call capability secret. The MCP server sends
    # it as the ``X-Office-Secret`` header so the backend can authenticate
    # the DIRECT (non-proxy) tool-call fallback. The supervisor injects
    # CUBICLE_OFFICE_TOOL_SECRET per-office into the subprocess env.
    office_tool_secret = os.environ.get("CUBICLE_OFFICE_TOOL_SECRET", "")
    if office_tool_secret:
        env["OFFICE_TOOL_SECRET"] = office_tool_secret
    if task_id:
        env["TASK_ID"] = task_id
    # WRK-09: the readable_id lets the triage guard match a move_task/archive
    # target whether the MA passes the UUID or the human RC-001.T14 form —
    # closing the readable_id bypass of the blocked-task triage lock.
    if task_readable_id:
        env["TASK_READABLE_ID"] = task_readable_id
    # Per-task output dir context. Only the SHORT_CODE is needed
    # for the path; SCOPE_READABLE_ID is optional and present
    # only when the task lives in a scope. The in-container
    # MCP server reads these to inject CUBICLE_OUTPUT_DIR into
    # script subprocesses (mirrors the host-side ScriptRunner).
    if workstream_short_code:
        env["CUBICLE_WORKSTREAM_SHORT_CODE"] = workstream_short_code
    if scope_readable_id:
        env["CUBICLE_SCOPE_READABLE_ID"] = scope_readable_id
    if worker.agent_name:
        env["AGENT_NAME"] = worker.agent_name

    return {
        "mcpServers": {
            "cubicle-tools": {
                "type": "stdio",
                "command": "python3",
                "args": ["/opt/cubicle/mcp_tool_server.py", "--role", role],
                "env": env,
            },
        },
    }
