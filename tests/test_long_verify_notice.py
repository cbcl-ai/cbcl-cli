"""LONG-VERIFY chat-notice tests (incident 2026-07-16 follow-up).

Contract under test:

* ``handlers.claim_due_verify_notice`` — the once-per-threshold claim the
  planner heartbeat runs on every verify pulse: a notice fires exactly once
  at 15m (900s) and once more at 30m (1800s) per consult, never below the
  threshold (a consult that finishes sooner never crosses one — the
  heartbeat loop is consult-owned: it exits, and is cancelled, the moment
  its consult ends — AREA-2, verify turn-end incident 2026-07-17), and a
  pulse that lands past SEVERAL unsent thresholds sends only the highest
  (a stale "(15m)" notice at minute 31 would be noise).
* ``handlers.build_long_verify_notice`` — the user-facing copy (pinned),
  including the cumulative "across N attempts" form a refired verify uses
  so the elapsed copy stays honest instead of resetting per attempt.
* ``backend_client.post_system_chat_notice`` — the REST delivery helper:
  POSTs a ``role='system'`` row to the messages endpoint with the Company
  Token bearer; ``True`` only on 201; swallows transport errors (a missed
  notice must never break the heartbeat).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.handlers import (
    VERIFY_NOTICE_THRESHOLDS_SECONDS,
    build_long_verify_notice,
    claim_due_verify_notice,
)


# ---------------------------------------------------------------------------
# Threshold claim logic
# ---------------------------------------------------------------------------


def test_thresholds_are_15_and_30_minutes():
    assert VERIFY_NOTICE_THRESHOLDS_SECONDS == (900, 1800)


def test_no_notice_below_first_threshold():
    """A fast consult never produces a notice — every pulse below 15m is a
    no-op and stamps nothing on the marker."""
    marker: dict = {}
    for elapsed in (75, 150, 300, 600, 899.9):
        assert claim_due_verify_notice(elapsed, marker) is None
    assert marker == {}


def test_notice_fires_once_per_threshold_over_a_pulse_sequence():
    """Simulate the heartbeat's 75s pulse cadence across a 35-minute verify:
    exactly one 15m notice and one 30m notice, nothing else."""
    marker: dict = {}
    fired: list[int] = []
    elapsed = 0.0
    while elapsed < 2100:  # 35 minutes of pulses
        elapsed += 75
        due = claim_due_verify_notice(elapsed, marker)
        if due is not None:
            fired.append(due)
    assert fired == [900, 1800]


def test_repeat_claim_at_same_elapsed_is_idempotent():
    marker: dict = {}
    assert claim_due_verify_notice(900, marker) == 900
    assert claim_due_verify_notice(900, marker) is None
    assert claim_due_verify_notice(1200, marker) is None
    assert claim_due_verify_notice(1800, marker) == 1800
    assert claim_due_verify_notice(1800, marker) is None
    assert claim_due_verify_notice(9999, marker) is None


def test_skip_ahead_sends_only_the_highest_threshold():
    """A pulse landing past both unsent thresholds (event-loop stall,
    suspend/resume) sends ONLY the 30m notice; the 15m one is claimed
    silently — a stale "(15m)" at minute 31 would be noise."""
    marker: dict = {}
    assert claim_due_verify_notice(2000, marker) == 1800
    # Both thresholds are claimed — nothing further ever fires.
    assert claim_due_verify_notice(2000, marker) is None
    assert marker.get("_verify_notice_900") is True
    assert marker.get("_verify_notice_1800") is True


def test_marker_flags_are_per_consult():
    """The sent-flags live on the consult's own stash entry, so a NEW
    consult (fresh marker) gets a fresh 15m/30m clock."""
    first: dict = {}
    second: dict = {}
    assert claim_due_verify_notice(900, first) == 900
    assert claim_due_verify_notice(900, second) == 900


# ---------------------------------------------------------------------------
# Copy pins
# ---------------------------------------------------------------------------


def test_notice_copy_15m():
    notice = build_long_verify_notice(900)
    assert "Scope verification is still running (15m)" in notice
    assert "large scope or constrained resources" in notice
    assert "it will report when done" in notice


def test_notice_copy_30m():
    notice = build_long_verify_notice(1800)
    assert "Scope verification is still running (30m)" in notice
    assert "large scope or constrained resources" in notice


def test_notice_copy_uses_cumulative_elapsed_minutes():
    """AREA-2 (verify turn-end incident 2026-07-17): the heartbeat passes
    CUMULATIVE minutes (threaded through refires via
    ``_verify_first_started``) so the copy reports honest wall-clock
    instead of the bare threshold."""
    notice = build_long_verify_notice(900, elapsed_minutes=17)
    assert "Scope verification is still running (17m)" in notice


def test_notice_copy_names_attempts_on_a_refire():
    """A refired attempt (attempts > 1) names the total and the attempt
    count — "(15m)" at minute 45 of attempt 3 would be a lie."""
    notice = build_long_verify_notice(1800, elapsed_minutes=45, attempts=3)
    assert "still running (~45m across 3 attempts)" in notice
    assert "large scope or constrained resources" in notice
    assert "it will report when done" in notice


def test_notice_is_a_progress_notice_not_a_failure():
    """The copy must never read as an error/failure — the verify-silence
    posture for failures (sweeper-owned) is a separate channel."""
    for threshold in VERIFY_NOTICE_THRESHOLDS_SECONDS:
        for notice in (
            build_long_verify_notice(threshold).lower(),
            build_long_verify_notice(
                threshold, elapsed_minutes=45, attempts=3,
            ).lower(),
        ):
            for banned in ("fail", "error", "stuck", "stalled", "wedged"):
                assert banned not in notice


# ---------------------------------------------------------------------------
# REST delivery helper (backend_client.post_system_chat_notice)
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _FakeClient:
    """Minimal httpx.AsyncClient stand-in capturing the POST call."""

    def __init__(self, status_code: int = 201, raise_exc: Exception | None = None):
        self._sc = status_code
        self._raise = raise_exc
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        if self._raise is not None:
            raise self._raise
        self.calls.append((url, kw))
        return _FakeResp(self._sc)


@pytest.mark.asyncio
async def test_post_system_chat_notice_posts_system_row_and_returns_true():
    from src.backend_client import post_system_chat_notice

    fake = _FakeClient(201)
    with patch("httpx.AsyncClient", return_value=fake):
        ok = await post_system_chat_notice(
            "http://x", "oid-1", "workstream:ws-1",
            "Scope verification is still running (15m) — …",
            "cbcl_co_token",
            action_payload={"kind": "planner_consulted",
                            "notice": "verify_progress"},
        )
    assert ok is True
    assert len(fake.calls) == 1
    url, kw = fake.calls[0]
    assert url == "http://x/api/offices/oid-1/messages"
    body = kw["json"]
    assert body["role"] == "system"
    assert body["context_key"] == "workstream:ws-1"
    assert body["action_payload"]["kind"] == "planner_consulted"
    # Company Token bearer rides the request (HYBRID route).
    assert kw["headers"] == {"Authorization": "Bearer cbcl_co_token"}


@pytest.mark.asyncio
async def test_post_system_chat_notice_false_on_non_201():
    from src.backend_client import post_system_chat_notice

    with patch("httpx.AsyncClient", return_value=_FakeClient(503)):
        ok = await post_system_chat_notice(
            "http://x", "oid-1", "general_chat", "n", None,
        )
    assert ok is False


@pytest.mark.asyncio
async def test_post_system_chat_notice_swallows_transport_errors():
    from src.backend_client import post_system_chat_notice

    with patch(
        "httpx.AsyncClient",
        return_value=_FakeClient(raise_exc=ConnectionError("down")),
    ):
        ok = await post_system_chat_notice(
            "http://x", "oid-1", "general_chat", "n", None,
        )
    assert ok is False


# ---------------------------------------------------------------------------
# Heartbeat wiring pin (source-level)
# ---------------------------------------------------------------------------


def test_heartbeat_wires_the_notice_for_verify_mode_only():
    """The claim runs inside the planner heartbeat's pulse branch, gated on
    ``mode == "verify"`` — a source-level pin so a refactor can't silently
    disconnect the notice from the heartbeat timer."""
    import inspect

    import src.handlers as handlers_mod

    source = inspect.getsource(handlers_mod)
    assert "claim_due_verify_notice(" in source
    assert "build_long_verify_notice(" in source
    assert "post_system_chat_notice" in source
