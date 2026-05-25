"""Worker-role MCP tool definitions (split from mcp_tool_server.py).

Same shape as ``tools_manager`` but for executor/reviewer sessions.
"""
from __future__ import annotations


def get_worker_tools() -> list[dict]:
    """Tool definitions for Worker sessions."""
    return [
        {
            "name": "update_task",
            "description": (
                "Update task fields. Workers use this ONLY to unassign "
                "themselves after a non-designated review (pass "
                "assigned_agent=\"\"). Do NOT use to change priority, "
                "labels, or other fields — propose those via "
                "propose_update_task instead."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task UUID or readable_id (e.g. 'WR-003.T01')."},
                    "assigned_agent": {"type": "string", "description": "New agent name, or empty string \"\" to unassign yourself after a review."},
                },
                "required": ["task_id"],
            },
            "action": "update_task",
        },
        {
            "name": "update_status",
            "description": (
                "Submit YOUR task by moving its status. Allowed: "
                "in_progress -> review (work complete), in_progress -> "
                "blocked (genuine blocker, after posting a `question` "
                "activity). Calling this with new_status=review is your "
                "FINAL action — STOP IMMEDIATELY after; further tool calls "
                "in the same session are rejected. Do NOT use to move other "
                "tasks; do NOT use as a substitute for the designated "
                "reviewer's move_task."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Your task's UUID (from the task prompt)."},
                    "new_status": {
                        "type": "string",
                        "enum": ["review", "blocked"],
                        "description": "review = work complete; blocked = cannot proceed.",
                    },
                    "comment": {"type": "string", "description": "Optional one-line summary of the submission or blocker."},
                },
                "required": ["task_id", "new_status"],
            },
            "action": "task_status_update",
        },
        {
            "name": "add_activity",
            "description": (
                "Post to this task's Activity feed. Use \"checkpoint\" for "
                "concrete progress (something was produced), \"question\" "
                "when you genuinely need Manager input before continuing, "
                "and \"comment\" for everything else. Reviewers also use "
                "\"comment\" to post their verdict. Do not use as a "
                "substitute for `update_status` when you finish a task, "
                "and do not use to post `task_proposed` events directly — "
                "use the `propose_task` tool for that."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task UUID or readable_id."},
                    "event_type": {
                        "type": "string",
                        "enum": ["checkpoint", "question", "comment", "task_proposed"],
                        "description": "checkpoint = progress; question = blocker for Manager; comment = note; task_proposed = legacy (prefer propose_task).",
                    },
                    "content": {"type": "string", "description": "Activity content text."},
                },
                "required": ["task_id", "event_type", "content"],
            },
            "action": "add_activity",
            "transform": "add_activity",
        },
        {
            "name": "get_my_brief",
            "description": (
                "Re-read YOUR currently-assigned task — full Brief, recent "
                "Activity, registered Artifacts. Use this to refresh state "
                "after a long tool sequence or to confirm the task is still "
                "in the status you assumed. Pass the task_id from your task "
                "prompt; do NOT use this to inspect other tasks (use "
                "get_task_detail for that)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Your task's UUID (from the task prompt)."},
                },
                "required": ["task_id"],
            },
            "action": "get_task_detail",
        },
        {
            "name": "get_task_detail",
            "description": (
                "Inspect ANOTHER task on the board (not your own). Used "
                "primarily by the Manager Assistant in Board Operator mode "
                "for triage. Returns Brief + Activity + Artifacts. For your "
                "OWN task call get_my_brief — same backend, but get_my_brief "
                "documents that you should not be inspecting others' work."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task UUID or readable_id (e.g. 'WR-003.T01')."},
                },
                "required": ["task_id"],
            },
            "action": "get_task_detail",
        },
        {
            "name": "create_task",
            "description": "Create a task with a complete brief. Provide ALL brief fields for auto-Ready. Only when you are Board Operator (Manager Assistant) or are explicitly authorised to create tasks on behalf of the Manager — regular worker agents must use `propose_task` instead, which routes the request through the Action Request inbox for approval.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workstream_id": {"type": "string", "description": "REQUIRED. Workstream UUID"},
                    "title": {"type": "string", "description": "REQUIRED. Task title"},
                    "description": {"type": "string", "description": "Task description"},
                    "assigned_agent": {"type": "string", "description": "Agent name to assign"},
                    "reviewer": {"type": "string", "description": "Reviewer agent name"},
                    "priority": {"type": "string", "description": "urgent, high, medium, low"},
                    "labels": {"type": "array", "items": {"type": "string"}, "description": "Optional label tags (e.g. ['frontend','urgent']) shown on the board card."},
                    "goal": {"type": "string", "description": "REQUIRED. What this task achieves"},
                    "context": {"type": "string", "description": "REQUIRED. Business context"},
                    "inputs": {"type": "string", "description": "REQUIRED. Files/refs or 'None'"},
                    "output_format": {"type": "string", "description": "REQUIRED. Expected output"},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "description": "REQUIRED. Checklist (min 1)"},
                    "allowed_tools": {"type": "array", "items": {"type": "string"}, "description": "Optional tool allow-list the assigned agent may use (defaults to the agent's full toolbelt if omitted)."},
                    "required_skills": {"type": "array", "items": {"type": "string"}, "description": "Optional skill slugs the assigned agent must have for this task."},
                    "risks_and_edge_cases": {"type": "string", "description": "REQUIRED. Pitfalls or 'None'"},
                    "verification_steps": {"type": "string", "description": "REQUIRED. How to validate"},
                },
                "required": ["workstream_id", "title", "goal", "context", "inputs", "output_format", "acceptance_criteria", "risks_and_edge_cases", "verification_steps"],
            },
            "action": "create_task",
        },
        {
            "name": "move_task",
            "description": "Move a task to a new board column. Used by Board Operator for review/blocked management. Do not use to submit your OWN task for review — call `update_status` with status=\"review\" instead. Only when triaging tasks assigned to others as a designated reviewer or in Board Operator mode.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task UUID or readable_id"},
                    "new_status": {"type": "string", "description": "Target status: done, ready, blocked, in_progress"},
                    "comment": {"type": "string", "description": "Reason for the move"},
                },
                "required": ["task_id", "new_status"],
            },
            "action": "move_task",
            "transform": "move_task",
        },
        {
            "name": "propose_task",
            "description": (
                "Propose a new task to the Manager. Legacy entry point — "
                "prefer the typed tools below (`propose_subtask`, "
                "`propose_split_into_scope`, etc.) for richer requests. "
                "This still works and is bridged into the Action Request "
                "inbox automatically. Do not use to create a task directly; "
                "this only sends a proposal that the Manager must approve."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Current task UUID"},
                    "content": {"type": "string", "description": "Proposed task title and description"},
                },
                "required": ["task_id", "content"],
            },
            "action": "add_activity",
            "transform": "propose_task",
        },
        # ── Action Request family (Phase A of Worker→Manager channel) ──
        # All of these are sugar over the generic ``propose_action`` —
        # they exist as separate tools so the Manager / Worker prompt
        # surfaces are friendlier than asking workers to remember enum
        # values. Backend dispatcher accepts ``propose_action`` with a
        # typed ``request_type`` + ``payload``; the transforms below
        # do the rewrite. The ``source_task_id`` is auto-injected from
        # ``TASK_ID`` env so workers don't have to pass it.
        {
            "name": "propose_subtask",
            "description": (
                "Propose a follow-up SUBTASK that should run inside the "
                "same Scope as your current task. Only when finishing your "
                "task naturally produces a clearly-scoped next step. Do not "
                "use for unrelated follow-up work that needs its own "
                "Scope — use `propose_split_into_scope` for that. Lands in "
                "the Inbox as request_type=create_subtask."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Concise task title (one sentence)."},
                    "brief_hints": {"type": "object", "description": "Optional partial Brief fields (goal, context, inputs, etc.) the Manager can use as a starting point."},
                    "justification": {"type": "string", "description": "Why this subtask is needed (one or two sentences)."},
                },
                "required": ["title", "justification"],
            },
            "action": "propose_action",
            "transform": "propose_subtask",
        },
        {
            "name": "propose_split_into_scope",
            "description": (
                "Propose splitting a body of work into a NEW Scope (multiple "
                "related tasks). Use when finishing your task reveals that "
                "follow-up requires real planning, not a single subtask. "
                "Lands in the Inbox as request_type=split_into_scope and "
                "always escalates to the Manager (never auto-handled)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scope_short_key": {"type": "string", "description": "1-2 word UI label, e.g. 'Auth', 'Sourcing'."},
                    "scope_name": {"type": "string", "description": "Full scope name, e.g. 'User auth migration'."},
                    "tasks": {
                        "type": "array",
                        "description": "Array of {title, brief_hints?} entries describing each task in the new scope.",
                        "items": {"type": "object"},
                    },
                    "justification": {"type": "string", "description": "Why this body of work needs its own Scope."},
                },
                "required": ["scope_short_key", "scope_name", "tasks", "justification"],
            },
            "action": "propose_action",
            "transform": "propose_split_into_scope",
        },
        {
            "name": "propose_update_task",
            "description": (
                "Propose updating fields on an existing task (priority, "
                "labels, description, assigned_agent, reviewer, depends_on). "
                "Lands in the Inbox as request_type=update_task. Routine "
                "field-only changes are auto-handled by Manager Assistant "
                "in Phase B; for now they queue for manual approval. Do not "
                "use to change a task's status or workstream — those are "
                "out of scope for this tool."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Target task UUID or readable_id."},
                    "changes": {"type": "object", "description": "Whitelisted keys: priority, labels, description, assigned_agent, reviewer, depends_on."},
                    "justification": {"type": "string", "description": "Why the change is needed."},
                },
                "required": ["task_id", "changes", "justification"],
            },
            "action": "propose_action",
            "transform": "propose_update_task",
        },
        {
            "name": "escalate_blocker",
            "description": (
                "Tell the Manager you are blocked and cannot proceed. Use "
                "ONLY when posting a `question` to Activity isn't enough "
                "(e.g. you need a scope decision, not a clarification). "
                "Lands in the Inbox as request_type=escalate_blocker — "
                "always routed to the Manager."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "blocker_summary": {"type": "string", "description": "One-sentence description of what's blocking you."},
                    "suggested_unblock": {"type": "string", "description": "Optional: what the Manager could do to unblock."},
                    "justification": {"type": "string", "description": "Detail / context the Manager needs to decide."},
                },
                "required": ["blocker_summary", "justification"],
            },
            "action": "propose_action",
            "transform": "escalate_blocker",
        },
        {
            "name": "request_clarification",
            "description": (
                "Ask a question that needs a real answer, not just a comment "
                "on Activity. Only when the brief is ambiguous and you can't "
                "make progress without input. Manager Assistant tries to "
                "answer from CLAUDE.md / board context first; truly "
                "ambiguous questions escalate to the Manager. Do not use "
                "for general comments or status updates — post those to "
                "Activity instead."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The exact question (precise, single-purpose)."},
                    "justification": {"type": "string", "description": "Why this matters / what you'll do once answered."},
                },
                "required": ["question", "justification"],
            },
            "action": "propose_action",
            "transform": "request_clarification",
        },
        {
            "name": "request_review_check",
            "description": (
                "Ask the Manager (or designated reviewer) to confirm whether "
                "a specific acceptance criterion is met BEFORE you submit. "
                "Only when one criterion is genuinely a judgement-call you "
                "cannot resolve yourself. Do not use for clear yes/no "
                "criteria you can verify mechanically — the reviewer will "
                "check those at submission time. Lands in the Inbox as "
                "request_type=request_review_check."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "criterion_index": {"type": "integer", "description": "Zero-indexed criterion from your brief (optional)."},
                    "justification": {"type": "string", "description": "What you produced and why you're unsure it satisfies the criterion."},
                },
                "required": ["justification"],
            },
            "action": "propose_action",
            "transform": "request_review_check",
        },
        {
            "name": "propose_artifact_handoff",
            "description": (
                "Tell the Manager that a specific artifact you produced is "
                "needed by another (already-existing) task. Lands in the "
                "Inbox as request_type=propose_artifact_handoff. Only when "
                "the target task already exists — do not use to propose "
                "new work; for that use `propose_subtask` or "
                "`propose_split_into_scope`."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target_task_id": {"type": "string", "description": "Task UUID/readable_id that should consume the artifact."},
                    "file_path": {"type": "string", "description": "Workspace-relative path to the artifact file."},
                    "justification": {"type": "string", "description": "Why this task needs that file."},
                },
                "required": ["target_task_id", "file_path", "justification"],
            },
            "action": "propose_action",
            "transform": "propose_artifact_handoff",
        },
        {
            "name": "schedule_script",
            "description": "Create or update a cron schedule for a script. Standard 5-field cron (min hour dom mon dow) or aliases (@hourly @daily @weekly @monthly). The schedule fires automatically; each firing produces a ScriptExecution row visible in the script's history. Use this to automate recurring tasks instead of calling execute_script repeatedly.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "script_name": {"type": "string", "description": "Slug of the script to schedule."},
                    "name": {"type": "string", "description": "Short name for this schedule (unique per script). Example: 'morning-refresh', 'hourly-sync'."},
                    "cron_expression": {"type": "string", "description": "Cron expression, e.g. '0 9 * * 1-5' (9am weekdays) or '@daily'."},
                    "description": {"type": "string", "description": "Optional. What this schedule does / why it exists."},
                    "variable_overrides": {"type": "object", "description": "Per-run variable overrides as a dict. Keys must match the script's variable_schema."},
                    "is_active": {"type": "boolean", "description": "Default true. Set false to create disabled."},
                },
                "required": ["script_name", "name", "cron_expression"],
            },
            "action": "schedule_script",
        },
        {
            "name": "list_script_crons",
            "description": "List cron schedules, optionally filtered by script_name. Only when you need to inspect existing schedules — do not use as a pre-check before `schedule_script`, which is itself idempotent on (script_name, name).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "script_name": {"type": "string", "description": "Optional filter."},
                },
            },
            "action": "list_script_crons",
        },
        {
            "name": "update_script_cron",
            "description": "Update a cron schedule (change expression, overrides, active state). Only when modifying an existing schedule — do not use to create a new one; for that, use `schedule_script` (idempotent by name).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "cron_id": {"type": "string", "description": "UUID of the cron schedule."},
                    "name": {"type": "string", "description": "New short name for this schedule (e.g. 'morning-sync')."},
                    "description": {"type": "string", "description": "Optional longer description of why this schedule exists."},
                    "cron_expression": {"type": "string", "description": "New crontab expression (e.g. '0 9 * * *') or shortcut ('@daily')."},
                    "variable_overrides": {"type": "object", "description": "Per-run variable values, merged on top of variables.json at execution time."},
                    "is_active": {"type": "boolean", "description": "Toggle the schedule on/off without deleting it."},
                },
                "required": ["cron_id"],
            },
            "action": "update_script_cron",
        },
        {
            "name": "delete_script_cron",
            "description": "Delete a cron schedule permanently. Only when the schedule should be removed entirely. Do not use to temporarily pause a schedule — call `update_script_cron` with is_active=false instead, which preserves the row.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "cron_id": {"type": "string", "description": "UUID of the cron schedule to remove."},
                },
                "required": ["cron_id"],
            },
            "action": "delete_script_cron",
        },
        {
            "name": "list_script_templates",
            "description": (
                "List the Cubicle-curated marketplace catalog of "
                "starter scripts (Phase 2 of the Scripts marketplace). "
                "Returns summary metadata per template: id, name, "
                "display_name, description, category, tags, "
                "recommended_office_secrets, variable_schema. Use this "
                "in your research phase before deciding to write a new "
                "script from scratch — a template is a faster start "
                "when it matches ≥80% of the requirements. Read-only."
            ),
            "inputSchema": {"type": "object", "properties": {}},
            "action": "list_script_templates",
        },
        {
            "name": "get_script_template",
            "description": (
                "Fetch a marketplace template's full payload — "
                "summary + default_files (script.yaml, main.py, "
                "lib/, requirements.txt, README.md). Use after "
                "list_script_templates picked a candidate, to "
                "preview the code before installing. Read-only."
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
            "name": "install_script_from_template",
            "description": (
                "Install a marketplace template into this office "
                "as a new script. Creates a Script row stamped "
                "source_kind='template' + source_template_id, and "
                "lays the template's file map under "
                "/workspace/.scripts/{name}/. Variable VALUES / "
                "bindings are NOT installed — the user configures "
                "them via the Variables UI after install. Prefer "
                "this over `register_script` from scratch when a "
                "matching template exists in the catalog."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "template_id": {
                        "type": "string",
                        "description": "Template id to install.",
                    },
                    "new_name": {
                        "type": "string",
                        "description": (
                            "Override slug (optional — defaults to "
                            "the template's own slug)."
                        ),
                    },
                    "new_display_name": {
                        "type": "string",
                        "description": (
                            "Override display name (optional — "
                            "defaults to the template's display name)."
                        ),
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Source task UUID (audit trail).",
                    },
                },
                "required": ["template_id"],
            },
            "action": "install_script_from_template",
        },
        {
            "name": "clone_script",
            "description": (
                "Duplicate an existing office script under a new name. "
                "Use this in your research phase when an existing script "
                "is a ~70%+ match for the new task — clone it, then Edit "
                "the cloned files to adapt. The clone copies every text "
                "file in the source's workspace dir (script.yaml, main.py, "
                "lib/*.py, requirements.txt, README.md, custom modules) "
                "PLUS the source's variable_schema declarations. It does "
                "NOT copy variable VALUES (variables.json), secrets, "
                "execution history, or cron schedules — the user "
                "reconfigures variables in the UI after the clone lands. "
                "Prefer this over `register_script` from scratch whenever "
                "a near-fit exists."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source_name": {
                        "type": "string",
                        "description": (
                            "Slug of the script to clone (e.g. "
                            "'linkedin-sourcing'). Either source_name OR "
                            "source_script_id must be provided."
                        ),
                    },
                    "source_script_id": {
                        "type": "string",
                        "description": (
                            "UUID of the script to clone. Use this when "
                            "you already have the id from list_scripts; "
                            "otherwise pass source_name."
                        ),
                    },
                    "new_name": {
                        "type": "string",
                        "description": (
                            "Slug for the cloned script (lowercase, "
                            "hyphens). Optional — when omitted the "
                            "platform slugifies new_display_name."
                        ),
                    },
                    "new_display_name": {
                        "type": "string",
                        "description": "Human-readable name for the cloned script.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Source task UUID (audit trail).",
                    },
                },
                "required": ["new_display_name"],
            },
            "action": "clone_script",
        },
        {
            "name": "register_script",
            "description": "Register OR update a script. Idempotent by (office, name). Creating a new script causes the platform to lay down the mini-project boilerplate (script.yaml, main.py, lib/, lib/cubicle/, requirements.txt, README.md) at /workspace/.scripts/{name}/. Then Edit those files via the Edit tool — NEVER Write them yourself, you'd clobber the boilerplate.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Script slug (lowercase, hyphens). Same name on repeat calls updates the existing script in place."},
                    "display_name": {"type": "string", "description": "Human-readable name."},
                    "description": {"type": "string", "description": "Short summary of what the script does."},
                    "variable_schema": {"type": "array", "description": "Variable definitions: [{name, type, is_secret, description}]. Should mirror the variables declared in script.yaml."},
                    "created_by": {"type": "string", "description": "Agent name that authored the script (e.g. 'automation-script-developer')."},
                    "task_id": {"type": "string", "description": "Source task UUID (only on first registration)."},
                },
                "required": ["name", "display_name"],
            },
            "action": "register_script",
        },
        {
            "name": "execute_script",
            "description": "Execute a script in the background. Returns an execution_id. Only when the script is registered AND its bootstrap_status is 'complete'. Do not use as a substitute for the Bash tool to run ad-hoc commands — scripts must be declared in advance via `register_script`.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "script_name": {"type": "string", "description": "Script slug name"},
                    "variable_overrides": {"type": "object", "description": "Optional variable overrides"},
                },
                "required": ["script_name"],
            },
            "action": "script_execute",
            "local": True,
        },
        {
            "name": "get_script_status",
            "description": "Check the status of a running script. Only when you need to wait synchronously for a specific execution — do not poll in a tight loop, the dispatcher will notify the Manager when long-running scripts complete.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "script_name": {"type": "string", "description": "Registered script slug."},
                    "execution_id": {"type": "string", "description": "The execution_id returned by a prior execute_script call."},
                },
                "required": ["script_name", "execution_id"],
            },
            "action": "script_get_status",
            "local": True,
        },
        {
            "name": "list_scripts",
            "description": (
                "List every script registered in this office. Returns a "
                "summary for each (name, display_name, entry_point, "
                "variable_count, has_manifest, source_kind, "
                "source_template_id, cloned_from_script_id, category, "
                "tags). Use this in the research phase (alongside "
                "list_script_templates) to find clone candidates — a "
                "near-fit existing script can be duplicated via "
                "clone_script instead of writing a new one. Also use "
                "when AUDITING a script task to confirm the DB row "
                "exists (a deliverable on disk without a DB row is not "
                "a valid script)."
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
                "List the office's SHARED secrets (GitLab-style — set once "
                "in Settings → Security, reusable by any script in the "
                "office). Returns ONLY metadata: name, description, "
                "fingerprint, timestamps. The actual VALUE is never "
                "returned — it lives on the user's machine and the script "
                "subprocess receives it only via env injection at "
                "``docker exec`` time. Use this BEFORE writing a new "
                "script that needs credentials: declare the variable as "
                "``is_secret: true`` in the manifest and recommend the "
                "matching Office Secret name in the variable's "
                "``description``; the user binds the variable to the "
                "Office Secret via the Variables UI (no manifest field "
                "required). If the secret does NOT exist, escalate to "
                "the user via ``escalate_blocker`` with "
                "category=credentials so the user adds it once and "
                "every script can reuse it. Do NOT try to set or rotate "
                "the value yourself — secrets are user-only by policy."
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
                "reference it (via a variable binding or — for legacy "
                "scripts — a manifest ``from_office_secret`` field). "
                "Pair with ``list_office_secrets``: that lists what "
                "EXISTS, this lists what's already CONNECTED. Use to "
                "answer 'is OPENAI_API_KEY already wired up?' before "
                "deciding to recommend it in a new script's variable "
                "description — and to warn the user 'updating this "
                "secret affects N scripts' before asking them to "
                "rotate it. Read-only."
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
                "Get the full details for a single registered script: name, "
                "display_name, entry_point, variable_schema, cached manifest. "
                "Use this to verify an Automation Script Developer's "
                "delivery — compare the declared variables in script.yaml "
                "against the task brief."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "script_name": {"type": "string", "description": "The script's slug name (folder name)."},
                },
                "required": ["script_name"],
            },
            "action": "get_script",
        },
        {
            "name": "list_script_executions",
            "description": (
                "List recent executions of a script (newest first). Use this "
                "when auditing to verify the Test Evidence block in a "
                "completion checkpoint matches real DB rows — look for at "
                "least one ``status='completed'`` row with a 0 exit_code."
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
            "name": "search_kb",
            "description": (
                "Full-text search across the office Knowledge Base — "
                "user-curated reference docs (specs, runbooks, decisions, "
                "playbooks). Use BEFORE WebSearch when the task is about "
                "this organisation's internal conventions; KB is "
                "authoritative for those, the web is not. Returns hit "
                "snippets + document IDs; call `get_kb_document` for full "
                "content. Do not use to search the office Files index "
                "(use `list_files` for that) or workspace source code "
                "(use `Grep` / `Glob`)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query string."},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tag labels to AND-filter results by."},
                    "limit": {"type": "integer", "description": "Max results (default 5)."},
                },
                "required": ["query"],
            },
            "action": "kb_search",
        },
        {
            "name": "get_kb_document",
            "description": (
                "Fetch the full body of a Knowledge Base document by ID. "
                "Use AFTER `search_kb` returned a candidate document_id "
                "whose snippet looks relevant. Do not call without a "
                "document_id (there is no 'browse all documents' mode — "
                "use `search_kb` with a broad query if you need to "
                "explore)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "KB document UUID returned by search_kb."},
                },
                "required": ["document_id"],
            },
            "action": "kb_get_document",
        },
        {
            "name": "save_file",
            "description": (
                "Register a CONTRACTED deliverable — a file named in the "
                "Brief's Output Format that the reviewer will open to "
                "decide PASS/FAIL. NOT for every file you touched. If "
                "your task is a code change spanning many source files, "
                "register ONE change-summary markdown (files touched, "
                "rationale, test evidence) — NOT each edited .py/.ts. "
                "Source edits live in git; only contracted outputs go "
                "through save_file. Workflow: (1) use the Write tool to "
                "create the file at the per-workstream output path your "
                "task prompt names (typically "
                "/workspace/outputs/{workstream_short_code}/[{scope_readable_id}/]<name>.md), "
                "then (2) call save_file with that exact file_path. "
                "Idempotent — same path on a repeat call reuses the existing "
                "artifact row. Auto-attaches to your current task, so do "
                "NOT also call attach_to_task for your own files."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Human-readable title shown in the office Files page."},
                    "file_path": {"type": "string", "description": "Absolute path where you wrote the file. Must already exist on disk."},
                    "file_type": {"type": "string", "description": "Optional content hint: markdown, text, json, csv."},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags for later discovery via list_files."},
                },
                "required": ["title", "file_path"],
            },
            "action": "office_save_file",
        },
        {
            "name": "list_files",
            "description": "List files in the office's shared storage. Use to discover prior deliverables before doing redundant research.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Filter by tags (any-match)."},
                    "source_agent": {"type": "string", "description": "Filter by the agent that created the file."},
                    "limit": {"type": "integer", "description": "Max rows (default 50)."},
                },
            },
            "action": "office_list_files",
        },
        {
            "name": "get_file",
            "description": "Get an office file's metadata (title, file_path, tags). Pair with the Read tool on the returned file_path to read actual content.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "File UUID returned by list_files or save_file."},
                },
                "required": ["file_id"],
            },
            "action": "office_get_file",
        },
        {
            "name": "attach_to_task",
            "description": (
                "Attach an office file (already created via save_file) to a "
                "DIFFERENT task as an artifact. Use to link a deliverable "
                "from a prior task as input to your current task. Do not "
                "use for your own deliverables — save_file auto-attaches "
                "to your current task, so attach_to_task is unnecessary "
                "in that case."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Target task UUID or readable_id."},
                    "file_id": {"type": "string", "description": "Office file UUID returned by save_file or list_files."},
                },
                "required": ["task_id", "file_id"],
            },
            "action": "office_attach_to_task",
        },
    ]

