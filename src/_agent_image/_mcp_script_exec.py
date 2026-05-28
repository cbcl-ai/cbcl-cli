"""Local script-execution path for the in-container MCP tool server.

Extracted from ``mcp_tool_server.py`` so that file can stay focused on
JSON-RPC dispatch + role/lock guards. Owns:

* ``_execute_script`` — the long ``execute_script`` MCP tool handler
  (manifest parse → env build → docker-internal subprocess spawn, or
  delegate to the host-side runner via the tool proxy when an
  office-secret reference is in scope).
* ``_monitor_script`` — fire-and-forget watcher that updates
  ``status.json`` on completion and reaps the subprocess.
* ``_get_script_status`` — the ``script_get_status`` MCP tool handler.
* Supporting pure helpers (``_stringify_env_value``,
  ``_parse_manifest``, ``_entry_module``, ``_ensure_deps_installed``,
  ``compute_output_dir``).

Reads env-var config at import time (same shape as the parent). The
HTTP session helper is imported from ``_mcp_backend`` so the singleton
stays singular across both modules.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _mcp_backend import _get_session

logger = logging.getLogger("mcp_tool_server")

# ── Configuration ──────────────────────────────────────────────────
# Mirror the parent's env reads. All four sibling modules import once
# per process and the env is fixed at process start, so values match.

TOOL_PROXY_URL = os.environ.get("TOOL_PROXY_URL", "")
TOOL_PROXY_TOKEN = os.environ.get("TOOL_PROXY_TOKEN", "")
TASK_ID = os.environ.get("TASK_ID", "")
AGENT_NAME = os.environ.get("AGENT_NAME", "")
TASK_MODE = os.environ.get("TASK_MODE", "execute")
WORKSTREAM_SHORT_CODE = os.environ.get("CUBICLE_WORKSTREAM_SHORT_CODE", "")
SCOPE_READABLE_ID = os.environ.get("CUBICLE_SCOPE_READABLE_ID", "")


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
    _UNSAFE_SEGMENT = {"/", "\\", ".."}
    if ws and any(s in ws for s in _UNSAFE_SEGMENT):
        return "/workspace/outputs"
    if not ws:
        return "/workspace/outputs"
    base = f"/workspace/outputs/{ws}"
    if scope and not any(s in scope for s in _UNSAFE_SEGMENT):
        return f"{base}/{scope}"
    return base


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


async def _report_status_to_backend(
    *,
    script_name: str,
    exec_id: str,
    status: str,
    task_id: str | None,
    triggered_by: str,
    started_at_iso: str,
    completed_at_iso: str | None = None,
    duration_seconds: int | None = None,
    error_message: str | None = None,
) -> None:
    """Persist a script execution row by calling the backend's
    ``record_script_execution`` tool-call action.

    Goes through ``_call_backend`` which tries the local tool proxy
    first (low-latency hop on healthy split-host setups) and falls
    back to direct backend HTTP with 3 retries when the proxy is
    unreachable. Replaced the bare ``aiohttp.post`` to the proxy
    ``/script-status`` endpoint in cbcl 0.2.49 — that path silently
    dropped every report when ``TOOL_PROXY_URL`` was unset OR the
    daemon's tool proxy was unreachable from the agent container
    (UFW, daemon restart, network blip), which left the Execution
    History tab empty for AI-test runs.

    The backend handler (``_handle_record_script_execution``) wraps
    the same ``handle_script_status`` the WS event consumer uses, so
    the row is broadcast to the board AND written to the DB the same
    way a host-runner execution is.
    """
    payload = {
        "script_name": script_name,
        "execution_id": exec_id,
        "status": status,
        "task_id": task_id,
        "triggered_by": triggered_by,
        "started_at": started_at_iso,
        "completed_at": completed_at_iso,
        "duration_seconds": duration_seconds,
        "error_message": error_message,
        # The in-container path doesn't carry cron context. Cron
        # executions go through the host-side ScriptRunner which
        # has its own notifier.
        "cron_id": None,
        # Progress isn't tracked through this path; the backend
        # handler tolerates missing fields.
        "progress": None,
    }
    from _mcp_backend import _call_backend

    try:
        result = await _call_backend("record_script_execution", payload)
        if isinstance(result, dict) and result.get("error"):
            logger.warning(
                "record_script_execution rejected for %s/%s: %s",
                script_name, exec_id, result.get("error"),
            )
    except Exception as exc:  # noqa: BLE001
        # ``_call_backend`` already retried 3x against direct backend
        # AFTER the proxy attempt. If we land here, the backend is
        # genuinely unreachable. Log loud so ops can spot it; the
        # status.json on disk is the source of truth for a future
        # ``cbcl backfill`` recovery.
        logger.warning(
            "record_script_execution failed for %s/%s after retries: %s",
            script_name, exec_id, exc,
        )


async def _trigger_outbox_scan(*, script_name: str) -> None:
    """Ask the backend to tell the daemon to scan a script's outbox.

    The Manager subprocess lives on the daemon host (not the
    backend), so notify-manager delivery requires the daemon's
    ``ScriptRunner.scan_outbox_for(name)`` to fire. The backend's
    ``request_outbox_scan`` action forwards a ``scan_outbox`` command
    over the existing connector WS to the right office's daemon —
    same channel the backend already uses for ``task_ready``,
    ``script_execute``, etc.

    Calls go through ``_call_backend`` for the proxy → direct-backend
    fallback + 3 retries. Replaced the bare ``aiohttp.post`` to the
    proxy's ``/outbox-scan`` endpoint in cbcl 0.2.49 — that path
    silently dropped every call when ``TOOL_PROXY_URL`` was unset OR
    the proxy was unreachable, leaving ``notify_manager()`` drops in
    ``.outbox/`` forever.
    """
    from _mcp_backend import _call_backend

    try:
        result = await _call_backend(
            "request_outbox_scan", {"script_name": script_name},
        )
        if isinstance(result, dict) and result.get("error"):
            logger.warning(
                "request_outbox_scan rejected for %s: %s",
                script_name, result.get("error"),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "request_outbox_scan failed for %s after retries: %s",
            script_name, exc,
        )


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
        # Retry transient connect / timeout failures before declaring
        # the proxy down. Three attempts with exponential backoff covers
        # the common "I'm reachable in a sec, just busy" case AND lets
        # the daemon's supervisor restart a crashed proxy without the
        # agent immediately escalating. The exact failure modes we
        # retry: TimeoutError (network slow / proxy event-loop wedged),
        # ConnectionError (proxy bouncing), ClientConnectorError
        # (DNS / route flake). 4xx/5xx HTTP responses are NOT retried
        # — they're business-logic failures the agent must surface.
        last_exc: Exception | None = None
        for attempt in range(3):
            if attempt > 0:
                await asyncio.sleep(2 ** attempt)  # 2s, 4s
            try:
                async with session.post(
                    proxy_url, json=payload, headers=proxy_headers,
                    timeout=aiohttp.ClientTimeout(total=15),
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
            except (aiohttp.ClientError, ConnectionError,
                    asyncio.TimeoutError) as exc:
                last_exc = exc
                continue
        return {
            "error": True,
            "message": (
                f"Could not reach the host-side script runner via "
                f"the tool proxy after 3 attempts "
                f"({type(last_exc).__name__ if last_exc else 'unknown'}). "
                "Most common cause on Linux: UFW's default-deny "
                "policy is blocking the docker bridge. Operator fix: "
                "``sudo ufw allow in on docker0 && sudo ufw reload``. "
                "Verify with ``docker exec <office-container> curl -sm 3 "
                "http://host.docker.internal:<proxy-port>/health``. "
                "Don't escalate as ``external_outage`` until the "
                "operator has confirmed the firewall rule is in place."
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

    # Emit a "running" record_script_execution event so the backend
    # creates the row IMMEDIATELY — the Execution History tab shows
    # the row in real time instead of waiting for the script to
    # finish. The terminal event from ``_monitor_script`` upserts the
    # same row on completion. Fire-and-forget — failure to report
    # the running state isn't fatal; the completion event still lands.
    asyncio.create_task(_report_status_to_backend(
        script_name=script_name,
        exec_id=exec_id,
        status="running",
        task_id=TASK_ID or None,
        triggered_by=AGENT_NAME or "agent",
        started_at_iso=started_iso,
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
        duration_seconds: int | None = None
        if started_at:
            duration_seconds = max(
                0, int((now - started_at).total_seconds())
            )
            status_data["duration_seconds"] = duration_seconds
        status_file.write_text(json.dumps(status_data))

        # Forward to the backend via the host-side tool proxy so
        # the ScriptExecution DB row gets written and the
        # Execution History panel can show this run. In split-host
        # production the backend has no filesystem access to the
        # daemon's workspace; the disk-scan fallback in
        # ``list_executions`` is a no-op there, so without this
        # POST the row would only ever exist on the daemon host
        # — invisible to the UI. The host-side ScriptRunner does
        # the equivalent publish via WS directly; the in-container
        # MCP path can't reach the WS, so it forwards through the
        # proxy's ``/script-status`` endpoint instead.
        await _report_status_to_backend(
            script_name=script_name,
            exec_id=exec_id,
            status=status,
            task_id=status_data.get("task_id"),
            triggered_by=status_data.get("triggered_by") or AGENT_NAME or "agent",
            started_at_iso=started_raw or "",
            completed_at_iso=status_data["completed_at"],
            duration_seconds=duration_seconds,
            error_message=status_data.get("error_message"),
        )

        # Trigger an outbox scan so any ``cubicle.notify_manager()``
        # drops the script left in ``.outbox/`` get dispatched to the
        # Manager. Without this nudge, agent-triggered in-container
        # runs leave notify payloads on disk forever — the host-side
        # monitor loop only scans outboxes for scripts spawned via
        # the host runner. Same proxy-based pattern as the status
        # report above; best-effort.
        await _trigger_outbox_scan(script_name=script_name)
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
