"""OAuth login flow helpers for ``cbcl auth`` / ``cbcl setup``.

Split out of ``cli_commands.py`` because the OAuth handshake is
subprocess/HTTP plumbing independent of the ``click`` command layer. The
CLI commands in ``cli_commands.py`` import the public helpers from here.

The fake-browser URL interceptor and container-local callback forwarder
that this docstring used to describe were removed (07/DEAD-01): they were
leftovers of the in-app MCP OAuth machinery deleted in 86b65bb1 and had had
no caller since — code that reads as live, and that the next person to
touch container auth would have had to understand before ignoring.
"""
from __future__ import annotations

import json
import subprocess
import time

import click

from src.auth_helpers import (
    get_auth_account_info as _get_auth_account_info,
    verify_claude_in_container as _verify_claude_in_container,
)
from src.config import OfficeConfig, fetch_offices_sync

# How long to wait for ``claude auth login`` to start and produce a URL (s).
_URL_CAPTURE_TIMEOUT = 60

# How long to wait for the user to complete browser login (seconds).
_BROWSER_LOGIN_TIMEOUT = 300


def _authenticate_office_container(
    container_name: str,
    *,
    force: bool = False,
) -> bool:
    """Authenticate Claude CLI in a Docker container via code-paste flow."""
    return _authenticate_office_container_remote(container_name, force=force)


