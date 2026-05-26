"""Tests for ``parse_mcp_list``.

Locks in the two ``claude mcp list`` output formats we've seen in
the wild so a future CLI bump that flips between glyph / dash
style doesn't silently break the Connectors page again.

The bug being prevented: when a server line couldn't be classified
the parser returned ``status="unknown"``, the frontend's connectors
page had filter buckets for ``connected`` / ``needs_auth`` /
``failed`` only, and the unknown servers vanished from the UI
even though they were live in the container.
"""
from __future__ import annotations

from src._handlers._mcp_listing import parse_mcp_list


def test_old_format_dash_separator() -> None:
    """Pre-2025 CLI: ``name: url - status``."""
    text = (
        "claude.ai Linear: https://mcp.linear.app/sse (sse) - Connected\n"
        "perplexity: npx -y @perplexity-ai/mcp-server (stdio) - Connected\n"
    )
    out = parse_mcp_list(text)
    assert len(out) == 2
    assert out[0]["name"] == "claude.ai Linear"
    assert out[0]["status"] == "connected"
    assert out[0]["transport"] == "sse"
    assert out[0]["source"] == "claude.ai"
    assert out[1]["name"] == "perplexity"
    assert out[1]["status"] == "connected"
    assert out[1]["transport"] == "stdio"
    assert out[1]["source"] == "local"


def test_new_format_check_mark_no_dash() -> None:
    """Current CLI: ``name: url (transport) ✓ Connected``.

    Without dash-separator handling this regressed to status=
    "unknown" and the UI dropped the entry.
    """
    text = (
        "Checking MCP server health...\n"
        "\n"
        "perplexity: npx -y @perplexity-ai/mcp-server (stdio) ✓ Connected\n"
        "linear: https://mcp.linear.app/sse (HTTP) ✓ Connected\n"
    )
    out = parse_mcp_list(text)
    names = {s["name"]: s for s in out}
    assert set(names) == {"perplexity", "linear"}
    assert names["perplexity"]["status"] == "connected"
    assert names["perplexity"]["transport"] == "stdio"
    assert names["linear"]["status"] == "connected"
    # Case-insensitive transport — newer CLIs print "(HTTP)" capped.
    assert names["linear"]["transport"] == "http"


def test_failed_server_with_cross_glyph() -> None:
    text = (
        "broken-one: https://bad.example.com (HTTP) ✗ Failed to connect\n"
    )
    out = parse_mcp_list(text)
    assert len(out) == 1
    assert out[0]["status"] == "failed"


def test_needs_auth_outranks_failed_in_compound_line() -> None:
    """A line that mentions both ``failed`` and ``needs authentication``
    should bucket as needs_auth — the user can fix the auth, not the
    transport. CLI sometimes prints ``✗ Failed: needs authentication``."""
    text = (
        "notion: https://mcp.notion.com/mcp (HTTP) ✗ Failed: "
        "needs authentication\n"
    )
    out = parse_mcp_list(text)
    assert out[0]["status"] == "needs_auth"


def test_unknown_status_keeps_server_in_list() -> None:
    """A server line with no recognisable status keyword (CLI version
    we haven't seen yet) must still surface in the parsed list so the
    UI can show it under "Other"."""
    text = (
        "mystery: npx mystery-server (stdio) [waiting...]\n"
    )
    out = parse_mcp_list(text)
    assert len(out) == 1
    assert out[0]["status"] == "unknown"
    assert out[0]["transport"] == "stdio"
    assert out[0]["name"] == "mystery"


def test_url_strips_transport_tag_and_status_glyph() -> None:
    """The parsed ``url`` should not contain the ``(stdio)`` tag, the
    ✓ glyph, or the status keyword — just the URL / command."""
    text = "linear: https://mcp.linear.app/sse (sse) ✓ Connected\n"
    out = parse_mcp_list(text)
    assert out[0]["url"] == "https://mcp.linear.app/sse"


def test_url_strips_in_old_format() -> None:
    text = "perplexity: npx -y @perplexity-ai/mcp-server (stdio) - Connected\n"
    out = parse_mcp_list(text)
    assert out[0]["url"] == "npx -y @perplexity-ai/mcp-server"


def test_ansi_color_codes_stripped() -> None:
    """Some CLI builds emit color escapes around the status word."""
    text = (
        "linear: https://mcp.linear.app/sse (HTTP) "
        "\x1b[32m✓ Connected\x1b[0m\n"
    )
    out = parse_mcp_list(text)
    assert out[0]["status"] == "connected"
    assert "\x1b" not in out[0]["url"]


def test_empty_input_returns_empty_list() -> None:
    assert parse_mcp_list("") == []
    assert parse_mcp_list("Checking MCP server health...\n\n") == []


def test_blank_name_line_dropped() -> None:
    """Defensive: a malformed line with empty name should drop, not
    crash."""
    out = parse_mcp_list(": something\n")
    assert out == []


def test_source_prefix_detection() -> None:
    text = (
        "claude.ai Slack: https://mcp.slack.com/v1 (HTTP) ✓ Connected\n"
        "custom-thing: https://example.com (HTTP) ✓ Connected\n"
    )
    out = parse_mcp_list(text)
    by_name = {s["name"]: s for s in out}
    assert by_name["claude.ai Slack"]["source"] == "claude.ai"
    assert by_name["custom-thing"]["source"] == "local"
