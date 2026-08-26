"""T5.4.7 — prompts state the same numbers as the code (would have caught I-10).

Each numeric invariant is imported from its code constant and asserted against
the rendered prompt — no hard-coded expected number on the code side.
"""
from __future__ import annotations

import re

from app.tasks.board import MAX_BLOCKED_BOUNCES, MAX_REWORK_CYCLES
from src.config_sync.claude_md_content import (
    MANAGER_ASSISTANT_CLAUDE_MD,
    MANAGER_CLAUDE_MD,
)


def _manager() -> str:
    return MANAGER_CLAUDE_MD.replace("{manager_tool_allowlist}", "").replace(
        "{office_name}", "X"
    )


def _norm(text: str) -> str:
    """Collapse runs of whitespace so a pin survives prose reflow / re-indent
    (the old pin embedded a literal ``\\n   `` and would break on any reflow)."""
    return re.sub(r"\s+", " ", text)


def test_blocked_bounce_cap_matches_code():
    # EVAL-05: pin the cap on BOTH surfaces that state it, each to the constant
    # (was a 3-way OR whose MA branch had already gone dead — the MA playbook
    # states the cap as `blocked_bounce_count >= 1`, so if MAX_BLOCKED_BOUNCES
    # became 2 the MA prose would drift silently while the OR stayed green via
    # a Manager fragment). AND-ing both surfaces closes that.
    assert MAX_BLOCKED_BOUNCES == 1  # if this changes, sweep the prompts
    # Manager: "The bounce cap on `blocked → ready` is 1 — a second auto-bounce…"
    assert (
        f"bounce cap on `blocked → ready` is {MAX_BLOCKED_BOUNCES}"
        in _norm(_manager())
    ), "the Manager prompt must state the bounce cap pinned to the constant"
    # Manager Assistant: "When a blocked task has `blocked_bounce_count >= 1`…"
    assert (
        f"blocked_bounce_count >= {MAX_BLOCKED_BOUNCES}"
        in MANAGER_ASSISTANT_CLAUDE_MD
    ), "the MA playbook must state the bounce cap pinned to the constant"


def test_rework_cap_matches_code():
    # MAX_REWORK_CYCLES drives the reviewer escalate-at-cap rule. The prompt
    # surfaces state it HEDGED — "the rework cap (default N)" — because the
    # runtime cap is the backend-synced value (sync_config →
    # ConfigStore.max_rework_cycles; env CUBICLE_MAX_REWORK_CYCLES as the
    # pre-sync fallback), which an operator can tune without changing this
    # constant. A bare "rework_count >= 2" in a prompt silently re-hardcodes
    # the D-03 single-sourcing fix at the prompt layer; the hedge keeps the
    # instruction true under tuning while this pin keeps the stated DEFAULT
    # tied to the code.
    assert MAX_REWORK_CYCLES == 2
    assert (
        f"rework cap (default {MAX_REWORK_CYCLES})"
        in _norm(MANAGER_ASSISTANT_CLAUDE_MD)
    ), "the MA playbook must state the rework cap hedged, tied to the default"


def test_worker_prompt_caps_are_hedged_and_match_code():
    """The per-dispatch worker/reviewer prompt surfaces state the caps and the
    triage cooldown as tunable defaults (the worker subprocess has no
    ConfigStore to interpolate the synced value from, so the honest phrasing
    is 'default N'), pinned to the code constants."""
    from src.backend_client import _blocked_triage_cooldown_seconds
    from src.orchestrator.worker_prompt import (
        _DESIGNATED_REVIEWER_INSTRUCTIONS,
        build_worker_prompt,
    )

    # Reviewer block: the escalate-at-cap rule names the default.
    assert (
        f"the rework cap (default {MAX_REWORK_CYCLES})"
        in _norm(_DESIGNATED_REVIEWER_INSTRUCTIONS)
    ), "the reviewer block must state the rework cap hedged to the default"

    task = {
        "task_id": "00000000-0000-0000-0000-000000000001",
        "readable_id": "NP-001.T01",
        "title": "x",
        "rework_count": 0,
        "brief": {
            "goal": "g", "context": "c", "inputs": "i",
            "output_format": "short", "acceptance_criteria": ["a"],
            "allowed_tools": [], "required_skills": [],
            "risks_and_edge_cases": "n", "verification_steps": "v",
        },
        "workstream_short_code": "NP",
    }
    # Execute prompt: the bounce-cap reminder names the default.
    execute = _norm(build_worker_prompt({
        **task, "status": "ready", "assigned_agent": "analyst",
    }))
    assert f"capped (default {MAX_BLOCKED_BOUNCES})" in execute, (
        "the blocker-protocol reminder must state the bounce cap hedged "
        "to the default"
    )
    # Triage prompt: the cooldown names the default hour.
    assert _blocked_triage_cooldown_seconds() == 3600
    triage = _norm(build_worker_prompt({
        **task, "status": "blocked", "assigned_agent": "manager-assistant",
    }))
    assert "triage cooldown (default 1 hour)" in triage, (
        "the triage block must state the cooldown hedged to the default"
    )


