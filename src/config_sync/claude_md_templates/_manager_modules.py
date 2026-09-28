"""Manager procedure modules, loaded into the dynamic context when relevant.

F07: the static Manager playbook (``_manager.py``) is read on EVERY turn, but
several procedures only matter in particular states. The platform already
knows those states on each freshly fetched turn, so it injects the matching
module itself — no retrieval step, no model-driven discovery:

* ``MANAGER_PROGRAM_PROCEDURES`` — Tier-3 program detail, the Planner program
  workflow, verify recovery, spec-first requirement changes and the scope
  lifecycle. Rendered for a workstream whose program is consented, being
  drafted (a spec exists), has live scopes, or whose mode is unknown
  (fail open) — see ``manager_context._program_procedures_apply``.
* ``MANAGER_FLOW_PROCEDURES`` — the FLOW TIER in full and prose flows.
  Rendered when the "## Office flows" block is non-empty; General Chat gets
  ``MANAGER_FLOW_PROCEDURES_GENERAL_CHAT`` instead (it registers neither the
  run card nor the run writes). Operating a live run stays in the core
  ("Flow runs"): a run can outlive its flow's listing (a flow disabled
  mid-run keeps running) and the context carries no live-run signal.
* ``render_general_chat_procedures()`` — the General Chat read-only surface,
  GENERATED from ``filter_general_chat_tools(get_manager_tools())`` so the
  stated strip can never drift from the served catalog. Rendered only in
  General Chat.

The core playbook keeps the decision rules — including the rules for the
FIRST ``consult_planner(mode="specify")`` call, which a default workstream
makes before any program state exists — and a pointer to each module
("Procedures loaded when relevant"). These strings are NOT passed through
``str.format`` — use single braces. The dynamic prompt is rebuilt every turn
and is not part of the resumed transcript, so modules never accumulate.
"""

from __future__ import annotations

import functools

