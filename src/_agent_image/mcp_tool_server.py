#!/usr/bin/env python3
"""Standalone MCP Tool Server for Cubicle agent containers.

Runs as a child process of the Claude CLI (standard MCP pattern).
Communicates via JSON-RPC over stdin/stdout.

Usage (spawned by Claude CLI via --mcp-config):
    python3 /opt/cubicle/mcp_tool_server.py --role manager
    python3 /opt/cubicle/mcp_tool_server.py --role worker

Environment variables:
    BACKEND_URL  — Platform backend URL (e.g. http://host.docker.internal:8000)
    OFFICE_ID    — Office UUID
    TASK_ID      — Current task UUID (worker only, optional)
    AGENT_NAME   — Agent name (worker only, optional)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

# P3-F: tool-definition lists + parameter transforms live in the
# sibling ``_mcp`` package. Both are pure functions; the imports are
# cheap and stay at module-load time so the JSON-RPC startup
# round-trip isn't slowed.
#
# P3.5-B: guard the sys.path insert so a host-side test that loads
# this module via ``importlib.util.spec_from_file_location`` doesn't
# accumulate duplicate path entries (or worse, shadow a future
# top-level ``_mcp`` module on the global path). Idempotent.
_OWN_DIR = str(Path(__file__).parent)
if _OWN_DIR not in sys.path:
    sys.path.insert(0, _OWN_DIR)
from _mcp import (  # noqa: E402
    get_manager_tools as _get_manager_tools,
    get_worker_tools as _get_worker_tools,
    transform_params as _transform_params,
)

logger = logging.getLogger("mcp_tool_server")

# ── Configuration ──────────────────────────────────────────────────

BACKEND_URL = os.environ.get("BACKEND_URL", "http://host.docker.internal:8000")
TOOL_PROXY_URL = os.environ.get("TOOL_PROXY_URL", "")  # Local proxy on communicator host
# Bearer token for ``TOOL_PROXY_URL``. Plumbed via the agent_worker's
# CLI env. When unset (older cbcl that didn't mint the token), the
# proxy responds 401 and the caller falls back to the direct
# /api/offices/{oid}/tool-call path (only meaningful for the
# ``/tool-call`` endpoint; ``/script-execute-host`` has no fallback).
TOOL_PROXY_TOKEN = os.environ.get("TOOL_PROXY_TOKEN", "")
OFFICE_ID = os.environ.get("OFFICE_ID", "")
TASK_ID = os.environ.get("TASK_ID", "")
AGENT_NAME = os.environ.get("AGENT_NAME", "")
# Per-task output dir context, set by ``agent_worker._build_mcp_config``
# when the worker is assigned a task. Used to inject ``CUBICLE_OUTPUT_DIR``
# into script subprocesses spawned via the local ``execute_script`` MCP
# tool — keeps the in-container path consistent with the host-side
# ScriptRunner when an agent triggers execution.
WORKSTREAM_SHORT_CODE = os.environ.get("CUBICLE_WORKSTREAM_SHORT_CODE", "")
SCOPE_READABLE_ID = os.environ.get("CUBICLE_SCOPE_READABLE_ID", "")


# Tools only the Automation Script Developer may call. Stripped from
# every other worker's tool list at registration time so non-script-
# authoring agents physically cannot author scripts. register_script
# is idempotent (create OR update) so this single name covers both
# creation and edits. ``bind_script_variable`` shipped in 0.2.22 to
# let the ASD wire its own credentials — it's gated to the same agent
# because random workers shouldn't be moving wiring decisions on a
# script they don't own.
_SCRIPT_AUTHOR_ONLY = frozenset({
    "register_script",
    "clone_script",
    "install_script_from_template",
    "bind_script_variable",
})


def filter_script_author_tools(
    tools: list[dict], agent_name: str
) -> list[dict]:
    """Return ``tools`` minus script-authoring tools for non-author agents.

    Pure function so the subprocess wiring stays trivial and the
    filter is unit-testable without spawning the MCP server. The
    automation-script-developer keeps everything; every other agent
    (including a worker spawned with an empty AGENT_NAME, which is
    a spawn-time bug — see caller) loses the authoring tools.
    """
    if agent_name == "automation-script-developer":
        return tools
    return [t for t in tools if t.get("name") not in _SCRIPT_AUTHOR_ONLY]


def compute_output_dir(
    workstream_short_code: str,
    scope_readable_id: str,
) -> str:
    """Compute the per-task ``CUBICLE_OUTPUT_DIR`` path the in-container
    Runner injects into script subprocesses.

    Mirrors the host-side ``ScriptRunner._build_launch_command``
    logic so agent-triggered runs land in the same directory as
    UI-triggered runs against the same task. Pure function so the
    subprocess wiring stays trivial and the path computation is
    unit-testable without spawning a script.

    Path shape:
        * ``/workspace/outputs/{ws}/{scope}/`` when both are set
        * ``/workspace/outputs/{ws}/`` when only workstream is set
        * ``/workspace/outputs/`` (flat root) when neither is set —
          this is the legacy fallback used by manual UI triggers
          without a task context.

    Hardening (QA round 8):
      * ``.strip()`` both inputs to match the host runner exactly,
        so a whitespace-padded env var doesn't produce a different
        path on the in-container side.
      * Reject any segment containing ``..``, ``/``, or ``\\`` —
        defends against a future producer that bypasses the
        backend's deterministic readable_id format and tries to
        smuggle a path-traversal sequence through the env var.
        Falls back to the safe parent (workstream-only or root).
    """
    ws = (workstream_short_code or "").strip()
    scope = (scope_readable_id or "").strip()
    # Path-segment safety: a workstream short_code or scope
    # readable_id with traversal characters would let the script
    # write outside its assigned output directory. The backend's
    # generators produce safe values (``WR``, ``WR-003.S01``); this
    # guard is defence in depth for future producers.
    _UNSAFE_SEGMENT = {"/", "\\", ".."}
    if ws and any(s in ws for s in _UNSAFE_SEGMENT):
        return "/workspace/outputs"
    if not ws:
        return "/workspace/outputs"
    base = f"/workspace/outputs/{ws}"
    if scope and not any(s in scope for s in _UNSAFE_SEGMENT):
        return f"{base}/{scope}"
    return base
TASK_MODE = os.environ.get("TASK_MODE", "execute")  # "execute" | "review" | "triage" | "manager"
# Triage mode = MA dispatch on a still-blocked task. The MCP server
# refuses ``update_status`` / ``move_task`` on the CURRENT blocked
# task (matched by ``TASK_ID``, defined at module top). Tools acting
# on OTHER tasks — ``create_task`` for a helper, ``update_task`` to
# set ``depends_on`` — stay available so the MA can run its three
# legitimate resolution paths (B answer-and-stop / C helper-task /
# D propose_action) without being able to silently un-block the task
# the playbook tells it never to un-block.
# Context of the current Manager chat turn. "general_chat" when the user
# is chatting without a workstream; "workstream:{uuid}" when inside a
# workstream. Empty for non-Manager (worker) sessions. Controls whether
# board-mutating tools are exposed.
CONTEXT_KEY = os.environ.get("CONTEXT_KEY", "")

# Actions / bare tool names that mutate the board, scopes, or
# workspace. Blocked in General Chat mode. The guard at
# ``_execute_tool`` checks BOTH ``tool["action"]`` and the bare tool
# name against this set, so the set legitimately mixes "actions" and
# "names" that are not 1-to-1 (e.g. the ``archive_task`` tool dispatches
# to action ``move_task`` with a transform).
#
# Several entries are belt-and-suspenders for actions that ONLY exist
# on the worker side today. They are kept here so a future change that
# accidentally exposes one to the Manager (e.g. by promoting a worker
# tool into the manager_tools list) still gets blocked in General Chat
# rather than silently letting the Manager mutate while in
# "general_chat" context. If you add a worker-only mutation, ALSO add
# its action / bare name here — that's cheaper than a runtime audit.
_BOARD_WRITE_ACTIONS = {
    # Manager tool actions (from ``_get_manager_tools``).
    "create_task",
    "update_task",
    "move_task",
    "add_activity",
    "delete_task",
    "create_scope",
    "update_scope",
    "activate_scope",
    "archive_scope",
    "office_save_file",
    # Bare tool names — Manager tools whose ``action`` aliases a less
    # specific verb (the bare-name check still trips the guard).
    "archive_task",  # tool name; action is move_task + transform
    # Escape hatch for the blocked-bounce-cap deadlock. Manager / MA
    # only; the General-Chat guard still blocks it (Manager in chat
    # has no business unblocking a stuck task without context).
    "retry_blocked_task",
    # Action-request decisions are workstream state changes — blocked
    # in General Chat so the Manager doesn't accidentally approve a
    # request without the workstream-context that informs the call.
    "decide_action_request",
    # Worker-only actions/names (defense-in-depth — see header above).
    "office_attach_to_task",
    "register_script",
    "clone_script",
    "install_script_from_template",
    "task_status_update",
    # F21 (audit): ``kb_save`` removed — no such tool is registered on
    # either Manager or Worker. Was a dead defense-in-depth entry.
}


def _is_general_chat() -> bool:
    return CONTEXT_KEY == "general_chat"


def _GENERAL_CHAT_REDIRECT(attempted: str) -> str:
    return (
        f"Board-mutating tool '{attempted}' is DISABLED in General Chat. "
        "Task and Scope manipulation is only available inside a workstream. "
        "Tell the user: \"I can't create or modify tasks from General Chat. "
        "Please switch to the appropriate workstream from the sidebar and "
        "ask me there.\" Do not retry this tool."
    )

# ── HTTP client ────────────────────────────────────────────────────

_http_session = None


async def _get_session():
    global _http_session
    if _http_session is None or _http_session.closed:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=30, connect=5)
        connector = aiohttp.TCPConnector(limit=10, keepalive_timeout=30)
        _http_session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            headers={"Content-Type": "application/json"},
        )
    return _http_session


async def _close_session():
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()
    _http_session = None


async def _call_backend(action: str, params: dict) -> dict:
    """Call the backend tool-call endpoint with retry logic.

    If TOOL_PROXY_URL is set, routes through the local communicator proxy
    (which forwards via WebSocket). Falls back to direct backend HTTP if
    the proxy is unavailable.
    """
    import aiohttp

    # Always carry the caller's identity through to the backend so
    # the dispatcher can apply defense-in-depth role gates (the
    # in-container tool-list filter is the primary defense; this
    # is the backstop for ASD-only actions like
    # ``bind_script_variable`` / ``install_script_from_template``
    # so a misbehaving call path that bypasses the filter can't
    # rebind someone else's script). Sent as a top-level envelope
    # field so handlers can read it without changing every
    # tool's params schema.
    payload = {
        "action": action,
        "params": params,
        "_caller": {
            "agent_name": AGENT_NAME or "",
            "role": "worker" if AGENT_NAME else "manager",
        },
    }
    session = await _get_session()
    last_error = None

    # Try local proxy first (WS-routed, lower latency for remote setups)
    if TOOL_PROXY_URL:
        proxy_url = f"{TOOL_PROXY_URL}/tool-call"
        proxy_headers = (
            {"Authorization": f"Bearer {TOOL_PROXY_TOKEN}"}
            if TOOL_PROXY_TOKEN
            else None
        )
        try:
            async with session.post(
                proxy_url, json=payload, headers=proxy_headers,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                # Proxy returned an error — fall through to direct backend
                body = await resp.text()
                last_error = f"Proxy HTTP {resp.status}: {body[:300]}"
        except (aiohttp.ClientError, ConnectionError, asyncio.TimeoutError):
            last_error = "Tool proxy unreachable, falling back to direct backend"

    # Direct backend call (original path)
    url = f"{BACKEND_URL}/api/offices/{OFFICE_ID}/tool-call"
    for attempt in range(3):
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    return await resp.json()
                body = await resp.text()
                last_error = f"HTTP {resp.status}: {body[:500]}"
                if resp.status < 500:
                    break  # Don't retry client errors
        except (aiohttp.ClientError, ConnectionError, asyncio.TimeoutError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < 2:
                await asyncio.sleep(1.0 * (attempt + 1))

    return {"error": True, "message": f"Backend call failed: {last_error}"}


# ── Local script tools ─────────────────────────────────────────────

# Mirror of the Runner's reserved list so a manifest-declared
# variable can't shadow Runner-injected metadata. Parallel to
# ``src.scripts.manifest._RESERVED_VARIABLE_NAMES`` but inlined
# because this MCP server runs inside the agent container and
# doesn't ship the full communicator package.
_RESERVED_ENV_NAMES = frozenset({
    "PYTHONPATH",
    "CUBICLE_SCRIPT_DIR",
    "CUBICLE_SCRIPT_NAME",
    "CUBICLE_EXECUTION_ID",
    "CUBICLE_TASK_ID",
    "CUBICLE_OUTPUT_DIR",
})


def _stringify_env_value(value: Any) -> str:
    """Coerce a manifest / variables.json value to the string the
    child process sees in ``os.environ``. Mirrors
    ``script_runner._stringify_override_value``: bools → ``true``/
    ``false``, numbers via ``repr``, anything else via ``str``.
    Keeps the wire shape consistent with the host-side Runner path.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return str(value)


