"""Script Runner — executes Python scripts inside the office container.

The runner dispatches scripts via ``docker exec`` into the long-lived
office container (one container per office, shared with the agents).
This matches ``docs/01-overview/system-architecture.md`` — scripts run inside the agent
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
import json
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

# The stdlib module, aliased: ``_execute_v2`` has a local ``secrets``
# variable (the per-script literal secrets dict), so a bare ``import
# secrets`` would be shadowed exactly where the token mint needs it.
from secrets import token_urlsafe
from typing import TYPE_CHECKING
from uuid import uuid4

from src._chown import chown_to_agent
from src.scripts.deps_installer import DepsCleanupUnconfirmed, DepsInstallError, ensure_deps_installed
from src.scripts.manifest import (
    _RESERVED_VARIABLE_NAMES,
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


def _recover_script_status(exec_dir: Path, task_id: str) -> dict:
    """Persist a terminal projection after independently confirmed cleanup.

    Preserve damaged metadata for diagnosis. It cannot supply process ownership
    and must not make a physically stopped, journal-owned lease unrecoverable.
    """
    path = exec_dir / "status.json"
    status = {}
    if path.exists():
        try:
            status = json.loads(path.read_text())
            if not isinstance(status, dict):
                raise ValueError("Script status must be an object")
            if "status" in status and not isinstance(status["status"], str):
                raise ValueError("Script status must be text")
        except (ValueError, UnicodeError):
            path.rename(exec_dir / f"status.corrupt-{uuid4().hex}.json")
            logger.warning("Preserved corrupt script status during confirmed execution cleanup: %s", exec_dir.name)
            status = {}
    state = status.get("status")
    if isinstance(state, str) and state in {"completed", "failed", "killed", "cancelled", "timeout", "timed_out"}:
        return status
    status.update(
        status="failed", task_id=task_id or None,
        completed_at=datetime.now(timezone.utc).isoformat(), exit_code=-15,
        error_message="The previous script execution was terminated and cleanup was confirmed.",
    )
    if exec_dir.is_dir():
        temporary = exec_dir / f".status-recovery-{uuid4().hex}"
        try:
            temporary.write_text(json.dumps(status, indent=2))
            chown_to_agent(temporary)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return status


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

# Path the host workspace is bind-mounted at inside the office
# container. Every office container uses the same convention (see
# docker/container_manager.py and session_bridge.py).
_CONTAINER_WORKSPACE = "/workspace"

# Per-execution file the launch wrapper writes its in-container PID to
# (under the bind-mounted exec_dir, so the host can read it). Consumed
# by ``script_execution.terminate_execution`` to kill the real process
# inside the container (NEW-2).
_IN_CONTAINER_PID_FILE = "in_container.pid"

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
    ``communicator/src/_agent_image/_mcp_script_exec.py::compute_output_dir``
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
    # Office container this run executes inside (docker mode). None in
    # the host-fallback test path. Used by ``terminate_execution`` to
    # ``docker exec ... kill`` the in-container process — terminating
    # the host-side ``docker exec`` client alone does NOT stop it
    # (Docker doesn't forward signals without a TTY — NEW-2).
    container_name: str | None = None
    # Per-execution collections-token revoker (script-lane completion
    # #2, 2026-08-21): ``on_complete`` calls it on every terminal
    # path — natural exit, timeout, UI kill, shutdown all funnel
    # there. None when the run launched without a per-exec token
    # (registry not wired / host fallback / pre-upgrade daemon).
    collections_token_revoke: Callable[[], None] | None = None
    cleanup_unconfirmed: Callable[[], None] | None = None
    completion_observer: Callable[[str], None] | None = None
    execution_attempt_id: str = ""
    resource_lease: object | None = None
    operation_id: str | None = None
    operation_observer: object | None = None
    operation_cancel_requested: bool = False


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
        platform_url: str = "",
        security_token: str = "",
    ) -> None:
        self._workspace = Path(workspace_path)
        self._secrets = secrets_store
        self._variables = variable_manager
        self._ws = ws_client
        self._router = router
        self._container_name = container_name
        self._office_id = office_id
        self._platform_url = platform_url
        self._security_token = security_token
        self._suppressed_tasks: set[str] = set()
        self._starting_by_task: dict[str, int] = {}
        self._legacy_starting = 0
        self._uncertain_tasks: set[str] = set()
        self._uncertain_scripts: set[str] = set()
        self._legacy_uncertain_launch = False
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
        self._runtime_state = None
        self._resource_supervisor = None
        self._active: dict[str, _Execution] = {}
        # A durable lease precedes dependency preparation and process launch.
        # Recovery must not mistake this live admission for an orphan before
        # _track_execution transfers it into the active execution map.
        self._starting_resource_leases: set[str] = set()
        # Parallel index: task_id → set[exec_id]. Keeps
        # :meth:`has_active_scripts` O(1). Maintained alongside
        # ``_active`` in :meth:`_track_execution` +
        # ``script_execution.on_complete``. Only populated for
        # task-linked executions — manual-trigger runs (task_id=None)
        # never land here.
        self._active_by_task: dict[str, set[str]] = {}
        # NOTE: the long-running `monitor_all()` loop is NOT owned here.
        # The daemon wraps it in a supervised background task and cancels
        # that task on office teardown (see daemon.py `_disconnect_office_
        # process_model`). ScriptRunner deliberately keeps no `_monitor_task`
        # handle — do not "consolidate" the monitor cancel into `shutdown()`,
        # that would re-introduce the T8.2.1 leak (monitor_all has no internal
        # stop flag, so only the daemon's task-cancel stops it).
        # P5-V (review): cron scheduler is attached by the daemon
        # after construction (`set_cron_scheduler` / direct assign).
        # Initialise to None here so `shutdown()` and any future
        # lifecycle code can read `self._cron_scheduler` without the
        # AttributeError-prone `getattr(..., None)` dance.
        self._cron_scheduler: object | None = None
        # Collections endpoint for script subprocesses (spec
        # ui-ux-aug19 D4.3): the per-office tool-proxy URL + the
        # NARROW collections-only bearer token, wired post-
        # construction via :meth:`set_collections_endpoint` (the
        # ``set_manager``/``set_router`` pattern — the proxy is built
        # after the runner in handlers.py). Empty in unit tests and
        # on pre-Item-4 daemons: the env vars are then simply not
        # injected and the SDK's ``cubicle.collections`` raises its
        # teaching error.
        self._collections_url: str = ""
        self._collections_token: str = ""
        # The ToolProxyServer's per-execution token registry
        # (script-lane completion #2) — wired BY the proxy's own
        # constructor via :meth:`set_collections_token_registry`.
        # None in unit tests and against a pre-upgrade proxy: launches
        # then fall back to injecting the office-narrow token above.
        self._collections_registry: object | None = None
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

    def set_runtime_state(self, runtime_state) -> None:
        self._runtime_state = runtime_state

    def set_resource_supervisor(self, supervisor) -> None:
        self._resource_supervisor = supervisor

    def active_execution_count(self) -> int:
        tracked = {
            execution.resource_lease.record["lease_id"]
            for execution in self._active.values()
            if execution.resource_lease is not None
        }
        unresolved = (
            self._runtime_state.active_script_resources()
            if self._runtime_state is not None
            else []
        )
        return (
            len(self._active)
            + sum(self._starting_by_task.values())
            + len(self._uncertain_tasks)
            + len(self._uncertain_scripts)
            + int(self._legacy_uncertain_launch)
            + sum(record["lease_id"] not in tracked for record in unresolved)
        )

    def unleased_resources(self) -> list[str]:
        """Conservative policy-adoption coverage for scripts started in legacy mode."""
        legacy = any(
            execution.resource_lease is None for execution in self._active.values()
        )
        leased_tasks = (
            {
                record["task_id"]
                for record in self._runtime_state.active_script_resources()
            }
            if self._runtime_state is not None
            else set()
        )
        if (
            legacy
            or self._legacy_starting
            or self._legacy_uncertain_launch
            or self._uncertain_scripts
            or self._uncertain_tasks - leased_tasks
        ):
            return ["shared-workspace"]
        return []

    async def _reconcile_legacy_script(self, execution_id: str) -> None:
        from src.scripts.script_resources import terminate_legacy_script_execution
        from src.scripts.script_execution import _read_in_container_pid
        from src.scripts.deps_installer import _container_id
        from src.docker.task_process_cleanup import _confirmed_container_stopped
        import re

        container_id = self._container_name or ""
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            container_id = await _container_id(container_id)
        await terminate_legacy_script_execution(container_id, execution_id)
        paths = list(
            (self._workspace / ".scripts").glob(f"*/executions/{execution_id}")
        )
        if len(paths) != 1 or _read_in_container_pid(paths[0]) is None:
            if not await asyncio.to_thread(_confirmed_container_stopped, container_id):
                raise RuntimeError(
                    "Legacy script launch acknowledgement is missing; resource ownership remains uncertain"
                )
        if len(paths) > 1:
            raise RuntimeError(
                "Legacy script status ownership is ambiguous; resource ownership remains uncertain"
            )
        # Commit both projections before releasing this execution's uncertainty.
        # A stale `running` file would otherwise resurrect its task hold during
        # the unresolved-handoff pass immediately below.
        receipts = (
            [
                receipt
                for receipt in self._runtime_state.unresolved_scripts()
                if receipt["execution_id"] == execution_id
            ]
            if self._runtime_state is not None
            else []
        )
        task_ids = {
            receipt["task_id"] for receipt in receipts if receipt.get("task_id")
        }
        if paths:
            import json

            status = await find_status_on_disk(self._workspace, execution_id) or {}
            if (
                not task_ids
                and isinstance(status.get("task_id"), str)
                and status["task_id"]
            ):
                task_ids.add(status["task_id"])
            if len(task_ids) > 1:
                raise RuntimeError(
                    "Legacy script task ownership is ambiguous; resource ownership remains uncertain"
                )
            if task_ids:
                status["task_id"] = next(iter(task_ids))
            status.update(
                status="failed",
                completed_at=datetime.now(timezone.utc).isoformat(),
                exit_code=-15,
                error_message="The previous script execution was terminated and cleanup was confirmed.",
            )
            # Unlike the best-effort notifier writer, a failed write must keep
            # the resource hold so a later reconciliation can retry safely.
            await asyncio.to_thread(
                (paths[0] / "status.json").write_text, json.dumps(status, indent=2)
            )
            chown_to_agent(paths[0] / "status.json")
        if self._runtime_state is not None:
            for receipt in receipts:
                self._runtime_state.note_script(
                    receipt["task_id"],
                    execution_id,
                    "failed",
                    cycle=receipt["cycle"],
                )
            recorded_tasks = {receipt["task_id"] for receipt in receipts}
            for task_id in task_ids - recorded_tasks:
                self._runtime_state.note_script(task_id, execution_id, "failed")
        self._uncertain_scripts.discard(execution_id)

        pending_tasks = set()
        if self._runtime_state is not None:
            pending_tasks.update(
                receipt["task_id"]
                for receipt in self._runtime_state.unresolved_scripts()
            )
            pending_tasks.update(
                record["task_id"]
                for record in self._runtime_state.active_script_resources()
            )
        unknown_sibling = False
        for other_id in self._uncertain_scripts:
            other_status = await find_status_on_disk(self._workspace, other_id)
            if isinstance(other_status, dict) and other_status.get("task_id"):
                pending_tasks.add(other_status["task_id"])
            else:
                unknown_sibling = True
        if not self._legacy_uncertain_launch and not unknown_sibling:
            for task_id in task_ids - pending_tasks:
                if not self._active_by_task.get(
                    task_id
                ) and not self._starting_by_task.get(task_id):
                    self._uncertain_tasks.discard(task_id)

    async def reconcile_handoffs(self) -> None:
        if self._runtime_state is None:
            return
        # Durable leases are physical ownership, never inferred from status.json.
        # A restarted daemon first cleans the exact immutable container marker.
        from src.scripts.script_resources import ScriptResourceLease

        settled_tasks: set[str] = set()
        for record in self._runtime_state.active_script_resources():
            if record["lease_id"] in self._starting_resource_leases or any(
                execution.resource_lease is not None
                and execution.resource_lease.record["lease_id"] == record["lease_id"]
                for execution in self._active.values()
            ):
                continue
            lease = ScriptResourceLease(
                record,
                self._runtime_state,
                launch_started=True,
                workspace=self._workspace,
            )
            try:
                await lease.confirm_stopped()
                exec_dir = self._workspace / ".scripts" / record["script_name"] / "executions" / record["execution_id"]
                status = await asyncio.to_thread(_recover_script_status, exec_dir, record["task_id"])
                state = status["status"]
                if record["task_id"]:
                    receipts = [
                        receipt for receipt in self._runtime_state.unresolved_scripts()
                        if receipt["execution_id"] == record["execution_id"]
                        and receipt["task_id"] == record["task_id"]
                    ]
                    for receipt in receipts:
                        self._runtime_state.note_script(
                            record["task_id"], record["execution_id"], state,
                            cycle=receipt["cycle"],
                        )
                    settled_tasks.add(record["task_id"])
                event = {
                    "type": "script_status", "script_name": record["script_name"],
                    "execution_id": record["execution_id"], "task_id": record["task_id"] or None,
                    "status": "completed" if state == "completed" else "failed",
                    "error_message": (
                        None if state == "completed" else
                        "The previous script execution was terminated and cleanup was confirmed."
                    ),
                }
                if self._router is not None:
                    await self._router.publish_event(event)
                elif self._ws is not None:
                    await self._ws.send(event)
                lease.release()
            except Exception:
                if record["task_id"]:
                    self._uncertain_tasks.add(record["task_id"])
                logger.exception(
                    "Script resource cleanup remains unconfirmed for %s",
                    record["execution_id"],
                )
        for execution_id in tuple(self._uncertain_scripts):
            try:
                await self._reconcile_legacy_script(execution_id)
            except Exception:
                logger.exception(
                    "Legacy script cleanup remains unconfirmed for %s", execution_id
                )
        for receipt in self._runtime_state.unresolved_scripts():
            if receipt["execution_id"] in self._active:
                # The live monitor owns this execution and its completion.
                # A running status is not an orphan/uncertain launch.
                continue
            status = await self.get_status(receipt["execution_id"])
            state = status.get("status")
            if state in {
                "completed",
                "failed",
                "killed",
                "cancelled",
                "timeout",
                "timed_out",
            }:
                self._runtime_state.note_script(receipt["task_id"], receipt["execution_id"], state, cycle=receipt["cycle"])
                settled_tasks.add(receipt["task_id"])
            else:
                self._uncertain_tasks.add(receipt["task_id"])
        remaining = {
            receipt["task_id"] for receipt in self._runtime_state.unresolved_scripts()
        } | {
            record["task_id"] for record in self._runtime_state.active_script_resources()
        }
        if not self._uncertain_scripts and not self._legacy_uncertain_launch:
            for task_id in settled_tasks - remaining:
                if not self._active_by_task.get(task_id) and not self._starting_by_task.get(task_id):
                    self._uncertain_tasks.discard(task_id)

        from src.scripts.operation_control import reconcile_operations
        await reconcile_operations(self)

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

    def set_router(self, router: object) -> None:
        """Plumb the WS transport reference after construction.

        Same post-hoc wiring pattern as ``set_manager``: the daemon
        builds the ``ScriptRunner`` before the ``WsTransport`` so it
        can keep the construction order linear (script_runner depends
        on workspace + container; router depends on platform URL +
        token). Without this setter, the constructor's ``router=None``
        default sticks and every ``self._router is not None`` guard
        below silently skips the publish path.

        Pre-fix posture (root cause of user-reported "manual Run shows
        a 'queued' toast then nothing"): no execution history row,
        no terminal event, no chat notification. The status.json file
        DID land on disk so the run actually happened; the backend
        just never heard about it. Agent-triggered runs use the
        in-container MCP's direct HTTP POST so they bypassed this
        bug — only the manual UI path was visibly broken.
        """
        if self._router is not None and self._router is not router:
            logger.warning(
                "ScriptRunner.set_router: overwriting existing router "
                "reference — this is unexpected; check the daemon init path",
            )
        self._router = router

    def set_collections_endpoint(self, url: str, token: str) -> None:
        """Plumb the collections RPC endpoint after construction
        (spec ui-ux-aug19 D4.2/D4.3).

        ``url`` is the per-office tool-proxy base URL
        (``http://host.docker.internal:{port}`` — reachable from
        inside the office container); ``token`` is the proxy's NARROW
        collections-only bearer token
        (:attr:`ToolProxyServer.collections_token`), valid ONLY on
        ``POST /collections/rpc``. Both ride the docker launch path
        into every script subprocess as ``CUBICLE_TOOL_PROXY_URL`` +
        ``CUBICLE_COLLECTIONS_TOKEN`` so the SDK's
        ``cubicle.collections`` can reach the office datastore — and
        nothing else on the proxy (scripts never see the main proxy
        token).
        """
        self._collections_url = url or ""
        self._collections_token = token or ""

    def set_collections_token_registry(self, registry: object) -> None:
        """Wire the ToolProxyServer's per-execution collections-token
        registry (script-lane completion #2, 2026-08-21).

        Called BY the proxy's constructor — handlers.py already
        builds the proxy with ``script_runner=...``, so no new wiring
        call site exists. Once wired, every docker-mode launch mints
        its OWN ``CUBICLE_COLLECTIONS_TOKEN``
        (``registry.register_exec_collections_token``) and revokes it
        at every terminal path, scoping a run's collections access to
        the run's lifetime. Unwired (unit tests / an older proxy),
        the launch injects the long-lived office-narrow token from
        :meth:`set_collections_endpoint` as before. The in-container
        agent-triggered script path keeps the office-narrow token
        either way — that split is deliberate and documented on the
        proxy.
        """
        self._collections_registry = registry

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
        execution_caller: dict | None = None,
        operation: dict | None = None,
        _operation_record: dict | None = None,
        _operation_action: str = "start",
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
        reservation = None
        script_dir = self._workspace / ".scripts" / script_name
        resource_lease = None
        operation_record = _operation_record
        preparation_started = False
        from src.scripts.script_resources import dynamic_scripts_enabled

        legacy_start = not dynamic_scripts_enabled(self)
        if legacy_start:
            self._legacy_starting += 1
        if task_id:
            self._starting_by_task[task_id] = self._starting_by_task.get(task_id, 0) + 1
        try:
            reservation = self._runtime_state.reserve("script", task_id or "") if self._runtime_state else None
            task = await self._assert_task_runnable(task_id, execution_caller)
            if operation is not None:
                from src.scripts import managed_operations
                from src.operation_state import OperationConflict

                operation_record, created = managed_operations.begin(
                    self, script_name, operation, task or {}, execution_caller or {}, variable_overrides,
                )
                if not created:
                    if operation_record["execution_id"]:
                        return operation_record["execution_id"]
                    operation_record = None
                    raise OperationConflict("The original operation launch needs reconciliation; no duplicate was started")
            if operation_record is not None:
                from src.scripts.managed_operations import capacity
                budget = capacity(self)
                if budget:
                    budget.reserve(operation_record["operation_id"], self._runtime_state.office_id, operation_record["resources"])
            from src.scripts.script_resources import (
                admit_script_resources,
                dynamic_scripts_enabled,
                reserve_script_resources,
            )

            # Policy changes do not erase ownership of an accepted launch.
            # Before preparation a legacy launch can adopt leases, removing
            # only its own conservative hold so it does not block itself.
            if operation_record is not None or not legacy_start or dynamic_scripts_enabled(self):
                if legacy_start:
                    self._legacy_starting -= 1
                    legacy_start = False
                lease = await reserve_script_resources(self, task, execution_caller)
                await admit_script_resources(
                    self, lease, script_name, task_id, execution_caller
                )
                resource_lease = lease
                if operation_record is not None:
                    operation_record = self._runtime_state.update_operation(
                        operation_record["operation_id"], execution_id=lease.record["execution_id"],
                    )
                self._starting_resource_leases.add(lease.record["lease_id"])
            preparation_started = True
            return await self._execute_v2(
                script_dir=script_dir,
                script_name=script_name,
                variable_overrides=variable_overrides,
                task_id=task_id,
                triggered_by=triggered_by,
                cron_id=cron_id,
                workstream_short_code=workstream_short_code,
                scope_readable_id=scope_readable_id,
                **(
                    {"execution_caller": execution_caller}
                    if execution_caller is not None
                    else {}
                ),
                **(
                    {"resource_lease": resource_lease}
                    if resource_lease is not None
                    else {}
                ),
                **({"operation_record": operation_record, "operation_action": _operation_action}
                   if operation_record is not None else {}),
            )
        except BaseException as exc:
            from src.operations.host_capacity import HostCapacityUnavailable
            if isinstance(exc, HostCapacityUnavailable) and operation_record is not None:
                record = self._runtime_state.update_operation(operation_record["operation_id"],
                    state="unknown" if operation_record.get("external_ref") else "queued", cleanup_confirmed=True)
                from src.scripts.managed_operations import publish
                from src.scripts.capacity_wait import accept_wait
                accepted = accept_wait(self, record, task, execution_caller, _operation_action, variable_overrides)
                await publish(self, record)
                if accepted:
                    raise accepted from exc
                raise
            if not preparation_started and resource_lease is None:
                # Initial authority/resource checks cannot have launched a
                # child. Cancellation here must not create an unrecoverable
                # uncertain task or keep the admission reservation forever.
                if operation_record is not None:
                    record = self._runtime_state.update_operation(operation_record["operation_id"],
                        state="unknown" if operation_record.get("external_ref") else "failed", cleanup_confirmed=True)
                    from src.scripts.managed_operations import publish, release_capacity
                    release_capacity(self, record)
                    await publish(self, record)
                raise
            resource_cleanup_confirmed = False
            if resource_lease is not None:
                try:
                    await asyncio.shield(resource_lease.confirm_stopped())
                    resource_lease.release()
                    resource_cleanup_confirmed = True
                except BaseException:
                    if task_id:
                        self._uncertain_tasks.add(task_id)
                    logger.exception(
                        "Script launch failed; exact resource cleanup is unconfirmed"
                    )
            if operation_record is not None:
                record = self._runtime_state.update_operation(
                    operation_record["operation_id"],
                    state="unknown" if not resource_cleanup_confirmed or operation_record.get("external_ref") else "failed",
                    cleanup_confirmed=resource_cleanup_confirmed,
                )
                from src.scripts.managed_operations import publish, release_capacity
                release_capacity(self, record)
                await publish(self, record)
            if not isinstance(exc, (asyncio.CancelledError, DepsCleanupUnconfirmed)):
                raise

            if resource_cleanup_confirmed:
                if task_id:
                    self._uncertain_tasks.discard(task_id)
            else:
                if legacy_start:
                    self._legacy_uncertain_launch = True
                if task_id:
                    self._uncertain_tasks.add(task_id)
                if resource_lease is None:
                    # Legacy launches have no durable physical lease; retain
                    # their generic admission until ownership is reconciled.
                    reservation = None
            raise
        finally:
            if resource_lease is not None:
                self._starting_resource_leases.discard(resource_lease.record["lease_id"])
            if legacy_start:
                self._legacy_starting -= 1
            if reservation is not None:
                self._runtime_state.release(reservation)
            if task_id:
                remaining = self._starting_by_task[task_id] - 1
                if remaining:
                    self._starting_by_task[task_id] = remaining
                else:
                    self._starting_by_task.pop(task_id, None)

    def suppress_task(self, task_id: str) -> None:
        """Prevent new script launches for a terminal task UUID."""
        self._suppressed_tasks.add(task_id)
        if self._runtime_state is not None:
            wait = self._runtime_state.capacity_wait(task_id)
            if wait and wait["state"] != "retired":
                from src.scripts.capacity_wait import CapacityWaitCoordinator
                try:
                    CapacityWaitCoordinator(self).retire(wait)
                except Exception:
                    logger.warning("Stopped task retains capacity wait for reconciliation: %s", task_id, exc_info=True)

    async def _assert_task_runnable(
        self, task_id: str | None, execution_caller: dict | None = None
    ) -> dict | None:
        if not task_id:
            return
        if task_id in self._suppressed_tasks:
            raise RuntimeError("Task script launch refused: task cancellation was requested")
        if not self._platform_url or not self._office_id:
            if self._use_docker():
                raise RuntimeError("Task script launch refused: authoritative task state unavailable")
            return
        import httpx

        from src.backend_client import auth_headers

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{self._platform_url}/api/offices/{self._office_id}/tasks/{task_id}",
                headers=auth_headers(self._security_token),
            )
        response.raise_for_status()
        task = response.json()
        if not isinstance(task, dict):
            raise RuntimeError("Task script launch refused: authoritative task state unavailable")
        if execution_caller and execution_caller.get("role") != "manager":
            expected_status = {"execute": "in_progress", "review": "review", "triage": "blocked"}.get(execution_caller.get("task_mode"), "in_progress")
            if (
                execution_caller.get("task_id") != task_id
                or execution_caller.get("execution_cycle") != task.get("execution_cycle")
                or execution_caller.get("execution_generation") != task.get("execution_generation")
                or execution_caller.get("review_retry_epoch", 0) != task.get("review_retry_epoch", 0)
                or task.get("status") != expected_status
            ):
                raise RuntimeError("Task script launch refused: execution identity is stale")
            from src.execution_claim import validate_worker_execution

            await validate_worker_execution(
                task_id, execution_caller, platform_url=self._platform_url,
                office_id=self._office_id, security_token=self._security_token,
                office_tool_secret=lambda: getattr(
                    self._resource_supervisor, "_office_tool_secret", ""
                ),
            )
        if self._runtime_state is not None:
            self._runtime_state.observe_cycle(task_id, task.get("execution_cycle"))
        if (
            task_id in self._suppressed_tasks
            or task.get("execution_blocked")
            or task.get("status") not in {"backlog", "ready", "in_progress", "review", "blocked"}
        ):
            raise RuntimeError("Task script launch refused: task is terminal or execution is blocked")
        return task

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
        exec_dir: Path,
        workstream_short_code: str | None = None,
        scope_readable_id: str | None = None,
        collections_exec_token: str | None = None,
        task_output_path: str | None = None,
        execution_marker: str | None = None,
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
        if execution_marker:
            from src.docker.task_process_cleanup import WORKER_EXECUTION_ENV

            meta_env[WORKER_EXECUTION_ENV] = execution_marker
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
        # ``communicator/src/_agent_image/_mcp_script_exec.py::compute_output_dir``
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
        if task_output_path:
            from pathlib import PurePosixPath

            relative = PurePosixPath(task_output_path).relative_to("/workspace")
            if ".." in relative.parts or not relative.parts:
                raise ValueError("Task script output path escapes its workspace")
            host_output_dir = self._workspace.joinpath(*relative.parts)
            if not host_output_dir.resolve().is_relative_to(self._workspace.resolve()):
                raise ValueError("Task script output path escapes its workspace")
        # Pre-create on the host (the docker mount surfaces the same
        # directory inside the container) so the script's first write
        # never races mkdir. Chown each new chain segment so the
        # in-container script subprocess (uid 1000) can write into
        # the per-scope output dir — without this the chain
        # /workspace/outputs/{ws}/{scope}/ ends up root-owned and
        # every script write returns EACCES (the symptom that
        # triggered the v0.2.21 chown sweep).
        from src._chown import _collect_new_parents
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

            # Collections access (spec ui-ux-aug19 D4.3): the proxy
            # URL + the NARROW collections-only token, so the SDK's
            # ``cubicle.collections`` can reach POST /collections/rpc.
            # Runner-owned metadata like every other CUBICLE_* key —
            # both names are in ``_RESERVED_VARIABLE_NAMES`` so the
            # reassert loop below protects them, and the values ride
            # the existing name-only ``-e KEY`` mechanism (NEW-4 —
            # the token never appears in host argv). Docker branch
            # only: ``host.docker.internal`` is meaningless to the
            # host-fallback test path.
            # The PER-EXECUTION token (script-lane completion #2)
            # wins; the office-narrow token is the fallback for runs
            # launched without a wired registry (older proxy, tests).
            collections_token = (
                collections_exec_token or self._collections_token
            )
            if self._collections_url and collections_token:
                meta_env["CUBICLE_TOOL_PROXY_URL"] = self._collections_url
                meta_env["CUBICLE_COLLECTIONS_TOKEN"] = collections_token

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
            # NEW-4: pass each var as ``-e KEY`` (NAME only) and supply
            # the VALUE in the docker-exec CLIENT's own environment, so
            # docker forwards it into the container WITHOUT the value
            # ever appearing in the host command line. The old
            # ``-e KEY=VALUE`` form leaked every secret's value into the
            # host process table (``ps``/``/proc/<pid>/cmdline``, world-
            # readable) for the whole run — contradicting the spec's
            # "the value never appears in ps" guarantee. With ``-e KEY``
            # the value lives only in the client's env
            # (``/proc/<pid>/environ``, readable solely by the owner +
            # root), which docker reads and injects into the container.
            for key in merged:
                argv.extend(["-e", key])
            # Wrap the entry in a tiny shell that records its OWN in-
            # container PID to a bind-mounted pidfile, then ``exec``s
            # into the script (preserving that PID). The host-side
            # ``terminate_execution`` reads the pidfile and
            # ``docker exec ... kill``s that PID — terminating the
            # host ``docker exec`` client alone does NOT stop the
            # in-container process (Docker doesn't forward signals
            # without a TTY — NEW-2). ``exec`` keeps the PID stable
            # through stdbuf→python, and stdout still flows to the
            # log file because the client's stdout pipe is inherited.
            cont_pidfile = self._to_container_path(
                exec_dir / _IN_CONTAINER_PID_FILE
            )
            argv.extend([
                "-w", cont_script_dir,
                self._container_name,
                "sh", "-c",
                'echo $$ > "$1"; exec stdbuf -oL python -m "$2"',
                "cubicle-script", cont_pidfile, manifest.entry_module,
            ])
            # The docker CLIENT process env = the FULL host env (so the
            # client keeps everything it had when it inherited the
            # parent's env — crucially DOCKER_HOST / DOCKER_CONTEXT /
            # DOCKER_CONFIG / DOCKER_TLS_VERIFY / DOCKER_CERT_PATH, which
            # are how it finds a non-default daemon on Docker Desktop,
            # Colima, rootless, or a remote host) OVERLAID with the
            # values to forward via the bare ``-e KEY`` flags above. Only
            # the ``-e KEY``-listed keys (``merged``) are injected INTO
            # the container; the rest of this env stays in the client, so
            # forwarding the full host env here does NOT leak it to the
            # script. The connection/resolution-critical keys are then
            # re-forced to the host's values so a (pathological) script
            # variable named e.g. PATH / DOCKER_HOST can't hijack the
            # client's ability to reach the daemon.
            launch_env = {**os.environ, **merged}
            for _k in (
                "PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG",
                "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
            ):
                if _k in os.environ:
                    launch_env[_k] = os.environ[_k]
            return argv, launch_env

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
        execution_caller: dict | None = None,
        resource_lease=None,
        operation_record: dict | None = None,
        operation_action: str = "start",
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
        if operation_action != "start":
            entry = getattr(manifest, f"operation_{operation_action}_entry_point", None)
            if operation_record is None or manifest.operation_mode != "external" or not entry:
                raise ValueError(f"This script does not support operation {operation_action}")
            manifest = manifest.model_copy(update={"entry_point": entry})
        from src.office_secrets.transient import human_action_overrides

        human_input_overrides = human_action_overrides(
            variable_overrides, {variable.name: variable.is_secret for variable in manifest.variables},
        )

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
        all_refs = {
            name: reference for name, reference in {**legacy_refs, **binding_refs}.items()
            if name not in human_input_overrides
        }
        if any(reference.startswith("CBCL_INPUT_") for reference in all_refs.values()):
            raise ValueError("Secure human inputs require a task-bound from_human_action override, not a general Office Secret binding")

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
                if key not in human_input_overrides:
                    manifest_env[key] = _stringify_override_value(value)

        # 3. Ensure deps are installed. Fast path (cache hit) is a
        # single stat; slow path runs pip inside the container.
        try:
            if resource_lease is not None:
                from src.scripts.deps_installer import plan_install

                if plan_install(script_dir).needed:
                    resource_lease.mark_launch(preparation=True)
            await ensure_deps_installed(
                script_dir=script_dir,
                container_name=self._container_name if self._use_docker() else None,
                workspace_to_container=self._to_container_path,
                **(
                    {"execution_marker": resource_lease.record["marker"]}
                    if resource_lease
                    else {}
                ),
            )
        except DepsInstallError as exc:
            if resource_lease is not None and not isinstance(
                exc, DepsCleanupUnconfirmed
            ):
                resource_lease.mark_launch(preparation=True, started=False)
            logger.error(
                "Script deps install failed for %s: %s",
                script_name, exc,
            )
            raise

        if human_input_overrides:
            from src.office_secrets.transient import resolve_human_action_input

            for variable_name, request_id in human_input_overrides.items():
                manifest_env[variable_name] = resolve_human_action_input(
                    self._office_name, request_id, task_id, script_name, variable_name,
                )

        # 4. Allocate the execution record so the monitor loop,
        # the history serialiser, and the log viewer all see a
        # consistent shape.
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        short_id = uuid4().hex[:6]
        exec_id = (
            resource_lease.record["execution_id"]
            if resource_lease
            else f"exec-{timestamp}-{short_id}"
        )
        exec_dir = script_dir / "executions" / exec_id
        exec_dir.mkdir(parents=True, exist_ok=True)
        # Chown the per-execution dir so the in-container script
        # subprocess (uid 1000) can drop its own log files /
        # progress.json into it without EACCES.
        chown_to_agent(exec_dir)
        if operation_record is not None:
            from src.scripts import managed_operations

            manifest_env.update(managed_operations.prepare_launch(
                self, operation_record, exec_dir, operation_action, execution_caller, variable_overrides,
            ))

        now = datetime.now(timezone.utc).isoformat()
        write_status(exec_dir, {
            "status": "running", "started_at": now,
            "completed_at": None, "duration_seconds": None,
            "exit_code": None, "task_id": task_id,
            "triggered_by": triggered_by, "error_message": None,
        })

        log_path = exec_dir / "log.txt"
        log_handle = await asyncio.to_thread(open, log_path, "w")

        # Per-execution collections token (script-lane completion #2,
        # 2026-08-21): mint + register a run-scoped credential so the
        # script's collections access dies with the execution instead
        # of outliving it on the daemon-lifetime office-narrow token.
        # Docker-mode only — the host fallback never injects the
        # collections endpoint. A mint/register failure degrades to
        # the office-narrow fallback rather than blocking the launch.
        exec_collections_token: str | None = None
        collections_token_revoke: Callable[[], None] | None = None
        registry = self._collections_registry
        if (
            registry is not None
            and self._collections_url
            and self._use_docker()
        ):
            exec_collections_token = token_urlsafe(32)
            try:
                registry.register_exec_collections_token(
                    exec_collections_token,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Per-execution collections token registration "
                    "failed for %s — falling back to the office-narrow "
                    "token.",
                    exec_id, exc_info=True,
                )
                exec_collections_token = None
            else:
                collections_token_revoke = partial(
                    registry.revoke_exec_collections_token,
                    exec_collections_token,
                )

        argv, env = self._build_launch_command(
            script_dir=script_dir,
            manifest=manifest,
            script_name=script_name,
            exec_id=exec_id,
            task_id=task_id,
            manifest_env=manifest_env,
            exec_dir=exec_dir,
            workstream_short_code=workstream_short_code,
            scope_readable_id=scope_readable_id,
            collections_exec_token=exec_collections_token,
            **(
                {"execution_marker": resource_lease.record["marker"]}
                if resource_lease
                else {}
            ),
            **(
                {"task_output_path": execution_caller["output_dir"]}
                if execution_caller
                and execution_caller.get("agent_instance_id")
                and execution_caller.get("output_dir")
                and operation_record is None
                else {}
            ),
            **({"task_output_path": f"/workspace/outputs/operations/{operation_record['operation_id']}"}
               if operation_record is not None else {}),
        )
        # NEW-4: the docker branch now returns a non-None env (it forwards
        # var VALUES to the client's env for ``-e KEY`` name-only flags),
        # so ``env is None`` no longer distinguishes docker from host.
        # Use the authoritative container check for the log label.
        launch_mode = "docker" if self._use_docker() else "host"
        logger.debug(
            "Launching v2 script '%s' (%s mode, entry=%s)",
            script_name, launch_mode, manifest.entry_module,
        )

        try:
            fresh_task = await self._assert_task_runnable(task_id, execution_caller)
            if resource_lease is not None:
                from src.agent_execution_policy import execution_resources

                fresh_resources = execution_resources(
                    {
                        "execution_resources": (fresh_task or {}).get(
                            "execution_resources"
                        )
                    },
                )
                if fresh_resources != resource_lease.record["resources"]:
                    raise RuntimeError(
                        "Script launch refused: task resource declarations changed during preparation"
                    )
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
            if resource_lease is not None:
                resource_lease.runtime.set_script_resource_state(
                    resource_lease.record["lease_id"], "launching"
                )
                resource_lease.mark_launch()
                if not self._use_docker():
                    subprocess_kwargs["start_new_session"] = True
                launch = asyncio.create_task(
                    asyncio.create_subprocess_exec(*argv, **subprocess_kwargs)
                )
                try:
                    process = await asyncio.shield(launch)
                except asyncio.CancelledError:
                    # No late spawn may appear AFTER cleanup proved the marker absent.
                    resource_lease.process = await asyncio.shield(launch)
                    raise
                except Exception:
                    # create_subprocess_exec failed before a client existed.
                    resource_lease.mark_launch(started=False)
                    raise
                resource_lease.process = process
            else:
                process = await asyncio.create_subprocess_exec(
                    *argv, **subprocess_kwargs
                )
        except BaseException as exc:
            # On spawn failure, mark status.json as failed so the
            # UI doesn't show a ghost "running" execution forever.
            # Keep the log.txt (may contain useful diagnostics) but
            # close its handle; the exec_dir stays for the user to
            # inspect — they can delete it from the Files tree.
            # Spawn failure is the one terminal path that never builds
            # an ``_Execution`` (so ``on_complete`` can't revoke) —
            # revoke the per-execution collections token here.
            if collections_token_revoke is not None:
                try:
                    collections_token_revoke()
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Per-execution collections token revoke failed "
                        "on spawn failure for %s", exec_id, exc_info=True,
                    )
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
            exec_id=exec_id,
            script_name=script_name,
            task_id=task_id,
            triggered_by=triggered_by,
            process=process,
            exec_dir=exec_dir,
            log_handle=log_handle,
            started_at=started_at,
            cron_id=cron_id,
            container_name=self._container_name if self._use_docker() else None,
            collections_token_revoke=collections_token_revoke,
            execution_attempt_id=(execution_caller or {}).get("attempt_id") or "",
            resource_lease=resource_lease,
            operation_id=operation_record["operation_id"] if operation_record is not None else None,
        )
        self._track_execution(execution)
        if execution.operation_id:
            from src.scripts.managed_operations import publish
            await publish(self, self._runtime_state.get_operation(execution.operation_id))

        logger.info(
            "script '%s' started: exec_id=%s entry=%s task_id=%s",
            script_name, exec_id, manifest.entry_module, task_id,
        )

        # Emit a "running" script_status event so the backend creates
        # the History row IMMEDIATELY (before the script finishes).
        # Without this, the Execution History tab stayed empty for
        # the whole duration of a long-running script. The terminal
        # status emitted by ``on_complete`` later upserts the same
        # row with completion data.
        #
        # AWAIT the publish INLINE rather than fire-and-forget: a
        # sub-50ms script (``echo hello`` smoke tests, dry-run no-op
        # invocations, cron health-checks) used to race terminal vs
        # running, with terminal arriving first and the row flipping
        # back to ``running`` when the late fire-and-forget landed.
        # The publish path is a Redis Streams XADD which completes in
        # ~1ms locally and at most ~10ms over a WAN; back-pressure
        # risk is negligible compared with the visible UI bug.
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
            try:
                await self._router.publish_event(event)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to publish script_status:running for "
                    "%s/%s — the row will appear when the script "
                    "completes.",
                    script_name, exec_id,
                )
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
        if self._runtime_state is not None and any(
            record["execution_id"] == execution_id
            for record in self._runtime_state.active_script_resources()
        ):
            return {
                "status": "unknown",
                "execution_id": execution_id,
                "resource_cleanup_pending": True,
                "error_message": "Script cleanup or completion recording is unconfirmed. Restore container access and retry Stop before starting conflicting work.",
            }
        if status:
            return {**status, "execution_id": execution_id}
        return {"status": "unknown", "execution_id": execution_id}

    async def get_operation(self, operation_id: str) -> dict:
        from src.scripts.operation_control import get_operation
        return await get_operation(self, operation_id)

    async def control_operation(self, operation_id: str, action: str, caller: dict) -> dict:
        from src.scripts.operation_control import control_operation
        return await control_operation(self, operation_id, action, caller)

    async def kill(self, execution_id: str) -> bool:
        """Terminate a running script. Returns True if found and terminated."""
        from src.scripts.script_execution import on_complete, terminate_execution

        execution = self._active.get(execution_id)
        if execution is None:
            if self._runtime_state is not None:
                from src.scripts.script_resources import ScriptResourceLease

                record = next(
                    (
                        record
                        for record in self._runtime_state.active_script_resources()
                        if record["execution_id"] == execution_id
                    ),
                    None,
                )
                if record is not None:
                    lease = ScriptResourceLease(
                        record,
                        self._runtime_state,
                        launch_started=True,
                        workspace=self._workspace,
                    )
                    await lease.confirm_stopped()
                    if record["task_id"]:
                        self._runtime_state.note_script(
                            record["task_id"], execution_id, "failed"
                        )
                        self._uncertain_tasks.discard(record["task_id"])
                    lease.release()
                    return True
            if execution_id in self._uncertain_scripts:
                await self._reconcile_legacy_script(execution_id)
                return True
            return False
        # Kill the REAL process inside the container, not just the host
        # docker-exec client (NEW-2). Terminating the client alone would
        # leave the in-container python running.
        execution.operation_cancel_requested = True
        await terminate_execution(execution)
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
            reconcile=self.reconcile_handoffs,
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
        if execution.operation_id:
            from src.scripts import managed_operations
            managed_operations.attach_completion_observer(self, execution)
        if execution.resource_lease is not None:
            execution.resource_lease.runtime.set_script_resource_state(
                execution.resource_lease.record["lease_id"], "running"
            )
        if execution.container_name and execution.resource_lease is None:

            def legacy_cleanup_unconfirmed() -> None:
                self._uncertain_scripts.add(execution.exec_id)
                if execution.task_id:
                    self._uncertain_tasks.add(execution.task_id)

            execution.cleanup_unconfirmed = legacy_cleanup_unconfirmed
        if execution.task_id:
            self._active_by_task.setdefault(
                execution.task_id, set(),
            ).add(execution.exec_id)
            if self._runtime_state is not None:
                cycle = self._runtime_state.current_cycle(execution.task_id)

                def persist_script(state: str) -> None:
                    if execution.execution_attempt_id:
                        self._runtime_state.note_script_owner(execution.exec_id, execution.execution_attempt_id)
                    self._runtime_state.note_script(execution.task_id, execution.exec_id, state, cycle=cycle)

                execution.completion_observer = persist_script
                persist_script("running")
        if execution.resource_lease is not None:
            previous_observer = execution.completion_observer

            def persist_resource_completion(state: str) -> None:
                if previous_observer is not None:
                    previous_observer(state)
                execution.resource_lease.release()
                if execution.task_id:
                    self._uncertain_tasks.discard(execution.task_id)

            execution.completion_observer = persist_resource_completion

    def has_active_script(self, script_name: str) -> bool:
        """Whether any tracked execution exists for this script.

        Used by the cron scheduler's overlap-skip: if a previous
        execution of the same script is still running, don't fire a
        second one on the same tick. Linear over ``self._active`` —
        bounded by ``CUBICLE_MAX_AGENTS`` (default 20), so cheap.
        """
        return any(
            ex.script_name == script_name for ex in self._active.values()
        ) or bool(
            self._runtime_state is not None
            and any(
                record["script_name"] == script_name
                for record in self._runtime_state.active_script_resources()
            )
        )

    def has_active_scripts(self, task_id: str) -> bool:
        """Check whether any running script is linked to the given task.

        O(1) via the ``_active_by_task`` index. The board transition
        engine calls this on every task move; previously it scanned
        all active executions, which compounded with frequent moves.
        """
        return bool(
            self._active_by_task.get(task_id)
            or self._starting_by_task.get(task_id)
            or task_id in self._uncertain_tasks
            or (
                self._runtime_state is not None
                and any(
                    record["task_id"] == task_id
                    for record in self._runtime_state.active_script_resources()
                )
            )
        )

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
        """Terminate all running scripts and clean up.

        The supervised ``monitor_all()`` loop is cancelled by the daemon
        (it owns the background task) — NOT here. See the note in
        ``__init__`` and daemon.py ``_disconnect_office_process_model``.
        """
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