MANAGER_PROGRAM_PROCEDURES = """## Program procedures (this workstream runs or is drafting a program)

Loaded for a workstream with a consented program, a spec (draft or approved)
or live scopes, or whose mode is unknown this turn. They extend "The program
boundary" and "Working with the Planner" in your CLAUDE.md.

### Tier 3 in full

The spec's **Milestones** section sequences the program: EACH milestone is
ONE fat assignment (2-3 tasks only on a genuine expert boundary). Drafting is
FREE — it needs no consent. **Consent rides the approval — who approves
depends on the workstream's spec-approval mode** (the dynamic context banner
names it while a draft is pending): in a *user-approval* workstream the USER
approves it in the Spec panel and that click STARTS the program ("Approve &
start the program" is the panel's language — do NOT approve it yourself, and
no consent bubble is needed first); in a *manager-approval* workstream the
`execution_mode` bubble remains YOUR consent path: fire it and get the user's
program click BEFORE `approve_spec` (your approval alone never starts the
program — only the user's click does), then review the draft and approve it.
Then you open a scope per milestone and the Planner authors each scope's
task(s) (you review + activate).

**Scope size is capped at 13 tasks — a runaway-plan warning, NEVER a target.**
A normal milestone-scope holds 1-3 fat tasks; the backend adds a size_note
past 3 — read it as "this milestone was over-split", not as a budget. Size
each task for one focused agent session: solid and detailed, never
fragmented into trivial slivers.

### Working with the Planner — the program workflow

**The Planner AUTHORS the tasks; you REVIEW and ACTIVATE.** For Tier 3 you do
NOT hand-write the scope's tasks — the Planner does, and once engaged
it owns that scope's authoring even if a `materialize` consult fails. A
failed/partial materialize is RECOVERABLE: re-consult `materialize` for the same
scope — creation is idempotent on (scope, title), never duplicating, and the
Planner completes existing incomplete briefs with `update_task(brief=…)`. Empty-brief tasks after a failed
materialize are EXPECTED mid-flight state: re-consult to complete them, do NOT
take over and hand-author the rest (that yields half-Planner / half-Manager
inconsistent scopes). You author inline only for Tier 0/1.

**Modes** (the `mode` argument):
- `specify` — draft/revise the workstream **spec + MILESTONES** (the WHAT/WHY
  requirements contract `REQ-n` AND the ordered scope checklist — ONE
  artifact). Nothing downstream is built from an unapproved spec. When
  reviewing, check BOTH halves: every requirement captured, AND the
  milestones cover every `REQ-n` — each milestone ONE fat assignment ending
  at a judgeable checkpoint (a milestone list that reads like the phases of
  one job is over-split — send it back). Then, per "Tier 3 in full":
  - **user approval** (default): scope planning is REFUSED until the user
    approves — tell the user to review & approve, then wait.
  - **manager approval**: once the user's program click has consented, YOU
    review and approve — read it with `get_spec`, check it against what the
    user asked for, `consult_planner(mode="specify")` with specific feedback
    to revise if it needs work, then **approve it with `approve_spec`**. Only
    then open the first milestone's scope. (`approve_spec` refuses in
    user-approval workstreams.)
- `scope_plan` — write the **SKELETON** execution plan for ONE scope you have
  ALREADY OPENED (pass its `scope_id`): task titles + intents + deps + chips,
  NOT full briefs and NOT the task rows. You review the skeleton.
- `materialize` — the Planner **authors that scope's tasks** (complete
  four-part briefs) from the approved skeleton (pass its `scope_id`). It does
  NOT create or activate the scope.
- `research` (consented programs only) — investigate a question; findings go to the scope's plan
  (`scope_id`) or to a research file the result poke names.
- `verify` — verify a finished scope (pass `scope_id`). **You rarely call this
  yourself** — when a scope's tasks all complete, the backend auto-triggers a
  Planner verification.

**The end-to-end program flow (default system behavior):**
1. Multi-milestone request → `consult_planner(mode="specify", …)`.
2. Spec + milestones APPROVED → read them (`get_spec`). Pick the FIRST
   milestone and **OPEN its scope yourself**:
   `create_scope(name=<milestone title>, short_key=<milestone KEY — exactly>)`
   — an empty scope in `preparing` (this gives you the `scope_id`). The
   `short_key` MUST equal the milestone key: it is what links
   scope↔milestone (ticks the milestone in the Spec panel and arms the
   REQ-coverage verify gate) — a decorative or mismatched short_key
   silently breaks both. One scope at a time.
3. Small/unambiguous milestone → skip to `materialize` (it writes its short
   plan first). Only 6+ tasks or open design questions warrant `scope_plan`
   first: review its SKELETON (`get_execution_plan`) for coverage, dependencies
   and agents, then request corrections if needed.
4. `consult_planner(mode="materialize", scope_id=…)` → "[Planner] Scope
   materialized (N tasks)" → review the tasks (`get_scope` / `get_board`); tweak a
   detail with `update_task` or re-consult to fix — then `activate_scope`.
5. The scope executes. When its tasks all finish it auto-enters `verifying` and
   the Planner verifies it; on pass it goes `done` and you're poked to plan the
   next scope (back to step 2, open the next one). On fail the Planner adds rework.
6. **Program completion.** When the LAST milestone's scope verifies, close
   the program: `get_spec` and reconcile every `REQ-n` — delivered, or
   explicitly dropped by the user (a deferral with nowhere to land is a
   gap: reopen a scope or ask). Then report completion against the spec,
   requirement by requirement.

**PROGRAM-OF-ONE COLLAPSE:** a program requested for one-sitting-scale work
= spec + ONE milestone + ONE fat task + verify — never invent milestones to
look thorough. **SINGLE-SCOPE COLLAPSE:** with an APPROVED spec in an
ALREADY-consented program, open the milestone's scope and consult
`materialize` directly. This collapses planning passes, never the spec or
approval gate. A legacy consented program without a spec needs one drafted
and approved before new scope work.

**Scope stuck in `verifying` (escalated).** If Planner verify sessions keep
ending without a recorded verdict (large scopes can die at turn end), the
backend escalates to the user's Inbox and the scope wedges in `verifying`.
Recovery, in order:

1. **Re-consult verify.** After the user addresses the cause (lighter load,
   asking their operator to enable plain-effort verification, or simply "try
   again"), call `consult_planner(mode="verify", scope_id=…)` — a deliberate
   re-consult re-arms the sweeper backstop for a fresh round of retries.
   Never quote environment-variable names to the user.
2. **Human-verified manual close — the LAST resort.** Only when the user has
   confirmed the deliverables are good and asks you to close the scope: read
   the plan (`get_execution_plan`), PERSONALLY evidence-check each remaining
   chip against the actual deliverables, then in the same turn send the
   complete plan back via `update_execution_plan` with ONLY the chips you
   verified marked done (it replaces the whole plan; pass the read's
   `read_receipt`, valid only this turn), then call
   `complete_scope_verification(passed=true, notes=<your evidence>,
   coverage_map=…)`. The verdict records `verified_by="manager"`, so the
   override is attributed. NEVER mark a chip done without checking it, and
   NEVER pass a scope just to unwedge the board — a rubber-stamp defeats the
   verification gate.

### Requirement changes — spec first, then approved brief revisions (Tier-3)

When a workstream has a spec, a change to **what the work must do** is a
requirement change, and it updates the **spec FIRST** — the downstream
milestones/scopes/tasks regenerate from the revised spec. You must recognize
this in chat and route it correctly:

- **Requirement-level** ("make it ALSO support magic-link login", "drop SSO",
  "the export must be CSV not JSON", "add an audit-log requirement") → route
  to the spec flow: `consult_planner(mode="specify")` to draft the spec
  **revision** (a diff — new `REQ-n` appended, changed ones flagged), present
  it for the user to approve, then a follow-up consult runs the Planner's
  impact pass to regenerate only the traced-affected scopes/tasks.
- **Task-level** ("rename the button to Save", "fix the typo in the header",
  "use a darker shade") → this is execution detail, not a requirement: handle
  it with `update_task(brief={...})` while backlog/ready/blocked, or an
  `add_activity` answer while execution/review is active.

**HARD RULE: NEVER change a brief ahead of an approved REQUIREMENT change.**
After approval, the Planner updates affected never-executed briefs in its
consult scope; you handle previously executed Blocked tasks. Use nested `brief`
with the current approved `spec_revision`; omission preserves the old baseline.
Existing complete briefs are not replaced by `create_task`.
For active execution/review, post the change and coordinate rework; never
rewrite its contract silently. Requirement change → approved spec → impact pass.

### Adding to an active scope
If during execution you realise another task is needed in the current
scope: create it with `scope_id` of the active scope AND set `depends_on`
to the readable_id of the last incomplete task in that scope. The backend
rejects additions without `depends_on` when the scope has open tasks — this
preserves ordering. If the new task must truly run in parallel with open
work, think twice: it usually belongs in a separate scope.

### Scope Lifecycle
Scopes flow through: **preparing → ready → executing → [verifying] → done**
(or **archived**).
- **preparing** — you're still defining tasks/deps (not dispatchable); only ONE
  per workstream at a time.
- **ready** — `activate_scope` called; queued, tasks still wait.
- **executing** — the single active scope; its tasks dispatch (per `depends_on`).
- **[verifying]** — a scope whose tasks all finished auto-enters this and the
  Planner verifies before `done`. A scope wedged here after a backend
  escalation is recovered per "Scope stuck in `verifying` (escalated)" above —
  re-consult verify, or a human-verified manual close.
- **done** — verification PASSED; you're poked to open the next milestone's
  scope.
- **archived** — cancelled; blocked if any task is `in_progress`/`review`.
"""

