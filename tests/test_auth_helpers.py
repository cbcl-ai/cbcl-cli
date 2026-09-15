"""Account-label extraction; login/quota regressions live in test_auth_verification.py."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch


from src.auth_helpers import (
    get_auth_account_info,
)


# ─── verify_claude_in_container ─────────────────────────────────────


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
