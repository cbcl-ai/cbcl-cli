"""FIX W1 (blink-resilience) — infra-outage deferred-resume ladder.

Contract under test (``_agent_worker_task.run_sdk_session``):

* an EXECUTE-mode board task whose quick retry budget is exhausted by a
  transient infra class (529/429/timeout/connection drop) does NOT
  escalate to blocked immediately — it parks on the 15/30-minute
  deferred-resume ladder (each rung grants one more attempt), posting a
  user-visible "⏸ Paused — provider outage" checkpoint per rung;
* the ladder is bounded (2 rungs) — after it the existing
  ``AgentErrorEscalation`` fires, naming the consumed ladder;
* reviewer sessions, Planner consults, and non-infra classes keep the
  plain 3-attempt budget (no defer);
* a rung that would blow the 6-hour wall-clock budget escalates NOW with
  the resume time named instead of sleeping into a guaranteed timeout.

``stream_cli_session`` is patched to yield scripted error frames and
``asyncio.sleep`` is patched to record backoffs without waiting.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

import src._agent_worker_task as awt
from src._agent_worker_task import (
    _INFRA_DEFER_DELAYS_SECONDS,
    run_sdk_session,
)
from src.agent_worker import AgentErrorEscalation, _MAX_SESSION_ATTEMPTS
from src.docker import session_bridge
from src.docker.session_bridge import SessionMessage


def _fake_worker() -> MagicMock:
    worker = MagicMock()
    # Empty backend_url skips the get_task_detail fetch entirely; the
    # in-hand brief below satisfies the brief-abort guard.
    worker.backend_url = ""
    worker.office_id = "office-1"
    worker.agent_name = "analyst"
    worker.workspace_path = "/tmp/cbcl-test-workspace"
    worker._send = MagicMock()
    worker._build_mcp_config = MagicMock(return_value={})
    return worker


def _task_data(status: str = "ready", *, planner: bool = False) -> dict:
    data = {
        "task_id": "task-1",
        "readable_id": "WR-001.T01",
        "status": status,
        "brief": {"goal": "Ship the thing"},
        "agent_config": {},
    }
    if planner:
        data["planner_consult"] = {"mode": "roadmap"}
        data["workstream_context"] = {}
    return data


AGENT_CONFIG = {"_container_name": "cbcl-office-test",
                "model": "claude-opus-4-7"}


def _err(text: str) -> SessionMessage:
    return SessionMessage(type="error", data={"error": text, "stderr": ""})


def _patch_error_stream(monkeypatch, error_text: str) -> dict:
    """Every attempt yields one error frame — the CLI never succeeds."""
    calls = {"n": 0}

    def factory(**kwargs):
        calls["n"] += 1

        async def agen():
            yield _err(error_text)

        return agen()

    monkeypatch.setattr(session_bridge, "stream_cli_session", factory)
    return calls


@pytest.fixture
def _no_sleep(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return slept


def _paused_checkpoints(worker) -> list[dict]:
    return [
        f for f in (c.args[0] for c in worker._send.call_args_list)
        if f.get("event_type") == "checkpoint"
        and "⏸ Paused — provider outage" in (f.get("content") or "")
    ]


@pytest.mark.asyncio
async def test_infra_exhaustion_defers_then_escalates(
    monkeypatch, _no_sleep,
):
    """A sustained 529: 3 quick attempts, then the 15-min and 30-min
    rungs (one attempt each), THEN the blocked escalation naming the
    consumed ladder."""
    worker = _fake_worker()
    calls = _patch_error_stream(
        monkeypatch, "API Error: 529 Overloaded",
    )

    with pytest.raises(AgentErrorEscalation) as exc:
        await run_sdk_session(worker, AGENT_CONFIG, _task_data())

    assert exc.value.error_class == "api_overloaded"
    assert "deferred ladder" in exc.value.escalation_message
    # 3 quick attempts + one per ladder rung.
    assert calls["n"] == _MAX_SESSION_ATTEMPTS + len(
        _INFRA_DEFER_DELAYS_SECONDS
    )
    # Both rungs actually slept their defer, not the 180s class backoff.
    for delay in _INFRA_DEFER_DELAYS_SECONDS:
        assert delay in _no_sleep
    paused = _paused_checkpoints(worker)
    assert len(paused) == len(_INFRA_DEFER_DELAYS_SECONDS)
    assert paused[0]["details"]["paused"] is True
    assert paused[0]["details"]["deferred_cycle"] == 1
    assert "resume_at" in paused[0]["details"]
    assert "Auto-resuming at" in paused[0]["content"]


@pytest.mark.asyncio
async def test_rate_limit_defers_too(monkeypatch, _no_sleep):
    worker = _fake_worker()
    calls = _patch_error_stream(
        monkeypatch, "API Error 429 rate limit exceeded",
    )
    with pytest.raises(AgentErrorEscalation) as exc:
        await run_sdk_session(worker, AGENT_CONFIG, _task_data())
    assert exc.value.error_class == "rate_limited"
    assert calls["n"] == _MAX_SESSION_ATTEMPTS + len(
        _INFRA_DEFER_DELAYS_SECONDS
    )


@pytest.mark.asyncio
async def test_reviewer_session_does_not_defer(monkeypatch, _no_sleep):
    """Review-mode sessions keep the plain budget — their infra failures
    are handled by the no-rework-cost review re-queue, not by holding
    the reviewer asleep for 45 minutes."""
    worker = _fake_worker()
    calls = _patch_error_stream(monkeypatch, "API Error: 529 Overloaded")
    with pytest.raises(AgentErrorEscalation):
        await run_sdk_session(
            worker, AGENT_CONFIG, _task_data(status="review"),
        )
    assert calls["n"] == _MAX_SESSION_ATTEMPTS
    assert not _paused_checkpoints(worker)
    assert all(s < 900.0 for s in _no_sleep)


@pytest.mark.asyncio
async def test_planner_consult_does_not_defer(monkeypatch, _no_sleep):
    """Planner consults are exempt — a sleeping consult would trip the
    stall watchdog; their recovery is the one-shot infra re-fire in
    ``handlers``."""
    worker = _fake_worker()
    calls = _patch_error_stream(monkeypatch, "API Error: 529 Overloaded")
    with pytest.raises(AgentErrorEscalation):
        await run_sdk_session(
            worker, AGENT_CONFIG,
            _task_data(status="planning", planner=True),
        )
    assert calls["n"] == _MAX_SESSION_ATTEMPTS
    assert not _paused_checkpoints(worker)


@pytest.mark.asyncio
async def test_non_infra_class_does_not_defer(monkeypatch, _no_sleep):
    """context_too_large is retryable but NOT an infra outage — waiting
    30 minutes doesn't shrink a context; the plain budget applies."""
    worker = _fake_worker()
    calls = _patch_error_stream(
        monkeypatch, "prompt is too long: 250000 tokens > 200000 maximum",
    )
    with pytest.raises(AgentErrorEscalation) as exc:
        await run_sdk_session(worker, AGENT_CONFIG, _task_data())
    assert exc.value.error_class == "context_too_large"
    assert calls["n"] == _MAX_SESSION_ATTEMPTS
    assert not _paused_checkpoints(worker)


@pytest.mark.asyncio
async def test_defer_beyond_wallclock_escalates_now(
    monkeypatch, _no_sleep,
):
    """A rung that would exceed the 6-hour budget escalates immediately
    with the resume time named, instead of sleeping into a guaranteed
    wall-clock timeout."""
    worker = _fake_worker()
    _patch_error_stream(monkeypatch, "API Error: 529 Overloaded")
    # Shrink the budget below the first rung so the guard fires.
    monkeypatch.setattr(
        awt, "_MAX_SESSION_WALLCLOCK_SECONDS",
        _INFRA_DEFER_DELAYS_SECONDS[0] - 1.0,
    )
    with pytest.raises(AgentErrorEscalation) as exc:
        await run_sdk_session(worker, AGENT_CONFIG, _task_data())
    assert exc.value.error_class == "api_overloaded"
    assert "runtime budget" in exc.value.escalation_message
    assert not _paused_checkpoints(worker)
