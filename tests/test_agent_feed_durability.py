"""AREA-2 feed durability (incident 2026-07-16) — the sidebar feed must
survive legitimately-SILENT ultracode dynamic-workflow phases.

The per-agent Redis feed had a 300s sliding TTL refreshed ONLY on push,
while a healthy ultracode session tolerates 1200s of CLI silence
(``session_bridge._DEFAULT_INACTIVITY_SECONDS``) and a 2400s Planner
stall ceiling (``handlers._planner_heartbeat``) — so during a subagent
burst the entire LIST expired and the panel blanked. Pins:

* the TTL default now exceeds both stall ceilings, is env-tunable
  (``CUBICLE_AGENT_FEED_TTL_SECONDS``) and clamps garbage/low values;
* every push stamps the full TTL;
* a non-empty READ refreshes the TTL, so an actively-watched feed
  can't expire underneath the sidebar's poll;
* the planner heartbeat pushes a placeholder feed row per pulse
  (keepalive + "dynamic workflow running" visibility);
* cubicle-internal MCP tool calls emit a LEAN feed row (name only)
  instead of total suppression, so cubicle-tool-dominated phases
  (a Planner materialize/verify) still pulse the feed.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest

from src._handlers import _agent_feed as agent_feed_mod
from src._handlers._agent_feed import (
    _AGENT_FEED_TTL,
    _feed_ttl_from_env,
    push_agent_feed,
)
from src._handlers._requests import _read_agent_feed


@pytest.fixture
async def redis_client():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


# ─── TTL sizing ─────────────────────────────────────────────────────────


class TestFeedTtlSizing:

    def test_ttl_outlives_silent_workflow_windows(self):
        """The TTL must exceed BOTH silence ceilings a healthy session
        can legitimately hit: the 1200s CLI inactivity window and the
        2400s ultracode Planner stall ceiling. A shorter TTL re-opens
        the mid-workflow blanking this file exists to prevent."""
        assert _AGENT_FEED_TTL >= 2400, (
            "feed TTL must outlive the 2400s ultracode stall ceiling "
            "(handlers._planner_heartbeat) — see incident 2026-07-16"
        )
        assert _AGENT_FEED_TTL >= 1200, (
            "feed TTL must outlive the 1200s CLI inactivity window "
            "(session_bridge._DEFAULT_INACTIVITY_SECONDS)"
        )

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("CUBICLE_AGENT_FEED_TTL_SECONDS", "7200")
        assert _feed_ttl_from_env() == 7200

    def test_env_clamped_to_floor(self, monkeypatch):
        """A typo'd tiny value must not resurrect the blanking —
        clamped to the historical 300s floor."""
        monkeypatch.setenv("CUBICLE_AGENT_FEED_TTL_SECONDS", "5")
        assert _feed_ttl_from_env() == 300

    def test_env_garbage_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CUBICLE_AGENT_FEED_TTL_SECONDS", "an hour")
        assert _feed_ttl_from_env() == 3600

    def test_env_unset_default(self, monkeypatch):
        monkeypatch.delenv("CUBICLE_AGENT_FEED_TTL_SECONDS", raising=False)
        assert _feed_ttl_from_env() == 3600


# ─── Push stamps the full TTL ───────────────────────────────────────────


class TestPushTtl:

    @pytest.mark.asyncio
    async def test_push_sets_full_ttl(self, redis_client):
        await push_agent_feed(
            "planner",
            {"type": "progress", "event_type": "checkpoint", "content": "hi"},
            office_id="office-1",
            redis_client=redis_client,
        )
        ttl = await redis_client.ttl("office:office-1:agent_feed:planner")
        # Allow a second of slack for slow test hosts.
        assert ttl > _AGENT_FEED_TTL - 5


# ─── Read refreshes the sliding TTL ─────────────────────────────────────


class TestReadRefreshesTtl:

    @pytest.mark.asyncio
    async def test_nonempty_read_refreshes_ttl(self, redis_client):
        """The push path only bumps the TTL on new frames; with the
        parent stream silent, the sidebar's own poll must keep an
        actively-watched feed alive."""
        key = "office:office-1:agent_feed:planner"
        await redis_client.lpush(key, json.dumps({"content": "spawn"}))
        await redis_client.expire(key, 10)  # about to expire

        items = await _read_agent_feed(redis_client, "office-1", "planner", 30)

        assert len(items) == 1
        ttl = await redis_client.ttl(key)
        assert ttl > 10, "read must refresh the sliding TTL"
        assert ttl > agent_feed_mod._AGENT_FEED_TTL - 5

    @pytest.mark.asyncio
    async def test_empty_read_does_not_create_key(self, redis_client):
        key = "office:office-1:agent_feed:ghost"
        items = await _read_agent_feed(redis_client, "office-1", "ghost", 30)
        assert items == []
        assert await redis_client.exists(key) == 0


# ─── Planner heartbeat pushes a placeholder feed row ────────────────────


class TestHeartbeatFeedKeepalive:

    def test_heartbeat_pulse_pushes_planner_feed_row(self):
        """Source pin: the ``_planner_heartbeat`` pulse branch (the
        ``elapsed < stall_after or mode == "verify"`` arm) must push a
        placeholder feed row via ``_push_agent_feed("planner", ...)``.
        The heartbeat is a closure inside
        ``_register_process_model_handlers`` (NOT the same scope as the
        office-bound push helper — see the runtime test below for the
        wiring), so this pin only guards the row's presence + wording."""
        import inspect

        import src.handlers as handlers

        source = inspect.getsource(handlers)
        heartbeat_src = source.split("async def _planner_heartbeat", 1)[1]
        pulse_branch = heartbeat_src.split(
            "STALL detected", 1,
        )[0]
        assert '_push_agent_feed("planner"' in pulse_branch, (
            "the heartbeat pulse must push a feed keepalive row so the "
            "sidebar shows 'dynamic workflow running' instead of "
            "blanking (incident 2026-07-16)"
        )
        assert "dynamic workflow running" in pulse_branch

    @pytest.mark.asyncio
    async def test_heartbeat_pulse_actually_pushes_feed_row_at_runtime(self):
        """Regression (2026-07-17): ``_push_agent_feed`` is a closure in
        ``init_office_process_model`` while ``_planner_heartbeat`` lives in
        ``_register_process_model_handlers`` — an unwired reference
        NameError'd inside the heartbeat's swallow-all except, silently
        killing BOTH the keepalive row and the stall watchdog on the
        first pulse (the source pin above cannot see that). Drive one
        real pulse through the wired ``push_agent_feed_ref`` and assert
        the placeholder row reaches ``push_agent_feed``."""
        import asyncio

        from tests.test_review_circuit_breaker import build_harness

        real_sleep = asyncio.sleep

        async def _fast_sleep(_delay, *args, **kwargs):
            await real_sleep(0)

        with patch(
            "src._handlers._agent_feed.push_agent_feed",
            new_callable=AsyncMock,
        ) as feed:
            h = await build_harness()
            from src.handlers import _BACKGROUND_TASKS, _planner_consults

            h.mgr._publish_manager_state = AsyncMock()
            h.config_store.get_agent.return_value = {"name": "planner"}
            h.config_store.get_workstream.return_value = {}
            consult_handler = {
                c.args[0]: c.args[1] for c in h.router.on.call_args_list
            }["consult_planner"]

            # Deterministic busy sequencing (no scheduling race): idle
            # until spawn (so the consult's pre-spawn busy check passes),
            # then busy for exactly ONE heartbeat pulse, then idle (clean
            # heartbeat loop exit).
            state = {"spawned": False, "pulses_left": 1}

            async def _spawn(*args, **kwargs):
                state["spawned"] = True
                return True

            def _busy(name):
                if not state["spawned"]:
                    return False
                if state["pulses_left"] > 0:
                    state["pulses_left"] -= 1
                    return True
                return False

            h.supervisor.spawn_worker = AsyncMock(side_effect=_spawn)
            h.supervisor.is_agent_busy.side_effect = _busy

            try:
                with patch("asyncio.sleep", _fast_sleep):
                    await asyncio.wait_for(consult_handler({
                        "mode": "roadmap",
                        "objective": "map the work",
                        "workstream_id": "ws-1",
                    }), timeout=2.0)
                    for _ in range(200):
                        if not [
                            t for t in _BACKGROUND_TASKS if not t.done()
                        ]:
                            break
                        await real_sleep(0.01)
            finally:
                _planner_consults.clear()
                for t in list(_BACKGROUND_TASKS):
                    if not t.done():
                        t.cancel()
                await real_sleep(0)

        feed.assert_awaited()
        agent_name, event = feed.await_args.args[:2]
        assert agent_name == "planner"
        assert event["event_type"] == "checkpoint"
        assert "dynamic workflow running" in event["content"]
        # The office-bound kwargs came through the closure wiring.
        assert feed.await_args.kwargs["office_id"] == "office-1"


