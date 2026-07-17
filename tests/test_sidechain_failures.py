"""FIX U1 (blink-resilience) — ultracode subagent (sidechain) failures
must not vanish.

Contract under test (``_agent_worker_task.run_sdk_session`` +
``handle_assign_task``):

* a native spawn-tool (``Agent``/``Task``) ``tool_result`` with
  ``is_error`` emits a lean "workflow subagent failed" error row and
  increments the per-session counter — INCLUDING after the terminal-tool
  output lock, which used to drop the frame wholesale;
* a sidechain assistant frame (``parent_tool_use_id`` set) whose text is
  an "API Error" emits the same failure row (a 529/limit inside a
  subagent never becomes a parent-stream ``error`` frame) without a
  duplicate checkpoint;
* the count rides the TASK_COMPLETE payload (``sidechain_failures``) so
  outcome gates / reviewers can weigh "clean exit with N dead phases";
* no retry semantics change — the session still returns success on a
  clean parent exit.

AREA-1 fix 3 (verify turn-end incident 2026-07-17) rides the same
plumbing: a spawn tool_use whose ``tool_result`` NEVER arrives by clean
stream end means the model ended its turn with a workflow still running
(under ``--print`` the process exit kills it) — counted into
``worker._pending_spawns`` and attached to TASK_COMPLETE as
``pending_spawns`` so the planner honesty check can name the trap.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src._agent_worker_task import handle_assign_task, run_sdk_session
from src.docker import session_bridge
from src.docker.session_bridge import SessionMessage


AGENT_CONFIG = {"_container_name": "cbcl-office-test",
                "model": "claude-opus-4-7"}


def _fake_worker() -> MagicMock:
    worker = MagicMock()
    worker.backend_url = ""  # skip the get_task_detail fetch
    worker.office_id = "office-1"
    worker.agent_name = "analyst"
    worker.workspace_path = "/tmp/cbcl-test-workspace"
    worker._send = MagicMock()
    worker._build_mcp_config = MagicMock(return_value={})
    return worker


def _task_data() -> dict:
    return {
        "task_id": "task-1",
        "readable_id": "WR-001.T01",
        "status": "ready",
        "brief": {"goal": "Ship the thing"},
        "agent_config": {},
    }


def _spawn_use(block_id: str = "spawn-1",
               name: str = "Agent") -> SessionMessage:
    return SessionMessage(type="assistant", data={"message": {"content": [
        {"type": "tool_use", "name": name, "id": block_id, "input": {}},
    ]}})


def _spawn_result(block_id: str = "spawn-1", *, is_error: bool,
                  content: str = "phase died") -> SessionMessage:
    return SessionMessage(type="user", data={"message": {"content": [
        {"type": "tool_result", "tool_use_id": block_id,
         "is_error": is_error, "content": content},
    ]}})


def _terminal_tool_use() -> SessionMessage:
    # update_status arms the output lock via the pre-scan.
    return SessionMessage(type="assistant", data={"message": {"content": [
        {"type": "tool_use", "name": "mcp__cubicle-tools__update_status",
         "id": "term-1", "input": {}},
    ]}})


def _sidechain_api_error() -> SessionMessage:
    return SessionMessage(type="assistant", data={
        "parent_tool_use_id": "spawn-1",
        "message": {"content": [
            {"type": "text",
             "text": "API Error: 529 Overloaded (subagent)"},
        ]},
    })


def _result(sid: str = "sess-1") -> SessionMessage:
    return SessionMessage(
        type="result", data={"session_id": sid, "cost_usd": 0.01},
    )


def _patch_stream(monkeypatch, seq: list[SessionMessage]) -> None:
    def factory(**kwargs):
        async def agen():
            for m in seq:
                yield m

        return agen()

    monkeypatch.setattr(session_bridge, "stream_cli_session", factory)


def _failure_rows(worker) -> list[dict]:
    return [
        f for f in (c.args[0] for c in worker._send.call_args_list)
        if f.get("event_type") == "error"
        and "workflow subagent failed" in (f.get("content") or "")
    ]


@pytest.mark.asyncio
async def test_spawn_tool_error_result_emits_failure_row(monkeypatch):
    worker = _fake_worker()
    _patch_stream(monkeypatch, [
        _spawn_use(),
        _spawn_result(is_error=True, content="subagent hit a 529"),
        _result(),
    ])
    sid, _cost = await run_sdk_session(worker, AGENT_CONFIG, _task_data())
    assert sid == "sess-1"  # clean parent exit is still a success
    rows = _failure_rows(worker)
    assert len(rows) == 1
    assert "subagent hit a 529" in rows[0]["content"]
    assert rows[0]["details"]["sidechain"] is True
    assert rows[0]["details"]["tool"] == "Agent"
    # No error_class — this is visibility, not session-terminal telemetry
    # (an off-enum class would violate the backend task_errors CHECK).
    assert "error_class" not in rows[0]["details"]
    assert worker._sidechain_failures == 1


@pytest.mark.asyncio
async def test_clean_spawn_result_is_not_counted(monkeypatch):
    worker = _fake_worker()
    _patch_stream(monkeypatch, [
        _spawn_use(),
        _spawn_result(is_error=False, content="phase ok"),
        _result(),
    ])
    await run_sdk_session(worker, AGENT_CONFIG, _task_data())
    assert not _failure_rows(worker)
    assert worker._sidechain_failures == 0


@pytest.mark.asyncio
async def test_spawn_failure_after_output_lock_still_surfaces(monkeypatch):
    """The terminal-tool output lock skips whole frames — the spawn-tool
    failure scan runs BEFORE the skip so a late phase death still leaves
    a record."""
    worker = _fake_worker()
    _patch_stream(monkeypatch, [
        _spawn_use(),
        _terminal_tool_use(),  # locks output
        _spawn_result(is_error=True, content="late phase death"),
        _result(),
    ])
    await run_sdk_session(worker, AGENT_CONFIG, _task_data())
    rows = _failure_rows(worker)
    assert len(rows) == 1
    assert "late phase death" in rows[0]["content"]


@pytest.mark.asyncio
async def test_sidechain_api_error_text_emits_row_without_dup_checkpoint(
    monkeypatch,
):
    worker = _fake_worker()
    _patch_stream(monkeypatch, [
        _spawn_use(),
        _sidechain_api_error(),
        _result(),
    ])
    await run_sdk_session(worker, AGENT_CONFIG, _task_data())
    rows = _failure_rows(worker)
    assert len(rows) == 1
    assert "API Error: 529" in rows[0]["content"]
    assert worker._sidechain_failures == 1
    # The same text must NOT also land as a checkpoint (duplicate row).
    checkpoints = [
        f for f in (c.args[0] for c in worker._send.call_args_list)
        if f.get("event_type") == "checkpoint"
        and "API Error" in (f.get("content") or "")
    ]
    assert not checkpoints


@pytest.mark.asyncio
async def test_count_rides_task_complete_payload(monkeypatch):
    """``handle_assign_task`` attaches the session's sidechain-failure
    count to the TASK_COMPLETE payload."""
    worker = _fake_worker()

    async def _fake_run(agent_config, task_data):
        worker._sidechain_failures = 2
        return "sess-1", 0.05

    worker._run_sdk_session = AsyncMock(side_effect=_fake_run)
    await handle_assign_task(worker, {
        "task_id": "task-1",
        "readable_id": "WR-001.T01",
        "status": "ready",
        "agent_config": {},
    })
    completes = [
        f for f in (c.args[0] for c in worker._send.call_args_list)
        if f.get("type") == "task_complete"
        or getattr(f.get("type"), "value", "") == "task_complete"
    ]
    assert completes, "no task_complete emitted"
    assert completes[-1]["sidechain_failures"] == 2


# ---------------------------------------------------------------------------
# AREA-1 fix 3 — pending spawns at clean stream end (turn-end trap marker)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unresolved_spawn_at_clean_end_counts_pending(monkeypatch):
    """A spawn tool_use with NO tool_result by clean stream end = the
    model ended its turn on a live workflow — counted, logged loudly."""
    worker = _fake_worker()
    _patch_stream(monkeypatch, [
        _spawn_use("spawn-1"),
        _spawn_use("spawn-2", name="Task"),
        _result(),  # clean end — neither spawn ever acked
    ])
    sid, _cost = await run_sdk_session(worker, AGENT_CONFIG, _task_data())
    assert sid == "sess-1"  # still a clean success — enrichment, not a gate
    assert worker._pending_spawns == 2


@pytest.mark.asyncio
async def test_acked_spawn_is_not_pending(monkeypatch):
    """A spawn whose tool_result arrived (even an error one) is resolved
    — only never-acked spawns count as pending."""
    worker = _fake_worker()
    _patch_stream(monkeypatch, [
        _spawn_use("spawn-1"),
        _spawn_result("spawn-1", is_error=True, content="phase died"),
        _result(),
    ])
    await run_sdk_session(worker, AGENT_CONFIG, _task_data())
    assert worker._pending_spawns == 0
    assert worker._sidechain_failures == 1  # still counted as a failure


@pytest.mark.asyncio
async def test_no_spawns_leaves_pending_zero(monkeypatch):
    worker = _fake_worker()
    _patch_stream(monkeypatch, [_result()])
    await run_sdk_session(worker, AGENT_CONFIG, _task_data())
    assert worker._pending_spawns == 0


@pytest.mark.asyncio
async def test_pending_spawns_rides_task_complete_payload(monkeypatch):
    """The count reaches the TASK_COMPLETE payload (``pending_spawns``)
    alongside ``sidechain_failures`` — including the planner branch the
    honesty check reads."""
    worker = _fake_worker()

    async def _fake_run(agent_config, task_data):
        worker._pending_spawns = 3
        worker._sidechain_failures = 0
        return "sess-1", 0.05

    worker._run_sdk_session = AsyncMock(side_effect=_fake_run)
    await handle_assign_task(worker, {
        "task_id": "planner-abc123",
        "readable_id": "PLAN",
        "status": "planning",
        "agent_config": {},
        "planner_consult": {"mode": "verify", "scope_id": "scope-1"},
    })
    completes = [
        f for f in (c.args[0] for c in worker._send.call_args_list)
        if f.get("type") == "task_complete"
        or getattr(f.get("type"), "value", "") == "task_complete"
    ]
    assert completes, "no task_complete emitted"
    assert completes[-1]["pending_spawns"] == 3
    assert completes[-1]["planner_consult"]["mode"] == "verify"
