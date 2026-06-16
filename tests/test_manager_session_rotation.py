"""T4.3.4 — proactive Manager session rotation at the turn boundary."""
from __future__ import annotations

import pytest

from src.orchestrator import _manager_events


class _FakeSessions:
    def __init__(self):
        self.saved = {}
        self.cleared = []

    async def save_session(self, ctx, sid):
        self.saved[ctx] = sid

    async def clear_session(self, ctx):
        self.cleared.append(ctx)
        self.saved.pop(ctx, None)


class _FakeRouter:
    async def publish_event(self, *a, **k):
        pass


class _FakeController:
    def __init__(self):
        self._sessions = _FakeSessions()
        self._router = _FakeRouter()
        self._active_context_key = "workstream:w1"
        self._active_conversation_id = "c1"
        import asyncio
        self._response_done = asyncio.Event()
        self._response_done.set()  # skip the publish path


@pytest.mark.asyncio
async def test_rotate_flag_clears_session_not_saves():
    c = _FakeController()
    await _manager_events.on_response_final(c, {
        "context_key": "workstream:w1", "session_id": "sess-big",
        "rotate_session": True, "conversation_id": "c1",
    })
    assert c._sessions.cleared == ["workstream:w1"]
    assert "workstream:w1" not in c._sessions.saved


@pytest.mark.asyncio
async def test_no_rotate_saves_session_normally():
    c = _FakeController()
    await _manager_events.on_response_final(c, {
        "context_key": "workstream:w1", "session_id": "sess-new",
        "conversation_id": "c1",
    })
    assert c._sessions.saved.get("workstream:w1") == "sess-new"
    assert c._sessions.cleared == []
