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
            "description": (
                "List tasks on the board (filtered). Use to check for "
                "duplicates before creating, and to answer 'what's in "
                "flight?'. NOT for reading one task's brief — that's "
                "`get_task_detail`."
            ),
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
            "description": (
                "Inspect one task — Brief + Activity + Artifacts. NOT "
                "for enumerating (use `get_board`) or reading file "
                "contents (use `Read` on the artifact file_path)."
            ),
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
            "description": (
                "Create a task with a complete Brief (all 9 fields). "
                "``assigned_agent`` + ``reviewer`` REQUIRED — unassigned "
                "tasks stall. Scoped tasks stay in Backlog until the "
                "scope is `executing`; unscoped tasks auto-move to "
                "Ready when the Brief is complete. Agent selection: "
                "see CLAUDE.md."
            ),
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
            "description": (
                "Create a Scope (planning container for related tasks). "
                "Starts in `preparing`. Order: create_scope → "
                "create_task(scope_id=…) × N → activate_scope. Max one "
                "`preparing` scope per workstream. Skip for a single "
                "one-off task with no follow-up."
            ),
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
                "Move a task to a new column. Manager use is RARE: "
                "reviews are automated by the designated reviewer; "
                "only use for an explicit user-requested override "
                "(\"force this to done\")."
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
                "Post to a task's Activity. `answer` = reply to a "
                "worker question; `comment` = ad-hoc note. NOT for "
                "`checkpoint` / `question` — those are worker types."
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
                "Archive a task (soft-delete; history preserved). "
                "DEFAULT for cancelled / superseded / duplicate work. "
                "Refused while `in_progress` or `review` — move to "
                "`blocked` first. NOT a substitute for `move_task → done`."
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
            "name": "retry_blocked_task",
            "description": (
                "ESCAPE HATCH for a blocked task that has hit the "
                "blocked-bounce cap (default 1) and cannot be moved "
                "back to ready by the regular move_task tool. Use this "
                "ONLY after the underlying issue is fixed (credentials "
                "refreshed, plan tier raised, rate-limit resolved, "
                "etc.) — it resets blocked_bounce_count to 0 and moves "
                "the task to ready in one atomic operation, with a "
                "full audit trail recording the actor + reason.\n\n"
                "WHEN NOT TO USE: this is NOT a way to skip the bounce "
                "cap on a task that will fail again. The cap exists to "
                "stop infinite loops on a permanently-broken setup. If "
                "the same task hits the cap again after retry, archive "
                "it and decide a different approach — do not retry "
                "twice in a row. Brief must be complete, dependencies "
                "must be met, scope must be executing (same gates as a "
                "normal backlog → ready move)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task UUID or readable_id (e.g. 'WR-003.T14').",
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "REQUIRED. What was fixed so the retry is "
                            "justified. Goes into the audit trail "
                            "(e.g. 'refreshed Claude credentials', "
                            "'rotated Unipile API key', 'raised plan "
                            "tier'). Short, specific sentence."
                        ),
                    },
                },
                "required": ["task_id", "reason"],
            },
            "action": "retry_blocked_task",
        },
        {
            "name": "decide_action_request",
            "description": (
                "Approve or reject a pending action_request that's "
                "waiting on YOUR decision (sent to you as a synthetic "
                "`[Action Request — Auto-Decide: ...]` chat turn). "
                "In Phase A only ``create_task`` and "
                "``request_clarification`` approvals trigger an "
                "automatic side effect; every other type records the "
                "decision for audit and you MUST take the follow-up "
                "action yourself (see your CLAUDE.md ``Phase A "
                "side-effect scope`` table). Reject closes the row "
                "with no effect — use ``decision_notes`` to explain "
                "why. Do not use for ``requires_user=True`` requests "
                "in the user's inbox (credentials / infrastructure / "
                "cost / critical severity) — those belong to the user. "
                "**DEDUP**: ``setup_office_secret`` and a few other "
                "request types deduplicate at propose-time on "
                "``(office_id, payload key fields)``. A second propose "
                "for the same key extends the existing pending row's "
                "metadata (e.g. ``used_by_scripts`` list) rather than "
                "creating a new one. So if you see the same request_id "
                "from multiple workers, that's expected — one decision "
                "covers all of them."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "request_id": {
                        "type": "string",
                        "description": "REQUIRED. UUID of the pending action_request.",
                    },
                    "decision": {
                        "type": "string",
                        "enum": ["approved", "rejected"],
                        "description": "REQUIRED. Approved fires the side effect; rejected closes without effect.",
                    },
                    "decision_notes": {
                        "type": "string",
                        "description": (
                            "Short justification for your decision. "
                            "On approve: what made this fit. On reject: "
                            "why and (if applicable) what the requester "
                            "should do instead. For "
                            "``request_clarification`` approvals, this "
                            "field IS the answer that gets posted as an "
                            "``answer`` Activity on the source task."
                        ),
                    },
                },
                "required": ["request_id", "decision"],
            },
            "action": "decide_action_request",
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
            "name": "list_office_secrets",
            "description": (
                "List the office's SHARED secrets (GitLab-style — set "
                "once in Settings → Security, reusable by any script). "
                "Returns metadata only (name, description, fingerprint, "
                "timestamps); the value never leaves the user's machine. "
                "Use to answer 'what credentials does this office have?' "
                "and to brief Automation Script Developer with the names "
                "to recommend in script variable descriptions (the user "
                "binds each variable to an Office Secret via the "
                "Variables UI; no manifest field needed). "
                "If a secret the user is asking about doesn't appear "
                "here, ask the user to add it (Settings → Security → "
                "Office Secrets) — never try to set it yourself."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
            "action": "list_office_secrets",
        },
        {
            "name": "list_office_secret_usage",
            "description": (
                "For every office secret, return which scripts already "
                "reference it. Pair with ``list_office_secrets`` to "
                "answer 'which scripts depend on this credential?' "
                "before rotating or deleting it."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
            "action": "list_office_secret_usage",
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
            "name": "list_script_templates",
            "description": (
                "List the Cubicle-curated marketplace catalog of starter "
                "scripts (Phase 2). Returns summary metadata per template "
                "(id, name, display_name, description, category, tags, "
                "recommended_office_secrets, variable_schema). Use BEFORE "
                "delegating a new-script task to the Automation Script "
                "Developer — if a template matches the requirements, brief "
                "the ASD with the template id so it installs instead of "
                "writing from scratch. Read-only."
            ),
            "inputSchema": {"type": "object", "properties": {}},
            "action": "list_script_templates",
        },
        {
            "name": "get_script_template",
            "description": (
                "Fetch one marketplace template's full payload — summary "
                "plus default_files (script.yaml, main.py, lib/, "
                "requirements.txt, README.md). Use after list_script_templates "
                "to preview the code before deciding whether to recommend "
                "the template to the Automation Script Developer. Read-only."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "template_id": {
                        "type": "string",
                        "description": "Template id (e.g. cubicle-hello-world).",
                    },
                },
                "required": ["template_id"],
            },
            "action": "get_script_template",
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
            "description": (
                "Full-text search across the office Knowledge Base — "
                "user-curated reference docs (specs, runbooks, decisions, "
                "playbooks). Use to ground a Brief in authoritative "
                "office knowledge before delegating, and to answer the "
                "user's questions about internal conventions. Returns "
                "hit snippets + document IDs; call `get_kb_document` "
                "for full content. Do not use for code search (use "
                "`Grep` / `Glob`) or for the office Files index "
                "(use `list_files`)."
            ),
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
            "description": (
                "Fetch the full body of a Knowledge Base document by "
                "ID. Use AFTER `search_kb` returned a candidate "
                "document_id whose snippet looked relevant. Do not "
                "call without a document_id (there is no 'browse all "
                "documents' mode — use `search_kb` with a broad query)."
            ),
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
            "description": (
                "List durable deliverables saved in the office Files "
                "index. Use BEFORE delegating new research to check "
                "whether a similar deliverable already exists (e.g. an "
                "Analyst report from last week), and to find input files "
                "to reference in a new Brief. Filter by tag or source "
                "agent to narrow noisy result sets. Do not use to read "
                "raw file content — pair with `get_file` for that."
            ),
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
            "description": (
                "Fetch the full content of an office file by ID. Use "
                "AFTER `list_files` returned a candidate file_id, or "
                "when a task's artifacts reference an office file you "
                "need to inspect. Do not call without a file_id — "
                "discover IDs via `list_files`."
            ),
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