def _authenticate_office_container_remote(
    container_name: str,
    *,
    force: bool = False,
) -> bool:
    """Authenticate Claude CLI in a container via own OAuth PKCE flow.

    Performs the full OAuth exchange ourselves (not via ``claude auth login``):
    1. Generate our own PKCE pair (code_verifier + code_challenge)
    2. Build auth URL with redirect_uri=platform.claude.com/oauth/code/callback
    3. User opens URL, authenticates, copies the code from the platform page
    4. We exchange the code at platform.claude.com/v1/oauth/token
    5. Write credentials directly to .credentials.json in the container

    This avoids the redirect_uri mismatch problem entirely — we control both
    the auth URL and the token exchange, using the same redirect_uri.
    """
    import base64
    import hashlib
    import secrets
    from urllib.parse import urlencode

    # OAuth constants (from claude-src/constants/oauth.ts)
    CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    AUTH_ENDPOINT = "https://claude.com/cai/oauth/authorize"
    # (token exchange happens inside the container via the Claude CLI itself,
    # so no TOKEN_ENDPOINT is needed in this authorize-URL builder path)
    REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
    SCOPES = "org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"

    # Pre-check
    if not force:
        click.echo("  Checking existing auth...")
        if _verify_claude_in_container(container_name):
            account = _get_auth_account_info(container_name)
            label = f" — {account}" if account else ""
            click.echo(f"  Already authenticated!{label}")
            return True

    click.echo("")
    click.echo("  Setting up authentication (remote mode)...")

    # Step 1: Generate PKCE (same algo as claude-src/services/oauth/crypto.ts)
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()

    # Step 2: Build auth URL with redirect_uri=platform.claude.com
    auth_params = {
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    auth_url = f"{AUTH_ENDPOINT}?{urlencode(auth_params)}"

    # Step 3: Show URL to user
    click.echo("")
    click.echo("  " + "=" * 60)
    click.echo("")
    click.echo("  Open this URL in your browser:")
    click.echo("")
    click.echo(f"  {auth_url}")
    click.echo("")
    click.echo("  Log in with your Claude account.")
    click.echo("  After login, the page will show a code.")
    click.echo("  Copy the code and paste it below.")
    click.echo("")
    click.echo("  " + "=" * 60)
    click.echo("")

    # Step 4: Get code from user
    raw_input = click.prompt("  Paste the code", type=str).strip()
    if not raw_input:
        click.echo("  No input. Aborting.")
        return False

    pasted_code = _extract_auth_code(raw_input)
    if not pasted_code:
        click.echo("  Could not extract code. Aborting.")
        return False

    click.echo(f"  Code: {pasted_code[:12]}...")

    # Step 5: Exchange code for tokens at the CORRECT endpoint
    # (from claude-src/constants/oauth.ts: platform.claude.com/v1/oauth/token)
    click.echo("  Exchanging code for tokens...")
    token_body = json.dumps({
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": pasted_code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": code_verifier,
        "state": state,
    })

    # Use Node.js inside the container for the token exchange.
    # curl gets blocked by Cloudflare's TLS fingerprinting (429).
    # Node.js has a proper TLS fingerprint that passes Cloudflare.
    node_script = f"""
const https = require('https');
const data = JSON.stringify({token_body});
const req = https.request({{
  hostname: 'platform.claude.com',
  path: '/v1/oauth/token',
  method: 'POST',
  headers: {{
    'Content-Type': 'application/json',
    'Content-Length': data.length
  }},
  timeout: 15000
}}, (res) => {{
  let body = '';
  res.on('data', chunk => body += chunk);
  res.on('end', () => {{
    process.stdout.write(JSON.stringify({{status: res.statusCode, body: body}}));
  }});
}});
req.on('error', e => {{
  process.stdout.write(JSON.stringify({{status: 0, body: e.message}}));
}});
req.write(data);
req.end();
"""
    result = subprocess.run(
        ["docker", "exec", container_name, "node", "-e", node_script],
        capture_output=True, text=True, timeout=30,
    )

    if result.returncode != 0:
        click.echo(f"  Token exchange failed: {result.stderr[:200]}")
        return False

    try:
        wrapper = json.loads(result.stdout)
        http_status = wrapper.get("status", 0)
        tokens = json.loads(wrapper.get("body", "{}"))
    except (json.JSONDecodeError, TypeError):
        click.echo(f"  Invalid response: {result.stdout[:300]}")
        return False

    if http_status == 429:
        click.echo("  Rate limited. Wait a few minutes and try again.")
        return False

    if http_status != 200 or "error" in tokens:
        click.echo(f"  Token exchange failed (HTTP {http_status}): {tokens}")
        return False

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_in = tokens.get("expires_in", 3600)

    if not access_token:
        click.echo(f"  No access_token in response: {result.stdout[:200]}")
        return False

    click.echo("  Token exchange successful!")

    # Step 6: Fetch profile to get subscription info
    # (from claude-src/services/oauth/getOauthProfile.ts)
    click.echo("  Fetching account profile...")
    profile_script = f"""
const https = require('https');
const req = https.request({{
  hostname: 'api.anthropic.com',
  path: '/api/oauth/profile',
  method: 'GET',
  headers: {{
    'Authorization': 'Bearer {access_token}',
    'Content-Type': 'application/json'
  }},
  timeout: 10000
}}, (res) => {{
  let body = '';
  res.on('data', chunk => body += chunk);
  res.on('end', () => process.stdout.write(body));
}});
req.on('error', () => process.stdout.write('{{}}'));
req.end();
"""
    profile_result = subprocess.run(
        ["docker", "exec", container_name, "node", "-e", profile_script],
        capture_output=True, text=True, timeout=15,
    )

    subscription_type = "unknown"
    rate_limit_tier = ""
    if profile_result.returncode == 0:
        try:
            profile = json.loads(profile_result.stdout)
            subscription_type = profile.get("subscription_type", "unknown")
            rate_limit_tier = profile.get("rate_limit_tier", "")
        except json.JSONDecodeError:
            pass

    # Step 7: Write credentials to container's .credentials.json
    # (format from claude-src/utils/auth.ts lines 1217-1229)
    import time as _time
    creds = {
        "claudeAiOauth": {
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "expiresAt": int(_time.time() * 1000) + (expires_in * 1000),
            "scopes": SCOPES.split(),
            "subscriptionType": subscription_type,
            "rateLimitTier": rate_limit_tier,
        },
    }
    creds_json = json.dumps(creds, indent=2)

    click.echo("  Writing credentials...")
    write_result = subprocess.run(
        ["docker", "exec", "-i", container_name, "bash", "-c",
         "mkdir -p /home/agent/.claude && cat > /home/agent/.claude/.credentials.json && chmod 600 /home/agent/.claude/.credentials.json"],
        input=creds_json.encode(),
        capture_output=True, timeout=10,
    )
    if write_result.returncode != 0:
        click.echo(f"  Failed to write credentials: {write_result.stderr.decode()[:200]}")
        return False

    # Step 8: Verify
    click.echo("  Verifying...")
    time.sleep(2)
    for attempt in range(3):
        if _verify_claude_in_container(container_name):
            account = _get_auth_account_info(container_name)
            label = f" — {account}" if account else ""
            click.echo(f"  Authentication verified!{label}")
            return True
        if attempt < 2:
            time.sleep(3)

    click.echo("  Authentication could not be verified.")
    click.echo("  Try 'cbcl auth --force' to retry.")
    return False


def _extract_auth_code(raw_input: str) -> str | None:
    """Extract auth code from user input.

    Handles these formats:
    - ``code#state``  (platform code page shows this)
    - ``http://...?code=X&state=Y``  (redirect URL from address bar)
    - raw code string
    """
    from urllib.parse import urlparse, parse_qs

    raw_input = raw_input.strip()
    if not raw_input:
        return None

    # User pasted a URL with code= query param
    if raw_input.startswith("http") and "code=" in raw_input:
        parsed = urlparse(raw_input)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        if code:
            return code

    # User pasted code#state format (common from platform code page)
    if "#" in raw_input and not raw_input.startswith("http"):
        return raw_input.split("#")[0].strip()

    # User pasted just the raw code
    return raw_input


def _find_offices_for_auth(
    platform_url: str,
    office_filter: str | None = None,
    security_token: str | None = None,
) -> list[OfficeConfig]:
    """Fetch offices from the platform, optionally filtering by name.

    ``security_token`` (the cbcl_co_... Company Token) is forwarded to
    ``fetch_offices_sync`` so the Bearer-authed discovery endpoint
    accepts the call. Without it the platform returns 401.
    """
    offices = fetch_offices_sync(platform_url, security_token)
    if not offices:
        click.echo("  No offices found on platform. Create one in the UI first.")
        return []
    if office_filter:
        matches = [o for o in offices if o.name.lower() == office_filter.lower()]
        if not matches:
            available = ", ".join(f'"{o.name}"' for o in offices)
            click.echo(f'  Office "{office_filter}" not found.')
            click.echo(f"  Available: {available}")
        return matches
    return offices

