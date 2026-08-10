"""Eval family: pivot-3 phase-1 pins (branch pivot-3/phase-1).

Pins the prompt-surface facts of the pivot-3 execution model
(docs/pivot_3/plan/01-concept-overview.md — the measured disease: a
landing page = 1-2 fat tasks as a big assignment but ~25-30 tasks across
5 scopes as a program; the model: a program is a SEQUENCE of fat
assignments with human-judgeable gates):

* P1-1 — the Planner's checkpoint law (milestone ≈ ONE fat assignment;
  2-3 only on a genuine expert boundary; justify-every-split), the
  ultracode-by-default materialize line, the size_note-as-signal posture,
  and the 13-cap reframed as a runaway alarm, never a target;
* P1-2 — the Manager ladder re-centered on the single unit (1 → direct;
  2-5 → chained, no Planner; larger → program), the program-of-one
  collapse, the D3.7 classification authority (Manager decides; the
  execution_mode bubble is a FALLBACK — pinned BOTH directions: never
  over-ask AND never silently ceremony), and the D3.1 consent-rides-the-
  spec copy ("Approve & start the program");
* P1-7 — the intake when-to-ask rules (what-gets-built unknowns only,
  one round, zero questions for a complete request, answers verbatim);
* P2-2/P2-7 — standing operations (D3.3/D3.4/D3.5): the schedule tools'
  judgment-vs-mechanical routing, the never-tracker-tasks (T8) +
  never-thinking-crons playbook law, the autonomy frame (policy in the
  brief_template; draft-mode outbound BLOCKS for approval), the digest
  offer (one manager_digest schedule, never spam-created), and the
  event-thread op line (one op task per conversation thread);
* phase-1 review fixes — F2/F3 (the schedule wire-shape transform seam),
  F9 (draft-mode user-only exception on BOTH auto-decide rows + the
  worker-side draft-mode home in the shared rules), F10 (the workstream
  template's milestone model; the Manager's time-vs-event reconcile line).

Same discipline as the pivot-1/2 families: each assertion targets a
specific load-bearing sentence in a playbook / prompt builder; deleting
or paraphrasing it away fails the eval. Whitespace-normalised views
defeat re-wrapping.
"""
from __future__ import annotations

from src.config_sync.claude_md_templates._manager import MANAGER_CLAUDE_MD
from src.config_sync.claude_md_templates._system_agents._planner import (
    PLANNER_CLAUDE_MD,
)
from src.orchestrator.planner_prompt import build_planner_prompt

_MANAGER_NORM = " ".join(MANAGER_CLAUDE_MD.split())
_PLANNER_NORM = " ".join(PLANNER_CLAUDE_MD.split())


def _prompt(mode: str, scope_id: str = "scope-1") -> str:
    return build_planner_prompt({
        "planner_consult": {
            "mode": mode,
            "objective": "obj",
            "workstream_id": "ws-1",
            "scope_id": scope_id,
        },
    })


# ---------------------------------------------------------------------------
# P1-1 — the checkpoint law (Planner playbook)
# ---------------------------------------------------------------------------


def test_planner_playbook_states_the_first_law():
    """The core inversion, stated as a first-class law near the top."""
    assert (
        "## The first law — you author CHECKPOINTS, not task lists"
        in PLANNER_CLAUDE_MD
    )
    assert (
        "A milestone is ONE fat assignment — one expert, one sitting, one "
        "deliverable the approver can judge." in _PLANNER_NORM
    )
    assert (
        "Split into 2-3 ONLY on a genuine expert boundary (different "
        "specialist, different review criteria), and the intent line must "
        "SAY why it cannot be one task." in _PLANNER_NORM
    )
    # Steps-of-one-job breakdowns are named WRONG, with the executor's
    # internal orchestration as the reason.
    assert (
        "A milestone whose breakdown lists the steps of one job (setup → "
        "implement → style → test) is WRONG" in _PLANNER_NORM
    )
    assert (
        "the executor orchestrates its own steps internally (ultracode)"
        in _PLANNER_NORM
    )


def test_planner_playbook_pins_justify_every_split():
    assert (
        "Every additional task must justify why it cannot be part of "
        "another." in _PLANNER_NORM
    )
    # The skeleton default: ONE item per milestone-scope.
    assert "DEFAULT ONE item" in _PLANNER_NORM


