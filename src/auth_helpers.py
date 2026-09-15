"""Claude CLI auth-status helpers — shared by the CLI and the
running daemon's request handler.

Checks the saved office token against Anthropic's OAuth profile endpoint.
Model capacity is independent of authentication: an exhausted session limit
must never send a successfully signed-in user back through OAuth.

Pure stdlib, no CLI-only dependencies (no ``click``, no
``threading``, no ``HTTPServer``). Safe to call from the daemon's
asyncio loop via ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import logging
import subprocess

logger = logging.getLogger(__name__)


class AuthVerificationUnavailableError(RuntimeError):
    """The login state is unknown; retry verification, not authentication."""


# Read and use the token entirely inside the selected container. Neither tokens
# nor provider response bodies cross stdout, argv, the backend, or its logs.
# No Claude session, workspace settings, hooks or MCP servers are loaded.
_AUTH_PROFILE_SCRIPT = r"""
const fs = require('fs');
const https = require('https');
let finished = false;
function finish(state) {
  if (!finished) { finished = true; console.log(JSON.stringify({state})); }
}
let credentials;
try {
  const fd = fs.openSync('/home/agent/.claude/.credentials.json',
    fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
  try {
    const stat = fs.fstatSync(fd);
    if (!stat.isFile() || stat.size > 1024 * 1024) throw new Error('unsafe file');
    credentials = JSON.parse(fs.readFileSync(fd, 'utf8')).claudeAiOauth;
  } finally { fs.closeSync(fd); }
} catch (error) {
  finish(error.code === 'ENOENT' ? 'missing' : 'unavailable');
}
if (!finished) {
  if (!credentials || typeof credentials.accessToken !== 'string' || !credentials.accessToken) {
    finish('missing');
  } else {
    const request = https.request({
      hostname: 'api.anthropic.com', path: '/api/oauth/profile', method: 'GET',
      headers: {Authorization: 'Bearer ' + credentials.accessToken, 'Content-Type': 'application/json'},
      timeout: 8000,
    }, response => {
      let body = '';
      response.on('data', chunk => {
        body += chunk;
        if (body.length > 256 * 1024) { finish('unavailable'); request.destroy(); }
      });
      response.on('error', () => finish('unavailable'));
      response.on('end', () => {
        if (response.statusCode === 401) return finish('invalid');
        if (response.statusCode !== 200) return finish('unavailable');
        try {
          const profile = JSON.parse(body);
          finish(typeof profile.account?.uuid === 'string' && profile.account.uuid ? 'valid' : 'unavailable');
        } catch { finish('unavailable'); }
      });
    });
    request.on('timeout', () => { finish('unavailable'); request.destroy(); });
    request.on('error', () => finish('unavailable'));
    request.end();
  }
}
"""


def _profile_auth_state(container_name: str) -> str:
    try:
        result = subprocess.run(
            ["docker", "exec", "-i", "-u", "agent", container_name,
             "/usr/bin/env", "-u", "NODE_OPTIONS", "-u", "NODE_PATH", "node", "-"],
            input=_AUTH_PROFILE_SCRIPT, capture_output=True, text=True, timeout=10,
        )
        state = json.loads(result.stdout).get("state") if result.returncode == 0 else None
        if isinstance(state, str) and state in {"valid", "invalid", "missing"}:
            return state
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        pass
    raise AuthVerificationUnavailableError(
        "Claude sign-in could not be checked right now. Your saved credentials "
        "have been kept. Wait a moment and click Recheck."
    )


def verify_claude_in_container(
    container_name: str, *, refresh: bool = True, warning_sink: list[str] | None = None,
) -> bool:
    """Validate login without spending model quota; let the CLI refresh on 401.

    Callers must first validate this office's immutable container and mounts.
    A model call is only needed to let the CLI rotate an expired token. Even
    then, the resulting profile check is authoritative: the model may be capped.
    """
    from src._setup_cli import GenerationPolicyError, _probe_claude_works

    state = _profile_auth_state(container_name)
    errors: list[str] = []
    capacity_checked = False
    if state == "invalid" and refresh:
        _probe_claude_works(container_name, error_sink=errors)
        capacity_checked = True
        state = _profile_auth_state(container_name)
    if state == "valid" and warning_sink is not None:
        if not capacity_checked:
            try:
                _probe_claude_works(container_name, error_sink=errors)
            except GenerationPolicyError as exc:
                warning_sink.append(f"Signed in, but AI execution needs attention. {exc}")
        if errors:
            from src.orchestrator.error_classifier import ErrorClass, classify_error

            # The CLI may have refreshed/rejected a token after our first
            # profile check. Re-read that saved login before reporting success.
            if not capacity_checked and classify_error(errors[-1]).error_class == ErrorClass.AUTH_FAILED:
                state = _profile_auth_state(container_name)
            if state == "valid":
                warning_sink.append(_availability_warning(errors[-1]))
    return state == "valid"


def _availability_warning(error: str) -> str:
    from src.orchestrator.error_classifier import ErrorClass, classify_error

    remedy = classify_error(error)
    if remedy.error_class == ErrorClass.USAGE_LIMIT_EXCEEDED:
        reset = (
            f" It resets at {remedy.reset_at.strftime('%H:%M UTC on %d %b')}."
            if remedy.reset_at else " Try again after your usage window resets."
        )
        return (
            "You're signed in, but this Claude account has reached its usage limit."
            + reset + " Signing in again won't reset the limit."
        )
    if remedy.error_class == ErrorClass.RATE_LIMITED:
        return "You're signed in, but Claude is temporarily rate-limiting requests. Try again shortly."
    return "You're signed in, but Claude's availability could not be confirmed. Try again shortly."


def warm_claude_in_container(container_name: str) -> bool:
    """Give the CLI a chance to refresh, without confusing quota with login."""
    from src._setup_cli import _probe_claude_works

    if _probe_claude_works(container_name) is True:
        return True
    return verify_claude_in_container(container_name, refresh=False)


def oauth_profile_metadata(profile: dict) -> dict:
    """Normalize current nested OAuth profiles and older flat responses."""
    organization = profile.get("organization")
    organization = organization if isinstance(organization, dict) else {}
    organization_type = organization.get("organization_type")
    subscription = {
        "claude_max": "max", "claude_pro": "pro",
        "claude_team": "team", "claude_enterprise": "enterprise",
    }.get(organization_type if isinstance(organization_type, str) else "") or profile.get("subscription_type")
    tier = organization.get("rate_limit_tier") or profile.get("rate_limit_tier")
    return {
        "subscriptionType": subscription if isinstance(subscription, str) and subscription in {"max", "pro", "team", "enterprise"} else None,
        "rateLimitTier": tier if isinstance(tier, str) and tier else None,
    }


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
        if not sub_type or not isinstance(sub_type, str) or sub_type == "unknown":
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
