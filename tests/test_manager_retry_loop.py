"""Manager per-turn retry loop (resilience, commit c1ac2925).

A retryable upfront API error (rate limit / overload / transient drop) that
occurs BEFORE any user-visible output is retried after the classifier backoff
by re-running the same turn (resuming the session). A mid-stream error (after
visible output) is NOT retried (would duplicate), a usage-limit is NOT retried
(an interactive turn must not sleep for a multi-hour window), and a fatal error
propagates. Drives the module-level ``run_manager_session`` with
``stream_cli_session`` patched to yield scripted ``SessionMessage`` frames and
``asyncio.sleep`` patched to record the backoff without waiting.
"""
from __future__ import annotations

import asyncio

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


def _err(text: str) -> SessionMessage:
    return SessionMessage(type="error", data={"error": text})


def _result(sid: str = "sess-1", cost: float = 0.01) -> SessionMessage:
    return SessionMessage(
        type="result",
        data={"session_id": sid, "cost_usd": cost, "usage": {}},
    )


def _text_start() -> SessionMessage:
    return SessionMessage(
        type="stream_event",
        data={"event": {"type": "content_block_start",
                        "content_block": {"type": "text"}}},
    )


def _text_delta(text: str) -> SessionMessage:
    return SessionMessage(
        type="stream_event",
        data={"event": {"type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": text}}},
    )


def _patch_stream(monkeypatch, scripts: list[list[SessionMessage]]) -> dict:
    """Patch stream_cli_session to yield ``scripts[attempt]`` per call."""
    calls = {"n": 0}

    def factory(**kwargs):
        idx = calls["n"]
        calls["n"] += 1
        seq = scripts[min(idx, len(scripts) - 1)]

        async def agen():
            for m in seq:
                yield m

        return agen()

    monkeypatch.setattr(session_bridge, "stream_cli_session", factory)
    return calls


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return slept


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


def _patch_stream_capture(monkeypatch, seq: list[SessionMessage]) -> list[dict]:
    """Patch stream_cli_session to record each call's kwargs and yield `seq`."""
    captured: list[dict] = []

    def factory(**kwargs):
        captured.append(kwargs)

        async def agen():
            for m in seq:
                yield m

        return agen()

    monkeypatch.setattr(session_bridge, "stream_cli_session", factory)
    return captured


def _assistant_with_tool_use() -> SessionMessage:
    # A complete `assistant` frame carrying a tool_use block — the shape the
    # NO-partial-frames fallback path receives (no stream_event frames at all).
    return SessionMessage(
        type="assistant",
        data={"message": {
            "usage": {"input_tokens": 10},
            "content": [{"type": "tool_use", "name": "mcp__cubicle-tools__create_task"}],
        }},
    )


@pytest.mark.asyncio
async def test_retry_gate_sees_tool_use_on_fallback_path(monkeypatch, _no_sleep):
    """SES-02 (review P4R-04): on the no-partial-frames fallback path, a
    tool_use arriving only inside the complete assistant frame must still arm
    the tools_executed gate — a retryable error AFTER an executed tool must
    NOT replay the turn (double board mutation)."""
    w = _FakeWorker()
    calls = _patch_stream(monkeypatch, [
        [_assistant_with_tool_use(),
         _err("API Error 429 rate limit exceeded")],
        [_result()],
    ])
    with pytest.raises(RuntimeError):
        await _run(w)
    assert calls["n"] == 1  # no retry once a tool executed — gate held