def test_planner_milestone_cutting_is_checkpoint_shaped():
    """Milestones are cut where the USER needs a checkpoint, not where the
    work changes phase — a one-sitting deliverable is ONE milestone even
    inside a big program."""
    assert (
        "cut milestones where the USER needs a checkpoint, not where the "
        "work changes phase" in _PLANNER_NORM
    )
    assert (
        "a one-sitting deliverable is ONE milestone even inside a big "
        "program" in _PLANNER_NORM
    )


def test_specify_prompt_carries_the_fat_milestone_sizing():
    specify = _prompt("specify")
    assert (
        "A milestone is ONE fat assignment — one expert, one sitting, one "
        "deliverable" in specify
    )
    assert (
        "Cut milestones where the USER needs a checkpoint, not where the "
        "work changes phase" in specify
    )
    assert (
        "a one-sitting deliverable is ONE milestone even inside a big "
        "program" in specify
    )


def test_scope_plan_prompt_defaults_to_one_item():
    plan = _prompt("scope_plan")
    assert "DEFAULT ONE item — the milestone IS one fat assignment" in plan
    assert (
        "the intent line must SAY why it cannot be one task" in plan
    )
    # Steps of one job are one assignment (the executor orchestrates).
    assert "are ONE assignment" in plan
    assert "orchestrates its own steps internally" in plan


# ---------------------------------------------------------------------------
# P1-1 — ultracode BY DEFAULT on build-shaped materialize items
# ---------------------------------------------------------------------------


def test_materialize_defaults_build_items_to_ultracode():
    """The pre-pivot default (hint only for 'a fat cohesive build task')
    is inverted: every build-shaped item gets the hint BY DEFAULT; drop it
    only for genuinely light items."""
    prompt = _prompt("materialize")
    assert (
        "Set effort_hint:'ultracode' on every build-shaped item BY DEFAULT"
        in prompt
    )
    assert "drop it only for a genuinely light item" in prompt
    assert "Write ONE FAT brief per breakdown item" in prompt

    assert (
        "Set `effort_hint: 'ultracode'` on every build-shaped item BY "
        "DEFAULT" in _PLANNER_NORM
    )
    assert (
        "drop the hint only for a genuinely light item" in _PLANNER_NORM
    )


# ---------------------------------------------------------------------------
# P1-1/P1-3 contract — size_note is a signal, 13 is never a target
# ---------------------------------------------------------------------------


def test_size_note_is_a_signal_not_a_budget():
    """The backend (sibling P1-3) warns past 3 tasks/scope; every surface
    that names the note reads it as over-split evidence, not headroom."""
    assert (
        "The backend adds a size_note past 3 tasks — a signal you "
        "over-split, not a budget." in _PLANNER_NORM
    )
    assert (
        "The backend adds a size_note past 3 tasks in a scope — treat it "
        "as a signal you over-split, not a budget." in _prompt("materialize")
    )
    # Manager: the size_note reads as "over-split", never a budget.
    assert (
        'the backend adds a size_note past 3 — read it as "this milestone '
        'was over-split", not as a budget' in _MANAGER_NORM
    )


def test_thirteen_is_a_runaway_alarm_never_a_target():
    assert (
        "13 is the runaway alarm" in _PLANNER_NORM
        or "13 is the runaway-plan alarm" in _PLANNER_NORM
    )
    assert (
        "the warning bound for a runaway plan, NEVER a target"
        in _PLANNER_NORM
    )
    assert (
        "13 is the runaway-plan alarm, not a target" in _prompt("scope_plan")
    )
    assert "(a runaway-plan alarm, never a target)" in _prompt("materialize")
    assert (
        "Scope size is capped at 13 tasks — a runaway-plan warning, NEVER "
        "a target." in _MANAGER_NORM
    )
    assert (
        "A normal milestone-scope holds 1-3 fat tasks" in _MANAGER_NORM
    )


# ---------------------------------------------------------------------------
# P1-2 — the Manager ladder re-centered on the single unit
# ---------------------------------------------------------------------------


def test_ladder_headline_every_unit_is_a_fat_assignment():
    assert (
        "**EVERY unit of work is a FAT assignment** — one expert, one "
        "sitting, one deliverable — at every tier" in _MANAGER_NORM
    )
    assert (
        "programs never decompose finer, they just SEQUENCE fat "
        "assignments behind approval gates" in _MANAGER_NORM
    )


