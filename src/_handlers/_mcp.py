"""MCP add/remove handler bodies (split from handlers.py).

The OAuth-heavy flows live in ``_oauth.py``; this module covers
the plain ``mcp_add`` / ``mcp_remove`` paths plus the on-demand
``mcp_list`` refresh.

Security note (stdio path): the daemon runs ``docker exec`` with an
argv ARRAY (never a shell string), so shell metacharacters in user-
controlled fields (args / env values) are literal bytes that never
get parsed by a shell. Backend-side Pydantic validation gives us
arg-shape + env-name guarantees; the validators below are
defence-in-depth in case a future code change introduces a shell
string OR the backend's Pydantic layer is bypassed by a direct WS
producer (e.g. a test fixture). Keep BOTH gates in sync.
"""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess

logger = logging.getLogger(__name__)

# Defence-in-depth: re-validate inputs here too. The backend's
# Pydantic ``McpAddRequest`` is the primary gate, but a buggy
# producer (test, future feature) could bypass it. Keep this in
# lock-step with ``backend/app/connectors/router.py``.
_STDIO_COMMAND_ALLOWLIST: set[str] = {
    "npx", "uvx", "python3", "node", "deno",
}
_SAFE_STDIO_ARG_RE = re.compile(r"^[A-Za-z0-9@:/._\-+~,=]+$")
_SAFE_STDIO_ARG_MAX_LEN = 512
_ENV_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def _build_stdio_argv(
    container_name: str,
    name: str,
    command: str,
    args: list[str],
    env_vars: list[dict],
) -> list[str] | None:
    """Construct the ``docker exec ... claude mcp add ...`` argv array.

    Returns ``None`` if any input fails re-validation (caller should
    drop the request and log a warning). Otherwise returns the
    argv list ready for ``subprocess.run`` with ``shell=False``.

    Shape: ``docker exec <ctr> claude mcp add --scope user
    [--env K=V ...] <name> -- <command> [<args>...]``.

    Why ``--`` before the command + args: it tells ``claude mcp add``
    to stop parsing flags so a user-typed arg like ``-y`` isn't
    interpreted as a ``claude`` flag.
    """
    if command not in _STDIO_COMMAND_ALLOWLIST:
        logger.warning(
            "mcp_add stdio: command %r not in allowlist", command,
        )
        return None
    for arg in args:
        if not isinstance(arg, str):
            logger.warning("mcp_add stdio: non-string arg %r", arg)
            return None
        if len(arg) > _SAFE_STDIO_ARG_MAX_LEN:
            logger.warning(
                "mcp_add stdio: arg exceeds %d char cap",
                _SAFE_STDIO_ARG_MAX_LEN,
            )
            return None
        if not _SAFE_STDIO_ARG_RE.fullmatch(arg):
            logger.warning(
                "mcp_add stdio: arg %r contains shell metacharacters",
                arg,
            )
            return None
    seen_env: set[str] = set()
    for ev in env_vars:
        ev_name = ev.get("name") if isinstance(ev, dict) else None
        if not isinstance(ev_name, str) or not _ENV_VAR_NAME_RE.fullmatch(ev_name):
            logger.warning(
                "mcp_add stdio: invalid env var name %r", ev_name,
            )
            return None
        if ev_name in seen_env:
            logger.warning(
                "mcp_add stdio: duplicate env var name %r", ev_name,
            )
            return None
        seen_env.add(ev_name)

    argv: list[str] = [
        "docker", "exec", container_name,
        "claude", "mcp", "add", "--scope", "user",
    ]
    for ev in env_vars:
        # subprocess passes each arg verbatim — the ``=`` inside
        # ``--env KEY=VAL`` is part of one token, never re-parsed
        # by a shell. Env values can contain ANY bytes here.
        argv.extend(["--env", f"{ev['name']}={ev.get('value', '')}"])
    argv.append(name)
    argv.append("--")
    argv.append(command)
    argv.extend(args)
    return argv


async def run_mcp_add(
    msg: dict,
    *,
    container_name: str,
    refresh_mcp_list,
) -> None:
    """Add an MCP server inside the container via ``claude mcp add``.

    Branches on ``transport``:

    * ``http`` / ``sse`` — legacy URL-based add.
    * ``stdio`` — local process add (Perplexity, Brave, GitHub MCPs,
      anything that ships as an npm/pip/deno package). Constructs
      ``claude mcp add --scope user [--env K=V ...] <name> -- <cmd>
      [<args>...]`` via an argv array (no shell, no parsing).

    Re-validates stdio inputs as defence-in-depth — the backend's
    Pydantic ``McpAddRequest`` is the primary gate. Logs without
    env values so secrets don't appear in the operator log.
    """
    name = msg.get("name", "")
    transport = msg.get("transport", "http")
    if not name:
        logger.warning("mcp_add: missing name")
        return

    if transport == "stdio":
        command = msg.get("command", "")
        args = msg.get("args", []) or []
        env_vars = msg.get("env_vars", []) or []
        if not command:
            logger.warning("mcp_add stdio: missing command")
            return
        argv = _build_stdio_argv(
            container_name, name, command, args, env_vars,
        )
        if argv is None:
            return
        # Log SHAPE without env values — the names alone are
        # operationally useful (which secret is set?) without
        # leaking the value.
        env_names = sorted({ev.get("name", "") for ev in env_vars})
        log_summary = (
            f"transport=stdio command={command} args={len(args)} "
            f"env={env_names or '[]'}"
        )
    else:
        url = msg.get("url", "")
        if not url:
            logger.warning("mcp_add: missing url")
            return
        argv = [
            "docker", "exec", container_name,
            "claude", "mcp", "add", "--transport", transport,
            "--scope", "user",
            name, url,
        ]
        log_summary = f"transport={transport} url={url}"

    try:
        result = await asyncio.to_thread(
            subprocess.run, argv,
            capture_output=True, text=True, timeout=30,
        )
        logger.info(
            "mcp_add %s: %s, rc=%d, out=%s",
            name, log_summary, result.returncode, result.stdout[:200],
        )
        await refresh_mcp_list()
    except Exception as exc:
        logger.warning("mcp_add failed: %s", exc)


async def run_mcp_remove(
    msg: dict,
    *,
    container_name: str,
    refresh_mcp_list,
) -> None:
    """Remove an MCP server from the container.

    Tries removing from both user and local scopes since the server
    may exist in multiple scopes.
    """
    name = msg.get("name", "")
    if not name:
        return
    try:
        await asyncio.to_thread(
            subprocess.run,
            ["docker", "exec", container_name,
             "claude", "mcp", "remove", name, "-s", "user"],
            capture_output=True, text=True, timeout=15,
        )
        await asyncio.to_thread(
            subprocess.run,
            ["docker", "exec", container_name,
             "claude", "mcp", "remove", name, "-s", "local"],
            capture_output=True, text=True, timeout=15,
        )
        logger.info("mcp_remove %s: removed from all scopes", name)
        await refresh_mcp_list()
    except Exception as exc:
        logger.warning("mcp_remove failed: %s", exc)
