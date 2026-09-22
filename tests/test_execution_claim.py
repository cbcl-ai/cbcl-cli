"""Execution claims are conditional, replay-safe, and completed before spawn."""

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.execution_claim import ExecutionClaimError, claim_worker_execution
from src.execution_claim import validate_worker_execution
from src.orchestrator.agent_supervisor import AgentProcess, AgentState, AgentSupervisor


async def test_claim_retries_same_identity_after_ambiguous_response(monkeypatch):
    requests = []
    attempt_id = "12345678-1234-4234-8234-123456789abc"

    def transport(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "status": "in_progress",
                    "assigned_agent": "engineer",
                    "execution_cycle": 2,
                    "execution_generation": 7,
                },
            )
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(
            200,
            json={
                "attempt_id": attempt_id,
                "execution_cycle": 2,
                "execution_generation": 8,
                "agent_name": "engineer",
                "agent_instance_id": "11111111-1111-4111-8111-111111111111",
                "profile_id": "22222222-2222-4222-8222-222222222222",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
    result = await claim_worker_execution(
        "engineer",
        {"task_id": "task", "status": "ready"},
        attempt_id,
        platform_url="http://platform",
        office_id="office",
        security_token="test-token",
    )
    assert result["execution_generation"] == 8
    assert result["expected_assigned_agent"] == "engineer"
    assert requests[0] == requests[1]
    assert requests[0]["expected_execution_generation"] == 7
    assert requests[0]["execution_mode"] == "execute"
    assert requests[0]["expected_execution_owner"] == "engineer"


@pytest.mark.parametrize(
    "task",
    [
        {"status": "done", "execution_cycle": 1, "execution_generation": 1},
        {"status": "in_progress"},
        {"status": "in_progress", "execution_cycle": True, "execution_generation": 1},
        {
            "status": "in_progress",
            "execution_cycle": 1,
            "execution_generation": 1,
            "execution_blocked": True,
        },
    ],
)
async def test_invalid_or_unfenced_state_never_claims(monkeypatch, task):
    methods = []

    def transport(request):
        methods.append(request.method)
        return httpx.Response(200, json=task)

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
    with pytest.raises(ExecutionClaimError):
        await claim_worker_execution(
            "engineer",
            {"task_id": "task"},
            "attempt",
            platform_url="http://platform",
            office_id="office",
            security_token="token",
        )
    assert methods == ["GET"]


async def test_claim_never_precedes_old_execution_cleanup(monkeypatch):
    supervisor = AgentSupervisor(".", "office")
    old = AgentProcess("engineer", "worker", state=AgentState.IDLE)
    supervisor._agents["engineer"] = old
    cleanup = AsyncMock(side_effect=RuntimeError("cleanup unconfirmed"))
    claim = AsyncMock()
    supervisor.set_execution_claimer(claim)
    monkeypatch.setattr(supervisor, "_kill_process", cleanup)
    with pytest.raises(RuntimeError, match="cleanup unconfirmed"):
        await supervisor.spawn_worker("engineer", {}, {"task_id": "task"})
    claim.assert_not_awaited()


async def test_claim_failure_never_creates_worker_process(monkeypatch):
    supervisor = AgentSupervisor(".", "office")
    supervisor.set_execution_claimer(
        AsyncMock(side_effect=ExecutionClaimError("stale"))
    )
    spawn = AsyncMock()
    monkeypatch.setattr(
        "src.orchestrator.agent_supervisor.asyncio.create_subprocess_exec", spawn
    )
    assert not await supervisor.spawn_worker("engineer", {}, {"task_id": "task"})
    spawn.assert_not_awaited()


def test_execution_event_uses_supervisor_identity_not_worker_payload():
    supervisor = AgentSupervisor(".", "office")
    agent = AgentProcess(
        "engineer",
        "worker",
        execution_task_id="task",
        execution_cycle=2,
        execution_generation=5,
        execution_attempt_id="trusted-attempt",
    )
    event = supervisor._execution_event(
        agent, {"type": "progress", "_caller": {"attempt_id": "spoofed"}}
    )
    assert event["_caller"]["attempt_id"] == "trusted-attempt"
    assert event["_caller"]["task_id"] == "task"
    assert event["_caller"]["execution_generation"] == 5


@pytest.mark.parametrize("role", ["manager", "worker"])
def test_local_script_fence_rejects_spoofed_role_or_stale_identity(role):
    supervisor = AgentSupervisor(".", "office")
    agent = AgentProcess(
        "engineer",
        "worker",
        process=MagicMock(returncode=None),
        current_task_id="task",
        execution_cycle=2,
        execution_generation=5,
        execution_attempt_id="attempt",
        state=AgentState.WORKING,
    )
    supervisor._agents["engineer"] = agent
    caller = {
        "agent_name": "engineer",
        "role": role,
        "task_id": "task",
        "attempt_id": "attempt",
        "execution_cycle": 2,
        "execution_generation": 5,
    }
    assert supervisor.execution_is_current(caller, "task") is (role == "worker")
    caller["execution_generation"] = 4
    assert not supervisor.execution_is_current(caller, "task")


async def test_script_validation_replays_original_claim_and_never_refreshes_owner(
    monkeypatch,
):
    requests = []
    caller = {
        "agent_name": "reviewer",
        "role": "worker",
        "task_id": "task",
        "attempt_id": "attempt",
        "execution_cycle": 2,
        "execution_generation": 5,
        "expected_assigned_agent": "original-executor",
        "task_mode": "review",
    }

    def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=caller)

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
    await validate_worker_execution(
        "task",
        caller,
        platform_url="http://platform",
        office_id="office",
        security_token="token",
    )
    assert requests == [
        {
            "attempt_id": "attempt",
            "execution_cycle": 2,
            "expected_execution_generation": 4,
            "expected_assigned_agent": "original-executor",
            "expected_review_retry_epoch": 0,
            "execution_mode": "review",
            "expected_execution_owner": "reviewer",
        }
    ]


async def test_script_validation_refuses_cleared_receipt_even_if_generation_unchanged(
    monkeypatch,
):
    caller = {
        "agent_name": "engineer",
        "role": "worker",
        "task_id": "task",
        "attempt_id": "attempt",
        "execution_cycle": 2,
        "execution_generation": 5,
        "expected_assigned_agent": "engineer",
        "task_mode": "execute",
    }
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(400, json={"detail": "stale"})
        )
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
    with pytest.raises(httpx.HTTPStatusError):
        await validate_worker_execution(
            "task",
            caller,
            platform_url="http://platform",
            office_id="office",
            security_token="token",
        )


async def test_script_validation_refuses_released_receipt_pending_outcome(monkeypatch):
    caller = {
        "agent_name": "engineer", "role": "worker", "task_id": "task",
        "attempt_id": "attempt", "agent_instance_id": "instance", "profile_id": "profile",
        "execution_cycle": 2, "execution_generation": 5,
        "expected_assigned_agent": "engineer", "task_mode": "execute",
    }
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={**caller, "runtime_release_required": True,
                   "runtime_released_at": "2026-09-22T00:00:00Z"},
    )))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
    with pytest.raises(ExecutionClaimError, match="stale"):
        await validate_worker_execution(
            "task", caller, platform_url="http://platform", office_id="office",
            security_token="token", office_tool_secret="synthetic-owner-secret",
        )
