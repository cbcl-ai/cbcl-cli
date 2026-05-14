"""Tests for Redis-backed SessionManager.

Tests both Redis mode and file fallback mode. Uses fakeredis for Redis
and tmp_path for file operations.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio

import fakeredis.aioredis

from src.orchestrator.session_manager import SessionManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_redis():
    """Provide a fakeredis async client."""
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
def office_id() -> str:
    return "test-office-001"


@pytest_asyncio.fixture
async def redis_session_mgr(fake_redis, office_id) -> SessionManager:
    """SessionManager with Redis backend."""
    mgr = SessionManager(
        workspace_path=None,
        redis=fake_redis,
        office_id=office_id,
    )
    await mgr.init()
    return mgr


@pytest_asyncio.fixture
async def file_session_mgr(tmp_path) -> SessionManager:
    """SessionManager with file fallback (no Redis)."""
    workspace = str(tmp_path / "workspace")
    mgr = SessionManager(
        workspace_path=workspace,
        redis=None,
        office_id="",
    )
    await mgr.init()
    return mgr


# ---------------------------------------------------------------------------
# Redis mode: basic operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_and_get_session_redis(redis_session_mgr):
    """save_session + get_session_id round-trips through Redis."""
    mgr = redis_session_mgr
    await mgr.save_session("general_chat", "session-abc-123")

    result = mgr.get_session_id("general_chat")
    assert result == "session-abc-123"


@pytest.mark.asyncio
async def test_get_nonexistent_session_redis(redis_session_mgr):
    """get_session_id returns None for unknown context."""
    assert redis_session_mgr.get_session_id("unknown") is None


@pytest.mark.asyncio
async def test_save_multiple_sessions_redis(redis_session_mgr):
    """Multiple sessions can coexist."""
    mgr = redis_session_mgr
    await mgr.save_session("general_chat", "gc-sess-1")
    await mgr.save_session("workstream:ws-1", "ws-sess-1")
    await mgr.save_session("workstream:ws-2", "ws-sess-2")

    assert mgr.get_session_id("general_chat") == "gc-sess-1"
    assert mgr.get_session_id("workstream:ws-1") == "ws-sess-1"
    assert mgr.get_session_id("workstream:ws-2") == "ws-sess-2"


@pytest.mark.asyncio
async def test_save_overwrites_session_redis(redis_session_mgr):
    """Saving a new session_id for the same context overwrites the old one."""
    mgr = redis_session_mgr
    await mgr.save_session("general_chat", "old-session")
    await mgr.save_session("general_chat", "new-session")

    assert mgr.get_session_id("general_chat") == "new-session"


@pytest.mark.asyncio
async def test_clear_session_redis(redis_session_mgr, fake_redis, office_id):
    """clear_session removes from both cache and Redis."""
    mgr = redis_session_mgr
    await mgr.save_session("general_chat", "session-1")
    await mgr.clear_session("general_chat")

    # In-memory cache cleared.
    assert mgr.get_session_id("general_chat") is None

    # Redis hash cleared.
    redis_key = f"office:{office_id}:sessions"
    value = await fake_redis.hget(redis_key, "general_chat")
    assert value is None


@pytest.mark.asyncio
async def test_clear_nonexistent_session_redis(redis_session_mgr):
    """Clearing a nonexistent session is a no-op."""
    await redis_session_mgr.clear_session("nonexistent")
    # Should not raise.


# ---------------------------------------------------------------------------
# Redis mode: switch_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_context_returns_existing_session(redis_session_mgr):
    """switch_context returns session_id if one exists."""
    mgr = redis_session_mgr
    await mgr.save_session("workstream:ws-1", "session-xyz")

    result = mgr.switch_context("workstream:ws-1")
    assert result == "session-xyz"
    assert mgr.active_context == "workstream:ws-1"


@pytest.mark.asyncio
async def test_switch_context_returns_none_for_new(redis_session_mgr):
    """switch_context returns None for a context with no saved session."""
    result = redis_session_mgr.switch_context("workstream:new")
    assert result is None
    assert redis_session_mgr.active_context == "workstream:new"


@pytest.mark.asyncio
async def test_switch_context_updates_active(redis_session_mgr):
    """switch_context changes the active context."""
    mgr = redis_session_mgr
    mgr.switch_context("general_chat")
    assert mgr.active_context == "general_chat"

    mgr.switch_context("workstream:ws-1")
    assert mgr.active_context == "workstream:ws-1"


# ---------------------------------------------------------------------------
# Redis mode: persistence verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_persists_to_redis(fake_redis, office_id):
    """Sessions saved via one manager instance are visible to another."""
    mgr1 = SessionManager(redis=fake_redis, office_id=office_id)
    await mgr1.init()
    await mgr1.save_session("general_chat", "persistent-session")

    # Create a new manager instance reading from the same Redis.
    mgr2 = SessionManager(redis=fake_redis, office_id=office_id)
    await mgr2.init()

    assert mgr2.get_session_id("general_chat") == "persistent-session"


@pytest.mark.asyncio
async def test_manager_sessions_property(redis_session_mgr):
    """manager_sessions returns a copy of all sessions."""
    mgr = redis_session_mgr
    await mgr.save_session("general_chat", "gc-1")
    await mgr.save_session("workstream:ws-1", "ws-1")

    sessions = mgr.manager_sessions
    assert sessions == {
        "general_chat": "gc-1",
        "workstream:ws-1": "ws-1",
    }

    # Modifying the returned dict does not affect the manager.
    sessions["extra"] = "value"
    assert "extra" not in mgr.manager_sessions


# ---------------------------------------------------------------------------
# File fallback mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_and_get_session_file(file_session_mgr):
    """File fallback: save and retrieve sessions."""
    mgr = file_session_mgr
    await mgr.save_session("general_chat", "file-session-1")

    assert mgr.get_session_id("general_chat") == "file-session-1"


@pytest.mark.asyncio
async def test_clear_session_file(file_session_mgr):
    """File fallback: clear a session."""
    mgr = file_session_mgr
    await mgr.save_session("general_chat", "to-clear")
    await mgr.clear_session("general_chat")

    assert mgr.get_session_id("general_chat") is None


@pytest.mark.asyncio
async def test_file_persistence(tmp_path):
    """File fallback: sessions persist across manager instances."""
    workspace = str(tmp_path / "workspace")

    mgr1 = SessionManager(workspace_path=workspace, redis=None, office_id="")
    await mgr1.init()
    await mgr1.save_session("general_chat", "persisted-session")

    mgr2 = SessionManager(workspace_path=workspace, redis=None, office_id="")
    await mgr2.init()

    assert mgr2.get_session_id("general_chat") == "persisted-session"


# ---------------------------------------------------------------------------
# Migration: file -> Redis
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_file_to_redis(fake_redis, tmp_path):
    """Existing file sessions are migrated to Redis on init."""
    workspace = str(tmp_path / "workspace")
    sessions_dir = Path(workspace) / ".cubicle"
    sessions_dir.mkdir(parents=True)
    sessions_file = sessions_dir / "sessions.json"

    # Create a legacy sessions file.
    sessions_file.write_text(json.dumps({
        "manager_sessions": {
            "general_chat": "legacy-gc-session",
            "workstream:ws-old": "legacy-ws-session",
        },
    }))

    office_id = "migration-test"
    mgr = SessionManager(
        workspace_path=workspace,
        redis=fake_redis,
        office_id=office_id,
    )
    await mgr.init()

    # Sessions should be available.
    assert mgr.get_session_id("general_chat") == "legacy-gc-session"
    assert mgr.get_session_id("workstream:ws-old") == "legacy-ws-session"

    # Sessions should be in Redis.
    redis_key = f"office:{office_id}:sessions"
    gc_val = await fake_redis.hget(redis_key, "general_chat")
    assert gc_val == "legacy-gc-session"

    # Sessions file should be deleted after migration.
    assert not sessions_file.exists()


@pytest.mark.asyncio
async def test_migration_redis_takes_precedence(fake_redis, tmp_path):
    """If a session exists in both Redis and file, Redis wins."""
    workspace = str(tmp_path / "workspace")
    sessions_dir = Path(workspace) / ".cubicle"
    sessions_dir.mkdir(parents=True)
    sessions_file = sessions_dir / "sessions.json"

    sessions_file.write_text(json.dumps({
        "manager_sessions": {
            "general_chat": "file-session",
        },
    }))

    office_id = "precedence-test"
    redis_key = f"office:{office_id}:sessions"
    await fake_redis.hset(redis_key, "general_chat", "redis-session")

    mgr = SessionManager(
        workspace_path=workspace,
        redis=fake_redis,
        office_id=office_id,
    )
    await mgr.init()

    # Redis session should take precedence.
    assert mgr.get_session_id("general_chat") == "redis-session"


@pytest.mark.asyncio
async def test_no_migration_without_file(fake_redis):
    """If no sessions file exists, migration is a no-op."""
    mgr = SessionManager(
        workspace_path="/nonexistent/path",
        redis=fake_redis,
        office_id="no-file-test",
    )
    await mgr.init()

    # Should have no sessions.
    assert mgr.manager_sessions == {}


# ---------------------------------------------------------------------------
# init_from_disk backward compat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_from_disk_alias(fake_redis):
    """init_from_disk() is an alias for init()."""
    mgr = SessionManager(redis=fake_redis, office_id="alias-test")
    await mgr.init_from_disk()  # Should not raise.
    assert mgr.manager_sessions == {}


# ---------------------------------------------------------------------------
# Additional coverage: remove_session, Redis failure fallback, corruption
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_session_clears_cache(redis_session_mgr):
    """remove_session removes from in-memory cache but NOT Redis."""
    mgr = redis_session_mgr
    await mgr.save_session("general_chat", "to-remove")

    mgr.remove_session("general_chat")
    assert mgr.get_session_id("general_chat") is None


@pytest.mark.asyncio
async def test_remove_session_does_not_clean_redis(
    redis_session_mgr, fake_redis, office_id,
):
    """remove_session does NOT delete from Redis (use clear_session for that)."""
    mgr = redis_session_mgr
    await mgr.save_session("general_chat", "still-in-redis")

    mgr.remove_session("general_chat")

    # Should still exist in Redis (not cleaned up).
    redis_key = f"office:{office_id}:sessions"
    value = await fake_redis.hget(redis_key, "general_chat")
    assert value == "still-in-redis"


@pytest.mark.asyncio
async def test_remove_nonexistent_session_is_noop(redis_session_mgr):
    """Removing a nonexistent session does not raise."""
    redis_session_mgr.remove_session("does-not-exist")
    # Should not raise.


@pytest.mark.asyncio
async def test_save_session_redis_failure_falls_back_to_file(tmp_path):
    """If Redis write fails, save_session falls back to file."""
    workspace = str(tmp_path / "workspace")

    # Create a mock redis that raises on hset.
    import fakeredis.aioredis
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)

    mgr = SessionManager(
        workspace_path=workspace,
        redis=r,
        office_id="fallback-test",
    )
    await mgr.init()

    # Patch hset to raise an error.
    original_hset = r.hset

    async def failing_hset(*args, **kwargs):
        raise ConnectionError("Redis down")

    r.hset = failing_hset

    await mgr.save_session("general_chat", "fallback-session")

    # Should be in memory.
    assert mgr.get_session_id("general_chat") == "fallback-session"

    # Should be in file.
    sessions_file = Path(workspace) / ".cubicle" / "sessions.json"
    assert sessions_file.exists()
    data = json.loads(sessions_file.read_text())
    assert data["manager_sessions"]["general_chat"] == "fallback-session"

    # Restore and cleanup.
    r.hset = original_hset
    await r.aclose()


@pytest.mark.asyncio
async def test_corrupted_sessions_file_handled_gracefully(tmp_path):
    """A corrupted sessions.json file is handled without raising."""
    workspace = str(tmp_path / "workspace")
    sessions_dir = Path(workspace) / ".cubicle"
    sessions_dir.mkdir(parents=True)
    sessions_file = sessions_dir / "sessions.json"
    sessions_file.write_text("not valid json {{{")

    mgr = SessionManager(workspace_path=workspace, redis=None, office_id="")
    await mgr.init()

    # Should start with empty sessions (corrupted file ignored).
    assert mgr.manager_sessions == {}


@pytest.mark.asyncio
async def test_corrupted_file_during_migration_handled(fake_redis, tmp_path):
    """A corrupted sessions file during migration is handled gracefully."""
    workspace = str(tmp_path / "workspace")
    sessions_dir = Path(workspace) / ".cubicle"
    sessions_dir.mkdir(parents=True)
    sessions_file = sessions_dir / "sessions.json"
    sessions_file.write_text("{bad json")

    mgr = SessionManager(
        workspace_path=workspace,
        redis=fake_redis,
        office_id="corrupt-migrate",
    )
    await mgr.init()

    # Should not crash. Sessions should be empty.
    assert mgr.manager_sessions == {}


@pytest.mark.asyncio
async def test_clear_session_file_updates_disk(tmp_path):
    """File fallback: clear_session persists the removal to disk."""
    workspace = str(tmp_path / "workspace")

    mgr = SessionManager(workspace_path=workspace, redis=None, office_id="")
    await mgr.init()
    await mgr.save_session("general_chat", "to-clear")
    await mgr.save_session("workstream:ws-1", "keep-this")
    await mgr.clear_session("general_chat")

    # Verify on disk: general_chat should be gone, ws-1 should remain.
    sessions_file = Path(workspace) / ".cubicle" / "sessions.json"
    data = json.loads(sessions_file.read_text())
    assert "general_chat" not in data["manager_sessions"]
    assert data["manager_sessions"]["workstream:ws-1"] == "keep-this"


@pytest.mark.asyncio
async def test_init_redis_failure_falls_back_to_file(tmp_path):
    """If Redis fails during init(), sessions load from file."""
    workspace = str(tmp_path / "workspace")
    sessions_dir = Path(workspace) / ".cubicle"
    sessions_dir.mkdir(parents=True)
    sessions_file = sessions_dir / "sessions.json"
    sessions_file.write_text(json.dumps({
        "manager_sessions": {"general_chat": "file-session"},
    }))

    # Create a mock redis that raises on hgetall.
    import fakeredis.aioredis
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    original_hgetall = r.hgetall

    async def failing_hgetall(*args, **kwargs):
        raise ConnectionError("Redis down")

    r.hgetall = failing_hgetall

    mgr = SessionManager(
        workspace_path=workspace,
        redis=r,
        office_id="redis-fail-init",
    )
    await mgr.init()

    # Should fall back to file.
    assert mgr.get_session_id("general_chat") == "file-session"

    # Restore and cleanup.
    r.hgetall = original_hgetall
    await r.aclose()
