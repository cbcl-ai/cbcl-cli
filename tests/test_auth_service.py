"""Unit tests for the Phase 3 ``auth_service`` module.

Coverage targets:

* ``start_auth_flow`` — happy path returns a session_id + auth_url
  in the right shape; refuses empty container_name.
* ``extract_auth_code`` — accepts the three formats
  (``code#state``, full URL, raw code).
* PKCE pair correctness (challenge is sha256 of verifier, base64
  url-safe, no padding).
* ``complete_auth_flow`` — looks up the session, exchanges via
  the mocked subprocess, writes credentials, returns the right
  shape on success and on each failure mode (rate limit, missing
  session, exchange error).
* Session TTL eviction kicks in.

We mock ``subprocess.run`` because the real flow shells out to
``docker exec node`` against platform.claude.com — neither
available nor desirable in unit tests.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import time
from unittest.mock import MagicMock, patch

import pytest

from src import auth_service
from src.auth_service import (
    AUTH_ENDPOINT,
    REDIRECT_URI,
    SCOPES,
    complete_auth_flow,
    extract_auth_code,
    start_auth_flow,
)


# ─── extract_auth_code ─────────────────────────────────────────────


def test_extract_code_handles_code_hash_state() -> None:
    """``platform.claude.com`` shows the user a string in the
    form ``CODE#STATE``. The user copies the whole thing."""
    assert extract_auth_code("abc123#xyz789") == "abc123"


def test_extract_code_handles_full_callback_url() -> None:
    """Some users grab the URL from the address bar instead of
    the displayed code. Pull ``code=`` out of the query string."""
    url = "https://platform.claude.com/oauth/code/callback?code=abc123&state=xyz"
    assert extract_auth_code(url) == "abc123"


def test_extract_code_handles_raw_code() -> None:
    assert extract_auth_code("just-the-code") == "just-the-code"


def test_extract_code_returns_none_on_empty() -> None:
    """Empty input → None so the caller knows the user submitted
    nothing rather than passing an empty string downstream."""
    assert extract_auth_code("") is None
    assert extract_auth_code("   ") is None
    assert extract_auth_code(None) is None  # type: ignore[arg-type]


def test_extract_code_strips_whitespace() -> None:
    assert extract_auth_code("  abc#xyz  ") == "abc"


# ─── start_auth_flow ───────────────────────────────────────────────


