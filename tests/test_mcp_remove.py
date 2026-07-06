"""Tests for ``_handlers._mcp.run_mcp_remove``.

Locks the name-guard contract fixed after "Remove Connector is not
working": the Claude CLI names catalog / OAuth connectors WITH SPACES
(e.g. ``claude.ai Google Drive``). An earlier build re-applied the
strict add-time ``_MCP_NAME_RE`` here, which made Remove a SILENT
NO-OP for the entire ``claude.ai *`` group. Removal now only refuses
the two genuine argv hazards — a leading ``-`` and control chars —
and still requires a running container.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from src._handlers import _mcp


def _run_remove(name: str, *, container_name: str = "cbcl-office-dev"):
    """Drive ``run_mcp_remove`` with a mocked ``subprocess.run`` +
    ``refresh_mcp_list``; return (docker_argvs, refresh_calls)."""
    calls: list[list[str]] = []
    refreshes: list[bool] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    async def fake_refresh(*, force: bool = False):
        refreshes.append(force)

    with patch.object(_mcp.subprocess, "run", fake_run):
        asyncio.run(
            _mcp.run_mcp_remove(
                {"name": name},
                container_name=container_name,
                refresh_mcp_list=fake_refresh,
            )
        )
    return calls, refreshes


# ── The core regression: spaced claude.ai names must be removable ──


def test_spaced_claude_ai_name_reaches_docker_exec():
    """``claude.ai Google Drive`` is the exact case that regressed.

    It MUST reach ``claude mcp remove`` for BOTH scopes — not get
    swallowed by a name-regex refusal.
    """
    calls, refreshes = _run_remove("claude.ai Google Drive")
    assert len(calls) == 2, "both user + local scopes should be tried"
    for argv in calls:
        assert argv[:6] == [
            "docker", "exec", "cbcl-office-dev",
            "claude", "mcp", "remove",
        ]
        assert "claude.ai Google Drive" in argv
    scopes = {argv[argv.index("-s") + 1] for argv in calls}
    assert scopes == {"user", "local"}
    # Force-refresh bypasses the debounce so the UI updates promptly.
    assert refreshes == [True]


def test_plain_name_removed():
    calls, _ = _run_remove("perplexity")
    assert len(calls) == 2
    assert all("perplexity" in argv for argv in calls)


# ── Refusals: the two genuine argv hazards + no container ──


def test_leading_dash_refused():
    """A leading ``-`` would be parsed as a CLI flag — refuse it."""
    calls, refreshes = _run_remove("-rf")
    assert calls == []
    assert refreshes == []  # early return, no refresh


def test_control_char_refused():
    calls, refreshes = _run_remove("evil\nname")
    assert calls == []
    assert refreshes == []


def test_empty_name_refused():
    calls, refreshes = _run_remove("")
    assert calls == []
    assert refreshes == []


def test_no_container_refused():
    """WS connected but container down: a ``docker exec ""`` would
    fail opaquely, so refuse before spawning anything."""
    calls, refreshes = _run_remove("perplexity", container_name="")
    assert calls == []
    assert refreshes == []
