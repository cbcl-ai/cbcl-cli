"""Connection rotation fences admissions without losing recoverable receipts."""

import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from src.execution_claim import (
    claim_worker_execution,
    recover_worker_claim,
    release_worker_execution,
    validate_worker_execution,
)
from src.runtime_state import RuntimeState
from src.scripts.script_runner import ScriptRunner


def mock_http(monkeypatch, handler):
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )


def caller():
    return {
        "role": "worker", "agent_name": "analyst", "task_id": "task",
        "task_mode": "execute", "attempt_id": str(uuid4()),
        "agent_instance_id": str(uuid4()), "profile_id": str(uuid4()),
        "execution_generation": 1, "execution_cycle": 0,
        "expected_assigned_agent": "analyst", "runtime_release_required": True,
    }


async def test_ambiguous_claim_uses_rotated_connection_secret_without_journaling_it(
    monkeypatch, tmp_path,
):
    identity = caller()
    state = RuntimeState(tmp_path / "control.sqlite", "office")
    owner = {"secret": "first-owner"}
    posts = []

    def handler(request):
        assert request.headers["authorization"] == "Bearer company-token"
        if request.method == "GET":
            assert "x-office-secret" not in request.headers
            return httpx.Response(200, json={
                "status": "in_progress", "assigned_agent": "analyst",
                "execution_generation": 0, "execution_cycle": 0,
            })
        posts.append((request.headers["x-office-secret"], json.loads(request.content)))
        if len(posts) == 1:
            owner["secret"] = "replacement-owner"
            raise httpx.ReadTimeout("claim response lost", request=request)
        return httpx.Response(200, json=identity)

    mock_http(monkeypatch, handler)
    await claim_worker_execution(
        "analyst", {"task_id": "task"}, identity["attempt_id"],
        platform_url="http://platform", office_id="office",
        security_token="company-token", runtime_state=state,
        office_tool_secret=lambda: owner["secret"],
    )
    assert [secret for secret, _ in posts] == ["first-owner", "replacement-owner"]
    assert posts[0][1] == posts[1][1]
    journal = json.dumps(state.pending_worker_executions())
    assert "first-owner" not in journal and "replacement-owner" not in journal


@pytest.mark.parametrize("receipt_exists", [False, True])
async def test_recovery_fences_only_claim_post_and_cleanup_needs_no_socket_secret(
    monkeypatch, receipt_exists,
):
    identity = caller()
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer company-token"
        if request.url.path.endswith("/claim"):
            assert request.headers["x-office-secret"] == "current-owner"
            return httpx.Response(200, json=identity)
        assert "x-office-secret" not in request.headers
        return httpx.Response(200 if receipt_exists or request.method == "POST" else 404,
                              json=identity)

    mock_http(monkeypatch, handler)
    connection = {"platform_url": "http://platform", "office_id": "office",
                  "security_token": "company-token"}
    record = {"task_id": "task", "agent_name": "analyst",
              "attempt_id": identity["attempt_id"], "request": identity}
    await recover_worker_claim(record, **connection, office_tool_secret="current-owner")
    await release_worker_execution("task", identity["attempt_id"],
                                   identity["agent_instance_id"], **connection)
    assert len(requests) == (2 if receipt_exists else 3)


async def test_script_identity_replay_uses_current_connection_secret(monkeypatch):
    identity = caller()

    def handler(request):
        assert request.headers["x-office-secret"] == "current-owner"
        return httpx.Response(200, json=identity)

    mock_http(monkeypatch, handler)
    await validate_worker_execution(
        "task", identity, platform_url="http://platform", office_id="office",
        security_token="company-token", office_tool_secret=lambda: "current-owner",
    )


async def test_script_runner_replay_observes_rotation_during_task_read(monkeypatch, tmp_path):
    identity = caller()
    supervisor = SimpleNamespace(_office_tool_secret="old-owner")
    runner = ScriptRunner(str(tmp_path), None, None, office_id="office",
                          platform_url="http://platform", security_token="company-token")
    runner.set_resource_supervisor(supervisor)

    def handler(request):
        if request.method == "GET":
            supervisor._office_tool_secret = "new-owner"
            return httpx.Response(200, json={
                **identity, "status": "in_progress",
            })
        assert request.headers["x-office-secret"] == "new-owner"
        return httpx.Response(200, json=identity)

    mock_http(monkeypatch, handler)
    await runner._assert_task_runnable("task", identity)
