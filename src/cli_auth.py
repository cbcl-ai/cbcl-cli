"""OAuth login flow helpers for ``cbcl auth`` / ``cbcl setup``.

Split out of ``cli_commands.py`` because the OAuth handshake (fake-browser
URL interception, container-local callback forwarding, code-paste fallback)
runs to ~635 LOC of subprocess/HTTP plumbing that is independent of the
``click`` command layer. The CLI commands in ``cli_commands.py`` import the
public helpers from here.
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

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


def _install_url_interceptor(container_name: str) -> None:
    """Install fake browser commands in the container to capture OAuth URLs."""
    try:
        subprocess.run(
            ["docker", "exec", "-u", "root", container_name, "bash", "-c",
             'cat > /usr/local/bin/xdg-open << "SCRIPT"\n'
             '#!/bin/bash\n'
             'echo "CAPTURED_URL: $1" >> /tmp/captured_urls.txt\n'
             'exit 0\n'
             'SCRIPT\n'
             'chmod +x /usr/local/bin/xdg-open\n'
             'for cmd in open sensible-browser x-www-browser www-browser; do\n'
             '  cp /usr/local/bin/xdg-open /usr/local/bin/$cmd\n'
             'done\n'
             'rm -f /tmp/captured_urls.txt'],
            capture_output=True, check=True, timeout=10,
        )
    except subprocess.CalledProcessError:
        click.echo("  WARNING: Could not install URL interceptor.")


def _capture_oauth_url(
    container_name: str,
    auth_proc: subprocess.Popen,
    timeout: float = _URL_CAPTURE_TIMEOUT,
) -> tuple[str | None, int | None]:
    """Poll for the OAuth URL captured by the fake browser.

    Returns ``(url, port)`` or ``(None, None)`` on timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(1)
        # Bail early if auth process died
        if auth_proc.poll() is not None:
            click.echo("  claude auth login exited unexpectedly.")
            return None, None
        try:
            result = subprocess.run(
                ["docker", "exec", container_name, "cat", "/tmp/captured_urls.txt"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and "CAPTURED_URL:" in result.stdout:
                captured = result.stdout.strip().split("CAPTURED_URL: ")[-1]
                port_match = re.search(r'localhost(?:%3A|:)(\d+)', captured)
                if port_match:
                    return captured, int(port_match.group(1))
        except Exception:
            pass
    return None, None


def _forward_callback_to_container(
    container_name: str, port: int, request_path: str,
) -> tuple[int, str, str]:
    """Forward the OAuth callback into the container using curl.

    Returns ``(http_status, body, verbose_log)``.
    """
    container_url = f"http://localhost:{port}{request_path}"
    result = subprocess.run(
        ["docker", "exec", container_name,
         "curl", "-s", "-S", "-v",
         "-w", "\n__HTTP_STATUS__:%{http_code}",
         "--max-time", "30",
         container_url],
        capture_output=True, timeout=35,
    )
    stdout = result.stdout.decode(errors="replace")
    stderr = result.stderr.decode(errors="replace")

    # Parse HTTP status from the -w trailer
    http_status = 0
    body = stdout
    if "__HTTP_STATUS__:" in stdout:
        parts = stdout.rsplit("__HTTP_STATUS__:", 1)
        body = parts[0]
        try:
            http_status = int(parts[1].strip())
        except ValueError:
            pass

    if result.returncode != 0:
        click.echo(f"  curl error (rc={result.returncode}): {stderr[:300]}")

    return http_status, body.strip(), stderr.strip()


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
    from urllib.parse import urlencode, quote

    # OAuth constants (from claude-src/constants/oauth.ts)
    CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    AUTH_ENDPOINT = "https://claude.com/cai/oauth/authorize"
    TOKEN_ENDPOINT = "https://platform.claude.com/v1/oauth/token"
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


def _find_container_callback_port(container_name: str) -> int | None:
    """Find the Claude CLI's callback server port from /proc/net/tcp."""
    try:
        result = subprocess.run(
            ["docker", "exec", container_name, "cat", "/proc/1/net/tcp"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().split("\n"):
            parts = line.split()
            if len(parts) >= 2 and "0100007F:" in parts[1]:
                port_hex = parts[1].split(":")[1]
                port_num = int(port_hex, 16)
                if port_num > 10000:
                    return port_num
    except Exception:
        pass
    return None


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


def _authenticate_office_container_local(
    container_name: str,
    *,
    force: bool = False,
) -> bool:
    """Authenticate Claude CLI in a container via localhost port forwarding.

    The Claude CLI opens a local HTTP server for the OAuth callback, but
    the browser on the host can't reach it inside Docker.  Solution:

    1. Install fake browser commands in the container to capture the real URL
    2. Start ``claude auth login`` in the container (stdout → temp file)
    3. Capture the browser URL (localhost:PORT redirect) from interceptor
    4. Also capture the stdout URL (platform redirect) for manual fallback
    5. Start an HTTP proxy on the host at the same port
    6. Show both URLs: browser URL auto-completes, stdout URL for manual flow
    7. Wait for callback (auto) or code paste (manual)
    """
    # -- Pre-check: already authenticated? ----------------------------------
    if not force:
        click.echo("  Checking existing auth...")
        if _verify_claude_in_container(container_name):
            account = _get_auth_account_info(container_name)
            label = f" — {account}" if account else ""
            click.echo(f"  Already authenticated!{label}")
            return True

    click.echo("")
    click.echo("  Setting up authentication...")

    # Step 1 — install fake browser to capture the real URL + clean up
    _install_url_interceptor(container_name)
    subprocess.run(
        ["docker", "exec", container_name, "rm", "-f", "/tmp/auth_output.txt"],
        capture_output=True, timeout=5,
    )

    # Step 2 — start claude auth login in background (stdout → temp file)
    auth_proc = subprocess.Popen(
        ["docker", "exec", "-i", "-e", "BROWSER=xdg-open",
         container_name, "bash", "-c",
         "claude auth login 2>&1 | tee /tmp/auth_output.txt"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Step 3 — wait for the browser URL (captured by interceptor)
    click.echo(f"  Waiting for auth URL (up to {_URL_CAPTURE_TIMEOUT}s)...")
    url, port = _capture_oauth_url(container_name, auth_proc)

    if not url or not port:
        click.echo("  Could not capture auth URL.")
        auth_proc.terminate()
        return False

    click.echo(f"  Auth server listening on container port {port}")

    # Step 3b — also capture the stdout URL (platform redirect, for manual flow)
    stdout_url = None
    try:
        result = subprocess.run(
            ["docker", "exec", container_name, "cat", "/tmp/auth_output.txt"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout:
            match = re.search(
                r'visit[:\s]+\s*(https://[^\s]+)',
                result.stdout, re.IGNORECASE,
            )
            if match:
                stdout_url = match.group(1)
    except Exception:
        pass

    # Step 4 — start HTTP proxy on the host that forwards callbacks
    callback_received = threading.Event()
    proxy_error: list[str | None] = [None]

    class _CallbackProxy(BaseHTTPRequestHandler):
        """Catches the OAuth callback and forwards it into the container."""

        def do_GET(self):  # noqa: N802
            try:
                _status, body, _verbose = _forward_callback_to_container(
                    container_name, port, self.path,
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    body.encode() if body else
                    b"<html><body><h2>Authentication successful!</h2>"
                    b"<p>You can close this tab and return to the terminal.</p>"
                    b"</body></html>"
                )
            except Exception as exc:
                proxy_error[0] = str(exc)
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h2>Authentication callback received.</h2>"
                    b"<p>Return to the terminal.</p></body></html>"
                )
            finally:
                callback_received.set()

        def log_message(self, format, *args):  # noqa: A002
            pass  # suppress HTTP server logs

    try:
        proxy_server = HTTPServer(("127.0.0.1", port), _CallbackProxy)
        proxy_server.timeout = 1
    except OSError as exc:
        click.echo(f"  Could not start proxy on port {port}: {exc}")
        auth_proc.terminate()
        return False

    def _serve() -> None:
        while not callback_received.is_set():
            proxy_server.handle_request()

    proxy_thread = threading.Thread(target=_serve, daemon=True)
    proxy_thread.start()

    click.echo(f"  Callback proxy listening on host port {port}")

    # Step 5 — show URLs to user
    click.echo("")
    click.echo("  " + "=" * 60)
    click.echo("")
    click.echo("  Open this URL in your browser:")
    click.echo("")
    click.echo(f"  {url}")
    click.echo("")
    click.echo("  Log in with your Claude account.")
    click.echo("  After login, authentication completes automatically.")
    if stdout_url:
        click.echo("")
        click.echo("  If the redirect doesn't work, use this URL instead:")
        click.echo(f"  {stdout_url}")
        click.echo("  (copy the code from the page and paste it below)")
    click.echo("")
    click.echo("  " + "=" * 60)
    click.echo("")
    click.echo("  Waiting for callback (or paste a code)...")

    # Step 6 — wait for callback OR manual code input
    # Use a background thread to accept code input while proxy is running
    manual_code: list[str | None] = [None]

    def _wait_for_input() -> None:
        try:
            raw = click.prompt(
                "  ", prompt_suffix="", default="", show_default=False,
            )
            if raw.strip():
                manual_code[0] = raw.strip()
                callback_received.set()
        except (EOFError, KeyboardInterrupt, click.Abort):
            pass

    input_thread = threading.Thread(target=_wait_for_input, daemon=True)
    input_thread.start()

    callback_received.wait(timeout=_BROWSER_LOGIN_TIMEOUT)

    if not callback_received.is_set():
        click.echo(
            f"\n  Auth timed out after {_BROWSER_LOGIN_TIMEOUT // 60} minutes."
        )
        auth_proc.terminate()
        proxy_server.server_close()
        return False

    # If user pasted a code manually, deliver it to the callback server
    if manual_code[0] and not proxy_error[0]:
        from urllib.parse import urlparse, parse_qs, quote

        code = manual_code[0]
        # Handle code#state format
        if "#" in code and not code.startswith("http"):
            code = code.split("#")[0]
        # Handle URL format
        if "code=" in code:
            parsed = urlparse(code)
            params = parse_qs(parsed.query)
            code = params.get("code", [code])[0]

        # Extract state from the browser URL
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        state = params.get("state", [""])[0]

        click.echo(f"\n  Delivering code to CLI...")
        callback_path = f"/callback?code={quote(code, safe='')}&state={quote(state, safe='')}"
        _forward_callback_to_container(container_name, port, callback_path)

    if proxy_error[0]:
        click.echo(f"  WARNING: proxy error: {proxy_error[0]}")
    else:
        click.echo("\n  Callback received!")

    # Wait for the CLI to process the callback and store tokens
    try:
        auth_proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        auth_proc.terminate()

    proxy_server.server_close()
    time.sleep(2)

    # Step 7 — verify
    click.echo("  Verifying authentication...")
    if _verify_claude_in_container(container_name):
        account = _get_auth_account_info(container_name)
        label = f" — {account}" if account else ""
        click.echo(f"  Authentication verified!{label}")
        return True

    click.echo("  Authentication could not be verified.")
    if click.confirm("  Retry?", default=True):
        return _authenticate_office_container(container_name, force=True)

    return False


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

