"""Optional verification-plan guidance delivered with the full task contract."""

from __future__ import annotations

import json


def render_verification_plan(plan: dict | None) -> list[str]:
    if plan is None:
        return []
    return [
        "## Structured verification plan",
        json.dumps(plan, ensure_ascii=False, sort_keys=True),
        "This supplements every prose criterion; it cannot waive required checks. "
        "Run only checks owned by your current phase, inspecting other owners' evidence. "
        "Record each owned check through `record_verification_evidence` with the actual "
        "source identity, relevant-input fingerprint, references and outcome. Backend "
        "binds the receipt to this task/cycle/attempt and approved plan. Independent "
        "checks need their designated owner; automation PASS needs a matching succeeded "
        "tracked operation in its declared phase. Never substitute a narrated PASS.",
        "Automation uses current-cycle freshness so resuming observation cannot force "
        "a duplicate run. Current-attempt freshness applies to executor/reviewer inspection.",
        "Use one shared acceptance input manifest/fingerprint across all required checks: "
        "delivered artifacts plus relevant source/environment identities. Each check retains "
        "its own method/scope; its tracked operation and receipt use that same fingerprint.",
        "Before completion, inspect `get_verification_status` for the current input "
        "fingerprint. Current-attempt, age and source changes can invalidate evidence. "
        "When closing Done, include that fingerprint in verdict.verification_input_fingerprint. "
        "Ask tasks still close directly with their answer; this adds no review round. "
        "Missing/failed/partial required evidence cannot pass. If the plan is incompatible "
        "with the current brief, surface the conflict; do not silently omit either.",
    ]
