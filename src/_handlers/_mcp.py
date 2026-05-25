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
# producer (test, future feature, direct WS post-bypass) could
# bypass it. Keep this in lock-step with
# ``backend/app/connectors/router.py``; a sibling test
# (``tests/test_mcp_constants_lockstep.py``) loads both copies and
# asserts they match so a one-sided edit fails CI.
_STDIO_COMMAND_ALLOWLIST: set[str] = {
    "npx", "uvx", "python3", "node", "deno",
}
_SAFE_STDIO_ARG_RE = re.compile(r"^[A-Za-z0-9@:/._\-+~,=]+$")
_SAFE_STDIO_ARG_MAX_LEN = 512
_ENV_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_MCP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,99}$")
# Mirror the backend's per-list Field(max_length=...). Guards
# against a payload that smuggled past Pydantic from blowing up the
# argv (or ARG_MAX) inside the container.
_STDIO_ARGS_MAX = 64
_STDIO_ENV_VARS_MAX = 32

# Regex used to scrub ``--env KEY=VALUE`` flag pairs from any log
# line that contains stdout / stderr captured from ``claude mcp
# add``. Replaces the VALUE with ``[REDACTED]`` so a curious
# operator reading the log can't read API keys that the CLI may
# have echoed back in a confirmation message.
_ENV_FLAG_VALUE_SCRUB_RE = re.compile(
    r"(--env\s+[A-Z][A-Z0-9_]*=)\S+"
)