def test_tier1_is_two_to_five_chained_fat_assignments_no_planner():
    assert "**Tier 1 — 2-5 related fat assignments.**" in _MANAGER_NORM
    assert (
        "YOU author them, chained with `depends_on` — no scope, no "
        "Planner, no spec (unless the user asks for a contract)."
        in _MANAGER_NORM
    )


def test_tier3_is_a_sequence_of_fat_assignments():
    assert (
        "**Tier 3 — A program: a SEQUENCE of fat assignments with "
        "approval gates.**" in _MANAGER_NORM
    )
    assert (
        "EACH milestone is ONE fat assignment (2-3 tasks only on a "
        "genuine expert boundary)" in _MANAGER_NORM
    )


def test_program_of_one_collapse():
    assert "**PROGRAM-OF-ONE COLLAPSE:**" in _MANAGER_NORM
    assert (
        "a program requested for one-sitting-scale work = spec + ONE "
        "milestone + ONE fat task + verify — never invent milestones to "
        "look thorough." in _MANAGER_NORM
    )


# ---------------------------------------------------------------------------
# P1-2 — D3.7 classification authority: Manager decides, bubble = fallback,
# pinned BOTH directions (never over-ask AND never silently ceremony)
# ---------------------------------------------------------------------------


def test_manager_decides_the_shape():
    assert "YOU decide the shape" in _MANAGER_NORM
    assert "the user should almost never answer one" in _MANAGER_NORM


def test_bubble_is_a_fallback_for_exactly_three_cases():
    assert (
        "The `execution_mode` bubble is a FALLBACK for exactly three "
        "cases" in _MANAGER_NORM
    )
    assert (
        "genuine fat-assignment-vs-program ambiguity that intake answers "
        "cannot resolve" in _MANAGER_NORM
    )
    assert "option C (a program in its own workstream)" in _MANAGER_NORM
    assert (
        "manager-approval workstreams (where the bubble remains your "
        "consent path)" in _MANAGER_NORM
    )


def test_never_silently_ceremony_direction():
    """The other direction of D3.7: consent is still ALWAYS the user's —
    the Manager never starts a program on its own authority."""
    assert (
        "Never silently ceremony either — a program always needs the "
        "user's consent (spec approval, or the bubble in those cases)"
        in _MANAGER_NORM
    )
    assert "you never start one on your own authority" in _MANAGER_NORM


# ---------------------------------------------------------------------------
# P1-2 — D3.1 consent rides the spec
# ---------------------------------------------------------------------------


def test_consent_rides_the_spec_copy():
    assert (
        "In the NORMAL flow consent rides the spec: draft it (drafting is "
        "free, no consent needed), send it for approval — the user's "
        "approval click starts the program." in _MANAGER_NORM
    )
    # The Spec panel's language, mirrored in the playbook (sibling P1-5
    # owns the panel copy itself).
    assert (
        "(\"Approve & start the program\" is the panel's language"
        in _MANAGER_NORM
    )


def test_manager_approval_bubble_fires_before_approve_spec():
    """D3.1's manager-approval half: the backend provably does NOT flip
    work_mode on the Manager's own approve_spec — the bubble click is the
    only consent there, so the playbook orders it BEFORE the approval."""
    assert (
        "fire it and get the user's program click BEFORE `approve_spec`"
        in _MANAGER_NORM
    )
    assert (
        "(your approval alone never starts the program — only the user's "
        "click does)" in _MANAGER_NORM
    )


def test_default_mode_banner_routes_by_approval_mode():
    """The dynamic-context default banner routes program-shaped work to
    the spec flow (user-approval) or the bubble (manager-approval), and
    keeps drafting free."""
    from src.config_sync.sync_service import ConfigStore
    from src.orchestrator.manager_context import build_dynamic_context

    ctx = build_dynamic_context(
        "workstream:11111111-1111-1111-1111-111111111111",
        {
            "workstream_id": "11111111-1111-1111-1111-111111111111",
            "workstream_name": "Pivot3 WS",
            "workstream_priority": "high",
            "work_mode": "default",
        },
        ConfigStore(),
    )
    assert (
        "in a user-approval workstream the USER's approval click starts "
        "the program" in ctx
    )
    assert "the bubble is your consent path there" in ctx
    assert 'consult_planner(mode="specify")` and spec drafts are free' in ctx


# ---------------------------------------------------------------------------
# P1-7 — intake: when to ask, and when not to
# ---------------------------------------------------------------------------