def _parse_manifest(script_dir: Path) -> dict:
    """Minimal manifest reader for the in-container MCP path.

    Pulls ``entry_point`` + the list of declared variables (with
    defaults, secret flags, and type info) out of ``script.yaml``.
    Strict validation lives in the host-side Runner
    (``src.scripts.manifest.load_manifest``); here we only need
    enough fields to build the launch env. Missing ``script.yaml``
    is a hard error — the agent must re-register to get the
    bootstrap to land.
    """
    manifest_path = script_dir / "script.yaml"
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"Missing {manifest_path}. Mini-projects need a "
            "script.yaml — call register_script to bootstrap one."
        )
    try:
        # Local import keeps the MCP process start-time lean. The
        # ImportError catch turns "ModuleNotFoundError: No module
        # named 'yaml'" — a confusing message for the agent that
        # makes it look like a script bug — into a clear "the
        # agent image is missing PyYAML" diagnostic that points at
        # the fix (rebuild the image).
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "Agent image is missing PyYAML — the in-container MCP "
            "server cannot parse script.yaml. This is an "
            "infrastructure issue, not a script bug. Fix: rebuild "
            "the cubicle-agent image (``cbcl stop && cbcl start`` "
            "after the latest cbcl release, which bumps PyYAML "
            "into the Dockerfile.agent pip install line). Do NOT "
            "modify the script — it's already correct.",
        ) from exc
    raw = yaml.safe_load(manifest_path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"script.yaml root must be a mapping, got {type(raw).__name__}",
        )
    return raw