def _scrub_env_values(s: str) -> str:
    """Return ``s`` with ``--env KEY=VALUE`` collapsed to ``--env KEY=[REDACTED]``.

    Belt-and-braces: ``claude mcp add`` doesn't seem to echo env
    values in its current stdout, but a future CLI version might.
    Better to redact preemptively than to leak.
    """
    return _ENV_FLAG_VALUE_SCRUB_RE.sub(r"\1[REDACTED]", s)


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

    All validations mirror ``backend/app/connectors/router.py``.
    Refusals log at WARNING level so an operator can spot a payload
    that bypassed the backend (= a backend regression we want to
    notice immediately).
    """
    # Name: refuse leading ``-`` (argparse-injection at the
    # ``claude mcp add`` layer), refuse slashes / control chars
    # that would corrupt ``~/.claude.json``. Backend Pydantic does
    # the same check.
    if not _MCP_NAME_RE.fullmatch(name):
        logger.warning("mcp_add stdio: name %r fails name regex", name)
        return None

    if command not in _STDIO_COMMAND_ALLOWLIST:
        logger.warning(
            "mcp_add stdio: command %r not in allowlist", command,
        )
        return None

    if len(args) > _STDIO_ARGS_MAX:
        logger.warning(
            "mcp_add stdio: args list exceeds %d cap (got %d)",
            _STDIO_ARGS_MAX, len(args),
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

    if len(env_vars) > _STDIO_ENV_VARS_MAX:
        logger.warning(
            "mcp_add stdio: env_vars list exceeds %d cap (got %d)",
            _STDIO_ENV_VARS_MAX, len(env_vars),
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
        # Refuse NUL / CR / LF in env values — these break subprocess
        # argv (NUL) or corrupt ``~/.claude.json`` (CR / LF). Backend
        # refuses too via the per-env-var ``McpStdioEnvVar`` validator.
        ev_value = ev.get("value", "") if isinstance(ev, dict) else ""
        if not isinstance(ev_value, str):
            logger.warning(
                "mcp_add stdio: env value for %r is not a string",
                ev_name,
            )
            return None
        if any(ch in ev_value for ch in ("\x00", "\n", "\r")):
            logger.warning(
                "mcp_add stdio: env value for %r contains forbidden "
                "char (NUL / CR / LF)", ev_name,
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
        # by a shell. Env values can contain ANY bytes here (modulo
        # the NUL / CR / LF check above).
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
    router=None,
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

    When ``router`` is provided, publishes a board-WS ``mcp_add_result``
    event after the subprocess returns so the UI gets explicit
    success / failure feedback instead of waiting for the
    background ``mcp_list_updated`` poll. The event payload is
    ``{type, name, transport, status: "added"|"failed"|"timed_out",
    error: str|None}`` — no env values, no stdout dump.
    """
    name = msg.get("name", "")
    transport = msg.get("transport", "http")

    async def _emit_result(status: str, error: str | None = None) -> None:
        """Best-effort WS notification of the add outcome.

        Failures publishing the event are swallowed (the operator
        still has the daemon log, and a missed event is far better
        than a crashed handler).
        """
        if router is None:
            return
        try:
            await router.publish_event({
                "type": "mcp_add_result",
                "name": name,
                "transport": transport,
                "status": status,
                "error": error,
            })
        except Exception as exc:
            logger.debug("mcp_add_result publish failed: %s", exc)
    if not name:
        logger.warning("mcp_add: missing name")
        await _emit_result("failed", "missing name")
        return

    if transport == "stdio":
        command = msg.get("command", "")
        args = msg.get("args", []) or []
        env_vars = msg.get("env_vars", []) or []
        if not command:
            logger.warning("mcp_add stdio: missing command")
            await _emit_result("failed", "missing command")
            return
        argv = _build_stdio_argv(
            container_name, name, command, args, env_vars,
        )
        if argv is None:
            await _emit_result("failed", "validation refused payload")
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
        # Defence-in-depth: same name regex applies to the http/sse
        # path. The url field is delivered verbatim to claude as a
        # positional argv, so a url starting with ``-`` would still
        # confuse claude's own flag parser.
        if not _MCP_NAME_RE.fullmatch(name):
            logger.warning(
                "mcp_add %s: name fails name regex", transport,
            )
            await _emit_result("failed", "name fails name regex")
            return
        url = msg.get("url", "")
        if not url:
            logger.warning("mcp_add: missing url")
            await _emit_result("failed", "missing url")
            return
        if not (url.startswith("http://") or url.startswith("https://")):
            logger.warning(
                "mcp_add %s: url %r must start with http(s)://",
                transport, url,
            )
            await _emit_result("failed", "url must start with http(s)://")
            return
        argv = [
            "docker", "exec", container_name,
            "claude", "mcp", "add", "--transport", transport,
            "--scope", "user",
            name, url,
        ]
        log_summary = f"transport={transport} url={url}"

    # 120 s timeout for stdio mode: ``npx -y @some/mcp-server`` can
    # spend most of that on the first-time package install (downloads
    # + dependency tree + compile of native deps). 30 s was enough
    # for HTTP adds but starved stdio installs on slow networks —
    # the subprocess timed out, the handler returned silently, no
    # refresh fired, and the user saw nothing happen in the UI.
    # HTTP transport keeps the 30 s budget; nothing to install.
    add_timeout = 120 if transport == "stdio" else 30
    try:
        result = await asyncio.to_thread(
            subprocess.run, argv,
            capture_output=True, text=True, timeout=add_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning(
            "mcp_add %s: timed out after %ds (%s)",
            name, add_timeout, exc,
        )
        await _emit_result(
            "timed_out",
            f"claude mcp add timed out after {add_timeout}s — "
            "the package may be slow to install on this network",
        )
        return
    except (subprocess.SubprocessError, OSError) as exc:
        # OSError covers "docker not on PATH" / "container vanished"
        # — failures that don't indicate a malicious payload but DO
        # need to be visible.
        logger.warning("mcp_add %s: subprocess failure: %s", name, exc)
        await _emit_result("failed", f"{type(exc).__name__}: {exc}")
        return
    # ``result.stdout[:200]`` could in principle echo back the
    # ``--env KEY=VAL`` flag if a future ``claude mcp add`` version
    # logs a "added with env vars: ..." line. Scrub VALUEs before
    # we ever write them to the operator log.
    scrubbed_out = _scrub_env_values(result.stdout[:200] or "")
    logger.info(
        "mcp_add %s: %s, rc=%d, out=%s",
        name, log_summary, result.returncode, scrubbed_out,
    )
    if result.returncode != 0:
        # Non-zero exit from ``claude mcp add`` (e.g. npm 404, name
        # collision, network refused). Surface to the UI with the
        # CLI's stderr so the operator knows WHY the install
        # failed. stdout is scrubbed; stderr too as a precaution.
        scrubbed_err = _scrub_env_values(
            (result.stderr or "").strip()[:400],
        )
        await _emit_result(
            "failed",
            scrubbed_err or f"rc={result.returncode}",
        )
        # Refresh anyway in case partial state landed in
        # ~/.claude.json (claude mcp add is mostly atomic but
        # safer to assume not).
        await refresh_mcp_list(force=True)
        return
    # Force=True bypasses the periodic-refresh 5s debounce. Without
    # it, an add fired within 5s of office startup (or another
    # mutation) silently no-ops the refresh and the UI never
    # learns the new server exists.
    await refresh_mcp_list(force=True)
    await _emit_result("added")


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
    # Defence-in-depth: backend ``McpRemoveRequest`` enforces the
    # name regex, but a direct WS post (test fixture, future
    # producer) could bypass it. The name lands as a positional
    # argv to ``claude mcp remove``; a leading ``-`` would be
    # parsed as a flag by claude itself.
    if not _MCP_NAME_RE.fullmatch(name):
        logger.warning("mcp_remove: name %r fails name regex", name)
        return
    for scope in ("user", "local"):
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["docker", "exec", container_name,
                 "claude", "mcp", "remove", name, "-s", scope],
                capture_output=True, text=True, timeout=15,
            )
        except subprocess.TimeoutExpired as exc:
            logger.warning(
                "mcp_remove %s: %s-scope timed out (%s)",
                name, scope, exc,
            )
            return
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning(
                "mcp_remove %s: %s-scope subprocess failure: %s",
                name, scope, exc,
            )
            return
    logger.info("mcp_remove %s: removed from all scopes", name)
    # Same debounce-bypass rationale as ``run_mcp_add``.
    await refresh_mcp_list(force=True)