def test_scope_task_soft_max_in_prompts():
    from app.config import settings

    from src.config_sync.claude_md_templates._system_agents import (
        PLANNER_CLAUDE_MD,
    )

    cap = settings.SCOPE_TASK_SOFT_MAX
    # T5.4.7 Fix: the cap must be stated in BOTH the Manager AND Planner
    # prompts (the Planner authors the scope; the Manager activates it).
    # EVAL-05: pin a SPECIFIC cap phrase, not a bare `str(cap)` — "13" occurs
    # ~7x in the Manager template, so a bare-number pin false-passes even if the
    # actual cap sentence is deleted. Each surface must carry a phrase that ties
    # the number to the scope-size rule.
    mgr = _norm(_manager())
    assert (
        f"capped at {cap} tasks" in mgr or f"never more than {cap} tasks" in mgr
    ), f"the Manager prompt must state the {cap}-task scope cap in a cap phrase"
    planner = _norm(PLANNER_CLAUDE_MD)
    assert (
        f"never more than {cap} tasks" in planner
        or f"hard ceiling of **{cap}**" in planner
    ), f"the Planner prompt must state the {cap}-task scope cap in a cap phrase"


def test_ma_review_budget_is_three_and_under_code_ceiling():
    # T5.2.13: prompt states a max review-triage call budget; the code enforces
    # a hard ceiling. Pin the relationship using the number PARSED FROM THE
    # PROMPT (not a test literal) so raising the prompt budget above the code
    # ceiling — or dropping the ceiling below it — fails CI.
    import re

    from src._agent_image.mcp_tool_server import _ma_tool_budget

    m = re.search(r"max(?:imum)?\s+(\d+)\s+tool calls",
                  MANAGER_ASSISTANT_CLAUDE_MD, re.I)
    assert m, "MA playbook must state a 'max N tool calls' budget"
    stated = int(m.group(1))
    assert stated == 3, "the MA triage budget the playbook states is 3"
    assert stated <= _ma_tool_budget(), (
        "the MA prompt's stated call budget must not exceed the code ceiling"
    )


def test_blocked_triage_cooldown_matches_code():
    # T5.4.7: the MA playbook states the triage cooldown as "1 hour"; the code
    # default (CUBICLE_BLOCKED_TRIAGE_COOLDOWN_SECONDS) must be 3600s = 1 hour.
    from src.backend_client import _blocked_triage_cooldown_seconds

    assert _blocked_triage_cooldown_seconds() == 3600
    assert "1 hour" in MANAGER_ASSISTANT_CLAUDE_MD


def test_board_sweeper_interval_matches_code():
    # T5.4.7 (re-review): the Manager playbook says "The 10-min board sweeper
    # will re-emit a user-routed escalate_blocker…". Pin it against the backend
    # SWEEPER_INTERVAL_SECONDS default so the prompt can't drift from the code.
    from app.tasks.sweeper import SWEEPER_INTERVAL_SECONDS

    assert SWEEPER_INTERVAL_SECONDS == 600  # 10 minutes (the prompt's number)
    assert "10-min board sweeper" in _manager()
    # The MA playbook states the same fact ("runs every ~10 minutes") — pin it
    # too so the two surfaces can't drift from the constant independently.
    assert "10 minutes" in MANAGER_ASSISTANT_CLAUDE_MD


def test_manager_inactivity_timeout_matches_code():
    # T5.4.7 (re-review): the Manager playbook says a turn going silent for
    # "300 seconds (5 minutes)" is treated as stalled. Pin it to the daemon
    # constant so the prompt can't drift from the watchdog.
    from src.orchestrator.manager_controller import MANAGER_INACTIVITY_TIMEOUT

    assert MANAGER_INACTIVITY_TIMEOUT == 300
    assert "300 seconds" in _manager()


# ── I-7: the Manager playbook and the canonical docs must agree ────────
#
# The playbook is what the Manager actually reads; docs/ is what humans and
# every agent working in this repo read (the CLAUDE.md files @-import it).
# When they disagree about a ROUTING dial, the platform behaves one way and
# is documented as behaving another, and nobody can tell which is the bug.
#
# That is not hypothetical: for weeks the docs said "scope-first applies to
# bodies of work with 4+ related tasks" (the 2026-07-21 fastlane wording)
# while pivot-1 T2 had already made scopes program-only machinery — a rule
# the playbook shipped throughout. scopes-and-planning.md stated BOTH, one
# paragraph apart.

def test_the_scope_threshold_is_not_the_retired_four_task_heuristic():
    """The playbook must not reintroduce the superseded rule."""
    from src.config_sync.claude_md_content import MANAGER_CLAUDE_MD

    lowered = MANAGER_CLAUDE_MD.lower()
    for retired in ("4+ related task", "four or more related task"):
        assert retired not in lowered, (
            f"the playbook states the retired scope heuristic {retired!r}; "
            "a scope is a Tier-3 program milestone (pivot-1 T2)"
        )


def test_the_playbook_states_the_milestone_model():
    """The live rule, stated where the Manager reads it."""
    from src.config_sync.claude_md_content import MANAGER_CLAUDE_MD

    lowered = MANAGER_CLAUDE_MD.lower()
    assert "scopes are program milestones" in lowered, (
        "the playbook must say what a scope IS, not only when to open one"
    )
    # The tier-3 entry threshold and the plain-task band must both be present
    # — one without the other leaves the Manager guessing at the boundary.
    assert "no scope" in lowered
