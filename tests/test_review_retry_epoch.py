"""An explicit review retry is a new fenced review epoch, not a new build."""

import json
import sqlite3
import uuid
from unittest.mock import MagicMock

import httpx
import pytest

from src.execution_claim import ExecutionClaimError, claim_worker_execution, validate_worker_execution
from src.execution_completion import completion_disposition
from src.review_completion import reconcile_review_completion, upgrade_legacy_review_hold
from src.runtime_state import RuntimeState
from src.orchestrator.agent_supervisor import AgentProcess, AgentSupervisor
from src.orchestrator.task_dispatcher import TaskDispatcher, _EXECUTION_BLOCKED


@pytest.fixture
def runtime(tmp_path):
    return RuntimeState(tmp_path / "runtime.sqlite3", "office")


def install_transport(monkeypatch, transport):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(transport), **kwargs))


def task_state(epoch=0):
    return {"id": "task", "status": "review", "reviewer": "reviewer", "assigned_agent": "engineer", "execution_cycle": 3, "execution_generation": 7, "review_retry_epoch": epoch}


def completion(epoch=0):
    return {"task_id": "task", "_caller": {"agent_name": "reviewer", "role": "worker", "task_id": "task", "attempt_id": str(uuid.UUID(int=7)), "execution_cycle": 3, "execution_generation": 7, "review_retry_epoch": epoch}}


async def reconcile(runtime, task, event):
    return await reconcile_review_completion(task, event, "reviewer", runtime_state=runtime, platform_url="http://platform", office_id="office", security_token="token")


def test_legacy_migration_preserves_hold_attempts_and_history(runtime):
    attempt = str(uuid.UUID(int=1))
    with sqlite3.connect(runtime.database_path) as connection:
        connection.execute("INSERT INTO review_recovery VALUES (?, ?, ?, ?, ?, ?)", ("office", "task", 3, "reviewer", 2, "legacy-request"))
        connection.execute("INSERT INTO review_attempts VALUES (?, ?, ?, ?, ?)", ("office", "task", 3, "reviewer", attempt))
    reopened = RuntimeState(runtime.database_path, "office")
    assert reopened.review_state("task", 3, "reviewer") == {"failures": 2, "request_id": "legacy-request", "hold_kind": "legacy"}
    assert reopened.latest_review_attempt("task", 3, "reviewer") == attempt
    assert reopened.review_state("task", 3, "reviewer", epoch=1)["failures"] == 0
    reopened.hold_review("task", 3, "reviewer", "typed", hold_kind="review_hold")
    assert RuntimeState(runtime.database_path, "office").review_state("task", 3, "reviewer")["request_id"] == "typed"
    with sqlite3.connect(runtime.database_path) as connection:
        assert connection.execute("SELECT request_id FROM review_recovery").fetchone() == ("legacy-request",)


@pytest.mark.parametrize("status", [None, "", "unknown", "review"])
def test_missing_or_invalid_phase_never_clears_hold(runtime, status):
    runtime.observe_cycle("task", 3)
    runtime.hold_review("task", 3, "reviewer", "hold", hold_kind="review_hold")
    runtime.observe_review_phase("task", 3, status)
    assert runtime.review_state("task", 3, "reviewer")["request_id"] == "hold"


def test_new_epoch_resets_review_budget_only_and_keeps_history(runtime):
    runtime.observe_cycle("task", 3)
    runtime.record_failure("task", "worker-attempt", cycle=3)
    runtime.record_review_attempt("task", 3, "reviewer", "first")
    runtime.hold_review("task", 3, "reviewer", "hold", hold_kind="review_hold")
    reopened = RuntimeState(runtime.database_path, "office")
    assert reopened.review_state("task", 3, "reviewer", epoch=1)["failures"] == 0
    assert reopened.record_review_attempt("task", 3, "reviewer", "next", epoch=1) == 1
    assert reopened.review_state("task", 3, "reviewer")["request_id"] == "hold"
    assert reopened.current_cycle("task") == 3
    assert reopened.failure_count("task") == 1


async def test_delayed_old_epoch_completion_does_not_consume_new_budget(runtime, monkeypatch):
    def unexpected(request):
        pytest.fail("A superseded completion must not publish a hold")
    install_transport(monkeypatch, unexpected)
    assert await reconcile(runtime, task_state(1), completion(0)) == "superseded"
    assert runtime.review_state("task", 3, "reviewer", epoch=1)["failures"] == 0
    assert completion_disposition(task_state(1), completion(0), active_scripts=False) == "superseded"


async def test_new_epoch_publishes_typed_hold_and_stale_backend_refusal_is_terminal(runtime, monkeypatch):
    requests = []
    def transport(request):
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(409, json={"code": "stale_execution", "error": "Retry superseded this attempt"})
    install_transport(monkeypatch, transport)
    assert await reconcile(runtime, task_state(1), completion(1)) == "superseded"
    assert requests[0][0].endswith("/tasks/task/review-hold")
    assert requests[0][1]["review_retry_epoch"] == 1
    assert runtime.review_state("task", 3, "reviewer", epoch=1)["request_id"] is None


