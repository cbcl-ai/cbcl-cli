"""Stateful MCP connector OAuth login (PTY-based paste-back).

``claude mcp login <name> --no-browser`` needs an interactive TTY to
accept the pasted redirect URL — a piped stdin is refused with "stdin
isn't a terminal, so authentication can't be completed here". So we
allocate a PTY, run the CLI under ``docker exec -it``, capture the
authorize URL it prints, keep the process alive, and later write the
user-pasted redirect URL into the PTY to finish the token exchange.

This is the DIRECT-connect path for the ~90% of connectors that support
Dynamic Client Registration (Sentry, Notion, Linear, …): the connector
authenticates on its OWN OAuth site, the token lands in the office
container, and it becomes a normal container connector — removable via
``claude mcp remove``. Account (``claude.ai *``) connectors instead print
a claude.ai URL and need no paste-back (``needs_callback=False``); the
~10% that can't self-register (Google-class) surface an error the caller
maps to the "add it in the Claude app" fallback.

The login sessions live in a module-level registry (one daemon = one
process) keyed by (container, connector). Sessions are swept on start and
have a TTL so an abandoned authorize doesn't leak a PTY forever.
"""
from __future__ import annotations

import logging
import os
import pty
import re
import select
import subprocess
import time

logger = logging.getLogger(__name__)

# First https URL in the CLI output is the authorize URL.
_URL_RE = re.compile(r"https://[^\s\x1b]+")
# Strip ANSI escape sequences so prompt/label matching + error extraction
# work on the plain text (the CLI emits colour + cursor-move codes).
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[=>]|\r")

# Markers in the CLI output (matched case-insensitively on ANSI-stripped
# text). These are copy-dependent, so keep the set broad.
_PASTE_PROMPT_MARKERS = ("paste the redirect url", "waiting for authorization")
_ACCOUNT_MARKERS = ("available the next time you start", "authorized on claude.ai")
_SUCCESS_MARKERS = ("authentication successful", "successfully authenticated", "connected to")
_FAILURE_MARKERS = ("couldn't complete", "could not complete", "authentication failed", "error")

_SESSION_TTL_SECONDS = 600.0  # 10 min: plenty of time to authorize in a browser.

# key -> {"proc": Popen, "master": int fd, "created": monotonic}
_SESSIONS: dict[str, dict] = {}


def _key(container_name: str, name: str) -> str:
    return f"{container_name}\x00{name}"


