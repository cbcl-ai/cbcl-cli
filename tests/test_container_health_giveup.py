"""The health loop's give-up message tells the operator where to look.

The office container's main process is ``tail -f /dev/null`` and all work
in it runs through ``docker exec``, so ``docker logs`` on it is always
empty. The message the loop stores as the office's ``last_error`` (returned
by the office status API and logged by cbcl) must therefore point at the
container's state.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.docker import container_health


class _DeadContainer:
    """A container that never comes back up."""

    status = "exited"

    def __init__(self, name: object) -> None:
        self.name = name

    def reload(self) -> None:
        return None


async def _give_up_message(monkeypatch, container: _DeadContainer) -> str:
    """Run the health loop until it gives up on ``container``."""
    messages: list[str] = []

    async def _sleep(_seconds: float) -> None:
        # The loop sleeps before every pass; end it after the give-up.
        if messages:
            raise asyncio.CancelledError

    async def _on_giveup(_office_id: str, message: str) -> None:
        messages.append(message)

    # Only this module's view of asyncio: the real ``asyncio.sleep`` stays.
    monkeypatch.setattr(
        container_health,
        "asyncio",
        SimpleNamespace(
            sleep=_sleep,
            to_thread=asyncio.to_thread,
            CancelledError=asyncio.CancelledError,
        ),
    )
    monkeypatch.setattr(container_health, "_MAX_ESCALATIONS", 1)

    await asyncio.wait_for(
        container_health.health_check_all(
            {"office-1": container}, on_giveup=_on_giveup,
        ),
        timeout=10,
    )
    assert len(messages) == 1
    return messages[0]


@pytest.mark.asyncio
async def test_give_up_message_points_at_the_container_state(monkeypatch) -> None:
    message = await _give_up_message(monkeypatch, _DeadContainer("cbcl-office-acme"))

    assert "'docker inspect cbcl-office-acme'" in message
    assert "'cbcl logs'" in message
    assert "cbcl stop && cbcl start" in message
    # The container's own log is always empty.
    assert "docker logs" not in message


@pytest.mark.asyncio
async def test_give_up_message_without_a_container_name(monkeypatch) -> None:
    """A container object without a usable name still gets a readable hint."""
    message = await _give_up_message(monkeypatch, _DeadContainer(None))

    assert "'docker inspect <the office container>'" in message
    assert "docker logs" not in message
