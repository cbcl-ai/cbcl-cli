"""Script Runner — executes Python scripts inside the office container.

The runner dispatches scripts via ``docker exec`` into the long-lived
office container (one container per office, shared with the agents).
This matches ``docs/architecture.md`` — scripts run inside the agent
image, not on the host — and gives them the same isolation and
Python runtime agents get.

If constructed without a ``container_name``, the runner falls back to
a host-side ``python`` subprocess with a tightly filtered env. This
is only used by the unit test suite (no Docker available in-process).
Production always constructs the runner with a container name.

Either way the subprocess is a host-side ``asyncio.subprocess.Process``
so monitor/kill/cleanup paths work uniformly.

Every script is a mini-project: ``script.yaml`` manifest +
``main.py`` entry point + optional ``lib/`` modules + optional
``requirements.txt``. Variables declared in the manifest are
injected as env vars; the script reads them via ``os.environ``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from src._chown import chown_to_agent
from src.scripts.deps_installer import DepsInstallError, ensure_deps_installed
from src.scripts.manifest import (
    _RESERVED_VARIABLE_NAMES,
    ManifestError,
    ScriptManifest,
    load_manifest,
)
from src.scripts.script_notifier import (
    cleanup_orphaned_run_files,
    find_status_on_disk,
    read_progress,
    write_status,
)
from src.office_secrets.store import (
    CorruptOfficeSecretsError,
    read_office_secrets,
)
from src.scripts.secrets_store import SecretsStore
from src.scripts.variable_manager import VariableManager
from src.utils import validate_name


class MissingOfficeSecretError(Exception):
    """Raised by :meth:`ScriptRunner._execute_v2` before launch when
    the manifest references office secrets that don't exist in the
    office's store. Carries ``missing`` — the list of secret names
    the user needs to add via Settings → Security — so the caller
    can build a ``setup_office_secret`` action_request payload."""

    def __init__(
        self,
        missing: list[str],
        *,
        script_name: str,
    ) -> None:
        super().__init__(
            f"script {script_name!r} references office secrets that "
            f"are not configured: {', '.join(sorted(missing))}",
        )
        self.missing = sorted(set(missing))
        self.script_name = script_name


# Back-compat alias — older import sites use the script-runner name.
# The actual class lives in ``office_secrets.store`` where the
# corruption detection happens; aliasing keeps script-runner callers
# (``tool_proxy_server.py``) from needing to learn the new import path.
OfficeSecretsCorruptError = CorruptOfficeSecretsError

if TYPE_CHECKING:
    from src.connection.ws_client import PlatformWSClient

logger = logging.getLogger(__name__)

# Strong-reference holder for fire-and-forget spawn-time
# script_status:running publishes. Self-cleans via add_done_callback
# so the set doesn't grow over time.
_RUNNING_PUBLISH_TASKS: set[asyncio.Task] = set()

# Path the host workspace is bind-mounted at inside the office
# container. Every office container uses the same convention (see
# docker/container_manager.py and session_bridge.py).
_CONTAINER_WORKSPACE = "/workspace"

# Host env vars the host-fallback subprocess is allowed to see. Kept
# tight to mirror the in-container isolation: scripts should NOT be
# able to read the operator's AWS creds, SSH keys, etc. even when
# running in the fallback path.
_HOST_ALLOWED_ENV_VARS = frozenset(
    {"PATH", "HOME", "LANG", "TERM", "TMPDIR", "USER", "SHELL"}
)


def _stringify_override_value(value: object) -> str:
    """Convert a per-execution override value into the string form
    the child process will see in ``os.environ``. Mirrors
    :func:`src.scripts.manifest._stringify_env_value` but kept
    here because the override dict bypasses the manifest schema."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return str(value)


# Segments that, if present in a workstream short_code or scope
# readable_id, would let a script write outside its assigned output
# directory. Backend generators produce safe values (``WR``,
# ``WR-003.S01``); this guard is defence in depth for a future
# producer that bypasses the deterministic format.
_UNSAFE_OUTPUT_SEGMENT = ("/", "\\", "..")


