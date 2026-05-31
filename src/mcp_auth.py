"""MCP connector authentication helpers.

Reads the CLI's registered client credentials from .credentials.json,
builds the OAuth URL with a localhost callback, starts a callback server,
and completes the auth flow automatically.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlparse, parse_qs

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


async def start_cli_auth(
    router: object,
    request_id: str,
    name: str,
    url: str,
    container_name: str,
) -> None:
    """Start MCP authentication by reading CLI credentials and building auth URL.

    1. Ensures server is added (so the CLI creates client registration)
    2. Reads clientId/clientSecret from .credentials.json
    3. Picks a random port, builds auth URL with localhost callback
    4. Starts a callback server on the host to catch the redirect
    5. Publishes the auth URL for the frontend
    6. On callback: exchanges code for token, writes to credentials
    """
    try:
        result = await asyncio.to_thread(
            _prepare_auth, container_name, name, url,
        )

        if result.get("already_connected"):
            await router.publish_event({
                "type": "mcp_cli_auth_complete",
                "request_id": request_id,
            })
            return

        if result.get("error"):
            await router.publish_event({
                "type": "mcp_cli_auth_failed",
                "request_id": request_id,
                "error": result["error"],
            })
            return

        # Publish auth URL for the frontend
        await router.publish_event({
            "type": "mcp_cli_auth_url",
            "request_id": request_id,
            "auth_url": result["auth_url"],
            "auth_state": {
                "code_verifier": result["code_verifier"],
                "state": result["state"],
                "client_id": result["client_id"],
                "client_secret": result["client_secret"],
                "token_endpoint": result["token_endpoint"],
                "redirect_uri": result["redirect_uri"],
                "cred_key": result["cred_key"],
                "callback_port": result["callback_port"],
                "server_name": name,
            },
        })

        # No local callback server needed — the backend handles the
        # callback at /callback and sends the token via WS.

    except Exception as exc:
        logger.error("CLI auth start failed for %s: %s", name, exc, exc_info=True)
        await router.publish_event({
            "type": "mcp_cli_auth_failed",
            "request_id": request_id,
            "error": str(exc),
        })


def _prepare_auth(container_name: str, name: str, url: str) -> dict:
    """Read CLI credentials and build the OAuth URL."""

    # Step 1: Ensure server is added
    subprocess.run(
        ["docker", "exec", container_name, "claude", "mcp", "remove", name, "-s", "user"],
        capture_output=True, timeout=10,
    )
    subprocess.run(
        ["docker", "exec", container_name, "claude", "mcp", "remove", name, "-s", "local"],
        capture_output=True, timeout=10,
    )
    subprocess.run(
        ["docker", "exec", container_name, "claude", "mcp", "add",
         "--transport", "http", "--scope", "user", name, url],
        capture_output=True, text=True, timeout=15,
    )

    # Step 2: Run health check to create mcpOAuth entry
    subprocess.run(
        ["docker", "exec", container_name, "claude", "mcp", "list"],
        capture_output=True, text=True, timeout=30,
    )

    # Step 3: Read credentials
    creds_result = subprocess.run(
        ["docker", "exec", container_name, "cat", "/home/agent/.claude/.credentials.json"],
        capture_output=True, text=True, timeout=5,
    )
    if creds_result.returncode != 0:
        return {"error": "Could not read credentials file"}

    creds = json.loads(creds_result.stdout)
    mcp_oauth = creds.get("mcpOAuth", {})

    cred_key = None
    entry = None
    for key, val in mcp_oauth.items():
        if val.get("serverName") == name or key.startswith(f"{name}|"):
            cred_key = key
            entry = val
            break

    if not entry:
        return {"error": f"No OAuth credentials found for {name}"}

    if entry.get("accessToken") and entry.get("expiresAt", 0) > time.time() * 1000:
        return {"already_connected": True}

    client_id = entry.get("clientId", "")
    client_secret = entry.get("clientSecret", "")
    if not client_id:
        return {"error": f"No client_id found for {name}"}

    # Step 4: Discover OAuth endpoints
    discovery = entry.get("discoveryState", {})
    auth_server_url = discovery.get("authorizationServerUrl", "")
    if not auth_server_url:
        return {"error": f"No authorization server URL for {name}"}

    import httpx
    try:
        resp = httpx.get(f"{auth_server_url}/.well-known/oauth-authorization-server", timeout=10)
        metadata = resp.json()
    except Exception as exc:
        return {"error": f"OAuth discovery failed: {exc}"}

    authorization_endpoint = metadata.get("authorization_endpoint", "")
    token_endpoint = metadata.get("token_endpoint", "")
    if not authorization_endpoint or not token_endpoint:
        return {"error": "Missing OAuth endpoints"}

    # Step 5: Generate PKCE
    code_verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()

    # Step 6: Use the backend's /callback endpoint as redirect_uri.
    # This works because CLI-registered clients accept any localhost/callback URI.
    # For remote deployments, use the platform's public URL.
    backend_url = os.environ.get("CBCL_BACKEND_URL", "http://localhost:8000")
    redirect_uri = f"{backend_url}/callback"
    callback_port = 0  # Not using local callback server
    scope = entry.get("stepUpScope", "mcp:connect")

    # Step 7: Build auth URL
    parsed = urlparse(url)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "resource": f"{parsed.scheme}://{parsed.netloc}/",
    }
    auth_url = f"{authorization_endpoint}?{urlencode(params)}"

    return {
        "auth_url": auth_url,
        "code_verifier": code_verifier,
        "state": state,
        "client_id": client_id,
        "client_secret": client_secret,
        "token_endpoint": token_endpoint,
        "redirect_uri": redirect_uri,
        "cred_key": cred_key,
        "callback_port": callback_port,
    }


async def _run_callback_server(
    router: object,
    request_id: str,
    container_name: str,
    auth_state: dict,
) -> None:
    """Run localhost callback server to catch OAuth redirect."""
    callback_port = auth_state["callback_port"]
    expected_state = auth_state["state"]
    auth_code_holder: list[str | None] = [None]
    callback_received = threading.Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            code = params.get("code", [None])[0]
            state_param = params.get("state", [None])[0]

            if code and state_param == expected_state:
                auth_code_holder[0] = code
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body style='font-family:system-ui;display:flex;justify-content:center;"
                    b"align-items:center;min-height:100vh;margin:0'>"
                    b"<div style='text-align:center'>"
                    b"<h2 style='color:#16a34a'>Connected successfully!</h2>"
                    b"<p style='color:#666'>You can close this tab and return to Cubicle.</p>"
                    b"<script>setTimeout(()=>window.close(),2000)</script>"
                    b"</div></body></html>"
                )
            else:
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body><h2>Invalid callback</h2></body></html>")
            callback_received.set()

        def log_message(self, *args):
            pass

    try:
        httpd = HTTPServer(("127.0.0.1", callback_port), CallbackHandler)
        httpd.timeout = 1
    except OSError as exc:
        logger.error("Could not start callback server on port %d: %s", callback_port, exc)
        await router.publish_event({
            "type": "mcp_cli_auth_failed",
            "request_id": request_id,
            "error": f"Could not start callback server on port {callback_port}",
        })
        return

    def serve():
        while not callback_received.is_set():
            httpd.handle_request()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    logger.info("MCP auth callback server listening on port %d", callback_port)

    # Wait for callback (5 min max)
    await asyncio.to_thread(callback_received.wait, 300)
    httpd.server_close()

    code = auth_code_holder[0]
    if not code:
        await router.publish_event({
            "type": "mcp_cli_auth_failed",
            "request_id": request_id,
            "error": "Authentication timed out.",
        })
        return

    # Exchange code for token
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                auth_state["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": auth_state["redirect_uri"],
                    "code_verifier": auth_state["code_verifier"],
                },
                auth=httpx.BasicAuth(auth_state["client_id"], auth_state["client_secret"]),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            if resp.status_code != 200:
                raise RuntimeError(f"Token exchange failed: {resp.status_code} {resp.text[:200]}")

            token_data = resp.json()
            access_token = token_data.get("access_token", "")
            expires_in = token_data.get("expires_in", 3600)

            if not access_token:
                raise RuntimeError("No access_token in response")

            # Write token to container credentials
            write_token_to_credentials(container_name, auth_state.get("cred_key", ""), access_token, expires_in)
            clear_auth_cache(container_name)

            logger.info("MCP auth completed for request %s", request_id)
            await router.publish_event({
                "type": "mcp_cli_auth_complete",
                "request_id": request_id,
            })

    except Exception as exc:
        logger.error("Token exchange failed: %s", exc)
        await router.publish_event({
            "type": "mcp_cli_auth_failed",
            "request_id": request_id,
            "error": str(exc),
        })


def write_token_to_credentials(
    container_name: str,
    cred_key: str,
    access_token: str,
    expires_in: int = 3600,
) -> None:
    """Write an access token to the container's .credentials.json."""
    result = subprocess.run(
        ["docker", "exec", container_name, "cat", "/home/agent/.claude/.credentials.json"],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError("Could not read .credentials.json")

    creds = json.loads(result.stdout)
    mcp_oauth = creds.get("mcpOAuth", {})

    if cred_key and cred_key in mcp_oauth:
        mcp_oauth[cred_key]["accessToken"] = access_token
        mcp_oauth[cred_key]["expiresAt"] = int(time.time() * 1000) + (expires_in * 1000)
    else:
        # Find by iterating
        for key, val in mcp_oauth.items():
            if cred_key and cred_key in key:
                val["accessToken"] = access_token
                val["expiresAt"] = int(time.time() * 1000) + (expires_in * 1000)
                break

    creds["mcpOAuth"] = mcp_oauth
    creds_json = json.dumps(creds)
    write_result = subprocess.run(
        ["docker", "exec", "-i", container_name, "bash", "-c",
         "cat > /tmp/.creds_tmp.json && mv /tmp/.creds_tmp.json /home/agent/.claude/.credentials.json"],
        input=creds_json, capture_output=True, text=True, timeout=10,
    )
    if write_result.returncode != 0:
        raise RuntimeError(f"Failed to write credentials: {write_result.stderr[:200]}")
    logger.info("Wrote token to credentials (key=%s)", cred_key)


def clear_auth_cache(container_name: str) -> None:
    """Remove mcp-needs-auth-cache.json."""
    subprocess.run(
        ["docker", "exec", container_name, "rm", "-f", "/home/agent/.claude/mcp-needs-auth-cache.json"],
        capture_output=True, timeout=5,
    )