@pytest.mark.asyncio
async def test_effort_unknown_flag_degrades_and_retries(monkeypatch, _no_sleep):
    # SES-05: an older container CLI that rejects --effort must NOT hard-fail
    # the Manager. It drops the flag and re-runs the turn (no backoff), and the
    # degrade does not consume an API-retry attempt.
    w = _FakeWorker()
    calls: list[dict] = []
    scripts = [
        [_err("error: unknown option '--effort'")],  # older CLI rejects it
        [_result()],                                  # retry without it succeeds
    ]

    def factory(**kwargs):
        idx = len(calls)
        calls.append(kwargs)
        seq = scripts[min(idx, len(scripts) - 1)]

        async def agen():
            for m in seq:
                yield m

        return agen()

    monkeypatch.setattr(session_bridge, "stream_cli_session", factory)
    sid, _cost, _rotate = await _run(w)  # AGENT_CONFIG = opus → effort xhigh
    assert len(calls) == 2                    # degraded once, then retried
    assert calls[0].get("effort") == "xhigh"  # first attempt sent --effort
    assert calls[1].get("effort") is None     # retry dropped --effort
    assert sid == "sess-1"
    # Not a rate-limit path → no "API is busy" chunk to the user.
    assert not any("busy" in (f.get("content") or "").lower() for f in w.sent)


@pytest.mark.asyncio
async def test_manager_session_runs_at_xhigh_effort_on_opus(monkeypatch, _no_sleep):
    # SES-05: the Manager is the highest-leverage reasoning surface; its session
    # must carry an explicit --effort xhigh (opus-tier), not CLI-default.
    w = _FakeWorker()
    captured = _patch_stream_capture(monkeypatch, [_result()])
    await _run(w)  # AGENT_CONFIG uses claude-opus-4-7
    assert captured, "stream_cli_session was not called"
    assert captured[0].get("effort") == "xhigh"


@pytest.mark.asyncio
async def test_manager_session_effort_none_on_non_opus(monkeypatch, _no_sleep):
    # Defense-in-depth: --effort is rejected by non-opus models, so the Manager
    # must pass effort=None there (mirrors the worker session policy).
    w = _FakeWorker()
    captured = _patch_stream_capture(monkeypatch, [_result()])
    await run_manager_session(
        w,
        user_message="hi",
        system_prompt="sys",
        session_id=None,
        context_key="workstream:abc",
        conversation_id="conv-1",
        agent_config={"_container_name": "cbcl-office-test",
                      "model": "claude-sonnet-4-6"},
    )
    assert captured
    assert captured[0].get("effort") is None


@pytest.mark.asyncio
async def test_rate_limit_before_output_retries(monkeypatch, _no_sleep):
    w = _FakeWorker()
    calls = _patch_stream(monkeypatch, [
        [_err("API Error 429 rate limit exceeded")],
        [_text_start(), _text_delta("hello"), _result()],
    ])
    sid, _cost, _rotate = await _run(w)
    assert calls["n"] == 2  # retried once
    assert sid == "sess-1"
    assert 60.0 in _no_sleep  # ~1-minute rate-limit backoff
    assert any("busy" in (f.get("content") or "").lower() for f in w.sent)


@pytest.mark.asyncio
async def test_overload_529_retries(monkeypatch, _no_sleep):
    # The exact reported bug: a 529 on the Manager pick-up turn.
    w = _FakeWorker()
    calls = _patch_stream(monkeypatch, [
        [_err("API Error: 529 Overloaded. Claude CLI exited with code 1")],
        [_result()],
    ])
    await _run(w)
    assert calls["n"] == 2
    assert 180.0 in _no_sleep  # overload backoff


@pytest.mark.asyncio
async def test_error_after_visible_output_does_not_retry(monkeypatch, _no_sleep):
    w = _FakeWorker()
    calls = _patch_stream(monkeypatch, [
        [_text_start(), _text_delta("partial answer"),
         _err("API Error 429 rate limit")],
    ])
    with pytest.raises(RuntimeError):
        await _run(w)
    assert calls["n"] == 1  # no retry once text reached the user


@pytest.mark.asyncio
async def test_usage_limit_not_retried(monkeypatch, _no_sleep):
    w = _FakeWorker()
    calls = _patch_stream(monkeypatch, [
        [_err("Claude usage limit reached. Your limit will reset at 11pm")],
    ])
    with pytest.raises(RuntimeError):
        await _run(w)
    assert calls["n"] == 1  # interactive turn must not sleep for the window
    assert not _no_sleep  # never slept