def test_intake_block_exists_with_the_when_to_ask_bar():
    assert "## Intake — collect before you build" in _MANAGER_NORM
    assert (
        "When a request leaves unknowns that change WHAT gets built "
        "(audience, brand/content source, deploy target, must-have "
        "features/sections, integration endpoints)" in _MANAGER_NORM
    )
    # Program review #19: the recipe teaches the call shape WITH the
    # backend-REQUIRED topic param (a topic-less intake ask is refused);
    # the bounded-slice pin lives in test_flow_intake_pins.
    assert (
        'ask ONCE with `ask_user_choice(kind="intake", '
        'topic="<what-it-collects>")`' in _MANAGER_NORM
    )
    assert "2-4 questions in ONE card" in _MANAGER_NORM


def test_intake_never_asks_process_questions():
    assert (
        "NEVER ask: process questions (mode / agent / task shape — YOURS "
        "to decide), questions you can answer from the board/KB/files, or "
        "questions whose answer would not change the deliverable."
        in _MANAGER_NORM
    )


def test_intake_zero_questions_when_complete_and_one_round():
    assert "A complete request gets ZERO questions — go." in _MANAGER_NORM
    # A second round needs genuinely NEW questions — the landed intake
    # stack dedups an identical question set to the same card.
    assert (
        "One round; a second only if the answers opened a genuinely new "
        "unknown, and it must ask NEW questions (re-asking the same set "
        "just re-shows the same card)." in _MANAGER_NORM
    )
    assert (
        "thread the answers VERBATIM from that message into the "
        "spec/brief Inputs" in _MANAGER_NORM
    )


def test_intake_reply_arrives_as_a_plain_user_message():
    """Integration fact from the landed intake stack: the Manager never
    sees a structured payload — the reply is a plain user message whose
    CONTENT is the answer summary (chip labels, free text verbatim, card
    order), so verbatim threading starts from that message."""
    assert (
        "The reply arrives on your NEXT turn as a plain user message "
        "whose content IS the answers" in _MANAGER_NORM
    )
    assert (
        "chip picks by label, free text verbatim, in card order"
        in _MANAGER_NORM
    )


def test_intake_flow_stated_once():
    assert (
        "intake → classify → spec for programs, direct execution for "
        "everything else" in _MANAGER_NORM
    )


# ---------------------------------------------------------------------------
# P2-2 — the schedule tools: judgment-vs-mechanical routing (tool surface)
# ---------------------------------------------------------------------------


def _manager_tool(name: str) -> dict:
    from src._agent_image._mcp.tools_manager import get_manager_tools

    for t in get_manager_tools():
        if t["name"] == name:
            return t
    raise AssertionError(f"{name} not in manager catalog")


def test_schedule_assignment_routes_judgment_vs_mechanical():
    """The §7 when-NOT-to-use clause is the load-bearing one: a scheduled
    assignment is recurring work WITH judgment; one-offs are tasks and
    mechanical batches are scripts (cheaper, no judgment needed)."""
    desc = _manager_tool("schedule_assignment")["description"]
    assert "RECURRING WORK WITH JUDGMENT" in desc
    assert (
        "daily content, weekly summaries, periodic reviews" in desc
    )
    assert "NOT for one-off work — create a task" in desc
    assert "NOT for pure mechanical batch jobs" in desc
    assert "`schedule_script`" in desc
    assert "cheaper and need no judgment" in desc


def test_schedule_assignment_runs_ride_the_normal_rails():
    """D3.3: due → a REAL op-class task on the normal rails, overlap-
    skipped while the prior run is open — no second execution system."""
    desc = _manager_tool("schedule_assignment")["description"]
    assert "mints a REAL `op`-class task" in desc
    assert "normal rails" in desc
    assert "overlap-skipped while the prior run is still open" in desc


