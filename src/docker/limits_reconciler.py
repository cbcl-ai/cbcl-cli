"""Sync-driven reconciliation of per-office container resource limits.

The backend ships the per-office ``container_cpus`` / ``container_memory``
overrides (Office Settings → Resources; ``None`` = daemon default) in
every ``sync_config``. Docker can only apply CPU/memory limits at
container CREATE time, so when the DESIRED limits differ from what the
running container was created with, the office container must be
recreated. Recreating kills any in-flight ``docker exec`` session
(worker / Manager / Planner CLI runs and script subprocesses), so the
policy is:

* **Idle office** (no working/spawning agents, Manager not mid-turn,
  no running scripts) → recreate immediately.
* **Busy office** → DEFER: mark the change pending and re-check on the
  existing periodic machinery (the HealthReporter's 15s tick calls
  :meth:`recheck_pending`) until the office goes idle. If a race slips
  a recreate under a just-started session, the standard crash-recovery
  machinery covers the casualties — the dispatcher re-dispatches
  orphaned ``in_progress`` tasks in place and the watchdog caps
  respawns.

Every decision (applied / deferred / failed) is logged so an operator
can always answer "why hasn't my limits change landed yet?".

Applied-vs-desired state rides the ConfigStore
(``mark_resource_limits_applied`` / ``resource_limits_applied``) — the
same drift pattern as ``extra_mounts``, except this reconciler can
actually FIX the drift instead of only warning.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from src.config import (
    coerce_per_office_cpus,
    coerce_per_office_memory,
    resolve_office_resource_limits,
)

if TYPE_CHECKING:
    from src.config import OfficeConfig, OfficeResourceLimits
    from src.config_sync.sync_service import ConfigStore

logger = logging.getLogger(__name__)

# Supervisor agent states that mean "an assignment is (about to be)
# executing inside the container". READY is deliberately NOT busy: a
# READY subprocess is alive but has no in-flight ``docker exec`` — its
# next exec simply lands in the fresh container (same name).
_BUSY_AGENT_STATES = ("spawning", "working")


class ResourceLimitReconciler:
    """Recreate-when-idle / defer-while-busy limits applier for ONE office."""

    def __init__(
        self,
        *,
        containers: object,
        office: OfficeConfig,
        config_store: ConfigStore,
        supervisor: object | None = None,
        script_runner: object | None = None,
        manager: object | None = None,
    ) -> None:
        self._containers = containers
        self._office = office
        self._config_store = config_store
        self._supervisor = supervisor
        self._script_runner = script_runner
        self._manager = manager
        # True while a limits change is waiting for the office to go
        # idle (or for a failed recreate to be retried).
        self._pending = False
        # Serializes reconcile runs — a sync_config landing during a
        # health-tick recheck must not race two recreates.
        self._lock = asyncio.Lock()

    @property
    def pending(self) -> bool:
        """True while a limits change is deferred/awaiting retry."""
        return self._pending

    async def on_sync_config(self, config: dict) -> str:
        """Handle a ``sync_config`` office payload.

        Updates the office's desired per-office values from the
        payload (absent/``null``/invalid → no override → host-global
        chain) and reconciles. Returns the reconcile outcome:
        ``"in_sync"`` | ``"recreated"`` | ``"deferred"`` | ``"failed"``.
        """
        source = f"sync_config (office '{self._office.name}')"
        self._office.container_cpus = coerce_per_office_cpus(
            config.get("container_cpus"), source,
        )
        self._office.container_memory = coerce_per_office_memory(
            config.get("container_memory"), source,
        )
        return await self._reconcile(trigger="sync_config")

    async def recheck_pending(self) -> str:
        """Periodic re-check hook (called from the HealthReporter tick).

        No-op unless a deferred/failed limits change is pending —
        keeps the 15s health tick free of docker chatter in the
        steady state.
        """
        if not self._pending:
            return "in_sync"
        return await self._reconcile(trigger="recheck")

    # -- internals ----------------------------------------------------------

    async def _reconcile(self, trigger: str) -> str:
        async with self._lock:
            desired = resolve_office_resource_limits(
                self._office.container_cpus, self._office.container_memory,
            )
            applied = self._config_store.resource_limits_applied
            if applied is None or applied == desired:
                # No baseline yet (container bring-up will stamp one)
                # or nothing changed — clear any stale pending flag
                # (e.g. the user reverted the change before the office
                # went idle).
                if self._pending:
                    logger.info(
                        "Office '%s': container resource limits back in "
                        "sync (cpus=%s, memory=%s) — pending recreate "
                        "cancelled.",
                        self._office.name,
                        desired.cpus, desired.memory,
                    )
                self._pending = False
                return "in_sync"

            if await self._office_busy():
                # INFO on the first defer (the operator-facing "why
                # didn't it apply" answer), DEBUG on the periodic
                # re-checks so a long-running task doesn't spam the
                # log every 15 seconds.
                level = (
                    logging.DEBUG
                    if self._pending and trigger == "recheck"
                    else logging.INFO
                )
                logger.log(
                    level,
                    "Office '%s': container resource limits changed "
                    "(cpus %s→%s, memory %s→%s) but the office is busy "
                    "(working agents, an in-flight Manager turn, or "
                    "running scripts) — deferring container recreate "
                    "until idle; re-checking on the health tick.",
                    self._office.name,
                    applied.cpus, desired.cpus,
                    applied.memory, desired.memory,
                )
                self._pending = True
                return "deferred"

            logger.info(
                "Office '%s': applying changed container resource limits "
                "(cpus %s→%s, memory %s→%s) — office is idle, recreating "
                "the container.",
                self._office.name,
                applied.cpus, desired.cpus,
                applied.memory, desired.memory,
            )
            try:
                await self._containers.recreate_office(self._office)
            except Exception:
                logger.exception(
                    "Office '%s': container recreate for resource limits "
                    "failed — will retry on the next health tick.",
                    self._office.name,
                )
                self._pending = True
                return "failed"
            self._config_store.mark_resource_limits_applied(desired)
            self._pending = False
            logger.info(
                "Office '%s': container recreated with resource limits "
                "cpus=%s, memory=%s.",
                self._office.name, desired.cpus, desired.memory,
            )
            return "recreated"

    async def _office_busy(self) -> bool:
        """True when recreating the container would kill in-flight work.

        Checks, in order: the Manager (mid-turn), supervisor agents
        (spawning/working — an active or imminent ``docker exec``),
        and running scripts. Fails BUSY on an unreadable script state
        — when in doubt, don't kill.
        """
        manager = self._manager
        if manager is not None and bool(getattr(manager, "is_busy", False)):
            return True

        supervisor = self._supervisor
        if supervisor is not None:
            try:
                statuses = supervisor.get_all_statuses()
            except Exception:
                logger.debug(
                    "supervisor status check failed during limits "
                    "reconcile — treating office as busy", exc_info=True,
                )
                return True
            for info in statuses.values():
                status = (
                    info.get("status") if isinstance(info, dict)
                    else str(info)
                )
                if status in _BUSY_AGENT_STATES:
                    return True

        runner = self._script_runner
        if runner is not None:
            try:
                if await runner.get_running_scripts():
                    return True
            except Exception:
                logger.debug(
                    "running-scripts check failed during limits "
                    "reconcile — treating office as busy", exc_info=True,
                )
                return True

        return False
