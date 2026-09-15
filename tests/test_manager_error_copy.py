"""FIX M1(a)/M2 (blink-resilience) — classified Manager error copy +
usage-limit reset UX.

Contract under test (``manager_controller``):

* ``_classified_error_copy`` returns actionable "your message was not
  lost" copy for the account/provider classes, names the parsed reset
  time for a usage limit, and returns ``None`` for unfamiliar classes
  (the raw error stays verbatim-debuggable);
Quota scheduling and verified recovery are covered in test_quota_recovery.py.
"""

from __future__ import annotations


from src.orchestrator.error_classifier import classify_error
from src.orchestrator.manager_controller import (
    _classified_error_copy,
)


# ---------------------------------------------------------------------------
# _classified_error_copy
# ---------------------------------------------------------------------------


def test_overload_copy_is_actionable():
    remedy = classify_error("API Error: 529 Overloaded")
    copy = _classified_error_copy(remedy, "raw")
    assert copy is not None
    assert "overloaded" in copy.lower()
    assert "not lost" in copy


def test_rate_limit_copy_is_actionable():
    remedy = classify_error("API Error 429 rate limit exceeded")
    copy = _classified_error_copy(remedy, "raw")
    assert "429" in copy
    assert "not lost" in copy


def test_connection_lost_copy_is_actionable():
    remedy = classify_error("connection reset by peer")
    assert "Check the live board before retrying" in _classified_error_copy(
        remedy, "raw"
    )


def test_auth_failed_copy_names_the_fix():
    remedy = classify_error("401 unauthorized")
    assert "auth" in _classified_error_copy(remedy, "raw").lower()


def test_usage_limit_copy_names_reset_time():
    remedy = classify_error(
        "Claude usage limit reached. Your limit resets in 2 hours",
    )
    copy = _classified_error_copy(remedy, "raw")
    assert "usage" in copy.lower()
    assert "UTC" in copy  # the parsed reset time is named


def test_usage_limit_copy_without_parseable_reset():
    remedy = classify_error("Claude usage limit reached.")
    copy = _classified_error_copy(remedy, "raw")
    assert "the next reset" in copy


def test_unknown_class_returns_none():
    remedy = classify_error("some totally novel explosion")
    assert _classified_error_copy(remedy, "raw") is None


def test_execution_cleanup_failure_preserves_conversation_and_names_runtime():
    error = "Task-scoped container cancellation failed"
    copy = _classified_error_copy(classify_error(error), error)
    assert "cleanup could not be confirmed" in copy
    assert "conversation has been preserved" in copy
    assert "office runtime" in copy
    assert "too large" not in copy
