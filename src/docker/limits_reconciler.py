"""Track resource-limit drift without restarting a live office.

Idle observations cannot exclude work starting during asynchronous Docker
recreation. New limits therefore remain pending until an explicit operator
restart after draining and verifying active work. Health ticks and config
sync never destroy an office container.
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
    from src.config import OfficeConfig
    from src.config_sync.sync_service import ConfigStore

logger = logging.getLogger(__name__)

class ResourceLimitReconciler:
    """Track pending limits; legacy component arguments remain API-compatible."""

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
        self._office = office
        self._config_store = config_store
        self._pending = False
        self._lock = asyncio.Lock()

    @property
    def pending(self) -> bool:
        """True while desired limits differ from the running container."""
        return self._pending

    async def on_sync_config(self, config: dict) -> str:
        """Handle a ``sync_config`` office payload.

        Updates the office's desired per-office values from the
        payload (absent/``null``/invalid → no override → host-global
        chain) and reconciles. Returns the reconcile outcome:
        ``"in_sync"`` | ``"deferred"``.
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

        No-op unless a deferred limits change is pending —
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

            if not self._pending or trigger == "sync_config":
                logger.info(
                    "Office '%s': deferring changed container resource limits "
                    "(cpus %s→%s, memory %s→%s). Applying them requires an "
                    "explicit operator restart after draining and verifying active work; "
                    "the running daemon never recreates an office automatically.",
                    self._office.name, applied.cpus, desired.cpus,
                    applied.memory, desired.memory,
                )
            self._pending = True
            return "deferred"
