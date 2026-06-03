"""Tests for proactive native /compact on long-lived Manager sessions.

The Manager runs headless `claude --print --resume`; a long workstream
chat grows past the context window and wedges. Instead of a custom
summarizer we reuse Claude Code's OWN /compact, fired proactively once a
turn's effective input context crosses a threshold. These tests lock:

- the threshold gate in ``run_manager_session`` (compact runs iff over),
- ``_run_native_compact`` returning the post-compact session id,
- best-effort semantics (a /compact failure never fails the turn).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.docker.session_bridge import SessionMessage
from src._agent_worker_manager import (
    _MANAGER_COMPACT_THRESHOLD_TOKENS,
    _run_native_compact,
    run_manager_session,
)


def _worker() -> MagicMock:
    w = MagicMock()
    w._build_mcp_config = MagicMock(return_value={})
    w._send = MagicMock()
    return w


def _stream_factory(calls: list[dict], *, main_usage: dict, compact_sid: str):
    """Build a fake ``stream_cli_session`` async-gen.

    Records each invocation's kwargs in ``calls``. The /compact pass
    (prompt == "/compact") yields a result with ``compact_sid``; the main
    turn yields a result carrying ``main_usage`` so the threshold logic
    has real numbers to act on.
    """
    async def fake_stream(**kwargs):
        calls.append(kwargs)
        if kwargs.get("prompt") == "/compact":
            yield SessionMessage(type="result", data={"session_id": compact_sid})
        else:
            yield SessionMessage(
                type="result",
                data={"session_id": "main-sid", "usage": main_usage},
            )
    return fake_stream


@pytest.mark.asyncio
async def test_compact_runs_when_over_threshold():
    """A turn whose context crosses the threshold triggers native /compact,
    and the returned session id is the post-compact one."""
    calls: list[dict] = []
    over = {"input_tokens": 5000, "cache_read_input_tokens": 160000}
    with patch(
        "src.docker.session_bridge.stream_cli_session",
        new=_stream_factory(calls, main_usage=over, compact_sid="compacted-sid"),
    ):
        sid, _cost = await run_manager_session(
            worker=_worker(),
            user_message="status?",
            system_prompt="sys",
            session_id="prev-sid",
            context_key="workstream:x",
            conversation_id="c1",
            agent_config={"_container_name": "cbcl-office-x", "model": "opus"},
        )
    prompts = [c.get("prompt") for c in calls]
    assert "/compact" in prompts, "native /compact was not triggered"
    assert sid == "compacted-sid"


@pytest.mark.asyncio
async def test_compact_skipped_when_under_threshold():
    """A small turn does NOT trigger /compact; the main session id stands."""
    calls: list[dict] = []
    under = {"input_tokens": 1200, "cache_read_input_tokens": 8000}
    with patch(
        "src.docker.session_bridge.stream_cli_session",
        new=_stream_factory(calls, main_usage=under, compact_sid="nope"),
    ):
        sid, _cost = await run_manager_session(
            worker=_worker(),
            user_message="hi",
            system_prompt="sys",
            session_id="prev-sid",
            context_key="workstream:x",
            conversation_id="c1",
            agent_config={"_container_name": "cbcl-office-x", "model": "opus"},
        )
    prompts = [c.get("prompt") for c in calls]
    assert "/compact" not in prompts
    assert sid == "main-sid"
    # Sanity: the chosen fixture is genuinely under the configured gate.
    assert 1200 + 8000 < _MANAGER_COMPACT_THRESHOLD_TOKENS


@pytest.mark.asyncio
async def test_run_native_compact_returns_post_compact_session():
    async def fake_stream(**kwargs):
        yield SessionMessage(type="result", data={"session_id": "after-compact"})

    with patch(
        "src.docker.session_bridge.stream_cli_session", new=fake_stream,
    ):
        out = await _run_native_compact(
            container_name="c", model="m", cwd="/workspace/agents/manager",
            session_id="before", observed_tokens=160000,
        )
    assert out == "after-compact"


@pytest.mark.asyncio
async def test_run_native_compact_is_best_effort_on_failure():
    """A /compact failure must return the original session id, never raise."""
    async def boom(**kwargs):
        raise RuntimeError("compact exploded")
        yield  # pragma: no cover — makes this an async generator

    with patch("src.docker.session_bridge.stream_cli_session", new=boom):
        out = await _run_native_compact(
            container_name="c", model="m", cwd="/workspace/agents/manager",
            session_id="keep-me", observed_tokens=160000,
        )
    assert out == "keep-me"
