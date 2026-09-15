"""An ended reviewer session is not an approval or a rework verdict."""

import json
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest

from src.review_completion import reconcile_review_completion
from src.runtime_state import RuntimeState
from src.review_routing import default_reviewer


@pytest.fixture
def runtime(tmp_path):
    return RuntimeState(tmp_path / "runtime.sqlite3", "office")


def client_factory(monkeypatch, requests, result=None):
    original = httpx.AsyncClient

    def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=result or {"action_request_id": "attention-request", "status": "pending"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(transport), **kwargs))


async def reconcile(runtime, task, event, agent="reviewer"):
    task = {"execution_generation": 1, **task}
    event = {**event, "_caller": {
        "execution_cycle": task.get("execution_cycle"), "execution_generation": 1,
        "attempt_id": str(uuid.UUID(int=1)), **event.get("_caller", {}),
    }}
    return await reconcile_review_completion(
        task, event, agent, runtime_state=runtime, platform_url="http://platform",
        office_id="office", security_token="token",
    )


@pytest.mark.parametrize("reviewer,rework_count", [("manager-assistant", 0), ("reviewer", 0), ("reviewer", 10)])
async def test_verdictless_review_never_moves_done_or_ready(runtime, monkeypatch, reviewer, rework_count):
    requests = []
    client_factory(monkeypatch, requests)
    task = {"status": "review", "execution_cycle": 3, "reviewer": reviewer, "rework_count": rework_count}
    event = {"task_id": "task"}
    assert await reconcile(runtime, task, event, reviewer) == "held"
    assert len(requests) == 1
    assert requests[0]["reviewer"] == reviewer
    assert requests[0]["reason"] == "missing_verdict"
    assert requests[0]["review_retry_epoch"] == 0
    reopened = RuntimeState(runtime.database_path, "office")
    assert reopened.review_state("task", 3, reviewer)["request_id"] == "attention-request"
    assert await reconcile(reopened, task, event, reviewer) == "held"
    assert len(requests) == 1


@pytest.mark.parametrize("status", ["done", "ready", "archived", "blocked"])
async def test_explicit_review_transition_is_not_replayed(runtime, monkeypatch, status):
    client = AsyncMock()
    monkeypatch.setattr(httpx, "AsyncClient", client)
    assert await reconcile(runtime, {"status": status}, {"task_id": "task"}) == "already_transitioned"
    client.assert_not_called()


async def test_review_infra_budget_survives_restart_and_deduplicates_callback(runtime, monkeypatch):
    requests = []
    client_factory(monkeypatch, requests)
    task = {"status": "review", "execution_cycle": 3, "reviewer": "reviewer"}
    event = {"task_id": "task", "error_class": "transport"}
    assert await reconcile(runtime, task, event) == "retry"
    assert await reconcile(runtime, task, event) == "retry"
    reopened = RuntimeState(runtime.database_path, "office")
    assert await reconcile(reopened, task, {**event, "_caller": {"attempt_id": str(uuid.UUID(int=2))}}) == "retry"
    assert await reconcile(reopened, task, {**event, "_caller": {"attempt_id": str(uuid.UUID(int=3))}}) == "held"
    assert len(requests) == 1
    assert reopened.review_state("task", 3, "reviewer")["failures"] == 3


async def test_review_escalation_failure_retains_retryable_completion(runtime, monkeypatch):
    requests = []
    client_factory(monkeypatch, requests, {"error": "temporary platform failure"})
    with pytest.raises(RuntimeError, match="not accepted"):
        await reconcile(runtime, {"status": "review", "execution_cycle": 3, "reviewer": "reviewer"}, {"task_id": "task"})
    assert runtime.review_state("task", 3, "reviewer")["request_id"] is None


def test_review_hold_only_resets_for_changed_owner_cycle_or_observed_phase(runtime):
    runtime.observe_cycle("task", 3)
    runtime.hold_review("task", 3, "reviewer", "request")
    runtime.observe_review_phase("task", 3, "review")
    assert runtime.review_state("task", 3, "reviewer")["request_id"] == "request"
    assert runtime.review_state("task", 3, "another-reviewer")["request_id"] is None
    assert runtime.review_state("task", 4, "reviewer")["request_id"] is None
    runtime.observe_review_phase("task", 3, "ready")
    assert runtime.review_state("task", 3, "reviewer")["request_id"] is None


@pytest.mark.parametrize("executor,reviewer", [("engineer", "manager-assistant"), ("manager-assistant", "auditor"), (None, "manager-assistant")])
def test_default_reviewer_is_independent(executor, reviewer):
    assert default_reviewer({"assigned_agent": executor}) == reviewer
