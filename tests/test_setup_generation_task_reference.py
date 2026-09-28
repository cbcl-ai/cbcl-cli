"""A setup-wizard generation task is strongly referenced while it runs.

The event loop keeps only weak references to tasks, so a bare
``asyncio.create_task`` result can be garbage-collected mid-generation
(L12, ruff RUF006). The helpers spawn through ``handlers._spawn_background``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from src import setup_generator
from src._handlers._setup import (
    run_generate_office_config,
    run_improve_office_config,
)
from src.handlers import _BACKGROUND_TASKS


@pytest.mark.parametrize(
    ("run", "generator", "task_name"),
    [
        (run_generate_office_config, "generate_office_config", "setup-generate-config"),
        (run_improve_office_config, "improve_office_config", "setup-improve-config"),
    ],
)
async def test_generation_task_is_held_until_it_finishes(
    monkeypatch, run, generator, task_name,
):
    release = asyncio.Event()

    async def generate(**_kwargs) -> None:
        await release.wait()

    monkeypatch.setattr(setup_generator, generator, generate)

    await run({"request_id": "request-1"}, router=AsyncMock(), container_name="c")

    held = [task for task in _BACKGROUND_TASKS if task.get_name() == task_name]
    assert len(held) == 1
    release.set()
    await held[0]
    assert held[0] not in _BACKGROUND_TASKS