def _compute_host_output_dir(
    outputs_root: Path,
    workstream_short_code: str | None,
    scope_readable_id: str | None,
) -> Path:
    """Compute the host-side per-task output directory.

    Returns the legacy flat ``outputs/`` root when no workstream is
    set, the per-workstream subdirectory when only ``ws`` is set,
    and the per-scope subdirectory when both are set. Whitespace is
    stripped and unsafe segments collapse to the safe parent so a
    malformed env var can't escape the workspace.

    MUST match
    ``communicator/docker/mcp_tool_server.py::compute_output_dir``
    behaviour for the same inputs (modulo the workspace prefix); the
    in-container helper returns string paths under ``/workspace``,
    this one returns ``Path`` objects under the host bind-mount root.
    The cross-check test in
    ``tests/test_mcp_tool_filter.py::test_output_dir_matches_host_runner_for_same_inputs``
    enforces the parity.
    """
    ws = (workstream_short_code or "").strip()
    scope = (scope_readable_id or "").strip()
    if not ws or any(seg in ws for seg in _UNSAFE_OUTPUT_SEGMENT):
        return outputs_root
    base = outputs_root / ws
    if scope and not any(seg in scope for seg in _UNSAFE_OUTPUT_SEGMENT):
        return base / scope
    return base


@dataclass
class _Execution:
    """Tracks a single running script execution."""
    exec_id: str
    script_name: str
    task_id: str | None
    triggered_by: str
    process: asyncio.subprocess.Process
    exec_dir: Path
    log_handle: object  # open file handle for stdout capture
    started_at: datetime
    last_progress: dict = field(default_factory=dict)
    cron_id: str | None = None


