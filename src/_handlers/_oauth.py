"""OAuth + token-write handler bodies (split from handlers.py).

Every handler in this module that takes a server ``name`` from the
WS payload MUST re-validate against ``_MCP_NAME_RE`` before any
``docker exec ... claude mcp ... <name>`` invocation. The backend's
Pydantic ``McpAuthenticateRequest`` / ``McpConnectRequest`` /
``McpCliAuthRequest`` / ``McpCliAuthCodeRequest`` are the primary
gate — but a direct WS post (test fixture, future producer, or
backend regression) could bypass them. Without the daemon-side
re-validation a name starting with ``-`` would be argv-parsed as
a flag by ``claude mcp add`` itself, so the http-injection defence
that argv arrays provide doesn't reach claude's own argparse layer.

Three entrypoints map to backend messages:

- ``run_mcp_authenticate`` — full OAuth-via-CLI flow. Installs a
  fake browser inside the container, runs ``claude mcp add`` so
  the CLI emits its OAuth URL, captures the URL, proxies the
  callback from host → container.
- ``run_cli_auth`` — thin shim around ``src.mcp_auth.start_cli_auth``.
- ``run_mcp_write_token`` — direct write into the container's
  credentials store (used when a frontend OAuth flow finishes
  outside the CLI). This is the live path the backend's OAuth proxy
  uses (``mcp_write_token``).

T8.3.7 removed the dead ``run_mcp_oauth_callback`` (local-callback
flow) and ``run_mcp_token_ready`` (Redis-token flow) handlers: the
backend never sent ``mcp_oauth_callback`` / ``mcp_token_ready``
(``publish_mcp_command`` only emits add / remove / list /
authenticate / cli_auth / cli_auth_code, and the OAuth proxy sends
``mcp_write_token``).

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

logger = logging.getLogger(__name__)


# Defence-in-depth name regex — mirrors ``_handlers._mcp._MCP_NAME_RE``
# and ``backend/app/connectors/router.py:MCP_NAME_RE``. Kept inline to
# avoid a cross-module import that would pull in subprocess args plus
# claude_agent_sdk transitively in modules that just need to validate
# a name string.
_MCP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,99}$")


def _name_or_warn(action: str, name: str) -> bool:
    """Return True if ``name`` is a safe MCP server name.

    Logs WARNING and returns False on failure so the caller can
    short-circuit. Centralised so every OAuth-path handler uses the
    SAME shape gate and a future regex tightening lands in ONE
    place.
    """
    if not _MCP_NAME_RE.fullmatch(name):
        logger.warning(
            "%s: name %r fails name regex — refused", action, name,
        )
        return False
    return True


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
    if not _name_or_warn("mcp_authenticate", name):
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
    if not _name_or_warn("mcp_cli_auth", name):
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
    if not _name_or_warn("mcp_write_token", name):
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
