"""T5.4.5 — rework-cap policy string pins (cross-ref Phase 1 F24).

The rework cap lived in 5 places with 3 behaviors (F7/F24). Phase 1 fixed the
daemon circuit breaker to escalate-at-cap (never rubber-stamp). These pins make
the chosen policy un-driftable across reviewer-facing surfaces.
"""
from __future__ import annotations

from src.config_sync.claude_md_content import (
    AUDITOR_CLAUDE_MD,
    MANAGER_ASSISTANT_CLAUDE_MD,
)
from src.orchestrator.worker_prompt import build_worker_prompt


def _reviewer_prompt() -> str:
    return build_worker_prompt({
        "task_id": "00000000-0000-0000-0000-000000000001",
        "readable_id": "RC-001.T05",
        "title": "x", "status": "review", "rework_count": 2,
        "recent_activities": [], "artifacts": [], "reviewer": "auditor",
        "assigned_agent": "dev",
        "brief": {
            "goal": "g", "context": "c", "inputs": "i",
            "output_format": "short", "acceptance_criteria": ["a"],
            "allowed_tools": [], "required_skills": [],
            "risks_and_edge_cases": "none", "verification_steps": "v",
        },
    })


def test_reviewer_block_says_escalate_not_rubber_stamp():
    p = _reviewer_prompt()
    assert "rubber-stamp" in p.lower()
    assert "escalate" in p.lower()


def test_ma_playbook_escalates_at_cap():
    assert "Rework cap" in MANAGER_ASSISTANT_CLAUDE_MD
    assert "never auto-approve" in MANAGER_ASSISTANT_CLAUDE_MD.lower() or \
        "do NOT" in MANAGER_ASSISTANT_CLAUDE_MD


def test_auditor_playbook_has_escalate_at_cap_guidance():
    # T5.4.5: the Auditor is the default reviewer for MA-assigned tasks, so its
    # playbook must POSITIVELY carry the escalate-at-cap policy (not just lack
    # an auto-approve instruction). Mirrors test_ma_playbook_escalates_at_cap.
    low = AUDITOR_CLAUDE_MD.lower()
    assert "rework cap" in low
    assert "escalate" in low


def test_no_reviewer_surface_instructs_silent_auto_approve():
    # Negative guard across every reviewer-facing surface — the reviewer block,
    # the MA playbook, AND the Auditor playbook. (The Auditor playbook currently
    # has no "auto-approv" mention, so the scan is a no-op there today; it
    # future-proofs against one being added. Its POSITIVE coverage is the test
    # above. The Manager review section is covered by T5.2.3's backend test.)
    for surface in (_reviewer_prompt(), MANAGER_ASSISTANT_CLAUDE_MD,
                    AUDITOR_CLAUDE_MD):
        low = surface.lower()
        idx = 0
        while (idx := low.find("auto-approv", idx)) != -1:
            window = low[max(0, idx - 30): idx]
            assert any(neg in window for neg in ("never", "not ", "n't", "silent", "worse")), (
                f"'auto-approve' used as an instruction near: ...{low[idx-30:idx+20]}..."
            )
            idx += 1