# ─── Lean feed rows for cubicle-internal MCP tools ──────────────────────


def _fake_worker() -> MagicMock:
    worker = MagicMock()
    worker.backend_url = "http://backend.test:8000"
    worker.office_id = "office-1"
    worker.agent_name = "planner"
    worker.workspace_path = "/tmp/cbcl-test-workspace"
    worker._send = MagicMock()
    worker._build_mcp_config = MagicMock(return_value={})
    return worker


def _failing_httpx_factory():
    """An ``httpx.AsyncClient`` whose ``post`` always raises (the brief
    is carried on the task_data, so the detail fetch is irrelevant)."""
    client = MagicMock()
    client.post = AsyncMock(side_effect=OSError("backend unreachable"))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


class TestLeanCubicleToolRows:

    @pytest.mark.asyncio
    async def test_cubicle_tool_emits_lean_row(self):
        """A ``mcp__cubicle*`` tool_use used to be dropped entirely —
        now it emits a LEAN tool_run row: bare tool name only, NO input
        payload, no tool_use_id (so no "end" enrichment later)."""
        from src._agent_worker_task import run_sdk_session
        from src.docker.session_bridge import SessionMessage

        worker = _fake_worker()

        async def _stream(*args, **kwargs):
            yield SessionMessage(
                type="assistant",
                data={"message": {"content": [{
                    "type": "tool_use",
                    "name": "mcp__cubicle-tools__update_execution_plan",
                    "id": "tu-1",
                    "input": {"plan": "SECRET-PLAN-CONTENT"},
                }]}},
            )
            yield SessionMessage(
                type="result",
                data={"session_id": "sess-1", "cost_usd": 0.01},
            )

        sb = __import__(
            "src.docker.session_bridge", fromlist=["stream_cli_session"],
        )
        task_data = {
            "task_id": "task-1",
            "readable_id": "WR-001.T01",
            "status": "ready",
            "brief": {"goal": "Do the thing"},
        }
        with patch("httpx.AsyncClient", _failing_httpx_factory()), \
                patch.object(sb, "stream_cli_session", _stream):
            await run_sdk_session(
                worker, agent_config={"model": "claude-opus-4-7"},
                task_data=task_data,
            )

        tool_frames = [
            call.args[0] for call in worker._send.call_args_list
            if call.args[0].get("event_type") == "tool_run"
        ]
        assert len(tool_frames) == 1
        frame = tool_frames[0]
        assert frame["content"] == "Using update_execution_plan"
        assert frame["details"]["tool"] == "update_execution_plan"
        # LEAN means no payload leaks into the feed.
        assert frame["details"]["summary"] == ""
        assert "SECRET-PLAN-CONTENT" not in json.dumps(frame)
        # No id → the UI never expects a matching "end" row.
        assert "tool_use_id" not in frame["details"]
        assert "running" not in frame["details"]

    @pytest.mark.asyncio
    async def test_cubicle_tool_result_emits_no_end_row(self):
        """The tool_result of a lean row is NOT buffered — no enriched
        end row (its output can carry board/brief content)."""
        from src._agent_worker_task import run_sdk_session
        from src.docker.session_bridge import SessionMessage

        worker = _fake_worker()

        async def _stream(*args, **kwargs):
            yield SessionMessage(
                type="assistant",
                data={"message": {"content": [{
                    "type": "tool_use",
                    "name": "mcp__cubicle-tools__get_board",
                    "id": "tu-2",
                    "input": {},
                }]}},
            )
            yield SessionMessage(
                type="user",
                data={"message": {"content": [{
                    "type": "tool_result",
                    "tool_use_id": "tu-2",
                    "content": "FULL-BOARD-DUMP",
                }]}},
            )
            yield SessionMessage(
                type="result",
                data={"session_id": "sess-2", "cost_usd": 0.01},
            )

        sb = __import__(
            "src.docker.session_bridge", fromlist=["stream_cli_session"],
        )
        task_data = {
            "task_id": "task-2",
            "readable_id": "WR-001.T02",
            "status": "ready",
            "brief": {"goal": "Do the thing"},
        }
        with patch("httpx.AsyncClient", _failing_httpx_factory()), \
                patch.object(sb, "stream_cli_session", _stream):
            await run_sdk_session(
                worker, agent_config={"model": "claude-opus-4-7"},
                task_data=task_data,
            )

        tool_frames = [
            call.args[0] for call in worker._send.call_args_list
            if call.args[0].get("event_type") == "tool_run"
        ]
        # Only the lean start row — no enriched end row.
        assert len(tool_frames) == 1
        assert "output_preview" not in tool_frames[0]["details"]
        assert "FULL-BOARD-DUMP" not in json.dumps(tool_frames[0])

    @pytest.mark.asyncio
    async def test_regular_tool_still_emits_full_rows(self):
        """Non-cubicle tools keep the rich start+end pair."""
        from src._agent_worker_task import run_sdk_session
        from src.docker.session_bridge import SessionMessage

        worker = _fake_worker()

        async def _stream(*args, **kwargs):
            yield SessionMessage(
                type="assistant",
                data={"message": {"content": [{
                    "type": "tool_use",
                    "name": "Bash",
                    "id": "tu-3",
                    "input": {"command": "ls -la"},
                }]}},
            )
            yield SessionMessage(
                type="user",
                data={"message": {"content": [{
                    "type": "tool_result",
                    "tool_use_id": "tu-3",
                    "content": "total 0",
                }]}},
            )
            yield SessionMessage(
                type="result",
                data={"session_id": "sess-3", "cost_usd": 0.01},
            )

        sb = __import__(
            "src.docker.session_bridge", fromlist=["stream_cli_session"],
        )
        task_data = {
            "task_id": "task-3",
            "readable_id": "WR-001.T03",
            "status": "ready",
            "brief": {"goal": "Do the thing"},
        }
        with patch("httpx.AsyncClient", _failing_httpx_factory()), \
                patch.object(sb, "stream_cli_session", _stream):
            await run_sdk_session(
                worker, agent_config={"model": "claude-opus-4-7"},
                task_data=task_data,
            )

        tool_frames = [
            call.args[0] for call in worker._send.call_args_list
            if call.args[0].get("event_type") == "tool_run"
        ]
        assert len(tool_frames) == 2  # running start + enriched end
        assert tool_frames[0]["details"].get("running") is True
        assert tool_frames[0]["details"]["tool_use_id"] == "tu-3"
        assert tool_frames[1]["details"]["output_preview"] == "total 0"