def _strip(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _first_meaningful_line(text: str) -> str:
    for line in _strip(text).splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("http"):
            return stripped
    return ""


def _valid_name(name: str) -> bool:
    # Refuse only genuine argv hazards; account connector names contain
    # spaces ("claude.ai Notion").
    return bool(name) and not name.startswith("-") and all(ord(c) >= 0x20 for c in name)


def _ensure_added(
    container_name: str, name: str, url: str, transport: str,
) -> None:
    """Add ``name`` as a container connector if it isn't present yet.

    Makes "connect a marketplace connector" a single, race-free call: the
    connector must exist before ``claude mcp login`` can authenticate it,
    and add is otherwise a fire-and-forget command. Idempotent — if the
    connector is already configured we skip the add.
    """
    try:
        present = subprocess.run(
            ["docker", "exec", container_name, "claude", "mcp", "get", name],
            capture_output=True, text=True, timeout=15,
        )
        if present.returncode == 0:
            return  # already configured
    except (subprocess.SubprocessError, OSError):
        pass  # fall through and try the add
    tport = "sse" if transport == "sse" else "http"
    try:
        subprocess.run(
            ["docker", "exec", container_name, "claude", "mcp", "add",
             "--transport", tport, name, url],
            capture_output=True, text=True, timeout=20,
        )
        logger.info("mcp_login_start %s: added container connector (%s)", name, tport)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("mcp_login_start %s: add failed: %s", name, exc)


def _read_available(master: int, *, window: float = 0.4) -> str:
    """Drain whatever is currently readable from the PTY, briefly."""
    out = ""
    deadline = time.monotonic() + window
    while time.monotonic() < deadline:
        r, _, _ = select.select([master], [], [], 0.1)
        if master not in r:
            break
        try:
            chunk = os.read(master, 4096)
        except OSError:
            break
        if not chunk:
            break
        out += chunk.decode(errors="replace")
    return out


def _cleanup(key: str) -> None:
    session = _SESSIONS.pop(key, None)
    if not session:
        return
    proc = session.get("proc")
    if proc is not None:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    fd = session.get("master")
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


def _sweep() -> None:
    now = time.monotonic()
    for key, session in list(_SESSIONS.items()):
        proc = session.get("proc")
        expired = now - session.get("created", now) > _SESSION_TTL_SECONDS
        dead = proc is None or proc.poll() is not None
        if expired or dead:
            _cleanup(key)


def start_login(
    container_name: str,
    name: str,
    *,
    url: str | None = None,
    transport: str = "http",
    read_timeout: float = 25.0,
) -> dict:
    """Start ``claude mcp login`` under a PTY.

    Returns ``{"auth_url", "needs_callback", "error"}``:
      * ``auth_url`` set + ``needs_callback=True``  → container/DCR
        connector: open the URL, then call :func:`complete_login` with the
        pasted redirect URL.
      * ``auth_url`` set + ``needs_callback=False`` → account connector:
        open the URL; it auto-connects, no paste-back.
      * ``auth_url=None`` + ``error`` → couldn't start / no URL (caller may
        fall back to the Claude-app instruction for the 10%).
    """
    if not container_name:
        return {"auth_url": None, "needs_callback": False,
                "error": "office container is not running — start it (cbcl start) and retry"}
    if not _valid_name(name):
        return {"auth_url": None, "needs_callback": False, "error": "invalid connector name"}

    _sweep()
    _cleanup(_key(container_name, name))  # drop any stale session for this connector

    # Connecting a NEW marketplace connector: add it as a container
    # connector first (race-free) so `claude mcp login` has something to
    # authenticate. No-op when re-authenticating an existing connector.
    if url:
        _ensure_added(container_name, name, url, transport)

    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            ["docker", "exec", "-it", container_name,
             "claude", "mcp", "login", name, "--no-browser"],
            stdin=slave, stdout=slave, stderr=slave, close_fds=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        os.close(master)
        os.close(slave)
        logger.warning("mcp_login_start %s: spawn failed: %s", name, exc)
        return {"auth_url": None, "needs_callback": False,
                "error": f"failed to start login: {type(exc).__name__}"}
    os.close(slave)

    buf = ""
    url: str | None = None
    deadline = time.monotonic() + read_timeout
    while time.monotonic() < deadline and url is None:
        r, _, _ = select.select([master], [], [], 0.5)
        if master in r:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk.decode(errors="replace")
            match = _URL_RE.search(_strip(buf))
            if match:
                url = match.group(0).rstrip(").,]}\"'")
        elif proc.poll() is not None:
            break

    if not url:
        buf += _read_available(master)
        os.close(master)
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        msg = _first_meaningful_line(buf) or "no authorization URL returned"
        logger.info("mcp_login_start %s: no URL (%s)", name, msg)
        return {"auth_url": None, "needs_callback": False, "error": msg}

    # Got the URL. Read a bit more to classify: does it prompt for a
    # paste-back (container/DCR) or say "available next session" (account)?
    time.sleep(0.3)
    combined = _strip(buf + _read_available(master)).lower()
    wants_paste = any(m in combined for m in _PASTE_PROMPT_MARKERS)
    account_style = any(m in combined for m in _ACCOUNT_MARKERS)
    alive = proc.poll() is None

    if account_style or (not alive and not wants_paste):
        # Account connector (or the process already exited): no paste-back.
        os.close(master)
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        logger.info("mcp_login_start %s: account-style URL (no callback)", name)
        return {"auth_url": url, "needs_callback": False, "error": None}

    _SESSIONS[_key(container_name, name)] = {
        "proc": proc, "master": master, "created": time.monotonic(),
    }
    logger.info("mcp_login_start %s: DCR URL, awaiting paste-back", name)
    return {"auth_url": url, "needs_callback": True, "error": None}


def complete_login(
    container_name: str,
    name: str,
    callback_url: str,
    *,
    read_timeout: float = 40.0,
) -> dict:
    """Finish a DCR login by writing the pasted redirect URL to the PTY.

    Returns ``{"success": bool, "error": str|None}``.
    """
    key = _key(container_name, name)
    session = _SESSIONS.get(key)
    if session is None:
        return {"success": False,
                "error": "no pending login for this connector (it may have expired) — click Authenticate again"}
    proc = session["proc"]
    master = session["master"]
    if proc.poll() is not None:
        _cleanup(key)
        return {"success": False, "error": "the login session ended — click Authenticate again"}
    if not callback_url or not callback_url.strip():
        return {"success": False, "error": "paste the full redirect URL from your browser"}

    try:
        os.write(master, callback_url.strip().encode() + b"\n")
    except OSError as exc:
        _cleanup(key)
        return {"success": False, "error": f"failed to submit the redirect URL: {exc}"}

    out = ""
    deadline = time.monotonic() + read_timeout
    while time.monotonic() < deadline:
        r, _, _ = select.select([master], [], [], 0.5)
        if master in r:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk.decode(errors="replace")
        if proc.poll() is not None:
            out += _read_available(master)
            break

    _cleanup(key)
    plain = _strip(out).lower()
    success = any(m in plain for m in _SUCCESS_MARKERS) and "couldn't complete" not in plain \
        and "could not complete" not in plain
    if success:
        logger.info("mcp_login_complete %s: success", name)
        return {"success": True, "error": None}
    # Extract the CLI's reason after "Couldn't complete authentication…".
    reason = ""
    for line in _strip(out).splitlines():
        low = line.lower()
        if any(m in low for m in _FAILURE_MARKERS):
            reason = line.strip()
            break
    logger.info("mcp_login_complete %s: failed (%s)", name, reason or "unknown")
    return {"success": False, "error": reason or "authentication did not complete"}