@pytest.mark.asyncio
async def test_max_attempts_surfaces_error(monkeypatch, _no_sleep):
    w = _FakeWorker()
    calls = _patch_stream(monkeypatch, [
        [_err("API Error 429 rate limit")],
        [_err("API Error 429 rate limit")],
        [_err("API Error 429 rate limit")],
    ])
    with pytest.raises(RuntimeError):
        await _run(w)
    assert calls["n"] == 3  # capped at _MANAGER_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_fatal_error_not_retried(monkeypatch, _no_sleep):
    w = _FakeWorker()
    calls = _patch_stream(monkeypatch, [
        [_err("prompt is too long: 250000 tokens > 200000 maximum")],
    ])
    with pytest.raises(RuntimeError):
        await _run(w)
    assert calls["n"] == 1  # context-too-large is not a Manager retry class


@pytest.mark.asyncio
async def test_clean_success_no_retry(monkeypatch, _no_sleep):
    w = _FakeWorker()
    calls = _patch_stream(monkeypatch, [
        [_text_start(), _text_delta("done"), _result()],
    ])
    sid, _cost, _rotate = await _run(w)
    assert calls["n"] == 1
    assert sid == "sess-1"
    assert not _no_sleep


# --- SES-01: rotation measures the FINAL call's context, not cumulative -------

def _assistant(input_tokens: int, cache_read: int = 0,
               cache_creation: int = 0) -> SessionMessage:
    return SessionMessage(
        type="assistant",
        data={"message": {"content": [], "usage": {
            "input_tokens": input_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        }}},
    )


def _tool_start(name: str = "create_task") -> SessionMessage:
    return SessionMessage(
        type="stream_event",
        data={"event": {"type": "content_block_start",
                        "content_block": {"type": "tool_use", "name": name}}},
    )


def _result_usage(cumulative_input: int, num_turns: int,
                  sid: str = "sess-1") -> SessionMessage:
    return SessionMessage(
        type="result",
        data={"session_id": sid, "cost_usd": 0.01, "num_turns": num_turns,
              "usage": {"input_tokens": cumulative_input}},
    )


@pytest.mark.asyncio
async def test_rotation_uses_final_call_not_cumulative(monkeypatch, _no_sleep):
    # A tool-heavy turn: cumulative usage is huge (300k) but each individual
    # call's own context stays small (≤50k). Rotation must key off the FINAL
    # call (50k < 120k) → do NOT rotate. The old code used the 300k cumulative
    # and would over-rotate.
    w = _FakeWorker()
    _patch_stream(monkeypatch, [[
        _assistant(40000), _tool_start(),
        _assistant(45000), _tool_start(),
        _assistant(50000),
        _result_usage(300000, num_turns=6),
    ]])
    _sid, _cost, rotate = await _run(w)
    assert rotate is False


@pytest.mark.asyncio
async def test_rotation_fires_when_final_call_exceeds_threshold(monkeypatch, _no_sleep):
    w = _FakeWorker()
    _patch_stream(monkeypatch, [[
        _assistant(130000),
        _result_usage(130000, num_turns=1),
    ]])
    _sid, _cost, rotate = await _run(w)
    assert rotate is True


# --- SES-02: a turn that executed a tool must NOT silently retry --------------


@pytest.mark.asyncio
async def test_no_silent_retry_after_tool_executed(monkeypatch, _no_sleep):
    # A tool_use block was emitted (a board mutation may have landed), THEN a
    # retryable 429. Silently resuming the pre-turn session would re-issue the
    # tool → duplicate side effect. Must surface the error, not retry.
    w = _FakeWorker()
    calls = _patch_stream(monkeypatch, [
        [_tool_start("create_task"), _err("API Error 429 rate limit")],
        [_result()],  # the retry attempt — must NOT be reached
    ])
    with pytest.raises(RuntimeError):
        await _run(w)
    assert calls["n"] == 1  # NOT retried
