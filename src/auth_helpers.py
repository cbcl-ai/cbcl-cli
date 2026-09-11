"""Claude CLI auth-status helpers — shared by the CLI and the
running daemon's request handler.

Both ``cbcl auth`` (interactive, in ``cli_commands.py``) and
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
    """Run the pinned tool-free diagnostic in an already verified container."""
    from src._setup_cli import GenerationPolicyError, _probe_claude_works

    try:
        return _probe_claude_works(container_name) is True
    except GenerationPolicyError:
        raise
    except Exception:
        logger.debug("Protected Claude auth diagnostic unavailable for container %s", container_name)
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
        sub_type = oauth.get("subscriptionType")
        # Missing ``subscriptionType`` (older CLI versions, manual
        # edits) used to fall back to the literal string ``"unknown"``,
        # which the UI then rendered as "Claude Unknown" — confusing
        # because the auth check itself was succeeding. Treat
        # missing metadata the same as a missing file: return None
        # so the UI shows "–". The authoritative auth pass is still
        # ``verify_claude_in_container``; account label is purely
        # informational.
        if not sub_type or not isinstance(sub_type, str):
            return None
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
