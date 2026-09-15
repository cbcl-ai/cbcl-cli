"""Real proxy requests use host identities, not container-supplied roles."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import uuid

import aiohttp
import pytest
from aiohttp import web

from src._agent_worker_mcp import build_mcp_config
from src.orchestrator.agent_supervisor import AgentProcess, AgentState, AgentSupervisor
from src.tool_proxy_identity import ProxySessionRegistry
from src.tool_proxy_server import ToolProxyServer


def worker_caller():
    return {
        "agent_name": "engineer", "role": "worker", "task_mode": "execute",
        "task_id": "task-a", "attempt_id": str(uuid.uuid4()),
        "execution_cycle": 2, "execution_generation": 3,
        "expected_assigned_agent": "engineer",
    }


@pytest.fixture
async def proxy():
    transport = SimpleNamespace(connected=True, request=AsyncMock(return_value={"ok": True}))
    runner = MagicMock()
    runner.execute = AsyncMock(return_value="exec-test")
    server = ToolProxyServer(transport, port=0, host="127.0.0.1", script_runner=runner)
    await server.start()
    try:
        yield server, transport, runner
    finally:
        await server.stop()


async def post(server, token, endpoint, body):
    async with aiohttp.ClientSession() as client:
        async with client.post(
            f"http://127.0.0.1:{server.port}{endpoint}", json=body,
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            return response.status, await response.text()


@pytest.mark.parametrize("placement", ["top", "nested", "both", "absent"])
async def test_forged_role_and_attempt_are_replaced_by_host_identity(proxy, placement):
    server, transport, _runner = proxy
    caller = worker_caller()
    credentials = server.sessions.issue(caller, lambda: True)
    invocation = str(uuid.uuid4())
    forged = {"agent_name": "manager", "role": "manager", "task_id": "other", "invocation_id": invocation}
    body = {"action": "move_task", "params": {"actor": "system", "task_id": "other"}}
    if placement in ("top", "both"):
        body["_caller"] = forged
    if placement in ("nested", "both"):
        body["params"]["_caller"] = forged
    status, _result = await post(server, credentials.tool_token, "/tool-call", body)
    assert status == 200
    forwarded = transport.request.await_args.kwargs["params"]["_caller"]
    assert forwarded == {**caller, **({"invocation_id": invocation} if placement != "absent" else {})}


@pytest.mark.parametrize("endpoint", ["/tool-call", "/script-execute-host", "/script-status", "/outbox-scan"])
async def test_collections_session_cannot_access_privileged_routes(proxy, endpoint):
    server, transport, runner = proxy
    credentials = server.sessions.issue(worker_caller(), lambda: True)
    status, _result = await post(server, credentials.collections_token, endpoint, {})
    assert status == 401
    transport.request.assert_not_awaited()
    runner.execute.assert_not_awaited()


@pytest.mark.parametrize("endpoint", ["/script-status", "/outbox-scan"])
async def test_agent_cannot_fabricate_host_completion_events(proxy, endpoint):
    server, _transport, _runner = proxy
    credentials = server.sessions.issue(worker_caller(), lambda: True)
    status, _result = await post(server, credentials.tool_token, endpoint, {})
    assert status == 401


async def test_script_task_and_caller_are_host_bound(proxy):
    server, _transport, runner = proxy
    caller = worker_caller()
    credentials = server.sessions.issue(caller, lambda: True)
    server.set_execution_validator(lambda received, task_id: received == caller and task_id == "task-a")
    body = {"script_name": "verify", "_caller": {"role": "manager"}, "triggered_by": "manager"}
    status, result = await post(server, credentials.tool_token, "/script-execute-host", body)
    assert status == 200
    assert json.loads(result)["execution_id"] == "exec-test"
    assert runner.execute.await_args.kwargs["execution_caller"] == caller
    assert runner.execute.await_args.kwargs["triggered_by"] == "engineer"
    assert runner.execute.await_args.kwargs["task_id"] == "task-a"
    status, _result = await post(server, credentials.tool_token, "/script-execute-host", {**body, "task_id": "task-b"})
    assert status == 403
    assert runner.execute.await_count == 1


async def test_manager_retains_office_task_script_linkage(proxy):
    server, _transport, runner = proxy
    caller = {"agent_name": "manager", "role": "manager", "task_mode": "manager"}
    credentials = server.sessions.issue(caller, lambda: True)
    server.set_execution_validator(lambda received, task_id: received == caller and task_id == "task-a")
    status, _result = await post(server, credentials.tool_token, "/script-execute-host", {
        "script_name": "verify", "task_id": "task-a",
    })
    assert status == 200
    assert runner.execute.await_args.kwargs["task_id"] == "task-a"
    assert runner.execute.await_args.kwargs["execution_caller"] == caller


async def test_revoked_or_dead_session_has_no_route_access(proxy):
    server, transport, _runner = proxy
    alive = True
    credentials = server.sessions.issue(worker_caller(), lambda: alive)
    assert server.sessions.resolve(credentials.collections_token, collections=True) is not None
    alive = False
    status, _result = await post(server, credentials.tool_token, "/tool-call", {"action": "get_board"})
    assert status == 401
    assert server.sessions.resolve(credentials.collections_token, collections=True) is None
    alive = True
    server.sessions.revoke(credentials)
    status, _result = await post(server, credentials.tool_token, "/collections/rpc", {"action": "data_rows_list"})
    assert status == 401
    assert server.sessions.resolve(credentials.tool_token) is None
    transport.request.assert_not_awaited()


async def test_identity_is_rechecked_after_reading_request_body(proxy):
    server, transport, _runner = proxy
    credentials = server.sessions.issue(worker_caller(), lambda: True)

    async def read_and_revoke():
        server.sessions.revoke(credentials)
        return {"action": "get_board"}

    request = SimpleNamespace(
        headers={"Authorization": f"Bearer {credentials.tool_token}"}, json=read_and_revoke,
    )
    with pytest.raises(web.HTTPUnauthorized):
        await server._handle_tool_call(request)
    transport.request.assert_not_awaited()


async def test_tokens_are_office_and_proxy_instance_scoped(proxy):
    server, transport, _runner = proxy
    other_registry = ProxySessionRegistry()
    credentials = other_registry.issue(worker_caller(), lambda: True)
    status, _result = await post(server, credentials.tool_token, "/tool-call", {"action": "get_board"})
    assert status == 401
    transport.request.assert_not_awaited()
    assert credentials.tool_token not in repr(credentials)


@pytest.mark.parametrize("body", [[], {"action": ["get_board"]}, {"action": "get_board", "params": []}])
async def test_malformed_payload_is_a_client_error(proxy, body):
    server, transport, _runner = proxy
    credentials = server.sessions.issue(worker_caller(), lambda: True)
    status, _result = await post(server, credentials.tool_token, "/tool-call", body)
    assert status == 400
    transport.request.assert_not_awaited()


def bound_supervisor():
    supervisor = AgentSupervisor("unused", "office-test")
    registry = ProxySessionRegistry()
    supervisor.set_tool_proxy("http://proxy", "host-token", "host-collections", sessions=registry)
    agent = AgentProcess(
        agent_name="engineer", role="worker", execution_task_id="task-a",
        execution_mode="execute", execution_generation=1, state=AgentState.WORKING,
        process=SimpleNamespace(returncode=None), execution_marker="owned-marker",
    )
    supervisor._agents[agent.agent_name] = agent
    env = supervisor._build_subprocess_env()
    supervisor._bind_proxy_session(agent, {}, env)
    return supervisor, registry, agent, env


def test_scoped_env_never_inherits_another_offices_credentials(monkeypatch):
    for name in ("CUBICLE_TOOL_PROXY_TOKEN", "CUBICLE_COLLECTIONS_TOKEN", "CUBICLE_OFFICE_TOOL_SECRET"):
        monkeypatch.setenv(name, "another-office")
    supervisor, registry, agent, env = bound_supervisor()
    assert env["CUBICLE_TOOL_PROXY_TOKEN"] == agent.proxy_credentials.tool_token
    assert env["CUBICLE_COLLECTIONS_TOKEN"] == agent.proxy_credentials.collections_token
    assert "another-office" not in env.values()
    assert "host-token" not in env.values()
    assert registry.resolve(env["CUBICLE_TOOL_PROXY_TOKEN"]) is not None
    supervisor.set_office_tool_secret("host-direct-credential")
    assert supervisor._build_subprocess_env()["CUBICLE_OFFICE_TOOL_SECRET"] == "host-direct-credential"
    for name, value in env.items():
        if name.startswith("CUBICLE_"):
            monkeypatch.setenv(name, value)
    monkeypatch.setenv("CUBICLE_OFFICE_TOOL_SECRET", "host-direct-credential")
    config = build_mcp_config(SimpleNamespace(office_id="office-test", backend_url="http://backend", agent_name="engineer"), "worker")
    assert "OFFICE_TOOL_SECRET" not in config["mcpServers"]["cubicle-tools"]["env"]


@pytest.mark.parametrize("reason", ["stopped", "superseded", "dead", "completion", "suppressed"])
def test_live_credential_does_not_outlive_execution_authority(reason):
    supervisor, registry, agent, env = bound_supervisor()
    if reason == "stopped":
        agent.stop_requested = True
    elif reason == "superseded":
        supervisor._agents[agent.agent_name] = AgentProcess(agent_name=agent.agent_name, role="worker")
    elif reason == "dead":
        agent.process.returncode = 0
    elif reason == "completion":
        agent.pending_completion = {"status": "review"}
    else:
        supervisor.suppress_task("task-a")
    assert registry.resolve(env["CUBICLE_TOOL_PROXY_TOKEN"]) is None
    assert registry.resolve(env["CUBICLE_COLLECTIONS_TOKEN"], collections=True) is None


async def test_uncertain_process_cleanup_still_revokes_credentials(monkeypatch):
    supervisor, registry, agent, env = bound_supervisor()
    monkeypatch.setattr("src.docker.task_process_cleanup.terminate_worker_execution", AsyncMock(side_effect=RuntimeError("unconfirmed")))
    with pytest.raises(RuntimeError, match="unconfirmed"):
        await supervisor._cleanup_execution(agent)
    assert agent.cleanup_failed
    assert registry.resolve(env["CUBICLE_TOOL_PROXY_TOKEN"]) is None
    assert agent.proxy_credentials is None


def test_consult_and_manager_identities_preserve_roles_without_fake_task_claims():
    supervisor = AgentSupervisor("unused", "office-test")
    consult = AgentProcess(agent_name="planner", role="worker", execution_task_id="planner-synthetic", execution_mode="execute")
    caller = supervisor._proxy_identity(consult, {"planner_consult": {"_infra_refire": True}})
    assert caller == {"agent_name": "planner", "role": "worker", "task_mode": "execute", "task_id": "planner-synthetic", "consult_refire": True}
    manager = AgentProcess(agent_name="manager", role="manager")
    assert supervisor._proxy_identity(manager, {}) == {"agent_name": "manager", "role": "manager", "task_mode": "manager"}
