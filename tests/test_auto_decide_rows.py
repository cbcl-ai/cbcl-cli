"""T5.3.1 — the auto-decide policy table lives in a constant injected per-type
into the synthetic turn, not in the standing Manager CLAUDE.md."""
from __future__ import annotations

from src.config_sync._auto_decide_rows import (
    AUTO_DECIDE_ROWS,
    render_auto_decide_guidance,
)
from src.config_sync.claude_md_templates._manager import MANAGER_CLAUDE_MD

def test_rows_cover_every_request_type():
    # T5.4.8: bidirectional parity with the REAL backend REQUEST_TYPES — a new
    # type without a row fails CI, and a stale row for a removed type fails too.
    from app.action_requests.schemas import REQUEST_TYPES

    assert set(AUTO_DECIDE_ROWS) == set(REQUEST_TYPES)


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
    from app.action_requests.service import _AUTO_UNBLOCK_REQUEST_TYPES

    assert "escalate_blocker" in _AUTO_UNBLOCK_REQUEST_TYPES
    idx = MANAGER_CLAUDE_MD.find("Approve ≠ done")
    assert idx != -1, "standing approve-semantics bullet not found"
    window = MANAGER_CLAUDE_MD[idx:idx + 700]
    assert "escalate_blocker" in window, (
        "approve-semantics bullet must name escalate_blocker as an auto-unblocker"
    )
    assert "auto-promote" in window.lower()


def test_standing_template_no_longer_carries_the_full_table():
    # The ~1.8k-token per-type table was moved out (T5.3.1). The standing
    # template keeps only the pointer + hard rules.
    assert "Each auto-decide synthetic\nturn carries its own policy" in MANAGER_CLAUDE_MD
    # A distinctive per-row phrase must NOT be in the standing template anymore.
    assert "Apply the **Agent Selection** 3-step audit" not in MANAGER_CLAUDE_MD
