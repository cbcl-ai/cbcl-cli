"""Inbox escalation cannot turn a pure dispatch-health alert into a hold."""

from types import SimpleNamespace

import pytest

from src.backend_client import _is_dispatch_diagnostic
from tests.backend_boundary import import_backend


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
        # The backend ager stamps a Manager-routed finding when it re-pokes,
        # hands it to the user or re-pokes after a reconnect. Those stamps
        # record the ager's bookkeeping; they must not turn a pure
        # dispatch-health finding into a hold on the task it reports.
        pytest.param(
            diagnostic(payload={"sweeper_signals": {"stuck_ready": {}}, **stamps}),
            True,
            id=f"ager-stamped-{'-'.join(stamps)}",
        )
        for stamps in (
            {"ager_re_poked_at": "2026-09-25T10:00:00"},
            {"ager_user_escalated_at": "2026-09-25T11:00:00"},
            {"ager_reconnect_repoked_at": "2026-09-25T09:00:00"},
            {
                "ager_re_poked_at": "2026-09-25T10:00:00",
                "ager_user_escalated_at": "2026-09-25T11:00:00",
            },
        )
    ],
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
            ("ager_re_poked_at_typo", "2026-09-25T10:00:00"),
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
    # Fails closed in the monorepo; skips only in the standalone mirror (X44).
    is_dispatch_diagnostic = import_backend(
        "app.tasks.blocker_requests"
    ).is_dispatch_diagnostic

    assert is_dispatch_diagnostic(SimpleNamespace(**row)) is expected
    assert _is_dispatch_diagnostic(row) is expected


def test_payload_allowance_matches_the_backend():
    """The daemon accepts exactly the payload keys the backend does: the
    finding's own keys plus the ager's bookkeeping stamps."""
    from src.backend_client import _DISPATCH_DIAGNOSTIC_PAYLOAD_KEYS

    backend = import_backend("app.tasks.blocker_requests")
    assert _DISPATCH_DIAGNOSTIC_PAYLOAD_KEYS == (
        backend._DISPATCH_DIAGNOSTIC_PAYLOAD_KEYS | backend.AGER_PAYLOAD_STAMPS
    )