def test_schedule_assignment_brief_template_is_the_four_part_contract():
    """The brief_template param carries the same four-part contract bar as
    create_task — including the verbatim-inputs rule and the autonomy_note
    POLICY slot (D3.4's escalate-outside-policy frame)."""
    props = _manager_tool("schedule_assignment")["inputSchema"]["properties"]
    bt = props["brief_template"]
    assert "four-part contract" in bt["description"]
    assert "VERBATIM" in bt["description"]
    assert "never paraphrase" in bt["description"]
    assert "POLICY" in bt["description"]
    assert "escalates to the Inbox" in bt["description"]
    assert set(bt["required"]) == {
        "title", "goal", "inputs", "acceptance_criteria",
        "verification_steps",
    }
    # The schema nests autonomy_note inside brief_template (model-friendly),
    # and carries the digest instruction as a TOP-LEVEL `prompt`. The backend
    # stores the inverse (top-level autonomy_note column; prompt inside
    # brief_template) — the transform seam below (F2/F3 pins) maps between
    # the two, so these schema shapes are load-bearing wiring, not drift.
    assert "autonomy_note" in bt["properties"]
    assert "prompt" in props
    assert "manager_digest" in props["prompt"]["description"]
    # cron_expr teaches by example (5-field + specials).
    cron = props["cron_expr"]["description"]
    assert "5-field cron" in cron
    assert "@daily" in cron and "@weekly" in cron


def test_manager_digest_kind_is_a_scheduled_turn_of_yours():
    """D3.5: the digest is a scheduled Manager turn reporting in chat —
    no new surface; one per office, never spam-created."""
    desc = _manager_tool("schedule_assignment")["description"]
    assert "a scheduled turn of YOURS that reports to the user in chat" in desc
    assert "yesterday / today / blocked / awaiting-you" in desc
    assert "ONE digest per office" in desc
    assert "never spam-create them" in desc


# ---------------------------------------------------------------------------
# Pivot-3 review F2/F3 — the schedule wire-shape seam: the transform maps
# the schema's model-friendly shapes into the backend storage shape, so a
# schema-conformant call can actually create a digest (F2) and the nested
# autonomy_note is never silently dropped (F3). The backend handler accepts
# both shapes too (the belt) — pinned by the backend round-trip tests in
# backend/tests/test_assignment_schedules.py.
# ---------------------------------------------------------------------------


def test_schedule_transform_maps_digest_prompt_into_brief_template():
    """F2: top-level `prompt` (the schema shape for kind=manager_digest)
    lands as `brief_template.prompt` — the ONLY place the backend digest
    validator reads it."""
    from src._agent_image._mcp.transforms import transform_params

    out = transform_params("schedule_assignment", None, {
        "name": "Weekly digest",
        "workstream_id": "ws-1",
        "kind": "manager_digest",
        "cron_expr": "@weekly",
        "prompt": "Summarize the week: done / today / blocked / awaiting.",
    })
    assert "prompt" not in out
    assert out["brief_template"] == {
        "prompt": "Summarize the week: done / today / blocked / awaiting.",
    }
    # The non-template fields pass through untouched.
    assert out["kind"] == "manager_digest"
    assert out["cron_expr"] == "@weekly"


def test_schedule_transform_hoists_nested_autonomy_note():
    """F3: `brief_template.autonomy_note` (the schema's nesting for
    kind=agent_task) hoists to the top-level column slot; the four-part
    contract fields survive inside the template."""
    from src._agent_image._mcp.transforms import transform_params

    out = transform_params("schedule_assignment", None, {
        "name": "Daily content",
        "workstream_id": "ws-1",
        "kind": "agent_task",
        "agent": "analyst",
        "cron_expr": "0 9 * * *",
        "brief_template": {
            "title": "Daily content run",
            "goal": "Produce the batch.",
            "inputs": "The playbook, verbatim.",
            "acceptance_criteria": ["Three posts drafted."],
            "verification_steps": "Open the file; count three posts.",
            "autonomy_note": "Draft only — never send without approval.",
        },
    })
    assert out["autonomy_note"] == (
        "Draft only — never send without approval."
    )
    assert "autonomy_note" not in out["brief_template"]
    assert out["brief_template"]["goal"] == "Produce the batch."
    assert out["brief_template"]["verification_steps"] == (
        "Open the file; count three posts."
    )


def test_update_schedule_transform_supports_prompt_only_update():
    """F2 (update half): a prompt-only `update_assignment_schedule` call —
    the schema's digest-update shape — maps into a brief_template patch
    instead of 400ing with 'Nothing to update'."""
    from src._agent_image._mcp.transforms import transform_params

    out = transform_params("update_assignment_schedule", None, {
        "schedule_id": "sched-1",
        "prompt": "New digest instruction.",
    })
    assert "prompt" not in out
    assert out["brief_template"] == {"prompt": "New digest instruction."}
    assert out["schedule_id"] == "sched-1"