class ScriptRunner:
    """Manages background script execution."""

    # Default maximum script duration: 4 hours.
    DEFAULT_MAX_DURATION_SECONDS: int = 4 * 60 * 60

    def __init__(
        self,
        workspace_path: str,
        secrets_store: SecretsStore,
        variable_manager: VariableManager,
        ws_client: PlatformWSClient | None = None,
        router: object | None = None,
        max_duration_seconds: int | None = None,
        container_name: str | None = None,
        office_id: str = "",
        office_name: str = "",
        config_store: object | None = None,
        manager: object | None = None,
    ) -> None:
        self._workspace = Path(workspace_path)
        self._secrets = secrets_store
        self._variables = variable_manager
        self._ws = ws_client
        self._router = router
        self._container_name = container_name
        self._office_id = office_id
        # Office name is the on-disk slug source for
        # ``read_office_secrets`` — looked up at execute time so the
        # runner resolves ``from_office_secret`` references against
        # the live host secrets file. Defaults to the empty string in
        # unit tests; the resolver short-circuits when no references
        # are declared, so empty office_name only matters when the
        # script actually uses an office secret.
        self._office_name = office_name
        # Wired by the daemon at construction time so the outbox
        # watcher can resolve workstream names + route through the
        # Manager. None in unit tests — watcher is a no-op then.
        self._config_store = config_store
        self._manager = manager
        self._active: dict[str, _Execution] = {}
        # Parallel index: task_id → set[exec_id]. Keeps
        # :meth:`has_active_scripts` O(1). Maintained alongside
        # ``_active`` in :meth:`_track_execution` +
        # :meth:`_untrack_execution`. Only populated for
        # task-linked executions — manual-trigger runs (task_id=None)
        # never land here.
        self._active_by_task: dict[str, set[str]] = {}
        self._monitor_task: asyncio.Task | None = None
        # P5-V (review): cron scheduler is attached by the daemon
        # after construction (`set_cron_scheduler` / direct assign).
        # Initialise to None here so `shutdown()` and any future
        # lifecycle code can read `self._cron_scheduler` without the
        # AttributeError-prone `getattr(..., None)` dance.
        self._cron_scheduler: object | None = None
        self._max_duration = (
            max_duration_seconds
            if max_duration_seconds is not None
            else self.DEFAULT_MAX_DURATION_SECONDS
        )

        if not self._workspace.exists():
            logger.warning(
                "Workspace path does not exist: %s — scripts will fail "
                "until this directory is created",
                self._workspace,
            )
        elif not self._workspace.is_dir():
            logger.warning(
                "Workspace path is not a directory: %s", self._workspace,
            )
        if not self._container_name:
            logger.warning(
                "ScriptRunner constructed without a container_name; "
                "falling back to host-side Python execution. This is "
                "expected in unit tests; in production check that the "
                "daemon is passing the office's container name.",
            )

    def set_manager(self, manager: object) -> None:
        """Plumb the Manager reference after construction.

        The daemon builds the ``ScriptRunner`` first so it can pass
        ``config_store`` through; the ``ManagerController`` is
        built a few lines later and wires itself in here. A
        dedicated setter (rather than reaching into ``_manager``
        from outside the class) documents the contract and lets
        us log the transition for ops visibility.
        """
        if self._manager is not None:
            logger.warning(
                "ScriptRunner.set_manager: overwriting existing manager "
                "reference — this is unexpected; check the daemon init path",
            )
        self._manager = manager

    # ----------------------------------------------------------------- #
    # Subprocess command construction
    # ----------------------------------------------------------------- #

    def _use_docker(self) -> bool:
        """Whether to dispatch via ``docker exec`` into the office
        container. True when the runner has a ``container_name``;
        False for the host-side test fallback. Container mode is
        unconditional in production.
        """
        return bool(self._container_name)

    def _to_container_path(self, host_path: Path) -> str:
        """Translate a host-side workspace path to its in-container
        equivalent. Both sides share ``/workspace`` as the bind-mount
        root, so the tail of the path is identical."""
        rel = host_path.relative_to(self._workspace)
        # PosixPath.as_posix keeps forward slashes even on Windows hosts
        # — the container is Linux regardless.
        return f"{_CONTAINER_WORKSPACE}/{rel.as_posix()}"

    async def execute(
        self,
        script_name: str,
        variable_overrides: dict | None = None,
        task_id: str | None = None,
        triggered_by: str = "system",
        cron_id: str | None = None,
        workstream_short_code: str | None = None,
        scope_readable_id: str | None = None,
    ) -> str:
        """Start a script in the background. Returns execution_id.

        Every script is a mini-project: ``script.yaml`` manifest
        + ``main.py`` entry point + optional ``lib/`` modules +
        optional ``requirements.txt``. Missing manifest → error
        (callers should re-bootstrap the mini-project via the
        backend's create flow).

        ``cron_id`` flows through to the execution record so the
        UI can link a run back to the schedule that fired it.
        None for manual / task-triggered runs.

        ``workstream_short_code`` + ``scope_readable_id`` parameterise
        the per-task ``CUBICLE_OUTPUT_DIR`` env var injected into the
        script process. When both are absent the script falls back
        to the legacy flat ``/workspace/outputs/`` root — agents
        triggering scripts always have a workstream context, but
        manual UI triggers without a task land here too.
        """
        validate_name(script_name)
        script_dir = self._workspace / ".scripts" / script_name
        return await self._execute_v2(
            script_dir=script_dir,
            script_name=script_name,
            variable_overrides=variable_overrides,
            task_id=task_id,
            triggered_by=triggered_by,
            cron_id=cron_id,
            workstream_short_code=workstream_short_code,
            scope_readable_id=scope_readable_id,
        )

    # ----------------------------------------------------------------- #
    # Mini-project execution path
    # ----------------------------------------------------------------- #

    def _build_launch_command(
        self,
        *,
        script_dir: Path,
        manifest: ScriptManifest,
        script_name: str,
        exec_id: str,
        task_id: str | None,
        manifest_env: dict[str, str],
        workstream_short_code: str | None = None,
        scope_readable_id: str | None = None,
    ) -> tuple[list[str], dict[str, str] | None]:
        """v2 equivalent of :meth:`_build_launch_command`.

        Runs ``python -m {entry_module}`` with PYTHONPATH pointing at:

            ``{script_dir}``           — so sibling files import cleanly
            ``{script_dir}/lib``       — the conventional project root
            ``{script_dir}/.deps``     — the pip --target cache

        All three paths translate to their container-side form when
        docker mode is on, so the injected PYTHONPATH matches the
        container's view of the filesystem.

        Metadata env vars (CUBICLE_SCRIPT_DIR etc.) are injected as
        env flags on docker exec (or process env on the host
        fallback) so the script can find its workspace directory,
        execution id, and optional task id at runtime.
        ``manifest_env`` carries the declared variable values (which
        the script reads via ``os.environ``).
        """
        host_script_dir = script_dir
        host_lib_dir = script_dir / "lib"
        host_deps_dir = script_dir / ".deps"

        meta_env = {
            "CUBICLE_SCRIPT_NAME": script_name,
            "CUBICLE_EXECUTION_ID": exec_id,
        }
        if task_id:
            meta_env["CUBICLE_TASK_ID"] = task_id
        # Workstream context — the SDK's ``cubicle.notify_manager``
        # uses these to auto-route the callback to the task's chat
        # without forcing scriptmakers to thread the value through
        # their own code. The outbox watcher's ``_resolve_context_key``
        # accepts short_code (matched against
        # ``ws.short_code`` in the synced config).
        if workstream_short_code:
            meta_env["CUBICLE_WORKSTREAM_SHORT_CODE"] = workstream_short_code
        if scope_readable_id:
            meta_env["CUBICLE_SCOPE_READABLE_ID"] = scope_readable_id

        # Per-workstream output directory. Mirrors the worker prompt
        # convention from QA #3: output_dir = /workspace/outputs/{ws}/[{scope}/]
        # when both fields are present, /workspace/outputs/{ws}/ when
        # only ws is set, and the legacy flat /workspace/outputs/ when
        # neither is provided (manual UI triggers without a task).
        # The script reads this via ``cubicle.output_dir()`` from the
        # SDK helper; legacy scripts that hardcode /workspace/outputs/
        # keep working since the parent directory still exists.
        #
        # MUST stay in lockstep with
        # ``communicator/docker/mcp_tool_server.py::compute_output_dir``
        # (which the in-container MCP server uses for agent-triggered
        # runs). The cross-check test in
        # ``communicator/tests/test_mcp_tool_filter.py``
        # (``test_output_dir_matches_host_runner_for_same_inputs``)
        # locks the parity. Both implementations apply the same
        # ``.strip()`` and unsafe-segment guard so an agent-triggered
        # run and a UI-triggered run land in the same directory and
        # neither can escape the workspace via ``..`` / ``/`` / ``\``.
        host_outputs_root = self._workspace / "outputs"
        host_output_dir = _compute_host_output_dir(
            host_outputs_root,
            workstream_short_code,
            scope_readable_id,
        )
        # Pre-create on the host (the docker mount surfaces the same
        # directory inside the container) so the script's first write
        # never races mkdir. Chown each new chain segment so the
        # in-container script subprocess (uid 1000) can write into
        # the per-scope output dir — without this the chain
        # /workspace/outputs/{ws}/{scope}/ ends up root-owned and
        # every script write returns EACCES (the symptom that
        # triggered the v0.2.21 chown sweep).
        from src.fs_handler import _collect_new_parents
        new_parents = _collect_new_parents(host_output_dir, self._workspace)
        host_output_dir.mkdir(parents=True, exist_ok=True)
        for parent in new_parents:
            chown_to_agent(parent)
        chown_to_agent(host_output_dir)

        if self._use_docker():
            cont_script_dir = self._to_container_path(host_script_dir)
            cont_lib_dir = self._to_container_path(host_lib_dir)
            cont_deps_dir = self._to_container_path(host_deps_dir)
            meta_env["CUBICLE_SCRIPT_DIR"] = cont_script_dir
            meta_env["CUBICLE_OUTPUT_DIR"] = self._to_container_path(
                host_output_dir,
            )
            pythonpath = ":".join([cont_lib_dir, cont_deps_dir, cont_script_dir])
            meta_env["PYTHONPATH"] = pythonpath

            # Merge order: manifest first, metadata LAST. If a
            # manifest somehow declared a reserved key (the manifest
            # validator rejects that at parse time, but defence in
            # depth protects against a future code path that
            # bypasses the validator), the Runner-owned value wins.
            merged = {**manifest_env, **meta_env}
            # Explicit reassert: guarantees Runner keys can never
            # be shadowed regardless of dict-merge order. Matches
            # the host fallback below.
            for key in _RESERVED_VARIABLE_NAMES:
                if key in meta_env:
                    merged[key] = meta_env[key]

            argv: list[str] = ["docker", "exec"]
            # Metadata env + manifest-declared variables, all
            # through -e so secrets never appear on the command line
            # seen by `ps`.
            for key, value in merged.items():
                argv.extend(["-e", f"{key}={value}"])
            argv.extend([
                "-w", cont_script_dir,
                self._container_name,
                "stdbuf", "-oL",
                "python", "-m", manifest.entry_module,
            ])
            return argv, None

        # Host fallback (tests only — no container_name configured).
        # ``sys.executable`` works on Ubuntu 24.04+ where ``python``
        # isn't on PATH; in-container path above keeps ``"python"``
        # because the agent image guarantees ``python3.12``.
        meta_env["CUBICLE_SCRIPT_DIR"] = str(host_script_dir)
        meta_env["CUBICLE_OUTPUT_DIR"] = str(host_output_dir)
        pythonpath = ":".join(
            [str(host_lib_dir), str(host_deps_dir), str(host_script_dir)]
        )
        meta_env["PYTHONPATH"] = pythonpath
        safe_env = {
            k: v for k, v in os.environ.items()
            if k in _HOST_ALLOWED_ENV_VARS
        }
        # Manifest-declared vars first, Runner-owned metadata LAST
        # so reserved keys can't be shadowed.
        safe_env.update(manifest_env)
        safe_env.update(meta_env)
        return (
            [sys.executable, "-m", manifest.entry_module],
            safe_env,
        )

    async def _execute_v2(
        self,
        *,
        script_dir: Path,
        script_name: str,
        variable_overrides: dict | None,
        task_id: str | None,
        triggered_by: str,
        cron_id: str | None = None,
        workstream_short_code: str | None = None,
        scope_readable_id: str | None = None,
    ) -> str:
        """Run a mini-project. Same outer contract as :meth:`execute`
        (returns ``exec_id``, task tracked in ``self._active``).
        Manifest-declared variables are injected as env vars; pip
        deps are installed into a per-script cache before the
        child process starts.

        ``cron_id`` flows through to the eventual ``script_status``
        event so the backend can persist it on the execution row.
        """
        # 1. Parse the manifest. Bad manifests surface straight to
        # the caller (ManifestError is a ValueError subclass) — we
        # want the UI to show the exact field/line that failed.
        manifest = await asyncio.to_thread(load_manifest, script_dir)

        # 2. Gather values. Resolution order (Phase 1.5):
        #   1. variables.json bindings (literal OR office_secret ref)
        #   2. .secrets.json (literal secret values from Set/Replace UI)
        #   3. Legacy manifest ``from_office_secret`` (fallback)
        #   4. Legacy bare-shape variables.json (back-compat)
        #   5. Manifest ``default``
        #
        # ``env_from`` walks this chain per declared variable.
        # Per-execution overrides apply on top after env_from.
        raw_variables = await asyncio.to_thread(
            self._variables.get_variables, script_name,
        )
        bindings = await asyncio.to_thread(
            self._variables.get_bindings, script_name,
        )
        secrets = await asyncio.to_thread(
            self._secrets.get_script_secrets, script_name,
        )

        # Preflight ANY office-secret reference (binding or legacy
        # manifest field) against the host's office secrets store.
        # The Runner REFUSES to launch when even one referenced secret
        # is missing — raising :class:`MissingOfficeSecretError` lets
        # the dispatch layer emit a single ``setup_office_secret``
        # action_request listing every missing ref. Pre-existing
        # script.yaml ``from_office_secret`` declarations still work
        # via this preflight; new scripts use bindings instead.
        legacy_refs = manifest.office_secret_refs()  # {var_name: ref}
        # Variables with an explicit binding override the manifest's
        # legacy reference: drop the legacy entry so we don't fail
        # preflight for a stale reference the user has since rebound
        # to a literal via the UI.
        legacy_refs = {
            name: ref
            for name, ref in legacy_refs.items()
            if name not in bindings
        }
        binding_refs = {
            name: binding["ref"]
            for name, binding in bindings.items()
            if binding.get("kind") == "office_secret"
        }
        all_refs = {**legacy_refs, **binding_refs}

        office_secrets: dict[str, str] = {}
        if all_refs:
            if not self._office_name:
                raise MissingOfficeSecretError(
                    list(all_refs.values()),
                    script_name=script_name,
                )
            try:
                office_secrets = await asyncio.to_thread(
                    read_office_secrets, self._office_name,
                )
            except CorruptOfficeSecretsError:
                # Let it propagate — the alias above means callers
                # importing ``OfficeSecretsCorruptError`` from this
                # module still catch this raise via isinstance.
                raise
            missing = [
                ref for ref in all_refs.values()
                if ref not in office_secrets
            ]
            if missing:
                raise MissingOfficeSecretError(
                    missing, script_name=script_name,
                )

        manifest_env = manifest.env_from(
            raw_variables, secrets, office_secrets, bindings=bindings,
        )
        if variable_overrides:
            # Overrides are a narrow escape hatch: limited to keys
            # the manifest already declared. This keeps two bad
            # things from happening:
            #   * an override like PYTHONPATH / CUBICLE_* shadowing
            #     Runner-injected metadata.
            #   * undeclared overrides leaking into env and breaking
            #     the "only-declared-vars-injected" contract that
            #     env_from enforces for variables.json.
            declared = {v.name for v in manifest.variables}
            for key, value in variable_overrides.items():
                if key not in declared:
                    logger.warning(
                        "Ignoring override %r for %s: not declared in manifest",
                        key, script_name,
                    )
                    continue
                manifest_env[key] = _stringify_override_value(value)

        # 3. Ensure deps are installed. Fast path (cache hit) is a
        # single stat; slow path runs pip inside the container.
        try:
            await ensure_deps_installed(
                script_dir=script_dir,
                container_name=self._container_name
                if self._use_docker() else None,
                workspace_to_container=self._to_container_path,
            )
        except DepsInstallError as exc:
            logger.error(
                "Script deps install failed for %s: %s",
                script_name, exc,
            )
            raise

        # 4. Allocate the execution record so the monitor loop,
        # the history serialiser, and the log viewer all see a
        # consistent shape.
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        short_id = uuid4().hex[:6]
        exec_id = f"exec-{timestamp}-{short_id}"
        exec_dir = script_dir / "executions" / exec_id
        exec_dir.mkdir(parents=True, exist_ok=True)
        # Chown the per-execution dir so the in-container script
        # subprocess (uid 1000) can drop its own log files /
        # progress.json into it without EACCES.
        chown_to_agent(exec_dir)

        now = datetime.now(timezone.utc).isoformat()
        write_status(exec_dir, {
            "status": "running", "started_at": now,
            "completed_at": None, "duration_seconds": None,
            "exit_code": None, "task_id": task_id,
            "triggered_by": triggered_by, "error_message": None,
        })

        log_path = exec_dir / "log.txt"
        log_handle = await asyncio.to_thread(open, log_path, "w")

        argv, env = self._build_launch_command(
            script_dir=script_dir,
            manifest=manifest,
            script_name=script_name,
            exec_id=exec_id,
            task_id=task_id,
            manifest_env=manifest_env,
            workstream_short_code=workstream_short_code,
            scope_readable_id=scope_readable_id,
        )
        launch_mode = "docker" if env is None else "host"
        logger.debug(
            "Launching v2 script '%s' (%s mode, entry=%s)",
            script_name, launch_mode, manifest.entry_module,
        )

        try:
            subprocess_kwargs: dict[str, object] = {
                "stdout": log_handle,
                "stderr": asyncio.subprocess.STDOUT,
                # cwd must match what the docker branch uses (-w
                # script_dir) so relative paths inside main.py work
                # the same in prod + tests.
                "cwd": str(script_dir),
            }
            if env is not None:
                subprocess_kwargs["env"] = env
            process = await asyncio.create_subprocess_exec(
                *argv, **subprocess_kwargs,
            )
        except Exception as exc:
            # On spawn failure, mark status.json as failed so the
            # UI doesn't show a ghost "running" execution forever.
            # Keep the log.txt (may contain useful diagnostics) but
            # close its handle; the exec_dir stays for the user to
            # inspect — they can delete it from the Files tree.
            log_handle.close()
            completed = datetime.now(timezone.utc).isoformat()
            write_status(exec_dir, {
                "status": "failed", "started_at": now,
                "completed_at": completed, "duration_seconds": 0,
                "exit_code": None, "task_id": task_id,
                "triggered_by": triggered_by,
                "error_message": f"spawn failed: {exc}",
            })
            raise

        started_at = datetime.now(timezone.utc)
        execution = _Execution(
            exec_id=exec_id, script_name=script_name,
            task_id=task_id, triggered_by=triggered_by,
            process=process, exec_dir=exec_dir,
            log_handle=log_handle, started_at=started_at,
            cron_id=cron_id,
        )
        self._track_execution(execution)

        logger.info(
            "script '%s' started: exec_id=%s entry=%s task_id=%s",
            script_name, exec_id, manifest.entry_module, task_id,
        )

        # Emit a "running" script_status event so the backend creates
        # the History row IMMEDIATELY (before the script finishes).
        # Without this, the Execution History tab stayed empty for
        # the whole duration of a long-running script. The terminal
        # status emitted by ``on_complete`` later upserts the same
        # row with completion data. Fire-and-forget — a slow WS
        # consumer must not back-pressure the spawn path (next
        # ``execute()`` call would stall waiting for this publish).
        if self._router is not None:
            event = {
                "type": "script_status",
                "script_name": script_name,
                "execution_id": exec_id,
                "status": "running",
                "task_id": task_id,
                "cron_id": cron_id,
                "triggered_by": triggered_by,
                "started_at": started_at.isoformat(),
                "progress": None,
            }
            async def _publish_running() -> None:
                try:
                    await self._router.publish_event(event)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Failed to publish script_status:running for "
                        "%s/%s — the row will appear when the script "
                        "completes.",
                        script_name, exec_id,
                    )
            task = asyncio.create_task(_publish_running())
            # Self-removing strong ref so GC doesn't collect the
            # task mid-flight.
            _RUNNING_PUBLISH_TASKS.add(task)
            task.add_done_callback(_RUNNING_PUBLISH_TASKS.discard)
        return exec_id

    async def get_status(self, execution_id: str) -> dict:
        """Get the current status of an execution."""
        from src.scripts.script_execution import on_complete

        execution = self._active.get(execution_id)
        if execution is not None:
            exit_code = execution.process.returncode
            if exit_code is not None:
                await on_complete(
                    execution, exit_code, self._active,
                    self._workspace, self._ws,
                    router=self._router,
                    office_id=self._office_id,
                    config_store=self._config_store,
                    manager=self._manager,
                    active_by_task=self._active_by_task,
                )
            else:
                progress = await read_progress(self._workspace, execution.script_name)
                return {
                    "status": "running", "execution_id": execution_id,
                    "script_name": execution.script_name,
                    "task_id": execution.task_id, "progress": progress,
                }

        status = await find_status_on_disk(self._workspace, execution_id)
        if status:
            return {**status, "execution_id": execution_id}
        return {"status": "unknown", "execution_id": execution_id}

    async def kill(self, execution_id: str) -> bool:
        """Terminate a running script. Returns True if found and terminated."""
        from src.scripts.script_execution import on_complete

        execution = self._active.get(execution_id)
        if execution is None:
            return False
        try:
            execution.process.terminate()
        except ProcessLookupError:
            pass
        await on_complete(
            execution, exit_code=-15, active=self._active,
            workspace=self._workspace, ws=self._ws,
            router=self._router,
            office_id=self._office_id,
            config_store=self._config_store,
            manager=self._manager,
            active_by_task=self._active_by_task,
        )
        logger.info("Killed script execution: %s", execution_id)
        return True

    async def monitor_all(self) -> None:
        """Background loop: check active executions; scan outboxes
        while any script is running."""
        from src.scripts.script_execution import monitor_all
        await monitor_all(
            self._active, str(self._workspace),
            self._max_duration, self._ws,
            router=self._router,
            office_id=self._office_id,
            config_store=self._config_store,
            manager=self._manager,
            active_by_task=self._active_by_task,
        )

    async def scan_outbox_for(self, script_name: str) -> int:
        """One-shot outbox scan for a script's ``.outbox/`` directory.

        Public entry point invoked from the tool proxy's
        ``/outbox-scan`` endpoint when the in-container MCP runner
        finishes a script. The in-container path doesn't go through
        the host-side monitor loop (which is what triggers
        ``scan_and_dispatch`` for UI / cron / host-runner executions),
        so a ``cubicle.notify_manager()`` drop from an agent-triggered
        in-container run would sit in ``.outbox/`` forever without
        an explicit nudge. This method IS that nudge.

        Returns the number of dispatched notifications (logged by
        the caller). Safe to call when no outbox exists — the
        watcher early-returns.
        """
        from src.scripts.outbox_watcher import scan_and_dispatch
        if self._config_store is None or self._manager is None:
            logger.warning(
                "scan_outbox_for(%s): ConfigStore or ManagerController "
                "not wired — skipping. notify_manager drops from this "
                "script will not be delivered until cbcl is restarted.",
                script_name,
            )
            return 0
        script_dir = self._workspace / ".scripts" / script_name
        return await scan_and_dispatch(
            script_dir=script_dir,
            script_name=script_name,
            office_id=self._office_id,
            config_store=self._config_store,
            manager=self._manager,
            workspace_root=self._workspace,
        )

    def _track_execution(self, execution: _Execution) -> None:
        """Insert ``execution`` into ``_active`` and the task index."""
        self._active[execution.exec_id] = execution
        if execution.task_id:
            self._active_by_task.setdefault(
                execution.task_id, set(),
            ).add(execution.exec_id)

    def _untrack_execution(self, exec_id: str) -> _Execution | None:
        """Remove ``exec_id`` from ``_active`` and the task index.
        Returns the removed execution (or None if it was already
        gone — idempotent, safe to call from both monitor and
        shutdown paths)."""
        execution = self._active.pop(exec_id, None)
        if execution and execution.task_id:
            bucket = self._active_by_task.get(execution.task_id)
            if bucket is not None:
                bucket.discard(exec_id)
                if not bucket:
                    del self._active_by_task[execution.task_id]
        return execution

    def has_active_script(self, script_name: str) -> bool:
        """Whether any tracked execution exists for this script.

        Used by the cron scheduler's overlap-skip: if a previous
        execution of the same script is still running, don't fire a
        second one on the same tick. Linear over ``self._active`` —
        bounded by ``CUBICLE_MAX_AGENTS`` (default 20), so cheap.
        """
        return any(
            ex.script_name == script_name for ex in self._active.values()
        )

    def has_active_scripts(self, task_id: str) -> bool:
        """Check whether any running script is linked to the given task.

        O(1) via the ``_active_by_task`` index. The board transition
        engine calls this on every task move; previously it scanned
        all active executions, which compounded with frequent moves.
        """
        return bool(self._active_by_task.get(task_id))

    async def get_running_scripts(self) -> list[dict]:
        """Return a summary of all active executions for health reports."""
        results = []
        for ex in self._active.values():
            progress = await read_progress(self._workspace, ex.script_name)
            results.append({
                "script_name": ex.script_name,
                "execution_id": ex.exec_id, "status": "running",
                "progress": progress,
                "task_id": ex.task_id,
            })
        return results

    async def shutdown(self) -> None:
        """Terminate all running scripts and clean up."""
        if self._monitor_task:
            self._monitor_task.cancel()
            self._monitor_task = None
        # Stop the cron scheduler if one was attached by the daemon
        cron = getattr(self, "_cron_scheduler", None)
        if cron is not None:
            try:
                await cron.stop()
            except Exception:
                logger.exception("Failed to stop cron scheduler")
            self._cron_scheduler = None
        for exec_id in list(self._active):
            await self.kill(exec_id)
        logger.info("Script runner shut down")

    def cleanup_orphaned_run_files(self) -> int:
        """Delete any leftover ``_run.py`` files from previous runs."""
        return cleanup_orphaned_run_files(self._workspace)
