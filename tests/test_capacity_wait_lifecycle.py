"""Accepted capacity refusals and resumed claims retain different authority."""

# ruff: noqa: F811 -- pytest resolves imported fixtures by their parameter name.
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from src.execution_claim import claim_worker_execution, recover_worker_claim
from src.execution_completion import completion_disposition
from src.runtime_state import RuntimeState
from src.scripts.capacity_wait import CapacityWaitCoordinator
from tests.test_capacity_waits import due, free, park, waiting_context  # noqa: F401
from tests.test_managed_operations import context  # noqa: F401


@pytest.mark.parametrize("active_attempt", [None, "different-attempt"])
async def test_wait_does_not_match_a_cleared_or_different_active_attempt(
    waiting_context, active_attempt
):
    ctx = waiting_context
    await park(ctx)
    changed = {**ctx.task, "active_execution_attempt_id": active_attempt}
    assert ctx.state.capacity_wait_for_task(changed) is None
    assert not ctx.state.capacity_completion_handoff(changed, {"_caller": ctx.caller})


async def test_incomplete_active_attempt_projection_never_retires_wait(waiting_context):
    ctx = waiting_context
    await park(ctx)
    incomplete = dict(ctx.task)
    incomplete.pop("active_execution_attempt_id")
    assert not ctx.coordinator.can_dispatch(incomplete)
    assert ctx.state.capacity_wait("task")["state"] == "waiting"


async def test_resumed_attempt_without_new_refusal_cannot_waive_review_verdict(
    waiting_context,
):
    ctx = waiting_context
    ctx.task.update(status="review", reviewer="manager-assistant")
    ctx.caller.update(task_mode="review", agent_name="manager-assistant")
    await park(ctx)
    wait = ctx.state.capacity_wait("task")
    ctx.state.begin_capacity_resume_claim(ctx.task, wait["wait_id"], "new-attempt")
    ctx.state.begin_worker_claim(
        "manager-assistant", "task", {"attempt_id": "new-attempt"}
    )
    ctx.state.record_worker_claim(
        "new-attempt",
        {
            "attempt_id": "new-attempt",
            "agent_name": "manager-assistant",
            "execution_cycle": 1,
            "execution_generation": 3,
            "review_retry_epoch": 0,
        },
    )
    ctx.task.update(execution_generation=3, active_execution_attempt_id="new-attempt")
    caller = {**ctx.caller, "attempt_id": "new-attempt", "execution_generation": 3}
    event = {"_caller": caller, "status": "done", "is_review_completion": True}
    # Assignment delivery and completion can interleave before the supervisor
    # acknowledges successful spawn; cleanup also resets a pending resume.
    for cleanup in (False, True):
        if cleanup:
            ctx.state.forget_worker_execution("new-attempt")
        handoff = ctx.state.capacity_completion_handoff(ctx.task, event)
        assert not handoff
        assert (
            completion_disposition(
                ctx.task, event, active_scripts=False, capacity_wait=handoff
            )
            == "normal"
        )


@pytest.mark.parametrize("lose_all_responses", [False, True])
async def test_real_claim_http_retries_and_restart_bind_only_original_pending_identity(
    waiting_context, monkeypatch, lose_all_responses
):
    ctx = waiting_context
    await park(ctx)
    free(ctx)
    task_data = {**ctx.task, "task_id": "task"}
    assert ctx.coordinator.can_dispatch(task_data)
    attempt = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    receipt = {
        "attempt_id": attempt,
        "execution_cycle": 1,
        "execution_generation": 3,
        "review_retry_epoch": 0,
        "agent_name": "analyst",
        "runtime_release_required": True,
        "agent_instance_id": "11111111-1111-4111-8111-111111111111",
        "profile_id": "22222222-2222-4222-8222-222222222222",
    }
    posts = []

    def transport(request):
        if request.method == "GET":
            return httpx.Response(
                200, json=receipt if request.url.path.endswith(attempt) else ctx.task
            )
        posts.append(json.loads(request.content))
        if lose_all_responses or len(posts) == 1:
            raise httpx.ReadTimeout("accepted response lost", request=request)
        return httpx.Response(200, json=receipt)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(transport), **kwargs
        ),
    )
    monkeypatch.setattr("src.execution_claim.asyncio.sleep", AsyncMock())

    async def claim():
        return await claim_worker_execution(
            "analyst",
            task_data,
            attempt,
            platform_url="http://platform",
            office_id="office",
            security_token="private-not-persisted",
            runtime_state=ctx.state,
        )

    if lose_all_responses:
        with pytest.raises(httpx.ReadTimeout):
            await claim()
        waiting = ctx.state.capacity_wait("task")
        assert (
            waiting["pending_resume_attempt_id"] == attempt
            and waiting["generation"] == 2
        )
        ctx.state = RuntimeState(ctx.state.database_path, "office")
        journal = ctx.state.pending_worker_executions()[0]
        assert journal["receipt"] is None
        recovered = await recover_worker_claim(
            journal,
            platform_url="http://platform",
            office_id="office",
            security_token="private-not-persisted",
        )
        ctx.state.record_worker_claim(attempt, recovered)
    else:
        assert (await claim())["attempt_id"] == attempt
    assert len(posts) == (3 if lose_all_responses else 2)
    assert all(body == posts[0] for body in posts)
    current = ctx.state.capacity_wait("task")
    assert current["attempt_id"] == attempt and current["generation"] == 3
    assert (
        current["pending_resume_attempt_id"] is None and current["state"] == "resuming"
    )
    ctx.task.update(execution_generation=3, active_execution_attempt_id=attempt)
    ctx.runner.set_runtime_state(ctx.state)
    coordinator = CapacityWaitCoordinator(ctx.runner)
    assert not coordinator.can_dispatch(dict(ctx.task))
    # Only exact cleanup/absence of a booted execution reopens model pickup.
    ctx.state.forget_worker_execution(attempt)
    due(ctx)
    resumed = dict(ctx.task)
    assert coordinator.can_dispatch(resumed)
    assert resumed["capacity_wait_resume"]["operation_id"] == current["operation_id"]
    assert not ctx.runner._active
    assert b"private-not-persisted" not in ctx.state.database_path.read_bytes()
