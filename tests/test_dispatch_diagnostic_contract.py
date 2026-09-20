"""Inbox escalation cannot turn a pure dispatch-health alert into a hold."""

from types import SimpleNamespace

import pytest

from src.backend_client import _is_dispatch_diagnostic


def diagnostic(**overrides):
    return {
        "request_type": "escalate_blocker",
        "requesting_agent": "system-sweeper",
        "category": "workstream",
        "requires_user": True,
        "payload": {"sweeper_signals": {"stuck_ready": {"minutes_ready": 35}}},
        **overrides,
    }


CASES = [
    pytest.param(
        diagnostic(
            category=category,
            requires_user=requires_user,
            payload={"sweeper_signals": {signal: {}}},
        ),
        True,
        id=f"{signal}-requires-user-{requires_user}",
    )
    for signal, category in (
        ("stuck_ready", "workstream"),
        ("stuck_review", "workstream"),
        ("workstream_stall", "infrastructure"),
    )
    for requires_user in (True, False, None)
] + [
    pytest.param(
        diagnostic(payload={
            "blocker_summary": "Dispatch has not resumed.",
            "suggested_unblock": "Check dispatch health.",
            "sweeper_signals": {"stuck_ready": {}, "stuck_review": {}, "workstream_stall": {}},
        }),
        True,
        id="combined-diagnostic-with-display-text",
    ),
    *[
        pytest.param(diagnostic(category=category), False, id=f"category-{category}")
        for category in (None, "credentials", "user_input", "scope", "cost", "quality", "informational")
    ],
    *[
        pytest.param(diagnostic(requesting_agent=author), False, id=f"author-{author}")
        for author in (None, "manager", "engineer", "System-Sweeper")
    ],
    *[
        pytest.param(diagnostic(request_type=kind), False, id=f"type-{kind}")
        for kind in ("request_user_action", "request_clarification", "review_hold", "informational")
    ],
    *[
        pytest.param(
            diagnostic(payload={"sweeper_signals": {"stuck_ready": {}}, key: value}),
            False,
            id=f"mixed-payload-{key}",
        )
        for key, value in (
            ("rework_cap", True),
            ("rework_cap", False),
            ("review_recovery", {"state": "operator_reconciliation_required"}),
            ("credentials", {"missing": "service token"}),
            ("auto_created_on_block", True),
            ("auto_detected_category", "credentials"),
            ("user_action", "Approve deployment"),
        )
    ],
    *[
        pytest.param(diagnostic(payload=payload), False, id=f"invalid-payload-{index}")
        for index, payload in enumerate((None, [], {}, {"blocker_summary": "Missing signal"}))
    ],
    *[
        pytest.param(
            diagnostic(payload={"sweeper_signals": signals}),
            False,
            id=f"invalid-signals-{index}",
        )
        for index, signals in enumerate((
            None, [], {},
            {"stuck_ready": None},
            {"stuck_review": True},
            {"workstream_stall": "stalled"},
            {"auth_failure": {}},
            {"stuck_ready": {}, "auth_failure": {}},
            {"stuck_review": {}, "quality_failure": {}},
        ))
    ],
]


@pytest.mark.parametrize("row,expected", CASES)
def test_dispatch_diagnostic_contract(row, expected):
    assert _is_dispatch_diagnostic(row) is expected


@pytest.mark.parametrize("row,expected", CASES)
def test_backend_and_communicator_dispatch_diagnostic_parity(row, expected):
    # The public standalone package has no backend dependency. The monorepo
    # lane must exercise both predicates against these same adversarial rows.
    pytest.importorskip("app", reason="requires the integrated monorepo lane")
    from app.tasks.blocker_requests import is_dispatch_diagnostic

    assert is_dispatch_diagnostic(SimpleNamespace(**row)) is expected
    assert _is_dispatch_diagnostic(row) is expected
