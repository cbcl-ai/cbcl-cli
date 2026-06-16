"""T8.1.4 / T8.1.5 — request frames never replay; failed replay head re-queues."""
from __future__ import annotations

import asyncio
import pytest

from src.connection.ws_client import PlatformWSClient
from src.connection.ws_reconnect import ReconnectManager, TIME_SENSITIVE_TYPES


@pytest.mark.asyncio
async def test_request_fails_fast_when_disconnected():
    c = PlatformWSClient("http://x", "off-1")
    c._connected = False
    with pytest.raises(ConnectionError):
        await c.request("auth_status", {})
    # And nothing was queued (no leaked pending future either).
    assert not c._pending_requests


def test_request_is_time_sensitive_never_replayed():
    assert "request" in TIME_SENSITIVE_TYPES


def test_request_frame_cannot_be_queued():
    # Time-sensitive types raise on queue (never enter the replay queue at all)
    # — the strongest form of "never replayed".
    mgr = ReconnectManager()
    with pytest.raises(ConnectionError):
        mgr.queue_message({"type": "request", "request_id": "r1", "action": "x"})
    assert len(mgr._message_queue) == 0
    mgr.queue_message({"type": "task_activity", "task_id": "t1"})
    assert len(mgr._message_queue) == 1


@pytest.mark.asyncio
async def test_failed_replay_head_is_requeued():
    mgr = ReconnectManager()
    mgr.queue_message({"type": "task_activity", "task_id": "t1"})
    mgr.queue_message({"type": "task_activity", "task_id": "t2"})

    class _FailWS:
        async def send(self, raw):
            raise ConnectionError("send failed")

    await mgr.replay_and_notify(_FailWS())
    # The first message failed to send and must be re-queued at the head, not
    # dropped — both messages survive for the next attempt.
    assert len(mgr._message_queue) == 2
    assert mgr._message_queue[0]["task_id"] == "t1"