def _entry_module(entry_point: str) -> str:
    """Translate ``entry_point`` into a ``python -m`` module spec.
    ``main.py`` → ``main``; ``lib/cli/run.py`` → ``lib.cli.run``.
    """
    trimmed = entry_point.rstrip("/").removesuffix(".py")
    return trimmed.replace("/", ".")


async def _ensure_deps_installed(script_dir: Path) -> str | None:
    """Install ``requirements.txt`` into a per-script ``.deps/``
    cache on first run (or when requirements.txt mtime is newer
    than the install stamp). Returns None on success or a
    human-readable error string on failure (caller surfaces to
    the agent).

    Cache semantics mirror the host-side Runner's
    ``src/scripts/deps_installer.py``: if the stamp file
    ``.deps/.installed_at`` has mtime >= ``requirements.txt``
    mtime, the install is skipped entirely. A missing or empty
    ``requirements.txt`` is also a no-op — the common case for
    stdlib-only scripts.

    Install uses ``pip install --target .deps/`` so the cache
    lives ON the bind-mounted workspace and survives container
    restarts. No lock file is taken because this MCP server is
    single-process per agent — concurrent installs of the same
    script from different callers aren't a concern here.
    """
    reqs = script_dir / "requirements.txt"
    if not reqs.is_file():
        return None
    try:
        reqs_contents = reqs.read_text().strip()
    except OSError as exc:
        return f"failed to read requirements.txt: {exc}"
    if not reqs_contents:
        return None

    deps_dir = script_dir / ".deps"
    stamp = deps_dir / ".installed_at"
    if stamp.is_file():
        try:
            if stamp.stat().st_mtime >= reqs.stat().st_mtime:
                return None  # Cache hit — nothing to do.
        except OSError:
            pass  # Fall through and reinstall.

    deps_dir.mkdir(parents=True, exist_ok=True)
    argv = [
        "python3", "-m", "pip", "install",
        "--quiet", "--disable-pip-version-check",
        "--no-input", "--no-cache-dir",
        "--target", str(deps_dir),
        "-r", str(reqs),
    ]
    logger.info(
        "Installing deps for %s via MCP path (%s)",
        script_dir.name, reqs,
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        # 10-minute ceiling — same as host-side Runner. A pip
        # install longer than that is almost certainly stuck.
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=600,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return (
                "pip install timed out after 10 minutes — check "
                "network or requirements.txt for slow-resolving "
                "dependencies"
            )
    except OSError as exc:
        return f"could not spawn pip: {exc}"
    if proc.returncode != 0:
        tail = (stdout or b"")[-4000:].decode(errors="replace")
        return (
            f"pip install failed (exit {proc.returncode}):\n{tail}"
        )
    try:
        stamp.touch()
    except OSError:
        pass  # Next run will just re-install — not fatal.
    return None


async def _execute_script(params: dict) -> dict:
    """Execute a mini-project script locally in the agent's container.

    Mirrors the host-side Runner (``src/scripts/script_runner.py``):
    parse ``script.yaml``, build the env dict from manifest defaults
    + ``variables.json`` + ``.secrets.json`` + per-call overrides,
    inject ``CUBICLE_*`` metadata, then launch
    ``python -m {entry_module}`` with that env. No Jinja
    substitution, no materialised ``_run.py`` — the entry module
    runs against the workspace as-is.

    Manager mode guard
    ------------------
    The Manager is a pure orchestrator — it must NEVER execute
    work, including scripts. Per-role tool filtering already keeps
    ``execute_script`` out of the Manager's tool list (see
    ``_get_manager_tools`` — script-execution names are absent),
    but we add a runtime guard here as defence in depth: even if
    a future change accidentally exposes the tool to a Manager
    session, the call returns a clear error rather than silently
    dumping output into the flat ``/workspace/outputs/`` root
    (the Manager has no workstream short_code env var, so
    ``CUBICLE_OUTPUT_DIR`` would resolve to the legacy fallback).
    """
    if TASK_MODE == "manager":
        return {
            "error": True,
            "message": (
                "execute_script is forbidden in Manager mode. The "
                "Manager is a pure orchestrator — script execution "
                "is a worker responsibility. Create a task and "
                "assign it to a worker (Manager Assistant for quick "
                "lookups, Automation Script Developer for new "
                "scripts) instead."
            ),
        }

    script_name = params.get("script_name", "")
    variable_overrides = params.get("variable_overrides") or {}

    script_dir = Path(f"/workspace/.scripts/{script_name}")
    if not script_dir.is_dir():
        return {"error": True, "message": (
            f"Script directory not found: {script_dir}. Call "
            "register_script first to bootstrap the mini-project."
        )}

    try:
        manifest = _parse_manifest(script_dir)
    except (FileNotFoundError, ValueError) as exc:
        return {"error": True, "message": str(exc)}

    # Install declared third-party deps into ``.deps/`` on first
    # run (mtime-cached). Without this the host-side Runner path
    # populates deps and the MCP path doesn't — a script whose
    # ``requirements.txt`` is non-empty ``ModuleNotFoundError``s
    # when an agent triggers execution via ``execute_script``.
    # Mirrors ``ensure_deps_installed`` semantics but inlined
    # because this MCP server doesn't ship the communicator package.
    try:
        install_err = await _ensure_deps_installed(script_dir)
    except Exception as exc:
        logger.exception("deps install raised for %s", script_name)
        return {"error": True, "message": f"deps install failed: {exc}"}
    if install_err:
        return {"error": True, "message": install_err}

    declared = manifest.get("variables") or []
    declared_by_name: dict[str, dict] = {}
    for var in declared:
        if isinstance(var, dict) and isinstance(var.get("name"), str):
            declared_by_name[var["name"]] = var

    # Office-secret references can ONLY be resolved by the host-side
    # Script Runner because the secrets file lives outside the
    # bind-mounted /workspace (otherwise any agent could read it via
    # the Read tool). Two sources of office-secret references exist:
    #
    #   1. Phase-1.5 BINDINGS in ``variables.json`` — the new path,
    #      set via the Variables UI.
    #   2. Legacy ``from_office_secret`` field in the manifest —
    #      still works for unmigrated scripts.
    #
    # If EITHER source has an office-secret reference, we must
    # delegate execution to the host-side runner via the tool
    # proxy's ``/script-execute-host`` endpoint. The runner reads
    # the office-secrets file on the host and injects the values
    # via ``docker exec -e KEY=VALUE`` at spawn time — values
    # never enter this container's filesystem.
    office_refs: list[str] = [
        var["from_office_secret"]
        for var in declared
        if (
            isinstance(var, dict)
            and isinstance(var.get("from_office_secret"), str)
        )
    ]
    # Bindings: parse the per-script variables.json from the bind-
    # mounted workspace (safe to read from inside the container —
    # it does NOT contain secret values, only literal non-secret
    # values and office-secret REF NAMES). Office-secret refs
    # discovered here join the manifest-side refs so the host-runner
    # delegation triggers for either source.
    try:
        bindings_path = script_dir / "variables.json"
        if bindings_path.is_file():
            import json as _json
            bindings_raw = _json.loads(bindings_path.read_text() or "{}")
            if isinstance(bindings_raw, dict):
                for raw in bindings_raw.values():
                    if (
                        isinstance(raw, dict)
                        and raw.get("kind") == "office_secret"
                        and isinstance(raw.get("ref"), str)
                    ):
                        office_refs.append(raw["ref"])
    except (OSError, ValueError) as exc:
        # Don't fail the whole call on a malformed variables.json —
        # the host runner will produce a clearer error if any of
        # those bindings actually mattered. Log so an operator
        # tailing cbcl can spot the corruption.
        logger.warning(
            "execute_script: failed to read variables.json for %s: %s",
            script_name, exc,
        )
    if office_refs:
        if not TOOL_PROXY_URL:
            # No proxy means we're talking to the backend directly,
            # which has no host-runner route. Surface the refusal
            # with a clear "rebuild cbcl" hint (older cbcl versions
            # before this change didn't expose the host endpoint).
            return {
                "error": True,
                "message": (
                    f"Script '{script_name}' references office "
                    "secret(s) "
                    f"({', '.join(sorted(set(office_refs)))}) but "
                    "this MCP session has no tool-proxy URL "
                    "configured — restart cbcl (``cbcl stop && "
                    "cbcl start``) so the host-side proxy is wired "
                    "and retry."
                ),
            }
        # Imported here (not at module top) so the standalone MCP
        # process doesn't pay aiohttp's import cost on every Claude
        # CLI session start — only sessions that actually delegate
        # via the proxy hit this path. The except clause below
        # references ``aiohttp.ClientError`` so this import must
        # succeed before the try/except runs.
        import aiohttp
        proxy_url = f"{TOOL_PROXY_URL}/script-execute-host"
        session = await _get_session()
        payload = {
            "script_name": script_name,
            "variable_overrides": variable_overrides,
            "task_id": TASK_ID or None,
            "triggered_by": AGENT_NAME or "agent",
            "workstream_short_code": (
                WORKSTREAM_SHORT_CODE or None
            ),
            "scope_readable_id": SCOPE_READABLE_ID or None,
        }
        proxy_headers = (
            {"Authorization": f"Bearer {TOOL_PROXY_TOKEN}"}
            if TOOL_PROXY_TOKEN
            else None
        )
        try:
            async with session.post(
                proxy_url, json=payload, headers=proxy_headers,
            ) as resp:
                body_text = await resp.text()
                try:
                    body = json.loads(body_text) if body_text else {}
                except json.JSONDecodeError:
                    body = {}
                if resp.status == 200 and "execution_id" in body:
                    return {
                        "execution_id": body["execution_id"],
                        "delegated_to": "host_runner",
                    }
                # The host-side runner reports its known failure
                # modes with typed ``error`` strings. Forward as
                # MCP errors with the message the agent will see.
                err_kind = body.get("error") if isinstance(body, dict) else None
                err_msg = body.get("message") if isinstance(body, dict) else body_text
                if err_kind == "missing_office_secret":
                    missing = body.get("missing") or []
                    return {
                        "error": True,
                        "message": (
                            f"Script '{script_name}' is parked on "
                            f"missing office secret(s): "
                            f"{', '.join(missing)}. The user must "
                            "add them in Settings → Security → "
                            "Office Secrets. The Script Runner has "
                            "already emitted a setup_office_secret "
                            "action_request to surface this in the "
                            "inbox — wait for the user to resolve "
                            "it before retrying."
                        ),
                    }
                if err_kind == "office_secrets_corrupt":
                    return {
                        "error": True,
                        "message": (
                            f"Office secrets file is corrupt: "
                            f"{body.get('detail') or 'unknown'}. "
                            "Ask the user to repair it in Settings "
                            "→ Security → Office Secrets before "
                            "retrying."
                        ),
                    }
                return {
                    "error": True,
                    "message": (
                        f"Host script execute failed (status "
                        f"{resp.status}): {err_msg or 'unknown'}"
                    ),
                }
        except (aiohttp.ClientError, ConnectionError, asyncio.TimeoutError) as exc:
            return {
                "error": True,
                "message": (
                    f"Could not reach the host-side script runner "
                    f"via the tool proxy: {type(exc).__name__}. "
                    "Is cbcl running?"
                ),
            }

    # 1) manifest defaults 2) variables.json (non-secret) 3) .secrets.json
    # 4) per-call overrides. Later layers win.
    env_values: dict[str, str] = {}
    for name, decl in declared_by_name.items():
        if "default" in decl and decl["default"] is not None:
            env_values[name] = _stringify_env_value(decl["default"])

    vars_file = script_dir / "variables.json"
    if vars_file.is_file():
        try:
            for k, v in (json.loads(vars_file.read_text()) or {}).items():
                if isinstance(k, str) and k in declared_by_name:
                    env_values[k] = _stringify_env_value(v)
        except (json.JSONDecodeError, OSError):
            pass

    secrets_file = script_dir / ".secrets.json"
    if secrets_file.is_file():
        try:
            for k, v in (json.loads(secrets_file.read_text()) or {}).items():
                if isinstance(k, str) and k in declared_by_name:
                    env_values[k] = _stringify_env_value(v)
        except (json.JSONDecodeError, OSError):
            pass

    for k, v in variable_overrides.items():
        if isinstance(k, str) and k in declared_by_name:
            env_values[k] = _stringify_env_value(v)
        # Undeclared overrides are silently dropped — same contract
        # as the host-side Runner.

    # Reserved keys can NEVER come from the manifest; if they
    # somehow leaked in, strip so the metadata below wins.
    for key in _RESERVED_ENV_NAMES:
        env_values.pop(key, None)

    # Execution dir + status.json (shape the backend backfill
    # reads on GET /scripts/{id}/executions).
    import uuid as _uuid
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    exec_id = f"exec-{timestamp}-{_uuid.uuid4().hex[:6]}"
    exec_dir = script_dir / "executions" / exec_id
    exec_dir.mkdir(parents=True, exist_ok=True)
    started_iso = datetime.now(timezone.utc).isoformat()
    (exec_dir / "status.json").write_text(json.dumps({
        "status": "running",
        "started_at": started_iso,
        "completed_at": None,
        "duration_seconds": None,
        "exit_code": None,
        "task_id": TASK_ID or None,
        "triggered_by": AGENT_NAME or "agent",
        "error_message": None,
    }))

    # Metadata env the script's cubicle SDK + any helper lib will
    # read. PYTHONPATH covers ``lib/`` (user modules),
    # ``.deps/`` (pip cache), and the script root (top-level
    # imports like ``from main import ...``).
    lib_dir = script_dir / "lib"
    deps_dir = script_dir / ".deps"
    env_values["CUBICLE_SCRIPT_DIR"] = str(script_dir)
    env_values["CUBICLE_SCRIPT_NAME"] = script_name
    env_values["CUBICLE_EXECUTION_ID"] = exec_id
    if TASK_ID:
        env_values["CUBICLE_TASK_ID"] = TASK_ID
    # Per-task output directory mirrors the host-side Runner. When
    # the worker has a workstream short_code (set by agent_worker on
    # assignment) the path narrows to /workspace/outputs/{ws}/[/{scope}/];
    # otherwise scripts fall back to the legacy flat root. The
    # subprocess pre-creates the dir so the script's first write
    # never races mkdir.
    output_dir = compute_output_dir(
        WORKSTREAM_SHORT_CODE, SCOPE_READABLE_ID,
    )
    try:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
    except OSError:
        # Directory creation is best-effort. The script's own first
        # write will surface a clearer error if the path is bad.
        pass
    env_values["CUBICLE_OUTPUT_DIR"] = output_dir
    env_values["PYTHONPATH"] = ":".join([
        str(lib_dir), str(deps_dir), str(script_dir),
    ])
    # Inherit the subset of the parent env pip + python need.
    base_env = {
        k: v for k, v in os.environ.items()
        if k in {"PATH", "HOME", "LANG", "TERM", "TMPDIR", "USER", "SHELL"}
    }
    base_env.update(env_values)

    entry_module = _entry_module(
        str(manifest.get("entry_point") or "main.py")
    )

    # Launch the entry module; subprocess inherits ``base_env``.
    log_file = exec_dir / "log.txt"
    log_f = None
    try:
        log_f = open(log_file, "w")
        proc = await asyncio.create_subprocess_exec(
            "python3", "-m", entry_module,
            stdout=log_f,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(script_dir),
            env=base_env,
        )
    except OSError as exc:
        # Spawn failed: close the log file we already opened
        # (otherwise the FD leaks until the MCP server exits) and
        # write a final ``failed`` status so the UI doesn't show
        # the execution stuck at ``running`` forever — mirrors the
        # host-side ScriptRunner cleanup at script_runner.py:493.
        if log_f is not None:
            try:
                log_f.close()
            except OSError:
                pass
        try:
            completed = datetime.now(timezone.utc).isoformat()
            (exec_dir / "status.json").write_text(json.dumps({
                "status": "failed",
                "started_at": started_iso,
                "completed_at": completed,
                "duration_seconds": 0,
                "exit_code": None,
                "task_id": TASK_ID or None,
                "triggered_by": AGENT_NAME or "agent",
                "error_message": f"spawn failed: {exc}",
            }))
        except OSError:
            pass
        return {"error": True, "message": f"Failed to spawn script: {exc}"}

    # Monitor → update status.json + clean up — fire-and-forget.
    # Pass ``log_f`` so the monitor can close it on completion;
    # without this the FD leaks for every script run.
    asyncio.create_task(_monitor_script(
        proc, exec_dir, None, script_name, exec_id, log_f,
    ))

    return {
        "execution_id": exec_id,
        "status": "running",
        "message": f"Script '{script_name}' started. Execution ID: {exec_id}",
    }


async def _monitor_script(
    proc: asyncio.subprocess.Process,
    exec_dir: Path,
    run_file: Path | None,
    script_name: str,
    exec_id: str,
    log_f=None,
):
    """Monitor a background script and update status on completion.

    Duration is computed from ``started_at`` (seeded in
    ``status.json`` at spawn time) so the backend's
    GET /scripts/{id}/executions disk-backfill can render it
    without a separate side-channel. The mini-project path never
    materialises a ``_run.py`` (``run_file=None`` is the common
    case) — the legacy parameter is kept only for the rollback
    pathway, still callable but effectively a noop.

    ``log_f`` is the open ``log.txt`` file handle that
    ``_execute_script`` passed to ``create_subprocess_exec`` as
    ``stdout``. We must close it after the process exits — leaving
    it open leaks a file descriptor for the lifetime of the MCP
    server, and a long-lived agent that runs many scripts will
    eventually hit ``EMFILE`` on the next ``open()``. Mirrors the
    host-side cleanup at ``script_runner.py:493``.
    """
    started_at = None
    try:
        await proc.wait()
        exit_code = proc.returncode
        status = "completed" if exit_code == 0 else "failed"

        status_file = exec_dir / "status.json"
        status_data = (
            json.loads(status_file.read_text())
            if status_file.exists() else {}
        )
        status_data["status"] = status
        now = datetime.now(timezone.utc)
        status_data["completed_at"] = now.isoformat()
        status_data["exit_code"] = exit_code
        # Best-effort duration: on a fresh status.json ``started_at``
        # was seeded in ``_execute_script``; parse it and subtract.
        started_raw = status_data.get("started_at")
        if isinstance(started_raw, str):
            try:
                started_at = datetime.fromisoformat(
                    started_raw.replace("Z", "+00:00"),
                )
            except ValueError:
                started_at = None
        if started_at:
            status_data["duration_seconds"] = max(
                0, int((now - started_at).total_seconds())
            )
        status_file.write_text(json.dumps(status_data))
    except asyncio.CancelledError:
        # P2-I: the MCP server is exiting (Claude session ended).
        # Without this, the script subprocess we launched leaks
        # inside the container — orphan ``python -m main``
        # processes accumulate across many runs and eventually
        # exhaust the container's process budget.
        #
        # P2.5-A: every awaitable inside this handler is wrapped in
        # ``asyncio.shield`` so a re-cancellation doesn't truncate
        # the cleanup mid-way. Re-cancellation is realistic during
        # daemon shutdown (parent task cancels twice). proc.wait()
        # after kill is also bounded so a wedged process can't
        # block shutdown indefinitely.
        #
        # P2.5-C: previous code wrote ``status="cancelled"`` to
        # status.json, but the backend only persists "completed" and
        # "failed" — so the row stayed "running" forever in the
        # DB / UI. We stamp ``status="failed"`` with a clear
        # error_message so the persisted row is correct and the
        # operator can distinguish a normal failure from a
        # session-cancellation.
        try:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.shield(
                        asyncio.wait_for(proc.wait(), timeout=5),
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    try:
                        await asyncio.shield(
                            asyncio.wait_for(proc.wait(), timeout=5),
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "script %s did not die within 10s of "
                            "terminate+kill; leaving to container "
                            "reaper", script_name,
                        )
                # Best-effort: atomic-write the failed-status row.
                try:
                    status_file = exec_dir / "status.json"
                    status_data = (
                        json.loads(status_file.read_text())
                        if status_file.exists() else {}
                    )
                    status_data["status"] = "failed"
                    status_data["completed_at"] = datetime.now(
                        timezone.utc,
                    ).isoformat()
                    status_data["exit_code"] = proc.returncode
                    status_data["error_message"] = (
                        "cancelled by session shutdown"
                    )
                    tmp = status_file.with_suffix(".tmp")
                    tmp.write_text(json.dumps(status_data))
                    tmp.replace(status_file)
                except Exception:
                    pass
        finally:
            raise
    except Exception as exc:
        logger.error("Error monitoring script %s: %s", script_name, exc)
    finally:
        # Close the stdout log file we opened in ``_execute_script``.
        # Without this the FD leaks for every script run.
        if log_f is not None:
            try:
                log_f.close()
            except OSError:
                pass
        # Legacy rollback hook — v2 mini-projects never create
        # ``_run.py``, so ``run_file`` is typically None. Guarded
        # so a future code path that does write one still cleans up.
        if run_file is not None and run_file.exists():
            run_file.unlink(missing_ok=True)


async def _get_script_status(params: dict) -> dict:
    """Check the status of a script execution."""
    script_name = params.get("script_name", "")
    execution_id = params.get("execution_id", "")

    exec_dir = Path(f"/workspace/.scripts/{script_name}/executions/{execution_id}")
    status_file = exec_dir / "status.json"
    progress_file = Path(f"/workspace/.scripts/{script_name}/.progress.json")

    if not status_file.exists():
        return {"error": True, "message": f"Execution not found: {execution_id}"}

    result = json.loads(status_file.read_text())

    # Add progress if available
    if progress_file.exists():
        try:
            result["progress"] = json.loads(progress_file.read_text())
        except json.JSONDecodeError:
            pass

    # Add last 20 lines of log
    log_file = exec_dir / "log.txt"
    if log_file.exists():
        lines = log_file.read_text().splitlines()
        result["log_tail"] = lines[-20:]

    return result



# ── MCP Protocol (JSON-RPC over stdio) ────────────────────────────

class MCPServer:
    """Minimal MCP server implementing the JSON-RPC protocol over stdio."""

    def __init__(self, tools: list[dict]):
        self._tools = {t["name"]: t for t in tools}
        # Session lock: set after a terminal action (update_status→review,
        # move_task→done/ready). ALL subsequent tool calls return an error.
        # This is the ONLY reliable way to stop Claude from continuing —
        # prompt instructions and kill signals have latency and can be ignored.
        self._session_locked = False
        self._lock_reason = ""

    async def run(self):
        """Main loop: read JSON-RPC requests from stdin, write responses to stdout.

        Claude CLI sends messages as NDJSON (one JSON object per line),
        NOT Content-Length framed. Uses thread-based stdin reading to avoid
        asyncio connect_read_pipe PermissionError in Docker containers.
        """
        loop = asyncio.get_running_loop()

        def _read_stdin_line() -> str:
            """Read one line from stdin (blocking, runs in thread)."""
            line = sys.stdin.buffer.readline()
            if not line:
                raise EOFError("stdin closed")
            return line.decode().strip()

        while True:
            try:
                line = await loop.run_in_executor(None, _read_stdin_line)

                if not line:
                    continue  # Skip empty lines

                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Non-JSON line from stdin: %s", line[:100])
                    continue

                # Dispatch
                response = await self._handle_message(msg)
                if response is not None:
                    self._write_response(response)

            except EOFError:
                break
            except Exception as exc:
                logger.exception("Error in MCP server loop: %s", exc)

        await _close_session()

    async def _handle_message(self, msg: dict) -> dict | None:
        """Handle a JSON-RPC message."""
        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params", {})

        if method == "initialize":
            return self._make_response(msg_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "cubicle-tools",
                    "version": "1.0.0",
                },
            })

        elif method == "notifications/initialized":
            return None  # No response for notifications

        elif method == "tools/list":
            tool_list = []
            for tool in self._tools.values():
                tool_list.append({
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "inputSchema": tool.get("inputSchema", {"type": "object", "properties": {}}),
                })
            return self._make_response(msg_id, {"tools": tool_list})

        elif method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            result = await self._execute_tool(tool_name, arguments)
            return self._make_response(msg_id, result)

        elif method == "ping":
            return self._make_response(msg_id, {})

        else:
            # Unknown method — return error
            if msg_id is not None:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": -32601,
                        "message": f"Unknown method: {method}",
                    },
                }
            return None

    async def _execute_tool(self, tool_name: str, arguments: dict) -> dict:
        """Execute a tool call and return MCP-formatted result."""
        # SESSION LOCK: reject ALL tool calls after terminal action.
        # Note: this is a secondary guard. The primary enforcement is in the
        # backend's action_add_activity. This lock works when tool calls
        # go through the local MCP server (not all calls do).
        if self._session_locked:
            return {
                "content": [{"type": "text", "text": (
                    f"SESSION TERMINATED: {self._lock_reason} "
                    "No further tool calls are allowed. STOP IMMEDIATELY."
                )}],
                "isError": True,
            }

        # GENERAL CHAT GUARD (Manager): board/scope-mutating tools are
        # forbidden in General Chat context. Task manipulation must
        # happen inside a workstream.
        if TASK_MODE == "manager" and _is_general_chat():
            tool = self._tools.get(tool_name)
            action = tool["action"] if tool else ""
            # Also check by tool_name suffix in case we didn't build this tool
            bare_name = tool_name.replace("mcp__cubicle-tools__", "")
            if action in _BOARD_WRITE_ACTIONS or bare_name in _BOARD_WRITE_ACTIONS:
                return {
                    "content": [{"type": "text", "text": _GENERAL_CHAT_REDIRECT(bare_name or tool_name)}],
                    "isError": True,
                }

        # TRIAGE GUARD: triage mode = MA dispatched to a still-blocked
        # task. Refuse any tool call that would un-block (or terminally
        # move) the CURRENT task. Tools targeting OTHER tasks are still
        # allowed so the MA can create a helper task, set depends_on on
        # the blocked task, or propose an action for the user.
        if TASK_MODE == "triage":
            bare_name = tool_name.replace("mcp__cubicle-tools__", "")
            tool_def = self._tools.get(tool_name)
            action_name = tool_def["action"] if tool_def else ""
            current_task = arguments.get("task_id", "")
            targets_current = bool(TASK_ID) and current_task == TASK_ID

            if bare_name in ("update_status",) or action_name == "task_status_update":
                return {
                    "content": [{"type": "text", "text": (
                        "update_status is disabled while triaging a blocked "
                        "task. Post a synthesis comment via add_activity, "
                        "then either (B) answer-and-stop, (C) create a "
                        "helper task and stamp depends_on, or (D) call "
                        "propose_action for the user — and STOP."
                    )}],
                    "isError": True,
                }
            if (bare_name in ("move_task", "archive_task")
                    or action_name == "move_task") and targets_current:
                return {
                    "content": [{"type": "text", "text": (
                        f"{bare_name} on the current blocked task is "
                        "disabled in triage mode — the cooldown lock + "
                        "bounce cap rely on this. Use propose_action or "
                        "leave the task in blocked for the user to resolve."
                    )}],
                    "isError": True,
                }

        # EXECUTOR GUARD: executors (TASK_MODE=execute) have restricted tools.
        if TASK_MODE == "execute":
            # Executors cannot call move_task (only reviewers/MA can)
            if tool_name in ("move_task", "mcp__cubicle-tools__move_task"):
                return {
                    "content": [{"type": "text", "text": "move_task is not available. Use update_status."}],
                    "isError": True,
                }
            # Executors cannot create tasks (only Manager can)
            if tool_name in ("create_task", "mcp__cubicle-tools__create_task"):
                return {
                    "content": [{"type": "text", "text": "create_task is not available. Use add_activity with event_type 'task_proposed' to suggest a new task to the Manager."}],
                    "isError": True,
                }
            # After session lock (submitted for review), block ALL MCP tools.
            # This catches add_activity calls that arrive after update_status.
            if self._session_locked:
                return {
                    "content": [{"type": "text", "text": (
                        f"SESSION TERMINATED: {self._lock_reason} "
                        "No further tool calls allowed."
                    )}],
                    "isError": True,
                }

        tool = self._tools.get(tool_name)
        if not tool:
            return {
                "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                "isError": True,
            }

        try:
            action = tool["action"]
            transform = tool.get("transform")
            is_local = tool.get("local", False)

            # Apply parameter transforms
            params = _transform_params(action, transform, arguments)

            # PRE-LOCK: Set session lock BEFORE the backend call for
            # terminal actions. This blocks same-turn tool calls —
            # Claude sometimes sends add_activity + update_status in
            # one turn, and the lock must be set before add_activity
            # can execute.
            is_terminal = False
            if action == "task_status_update":
                ns = params.get("new_status", "")
                if ns in ("review", "blocked"):
                    is_terminal = True
                    self._session_locked = True
                    self._lock_reason = f"Task submitted for {ns}."
                    logger.debug("PRE-LOCK SET: action=%s, new_status=%s", action, ns)
            elif action == "move_task":
                ns = params.get("new_status", "")
                if ns in ("done", "ready"):
                    is_terminal = True
                    self._session_locked = True
                    self._lock_reason = f"Task moved to {ns}."

            # Execute locally or via backend
            if is_local:
                if action == "script_execute":
                    result = await _execute_script(params)
                elif action == "script_get_status":
                    result = await _get_script_status(params)
                else:
                    result = {"error": True, "message": f"Unknown local action: {action}"}
            else:
                result = await _call_backend(action, params)

            # If terminal action failed, unlock (allow retry)
            if is_terminal and isinstance(result, dict) and result.get("error"):
                self._session_locked = False
                self._lock_reason = ""

            # Format response
            if isinstance(result, dict) and result.get("error"):
                return {
                    "content": [{"type": "text", "text": f"Error: {result.get('message') or result.get('error') or 'Unknown error'}"}],
                    "isError": True,
                }

            # For terminal actions, return a clean completion message
            if is_terminal:
                result = {
                    "status": "complete",
                    "message": f"Session complete. {self._lock_reason}",
                }

            # Truncate large responses to prevent buffer overflow
            text = json.dumps(result, indent=2, default=str)
            if len(text) > 50_000:
                text = text[:50_000] + "\n\n... (truncated, response too large)"

            return {
                "content": [{"type": "text", "text": text}],
            }

        except Exception as exc:
            logger.exception("Tool %s failed: %s", tool_name, exc)
            return {
                "content": [{"type": "text", "text": f"Tool error: {exc}"}],
                "isError": True,
            }

    def _make_response(self, msg_id: Any, result: dict) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": result,
        }

    def _write_response(self, response: dict):
        """Write a JSON-RPC response to stdout as NDJSON (one line)."""
        line = json.dumps(response) + "\n"
        sys.stdout.buffer.write(line.encode())
        sys.stdout.buffer.flush()


