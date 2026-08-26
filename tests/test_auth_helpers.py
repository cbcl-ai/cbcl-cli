"""Unit tests for ``src.auth_helpers``.

Both helpers are pure adapters around ``subprocess.run`` against
``docker exec`` — they take a container name string and return a
bool / str|None. The behaviour we lock down here:

* Exit code 0 → authenticated; non-zero → not authenticated.
* JSON parse / missing fields → graceful None (the helper says
  "I don't know who's logged in" rather than raising — the auth
  check is informational, the boolean from ``verify`` is the
  authoritative answer).
* Subprocess timeout / exec failure → False (for verify) or None
  (for account info), with a debug log line so operators can
  trace what failed.

We mock ``subprocess.run`` because spinning up a real container
in unit tests is overkill — all we're testing is the adapter
shape, not docker.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

from src.auth_helpers import (
    get_auth_account_info,
    verify_claude_in_container,
)


# ─── verify_claude_in_container ─────────────────────────────────────


def test_verify_returns_true_on_zero_exit() -> None:
    """``claude --print`` exited 0 → token is good."""
    mock_result = MagicMock(spec=subprocess.CompletedProcess)
    mock_result.returncode = 0
    with patch("src.auth_helpers.subprocess.run", return_value=mock_result):
        assert verify_claude_in_container("cbcl-office-x") is True


def test_verify_returns_false_on_nonzero_exit() -> None:
    """Non-zero exit → token missing or invalid. We don't try to
    distinguish 'no token' from 'expired token' here because the
    user-facing fix is the same: ``cbcl auth``."""
    mock_result = MagicMock(spec=subprocess.CompletedProcess)
    mock_result.returncode = 1
    with patch("src.auth_helpers.subprocess.run", return_value=mock_result):
        assert verify_claude_in_container("cbcl-office-x") is False


def test_verify_returns_false_on_timeout() -> None:
    """Subprocess timed out → return False, don't raise. The
    30s timeout matches a wedged container or hung CLI."""
    err = subprocess.TimeoutExpired(cmd="docker exec ...", timeout=30)
    with patch("src.auth_helpers.subprocess.run", side_effect=err):
        assert verify_claude_in_container("cbcl-office-x") is False


def test_verify_returns_false_on_generic_exception() -> None:
    """Docker daemon down, container missing, etc — broad catch
    keeps the bool contract intact for callers."""
    with patch(
        "src.auth_helpers.subprocess.run",
        side_effect=OSError("docker not found"),
    ):
        assert verify_claude_in_container("cbcl-office-x") is False


# ─── get_auth_account_info ─────────────────────────────────────────


def test_account_info_with_tier() -> None:
    """Both subscription type and rate-limit tier present → returns
    a friendly label. Powers the "Connected as Claude Max
    (default_claude_max_20x)" line in the wizard's success state."""
    creds = {
        "claudeAiOauth": {
            "subscriptionType": "max",
            "rateLimitTier": "default_claude_max_20x",
        }
    }
    mock_result = MagicMock(spec=subprocess.CompletedProcess)
    mock_result.returncode = 0
    mock_result.stdout = json.dumps(creds)
    with patch("src.auth_helpers.subprocess.run", return_value=mock_result):
        assert (
            get_auth_account_info("cbcl-office-x")
            == "Claude Max (default_claude_max_20x)"
        )


def test_account_info_without_tier() -> None:
    """Free / Pro accounts may have no rate-limit tier — fall back
    to just the subscription type."""
    creds = {"claudeAiOauth": {"subscriptionType": "pro"}}
    mock_result = MagicMock(spec=subprocess.CompletedProcess)
    mock_result.returncode = 0
    mock_result.stdout = json.dumps(creds)
    with patch("src.auth_helpers.subprocess.run", return_value=mock_result):
        assert get_auth_account_info("cbcl-office-x") == "Claude Pro"


def test_account_info_returns_none_when_file_missing() -> None:
    """``cat /home/agent/.claude/.credentials.json`` returned non-zero
    (file doesn't exist, perm denied, etc.) → None. The caller's
    rendering renders this as a dash rather than crashing."""
    mock_result = MagicMock(spec=subprocess.CompletedProcess)
    mock_result.returncode = 1
    mock_result.stdout = ""
    with patch("src.auth_helpers.subprocess.run", return_value=mock_result):
        assert get_auth_account_info("cbcl-office-x") is None


def test_account_info_returns_none_on_invalid_json() -> None:
    """File exists but isn't valid JSON (truncated write, manual
    edit, older CLI). None > raising, since the check is purely
    informational."""
    mock_result = MagicMock(spec=subprocess.CompletedProcess)
    mock_result.returncode = 0
    mock_result.stdout = "<<not json>>"
    with patch("src.auth_helpers.subprocess.run", return_value=mock_result):
        assert get_auth_account_info("cbcl-office-x") is None


def test_account_info_returns_none_when_subscription_missing() -> None:
    """``claudeAiOauth`` block exists but ``subscriptionType`` is
    absent — return None so the UI renders "–" rather than the
    confusing "Claude Unknown" string. The auth pass itself
    succeeds via ``verify_claude_in_container``; account label is
    purely informational, and a missing field shouldn't be
    surfaced as a state."""
    creds = {"claudeAiOauth": {"otherField": "x"}}
    mock_result = MagicMock(spec=subprocess.CompletedProcess)
    mock_result.returncode = 0
    mock_result.stdout = json.dumps(creds)
    with patch("src.auth_helpers.subprocess.run", return_value=mock_result):
        assert get_auth_account_info("cbcl-office-x") is None


def test_account_info_returns_none_on_subprocess_exception() -> None:
    """Docker daemon down → broad catch returns None."""
    with patch(
        "src.auth_helpers.subprocess.run",
        side_effect=OSError("docker not found"),
    ):
        assert get_auth_account_info("cbcl-office-x") is None