@pytest.mark.parametrize("status", [401, 403, 500, 503])
async def test_unconfirmed_hold_delivery_remains_retryable(runtime, monkeypatch, status):
    install_transport(monkeypatch, lambda request: httpx.Response(status, json={"error": "unavailable"}))
    with pytest.raises(httpx.HTTPStatusError):
        await reconcile(runtime, task_state(), completion())
    assert runtime.review_state("task", 3, "reviewer")["request_id"] is None


async def test_legacy_upgrade_uses_retained_uuid_and_never_infers_from_prose(runtime, monkeypatch):
    old_request = str(uuid.UUID(int=12))
    attempt = str(uuid.UUID(int=7))
    runtime.hold_review("task", 3, "reviewer", old_request)
    requests = []
    def transport(request):
        requests.append(json.loads(request.content))
        if request.url.path.endswith("/reconciliation"):
            return httpx.Response(200, json={"status": "operator_reconciliation_required", "action_request_id": old_request})
        return httpx.Response(200, json={"action_request_id": "typed-request", "status": "pending", "review_retry_epoch": 0})
    install_transport(monkeypatch, transport)
    options = {"runtime_state": runtime, "platform_url": "http://platform", "office_id": "office", "security_token": "token"}
    assert not await upgrade_legacy_review_hold("task", task_state(), **options)
    assert len(requests) == 1
    assert "attempt_id" not in requests[0]
    assert runtime.review_state("task", 3, "reviewer")["hold_kind"] == "legacy_reconciliation_reported"
    assert not await upgrade_legacy_review_hold("task", task_state(), **options)
    assert len(requests) == 1
    runtime.record_review_attempt("task", 3, "reviewer", attempt)
    assert await upgrade_legacy_review_hold("task", task_state(), **options)
    assert requests[1]["attempt_id"] == attempt
    assert requests[1]["legacy_request_id"] == old_request
    assert runtime.review_state("task", 3, "reviewer")["hold_kind"] == "review_hold"


async def test_claim_requires_fresh_epoch_and_refuses_stale_receipt(monkeypatch):
    attempt = str(uuid.UUID(int=8))
    requests = []
    def transport(request):
        if request.method == "GET":
            return httpx.Response(200, json=task_state(1))
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"attempt_id": attempt, "agent_name": "reviewer", "execution_cycle": 3, "execution_generation": 8, "review_retry_epoch": 0})
    install_transport(monkeypatch, transport)
    with pytest.raises(ExecutionClaimError):
        await claim_worker_execution("reviewer", {"task_id": "task", "status": "review"}, attempt, platform_url="http://platform", office_id="office", security_token="token")
    assert requests[0]["expected_review_retry_epoch"] == 1
    assert requests[0]["expected_execution_generation"] == 7


async def test_script_validation_replays_original_epoch_not_fresh_retry(monkeypatch):
    caller = {**completion(0)["_caller"], "expected_assigned_agent": "engineer", "task_mode": "review"}
    requests = []
    def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(409, json={"code": "stale_execution"})
    install_transport(monkeypatch, transport)
    with pytest.raises(httpx.HTTPStatusError):
        await validate_worker_execution("task", caller, platform_url="http://platform", office_id="office", security_token="token")
    assert requests[0]["expected_review_retry_epoch"] == 0


def test_supervisor_attests_epoch_and_restores_it_from_retained_completion(runtime):
    supervisor = AgentSupervisor(".", "office")
    agent = AgentProcess("reviewer", "worker", execution_task_id="task", execution_attempt_id=str(uuid.UUID(int=7)), execution_cycle=3, execution_generation=7, execution_mode="review", review_retry_epoch=2)
    event = supervisor._execution_event(agent, {"type": "task_complete", "task_id": "task", "_caller": {"review_retry_epoch": 99}})
    assert event["_caller"]["review_retry_epoch"] == 2
    assert supervisor._proxy_identity(agent, {})["review_retry_epoch"] == 2
    runtime.retain_completion("reviewer", agent.execution_attempt_id, "task", event)
    supervisor.set_runtime_state(RuntimeState(runtime.database_path, "office"))
    restored = supervisor._agents["reviewer"]
    assert restored.review_retry_epoch == 2
    assert restored.pending_completion == event


async def test_reopened_dispatcher_only_releases_hold_for_authoritative_new_epoch(runtime, monkeypatch):
    runtime.observe_cycle("task", 3)
    runtime.hold_review("task", 3, "reviewer", "hold", hold_kind="review_hold")
    reopened = RuntimeState(runtime.database_path, "office")
    supervisor = AgentSupervisor(".", "office")
    dispatcher = TaskDispatcher(MagicMock(), "office", supervisor, MagicMock(), MagicMock())
    dispatcher.set_runtime_state(reopened)
    detail = task_state(0)
    install_transport(monkeypatch, lambda request: httpx.Response(
        200, json={"items": [], "total": 0}
        if request.url.path.endswith("/action-requests") else detail,
    ))
    assert await dispatcher._fetch_task_status("task") == _EXECUTION_BLOCKED
    detail["review_retry_epoch"] = 1
    assert await dispatcher._fetch_task_status("task") == "review"
    assert reopened.review_state("task", 3, "reviewer")["request_id"] == "hold"
    assert reopened.current_cycle("task") == 3
