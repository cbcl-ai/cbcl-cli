"""T5.3.1 — the auto-decide policy table lives in a constant injected per-type
into the synthetic turn, not in the standing Manager CLAUDE.md."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from src.config_sync._auto_decide_rows import (
    AUTO_DECIDE_ROWS,
    render_auto_decide_guidance,
)
from src.config_sync.claude_md_templates._manager import MANAGER_CLAUDE_MD
from tests.backend_boundary import import_backend

def test_rows_cover_every_request_type():
    # T5.4.8: bidirectional parity with the REAL backend REQUEST_TYPES — a new
    # type without a row fails CI, and a stale row for a removed type fails too.
    REQUEST_TYPES = import_backend("app.action_requests.schemas").REQUEST_TYPES

    assert set(AUTO_DECIDE_ROWS) == set(REQUEST_TYPES)


def test_review_hold_policy_never_uses_generic_decision_instructions():
    guidance = render_auto_decide_guidance("review_hold")
    assert "USER-ONLY" in guidance
    assert "never approve, reject, archive" in guidance
    assert "dedicated Retry review" in guidance
    assert "MUST take the follow-up action" not in guidance
    assert "reject with a note" not in guidance


async def test_misrouted_review_hold_never_prompts_manager(monkeypatch):
    from src.orchestrator import _manager_action_requests

    dispatch = AsyncMock()
    monkeypatch.setattr(_manager_action_requests, "_dispatch_poke", dispatch)
    await _manager_action_requests.ingest_action_request_auto_decide(MagicMock(), {"request_type": "review_hold"})
    dispatch.assert_not_awaited()


def test_no_row_contradicts_itself():
    # F13's failure shape: a cell asserting both "no auto side-effect" AND
    # "auto-unblock". Mechanically excluded.
    for rtype, row in AUTO_DECIDE_ROWS.items():
        low = row.lower()
        assert not ("no auto side-effect" in low and "auto-unblock" in low), (
            f"row {rtype!r} both asserts and denies an auto side-effect"
        )


def test_render_includes_preamble_and_only_the_matching_row():
    out = render_auto_decide_guidance("escalate_blocker")
    assert "Approve ≠ done" in out
    assert "Policy for `escalate_blocker`" in out
    # Only the matching row's distinctive text appears, not other rows'.
    assert "auto-promotes the blocked source task" in out
    assert "Apply the Agent-Selection 3-step audit" not in out  # create_task row


def test_unknown_type_gets_generic_fallback():
    out = render_auto_decide_guidance("totally_made_up")
    assert "Unrecognised request_type" in out


def test_standing_approve_semantics_names_escalate_blocker_autounblock():
    # F-5.2.6-A regression: the standing "Approve ≠ done" bullet must stay
    # consistent with the backend auto-unblock set. It previously claimed ONLY
    # create_task + request_clarification auto-fire, omitting escalate_blocker —
    # which DOES auto-promote a blocked source task on approve.
    auto_unblock_types = import_backend(
        "app.action_requests.decisions"
    ).AUTO_UNBLOCK_REQUEST_TYPES

    assert "escalate_blocker" in auto_unblock_types
    idx = MANAGER_CLAUDE_MD.find("Approve ≠ done")
    assert idx != -1, "standing approve-semantics bullet not found"
    window = MANAGER_CLAUDE_MD[idx:idx + 700]
    assert "escalate_blocker" in window, (
        "approve-semantics bullet must name escalate_blocker as an auto-unblocker"
    )
    assert "auto-promote" in window.lower()


def test_blocker_shaped_rows_share_the_draft_mode_user_only_exception():
    # Pivot-3 review F9(a): draft-mode outbound normally rides
    # request_clarification, but a sweeper-raised / rerouted escalation can
    # carry the same outbound draft as an escalate_blocker — approving THAT on
    # auto-decide would auto-send on an ungraduated channel (both types are in
    # the backend AUTO_UNBLOCK_REQUEST_TYPES set). Both rows must carry the
    # user-only REJECT exception.
    auto_unblock_types = import_backend(
        "app.action_requests.decisions"
    ).AUTO_UNBLOCK_REQUEST_TYPES

    for rtype in ("request_clarification", "escalate_blocker"):
        assert rtype in auto_unblock_types
        row = " ".join(AUTO_DECIDE_ROWS[rtype].split())
        assert "DRAFT-MODE OUTBOUND" in row, rtype
        assert "belongs to the USER, never you" in row, rtype
        assert "REJECT so it re-routes" in row, rtype


def test_standing_template_no_longer_carries_the_full_table():
    # The ~1.8k-token per-type table was moved out (T5.3.1). The standing
    # template keeps only the pointer + hard rules.
    assert "Each auto-decide synthetic\nturn carries its own policy" in MANAGER_CLAUDE_MD
    # A distinctive per-row phrase must NOT be in the standing template anymore.
    assert "Apply the **Agent Selection** 3-step audit" not in MANAGER_CLAUDE_MD
    # F07: nor in any procedure module the dynamic context injects.
    from tests.evals._prompt_composition import manager_corpus

    assert "Apply the **Agent Selection** 3-step audit" not in manager_corpus()


# ── X11 / X64 / X01: rows must be executable with the Manager's catalog ──

_WORKER_ONLY_EVENT_TYPES = ("checkpoint", "question", "task_proposed")


def _manager_tool_names() -> set[str]:
    from src._agent_image._mcp.tools_manager import get_manager_tools

    return {tool["name"] for tool in get_manager_tools()}


def _backticked(text: str) -> set[str]:
    import re

    return set(re.findall(r"`([a-z][a-z0-9_]+)`", text))


def test_rows_only_name_tools_the_manager_holds():
    from src.config_sync._auto_decide_rows import AUTO_DECIDE_PREAMBLE
    from src.orchestrator._manager_action_requests import _FOLLOWUP_BY_TYPE

    tools = _manager_tool_names()
    texts = [AUTO_DECIDE_PREAMBLE, *AUTO_DECIDE_ROWS.values(), *_FOLLOWUP_BY_TYPE.values()]
    tool_shaped = {
        token for text in texts for token in _backticked(text)
        if token.split("_", 1)[0] in {"create", "update", "move", "add", "get",
                                      "retry", "consult", "decide", "archive",
                                      "stop", "delete"}
    }
    assert tool_shaped <= tools, tool_shaped - tools


def test_manager_add_activity_follow_ups_use_manager_event_types():
    # The Manager's add_activity enum is comment/answer only; a row telling it
    # to post a checkpoint/question is refused by the schema (X64).
    from src._agent_image._mcp.tools_manager import get_manager_tools
    from src.orchestrator._manager_action_requests import _FOLLOWUP_BY_TYPE

    add_activity = next(t for t in get_manager_tools() if t["name"] == "add_activity")
    allowed = set(add_activity["inputSchema"]["properties"]["event_type"]["enum"])
    assert allowed == {"comment", "answer"}
    for text in [*AUTO_DECIDE_ROWS.values(), *_FOLLOWUP_BY_TYPE.values()]:
        if "add_activity" in text:
            for worker_only in _WORKER_ONLY_EVENT_TYPES:
                assert worker_only not in text, (worker_only, text)


def test_review_check_row_and_reconcile_follow_up_agree():
    from src.orchestrator._manager_action_requests import _FOLLOWUP_BY_TYPE

    row = AUTO_DECIDE_ROWS["request_review_check"]
    assert "`answer`" in row and "rarely auto-decided" not in row
    assert "`answer`" in _FOLLOWUP_BY_TYPE["request_review_check"]


def test_multi_task_unblock_never_prescribes_a_locking_move_chain():
    # move_task → ready/done PRE-LOCKs the Manager turn, so a "for each"
    # move_task chain dies after the first call (X11).
    from src.config_sync._auto_decide_rows import AUTO_DECIDE_PREAMBLE

    row = AUTO_DECIDE_ROWS["setup_office_secret"]
    assert "retry_blocked_task" in row
    assert "USER-ONLY" in row
    assert "closes matching `missing_credential` escalations" in row
    assert "only a task still blocked" in row
    assert "resumes no task" not in row
    assert "`retry_blocked_task` (one call each)" in AUTO_DECIDE_PREAMBLE


def test_split_into_scope_row_matches_the_scope_gates():
    # create_scope is refused outside program mode and while any scope is
    # live; scopes are program milestones only (X01).
    row = AUTO_DECIDE_ROWS["split_into_scope"]
    assert "`create_scope` → `create_task`" not in row
    assert "chained with `depends_on` (no scope)" in row
    assert "refused outside program mode and while any scope is live" in row


def test_escalate_blocker_row_says_a_prerequisite_card_cannot_park_a_task():
    """R03: a Manager decision that changes nothing hands the next Backlog
    prerequisite card to the user (backend/app/tasks/escalation.py)."""
    row = AUTO_DECIDE_ROWS["escalate_blocker"]
    assert "cannot park it" in row
    assert "hands the next card to the user" in row
