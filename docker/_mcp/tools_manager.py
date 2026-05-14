"""Manager-role MCP tool definitions (split from mcp_tool_server.py).

Pure function: returns the JSON-RPC tool list a Manager session sees.
No side effects, no state — safe to import lazily or repeatedly.
"""
from __future__ import annotations


def get_manager_tools() -> list[dict]:
    """Tool definitions for Manager sessions."""
    return [
        {
            "name": "get_board",
            "description": "Get tasks from the board. All parameters are optional filters.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workstream_id": {"type": "string", "description": "Filter by workstream UUID"},
                    "status": {"type": "string", "description": "Filter by status: backlog, ready, in_progress, blocked, review, done (comma-separated for multiple)"},
                    "assigned_agent": {"type": "string", "description": "Filter by agent name"},
                    "priority": {"type": "string", "description": "Filter by priority: urgent, high, medium, low"},
                },
            },
            "action": "get_board",
        },
        {
            "name": "get_task_detail",
            "description": "Get full task details including brief, activity log, and artifacts.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task UUID or readable_id (e.g. 'WR-003.T01')"},
                },
                "required": ["task_id"],
            },
            "action": "get_task_detail",
        },
        {
            "name": "create_task",
            "description": "Create a task with a complete Task Brief. ALWAYS provide ALL brief fields. assigned_agent and reviewer are REQUIRED — an unassigned task will never be picked up. Pick an agent whose role matches the work (see 'Agent Selection Guide' in CLAUDE.md). Tasks that belong to a Scope (scope_id set) stay in Backlog until the scope transitions to 'executing'. Tasks without a scope auto-move to Ready when brief is complete.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workstream_id": {"type": "string", "description": "REQUIRED. Workstream UUID."},
                    "title": {"type": "string", "description": "REQUIRED. Task title."},
                    "description": {"type": "string", "description": "Task description"},
                    "assigned_agent": {"type": "string", "description": "REQUIRED. Name of the agent that will execute this task (e.g. 'manager-assistant', 'analyst', 'frontend-developer'). Must match an agent in your team roster. Never leave empty — unassigned tasks stall in Ready."},
                    "reviewer": {"type": "string", "description": "REQUIRED. Agent name for the designated reviewer. MUST be different from assigned_agent. An agent cannot review its own work."},
                    "priority": {"type": "string", "description": "Priority: urgent, high, medium, low"},
                    "labels": {"type": "array", "items": {"type": "string"}, "description": "Labels as JSON array"},
                    "scope_id": {"type": "string", "description": "Scope UUID this task belongs to. REQUIRED when the workstream has any non-Done scopes — you MUST create a Scope first (create_scope), then create tasks inside it, then call activate_scope. Leave out ONLY for quick legacy/ad-hoc tasks."},
                    "goal": {"type": "string", "description": "REQUIRED for Ready. What this task achieves (1 sentence)"},
                    "context": {"type": "string", "description": "REQUIRED for Ready. Business context — why this matters"},
                    "inputs": {"type": "string", "description": "REQUIRED for Ready. Files, links, references. Use 'None' if no inputs needed"},
                    "output_format": {"type": "string", "description": "REQUIRED for Ready. Expected output structure"},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "description": "REQUIRED for Ready. Checklist items (at least 1)"},
                    "allowed_tools": {"type": "array", "items": {"type": "string"}, "description": "Optional. Tools the worker may use"},
                    "required_skills": {"type": "array", "items": {"type": "string"}, "description": "Optional. Required skills"},
                    "risks_and_edge_cases": {"type": "string", "description": "REQUIRED for Ready. Known pitfalls. Use 'None' if no risks"},
                    "verification_steps": {"type": "string", "description": "REQUIRED for Ready. How to self-validate before submitting"},
                    "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Array of readable_ids (e.g. ['WR-003.T01']) that must reach 'done' before this task can move to Ready. REQUIRED when adding a task to a scope that is already Ready/Executing with active tasks — set it to the readable_id of the last incomplete task to preserve ordering."},
                },
                "required": ["workstream_id", "title", "assigned_agent", "reviewer", "goal", "context", "inputs", "output_format", "acceptance_criteria", "risks_and_edge_cases", "verification_steps"],
            },
            "action": "create_task",
        },
        {
            "name": "create_scope",
            "description": "Create a Scope — a planning container for a body of work inside a workstream. The scope starts in 'preparing' state. Create the scope first, then create all tasks inside it with proper depends_on, then call activate_scope to release them. Only ONE scope per workstream may be in 'preparing' at a time. Do not use for a single one-off task with no follow-up — create the task directly.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workstream_id": {"type": "string", "description": "REQUIRED. Workstream UUID."},
                    "name": {"type": "string", "description": "REQUIRED. Descriptive name (e.g. 'User Authentication Epic')."},
                    "description": {"type": "string", "description": "Optional long-form description of the scope."},
                    "short_key": {"type": "string", "description": "Optional 1-2 word key for UI display (e.g. 'Auth', 'Onboarding'). Max 30 chars."},
                    "position": {"type": "integer", "description": "Ordering position within the workstream. Lower position scopes execute first. Default 0."},
                },
                "required": ["workstream_id", "name"],
            },
            "action": "create_scope",
        },
        {
            "name": "update_scope",
            "description": "Update a scope's metadata (name, description, short_key, position). Only when the scope is still in 'preparing', 'ready', or 'executing' state. Do not use on 'done' or 'archived' scopes — those are terminal and will reject the update.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scope_id": {"type": "string", "description": "REQUIRED. Scope UUID."},
                    "name": {"type": "string", "description": "New scope name (e.g. 'User Authentication Epic')."},
                    "description": {"type": "string", "description": "New long-form description of the scope."},
                    "short_key": {"type": "string", "description": "1-2 word key for UI display (e.g. 'Auth'). Max 30 chars."},
                    "position": {"type": "integer", "description": "Ordering position within the workstream — lower scopes execute first."},
                },
                "required": ["scope_id"],
            },
            "action": "update_scope",
        },
        {
            "name": "activate_scope",
            "description": "Activate a scope: preparing → ready. Validates that the scope has at least one task and all task briefs are complete. If no other scope is currently executing in the same workstream, this scope also auto-promotes to 'executing' and its ready tasks become dispatchable. Only when all planning for the scope is done — do not activate while you are still adding tasks or refining their briefs, as the validation will likely reject empty briefs.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scope_id": {"type": "string", "description": "REQUIRED. Scope UUID."},
                },
                "required": ["scope_id"],
            },
            "action": "activate_scope",
        },
        {
            "name": "archive_scope",
            "description": "Archive a scope (cancel / soft-delete). Blocked if any task inside is in 'in_progress' or 'review'. Auto-promotes the next ready scope if this was the executing scope. Only when cancelling work entirely — do not use to mark a scope as finished (the scope auto-completes to 'done' when its last task reaches a terminal state).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scope_id": {"type": "string", "description": "REQUIRED. Scope UUID."},
                },
                "required": ["scope_id"],
            },
            "action": "archive_scope",
        },
        {
            "name": "list_scopes",
            "description": "List scopes, optionally filtered by workstream and/or state.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workstream_id": {"type": "string", "description": "Filter by workstream UUID."},
                    "state": {"type": "string", "description": "Filter by state (comma-separated for multiple): preparing, ready, executing, done, archived."},
                },
            },
            "action": "list_scopes",
        },
        {
            "name": "get_scope",
            "description": "Get full details of a single scope including aggregate task counts.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scope_id": {"type": "string", "description": "REQUIRED. Scope UUID."},
                },
                "required": ["scope_id"],
            },
            "action": "get_scope",
        },
        {
            "name": "update_task",
            "description": "Update task fields. Only include fields you want to change. Do not use to move the task between board columns — that's `move_task`. Do not use to change a task's workstream or its parent scope; those are immutable after creation.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "REQUIRED. Task UUID or readable_id"},
                    "title": {"type": "string", "description": "New task title."},
                    "description": {"type": "string", "description": "New task description."},
                    "assigned_agent": {"type": "string", "description": "Reassign the task to this agent. Must match a roster name; empty string clears the assignment (task will stall in Ready)."},
                    "reviewer": {"type": "string", "description": "Designated reviewer agent name. Empty string to clear."},
                    "priority": {"type": "string", "description": "New priority: urgent / high / medium / low."},
                    "labels": {"type": "array", "items": {"type": "string"}, "description": "Replacement labels list (REPLACES existing — to add one, pass the full set)."},
                    "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Array of readable_ids that must reach 'done' before this task can move to Ready. Replaces existing dependencies."},
                },
                "required": ["task_id"],
            },
            "action": "update_task",
        },
        {
            "name": "move_task",
            "description": (
                "Move a task to a new board column. Manager use is RARE — "
                "reviews are automated by the designated reviewer, so the "
                "only legitimate Manager case is an explicit user-requested "
                "manual override (e.g. \"force this to done\"). Do NOT use "
                "to drive normal review flow. Valid statuses: backlog, "
                "ready, in_progress, blocked, review, done, archived."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "REQUIRED. Task UUID or readable_id."},
                    "new_status": {
                        "type": "string",
                        "enum": ["backlog", "ready", "in_progress", "blocked", "review", "done", "archived"],
                        "description": "REQUIRED. Target column.",
                    },
                    "comment": {"type": "string", "description": "Reason for the move (will appear in Activity)."},
                },
                "required": ["task_id", "new_status"],
            },
            "action": "move_task",
            "transform": "move_task",
        },
        {
            "name": "add_activity",
            "description": (
                "Post to a task's Activity feed. Manager use: \"answer\" "
                "to respond to a worker's question; \"comment\" for ad-hoc "
                "notes the user might want to see in the task history. Do "
                "NOT use \"checkpoint\" or \"question\" — those are worker "
                "event types."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "REQUIRED. Task UUID or readable_id."},
                    "event_type": {
                        "type": "string",
                        "enum": ["comment", "answer"],
                        "description": "REQUIRED. answer = reply to a worker question; comment = general note.",
                    },
                    "content": {"type": "string", "description": "REQUIRED. The message text."},
                },
                "required": ["task_id", "event_type", "content"],
            },
            "action": "add_activity",
            "transform": "add_activity",
        },
        {
            "name": "archive_task",
            "description": (
                "Archive a task (soft-delete; preserves history). DEFAULT "
                "for \"make this go away\" — superseded scopes, dropped "
                "requirements, duplicate work kept under a single "
                "authoritative task. Blocked while the task is in_progress "
                "or review (move it to blocked first). Prefer this over "
                "delete_task in 99% of cases. Do not use as a substitute "
                "for moving a completed task to 'done' — only use for work "
                "that should NOT have happened (cancelled, superseded, "
                "duplicate)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task UUID or readable_id."},
                    "comment": {"type": "string", "description": "Reason for archiving (recorded in Activity)."},
                },
                "required": ["task_id"],
            },
            "action": "move_task",
            "transform": "archive_task",
        },
        {
            "name": "delete_task",
            "description": (
                "Permanently delete a task. IRREVERSIBLE — destroys the "
                "Activity log and any Artifacts. Use ONLY for typos, "
                "accidentally created tasks with no meaningful history, or "
                "PII removal after review. For everything else use "
                "archive_task."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task UUID."},
                },
                "required": ["task_id"],
            },
            "action": "delete_task",
        },
        {
            "name": "list_scripts",
            "description": (
                "List every script registered in this office (summary only). "
                "Use BEFORE delegating scripting work to check if a matching "
                "script already exists — re-using an existing script is "
                "faster than creating one. Also useful when the user asks "
                "'what scripts do we have?'."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
            "action": "list_scripts",
        },
        {
            "name": "get_script",
            "description": (
                "Get full details for one registered script: manifest, "
                "variable_schema, entry_point. Useful when reviewing a "
                "completed script task or answering a user question about "
                "what a script does."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "script_name": {"type": "string", "description": "Registered script slug (e.g. 'source-linkedin-profiles')."},
                },
                "required": ["script_name"],
            },
            "action": "get_script",
        },
        {
            "name": "list_script_executions",
            "description": (
                "List the recent execution history of a script (newest first). "
                "Use this to investigate user questions like 'why did my cron "
                "fail' or to verify an Automation Script Developer's Test "
                "Evidence block matches the DB."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "script_name": {"type": "string", "description": "Registered script slug to list executions for."},
                    "limit": {"type": "integer", "description": "Max rows returned (default 20, max 100)."},
                },
                "required": ["script_name"],
            },
            "action": "list_script_executions",
        },
        {
            "name": "list_agents",
            "description": (
                "List the office's team roster — every active agent "
                "with their name, role, model, allowed tools, skills "
                "(with descriptions and connection types) and "
                "connectors. Your system prompt includes a snapshot "
                "of this at turn start, but the snapshot can drift "
                "if the user added/removed agents during the turn. "
                "Call this when: (1) you need to pick the right agent "
                "for a task and want to confirm capabilities, "
                "(2) the user asks 'who's on the team?' / 'what can "
                "X do?', (3) you suspect the roster may have changed. "
                "Returns only ``is_active=true`` agents by default; "
                "pass include_inactive=true to see deactivated ones."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_type": {
                        "type": "string",
                        "enum": ["system", "custom"],
                        "description": (
                            "Optional filter. ``system`` = the four "
                            "built-in agents (Analyst, Automation "
                            "Script Developer, Auditor, Manager "
                            "Assistant). ``custom`` = user-defined "
                            "domain agents. Omit to list both."
                        ),
                    },
                    "include_inactive": {
                        "type": "boolean",
                        "description": (
                            "When true, deactivated agents are "
                            "included in the result. Default false."
                        ),
                    },
                },
            },
            "action": "list_agents",
        },
        {
            "name": "search_kb",
            "description": "Search the Knowledge Base for reference documents.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional list of tag labels to AND-filter results by."},
                    "limit": {"type": "integer", "description": "Max results (default 5)"},
                },
                "required": ["query"],
            },
            "action": "kb_search",
        },
        {
            "name": "get_kb_document",
            "description": "Get full content of a Knowledge Base document.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "Document UUID"},
                },
                "required": ["document_id"],
            },
            "action": "kb_get_document",
        },
        {
            "name": "save_file",
            "description": "Register a file you already wrote to disk. First write the file, then call this to register it. Only for files that should appear in the Office Files UI as durable deliverables. Do not use for transient scratch files, intermediate drafts you'll overwrite, or files you wrote into a task's workspace directory (those are task artifacts, not office files).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Human-readable title shown in the Office Files UI."},
                    "file_path": {"type": "string", "description": "Path where you wrote the file"},
                    "file_type": {"type": "string", "description": "markdown, text, json, csv"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tag labels for filtering in the Office Files UI."},
                },
                "required": ["title", "file_path"],
            },
            "action": "office_save_file",
        },
        {
            "name": "list_files",
            "description": "List files in the office file storage.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "AND-filter the listing to files carrying every tag in this list."},
                    "source_agent": {"type": "string", "description": "Filter to files written by this agent name (e.g. 'analyst')."},
                    "limit": {"type": "integer", "description": "Max rows returned (default 100)."},
                },
            },
            "action": "office_list_files",
        },
        {
            "name": "get_file",
            "description": "Get the full content of an office file.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "File UUID"},
                },
                "required": ["file_id"],
            },
            "action": "office_get_file",
        },
    ]