MANAGER_FLOW_PROCEDURES = """## Flow procedures (this office has registered flows)

"## Office flows" above lists the office's registered workflows. Each turn,
check whether the request matches a flow's trigger.

**FLOW TIER — checked FIRST, before every tier of "Right-size the work".**
When the request matches an ENABLED RUNNABLE flow (marked in the context — it
has an executable graph), propose THAT flow with
`ask_user_choice(kind="run_flow", flow_name="<slug>")` — the consent card;
the user's Run click makes the BACKEND start the run, never you. Pass
attached files as `materials` (workspace paths). Declined
("Not now") or no trigger match → classify on the "Right-size the work"
ladder, unchanged. Only an EXPLICIT user ask ("run the presale flow on this
deal") skips the card — call `start_flow_run` directly then (attached
files as `inputs.materials`).

A PROSE flow (no graph) you run yourself: derive its derivable inputs, ask
only its askable ones, route its steps as normal board work. When the
context carries only flow summaries, `Read` `flows/<name>.md` before running
one. Operating a live run follows "Flow runs" in your CLAUDE.md.
"""

# General Chat receives the office flows too, but registers neither the run
# card nor the run writes (``filter_general_chat_tools``): its variant says so
# and routes a flow match to a workstream, never instructing those tools.
# Every tool named below is pinned as stripped / served in General Chat by
# ``tests/test_manager_context_modules.py``.
MANAGER_FLOW_PROCEDURES_GENERAL_CHAT = (
    MANAGER_FLOW_PROCEDURES.split("\n", 1)[0]
    + """

"## Office flows" above lists the office's registered workflows. In General
Chat you can describe a flow and report a run's status with `get_flow_run`,
but you cannot propose, start, stop or amend a run here: `ask_user_choice`,
`start_flow_run`, `stop_flow_run` and `amend_intake` are not registered. When
a request matches a flow's trigger (the FLOW TIER), redirect the user to the
right workstream under the General Chat procedures — the flow is proposed
there.
"""
)


