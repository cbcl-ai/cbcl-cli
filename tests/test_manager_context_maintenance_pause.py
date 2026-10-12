"""installer-docs-7/8: the Manager's turn context names a maintenance pause.

A pause left on closes the office's admission on its cbcl machine until an
operator reopens it there. Without it in the context, a Manager asked why
nothing starts could only guess, retry or reassign work.
"""

from __future__ import annotations

import pytest

from src.config_sync.sync_service import ConfigStore
from src.orchestrator.manager_context import build_dynamic_context

WS = "workstream:6f1a2b3c-0000-4000-8000-000000000001"
PAUSE = {
    # As the backend sends it: an ISO time to the microsecond.
    "paused_since": "2026-10-06T08:30:06.123456+00:00",
    "scopes": ["office"],
    "reopen_command": "cbcl maintenance disable --office-id o-1",
}


@pytest.mark.parametrize("context_key", [WS, "general_chat"])
def test_every_turn_names_the_pause_and_the_command(context_key) -> None:
    text = build_dynamic_context(
        context_key,
        {"workstream_name": "Reports", "maintenance_pause": PAUSE},
        ConfigStore(),
    )
    assert "## Office paused on its cbcl machine" in text
    # The start time to the minute, in words the Manager can repeat.
    assert (
        "This office has been in a maintenance pause since 2026-10-06 08:30 UTC: "
        "no new task" in text
    )
    assert "`cbcl maintenance disable --office-id o-1`" in text
    assert "You cannot lift it" in text


@pytest.mark.parametrize(
    ("since", "expected"),
    [
        # Another offset is shown in UTC; a time without one is read as UTC.
        ("2026-10-06T10:30:59+02:00", "since 2026-10-06 08:30 UTC: "),
        ("2026-10-06T08:30", "since 2026-10-06 08:30 UTC: "),
        ("2026-10-06T08:30:00Z", "since 2026-10-06 08:30 UTC: "),
    ],
)
def test_the_start_time_is_shown_in_utc_to_the_minute(since, expected) -> None:
    text = build_dynamic_context(
        WS, {"maintenance_pause": {**PAUSE, "paused_since": since}}, ConfigStore()
    )
    assert f"This office has been in a maintenance pause {expected}" in text


# An older cbcl reports no start time; an unreadable one is left out (the
# last one is out of range once moved to UTC).
@pytest.mark.parametrize(
    "since", [None, "", "yesterday", 1790000000, "0001-01-01T00:00+05:00"]
)
def test_a_pause_without_a_readable_start_time_has_none(since) -> None:
    text = build_dynamic_context(
        WS,
        {"maintenance_pause": {**PAUSE, "paused_since": since}},
        ConfigStore(),
    )
    assert "This office is in a maintenance pause: no new task" in text
    assert "maintenance pause since" not in text


@pytest.mark.parametrize(
    "pause",
    [None, "paused", {}, {"reopen_command": " "}, {"reopen_command": 5}],
)
def test_no_section_when_open_absent_or_malformed(pause) -> None:
    context = {} if pause is None else {"maintenance_pause": pause}
    text = build_dynamic_context(WS, context, ConfigStore())
    assert "Office paused on its cbcl machine" not in text
