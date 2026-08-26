"""UI-driven Claude authentication for the office container (Phase 3).

The CLI's ``cbcl auth login`` runs the same OAuth code-paste flow
interactively in the terminal. Phase 3 lifts that machinery into
the daemon so the UI can drive it without the user ever leaving
the browser.

Flow (two-stage):

  1. ``start_auth_flow(container_name)`` → generates a fresh PKCE
     pair + state, builds the authorization URL, stashes the
     verifier under a session_id, returns ``{auth_url, session_id}``.
     The user opens ``auth_url`` in a new tab, signs in at
     claude.com, and ``platform.claude.com`` shows them a code
     to paste back into the UI.

  2. ``complete_auth_flow(session_id, raw_code)`` → recovers the
     verifier for the session, exchanges the code at
     ``platform.claude.com/v1/oauth/token``, fetches the account
     profile, writes ``.credentials.json`` into the container, and
     verifies with a no-op ``claude --print`` call. Returns
     ``{authenticated, account, error}``.

Session state is in-memory (a process-wide dict keyed by
session_id with timestamp-based eviction). A daemon restart
invalidates in-flight flows — the user just clicks "Sign in" again.
That's an acceptable trade-off for not introducing a persistence
layer for ephemeral 5-minute auth sessions.

Token exchanges run via Node.js inside the container (curl gets
blocked by Cloudflare's TLS fingerprinting). This mirrors the
CLI implementation; the goal of this module is to expose the same
contract via plain Python functions without any ``click`` calls.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

logger = logging.getLogger(__name__)

# OAuth constants — match the upstream Claude CLI exactly.
# Source: claude-src/constants/oauth.ts.
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTH_ENDPOINT = "https://claude.com/cai/oauth/authorize"
TOKEN_ENDPOINT_HOST = "platform.claude.com"
TOKEN_ENDPOINT_PATH = "/v1/oauth/token"
PROFILE_ENDPOINT_HOST = "api.anthropic.com"
PROFILE_ENDPOINT_PATH = "/api/oauth/profile"
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
SCOPES = (
    "org:create_api_key user:profile user:inference "
    "user:sessions:claude_code user:mcp_servers user:file_upload"
)

# How long an unfinished auth session lives before being garbage
# collected from the in-memory store. Generous enough that a user
# can take a coffee break between opening the URL and pasting the
# code, but tight enough that idle sessions don't leak indefinitely.
SESSION_TTL_SECONDS = 600  # 10 minutes


@dataclass
class AuthFlowSession:
    """Per-session PKCE state for an in-flight UI auth attempt.

    Discarded once ``complete_auth_flow`` returns (success or
    failure) — completion is one-shot. If the user retries by
    clicking "Sign in" again, ``start_auth_flow`` issues a fresh
    session_id; the old one ages out via TTL.
    """

    session_id: str
    container_name: str
    code_verifier: str
    state: str
    created_at: float = field(default_factory=time.monotonic)


# Process-wide store. Keyed by session_id (UUID) so concurrent
# auth attempts for the same office don't clobber each other.
_SESSIONS: dict[str, AuthFlowSession] = {}


def _evict_expired_sessions() -> None:
    """Drop sessions older than ``SESSION_TTL_SECONDS``. Cheap; runs
    on every start/complete call rather than via a background task
    so we don't need to wire another asyncio task into the daemon."""
    now = time.monotonic()
    expired = [
        sid for sid, sess in _SESSIONS.items()
        if (now - sess.created_at) > SESSION_TTL_SECONDS
    ]
    for sid in expired:
        _SESSIONS.pop(sid, None)


def _pkce_pair() -> tuple[str, str]:
    """Generate a PKCE ``(verifier, challenge)`` pair.

    Matches the algorithm the CLI uses (claude-src/services/oauth/crypto.ts):
    32 random bytes → URL-safe base64 (no padding) for the verifier;
    SHA-256 of the verifier → URL-safe base64 (no padding) for the challenge.
    """
    verifier = (
        base64.urlsafe_b64encode(secrets.token_bytes(32))
        .rstrip(b"=")
        .decode()
    )
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def _random_state() -> str:
    """OAuth state parameter — 32 bytes of randomness."""
    return (
        base64.urlsafe_b64encode(secrets.token_bytes(32))
        .rstrip(b"=")
        .decode()
    )