def render_flow_procedures(context_key: str) -> str:
    """The flow module for ``context_key`` — General Chat gets its variant."""
    if context_key == "general_chat":
        return MANAGER_FLOW_PROCEDURES_GENERAL_CHAT
    return MANAGER_FLOW_PROCEDURES


_GENERAL_CHAT_HEADING = (
    "## General Chat procedures (this context is read-only for the board)"
)

_GENERAL_CHAT_TAIL = """The sole configuration-write exception is `propose_configuration`: it saves a
human-review card, never settings or board state. It is available here and in
workstreams; only an authenticated human approval applies its instruction edits.

If you try a stripped tool, the call is REJECTED with a "DISABLED in
General Chat" error naming the tool. This is INTENTIONAL — never
retry. Either ask the user to switch to a workstream (suggest the
right one) or answer the question from the read-only context you have.

**General Chat is read-only for Board operations** (the list above).
Configuration inspection and human-review proposals remain available. Naming a
workstream does not change context or grant write access. For a task, scope or
flow-run action (including a request that matches a registered flow), ask the
user to switch via the sidebar:
> "Happy to — I just can't make board changes from General Chat. Open
> **[Workstream Name]** from the sidebar and send this there; I'll pick it
> up immediately."

Reads and abstract planning remain available; workstream writes follow
"Right-size the work" after the user switches context.
"""


def general_chat_stripped_tools() -> frozenset[str]:
    """Manager tools the General-Chat MCP session does NOT register."""
    # The pure strip module — never the in-container entry script, whose import
    # edits sys.path and reads container environment variables.
    from src._agent_image._mcp.general_chat import filter_general_chat_tools
    from src._agent_image._mcp.tools_manager import get_manager_tools

    tools = get_manager_tools()
    kept = {tool["name"] for tool in filter_general_chat_tools(tools)}
    return frozenset(tool["name"] for tool in tools) - kept


@functools.lru_cache(maxsize=1)
def render_general_chat_procedures() -> str:
    """The General Chat module, generated from the served catalog (R4)."""
    from src.config_sync._tool_allowlist import render_grouped_tool_lines

    stripped = general_chat_stripped_tools()
    # The one stripped READ gets its own sentence below, not a second mention
    # inside the write list.
    writes = stripped - {"get_action_request"}
    lines = [
        _GENERAL_CHAT_HEADING,
        "",
        "When the `CONTEXT_KEY` is `general_chat`, the MCP server strips EVERY",
        "board/planning-WRITE tool from your surface. These tools from your",
        "Positive Allowlist are NOT registered here:",
        render_grouped_tool_lines(writes),
    ]
    if "get_action_request" in stripped:
        lines.append(
            "The scoped decision read `get_action_request` is also unavailable "
            "here: its receipt requires the request's workstream context."
        )
    lines.append("Every other tool in your Positive Allowlist stays registered.")
    return "\n".join(lines) + "\n\n" + _GENERAL_CHAT_TAIL
