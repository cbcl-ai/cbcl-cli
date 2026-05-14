"""OAuth + token-write handler bodies (split from handlers.py).

Five entrypoints map to backend messages:

- ``run_mcp_oauth_callback`` — local OAuth-code callback server +
  token exchange + claude-mcp-add. The longest path: starts an
  HTTPServer thread on the user's host, waits 5 min for the
  redirect, exchanges the auth code with the MCP server's token
  endpoint, then registers the server inside the office container.
- ``run_mcp_authenticate`` — full OAuth-via-CLI flow. Installs a
  fake browser inside the container, runs ``claude mcp add`` so
  the CLI emits its OAuth URL, captures the URL, proxies the
  callback from host → container.
- ``run_mcp_token_ready`` — token arrived via the backend's OAuth
  proxy (remote mode). Read from Redis, register the MCP server.
- ``run_cli_auth`` — thin shim around ``src.mcp_auth.start_cli_auth``.
- ``run_mcp_write_token`` — direct write into the container's
  credentials store (used when a frontend OAuth flow finishes
  outside the CLI).

Every function takes its dependencies as explicit args so it stays
testable and decoupled from handlers.py's closure scope.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
import re
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import httpx

logger = logging.getLogger(__name__)


async def run_mcp_oauth_callback(
    msg: dict,
    *,
    container_name: str,
    office_id: str,
    redis_client,
    refresh_mcp_list,
) -> None:
    """Local-callback OAuth flow."""
    name = msg.get("name", "")
    url = msg.get("url", "")
    callback_port = msg.get("callback_port", 0)
    expected_state = msg.get("state", "")
    code_verifier = msg.get("code_verifier", "")
    token_endpoint = msg.get("token_endpoint", "")
    client_id = msg.get("client_id", "")
    callback_url = msg.get("callback_url", "")

    if not name or not callback_port:
        logger.warning("mcp_oauth_callback: missing required params")
        return

    status_key = f"office:{office_id}:mcp_connect_status:{name}"

    auth_code: list[str | None] = [None]
    callback_received = threading.Event()

    class _OAuthCallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — http.server convention
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            code = params.get("code", [None])[0]
            state = params.get("state", [None])[0]

            if code and state == expected_state and not auth_code[0]:
                auth_code[0] = code
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body>"
                    b"<h2>Connected successfully!</h2>"
                    b"<p>You can close this tab and return to Cubicle.</p>"
                    b"<script>window.close()</script>"
                    b"</body></html>"
                )
                callback_received.set()
            else:
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h2>Invalid callback</h2></body></html>"
                )

        def log_message(self, format, *args):  # noqa: A002, ARG002
            pass

    try:
        server = HTTPServer(
            ("127.0.0.1", callback_port), _OAuthCallbackHandler,
        )
        server.timeout = 1
    except OSError as exc:
        logger.warning(
            "mcp_oauth_callback %s: bind failed port %d: %s",
            name, callback_port, exc,
        )
        await redis_client.set(status_key, "failed", ex=300)
        return

    def _serve():
        while not callback_received.is_set():
            server.handle_request()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    logger.info(
        "mcp_oauth_callback %s: listening on port %d", name, callback_port,
    )

    for _ in range(300):
        if callback_received.is_set():
            break
        await asyncio.sleep(1)

    callback_received.set()
    thread.join(timeout=5)
    server.server_close()

    if not auth_code[0]:
        logger.warning(
            "mcp_oauth_callback %s: no auth code received "
            "(timeout or invalid)", name,
        )
        await redis_client.set(status_key, "timeout", ex=300)
        return

    logger.info("mcp_oauth_callback %s: exchanging code for token", name)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": auth_code[0],
                    "redirect_uri": callback_url,
                    "client_id": client_id,
                    "code_verifier": code_verifier,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            if resp.status_code != 200:
                logger.warning(
                    "mcp_oauth_callback %s: token exchange failed: %d %s",
                    name, resp.status_code, resp.text[:200],
                )
                await redis_client.set(status_key, "failed", ex=300)
                return
            tokens = resp.json()
    except Exception as exc:
        logger.warning(
            "mcp_oauth_callback %s: token exchange error: %s", name, exc,
        )
        await redis_client.set(status_key, "failed", ex=300)
        return

    access_token = tokens.get("access_token", "")
    if not access_token:
        logger.warning(
            "mcp_oauth_callback %s: no access_token in response", name,
        )
        await redis_client.set(status_key, "failed", ex=300)
        return

    logger.info(
        "mcp_oauth_callback %s: adding MCP server with token", name,
    )
    transport = msg.get("transport", "http")
    add_cmd = [
        "docker", "exec", container_name,
        "claude", "mcp", "add",
        "--transport", transport,
        "--header", f"Authorization: Bearer {access_token}",
        "--scope", "user",
        name, url,
    ]
    try:
        await asyncio.to_thread(subprocess.run, [
            "docker", "exec", container_name, "claude", "mcp", "remove", name,
        ], capture_output=True, timeout=10)

        result = await asyncio.to_thread(
            subprocess.run, add_cmd,
            capture_output=True, text=True, timeout=30,
        )
        logger.info(
            "mcp_oauth_callback %s: add rc=%d out=%s",
            name, result.returncode, result.stdout[:200],
        )
    except Exception as exc:
        logger.warning("mcp_oauth_callback %s: add failed: %s", name, exc)
        await redis_client.set(status_key, "failed", ex=300)
        return

    await redis_client.set(status_key, "completed", ex=300)
    await refresh_mcp_list()
    logger.info("mcp_oauth_callback %s: CONNECTED!", name)


async def run_mcp_authenticate(
    msg: dict,
    *,
    container_name: str,
    router,
    refresh_mcp_list,
) -> None:
    """OAuth via CLI: install URL interceptor, run claude mcp add,
    capture the OAuth URL, proxy it. Runs the heavy lifting in a
    daemon thread so the asyncio reader loop isn't blocked."""
    name = msg.get("name", "")
    if not name:
        return

    logger.info("Authenticating MCP server: %s", name)
    main_loop = asyncio.get_running_loop()

    def _run_auth():
        interceptor_script = r'''#!/bin/bash
echo "CAPTURED_URL: $1" >> /tmp/captured_mcp_urls.txt'''
        try:
            subprocess.run(
                ["docker", "exec", container_name, "bash", "-c",
                 f"rm -f /tmp/captured_mcp_urls.txt; echo '{interceptor_script}' > /usr/local/bin/xdg-open && chmod +x /usr/local/bin/xdg-open && cp /usr/local/bin/xdg-open /usr/local/bin/open 2>/dev/null || true"],
                capture_output=True, text=True, timeout=10,
            )
        except Exception as exc:
            logger.warning("Failed to install URL interceptor: %s", exc)
            return

        server_url = msg.get("url", "")
        if not server_url:
            logger.warning("No URL provided for MCP authenticate %s", name)
            return

        for scope in ("user", "local"):
            result = subprocess.run(
                ["docker", "exec", container_name,
                 "claude", "mcp", "remove", name, "-s", scope],
                capture_output=True, text=True, timeout=10,
            )
            logger.info(
                "mcp auth remove %s (scope=%s): rc=%d",
                name, scope, result.returncode,
            )

        time.sleep(1)

        logger.info("mcp auth: re-adding %s with URL interceptor", name)
        auth_proc = subprocess.Popen(
            ["docker", "exec", "-e", "BROWSER=xdg-open", "-i",
             container_name, "claude", "mcp", "add",
             "--transport", "http", name, server_url],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        url = None
        port = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            time.sleep(1)
            if auth_proc.poll() is not None:
                out = (
                    auth_proc.stdout.read().decode()
                    if auth_proc.stdout else ""
                )
                logger.info(
                    "mcp authenticate exited early (rc=%s): %s",
                    auth_proc.returncode, out[:300],
                )
                break
            try:
                result = subprocess.run(
                    ["docker", "exec", container_name,
                     "cat", "/tmp/captured_mcp_urls.txt"],
                    capture_output=True, text=True, timeout=5,
                )
                if (
                    result.returncode == 0
                    and "CAPTURED_URL:" in result.stdout
                ):
                    captured = (
                        result.stdout.strip().split("CAPTURED_URL: ")[-1]
                    )
                    port_match = re.search(
                        r"localhost(?:%3A|:)(\d+)", captured,
                    )
                    if port_match:
                        url = captured
                        port = int(port_match.group(1))
                        break
            except Exception:
                pass

        if not url or not port:
            logger.warning("Could not capture MCP auth URL for %s", name)
            auth_proc.terminate()
            return

        logger.info(
            "Captured MCP auth URL on port %d for %s", port, name,
        )

        main_loop.call_soon_threadsafe(
            asyncio.ensure_future,
            router.publish_event({
                "type": "mcp_auth_url",
                "name": name,
                "auth_url": url,
            }),
        )

        callback_done = threading.Event()

        class Proxy(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                try:
                    body = subprocess.run(
                        ["docker", "exec", container_name,
                         "curl", "-s", "-S", "--max-time", "10",
                         f"http://localhost:{port}{self.path}"],
                        capture_output=True, timeout=15,
                    ).stdout
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(
                        body or (
                            b"<html><body><h2>Authentication successful!"
                            b"</h2><p>You can close this tab.</p>"
                            b"</body></html>"
                        )
                    )
                except Exception:
                    self.send_response(500)
                    self.end_headers()
                callback_done.set()

            def log_message(self, *args):  # noqa: A002, ARG002
                pass

        try:
            httpd = HTTPServer(("127.0.0.1", port), Proxy)
            httpd.timeout = 120
            logger.info("MCP auth proxy listening on host port %d", port)

            while not callback_done.is_set():
                httpd.handle_request()
                if auth_proc.poll() is not None:
                    break

            httpd.server_close()
        except OSError as exc:
            logger.warning(
                "Could not start MCP auth proxy on port %d: %s", port, exc,
            )
            auth_proc.terminate()
            return

        try:
            auth_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            auth_proc.terminate()

        logger.info(
            "MCP authenticate completed for %s (rc=%s)",
            name, auth_proc.returncode,
        )

        subprocess.run(
            ["docker", "exec", container_name,
             "rm", "-f", "/tmp/captured_mcp_urls.txt"],
            capture_output=True, timeout=5,
        )

    def _run_and_refresh():
        _run_auth()
        main_loop.call_soon_threadsafe(
            asyncio.ensure_future, refresh_mcp_list(),
        )

    threading.Thread(target=_run_and_refresh, daemon=True).start()


async def run_mcp_token_ready(
    msg: dict,
    *,
    container_name: str,
    office_id: str,
    redis_client,
    refresh_mcp_list,
) -> None:
    """Token arrived via the backend's OAuth proxy (remote mode).

    The backend exchanged the OAuth code and pushed the token into
    Redis at ``office:{oid}:mcp_token:{name}``. Read it, register the
    MCP server with the auth header, delete the token from Redis.
    """
    name = msg.get("name", "")
    url = msg.get("url", "")
    if not name or not url:
        return

    token_key = f"office:{office_id}:mcp_token:{name}"
    token_val = await redis_client.get(token_key)
    if not token_val:
        logger.warning("mcp_token_ready %s: no token in Redis", name)
        return

    access_token = (
        token_val.decode() if isinstance(token_val, bytes) else str(token_val)
    )
    await redis_client.delete(token_key)

    await asyncio.to_thread(subprocess.run, [
        "docker", "exec", container_name, "claude", "mcp", "remove", name,
    ], capture_output=True, timeout=10)

    add_cmd = [
        "docker", "exec", container_name,
        "claude", "mcp", "add",
        "--transport", "http",
        "--header", f"Authorization: Bearer {access_token}",
        "--scope", "user",
        name, url,
    ]
    result = await asyncio.to_thread(
        subprocess.run, add_cmd,
        capture_output=True, text=True, timeout=30,
    )
    logger.info("mcp_token_ready %s: rc=%d", name, result.returncode)

    await refresh_mcp_list()


async def run_cli_auth(
    msg: dict,
    *,
    router,
    container_name: str,
    refresh_mcp_list,
) -> None:
    """Thin shim around ``src.mcp_auth.start_cli_auth``."""
    from src.mcp_auth import start_cli_auth as _start_cli_auth

    logger.info(">>> mcp_cli_auth handler called: %s", msg.get("name", ""))
    request_id = msg.get("request_id", "")
    name = msg.get("name", "")
    url = msg.get("url", "")
    if not name or not url:
        return
    await _start_cli_auth(
        router=router,
        request_id=request_id,
        name=name,
        url=url,
        container_name=container_name,
    )
    await refresh_mcp_list()


async def run_mcp_write_token(
    msg: dict,
    *,
    container_name: str,
    mcp_refresh_state,
    refresh_mcp_list,
) -> None:
    """Write an access token directly to the container's credentials.

    Bypasses the refresh debounce so the new status is visible
    immediately in the connectors UI.
    """
    from src.mcp_auth import clear_auth_cache, write_token_to_credentials

    name = msg.get("name", "")
    access_token = msg.get("access_token", "")
    expires_in = msg.get("expires_in", 3600)
    if not name or not access_token:
        return

    try:
        await asyncio.to_thread(
            write_token_to_credentials,
            container_name, name, access_token, expires_in,
        )
        await asyncio.to_thread(clear_auth_cache, container_name)
        logger.info("MCP token written for %s", name)
        mcp_refresh_state.last = 0
        await refresh_mcp_list()
    except Exception as exc:
        logger.warning("Failed to write MCP token for %s: %s", name, exc)
