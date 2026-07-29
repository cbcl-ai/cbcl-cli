"""Manager activity-feed parity — enriched tool telemetry (spec:
docs/specs/manager-fixes/00-research.md, "Manager activity-feed parity").

The Manager stream runner mirrors the worker feed enrichment
(``_agent_worker_task``): each complete ``tool_use`` block (from the full
``assistant`` frame, where the input is available) is buffered by block id
and emitted as a ``kind='tool_start'`` ACTIVITY frame carrying
``build_tool_activity`` details; the matching ``tool_result`` (a ``user``
frame) closes the pair with a ``kind='tool_end'`` frame carrying the
redacted output preview, ``duration_ms`` and ``ok``. The legacy name-only
pulse at ``content_block_start`` stays for the instant typing indicator,
now tagged ``kind='pulse'`` so the backend doesn't double-persist it.

Drives the module-level ``run_manager_session`` with ``stream_cli_session``
patched to yield scripted ``SessionMessage`` frames (same harness as
``test_manager_retry_loop``).
"""
from __future__ import annotations

import pytest

from src.docker import session_bridge
from src.docker.session_bridge import SessionMessage
from src._agent_worker_manager import run_manager_session

AGENT_CONFIG = {"_container_name": "cbcl-office-test", "model": "claude-opus-4-7"}


class _FakeWorker:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def _build_mcp_config(self, role, task_mode=None, context_key=None):
        return {}

    def _send(self, frame: dict) -> None:
        self.sent.append(frame)


def _result(sid: str = "sess-1", cost: float = 0.01) -> SessionMessage:
    return SessionMessage(
        type="result",
        data={"session_id": sid, "cost_usd": cost, "usage": {}},
    )


def _tool_block_start(name: str) -> SessionMessage:
    return SessionMessage(
        type="stream_event",
        data={"event": {"type": "content_block_start",
                        "content_block": {"type": "tool_use", "name": name}}},
    )


def _assistant_tool_use(
    tool_use_id: str, name: str, tool_input: dict,
) -> SessionMessage:
    return SessionMessage(
        type="assistant",
        data={"message": {"content": [
            {"type": "tool_use", "id": tool_use_id,
             "name": name, "input": tool_input},
        ]}},
    )


def _user_tool_result(
    tool_use_id: str, content: object, *, is_error: bool = False,
) -> SessionMessage:
    block: dict = {
        "type": "tool_result", "tool_use_id": tool_use_id, "content": content,
    }
    if is_error:
        block["is_error"] = True
    return SessionMessage(
        type="user", data={"message": {"content": [block]}},
    )


def _patch_stream(monkeypatch, seq: list[SessionMessage]) -> None:
    def factory(**kwargs):
        async def agen():
            for m in seq:
                yield m

        return agen()

    monkeypatch.setattr(session_bridge, "stream_cli_session", factory)


async def _run(worker: _FakeWorker):
    return await run_manager_session(
        worker,
        user_message="hi",
        system_prompt="sys",
        session_id=None,
        context_key="workstream:abc",
        conversation_id="conv-1",
        agent_config=AGENT_CONFIG,
    )


def _activity_frames(worker: _FakeWorker, kind: str) -> list[dict]:
    return [
        f for f in worker.sent
        if f.get("type") == "activity" and f.get("kind") == kind
    ]


@pytest.mark.asyncio
async def test_tool_use_emits_paired_tool_start_and_tool_end(monkeypatch):
    """A complete assistant tool_use + its user tool_result emit ONE
    tool_start and ONE tool_end frame sharing the tool_use_id, with the
    worker-parity ``build_tool_activity`` details."""
    worker = _FakeWorker()
    _patch_stream(monkeypatch, [
        _assistant_tool_use(
            "toolu_01", "mcp__cubicle-tools__get_board",
            {"workstream_id": "ws-1"},
        ),
        _user_tool_result("toolu_01", "3 tasks in ready"),
        _result(),
    ])

    await _run(worker)

    starts = _activity_frames(worker, "tool_start")
    ends = _activity_frames(worker, "tool_end")
    assert len(starts) == 1
    assert len(ends) == 1

    start = starts[0]
    assert start["tool"] == "get_board"  # MCP prefix stripped
    assert start["tool_use_id"] == "toolu_01"
    assert start["context_key"] == "workstream:abc"
    assert start["details"]["tool"] == "get_board"
    assert start["details"]["summary"] == "ws-1"
    assert start["details"]["running"] is True

    end = ends[0]
    assert end["tool"] == "get_board"
    assert end["tool_use_id"] == "toolu_01"
    assert end["ok"] is True
    assert isinstance(end["duration_ms"], int)
    assert end["duration_ms"] >= 0
    assert end["details"]["output_preview"] == "3 tasks in ready"
    assert "running" not in end["details"]


@pytest.mark.asyncio
async def test_tool_result_error_marks_end_not_ok(monkeypatch):
    worker = _FakeWorker()
    _patch_stream(monkeypatch, [
        _assistant_tool_use("toolu_02", "WebFetch", {"url": "https://x.test"}),
        _user_tool_result("toolu_02", "connection refused", is_error=True),
        _result(),
    ])

    await _run(worker)

    ends = _activity_frames(worker, "tool_end")
    assert len(ends) == 1
    assert ends[0]["ok"] is False
    assert ends[0]["details"]["is_error"] is True


@pytest.mark.asyncio
async def test_content_block_start_pulse_is_tagged_pulse(monkeypatch):
    """The instant typing-indicator ping keeps firing at
    content_block_start, now tagged kind='pulse' so the backend skips
    persisting it (the tool_start row carries the feed)."""
    worker = _FakeWorker()
    _patch_stream(monkeypatch, [
        _tool_block_start("mcp__cubicle-tools__get_board"),
        _assistant_tool_use("toolu_03", "mcp__cubicle-tools__get_board", {}),
        _user_tool_result("toolu_03", "ok"),
        _result(),
    ])

    await _run(worker)

    pulses = _activity_frames(worker, "pulse")
    assert len(pulses) == 1
    assert pulses[0]["tool"] == "get_board"
    # Exactly one start + one end despite the pulse also firing.
    assert len(_activity_frames(worker, "tool_start")) == 1
    assert len(_activity_frames(worker, "tool_end")) == 1


@pytest.mark.asyncio
async def test_unmatched_tool_result_is_ignored(monkeypatch):
    """A tool_result with no buffered tool_use (e.g. resumed-session
    replay noise) emits nothing rather than crashing the stream."""
    worker = _FakeWorker()
    _patch_stream(monkeypatch, [
        _user_tool_result("toolu_unknown", "stray"),
        _result(),
    ])

    await _run(worker)

    assert _activity_frames(worker, "tool_end") == []


@pytest.mark.asyncio
async def test_unmatched_tool_start_survives_as_running_record(monkeypatch):
    """A tool_use whose result never arrives (stream died mid-tool) still
    leaves its tool_start frame — the record of what was invoked."""
    worker = _FakeWorker()
    _patch_stream(monkeypatch, [
        _assistant_tool_use("toolu_04", "Read", {"file_path": "/workspace/a.md"}),
        _result(),
    ])

    await _run(worker)

    starts = _activity_frames(worker, "tool_start")
    assert len(starts) == 1
    assert starts[0]["details"]["summary"] == "/workspace/a.md"
    assert _activity_frames(worker, "tool_end") == []
