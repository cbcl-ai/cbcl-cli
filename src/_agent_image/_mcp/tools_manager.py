"""Manager-role MCP tool definitions (split from mcp_tool_server.py).

Pure function: returns the JSON-RPC tool list a Manager session sees.
No side effects, no state — safe to import lazily or repeatedly.
"""
from __future__ import annotations

from .tools_plan import MANAGER_PLAN_TOOLS

# Collection READS in the Manager voice (ui-ux-aug19 D4.7 — Manager 46→48).
# The inputSchema + action are pulled by name from the worker pool so the
# definitions stay single-sourced (the _MA_BOARD_OPERATOR_EXTRAS precedent);
# only the description is re-voiced per role (the add_activity pattern —
# same action, role-tuned description). Backend-ungated reads.
_COLLECTION_READ_DESCRIPTIONS: dict[str, str] = {
    "get_collection": (
        "Read ONE office collection's schema: ordered field definitions "
        "(type, options, ref_to, required, help), schema_revision, and "
        "row count. Collections are the office's shared data tables (a "
        "services catalog, rate cards, deal manifests) that flows, "
        "scripts, and briefs reference. Use to understand a collection's "
        "shape before reading its rows with `query_rows` or before "
        "referencing it in a Brief. READ-ONLY research surface — schema "
        "and row changes are the Data Curator's consult surface, never "
        "yours."
    ),
    "query_rows": (
        "Read rows from an office collection — the office-local "
        "datastore on the USER'S machine (a live proxy read that errors "
        "honestly when the office daemon is offline; rows never live "
        "platform-side). Supports free-text `search`, exact-match AND "
        "`filter`, and limit/offset paging. Use to answer 'what did the "
        "script/flow save?' or to pull collection data into a Brief's "
        "Inputs. NOT a polling target — read once when the user asks or "
        "a Brief needs the data, never in a loop (scripts and flows "
        "report their own completions). Not a KB search (use `search_kb`) "
        "and not a file read (use `list_files`/`get_file`). READ-ONLY — "
        "row writes are the Data Curator's surface."
    ),
}


