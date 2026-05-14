"""Parameter transforms for MCP tools (split from mcp_tool_server.py).

Translates backend-friendly parameter shapes from Claude CLI's
arg conventions. Pure function with the same signature as the
original ``_transform_params``.

Reads ``TASK_ID`` / ``AGENT_NAME`` from ``os.environ`` (the same
values mcp_tool_server.py sees) so the propose_* transforms can
auto-inject them. Reading at call time rather than import time
means a test that monkeypatches the env vars after import still
sees the override.
"""
from __future__ import annotations

import os


def transform_params(action: str, transform: str | None, params: dict) -> dict:
    """Apply transforms to tool parameters before sending to backend."""
    # P3-F: these used to be module-level globals in mcp_tool_server.py.
    # Reading via ``os.environ.get`` at call time keeps the helper
    # decoupled from the entrypoint and survives env mutation in tests.
    TASK_ID = os.environ.get("TASK_ID", "")  # noqa: N806 — matches original
    AGENT_NAME = os.environ.get("AGENT_NAME", "")  # noqa: N806

    # Auto-inject context for file operations
    if action == "office_save_file":
        # Auto-attach files to the current task
        if TASK_ID and "source_task_id" not in params:
            params["source_task_id"] = TASK_ID
        if AGENT_NAME and "source_agent" not in params:
            params["source_agent"] = AGENT_NAME
        return params

    if transform == "move_task":
        return {
            "task_id": params.get("task_id", ""),
            "new_status": params.get("new_status", ""),
            "actor": AGENT_NAME or "manager",
            "comment": params.get("comment", ""),
        }
    elif transform == "archive_task":
        return {
            "task_id": params.get("task_id", ""),
            "new_status": "archived",
            "actor": "manager",
            "comment": params.get("comment", "Archived by Manager"),
        }
    elif transform == "add_activity":
        return {
            "task_id": params.get("task_id", ""),
            "event_type": params.get("event_type", "comment"),
            "actor": AGENT_NAME or "manager",
            "content": params.get("content", ""),
        }
    elif transform == "propose_task":
        return {
            "task_id": params.get("task_id", ""),
            "event_type": "task_proposed",
            "actor": AGENT_NAME or "worker",
            "content": params.get("content", ""),
        }
    # ── Action Request transforms ────────────────────────────────────
    # Each rewrites a typed worker-friendly tool call into the generic
    # ``propose_action`` shape the backend dispatcher expects:
    #   {request_type, payload, justification, source_task_id?,
    #    requesting_agent}
    # ``source_task_id`` is auto-injected from TASK_ID env so workers
    # don't have to thread it through every call.
    elif transform == "propose_subtask":
        return {
            "request_type": "create_subtask",
            "payload": {
                "title": params.get("title", ""),
                "brief_hints": params.get("brief_hints") or {},
                # Phase A backend doesn't read parent_task_id from
                # payload — it derives the parent from source_task_id.
                # Pass it anyway so logs show what the worker meant.
                "parent_task_id": TASK_ID,
            },
            "justification": params.get("justification", ""),
            "source_task_id": TASK_ID,
            "requesting_agent": AGENT_NAME or "worker",
        }
    elif transform == "propose_split_into_scope":
        return {
            "request_type": "split_into_scope",
            "payload": {
                "scope_short_key": params.get("scope_short_key", ""),
                "scope_name": params.get("scope_name", ""),
                "tasks": params.get("tasks") or [],
            },
            "justification": params.get("justification", ""),
            "source_task_id": TASK_ID,
            "requesting_agent": AGENT_NAME or "worker",
        }
    elif transform == "propose_update_task":
        return {
            "request_type": "update_task",
            "payload": {
                "task_id": params.get("task_id", ""),
                "changes": params.get("changes") or {},
            },
            "justification": params.get("justification", ""),
            "source_task_id": TASK_ID,
            "requesting_agent": AGENT_NAME or "worker",
        }
    elif transform == "escalate_blocker":
        return {
            "request_type": "escalate_blocker",
            "payload": {
                "blocker_summary": params.get("blocker_summary", ""),
                "suggested_unblock": params.get("suggested_unblock") or "",
            },
            "justification": params.get("justification", ""),
            "source_task_id": TASK_ID,
            "requesting_agent": AGENT_NAME or "worker",
        }
    elif transform == "request_clarification":
        return {
            "request_type": "request_clarification",
            "payload": {"question": params.get("question", "")},
            "justification": params.get("justification", ""),
            "source_task_id": TASK_ID,
            "requesting_agent": AGENT_NAME or "worker",
        }
    elif transform == "request_review_check":
        payload: dict = {}
        if "criterion_index" in params:
            payload["criterion_index"] = params["criterion_index"]
        if TASK_ID:
            payload["task_id"] = TASK_ID
        return {
            "request_type": "request_review_check",
            "payload": payload,
            "justification": params.get("justification", ""),
            "source_task_id": TASK_ID,
            "requesting_agent": AGENT_NAME or "worker",
        }
    elif transform == "propose_artifact_handoff":
        return {
            "request_type": "propose_artifact_handoff",
            "payload": {
                "source_task_id": TASK_ID,
                "target_task_id": params.get("target_task_id", ""),
                "file_path": params.get("file_path", ""),
            },
            "justification": params.get("justification", ""),
            "source_task_id": TASK_ID,
            "requesting_agent": AGENT_NAME or "worker",
        }
    return params


# ── MCP Protocol (JSON-RPC over stdio) ────────────────────────────

