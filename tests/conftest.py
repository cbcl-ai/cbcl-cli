"""Test-owned asynchronous resources must not survive loop teardown."""

import pytest_asyncio

from tests.background_tasks import drain_handler_background_tasks


@pytest_asyncio.fixture(autouse=True)
async def handler_background_task_cleanup():
    yield
    await drain_handler_background_tasks()

