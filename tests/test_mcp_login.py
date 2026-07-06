"""Unit tests for the pure helpers in ``_handlers._mcp_login``.

The stateful start/complete flow needs a live container + PTY (exercised
manually against the office container), but the parsing helpers — ANSI
stripping, URL extraction, name validation, success/failure detection —
are pure and locked here so a copy/regex tweak can't silently break the
classification.
"""
from __future__ import annotations

from src._handlers import _mcp_login as L


# ── name guard ──────────────────────────────────────────────────────


def test_valid_name_accepts_spaced_account_names():
    assert L._valid_name("claude.ai Google Drive")
    assert L._valid_name("sentry")


def test_valid_name_refuses_argv_hazards():
    assert not L._valid_name("")
    assert not L._valid_name("-rf")
    assert not L._valid_name("evil\nname")


# ── ANSI strip + URL extraction ─────────────────────────────────────


def test_strip_removes_ansi_and_cr():
    raw = "\x1b[1G\x1b[0JOr paste the redirect URL here: \x1b[33Ghttps://x\r\n"
    assert "\x1b" not in L._strip(raw)
    assert "\r" not in L._strip(raw)
    assert "https://x" in L._strip(raw)


def test_url_regex_matches_connector_oauth():
    line = (
        "  https://mcp.sentry.dev/oauth/authorize?response_type=code"
        "&client_id=abc&code_challenge=xyz\x1b[0m"
    )
    m = L._URL_RE.search(L._strip(line))
    assert m is not None
    # Stops at the ANSI reset (stripped) / whitespace, keeps the query.
    assert m.group(0).startswith("https://mcp.sentry.dev/oauth/authorize")
    assert "\x1b" not in m.group(0)


def test_first_meaningful_line_skips_urls():
    text = 'No MCP server named "nope".\nhttps://example.com/x\n'
    assert L._first_meaningful_line(text) == 'No MCP server named "nope".'


# ── classification markers ──────────────────────────────────────────


def test_paste_prompt_marker_present():
    combined = L._strip("Or paste the redirect URL here:").lower()
    assert any(m in combined for m in L._PASTE_PROMPT_MARKERS)


def test_account_marker_present():
    combined = (
        "Once authorized on claude.ai, the connector will be available "
        "the next time you start Claude Code."
    ).lower()
    assert any(m in combined for m in L._ACCOUNT_MARKERS)


def test_success_vs_failure_markers_are_disjoint_on_real_output():
    success_out = "Authentication successful. Connected to sentry.".lower()
    assert any(m in success_out for m in L._SUCCESS_MARKERS)
    assert "couldn't complete" not in success_out

    fail_out = (
        "Couldn't complete authentication for \"sentry\": OAuth state "
        "mismatch - possible CSRF attack"
    ).lower()
    assert "couldn't complete" in fail_out
