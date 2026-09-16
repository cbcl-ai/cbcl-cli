"""Real proxy requests use host identities, not container-supplied roles."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import uuid

import aiohttp
import pytest
from aiohttp import web

from src._agent_worker_mcp import build_mcp_config
from src.orchestrator.agent_supervisor import AgentProcess, AgentState, AgentSupervisor, ManagerTurnBusy
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


async def test_manager_history_context_is_host_bound_not_tool_supplied(proxy):
    server, transport, _runner = proxy
    caller = {"agent_name": "manager", "role": "manager", "task_mode": "manager"}
    credentials = server.sessions.issue(caller, lambda: True)
    context = "workstream:" + str(uuid.uuid4())
    server.sessions.bind_manager_context(credentials, context)
    status, _ = await post(server, credentials.tool_token, "/tool-call", {
        "action": "get_chat_history", "params": {"context_key": "general_chat"},
        "_caller": {**caller, "context_key": "general_chat"},
    })
    assert status == 200
    assert transport.request.await_args.kwargs["params"]["_caller"]["context_key"] == context
    server.sessions.bind_manager_context(credentials, "general_chat")
    assert server.sessions.resolve(credentials.tool_token).caller["context_key"] == "general_chat"


def test_worker_identity_cannot_be_rebound_as_manager_context():
    registry = ProxySessionRegistry()
    credentials = registry.issue(worker_caller(), lambda: True)
    with pytest.raises(ValueError):
        registry.bind_manager_context(credentials, "general_chat")
    assert "context_key" not in registry.resolve(credentials.tool_token).caller


async def test_supervisor_binds_context_from_manager_dispatch():
    supervisor = AgentSupervisor("unused", "office-test")
    registry = ProxySessionRegistry()
    supervisor.set_tool_proxy("http://proxy", "legacy", sessions=registry)
    agent = AgentProcess(
        agent_name="manager", role="manager", state=AgentState.READY,
        process=SimpleNamespace(returncode=None),
    )
    supervisor._agents["manager"] = agent
    supervisor._bind_proxy_session(agent, {}, {})
    supervisor._send_to_agent = AsyncMock()
    key = "workstream:" + str(uuid.uuid4())
    await supervisor.send_chat_to_manager({
        "context_key": key, "content": "Continue", "conversation_id": "first",
    })
    assert registry.resolve(agent.proxy_credentials.tool_token).caller["context_key"] == key
    with pytest.raises(ManagerTurnBusy):
        await supervisor.send_chat_to_manager({
            "context_key": "general_chat", "content": "Hi", "conversation_id": "second",
        })
    assert registry.resolve(agent.proxy_credentials.tool_token).caller["context_key"] == key
    supervisor._send_to_agent.assert_awaited_once()
    reader = asyncio.StreamReader()
    reader.feed_data((json.dumps({
        "type": "response_final", "context_key": key, "conversation_id": "first",
    }) + "\n").encode())
    reader.feed_eof()
    await supervisor._reader_loop("manager", reader)
    await supervisor.send_chat_to_manager({
        "context_key": "general_chat", "content": "Hi", "conversation_id": "second",
    })
    assert registry.resolve(agent.proxy_credentials.tool_token).caller["context_key"] == "general_chat"


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


def planner_proxy_identity(marker, *, agent_name="planner", role="worker", task_id="planner-synthetic"):
    supervisor = AgentSupervisor("unused", "office-test")
    agent = AgentProcess(agent_name=agent_name, role=role, execution_task_id=task_id, execution_mode="execute")
    return supervisor._proxy_identity(agent, {"planner_consult": marker})


def test_planner_consult_identity_uses_canonical_host_bound_workstream_and_scope():
    workstream_id, scope_id = uuid.uuid4(), uuid.uuid4()
    marker = {"mode": "materialize", "workstream_id": workstream_id.hex.upper(),
              "scope_id": scope_id.hex, "_infra_refire": True}
    caller = planner_proxy_identity(marker)
    assert caller["consult_mode"] == "materialize"
    assert caller["consult_workstream_id"] == str(workstream_id)
    assert caller["consult_scope_id"] == str(scope_id)
    assert caller["consult_refire"] is True
    assert "execution_generation" not in caller
    assert "attempt_id" not in caller


@pytest.mark.parametrize("marker", [
    {"mode": "materialize", "workstream_id": "bad"},
    {"mode": "materialize", "workstream_id": str(uuid.uuid4()), "scope_id": "bad"},
    {"mode": "materialize", "workstream_id": str(uuid.uuid4()), "scope_id": False},
    {"mode": "materialize", "workstream_id": str(uuid.uuid4()), "scope_id": 0},
    {"mode": [], "workstream_id": str(uuid.uuid4())},
    {"mode": " ", "workstream_id": str(uuid.uuid4())},
])
def test_malformed_planner_marker_never_widens_draft_edit_authority(marker):
    caller = planner_proxy_identity(marker)
    assert not {"consult_mode", "consult_workstream_id", "consult_scope_id"}.intersection(caller)


@pytest.mark.parametrize("agent_name,role,task_id", [
    ("engineer", "worker", "planner-synthetic"),
    ("manager", "manager", "planner-synthetic"),
    ("planner", "worker", "board-task"),
])
def test_consult_metadata_requires_actual_planner_consult(agent_name, role, task_id):
    caller = planner_proxy_identity(
        {"mode": "scope_plan", "workstream_id": str(uuid.uuid4())},
        agent_name=agent_name, role=role, task_id=task_id,
    )
    assert "consult_mode" not in caller
    assert "consult_workstream_id" not in caller


@pytest.mark.parametrize("placement", ["top", "nested", "both"])
async def test_forged_planner_scope_and_mode_cannot_override_host_consult(proxy, placement):
    server, transport, _runner = proxy
    caller = planner_proxy_identity({"mode": "scope_plan", "workstream_id": str(uuid.uuid4()), "scope_id": str(uuid.uuid4())})
    credentials = server.sessions.issue(caller, lambda: True)
    forged = {"consult_mode": "materialize", "consult_workstream_id": str(uuid.uuid4()), "consult_scope_id": str(uuid.uuid4())}
    body = {"action": "update_task", "params": {"task_id": "draft-task", "description": "Correct the draft"}}
    if placement in ("top", "both"):
        body["_caller"] = forged
    if placement in ("nested", "both"):
        body["params"]["_caller"] = forged
    status, _result = await post(server, credentials.tool_token, "/tool-call", body)
    assert status == 200
    assert transport.request.await_args.kwargs["params"]["_caller"] == caller


@pytest.mark.parametrize("mode", ["scope_plan", "materialize", "verify"])
@pytest.mark.parametrize("scope_fields", [{}, {"scope_id": None}, {"scope_id": ""}])
def test_scope_consults_cannot_receive_workstream_wide_authority(mode, scope_fields):
    caller = planner_proxy_identity({"mode": mode, "workstream_id": str(uuid.uuid4()), **scope_fields})
    assert not {"consult_mode", "consult_workstream_id", "consult_scope_id"}.intersection(caller)


def test_workstream_specify_consult_can_retain_identity_without_scope():
    workstream_id = str(uuid.uuid4())
    caller = planner_proxy_identity({"mode": "specify", "workstream_id": workstream_id})
    assert caller["consult_mode"] == "specify"
    assert caller["consult_workstream_id"] == workstream_id
    assert "consult_scope_id" not in caller
