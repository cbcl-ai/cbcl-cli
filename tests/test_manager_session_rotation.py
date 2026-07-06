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


# --- FX-24.T03 — typed rotation frame (replaces the inline prose note) -------


class _RecordingRouter:
    def __init__(self):
        self.events = []

    async def publish_event(self, ev):
        self.events.append(ev)


class _LiveController:
    """Controller double whose response is NOT pre-completed, so the publish
    path (is_final + the rotation chip) actually runs (unlike _FakeController,
    which sets _response_done to skip publishing)."""

    def __init__(self):
        self._sessions = _FakeSessions()
        self._router = _RecordingRouter()
        self._active_context_key = "workstream:w1"
        self._active_conversation_id = "c1"
        import asyncio

        self._response_done = asyncio.Event()  # deliberately NOT set
        self.states = []

    async def _publish_manager_state(self, context_key, state, message):
        # on_response_final pulses an "idle" state at the end of the turn;
        # record it via a separate channel so it doesn't interleave with the
        # router events the rotation assertions inspect.
        self.states.append((context_key, state, message))


@pytest.mark.asyncio
async def test_rotation_emits_typed_frame_after_final_not_inline():
    c = _LiveController()
    await _manager_events.on_response_final(c, {
        "context_key": "workstream:w1", "session_id": "sess-big",
        "rotate_session": True, "conversation_id": "c1", "token_cost": 0.1,
    })
    types = [e.get("type") for e in c._router.events]
    # The final marker is published, THEN the typed rotation frame (so the
    # chip orders after the finalized message bubble).
    assert types == ["manager_response", "manager_session_rotated"]
    assert c._router.events[0]["is_final"] is True
    assert c._router.events[1]["context_key"] == "workstream:w1"
    # Session was actually rotated (cleared, not saved).
    assert c._sessions.cleared == ["workstream:w1"]
    # No inline "grown large" prose leaked into any published content.
    blob = " ".join(str(e.get("content", "")) for e in c._router.events)
    assert "grown large" not in blob


@pytest.mark.asyncio
async def test_no_rotation_emits_no_rotated_frame():
    c = _LiveController()
    await _manager_events.on_response_final(c, {
        "context_key": "workstream:w1", "session_id": "sess-new",
        "conversation_id": "c1", "token_cost": 0.0,
    })
    types = [e.get("type") for e in c._router.events]
    assert "manager_session_rotated" not in types
    assert c._sessions.saved.get("workstream:w1") == "sess-new"