# ── Entry point ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Cubicle MCP Tool Server")
    parser.add_argument(
        "--role",
        choices=["manager", "worker"],
        required=True,
        help="Agent role determines available tools",
    )
    args = parser.parse_args()

    # Configure logging to stderr (stdout is for MCP protocol)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    if not OFFICE_ID:
        logger.error("OFFICE_ID environment variable is required")
        sys.exit(1)

    tools = _get_manager_tools() if args.role == "manager" else _get_worker_tools()

    # Workers: only the Automation Script Developer may author scripts.
    # Stripping the script-authoring tools (``register_script`` for
    # create/update, ``clone_script`` for marketplace-Phase-1
    # duplicate-and-adapt) at registration time means non-script-
    # authoring agents (research, copywriting, code review, etc.)
    # physically cannot author scripts — closing the routing gap that
    # produced orphan .py files when other custom agents tried to
    # "help" with automation. ``register_script`` is idempotent
    # (create OR update); ``clone_script`` is the duplicate path.
    if args.role == "worker":
        if not AGENT_NAME:
            logger.critical(
                "Worker MCP server started with empty AGENT_NAME — "
                "this is a spawn-time bug. Falling back to "
                "non-script-author behaviour (register_script + "
                "clone_script will be stripped). Investigate the "
                "orchestrator/agent spawn path."
            )
        before = len(tools)
        tools = filter_script_author_tools(tools, AGENT_NAME)
        removed = before - len(tools)
        if removed:
            logger.info(
                "Worker '%s' is not the Automation Script Developer: "
                "stripped %d script-authoring tool(s)",
                AGENT_NAME or "?", removed,
            )

    # General Chat mode: strip board-mutating tools so the Manager cannot
    # even attempt to create/modify tasks or scopes. This is the primary
    # defense; the _execute_tool guard is the secondary defense.
    if args.role == "manager" and _is_general_chat():
        filtered = [
            t for t in tools
            if t.get("action") not in _BOARD_WRITE_ACTIONS
        ]
        removed = len(tools) - len(filtered)
        tools = filtered
        logger.info(
            "General Chat mode: stripped %d write tools (kept %d read-only)",
            removed, len(tools),
        )

    logger.info(
        "Starting MCP tool server: role=%s, tools=%d, backend=%s, office=%s, context=%s",
        args.role, len(tools), BACKEND_URL, OFFICE_ID[:8], CONTEXT_KEY or "-",
    )

    server = MCPServer(tools)
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