def extract_auth_code(raw_input: str) -> str | None:
    """Pull the auth code out of whatever the user pasted.

    Three formats accepted (mirrors the CLI helper):

    * ``code#state`` — what platform.claude.com shows on the code
      page; the user copies the whole thing.
    * ``http://...?code=X&state=Y`` — if the user opened the URL
      from their browser bar instead of the code page.
    * Raw code string.

    Returns ``None`` for empty input. Caller should treat that as
    "user submitted nothing" and re-prompt rather than passing
    ``None`` to ``complete_auth_flow``.
    """
    raw = (raw_input or "").strip()
    if not raw:
        return None

    if raw.startswith("http") and "code=" in raw:
        parsed = urlparse(raw)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        if code:
            return code

    if "#" in raw and not raw.startswith("http"):
        return raw.split("#")[0].strip()

    return raw


def start_auth_flow(container_name: str) -> dict[str, str]:
    """Begin a UI-driven auth attempt.

    Returns ``{"auth_url": ..., "session_id": ...}``. The frontend
    opens ``auth_url`` in a new tab, the user signs in, and the
    code shown on platform.claude.com is what they paste back to
    finish via ``complete_auth_flow``.

    Raises ``ValueError`` only if ``container_name`` is empty —
    the caller (RPC handler) should surface that as a 400 to the
    frontend rather than letting it fail later inside the token
    exchange.
    """
    if not container_name:
        raise ValueError(
            "container_name is required to start an auth flow"
        )

    _evict_expired_sessions()

    session_id = str(uuid.uuid4())
    verifier, challenge = _pkce_pair()
    state = _random_state()

    auth_params = {
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    auth_url = f"{AUTH_ENDPOINT}?{urlencode(auth_params)}"

    _SESSIONS[session_id] = AuthFlowSession(
        session_id=session_id,
        container_name=container_name,
        code_verifier=verifier,
        state=state,
    )

    return {"auth_url": auth_url, "session_id": session_id}


def _node_request_in_container(
    container_name: str,
    host: str,
    path: str,
    method: str,
    headers: dict[str, str],
    body: str | None = None,
    timeout_seconds: int = 30,
) -> tuple[int, str]:
    """Run an HTTPS request from inside the container via Node.js.

    curl gets fingerprint-blocked by Cloudflare in front of
    platform.claude.com. Node.js's TLS handshake passes. We
    serialise the body as JSON outside, then ``JSON.stringify`` it
    inside the script — this avoids fragile shell quoting.
    """
    body_for_script = body or ""
    headers_block = "{ " + ", ".join(
        f"'{k}': '{v}'" for k, v in headers.items()
    ) + " }"
    if body:
        body_payload = (
            f"const data = {json.dumps(body_for_script)};\n"
            "options.headers['Content-Length'] = "
            "Buffer.byteLength(data);\n"
        )
        write_block = "req.write(data);\n"
    else:
        body_payload = ""
        write_block = ""

    script = (
        "const https = require('https');\n"
        f"const options = {{ hostname: '{host}', "
        f"path: '{path}', "
        f"method: '{method}', "
        f"headers: {headers_block}, "
        f"timeout: {timeout_seconds * 1000} }};\n"
        + body_payload
        + "const req = https.request(options, (res) => {\n"
        "  let body = '';\n"
        "  res.on('data', c => body += c);\n"
        "  res.on('end', () => process.stdout.write(\n"
        "    JSON.stringify({status: res.statusCode, body: body})));\n"
        "});\n"
        "req.on('error', e => process.stdout.write(\n"
        "  JSON.stringify({status: 0, body: e.message})));\n"
        # Without an explicit destroy, the ``timeout`` option only
        # emits an event — the socket stays open and the subprocess
        # hangs until the outer kill. Destroying surfaces a clean
        # status-0 error instead.
        "req.on('timeout', () => req.destroy(new Error('request timed out')));\n"
        + write_block
        + "req.end();\n"
    )

    result = subprocess.run(
        ["docker", "exec", container_name, "node", "-e", script],
        capture_output=True, text=True, timeout=timeout_seconds + 5,
    )
    if result.returncode != 0:
        return 0, result.stderr[:500]
    try:
        wrapper = json.loads(result.stdout)
        return int(wrapper.get("status", 0)), str(wrapper.get("body", ""))
    except (json.JSONDecodeError, TypeError):
        return 0, result.stdout[:500]


def _exchange_code_for_tokens(
    container_name: str, code: str, code_verifier: str, state: str,
) -> dict[str, Any]:
    """POST /v1/oauth/token. Returns the token dict on success;
    raises ``RuntimeError`` with a user-facing message on failure
    (rate-limit, expired code, etc.)."""
    body = json.dumps({
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": code_verifier,
        "state": state,
    })
    status, raw = _node_request_in_container(
        container_name,
        TOKEN_ENDPOINT_HOST, TOKEN_ENDPOINT_PATH,
        "POST",
        {"Content-Type": "application/json"},
        body=body,
        timeout_seconds=15,
    )

    if status == 429:
        raise RuntimeError(
            "Rate limited by Claude. Wait a few minutes and try again."
        )
    if status == 0:
        # The request never completed — ``raw`` is docker/Node error
        # text, not an endpoint response. Name the two field-diagnosed
        # causes instead of the old opaque "non-JSON response (status
        # 0)" (2026-08-03: the office container had been torn down, so
        # ``docker exec`` itself failed and its stderr landed here).
        if "No such container" in raw or "is not running" in raw:
            raise RuntimeError(
                "The office container is not running. Restart the "
                "communicator (cbcl stop, then cbcl start), wait for the "
                "office to reconnect, then start the sign-in again from "
                "the button — pasted codes are single-use."
            )
        raise RuntimeError(
            "Could not reach the Claude token endpoint from the office "
            f"container ({raw[:200].strip() or 'no error detail'}). Check "
            "the machine's network/VPN and retry; if it persists, restart "
            "the communicator."
        )
    try:
        tokens = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        raise RuntimeError(
            f"Token endpoint returned non-JSON response (status {status})."
        )
    if status != 200 or "error" in tokens:
        msg = tokens.get("error_description") or tokens.get("error") or raw[:200]
        raise RuntimeError(f"Token exchange failed (HTTP {status}): {msg}")
    if not tokens.get("access_token"):
        raise RuntimeError("Token endpoint response missing access_token.")
    return tokens


def _fetch_profile(container_name: str, access_token: str) -> dict[str, Any]:
    """GET /api/oauth/profile. Returns ``{}`` on any failure — the
    profile is informational (subscription label) and should never
    block authentication."""
    status, raw = _node_request_in_container(
        container_name,
        PROFILE_ENDPOINT_HOST, PROFILE_ENDPOINT_PATH,
        "GET",
        {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        body=None,
        timeout_seconds=10,
    )
    if status != 200 or not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _write_credentials(
    container_name: str, tokens: dict[str, Any], profile: dict[str, Any],
) -> None:
    """Write ``.credentials.json`` into the container.

    Format mirrors the CLI's claude-src/utils/auth.ts (lines
    1217-1229). We include both the profile-derived metadata
    (subscription label, rate-limit tier) and the token bundle.
    """
    expires_in = tokens.get("expires_in", 3600)
    creds = {
        "claudeAiOauth": {
            "accessToken": tokens["access_token"],
            "refreshToken": tokens.get("refresh_token", ""),
            "expiresAt": int(time.time() * 1000) + (expires_in * 1000),
            "scopes": SCOPES.split(),
            "subscriptionType": profile.get("subscription_type", "unknown"),
            "rateLimitTier": profile.get("rate_limit_tier", ""),
        },
    }
    creds_json = json.dumps(creds, indent=2)

    # The .backup twin is the auth keepalive's corruption guard
    # (``src.auth_keepalive`` restores it ONLY when the live file fails
    # JSON-parse — never on token invalidity). Written on every
    # successful sign-in and refreshed by the keepalive whenever the
    # live bundle shows evidence of working, so it tracks the newest
    # ROTATED refresh token.
    write_result = subprocess.run(
        [
            "docker", "exec", "-i", container_name, "bash", "-c",
            "mkdir -p /home/agent/.claude && "
            "cat > /home/agent/.claude/.credentials.json && "
            "chmod 600 /home/agent/.claude/.credentials.json && "
            "cp /home/agent/.claude/.credentials.json "
            "/home/agent/.claude/.credentials.json.backup && "
            "chmod 600 /home/agent/.claude/.credentials.json.backup",
        ],
        input=creds_json.encode(),
        capture_output=True, timeout=10,
    )
    if write_result.returncode != 0:
        raise RuntimeError(
            f"Failed to write credentials.json: "
            f"{write_result.stderr.decode()[:200]}"
        )


def complete_auth_flow(session_id: str, raw_code: str) -> dict[str, Any]:
    """Finish an in-flight auth attempt.

    Looks up the session, exchanges the code for tokens, writes
    credentials to the container, and verifies. Returns:

    * ``{"authenticated": True, "account": "Claude Max"}`` on success.
    * ``{"authenticated": False, "error": "..."}`` on any failure.

    Side effect: deletes the session from ``_SESSIONS`` so the
    same code can't be exchanged twice — important for safety
    against a user double-clicking Submit.
    """
    _evict_expired_sessions()

    session = _SESSIONS.pop(session_id, None)
    if session is None:
        return {
            "authenticated": False,
            "error": (
                "Auth session expired or not found. Click Sign In again "
                "to start a new attempt."
            ),
        }

    code = extract_auth_code(raw_code)
    if not code:
        return {
            "authenticated": False,
            "error": (
                "Could not parse the code. Paste either the code shown "
                "on platform.claude.com, or the full callback URL."
            ),
        }

    try:
        tokens = _exchange_code_for_tokens(
            session.container_name, code,
            session.code_verifier, session.state,
        )
        profile = _fetch_profile(session.container_name, tokens["access_token"])
        _write_credentials(session.container_name, tokens, profile)
    except RuntimeError as exc:
        logger.warning(
            "Auth flow failed for container %s: %s",
            session.container_name, exc,
        )
        return {"authenticated": False, "error": str(exc)}
    except Exception as exc:
        # Last-ditch — anything else (subprocess, JSON, etc.) gets
        # a generic error so we never crash the RPC handler.
        logger.exception(
            "Unexpected error in auth flow for %s",
            session.container_name,
        )
        return {
            "authenticated": False,
            "error": f"Unexpected error during sign-in: {exc}",
        }

    # Verify by hitting the Claude API once. The CLI also retries
    # this 3 times with 3s pauses; here we do a single attempt
    # because the RPC has its own retry/timeout budget on the
    # caller side. If verification fails the credentials are still
    # written; the user can re-check from the UI.
    from src.auth_helpers import (
        get_auth_account_info,
        verify_claude_in_container,
    )

    if verify_claude_in_container(session.container_name):
        return {
            "authenticated": True,
            "account": get_auth_account_info(session.container_name),
        }

    # Credentials written but Claude --print didn't succeed. Most
    # commonly this is a transient cold-start (haiku model load).
    # Tell the user "wrote creds but couldn't verify yet" rather
    # than claiming failure — they can click Recheck in 5s.
    return {
        "authenticated": False,
        "error": (
            "Credentials saved but the verification call didn't "
            "succeed yet. This is usually transient — wait a few "
            "seconds and click Recheck."
        ),
        "credentials_written": True,
    }