def test_schedule_transform_passes_canonical_shape_through():
    """Already-backend-shaped params (top-level autonomy_note, prompt inside
    brief_template) pass through unchanged — the transform is idempotent
    with the handler belt."""
    from src._agent_image._mcp.transforms import transform_params

    params = {
        "name": "Digest",
        "workstream_id": "ws-1",
        "kind": "manager_digest",
        "cron_expr": "@daily",
        "brief_template": {"prompt": "Report."},
    }
    assert transform_params("schedule_assignment", None, dict(params)) == params


def test_schedule_update_and_delete_split_pause_from_retire():
    """Pause (update is_active=false) keeps the cadence + template; delete
    is permanent retirement — each tool's when-NOT names the other."""
    upd = _manager_tool("update_assignment_schedule")["description"]
    assert "Pausing keeps the cadence + template" in upd
    assert "`delete_assignment_schedule`" in upd
    # A run's outcome lives on the minted op task, not the schedule.
    assert "NOT for one run's outcome" in upd
    dele = _manager_tool("delete_assignment_schedule")["description"]
    assert "already-minted op tasks are untouched" in dele
    assert "NOT for a pause" in dele
    assert "`update_assignment_schedule(is_active=false)`" in dele


def test_list_schedules_is_the_dedup_read():
    desc = _manager_tool("list_assignment_schedules")["description"]
    assert "BEFORE `schedule_assignment` to avoid duplicates" in desc
    assert "one per office" in desc  # the digest dedup rule rides the read


# ---------------------------------------------------------------------------
# P2-2 — the playbook law: schedules, never tracker tasks, never
# cron-scripts-pretending-to-think
# ---------------------------------------------------------------------------


def test_standing_ops_are_schedules_never_tracker_tasks():
    assert (
        "## Standing operations — schedules, never tracker tasks"
        in _MANAGER_NORM
    )
    # The T8 rule survives, restated at the canon home (pivot-1 pins keep
    # asserting the exact strings).
    assert "the standing object is a SCHEDULE, never a board object" in _MANAGER_NORM
    assert "never a cron-script-pretending-to-think" in _MANAGER_NORM


def test_standing_ops_route_by_judgment():
    assert (
        "**Recurring + mechanical** (same steps every time, no judgment) → "
        "`schedule_script`" in _MANAGER_NORM
    )
    assert (
        "**Recurring + judgment** (daily campaign content, support replies, "
        "weekly summaries, periodic reviews) → `schedule_assignment`"
        in _MANAGER_NORM
    )
    assert "a fat op task on a cadence" in _MANAGER_NORM
    assert "**One-off** → a task, never a schedule." in _MANAGER_NORM
    # Tier 2 routes judgment-shaped recurrence away from scripts.
    assert (
        "recurring work WITH judgment is a scheduled assignment, not a "
        "script" in _MANAGER_NORM
    )


def test_autonomy_frame_policy_in_brief_draft_mode_blocks():
    """D3.4: the brief carries the POLICY; outside-policy escalates; a
    draft-mode outbound op task BLOCKS for approval before sending and the
    approval resumes it — never auto-send on an ungraduated channel."""
    assert (
        "the schedule's `brief_template` carries the POLICY" in _MANAGER_NORM
    )
    assert (
        "what the op may do WITHOUT asking, drawn from the approved spec or "
        "a policy skill" in _MANAGER_NORM
    )
    assert "Anything outside policy escalates to the Inbox" in _MANAGER_NORM
    assert (
        "the op task BLOCKS for approval before sending — the draft is the "
        "Inbox card, approval resumes it" in _MANAGER_NORM
    )
    assert (
        "never auto-send on a channel the user hasn't graduated"
        in _MANAGER_NORM
    )


# ---------------------------------------------------------------------------
# P2-7 — the digest offer (playbook side)
# ---------------------------------------------------------------------------


def test_digest_offered_once_never_spammed():
    assert (
        "when an office starts running standing work, OFFER the user ONE "
        "daily/weekly digest schedule" in _MANAGER_NORM
    )
    assert (
        "a scheduled turn of YOURS that reports yesterday / today / blocked "
        "/ awaiting-you in chat" in _MANAGER_NORM
    )
    assert "never spam-create digests" in _MANAGER_NORM
    assert "drop the offer if the user declines" in _MANAGER_NORM


