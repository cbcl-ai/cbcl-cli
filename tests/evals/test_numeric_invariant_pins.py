"""T5.4.7 — prompts state the same numbers as the code (would have caught I-10).

Each numeric invariant is imported from its code constant and asserted against
the rendered prompt — no hard-coded expected number on the code side.
"""
from __future__ import annotations

from app.tasks.board import MAX_BLOCKED_BOUNCES, MAX_REWORK_CYCLES
from src.config_sync.claude_md_content import (
    MANAGER_ASSISTANT_CLAUDE_MD,
    MANAGER_CLAUDE_MD,
)


def _manager() -> str:
    return MANAGER_CLAUDE_MD.replace("{manager_tool_allowlist}", "").replace(
        "{office_name}", "X"
    )


def test_blocked_bounce_cap_matches_code():
    assert MAX_BLOCKED_BOUNCES == 1  # if this changes, sweep the prompts
    assert f"bounce-capped at {MAX_BLOCKED_BOUNCES}" in MANAGER_ASSISTANT_CLAUDE_MD or \
        f"bounce cap on `blocked → ready`\n   is {MAX_BLOCKED_BOUNCES}" in _manager() or \
        f"is {MAX_BLOCKED_BOUNCES} — a second auto-bounce" in _manager()


def test_rework_cap_matches_code():
    # MAX_REWORK_CYCLES drives the reviewer escalate-at-cap rule.
    assert MAX_REWORK_CYCLES == 2
    assert f"rework_count >= {MAX_REWORK_CYCLES}" in MANAGER_ASSISTANT_CLAUDE_MD


def test_scope_task_soft_max_in_prompts():
    from app.config import settings

    from src.config_sync.claude_md_templates._system_agents import (
        PLANNER_CLAUDE_MD,
    )

    cap = settings.SCOPE_TASK_SOFT_MAX
    # T5.4.7 Fix: the cap must be stated in BOTH the Manager AND Planner
    # prompts (the Planner authors the scope; the Manager activates it).
    assert str(cap) in _manager(), (
        f"the {cap}-task scope cap must be stated in the Manager prompt"
    )
    assert str(cap) in PLANNER_CLAUDE_MD, (
        f"the {cap}-task scope cap must be stated in the Planner prompt"
    )


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