def _collection_read_tools() -> list[dict]:
    """The two collection reads, re-voiced for the Manager (D4.7)."""
    # Lazy import: tools_worker pulls the MA Board-Operator extras from
    # THIS module inside a function — mirror that to keep the import
    # relationship acyclic in both directions.
    from .tools_worker import get_worker_tools

    by_name = {t["name"]: t for t in get_worker_tools()}
    tools: list[dict] = []
    for name, description in _COLLECTION_READ_DESCRIPTIONS.items():
        tool = dict(by_name[name])
        tool["description"] = description
        tools.append(tool)
    return tools


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
                    "scope_id": {"type": "string", "description": "Filter to one scope's tasks (UUID). Use in Planner materialize to see which breakdown tasks already exist."},
                    "status": {"type": "string", "description": "Filter by status: backlog, ready, in_progress, blocked, review, done (comma-separated for multiple)"},
                    "assigned_agent": {"type": "string", "description": "Filter by agent name"},
                    "priority": {"type": "string", "description": "Filter by priority: urgent, high, medium, low"},
                    "limit": {"type": "number", "description": "Max tasks to return (default 100). The result includes a `truncated: true` flag when more exist — page with `offset` to see the rest."},
                    "offset": {"type": "number", "description": "Skip this many tasks (default 0). Use with `limit` to page a board over 100 tasks."},
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
                    "scope_id": {"type": "string", "description": "Scope UUID — scopes are PROGRAM MILESTONES: a milestone-scope normally holds ONE fat assignment (2-3 only on a genuine expert boundary). 2-5 related fat assignments ship as plain tasks chained with depends_on — no scope. A cohesive deliverable one agent can finish in a single session ships as ONE unscoped task — the DEFAULT for prototypes and one-sitting builds."},
                    "goal": {"type": "string", "description": "REQUIRED for Ready — the OUTCOME: what 'done' means, one sentence."},
                    "context": {"type": "string", "description": "OPTIONAL (Brief 2.0). Extra framing ONLY when it adds signal beyond the verbatim request in inputs — quote the user's own words instead of re-summarizing. Omit rather than pad."},
                    "inputs": {"type": "string", "description": "REQUIRED for Ready. Paste the user's ORIGINAL request VERBATIM (quoted, unedited) plus the exact path/URL of every user-provided reference — never paraphrase or summarize it. Add supporting files/links after the quote. Use 'None' only when the task has no upstream request."},
                    "output_format": {"type": "string", "description": "OPTIONAL (Brief 2.0). Name the expected artifact only when the shape isn't obvious from goal + acceptance criteria. Omit rather than pad."},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "description": "REQUIRED for Ready. ≤3-5 objectively checkable items (at least 1)."},
                    "allowed_tools": {"type": "array", "items": {"type": "string"}, "description": "Optional + ADVISORY only — a hint shown to the worker, NOT enforced (the agent's own config is the real tool boundary). Leave empty unless you have a specific reason to suggest a subset."},
                    "required_skills": {"type": "array", "items": {"type": "string"}, "description": "Optional. Required skills"},
                    "risks_and_edge_cases": {"type": "string", "description": "OPTIONAL (Brief 2.0). Known pitfalls worth a warning. Omit rather than write 'None'."},
                    "verification_steps": {"type": "string", "description": "REQUIRED for Ready — the REVIEW: how the reviewer checks the deliverable (smoke check for drafts/prototypes; audit steps for production)."},
                    "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Array of readable_ids (e.g. ['WR-003.T01']) that must reach 'done' before this task can move to Ready. REQUIRED when adding a task to a scope that is already Ready/Executing with active tasks — set it to the readable_id of the last incomplete task to preserve ordering."},
                    "effort_hint": {"type": "string", "enum": ["low", "medium", "high", "xhigh", "max", "ultracode"], "description": "Optional per-task effort sizing (pivot-1 T4). Set 'ultracode' for ANY fat cohesive build task — Tier 1b, AND Planner-materialized program tasks one expert finishes end-to-end in one sitting (the agent orchestrates its own sub-agents internally); omit for normal tasks (the agent's configured effort applies). Opus-tier agents only — ignored otherwise."},
                    "task_class": {"type": "string", "enum": ["ask", "assignment", "program", "op"], "description": "Assignment class (pivot-1 T5). 'ask' = Tier-0 lookup/check — SKIPS Review (the answer is the deliverable; the assignee or you close it straight to done). 'assignment' (default) = a normal fat task with a review gate. Scoped tasks auto-stamp 'program' regardless. 'op' = standing operation (incl. tasks YOU create as a standing REACTION to an inbound event stream — event hooks)."},
                },
                # Brief 2.0 (pivot-1 T3): the four-part assignment contract.
                # context / output_format / risks_and_edge_cases became
                # OPTIONAL — the verbatim request in ``inputs`` carries the
                # requirements; padding them forced paraphrase noise.
                "required": ["workstream_id", "title", "assigned_agent", "reviewer", "goal", "inputs", "acceptance_criteria", "verification_steps"],
            },
            "action": "create_task",
        },
        {
            "name": "consult_planner",
            "description": (
                "Engage the office Planner. ASYNC: returns immediately "
                "('engaged'); the Planner runs separately and messages you "
                "in chat when done. WHEN: consented PROGRAMS only (Tier 3 — "
                "the workstream runs in program work_mode). EXCEPTION: "
                "mode='specify' works in default mode too — drafting is "
                "free. If an execution mode is refused by the consent "
                "gate, the spec is not approved yet: draft it (specify) "
                "and get it APPROVED — the user's approval starts the "
                "program (in manager-approval workstreams run "
                "ask_user_choice(kind='execution_mode') first; you never "
                "self-consent). NEVER surface the refusal error to the "
                "user. WHEN NOT: a one-sitting build "
                "is ONE fat task (Tier 1b); 2-5 related fat assignments "
                "are a depends_on chain; a SINGLE-scope body of work skips "
                "straight to a materialize consult on the scope you open "
                "(the SINGLE-SCOPE COLLAPSE — no specify/scope_plan) — or "
                "author the tasks yourself. Each consult is a separate "
                "async session costing many minutes. "
                "Modes: 'specify' (draft/revise the workstream SPEC + its "
                "MILESTONES section — the requirements contract AND the "
                "ordered scope checklist in ONE artifact; must be APPROVED "
                "before scopes are planned — Tier-3 STARTS here; who approves "
                "depends on the workstream's spec-approval mode: user-mode = "
                "the USER approves in the UI, manager-mode = YOU review + "
                "call approve_spec), "
                "'scope_plan' (write the SKELETON plan onto an existing scope "
                "— task titles + intents + deps + chips, NO task rows yet; "
                "for large or uncertain scopes), "
                "'materialize' (author the scope's tasks with full briefs — "
                "writes its own short plan first when none exists; never "
                "creates the scope, never activates; BOTH scope_plan and "
                "materialize are refused while the spec is an unapproved "
                "draft), "
                "'research' (investigate a question), "
                "'verify' (verify a completed scope before the next starts). "
                "Multi-milestone flow: specify -> approved (per mode) -> YOU "
                "create_scope for the next milestone -> scope_plan -> "
                "materialize -> review -> YOU activate_scope."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workstream_id": {"type": "string", "description": "REQUIRED. Workstream UUID."},
                    "objective": {"type": "string", "description": "REQUIRED. What the Planner must produce. Include the user's original request VERBATIM (quoted) and the exact paths/URLs of every attached reference — the Planner sees only what you pass here; a paraphrase loses requirements."},
                    "mode": {"type": "string", "enum": ["specify", "scope_plan", "materialize", "research", "verify"], "description": "specify (spec + MILESTONES — the roadmap lives in the spec now) | scope_plan | materialize | research | verify. Default specify."},
                    "scope_id": {"type": "string", "description": "Scope UUID — REQUIRED for scope_plan / materialize / verify modes."},
                },
                "required": ["workstream_id", "objective"],
            },
            "action": "consult_planner",
        },
        {
            "name": "ask_user_choice",
            "description": (
                "Ask the USER a multiple-choice question in chat — a "
                "question bubble with 2-4 one-click option buttons. Use "
                "ONLY when a genuine decision needs the user (a tradeoff "
                "only they can make); never for anything you can resolve "
                "yourself from the board, KB, or files. Asking ENDS your "
                "turn — the answer arrives as the user's next message "
                "(\"Selected: {label}\") in a NEW turn; never poll or "
                "wait for it. At most one open question per conversation "
                "(a new ask supersedes the old), and any free-text user "
                "message supersedes it too — honor the text, do not "
                "re-ask. Not available in General Chat. Option key "
                "'own_workstream' (valid on execution_mode questions "
                "ONLY) offers a program in its own NEW workstream — "
                "include it only together with proposed_workstream_name; "
                "the backend creates that workstream from the user's "
                "click and moves the request there, never you. "
                "Kind 'intake' is the multi-question INTAKE CARD: pass "
                "'questions' (2-4, each with its own option chips and/or "
                "a free-text 'other') INSTEAD of top-level options — one "
                "card, ONE submit; all answers arrive together in one "
                "reply message. Use it ONLY for genuine unknowns that "
                "change WHAT gets built — never process questions; a "
                "complete request gets zero questions. No side effects: "
                "the answers inform your next turn, nothing else. "
                "Intake extras (ALL intake-only — forbidden on other "
                "kinds): `topic` (REQUIRED — the durable intake record "
                "files under it), `derived_values` (what you already "
                "derived; the user confirms instead of typing — derive "
                "first, ask second), per-question `multi` "
                "(+ `min_select`/`max_select`), per-option "
                "`requires_input` (the option demands a short text when "
                "selected). "
                "Kind 'hire_agent' is the HIRE consent card (only when "
                "the roster audit finds NO fitting profile): pass "
                "'proposed_agent' (the drafted profile) with EXACTLY "
                "two options keyed 'hire' then 'not_now'; the user's "
                "Hire click makes the BACKEND generate and create the "
                "agent — NEVER you (the [Hired] note lands in chat when "
                "it's done; declined → use the closest existing "
                "profile). "
                "Kind 'run_flow' is the RUN-A-FLOW consent card (Flow "
                "Studio): when the request matches an ENABLED runnable "
                "flow's trigger, pass 'flow_name' (the flow's slug) "
                "with EXACTLY two options keyed 'run' then 'not_now' "
                "(+ optional 'derived_preview' — inputs you already "
                "derived); the user's Run click makes the BACKEND "
                "start the run — NEVER you (the run's cards and chips "
                "land in chat as it advances; declined → classify on "
                "the normal ladder). Only an EXPLICIT user ask ('run "
                "the presale flow on this') skips the card — call "
                "start_flow_run directly then."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 500,
                        "description": "REQUIRED. The question, in plain human words — it is also the chat bubble text.",
                    },
                    "options": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 4,
                        "description": "REQUIRED for kind 'informational'/'execution_mode' — 2-4 options, each a one-click answer; keys unique within the question. FORBIDDEN for kind='intake' (each intake question carries its OWN options).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {
                                    "type": "string",
                                    "pattern": "^[a-z][a-z0-9_]{0,31}$",
                                    "description": "Stable snake_case identifier, max 32 chars (e.g. 'big_assignment'). Unique per question.",
                                },
                                "label": {
                                    "type": "string",
                                    "maxLength": 80,
                                    "description": "The button text the user clicks (max 80 chars).",
                                },
                                "description": {
                                    "type": "string",
                                    "maxLength": 200,
                                    "description": "One line stating the option's tradeoff in human words (max 200 chars).",
                                },
                            },
                            "required": ["key", "label", "description"],
                        },
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["informational", "execution_mode", "intake", "hire_agent", "run_flow"],
                        "description": "Question kind. Default 'informational' — the answer just informs your next turn. 'execution_mode' = the program-boundary consent ask: the backend applies the user's click itself (selecting 'program' unlocks the program machinery for the workstream) BEFORE your reply turn — you never set or change the mode yourself. 'intake' = the multi-question intake card (pass 'questions' instead of 'options'): informational-class, no side effects — the answers arrive together as one reply. 'hire_agent' = the hire consent card (pass 'proposed_agent' + exactly the 'hire'/'not_now' options): the user's Hire click makes the backend generate + create the agent — never you. 'run_flow' = the run-a-flow consent card (pass 'flow_name' + exactly the 'run'/'not_now' options): the user's Run click makes the backend start the flow run — never you.",
                    },
                    # Pivot-4 flow-intake (spec §A): topic + derived_values +
                    # per-question multi bounds + per-option attached input.
                    # All intake-only; the backend enforces the per-kind
                    # coupling and every cap with teaching errors.
                    "topic": {
                        "type": "string",
                        "pattern": "^[a-z][a-z0-9-]{1,39}$",
                        "description": "REQUIRED for kind='intake'; FORBIDDEN for every other kind. Stable kebab-case noun naming WHAT the card collects (e.g. 'quote-inputs', 'campaign-brief') — the durable intake record files under it: re-asking the same topic in a workstream SUPERSEDES the old record, and `amend_intake` addresses records by it. When running a registered flow, reuse a topic from that flow's `intake_topics`.",
                    },
                    "derived_values": {
                        "type": "array",
                        "maxItems": 12,
                        "description": "Optional, kind='intake' only: the display-only 'what I understood' panel — up to 12 {label, value} rows of inputs you ALREADY derived (from the request, source files, KB, prior records). Derive first, ask second: showing derivations shrinks the question list to genuine unknowns, and the user CONFIRMS instead of typing (a wrong row gets corrected in free text). Display only — no reply shape.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 80,
                                    "description": "What the row names (max 80 chars).",
                                },
                                "value": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 300,
                                    "description": "The derived value, human-readable (max 300 chars).",
                                },
                            },
                            "required": ["label", "value"],
                        },
                    },
                    "questions": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 4,
                        "description": "REQUIRED for kind='intake'; forbidden otherwise. 2-4 questions rendered as ONE card with ONE submit. Each question offers 0-16 one-click chips and/or a free-text 'other' field (at least one of the two) — the wide chip budget exists for set-shaped `multi` questions ('which of these 14 services?'); keep single-pick questions to a handful of chips.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {
                                    "type": "string",
                                    "pattern": "^[a-z][a-z0-9_]{0,31}$",
                                    "description": "Stable snake_case identifier, unique across the card's questions (e.g. 'audience', 'deploy_target') — the reply's answers object is keyed by it.",
                                },
                                "text": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 300,
                                    "description": "The question, in plain human words (max 300 chars).",
                                },
                                "multi": {
                                    "type": "boolean",
                                    "description": "Default false. true = the user may select SEVERAL chips (this question's reply arrives as an array). Use for set-shaped decisions ('which services?', 'which doc sets?'); leave false for an either-or pick.",
                                },
                                "min_select": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": "Optional, multi questions only: minimum selections (1..options length). Omit when any number is fine.",
                                },
                                "max_select": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": "Optional, multi questions only: maximum selections (1..options length, ≥ min_select). Omit when any number is fine.",
                                },
                                "options": {
                                    "type": "array",
                                    "minItems": 0,
                                    "maxItems": 16,
                                    "description": "0-16 predefined answers rendered as one-click chips (the full budget is for `multi` set-selections; single-pick questions read best with ≤4). Keys unique within the question.",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "key": {
                                                "type": "string",
                                                "pattern": "^[a-z][a-z0-9_]{0,31}$",
                                                "description": "Stable snake_case identifier, unique within the question.",
                                            },
                                            "label": {
                                                "type": "string",
                                                "maxLength": 80,
                                                "description": "The chip text the user clicks (max 80 chars).",
                                            },
                                            "requires_input": {
                                                "type": "object",
                                                "description": "Intake only, optional: selecting this chip (single OR multi) REQUIRES a short text from the user — the ONE in-card conditional ('Other vendor — name it'). Use ONLY when the option is meaningless without its detail; any conditional beyond one attached input is a follow-up ROUND (a later card), never more card logic.",
                                                "properties": {
                                                    "label": {
                                                        "type": "string",
                                                        "minLength": 1,
                                                        "maxLength": 80,
                                                        "description": "Label over the revealed text field (max 80 chars).",
                                                    },
                                                    "placeholder": {
                                                        "type": "string",
                                                        "maxLength": 120,
                                                        "description": "Optional placeholder hint (max 120 chars).",
                                                    },
                                                },
                                                "required": ["label"],
                                            },
                                        },
                                        "required": ["key", "label"],
                                    },
                                },
                                "allow_other": {
                                    "type": "boolean",
                                    "description": "Default true — offer a free-text 'Other…' field beside the chips. A question must offer at least one of: options, allow_other.",
                                },
                            },
                            "required": ["key", "text"],
                        },
                    },
                    "proposed_workstream_name": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 100,
                        "description": "Name for the NEW workstream an 'own_workstream' option proposes (short, human, 2-4 words — the project's name, not a sentence). REQUIRED whenever an option with key 'own_workstream' is included; omit otherwise. On the user's click the BACKEND creates the workstream from this name and re-dispatches the request there — you never create workstreams yourself.",
                    },
                    "proposed_agent": {
                        "type": "object",
                        "description": "REQUIRED for kind='hire_agent'; forbidden otherwise. The drafted profile the user consents to — the backend generates + creates the agent from it on the Hire click.",
                        "properties": {
                            "name": {
                                "type": "string",
                                "pattern": "^[a-z][a-z0-9-]{1,63}$",
                                "description": "New lowercase-hyphenated agent slug (must not collide with the roster or a system agent).",
                            },
                            "display_name": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 80,
                                "description": "Human name the card and roster show.",
                            },
                            "ownership": {
                                "type": "string",
                                "maxLength": 700,
                                "description": "The ownership statement — MUST open '<Function label> — ' (governance style), then 2-4 sentences: what it OWNS, its boundary, and the reason it earns its seat (context / keys / review separation / cost tier). Becomes role_description verbatim.",
                            },
                            "preset": {
                                "type": "string",
                                "enum": ["doer", "specialist", "responder"],
                                "description": "Role shape → model+effort: doer=Opus/ultracode, specialist=Opus/xhigh, responder=Sonnet (no effort).",
                            },
                            "skill_names": {
                                "type": "array",
                                "maxItems": 2,
                                "items": {"type": "string", "pattern": "^[a-z][a-z0-9-]{1,63}$"},
                                "description": "0-2 SOP skill slugs the generation should author for this agent.",
                            },
                            "reason": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 300,
                                "description": "ONE sentence: why the roster audit failed (the card shows it).",
                            },
                        },
                        "required": ["name", "display_name", "ownership", "preset", "reason"],
                    },
                    # Flow Studio (FS-P2.T9, spec §7.1): the run_flow
                    # consent card's payload — both run_flow-only; the
                    # backend enforces the per-kind coupling with
                    # teaching errors (the proposed_agent posture).
                    "flow_name": {
                        "type": "string",
                        "pattern": "^[a-z][a-z0-9-]{1,63}$",
                        "description": "REQUIRED for kind='run_flow'; forbidden otherwise. Slug of the ENABLED runnable flow to propose (from the '## Office flows' context — runnable flows are marked there). Validated backend-side: unknown / disabled / prose-only flows are refused at ask time.",
                    },
                    "derived_preview": {
                        "type": "array",
                        "maxItems": 12,
                        "description": "Optional, kind='run_flow' only: up to 12 {label, value} rows of run inputs you ALREADY derived from the request/files — the card shows them so the user consents to a concrete run, not a mystery. Display only.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 80,
                                    "description": "What the row names (max 80 chars).",
                                },
                                "value": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 300,
                                    "description": "The derived value, human-readable (max 300 chars).",
                                },
                            },
                            "required": ["label", "value"],
                        },
                    },
                },
                # Pivot-3 P1-6: ``options`` left the required set because
                # kind='intake' FORBIDS it (questions replace it) — JSON
                # Schema here carries no conditionals by convention; the
                # backend enforces the per-kind coupling with teaching
                # errors (options required for the one-click kinds,
                # questions required for intake, never both).
                "required": ["question"],
            },
            "action": "ask_user_choice",
        },
        # ── Flows & intake records (pivot-4 flow-intake, spec §B/§C) ────
        # Intake answers persist as durable, revisable RECORDS; flows are
        # first-class office workflow definitions. All three tools are
        # Manager/MA-gated backend-side and excluded from the Planner
        # catalog + the worker pool.
        {
            "name": "amend_intake",
            "description": (
                "Amend ONE answer field of an ANSWERED intake record — "
                "the user changed a single decision ('actually make it "
                "CAP-3'): the field updates, the rest keeps, and the "
                "record's revisions list carries the audit trail. "
                "Address the record by workstream + topic (newest "
                "record of that topic) or by exact record_id. "
                "WHEN NOT TO USE: an OPEN record (its card still awaits "
                "the user) — the user answers the open card instead; "
                "never amend it. A change touching MOST of the answers "
                "is a fresh intake card (same topic — it supersedes the "
                "old record), not a string of amendments. `field` must "
                "be one of the record's question keys — unknown fields "
                "are refused with a teaching error. FLOW RUNS: a value "
                "change inside a flow run ('actually the country is "
                "DE') additionally passes `flow_run_id` — the run's "
                "manifest updates too (both stores in one call; for a "
                "non-intake manifest value pass flow_run_id WITHOUT "
                "topic/record_id and `field` is the manifest path). "
                "Completed blocks are NOT re-run — state what the "
                "amendment affects."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workstream_id": {"type": "string", "description": "Workstream UUID the record lives in. REQUIRED with `topic` (topic resolution is per-workstream); IGNORED with `record_id` (the record already carries its workstream)."},
                    "topic": {"type": "string", "description": "Topic of the record to amend (resolves to the newest answered record of that topic in the workstream — requires `workstream_id`). Pass topic OR record_id."},
                    "record_id": {"type": "string", "description": "Exact intake-record UUID (from the workstream's intake/ index or REST). Pass topic OR record_id; with record_id no workstream_id is needed."},
                    "flow_run_id": {"type": "string", "description": "Optional (Flow Studio): a flow run's UUID or readable id (e.g. 'WR-003.F02') — the amendment ALSO lands in that run's manifest (amend:<n> provenance). With topic/record_id both stores update in one transaction; without them `field` is the manifest path (dotted paths allowed) and only the manifest updates."},
                    "field": {"type": "string", "description": "REQUIRED. The question key whose answer changes — must exist on the record."},
                    "new_value": {"description": "REQUIRED. The new answer, in the same shapes reply answers use: a string (option key or free text), {key, input} for an attached-input option, or an array of those for a multi question."},
                    "reason": {"type": "string", "description": "Optional one-line reason, recorded in the revision entry."},
                },
                # Program review #14: workstream_id left the required set —
                # it is required only for topic-targeting (MCP JSON Schema
                # carries no conditional requireds by convention; the
                # backend enforces the topic↔workstream_id coupling with a
                # teaching error, and the record_id path ignores it).
                "required": ["field", "new_value"],
            },
            "action": "amend_intake",
        },
        {
            "name": "define_flow",
            "description": (
                "Register a NEW office flow — a first-class workflow "
                "definition (trigger, required inputs split "
                "derivable/askable, intake topics, steps, outputs) for "
                "work the office runs repeatedly. USER CONSENT FIRST — "
                "registering a flow is a structural change to how the "
                "office works: PROPOSE it in chat (name + trigger + "
                "steps in two lines) and call this ONLY after the user "
                "agrees or explicitly asked for it. NEVER define a flow "
                "silently mid-turn. WHEN NOT TO USE: one-off work "
                "(route the tasks and move on); a variation of an "
                "existing flow (`update_flow` it instead); a pattern "
                "you have seen once (flows are for RECURRING work); a "
                "RUNNABLE flow (one with an engine graph) — that is "
                "designed in the Flow Studio by the Flow Architect, "
                "never through this tool. A PROSE flow guides your "
                "routing and intake — it executes nothing; the board "
                "stays the execution substrate. Runnable flows DO "
                "execute (the engine runs their graph)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "pattern": "^[a-z][a-z0-9-]{1,63}$", "description": "REQUIRED. Kebab-case slug, unique in the office (doubles as the workspace filename flows/<name>.md)."},
                    "display_name": {"type": "string", "minLength": 1, "maxLength": 120, "description": "REQUIRED. Human name (max 120 chars)."},
                    "description": {"type": "string", "maxLength": 500, "description": "One-sentence description of the flow (max 500 chars)."},
                    "trigger": {"type": "string", "minLength": 1, "maxLength": 300, "description": "REQUIRED. The arriving event/request that starts a run, stated concretely (max 300 chars — e.g. 'user asks for a quote')."},
                    "required_inputs": {
                        "type": "array",
                        "maxItems": 20,
                        "description": "Every input a run needs, split honestly: derivable=true + `from` naming the source (a source file, KB doc, record topic) for anything you can compute WITHOUT asking; derivable=false for genuine user-only decisions — those become the flow's intake questions.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Input name."},
                                "derivable": {"type": "boolean", "description": "true = compute it from `from` instead of asking."},
                                "from": {"type": "string", "maxLength": 200, "description": "Where a derivable input comes from (max 200 chars)."},
                            },
                            "required": ["name"],
                        },
                    },
                    "intake_topics": {"type": "array", "maxItems": 10, "items": {"type": "string"}, "description": "Kebab-case intake topics this flow's cards use (e.g. 'quote-inputs') — one topic per card-worth of askable decisions."},
                    "steps": {
                        "type": "array",
                        "maxItems": 15,
                        "description": "REQUIRED. The end-to-end run as ordered steps.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "minLength": 1, "maxLength": 120, "description": "Step title (max 120 chars)."},
                                "owner_hint": {"type": "string", "maxLength": 64, "description": "Roster agent slug that usually owns the step (max 64 chars)."},
                                "notes": {"type": "string", "maxLength": 300, "description": "One-line step notes (max 300 chars)."},
                            },
                            "required": ["title"],
                        },
                    },
                    "outputs": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 200}, "description": "The artifacts a run delivers (max 10, each ≤200 chars)."},
                    "adjustment_notes": {"type": "string", "maxLength": 500, "description": "The user's standing adjustments field — usually empty at creation (max 500 chars)."},
                },
                "required": ["name", "display_name", "trigger", "steps"],
            },
            "action": "define_flow",
        },
        {
            "name": "update_flow",
            "description": (
                "PATCH an existing office flow by name — pass ONLY the "
                "fields that change; each supplied definition field "
                "REPLACES that field whole (arrays are not merged "
                "item-wise), unsupplied fields keep, and the revision "
                "bumps. STRUCTURAL changes (trigger, steps, "
                "required_inputs, outputs) take the same "
                "USER-CONSENT-FIRST posture as define_flow — propose in "
                "chat, then call; bookkeeping edits (adjustment_notes, "
                "a step's notes) are fine directly. WHEN NOT TO USE: "
                "a RUNNABLE flow's shape (trigger/steps/inputs of a "
                "flow with an engine graph) — that is the Flow "
                "Studio's design surface (the Flow Architect); "
                "patching its prose here desyncs it from the graph "
                "the engine actually runs. Also not for: turning it "
                "into a DIFFERENT workflow — that's a new define_flow "
                "(the user retires the old one); one run's "
                "peculiarity — that's task guidance, not a flow edit."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "REQUIRED. Slug of the flow to update."},
                    "display_name": {"type": "string", "minLength": 1, "maxLength": 120, "description": "New human name."},
                    "description": {"type": "string", "maxLength": 500, "description": "New one-sentence description."},
                    "trigger": {"type": "string", "minLength": 1, "maxLength": 300, "description": "New trigger (structural — consent first)."},
                    "required_inputs": {
                        "type": "array",
                        "maxItems": 20,
                        "description": "Replacement required-inputs list (structural — consent first). Same shape as define_flow.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Input name."},
                                "derivable": {"type": "boolean", "description": "true = compute it from `from` instead of asking."},
                                "from": {"type": "string", "maxLength": 200, "description": "Where a derivable input comes from (max 200 chars)."},
                            },
                            "required": ["name"],
                        },
                    },
                    "intake_topics": {"type": "array", "maxItems": 10, "items": {"type": "string"}, "description": "Replacement intake-topics list."},
                    "steps": {
                        "type": "array",
                        "maxItems": 15,
                        "description": "Replacement steps list (structural — consent first). Same shape as define_flow.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "minLength": 1, "maxLength": 120, "description": "Step title (max 120 chars)."},
                                "owner_hint": {"type": "string", "maxLength": 64, "description": "Roster agent slug that usually owns the step (max 64 chars)."},
                                "notes": {"type": "string", "maxLength": 300, "description": "One-line step notes (max 300 chars)."},
                            },
                            "required": ["title"],
                        },
                    },
                    "outputs": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 200}, "description": "Replacement outputs list (structural — consent first)."},
                    "adjustment_notes": {"type": "string", "maxLength": 500, "description": "New adjustment notes (bookkeeping — no consent needed)."},
                },
                "required": ["name"],
            },
            "action": "update_flow",
        },
        # ── Flow runs (Flow Studio, FS-P2.T9 / spec §7.2) ───────────────
        # The Manager OPERATES runs (start / stop / status) — it never
        # edits flow definitions or graphs (flow design belongs to the
        # design surface / the Flow Architect). All three are
        # Manager/MA-gated backend-side and Planner-excluded; the two
        # writes are stripped in General Chat (get_flow_run stays).
        {
            "name": "start_flow_run",
            "description": (
                "Start a run of an ENABLED runnable flow (a Flow Studio "
                "graph flow) in a workstream — the deterministic engine "
                "executes it block by block; its cards, tasks, and "
                "documents post themselves as it advances. Use ONLY on "
                "an EXPLICIT user ask ('run the presale flow on this') "
                "or after the user clicked Run on your run_flow consent "
                "card and the start needs a retry — the NORMAL path is "
                "proposing via ask_user_choice(kind='run_flow'); the "
                "user's Run click starts the run without you. WHEN NOT "
                "TO USE: never start a run the user didn't ask for or "
                "consent to; not for prose flows (no graph — they guide "
                "YOUR routing, there is nothing to execute); not to "
                "resume a paused run (the user resumes from the run "
                "view). One run per workstream runs at a time — an "
                "extra start QUEUES and auto-promotes when the slot "
                "frees. Starting is ASYNC: report that the run started "
                "and end your turn — never poll it."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "flow_name": {"type": "string", "description": "REQUIRED. Slug of the enabled runnable flow (see '## Office flows' in your context — runnable flows are marked)."},
                    "workstream_id": {"type": "string", "description": "REQUIRED. Workstream UUID the run rides in (runs live in ONE workstream's chat)."},
                    "inputs": {"type": "object", "description": "Optional {name: value} map seeding the run's manifest (values the user already gave in chat — the run's collect blocks then skip asking for them)."},
                },
                "required": ["flow_name", "workstream_id"],
            },
            "action": "start_flow_run",
        },
        {
            "name": "stop_flow_run",
            "description": (
                "Stop a flow run: archives the run's open board tasks, "
                "keeps its manifest for post-mortem/re-runs, frees the "
                "workstream's run slot (the oldest queued run "
                "auto-promotes), and posts the stopped chip in chat. "
                "Use when the user asks to stop/cancel a run. WHEN NOT "
                "TO USE: not for a temporary hold — pausing is the "
                "user's affordance in the run view, not yours; not to "
                "'restart' a run (stop + start mints a NEW run with a "
                "fresh manifest — say so before doing it); already-"
                "finished runs are an idempotent no-op."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "REQUIRED. Run UUID or readable id (e.g. 'WR-003.F02' — from the run's chat card or get_flow_run)."},
                    "reason": {"type": "string", "description": "Optional one-line reason — recorded on the run and shown in the stopped chip."},
                },
                "required": ["run_id"],
            },
            "action": "stop_flow_run",
        },
        {
            "name": "get_flow_run",
            "description": (
                "Read one flow run's live state: status, the blocks it "
                "is on (and what each waits for — a card, tasks, the "
                "daemon, a timer), the manifest's collected values with "
                "cost, and any error. Your context refresher for 'how "
                "is the run going?' and the pre-check before "
                "stop_flow_run. WHEN NOT TO USE: not a polling target — "
                "runs report themselves in chat (cards + chips); read "
                "it when the USER asks or a decision needs run state, "
                "then answer from it."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "REQUIRED. Run UUID or readable id (e.g. 'WR-003.F02')."},
                },
                "required": ["run_id"],
            },
            "action": "get_flow_run",
        },
        {
            "name": "create_scope",
            "description": (
                "Create a Scope (planning container for related tasks). "
                "Starts in `preparing`. Order: create_scope → "
                "create_task(scope_id=…) × N → activate_scope. Max ONE "
                "live scope per workstream "
                "(preparing/ready/executing/verifying) — the next opens "
                "only after the current is done/archived; future scopes "
                "stay as spec milestones. Skip whenever the work "
                "fits 1-3 tasks or one agent session — create unscoped "
                "task(s) directly. Scopes add planning + verification "
                "wall-clock."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workstream_id": {"type": "string", "description": "REQUIRED. Workstream UUID."},
                    "name": {"type": "string", "description": "REQUIRED. Descriptive name (e.g. 'User Authentication Epic')."},
                    "description": {"type": "string", "description": "Optional long-form description of the scope."},
                    "short_key": {"type": "string", "description": "1-2 word key for UI display (e.g. 'Auth'). Max 30 chars. For a milestone's scope this MUST equal the milestone key exactly — the match links scope↔milestone (ticks the Spec panel and arms the coverage gate)."},
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
                    "state": {"type": "string", "description": "Filter by state (comma-separated for multiple): preparing, ready, executing, verifying, done, archived."},
                },
            },
            "action": "list_scopes",
        },
        {
            "name": "get_scope",
            "description": "Get full details of a single scope including aggregate task counts. NOT for enumerating scopes (use `list_scopes`) or reading the scope's plan (use `get_execution_plan`).",
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
                    "assigned_agent": {"type": "string", "description": "Reassign the task to a different agent SLUG (from the roster). Clearing (empty string) works ONLY while the task is in Backlog; from Ready onward the executor is pinned (no-unassign-after-Ready) and a clear is silently IGNORED — reassign to another agent instead of clearing."},
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
                        # AIQ fix 12 (2026-07-29): "backlog" removed — no
                        # *→backlog edge exists in VALID_TRANSITIONS (backlog
                        # is a source-only status), so offering it invited a
                        # guaranteed-reject round-trip. Pinned by
                        # evals/test_prompt_transitions_legal.py.
                        "enum": ["ready", "in_progress", "blocked", "review", "done", "archived"],
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
                "Approval side effects: ``create_task`` creates the "
                "task; approving an ``escalate_blocker`` / "
                "``request_clarification`` / ``setup_office_secret`` "
                "whose source task is currently blocked auto-promotes "
                "it back to ready (never ALSO move_task it); every "
                "other type only records the decision — the auto-decide "
                "turn's guidance row states your follow-up. Reject "
                "closes the row with no effect (``decision_notes`` = "
                "why; for ``request_clarification`` approvals "
                "``decision_notes`` IS the answer posted to the source "
                "task). Do not use for ``requires_user=True`` requests "
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
        # ── Standing operations (pivot-3 P2-2, D3.3/D3.5) ──────────────
        # Backend-owned assignment_schedules: due → a REAL op-class task on
        # the normal rails (or a scheduled Manager digest turn); overlap-skip
        # while the prior run is non-terminal. Manager/MA-gated backend-side.
        {
            "name": "schedule_assignment",
            "description": (
                "Create a STANDING OPERATION: a cron-cadence schedule the "
                "backend sweeps — each due run mints a REAL `op`-class task "
                "on the normal rails (dispatch → review → Inbox), "
                "overlap-skipped while the prior run is still open. A "
                "scheduled assignment is for RECURRING WORK WITH JUDGMENT "
                "(daily content, weekly summaries, periodic reviews, "
                "support-queue passes). NOT for one-off work — create a "
                "task; NOT for pure mechanical batch jobs — scripts "
                "(`schedule_script`, the ASD builds them) are cheaper and "
                "need no judgment. kind='manager_digest' is the exception "
                "shape: a scheduled turn of YOURS that reports to the user "
                "in chat (yesterday / today / blocked / awaiting-you) — "
                "pass `prompt` instead of agent + brief_template; ONE "
                "digest per office, never spam-create them. Call "
                "`list_assignment_schedules` first to avoid duplicates."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "REQUIRED. Short human name for the schedule (e.g. 'Daily campaign content', 'Weekly digest')."},
                    "workstream_id": {"type": "string", "description": "REQUIRED. Workstream UUID the minted runs belong to."},
                    "kind": {
                        "type": "string",
                        "enum": ["agent_task", "manager_digest"],
                        "description": "REQUIRED. 'agent_task' = each due run mints an op-class task from brief_template for `agent`. 'manager_digest' = each due run is a scheduled turn of YOURS that reports to the user in chat — no agent, no brief_template, just `prompt`.",
                    },
                    "cron_expr": {
                        "type": "string",
                        "description": "REQUIRED. 5-field cron ('0 9 * * 1-5' = 09:00 weekdays; '30 8 * * 1' = Mondays 08:30) or a special: @daily, @weekly.",
                    },
                    "agent": {"type": "string", "description": "REQUIRED for kind='agent_task' — the executing agent slug from the roster. Omit for manager_digest."},
                    "reviewer": {"type": "string", "description": "Optional reviewer agent slug for minted runs — must differ from `agent`. Omit for manager_digest."},
                    "brief_template": {
                        "type": "object",
                        "description": (
                            "REQUIRED for kind='agent_task' (forbidden for manager_digest). "
                            "The four-part contract every minted run's brief is stamped "
                            "from — same bar as create_task: goal = the OUTCOME; inputs = "
                            "the user's standing request VERBATIM (quoted, unedited) plus "
                            "every reference path/URL — never paraphrase; "
                            "acceptance_criteria = ≤3-5 objectively checkable items; "
                            "verification_steps = how the reviewer checks each run. "
                            "autonomy_note carries the POLICY: what the op may do WITHOUT "
                            "asking (from the approved spec / policy skill) — anything "
                            "outside it escalates to the Inbox."
                        ),
                        "properties": {
                            "title": {"type": "string", "description": "REQUIRED. Title stamped on each minted run."},
                            "goal": {"type": "string", "description": "REQUIRED. The OUTCOME of one run, one sentence."},
                            "inputs": {"type": "string", "description": "REQUIRED. The user's standing request VERBATIM + reference paths/URLs."},
                            "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "description": "REQUIRED. ≤3-5 objectively checkable items (at least 1)."},
                            "verification_steps": {"type": "string", "description": "REQUIRED. How the reviewer checks one run's deliverable."},
                            "context": {"type": "string", "description": "Optional extra framing beyond the verbatim request. Omit rather than pad."},
                            "autonomy_note": {"type": "string", "description": "Optional POLICY line: what this op may do WITHOUT asking; outside-policy work escalates to the Inbox."},
                        },
                        "required": ["title", "goal", "inputs", "acceptance_criteria", "verification_steps"],
                    },
                    "prompt": {"type": "string", "description": "REQUIRED for kind='manager_digest' (forbidden for agent_task) — the digest turn's instruction: what to summarize and report in chat."},
                    "is_active": {"type": "boolean", "description": "Default true. Pass false to create paused."},
                },
                "required": ["name", "workstream_id", "kind", "cron_expr"],
            },
            "action": "schedule_assignment",
        },
        {
            "name": "update_assignment_schedule",
            "description": (
                "Update a standing assignment schedule in place — partial "
                "fields: pause/resume (`is_active`), cadence (`cron_expr`), "
                "agent/reviewer, the brief_template or digest prompt. "
                "Pausing keeps the cadence + template for a later resume. "
                "NOT for one run's outcome — that lives on the minted op "
                "task (Activity/review), not the schedule; NOT for retiring "
                "it (that's `delete_assignment_schedule`)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "schedule_id": {"type": "string", "description": "REQUIRED. Schedule UUID (from list_assignment_schedules)."},
                    "name": {"type": "string", "description": "New schedule name."},
                    "cron_expr": {"type": "string", "description": "New cadence — 5-field cron or @daily / @weekly."},
                    "agent": {"type": "string", "description": "New executing agent slug (agent_task schedules)."},
                    "reviewer": {"type": "string", "description": "New reviewer slug — must differ from agent."},
                    "brief_template": {"type": "object", "description": "Replacement brief template (agent_task schedules) — same four-part contract + autonomy_note as schedule_assignment; REPLACES the stored template whole."},
                    "prompt": {"type": "string", "description": "Replacement digest instruction (manager_digest schedules)."},
                    "is_active": {"type": "boolean", "description": "false pauses the schedule (no new runs); true resumes it."},
                },
                "required": ["schedule_id"],
            },
            "action": "update_assignment_schedule",
        },
        {
            "name": "delete_assignment_schedule",
            "description": (
                "Delete a standing assignment schedule permanently — future "
                "runs stop; already-minted op tasks are untouched. Use only "
                "when the operation is retired for good. NOT for a pause — "
                "`update_assignment_schedule(is_active=false)` keeps the "
                "cadence + brief_template for a later resume."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "schedule_id": {"type": "string", "description": "REQUIRED. Schedule UUID."},
                },
                "required": ["schedule_id"],
            },
            "action": "delete_assignment_schedule",
        },
        {
            "name": "list_assignment_schedules",
            "description": (
                "List the office's standing assignment schedules — name, "
                "kind, cadence, agent, active state, last run. Call BEFORE "
                "`schedule_assignment` to avoid duplicates (especially "
                "digests — one per office) and to answer 'what runs on a "
                "schedule?'. NOT for script crons — those live on the "
                "Scripts page (the ASD manages them)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workstream_id": {"type": "string", "description": "Optional filter — only this workstream's schedules."},
                },
            },
            "action": "list_assignment_schedules",
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
                            "Optional filter. ``system`` = the eight "
                            "built-in agents (Analyst, Automation "
                            "Script Developer, Auditor, Builder, "
                            "Manager Assistant, and the consult-only "
                            "Planner, Flow Architect, and Data "
                            "Curator). "
                            "``custom`` = user-defined domain agents. "
                            "Omit to list both."
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
                "index (newest first). Use BEFORE delegating new work to "
                "check whether a similar deliverable already exists (e.g. "
                "an Analyst report from last week), and to find input "
                "files to reference in a new Brief. Filters: `tags` "
                "(AND-match — only files carrying EVERY listed tag), "
                "`source_agent` (exact agent name), `limit`. Do not use "
                "to read raw file content — pair with `get_file` for that."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "AND-filter: return only files carrying EVERY tag in this list."},
                    "source_agent": {"type": "string", "description": "Filter to files written by this exact agent name (e.g. 'analyst')."},
                    "limit": {"type": "integer", "description": "Max rows returned (default 20, hard cap 100 — pass limit explicitly when you need more than the first 20)."},
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
        # Collection READS (ui-ux-aug19 D4.7 — Manager 46→48): the "what
        # did the script save?" leg of webhook→script→collection→Manager.
        # Schemas single-sourced from the worker pool; descriptions
        # Manager-voiced. Both names join the Planner exclusion set (the
        # v1 Planner-no-collection-reads pin stays green).
        *_collection_read_tools(),
        # Execution-Plan reads + close-verification. The Manager reviews the
        # Planner's skeleton (get_execution_plan) + spec/milestones
        # (get_spec) and closes a scope's verification
        # (complete_scope_verification) — incl. the stuck case where the
        # Planner verified PASS but couldn't close it. (The workstream-plan
        # tools retired in pivot-1 T6 — milestones live in the spec.)
        *MANAGER_PLAN_TOOLS,
    ]

