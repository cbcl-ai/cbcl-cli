"""Flow Studio V2-P2 — daemon emission of the live ``agent_channel``
stream (``docs/specs/flow-studio/spec-v2-surfaces.md`` §4).

Contract under test:

* the emitter (``src/agent_channel.py``) maps a consult's worker
  PROGRESS frames onto ``agent_channel`` events — checkpoint text as
  coalesced ``chunk`` frames (≤10 publishes/s per channel), tool
  telemetry as ``tool_start``/``tool_end`` carrying the EXISTING
  ``build_tool_activity`` details shape + ``tool_use_id`` pairing +
  ``duration_ms``/``ok`` on end (Manager-feed parity);
* channel keys: ``flow-design:{flow_id}`` (kind ``flow_design``) /
  ``collections-curate`` (kind ``collections_curate``); a flow_id-less
  design marker is unaddressable and skipped;
* lifecycle: ``state: working`` at spawn, ``final`` + ``state: done``
  on clean completion, and ``state: failed`` on EVERY death path
  (pre-spawn refusals, error-classed/cancelled completions,
  worker-emitted errors, supervisor-synthesized fatals) so the FE
  typing indicator can never hang while the daemon lives;
* emission is best-effort — a publish failure never raises into the
  consult path — and the frames are an ephemeral overlay (the durable
  half stays ``flow_consult_*`` + design_log, untouched here).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.agent_channel import AgentChannelEmitter, channel_key
from src.handlers import _flow_consults
from tests.test_flow_consults import (
    ARCHITECT_MSG,
    _arm_spawn,
    _handler,
    _published,
)
from tests.test_review_circuit_breaker import build_harness


@pytest.fixture(autouse=True)
def _clean_module_state():
    _flow_consults.clear()
    yield
    _flow_consults.clear()


class _Collector:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def __call__(self, frame: dict) -> None:
        self.frames.append(frame)


ARCH_MARKER = {
    "request_id": "req-arch-1",
    "kind": "flow_design",
    "flow_id": "flow-uuid-1",
}
CUR_MARKER = {"request_id": "req-cur-1", "kind": "collections_curate"}


def _kinds(collector: _Collector) -> list[str]:
    return [f["kind"] for f in collector.frames]


# ---------------------------------------------------------------------------
# Channel keys
# ---------------------------------------------------------------------------


def test_channel_keys():
    assert channel_key("flow_design", "f1") == "flow-design:f1"
    assert channel_key("collections_curate") == "collections-curate"
    assert channel_key("collections_curate", "f1") == "collections-curate"
    # A design consult with no flow_id has no addressable surface.
    assert channel_key("flow_design", "") == ""
    assert channel_key("", "f1") == ""
    assert channel_key("unknown_kind", "f1") == ""


# ---------------------------------------------------------------------------
# Chunk coalescing (≤10 publishes/s per channel)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chunks_coalesce_within_the_rate_window():
    pub = _Collector()
    emitter = AgentChannelEmitter(pub)

    for text in ("one", "two", "three", "four", "five"):
        await emitter.relay_progress(
            ARCH_MARKER,
            {"event_type": "checkpoint", "content": text},
        )
    await emitter.drain()

    chunks = [f for f in pub.frames if f["kind"] == "chunk"]
    # First chunk publishes immediately (channel outside its window);
    # the four inside the window coalesce into ONE joined delta.
    assert len(chunks) == 2
    assert chunks[0]["text"] == "one"
    assert chunks[1]["text"] == "two\n\nthree\n\nfour\n\nfive"
    for frame in chunks:
        assert frame["type"] == "agent_channel"
        assert frame["channel"] == "flow-design:flow-uuid-1"
        assert frame["request_id"] == "req-arch-1"


@pytest.mark.asyncio
async def test_buffered_chunks_flush_on_the_timer_without_drain():
    pub = _Collector()
    emitter = AgentChannelEmitter(pub)
    await emitter.relay_progress(
        ARCH_MARKER, {"event_type": "checkpoint", "content": "a"},
    )
    await emitter.relay_progress(
        ARCH_MARKER, {"event_type": "checkpoint", "content": "b"},
    )
    assert len([f for f in pub.frames if f["kind"] == "chunk"]) == 1
    await asyncio.sleep(0.25)  # the window-edge timer fires
    chunks = [f for f in pub.frames if f["kind"] == "chunk"]
    assert len(chunks) == 2
    assert chunks[1]["text"] == "b"


@pytest.mark.asyncio
async def test_sidechain_checkpoints_are_not_transcript_chunks():
    pub = _Collector()
    emitter = AgentChannelEmitter(pub)
    await emitter.relay_progress(
        ARCH_MARKER,
        {
            "event_type": "checkpoint",
            "content": "subagent narration",
            "details": {"sidechain": True, "parent_tool_use_id": "tu-9"},
        },
    )
    await emitter.drain()
    assert pub.frames == []


@pytest.mark.asyncio
async def test_flow_id_less_design_marker_is_skipped():
    pub = _Collector()
    emitter = AgentChannelEmitter(pub)
    await emitter.relay_progress(
        {"request_id": "r1", "kind": "flow_design", "flow_id": ""},
        {"event_type": "checkpoint", "content": "text"},
    )
    await emitter.relay_failed(
        {"request_id": "r1", "kind": "flow_design"}, "boom",
    )
    await emitter.drain()
    assert pub.frames == []


# ---------------------------------------------------------------------------
# Tool telemetry — Manager-feed parity (build_tool_activity details)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_pair_maps_to_start_and_end_with_pairing_and_ok():
    pub = _Collector()
    emitter = AgentChannelEmitter(pub)
    start_details = {
        "tool": "Bash", "summary": "ls /workspace",
        "tool_use_id": "tu-1", "running": True,
    }
    end_details = {
        "tool": "Bash", "summary": "ls /workspace",
        "tool_use_id": "tu-1", "output_preview": "outputs\nscripts",
        "duration_ms": 340,
    }
    await emitter.relay_progress(
        CUR_MARKER,
        {"event_type": "tool_run", "content": "Bash: ls",
         "details": start_details},
    )
    await emitter.relay_progress(
        CUR_MARKER,
        {"event_type": "tool_run", "content": "Bash: ls",
         "details": end_details},
    )

    assert _kinds(pub) == ["tool_start", "tool_end"]
    start, end = pub.frames
    assert start["channel"] == "collections-curate"
    assert start["tool_use_id"] == "tu-1"
    assert start["details"] == start_details  # the existing shape, verbatim
    assert "ok" not in start and "duration_ms" not in start
    assert end["tool_use_id"] == "tu-1"
    assert end["details"] == end_details
    assert end["duration_ms"] == 340
    assert end["ok"] is True


@pytest.mark.asyncio
async def test_erroring_tool_end_carries_ok_false():
    pub = _Collector()
    emitter = AgentChannelEmitter(pub)
    await emitter.relay_progress(
        CUR_MARKER,
        {"event_type": "tool_run", "details": {
            "tool": "Bash", "summary": "false",
            "tool_use_id": "tu-2", "is_error": True,
        }},
    )
    (frame,) = pub.frames
    assert frame["kind"] == "tool_end"
    assert frame["ok"] is False


@pytest.mark.asyncio
async def test_lean_unpaired_tool_row_rides_as_a_start():
    pub = _Collector()
    emitter = AgentChannelEmitter(pub)
    # Cubicle-internal tools emit name-only rows: no tool_use_id, no
    # running flag, no output — a single unpaired start.
    await emitter.relay_progress(
        CUR_MARKER,
        {"event_type": "tool_run",
         "details": {"tool": "search_kb", "summary": ""}},
    )
    (frame,) = pub.frames
    assert frame["kind"] == "tool_start"
    assert frame["tool_use_id"] == ""


@pytest.mark.asyncio
async def test_buffered_chunk_flushes_before_a_tool_frame():
    pub = _Collector()
    emitter = AgentChannelEmitter(pub)
    await emitter.relay_progress(
        ARCH_MARKER, {"event_type": "checkpoint", "content": "first"},
    )
    await emitter.relay_progress(
        ARCH_MARKER, {"event_type": "checkpoint", "content": "buffered"},
    )
    await emitter.relay_progress(
        ARCH_MARKER,
        {"event_type": "tool_run",
         "details": {"tool": "Read", "summary": "spec.md",
                     "tool_use_id": "tu-3", "running": True}},
    )
    # Transcript order: chunk("first"), chunk("buffered"), tool_start.
    assert _kinds(pub) == ["chunk", "chunk", "tool_start"]
    assert pub.frames[1]["text"] == "buffered"


# ---------------------------------------------------------------------------
# Lifecycle frames
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_final_emits_text_then_state_done():
    pub = _Collector()
    emitter = AgentChannelEmitter(pub)
    await emitter.relay_progress(
        ARCH_MARKER, {"event_type": "checkpoint", "content": "working…"},
    )
    await emitter.relay_progress(
        ARCH_MARKER, {"event_type": "checkpoint", "content": "tail"},
    )
    await emitter.relay_final(ARCH_MARKER, "Extracted 2 collections.")

    assert _kinds(pub) == ["chunk", "chunk", "final", "state"]
    final, state = pub.frames[2], pub.frames[3]
    assert final["text"] == "Extracted 2 collections."
    assert state["state"] == "done"


@pytest.mark.asyncio
async def test_failed_emits_state_failed_with_message():
    pub = _Collector()
    emitter = AgentChannelEmitter(pub)
    await emitter.relay_failed(CUR_MARKER, "the session was killed (crash)")
    (frame,) = pub.frames
    assert frame == {
        "type": "agent_channel",
        "channel": "collections-curate",
        "request_id": "req-cur-1",
        "kind": "state",
        "state": "failed",
        "message": "the session was killed (crash)",
    }


@pytest.mark.asyncio
async def test_publish_failures_never_raise():
    async def _boom(_frame: dict) -> None:
        raise RuntimeError("transport down")

    emitter = AgentChannelEmitter(_boom)
    await emitter.relay_started(ARCH_MARKER, "started")
    await emitter.relay_progress(
        ARCH_MARKER, {"event_type": "checkpoint", "content": "x"},
    )
    await emitter.relay_final(ARCH_MARKER, "done")
    await emitter.relay_failed(ARCH_MARKER, "failed")
    await emitter.drain()  # nothing raised anywhere


# ---------------------------------------------------------------------------
# Handler wiring (build_harness — the real closures)
# ---------------------------------------------------------------------------


def _channel_frames(h, kind: str | None = None) -> list[dict]:
    frames = _published(h, "agent_channel")
    if kind is not None:
        frames = [f for f in frames if f.get("kind") == kind]
    return frames


@pytest.mark.asyncio
async def test_spawn_emits_state_working_on_the_flow_channel():
    h = await build_harness()
    _arm_spawn(h, "flow-architect")
    await _handler(h, "consult_flow_architect")(dict(ARCHITECT_MSG))

    states = _channel_frames(h, "state")
    assert states and states[-1]["state"] == "working"
    assert states[-1]["channel"] == "flow-design:flow-uuid-1"
    assert states[-1]["request_id"] == "req-arch-1"
    assert "Flow Architect session started" in states[-1]["message"]


@pytest.mark.asyncio
async def test_prespawn_refusal_emits_state_failed():
    h = await build_harness()
    _arm_spawn(h, "flow-architect")
    h.supervisor.is_agent_busy.return_value = True
    await _handler(h, "consult_flow_architect")(dict(ARCHITECT_MSG))

    states = _channel_frames(h, "state")
    assert states and states[-1]["state"] == "failed"
    assert "already running another consult" in states[-1]["message"]
    assert states[-1]["channel"] == "flow-design:flow-uuid-1"


@pytest.mark.asyncio
async def test_spawn_refusal_emits_state_failed():
    h = await build_harness()
    _arm_spawn(h, "flow-architect")
    h.supervisor.spawn_worker = AsyncMock(return_value=False)
    await _handler(h, "consult_flow_architect")(dict(ARCHITECT_MSG))

    states = _channel_frames(h, "state")
    assert states and states[-1]["state"] == "failed"
    assert "failed to start" in states[-1]["message"]


def _completion(task_id: str = "flow-consult-abc123", **over) -> dict:
    event = {
        "type": "task_complete",
        "task_id": task_id,
        "status": "consulting",
        "comment": "Flow consult complete.",
        "is_review_completion": True,
        "flow_consult": dict(ARCH_MARKER, role="architect", mode="extract"),
        "summary": "Extracted 2 collections.",
    }
    event.update(over)
    return event


@pytest.mark.asyncio
async def test_clean_completion_emits_final_then_done():
    h = await build_harness()
    await h.on_event("flow-architect", _completion())

    finals = _channel_frames(h, "final")
    assert finals and finals[0]["text"] == "Extracted 2 collections."
    assert finals[0]["channel"] == "flow-design:flow-uuid-1"
    states = _channel_frames(h, "state")
    assert states and states[-1]["state"] == "done"


@pytest.mark.asyncio
async def test_failed_completion_emits_state_failed():
    h = await build_harness()
    await h.on_event("flow-architect", _completion(
        status="blocked",
        comment="ESCALATED (timeout): the session timed out.",
        details={"error_class": "timeout"},
    ))

    assert not _channel_frames(h, "final")
    states = _channel_frames(h, "state")
    assert states and states[-1]["state"] == "failed"
    assert "ESCALATED (timeout)" in states[-1]["message"]


@pytest.mark.asyncio
async def test_cancelled_completion_emits_state_failed():
    h = await build_harness()
    await h.on_event("data-curator", _completion(
        task_id="flow-consult-cur1",
        flow_consult={"request_id": "req-cur-1",
                      "kind": "collections_curate", "flow_id": ""},
        status="blocked", comment="Task was cancelled.",
    ))
    states = _channel_frames(h, "state")
    assert states and states[-1]["state"] == "failed"
    assert states[-1]["channel"] == "collections-curate"


@pytest.mark.asyncio
async def test_synthesized_fatal_emits_state_failed_from_the_stash():
    """Supervisor-synthesized fatals carry no marker — the spawn-time
    stash recovers channel + request_id, so the death still flips the
    typing indicator (every death path emits state:failed)."""
    h = await build_harness()
    _flow_consults["flow-consult-dead1"] = dict(ARCH_MARKER)
    await h.on_event("flow-architect", {
        "type": "error",
        "task_id": "flow-consult-dead1",
        "fatal": True,
        "message": "",
        "reason": "heartbeat timeout",
    })
    states = _channel_frames(h, "state")
    assert states and states[-1]["state"] == "failed"
    assert states[-1]["channel"] == "flow-design:flow-uuid-1"
    assert "heartbeat timeout" in states[-1]["message"]


@pytest.mark.asyncio
async def test_markerless_curator_death_falls_back_to_the_curate_channel():
    """A stash-less, marker-less curator death still addresses the
    office-wide ``collections-curate`` channel via the agent-name
    fallback (the architect equivalent is unaddressable without a
    flow_id and stays poll-only)."""
    h = await build_harness()
    await h.on_event("data-curator", {
        "type": "error",
        "task_id": "flow-consult-gone",
        "fatal": True,
        "message": "process exited",
        "flow_consult": {"request_id": "req-cur-9"},
    })
    states = _channel_frames(h, "state")
    assert states and states[-1]["state"] == "failed"
    assert states[-1]["channel"] == "collections-curate"
    assert states[-1]["request_id"] == "req-cur-9"


@pytest.mark.asyncio
async def test_progress_chunks_ride_the_channel_unthrottled():
    """The 10s ``flow_consult_progress`` throttle does NOT gate the
    channel overlay — its own ≤10/s coalescer does."""
    h = await build_harness()
    _flow_consults["flow-consult-prog1"] = dict(ARCH_MARKER)
    event = {
        "type": "progress",
        "task_id": "flow-consult-prog1",
        "event_type": "checkpoint",
        "content": "Reading impressit_studio_draft.htm",
    }
    await h.on_event("flow-architect", event)
    await h.on_event("flow-architect", dict(event, content="Second block"))
    await asyncio.sleep(0.15)  # let the coalescer's window-edge flush run

    chunks = _channel_frames(h, "chunk")
    assert len(chunks) == 2
    assert chunks[0]["text"].startswith("Reading")
    assert chunks[1]["text"] == "Second block"
    # The durable poll pulse stays throttled to one.
    assert len(_published(h, "flow_consult_progress")) == 1
    # And the synthetic id still never rides task_activity.
    assert not _published(h, "task_activity")


@pytest.mark.asyncio
async def test_progress_tool_rows_ride_as_tool_frames():
    h = await build_harness()
    _flow_consults["flow-consult-tool1"] = dict(ARCH_MARKER)
    await h.on_event("flow-architect", {
        "type": "progress",
        "task_id": "flow-consult-tool1",
        "event_type": "tool_run",
        "content": "Bash: ls",
        "details": {"tool": "Bash", "summary": "ls",
                    "tool_use_id": "tu-7", "running": True},
    })
    starts = _channel_frames(h, "tool_start")
    assert starts and starts[0]["tool_use_id"] == "tu-7"
    assert starts[0]["details"]["tool"] == "Bash"


@pytest.mark.asyncio
async def test_kindless_stash_stays_silent_on_the_channel():
    """Pre-V2 stash shapes (request_id only) can't derive a channel —
    the overlay skips them; the poll relay is untouched."""
    h = await build_harness()
    _flow_consults["flow-consult-old1"] = {"request_id": "req-old-1"}
    await h.on_event("flow-architect", {
        "type": "progress",
        "task_id": "flow-consult-old1",
        "event_type": "checkpoint",
        "content": "text",
    })
    assert not _channel_frames(h)
    assert len(_published(h, "flow_consult_progress")) == 1
