"""Drain handler tasks while the test's owning event loop is still open."""

from __future__ import annotations

import asyncio
import sys


async def drain_handler_background_tasks() -> None:
    handlers = sys.modules.get("src.handlers")
    if handlers is None:
        return
    current_loop = asyncio.get_running_loop()
    async with asyncio.timeout(1):
        while handlers._BACKGROUND_TASKS:
            tasks = list(handlers._BACKGROUND_TASKS)
            assert all(task.get_loop() is current_loop for task in tasks), (
                "A handler background task escaped its test's event loop"
            )
            for task in tasks:
                if not task.done():
                    task.cancel()
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            failures = [
                outcome for outcome in outcomes if isinstance(outcome, Exception)
            ]
            assert not failures, f"Handler background cleanup failed: {failures!r}"
            await asyncio.sleep(0)
