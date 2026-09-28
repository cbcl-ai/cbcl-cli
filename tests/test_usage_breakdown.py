"""F07 — session logs split uncached input, cache writes and cache reads.

A prompt-size change is only measurable when the logs show the three input
kinds separately (the Manager's rotation threshold counts all three). These
tests pin the parser and that both session paths log the split without
changing what the session returns.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

from src._usage_breakdown import UsageBreakdown, describe_usage
from src.docker import session_bridge
from src.docker.session_bridge import SessionMessage


def test_breakdown_parses_each_kind_separately():
    usage = UsageBreakdown.from_usage(
        {
            "input_tokens": 120,
            "cache_creation_input_tokens": 3_000,
            "cache_read_input_tokens": 40_000,
            "output_tokens": 250,
        }
    )
    assert usage == UsageBreakdown(120, 3_000, 40_000, 250)
    # Log-only: rotation keeps its own arithmetic, so no summed property.
    assert not hasattr(usage, "context_tokens")
    assert usage.describe() == (
        "input=120 cache_creation=3000 cache_read=40000 output=250"
    )


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "usage",
        [],
        {},
        {"input_tokens": "12", "output_tokens": True, "other": 5},
    ],
)
def test_absent_usage_is_none_not_zero(raw):
    """No usage, or no numeric count in it, is "unavailable" — never zeros
    that read as a measured empty call."""
    assert UsageBreakdown.from_usage(raw) is None
    assert describe_usage(UsageBreakdown.from_usage(raw)) == "unavailable"


def test_malformed_sibling_fields_count_as_zero():
    usage = UsageBreakdown.from_usage(
        {"input_tokens": "12", "cache_read_input_tokens": -3, "output_tokens": 40}
    )
    assert usage == UsageBreakdown(0, 0, 0, 40)


def test_missing_usage_is_reported_unavailable_not_zero():
    assert describe_usage(None) == "unavailable"
    assert describe_usage(UsageBreakdown()) == (
        "input=0 cache_creation=0 cache_read=0 output=0"
    )


def _patch_stream(monkeypatch, seq: list[SessionMessage]) -> None:
    _patch_attempts(monkeypatch, [seq])


def _patch_attempts(monkeypatch, attempts: list[list[SessionMessage]]) -> None:
    """Each CLI launch streams the next sequence (the last one repeats)."""
    calls = {"n": 0}

    def factory(**kwargs):
        seq = attempts[min(calls["n"], len(attempts) - 1)]
        calls["n"] += 1

        async def agen():
            for message in seq:
                yield message

        return agen()

    monkeypatch.setattr(session_bridge, "stream_cli_session", factory)


@pytest.fixture
def _no_sleep(monkeypatch):
    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)


@pytest.mark.asyncio
async def test_manager_log_splits_final_call_and_run_usage(
    monkeypatch, caplog, _no_sleep
):
    from src._agent_worker_manager import run_manager_session

    worker = MagicMock()
    worker._build_mcp_config = MagicMock(return_value={})
    _patch_stream(
        monkeypatch,
        [
            SessionMessage(
                type="assistant",
                data={
                    "message": {
                        "content": [],
                        "usage": {
                            "input_tokens": 7,
                            "cache_creation_input_tokens": 900,
                            "cache_read_input_tokens": 31_000,
                            "output_tokens": 55,
                        },
                    }
                },
            ),
            SessionMessage(
                type="result",
                data={
                    "session_id": "sess-1",
                    "cost_usd": 0.01,
                    # T13: run totals differ from the final call, so the log
                    # must place each figure in its own slot.
                    "num_turns": 2,
                    "usage": {
                        "input_tokens": 14,
                        "cache_creation_input_tokens": 1_800,
                        "cache_read_input_tokens": 62_000,
                        "output_tokens": 110,
                    },
                },
            ),
        ],
    )
    with caplog.at_level(logging.INFO, logger="src._agent_worker_manager"):
        session_id, _cost, rotate = await run_manager_session(
            worker,
            user_message="hi",
            system_prompt="sys",
            session_id=None,
            context_key="workstream:abc",
            conversation_id="conv-1",
            agent_config={"_container_name": "c", "model": "claude-opus-4-7"},
        )
    # Behaviour unchanged: same session and rotation decision as before.
    assert session_id == "sess-1" and rotate is False
    line = next(
        r.getMessage()
        for r in caplog.records
        if "Manager stream ended" in r.getMessage()
    )
    assert line.endswith(
        "final_call_input_tokens=31907 (cumulative=63814 over 2 turns); "
        "final call usage [input=7 cache_creation=900 cache_read=31000 "
        "output=55]; "
        "run usage [input=14 cache_creation=1800 cache_read=62000 output=110]"
    ), line


@pytest.mark.asyncio
async def test_worker_log_splits_run_usage(monkeypatch, caplog, _no_sleep):
    from src._agent_worker_task import run_sdk_session

    worker = MagicMock()
    worker.backend_url = ""  # skip the detail fetch
    worker.office_id = "office-1"
    worker.agent_name = "analyst"
    worker.workspace_path = "/tmp/cbcl-test-workspace"
    worker._send = MagicMock()
    worker._build_mcp_config = MagicMock(return_value={})
    _patch_stream(
        monkeypatch,
        [
            SessionMessage(
                type="result",
                data={
                    "session_id": "sess-9",
                    "cost_usd": 0.02,
                    "num_turns": 3,
                    "usage": {
                        "input_tokens": 40,
                        "cache_creation_input_tokens": 12_000,
                        "cache_read_input_tokens": 88_000,
                        "output_tokens": 900,
                    },
                },
            ),
        ],
    )
    task_data = {
        "task_id": "task-1",
        "readable_id": "WR-001.T01",
        "status": "ready",
        "brief": {"goal": "Ship the thing"},
        "agent_config": {},
    }
    with caplog.at_level(logging.INFO, logger="src._agent_worker_task"):
        session_id, cost = await run_sdk_session(
            worker,
            {"_container_name": "c", "model": "claude-opus-4-7"},
            task_data,
        )
    assert (session_id, cost) == ("sess-9", 0.02)
    line = next(
        r.getMessage()
        for r in caplog.records
        if "CLI stream ended (attempt" in r.getMessage()
    )
    assert "3 turns" in line
    assert (
        "run usage [input=40 cache_creation=12000 cache_read=88000 output=900]" in line
    )


@pytest.mark.asyncio
async def test_manager_log_without_usage_frames_says_unavailable(
    monkeypatch, caplog, _no_sleep
):
    """No assistant usage and no result frame: never log measured zeros."""
    from src._agent_worker_manager import run_manager_session

    worker = MagicMock()
    worker._build_mcp_config = MagicMock(return_value={})
    _patch_stream(
        monkeypatch,
        [SessionMessage(type="system", data={"session_id": "sess-2"})],
    )
    with caplog.at_level(logging.INFO, logger="src._agent_worker_manager"):
        await run_manager_session(
            worker,
            user_message="hi",
            system_prompt="sys",
            session_id=None,
            context_key="workstream:abc",
            conversation_id="conv-2",
            agent_config={"_container_name": "c", "model": "claude-opus-4-7"},
        )
    line = next(
        r.getMessage()
        for r in caplog.records
        if "Manager stream ended" in r.getMessage()
    )
    assert "final call usage [unavailable]; run usage [unavailable]" in line


@pytest.mark.asyncio
async def test_worker_log_without_result_frame_says_unavailable(
    monkeypatch, caplog, _no_sleep
):
    from src._agent_worker_task import run_sdk_session

    worker = MagicMock()
    worker.backend_url = ""
    worker.office_id = "office-1"
    worker.agent_name = "analyst"
    worker.workspace_path = "/tmp/cbcl-test-workspace"
    worker._send = MagicMock()
    worker._build_mcp_config = MagicMock(return_value={})
    _patch_stream(
        monkeypatch,
        [SessionMessage(type="system", data={"session_id": "sess-10"})],
    )
    task_data = {
        "task_id": "task-2",
        "readable_id": "WR-001.T02",
        "status": "ready",
        "brief": {"goal": "Ship the thing"},
        "agent_config": {},
    }
    with caplog.at_level(logging.INFO, logger="src._agent_worker_task"):
        await run_sdk_session(
            worker,
            {"_container_name": "c", "model": "claude-opus-4-7"},
            task_data,
        )
    line = next(
        r.getMessage()
        for r in caplog.records
        if "CLI stream ended (attempt" in r.getMessage()
    )
    assert "0 turns" in line and "run usage [unavailable]" in line


def _manager_worker() -> MagicMock:
    worker = MagicMock()
    worker._build_mcp_config = MagicMock(return_value={})
    return worker


async def _run_manager(conversation_id: str):
    from src._agent_worker_manager import run_manager_session

    return await run_manager_session(
        _manager_worker(),
        user_message="hi",
        system_prompt="sys",
        session_id=None,
        context_key="workstream:abc",
        conversation_id=conversation_id,
        agent_config={"_container_name": "c", "model": "claude-opus-4-7"},
    )


def _manager_log_line(caplog) -> str:
    return [
        r.getMessage()
        for r in caplog.records
        if "Manager stream ended" in r.getMessage()
    ][-1]


@pytest.mark.asyncio
async def test_manager_result_frame_without_usage_says_unavailable(
    monkeypatch, caplog, _no_sleep
):
    """A result frame that carries no usage must not log measured zeros."""
    _patch_stream(
        monkeypatch,
        [
            SessionMessage(
                type="result",
                data={"session_id": "sess-3", "cost_usd": 0.0, "num_turns": 1},
            )
        ],
    )
    with caplog.at_level(logging.INFO, logger="src._agent_worker_manager"):
        await _run_manager("conv-3")
    line = _manager_log_line(caplog)
    assert "run usage [unavailable]" in line
    assert "input=0" not in line


@pytest.mark.asyncio
async def test_retried_manager_attempt_never_logs_the_previous_attempts_usage(
    monkeypatch, caplog, _no_sleep
):
    """Attempt 1 reports usage, then hits an upfront rate limit; attempt 2
    reports none. The attempt-2 log line says unavailable — not attempt 1's
    figures."""
    first = [
        SessionMessage(
            type="assistant",
            data={
                "message": {
                    "content": [],
                    "usage": {
                        "input_tokens": 5,
                        "cache_creation_input_tokens": 700,
                        "cache_read_input_tokens": 20_000,
                        "output_tokens": 9,
                    },
                }
            },
        ),
        SessionMessage(
            type="result",
            data={
                "session_id": "sess-4",
                "num_turns": 1,
                "usage": {"input_tokens": 5, "output_tokens": 9},
            },
        ),
        SessionMessage(type="error", data={"error": "API Error: 429 rate limit"}),
    ]
    second = [SessionMessage(type="system", data={"session_id": "sess-5"})]
    _patch_attempts(monkeypatch, [first, second])
    with caplog.at_level(logging.INFO, logger="src._agent_worker_manager"):
        await _run_manager("conv-4")
    line = _manager_log_line(caplog)
    assert "final call usage [unavailable]; run usage [unavailable]" in line
    assert "cache_read=20000" not in line


@pytest.mark.asyncio
async def test_worker_result_frame_without_usage_says_unavailable(
    monkeypatch, caplog, _no_sleep
):
    from src._agent_worker_task import run_sdk_session

    worker = MagicMock()
    worker.backend_url = ""
    worker.office_id = "office-1"
    worker.agent_name = "analyst"
    worker.workspace_path = "/tmp/cbcl-test-workspace"
    worker._send = MagicMock()
    worker._build_mcp_config = MagicMock(return_value={})
    _patch_stream(
        monkeypatch,
        [
            SessionMessage(
                type="result",
                data={"session_id": "sess-11", "cost_usd": 0.0, "num_turns": 2},
            )
        ],
    )
    task_data = {
        "task_id": "task-3",
        "readable_id": "WR-001.T03",
        "status": "ready",
        "brief": {"goal": "Ship the thing"},
        "agent_config": {},
    }
    with caplog.at_level(logging.INFO, logger="src._agent_worker_task"):
        await run_sdk_session(
            worker,
            {"_container_name": "c", "model": "claude-opus-4-7"},
            task_data,
        )
    line = next(
        r.getMessage()
        for r in caplog.records
        if "CLI stream ended (attempt" in r.getMessage()
    )
    assert "run usage [unavailable]" in line and "input=0" not in line
