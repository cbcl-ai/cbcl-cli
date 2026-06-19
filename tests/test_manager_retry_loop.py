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
