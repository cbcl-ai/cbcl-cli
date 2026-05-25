"""Tests for the ``agent_queues`` + ``agent_feed`` request-bridge actions.

The backend's ``/agent-queues`` and ``/agents/.../recent-activity``
endpoints route reads through these bridge actions because the
dispatcher writes its queue/feed state to the communicator's
OWN Redis (in-process fakeredis by default), not the backend's
docker-compose Redis. Without these handlers the backend reads
were always empty.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest

from src._handlers._requests import (
    _read_agent_feed,
    _snapshot_agent_queues,
    dispatch_backend_request,
)


@pytest.fixture
async def redis_client():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
def office():
    o = MagicMock()
    o.id = "11111111-1111-1111-1111-111111111111"
    return o


@pytest.fixture
def router():
    r = MagicMock()
    r.ws_client = MagicMock()
    r.ws_client.send = AsyncMock()
    return r


# ─── _snapshot_agent_queues ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_snapshot_empty(redis_client):
    out = await _snapshot_agent_queues(redis_client, "office-1")
    assert out == {}


@pytest.mark.asyncio
async def test_snapshot_pending_only(redis_client):
    prefix = "office:office-1:aq"
    await redis_client.zadd(
        f"{prefix}:python-developer:queue",
        {json.dumps({
            "task_id": "task-uuid-1",
            "readable_id": "WR-003.T14",
            "title": "Build form",
            "priority": "medium",
            "status": "ready",
            "assigned_agent": "python-developer",
        }): 1.0},
    )
    out = await _snapshot_agent_queues(redis_client, "office-1")
    assert "python-developer" in out
    assert out["python-developer"]["active"] is None
    pending = out["python-developer"]["pending"]
    assert len(pending) == 1
    assert pending[0]["readable_id"] == "WR-003.T14"
    assert pending[0]["title"] == "Build form"


@pytest.mark.asyncio
async def test_snapshot_active_only(redis_client):
    prefix = "office:office-1:aq"
    await redis_client.hset(
        f"{prefix}:react-developer:active",
        mapping={
            "task_id": "task-uuid-2",
            "readable_id": "WR-003.T15",
            "status": "in_progress",
            "mode": "execute",
            "pid": "42",
            "started_at": "2026-05-18T10:00:00Z",
        },
    )
    out = await _snapshot_agent_queues(redis_client, "office-1")
    assert "react-developer" in out
    assert out["react-developer"]["pending"] == []
    active = out["react-developer"]["active"]
    assert active is not None
    assert active["readable_id"] == "WR-003.T15"
    assert active["mode"] == "execute"


@pytest.mark.asyncio
async def test_snapshot_pending_and_active(redis_client):
    prefix = "office:office-1:aq"
    await redis_client.zadd(
        f"{prefix}:dev:queue",
        {json.dumps({
            "task_id": "t1", "readable_id": "WR.T01", "priority": "high",
        }): 1.0},
    )
    await redis_client.hset(
        f"{prefix}:dev:active",
        mapping={
            "task_id": "t2", "readable_id": "WR.T02",
            "status": "in_progress", "mode": "execute",
            "started_at": "now",
        },
    )
    out = await _snapshot_agent_queues(redis_client, "office-1")
    assert len(out["dev"]["pending"]) == 1
    assert out["dev"]["pending"][0]["task_id"] == "t1"
    assert out["dev"]["active"]["task_id"] == "t2"


@pytest.mark.asyncio
async def test_snapshot_skips_corrupt_json(redis_client):
    """A corrupt queue entry should be silently dropped — the rest
    of the queue + active state pass through."""
    prefix = "office:office-1:aq"
    await redis_client.zadd(
        f"{prefix}:dev:queue",
        {
            "{not json": 1.0,
            json.dumps({"task_id": "good", "readable_id": "OK.T01"}): 2.0,
        },
    )
    out = await _snapshot_agent_queues(redis_client, "office-1")
    # One good row only — the bad one is dropped, no exception.
    assert len(out["dev"]["pending"]) == 1
    assert out["dev"]["pending"][0]["task_id"] == "good"


# ─── _read_agent_feed ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_feed_empty(redis_client):
    out = await _read_agent_feed(redis_client, "office-1", "dev", 30)
    assert out == []


@pytest.mark.asyncio
async def test_feed_returns_entries_newest_first(redis_client):
    key = "office:office-1:agent_feed:dev"
    # LPUSH order: oldest first in list → newest at head.
    for i in range(3):
        await redis_client.lpush(key, json.dumps({
            "event_type": "progress",
            "content": f"step {i}",
        }))
    out = await _read_agent_feed(redis_client, "office-1", "dev", 30)
    # LPUSH means the LAST pushed is at index 0 → newest first.
    assert len(out) == 3
    assert out[0]["content"] == "step 2"
    assert out[2]["content"] == "step 0"


@pytest.mark.asyncio
async def test_feed_respects_limit(redis_client):
    key = "office:office-1:agent_feed:dev"
    for i in range(20):
        await redis_client.lpush(key, json.dumps({"step": i}))
    out = await _read_agent_feed(redis_client, "office-1", "dev", 5)
    assert len(out) == 5


@pytest.mark.asyncio
async def test_feed_drops_corrupt_entries(redis_client):
    key = "office:office-1:agent_feed:dev"
    await redis_client.lpush(key, "bad json {")
    await redis_client.lpush(key, json.dumps({"good": True}))
    out = await _read_agent_feed(redis_client, "office-1", "dev", 30)
    # Only the well-formed entry survives.
    assert len(out) == 1
    assert out[0]["good"] is True


@pytest.mark.asyncio
async def test_feed_missing_agent_name(redis_client):
    out = await _read_agent_feed(redis_client, "office-1", "", 30)
    assert out == []


# ─── dispatch_backend_request routing ──────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_routes_agent_queues(
    router, office, redis_client,
):
    # Seed one queue entry so the snapshot has something to return.
    await redis_client.zadd(
        "office:11111111-1111-1111-1111-111111111111:aq:dev:queue",
        {json.dumps({"task_id": "t1", "readable_id": "X.T01"}): 1.0},
    )
    await dispatch_backend_request(
        {
            "type": "request",
            "request_id": "req-1",
            "action": "agent_queues",
            "params": {},
        },
        router=router,
        fs_handler=MagicMock(),
        office=office,
        redis_client=redis_client,
        container_name="",
    )
    router.ws_client.send.assert_awaited_once()
    payload = router.ws_client.send.await_args.args[0]
    assert payload["type"] == "response"
    assert payload["request_id"] == "req-1"
    agents = payload["data"]["agents"]
    assert "dev" in agents
    assert agents["dev"]["pending"][0]["task_id"] == "t1"


@pytest.mark.asyncio
async def test_dispatch_routes_agent_feed(
    router, office, redis_client,
):
    key = "office:11111111-1111-1111-1111-111111111111:agent_feed:dev"
    await redis_client.lpush(key, json.dumps({"step": 1}))
    await dispatch_backend_request(
        {
            "type": "request",
            "request_id": "req-2",
            "action": "agent_feed",
            "params": {"agent_name": "dev", "limit": 10},
        },
        router=router,
        fs_handler=MagicMock(),
        office=office,
        redis_client=redis_client,
        container_name="",
    )
    payload = router.ws_client.send.await_args.args[0]
    assert payload["data"]["items"] == [{"step": 1}]
