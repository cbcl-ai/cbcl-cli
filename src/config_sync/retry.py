"""Retry failed materialization without cancelling a thread still writing files."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging

from src.agent_execution_policy import ExecutionPolicyDrainPending

logger = logging.getLogger("cbcl.config-sync")


class ConfigSyncRetry:
    """Serialize/coalesce revisions and retry the latest until applied or closed."""

    def __init__(
        self,
        apply: Callable[[dict, Callable[[], bool]], Awaitable[None]],
        pause: Callable[[], None],
        report: Callable[[str | None], None],
        *,
        retry_delay: float = 2.0,
        max_retry_delay: float = 30.0,
    ) -> None:
        self._apply = apply
        self._pause = pause
        self._report = report
        self._retry_delay = retry_delay
        self._max_retry_delay = max_retry_delay
        self._latest: tuple[int, dict] | None = None
        self._version = 0
        self._changed = asyncio.Event()
        self._closed = False
        self._task: asyncio.Task | None = None
        self._waiters: dict[int, asyncio.Future] = {}

    async def submit(self, message: dict) -> None:
        if self._closed:
            return
        self._pause()
        self._version += 1
        version = self._version
        self._latest = (version, message)
        waiter = asyncio.get_running_loop().create_future()
        self._waiters[version] = waiter
        self._changed.set()
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="config-materialization")
        # Caller cancellation must not abandon a live to_thread writer while
        # releasing the admission lock to another revision.
        await asyncio.shield(waiter)

    def _resolve_waiters(self, version: int) -> None:
        for previous in list(self._waiters):
            if previous <= version:
                waiter = self._waiters.pop(previous)
                if not waiter.done():
                    waiter.set_result(None)

    async def _run(self) -> None:
        delay = self._retry_delay
        previous_version = None
        try:
            while not self._closed and self._latest is not None:
                version, message = self._latest
                if version != previous_version:
                    delay = self._retry_delay
                    previous_version = version
                self._changed.clear()

                def is_current() -> bool:
                    return not self._closed and self._version == version

                failed = False
                try:
                    await self._apply(message, is_current)
                except Exception as exc:
                    failed = True
                    if is_current():
                        error = (
                            str(exc)
                            if isinstance(exc, ExecutionPolicyDrainPending)
                            else f"Configuration materialization failed ({type(exc).__name__}); retrying"
                        )
                        self._report(error)
                        if isinstance(exc, ExecutionPolicyDrainPending):
                            logger.info("%s", error)
                        else:
                            logger.warning("%s", error)
                else:
                    if is_current():
                        self._report(None)
                finally:
                    self._resolve_waiters(version)
                if not is_current():
                    continue
                if not failed:
                    return
                try:
                    await asyncio.wait_for(self._changed.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, self._max_retry_delay)
        finally:
            self._task = None
            self._resolve_waiters(self._version)

    async def close(self) -> None:
        self._closed = True
        self._pause()
        self._changed.set()
        if self._task is not None:
            await asyncio.shield(self._task)