# ---------------------------------------------------------------------------
# P2-4 contract — ops in the event flow (one op task per thread)
# ---------------------------------------------------------------------------


def test_event_stream_reaction_is_one_op_task_per_thread():
    """The inbound-events block states the threading contract the sibling
    P2-4 backend implements: events thread by thread_key into the OPEN op
    task; the agent replies from the task; new outbound channels default
    to draft mode."""
    assert "ONE op task per conversation thread" in _MANAGER_NORM
    assert (
        "the backend threads events by thread_key into the open op task"
        in _MANAGER_NORM
    )
    assert (
        "the agent replies FROM the task, draft-mode by default for new "
        "outbound channels" in _MANAGER_NORM
    )


def test_auto_decide_never_self_approves_draft_mode_outbound():
    """P2-5 seam (the events sibling's verdict): draft-mode outbound rides
    request_clarification, whose auto-decide default would let the MANAGER
    approve — i.e. auto-send on an ungraduated channel. The per-poke row
    must carry the user-only exception. Review F9(a): the SAME exception
    must ride the escalate_blocker row — a rerouted/sweeper-raised
    escalation carrying an outbound draft is the sweeper bypass around the
    request_clarification row."""
    from src.config_sync._auto_decide_rows import AUTO_DECIDE_ROWS

    for rtype in ("request_clarification", "escalate_blocker"):
        row = " ".join(AUTO_DECIDE_ROWS[rtype].split())
        assert "DRAFT-MODE OUTBOUND" in row, rtype
        assert "belongs to the USER, never you" in row, rtype
        assert "REJECT so it re-routes" in row, rtype


def test_worker_shared_rules_carry_outbound_draft_mode_discipline():
    """Review F9(b): the worker-side home for D3.4's draft-mode frame —
    never send first; the COMPLETE draft rides the request_clarification
    question; after resume, send EXACTLY the approved draft."""
    from src.config_sync.claude_md_content import SHARED_AGENT_WORK_RULES

    norm = " ".join(SHARED_AGENT_WORK_RULES.split())
    assert "**Outbound DRAFT MODE:**" in norm
    assert "never execute the send first" in norm
    assert (
        "block with `request_clarification` carrying the COMPLETE draft "
        "in the `question`" in norm
    )
    assert "<=5000 chars" in norm
    assert "a longer draft goes to an office file, reference the path" in norm
    assert (
        "send EXACTLY the approved draft, amended only by the approval's "
        "answer notes" in norm
    )


# ---------------------------------------------------------------------------
# Review F10 — drift batch repins (workstream template milestone model;
# the Manager's time-vs-event standing-work reconcile line)
# ---------------------------------------------------------------------------


def test_workstream_template_states_the_milestone_model():
    """The per-workstream CLAUDE.md no longer teaches the retired fastlane
    4+ scope threshold — scopes are program milestones (normally ONE fat
    assignment); 2-5 related fat assignments chain unscoped."""
    from src.config_sync.claude_md_templates._workstream import (
        generate_workstream_claude_md,
    )

    text = generate_workstream_claude_md({
        "short_code": "WS", "name": "Pins", "priority": "high",
    })
    norm = " ".join(text.split())
    assert "**Scopes are program milestones**" in norm
    assert (
        "a scope normally holds ONE fat assignment (2-3 tasks only on a "
        "genuine expert boundary)" in norm
    )
    assert (
        "2-5 related fat assignments chain with `depends_on` — no scope"
        in norm
    )
    assert "4+ related" not in norm


def test_manager_reconciles_schedules_with_event_thread_ops():
    """The one sentence squaring 'never a board object' with the P2-4
    threading contract: TIME-driven standing work is a schedule; an
    EVENT-driven conversation is the one exception — one op task per
    thread, created on the first event."""
    assert (
        "TIME-driven standing work = a SCHEDULE, never a board object; "
        "EVENT-driven conversations are the ONE exception — each "
        "conversation thread lives as ONE `op` task, created on the first "
        "event" in _MANAGER_NORM
    )


def test_board_projection_keeps_completed_at_for_digests():
    """F12: the lean get_board projection must keep ``completed_at`` so a
    weekly digest turn can date completions (the projection strips all
    other timestamps by design)."""
    from src._agent_image._mcp.transforms import _BOARD_TASK_KEEP

    assert "completed_at" in _BOARD_TASK_KEEP
    assert "created_at" not in _BOARD_TASK_KEEP  # the strip stays a strip
