"""Claude CLI auth-status helpers — shared by the CLI and the
running daemon's request handler.

Both ``cbcl auth login`` (interactive, in ``cli_commands.py``) and
the backend's pre-flight auth check (RPC over the connector WS,
handled in ``handlers.py``) need to answer "does this office's
container have a valid Claude token?". The check is identical in
both contexts — exec ``claude --print`` with a no-op prompt and
look at the exit code — so it lives here once and both callers
import it.

Pure stdlib, no CLI-only dependencies (no ``click``, no
``threading``, no ``HTTPServer``). Safe to call from the daemon's
asyncio loop via ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import logging
import subprocess

logger = logging.getLogger(__name__)


def verify_claude_in_container(container_name: str) -> bool:
    """Return True iff the office container has a working Claude token.

    Runs ``claude --print`` with a no-op prompt; the CLI exits 0
    only when authentication succeeds. Times out at 30s — typical
    successful round-trip is 1-3s, so 30s is generous enough that
    a slow API doesn't cause false-negatives but tight enough that
    a totally-broken container doesn't hang the caller.

    The model is pinned to a cheap one (haiku) so the check costs
    a fraction of a cent. ``--max-turns 1`` and the trivial prompt
    make the response a few tokens at most.
    """
    try:
        result = subprocess.run(
            [
                "docker", "exec", container_name,
                "claude", "--print",
                "-p", "respond with just the word ok",
                "--output-format", "text",
                "--model", "claude-haiku-4-5-20251001",
                "--max-turns", "1",
                "--permission-mode", "bypassPermissions",
            ],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    except Exception as exc:
        # Don't raise — callers want a bool, not exception handling.
        # Real failures (container missing, docker daemon down) all
        # collapse to "auth not working" which is the right user-facing
        # message.
        logger.debug(
            "verify_claude_in_container(%s) failed: %s",
            container_name, exc,
        )
        return False


def get_auth_account_info(container_name: str) -> str | None:
    """Return a friendly account label, or None when unreadable.

    Examples: ``"Claude Max (default_claude_max_20x)"`` or
    ``"Claude Pro"``. Reads
    ``/home/agent/.claude/.credentials.json`` directly because
    the CLI itself doesn't surface this metadata in a clean way.

    None is the "we don't know" state — used both when the file
    doesn't exist (not authenticated yet) and when it does exist
    but has an unexpected shape (older CLI versions, manual edits).
    The caller should treat it as informational only — the
    authoritative auth check is ``verify_claude_in_container``.
    """
    try:
        result = subprocess.run(
            [
                "docker", "exec", container_name,
                "cat", "/home/agent/.claude/.credentials.json",
            ],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        creds = json.loads(result.stdout)
        oauth = creds.get("claudeAiOauth", {})
        sub_type = oauth.get("subscriptionType", "unknown")
        tier = oauth.get("rateLimitTier", "")
        return (
            f"Claude {sub_type.title()} ({tier})"
            if tier
            else f"Claude {sub_type.title()}"
        )
    except Exception as exc:
        logger.debug(
            "get_auth_account_info(%s) failed: %s",
            container_name, exc,
        )
        return None