def test_start_returns_session_and_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: returns ``auth_url`` (with all the right
    parameters) plus a session_id we can look up later."""
    auth_service._SESSIONS.clear()
    result = start_auth_flow("cbcl-office-test")
    assert "auth_url" in result
    assert "session_id" in result

    assert result["auth_url"].startswith(AUTH_ENDPOINT + "?")
    # All required PKCE-flow params present.
    for required in (
        "client_id=", "response_type=code", "code_challenge_method=S256",
        "code_challenge=", "state=", "scope=",
    ):
        assert required in result["auth_url"]
    # Redirect URI is URL-encoded so check the raw substring.
    assert "redirect_uri=" in result["auth_url"]

    sess = auth_service._SESSIONS[result["session_id"]]
    assert sess.container_name == "cbcl-office-test"
    assert len(sess.code_verifier) > 30  # base64-encoded 32 bytes
    assert len(sess.state) > 30


def test_start_refuses_empty_container_name() -> None:
    """No container = no place to write credentials. The RPC
    handler converts this into a 503."""
    with pytest.raises(ValueError):
        start_auth_flow("")


def test_pkce_challenge_is_sha256_of_verifier() -> None:
    """Without this property the code-paste flow can't complete —
    Claude rejects the token exchange. Locks the algorithm so a
    refactor doesn't accidentally swap base64 variants or hash
    funcs."""
    auth_service._SESSIONS.clear()
    result = start_auth_flow("c")
    sess = auth_service._SESSIONS[result["session_id"]]
    expected = (
        base64.urlsafe_b64encode(
            hashlib.sha256(sess.code_verifier.encode()).digest(),
        )
        .rstrip(b"=")
        .decode()
    )
    # The challenge isn't stored on the session (only verifier is),
    # so re-derive it and assert the URL contains it.
    assert f"code_challenge={expected}" in result["auth_url"]


# ─── complete_auth_flow ────────────────────────────────────────────


def _ok_token_response(
    access: str = "tok-abc", refresh: str = "ref-xyz",
) -> dict:
    return {
        "status": 200,
        "body": json.dumps({
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": 3600,
        }),
    }


def _ok_profile_response(sub: str = "max", tier: str = "default_max_20x") -> dict:
    return {
        "status": 200,
        "body": json.dumps({
            "subscription_type": sub,
            "rate_limit_tier": tier,
        }),
    }


def _make_run_mocks(
    *responses: dict,
    creds_ok: bool = True,
    verify_ok: bool = True,
) -> list[MagicMock]:
    """Build a single side_effect list covering EVERY ``subprocess.run``
    call in a happy auth flow:

      0. Token exchange (auth_service → Node POST /v1/oauth/token).
      1. Profile fetch (auth_service → Node GET /api/oauth/profile).
      2. Credentials write (auth_service → docker exec cat).
      3. Verify (auth_helpers → docker exec claude --print).
      4. Account info (auth_helpers → docker exec cat) — only when
         verify_ok=True.

    We patch the GLOBAL ``subprocess.run`` rather than the per-
    module reference because ``auth_service`` imports
    ``subprocess`` and accesses ``subprocess.run`` lazily — the
    ``patch("src.auth_service.subprocess.run", ...)`` form
    doesn't take effect under that pattern. Patching the canonical
    name covers both auth_service and auth_helpers."""
    runs: list[MagicMock] = []
    for resp in responses:
        m = MagicMock()
        m.returncode = 0
        m.stdout = json.dumps(resp)
        m.stderr = ""
        runs.append(m)

    creds = MagicMock()
    creds.returncode = 0 if creds_ok else 1
    creds.stderr = b"" if creds_ok else b"oh no"
    runs.append(creds)

    verify = MagicMock()
    verify.returncode = 0 if verify_ok else 1
    runs.append(verify)

    if verify_ok:
        account = MagicMock()
        account.returncode = 0
        account.stdout = json.dumps({
            "claudeAiOauth": {
                "subscriptionType": "max",
                "rateLimitTier": "default_max_20x",
            }
        })
        runs.append(account)
    return runs


def test_complete_returns_authenticated_on_happy_path() -> None:
    """End-to-end success: token exchange + profile + write +
    verify all succeed. Account label comes from the verifier
    helper."""
    auth_service._SESSIONS.clear()
    started = start_auth_flow("cbcl-office-test")

    runs = _make_run_mocks(
        _ok_token_response(),
        _ok_profile_response(),
        creds_ok=True,
        verify_ok=True,
    )
    with patch("subprocess.run", side_effect=runs):
        result = complete_auth_flow(started["session_id"], "code123#state")

    assert result["authenticated"] is True
    assert "max" in (result.get("account") or "").lower()
    # Session is consumed — second attempt with the same id fails.
    assert started["session_id"] not in auth_service._SESSIONS


def test_complete_with_unknown_session_fails_clean() -> None:
    """Stale or never-existed session_id → friendly error rather
    than KeyError. Common case: session expired between Open Claude
    and code-paste."""
    auth_service._SESSIONS.clear()
    result = complete_auth_flow("00000000-0000-0000-0000-000000000000", "x")
    assert result["authenticated"] is False
    assert "expired" in result["error"].lower() or "not found" in result["error"].lower()


def test_complete_with_empty_code_fails_clean() -> None:
    """Empty paste — extract_auth_code returns None. Don't blow
    up trying to use None as a code."""
    auth_service._SESSIONS.clear()
    started = start_auth_flow("cbcl-office-test")
    result = complete_auth_flow(started["session_id"], "")
    assert result["authenticated"] is False
    assert result["error"]
    # Session was consumed even on bad input — prevents replay.
    assert started["session_id"] not in auth_service._SESSIONS


def test_complete_handles_rate_limit_error() -> None:
    """Token endpoint returns 429 → user sees rate-limit copy,
    NOT a generic "exchange failed"."""
    auth_service._SESSIONS.clear()
    started = start_auth_flow("cbcl-office-test")

    rate_limited = MagicMock()
    rate_limited.returncode = 0
    rate_limited.stdout = json.dumps({"status": 429, "body": "rate"})
    rate_limited.stderr = ""

    with patch("subprocess.run", return_value=rate_limited):
        result = complete_auth_flow(started["session_id"], "code")
    assert result["authenticated"] is False
    assert "rate limit" in result["error"].lower()


def test_complete_writes_creds_but_verify_fails() -> None:
    """If the token exchange succeeds and credentials write OK
    but the verify round-trip doesn't, surface
    ``credentials_written=True`` so the UI advises "wait and
    Recheck" rather than "ask for a new code"."""
    auth_service._SESSIONS.clear()
    started = start_auth_flow("cbcl-office-test")

    runs = _make_run_mocks(
        _ok_token_response(),
        _ok_profile_response(),
        creds_ok=True,
        verify_ok=False,
    )
    with patch("subprocess.run", side_effect=runs):
        result = complete_auth_flow(started["session_id"], "code123")

    assert result["authenticated"] is False
    assert result.get("credentials_written") is True
    assert "transient" in result["error"].lower() or "wait" in result["error"].lower()


def test_session_ttl_eviction() -> None:
    """A session older than ``SESSION_TTL_SECONDS`` is dropped on
    the next ``start`` or ``complete`` call. Without this the
    in-memory dict would grow indefinitely on a long-running
    daemon with users abandoning auth flows."""
    auth_service._SESSIONS.clear()
    started = start_auth_flow("cbcl-office-test")
    # Backdate the session past TTL.
    auth_service._SESSIONS[started["session_id"]].created_at -= (
        auth_service.SESSION_TTL_SECONDS + 60
    )
    # A new start_auth_flow call evicts.
    start_auth_flow("cbcl-other-office")
    assert started["session_id"] not in auth_service._SESSIONS
