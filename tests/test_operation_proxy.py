"""Operation tools use the live host identity and real HTTP route."""

from unittest.mock import AsyncMock, MagicMock
import uuid
import aiohttp
import pytest

from src.runtime_state import RuntimeState
from src.operations.host_capacity import HostCapacityUnavailable
from src.tool_proxy_server import ToolProxyServer
from tests.test_mcp_script_exec_bindings import mcp_script_exec  # noqa: F401


@pytest.fixture
async def proxy_with_runner():
    runner = MagicMock(execute=AsyncMock())
    server = ToolProxyServer(MagicMock(connected=True), port=0, host="127.0.0.1", script_runner=runner)
    await server.start()
    try:
        yield server, runner
    finally:
        await server.stop()


async def _post(server, path, body, *, token):
    async with aiohttp.ClientSession() as session:
        async with session.post(f"http://127.0.0.1:{server.port}{path}", json=body,
                                headers={"Authorization": f"Bearer {token}"}) as response:
            return response.status, await response.json()


def bind(server, tmp_path):
    state = RuntimeState(tmp_path / "runtime.sqlite", "office")
    server.set_runtime_state(state)
    caller = {"role": "worker", "agent_name": "analyst", "task_id": "task", "task_mode": "execute",
              "execution_cycle": 1, "attempt_id": "attempt"}
    server.set_execution_validator(lambda supplied, task: supplied == caller and task == "task")
    credentials = server.sessions.issue(caller, lambda: True)
    record, _ = state.begin_operation(task_id="task", cycle=1, phase="execute", key="report",
                                       fingerprint="a" * 64, script_name="report", attempt_id="attempt",
                                       mechanism="local", resources=[])
    return state, caller, credentials, record


async def test_operation_read_is_bound_to_task_and_revoked_with_session(proxy_with_runner, tmp_path):
    server, runner = proxy_with_runner
    state, caller, credentials, record = bind(server, tmp_path)
    runner.get_operation = AsyncMock(return_value=record)
    request = {"action": "get", "operation_id": record["operation_id"], "_caller": {**caller, "task_id": "forged"}}
    status, body = await _post(server, "/operations", request, token=credentials.tool_token)
    assert status == 200 and body["operation"]["operation_id"] == record["operation_id"]
    runner.get_operation.assert_awaited_once_with(record["operation_id"])
    server.sessions.revoke(credentials)
    assert (await _post(server, "/operations", request, token=credentials.tool_token))[0] == 401
    runner.get_operation.assert_awaited_once()


async def test_operation_cross_task_and_collections_tokens_are_denied(proxy_with_runner, tmp_path):
    server, runner = proxy_with_runner
    state, caller, credentials, record = bind(server, tmp_path)
    foreign, _ = state.begin_operation(task_id="another-task", cycle=1, phase="execute", key="report",
                                       fingerprint="a" * 64, script_name="report", attempt_id="other",
                                       mechanism="local", resources=[])
    request = {"action": "get", "operation_id": foreign["operation_id"]}
    assert (await _post(server, "/operations", request, token=credentials.tool_token))[0] == 404
    assert (await _post(server, "/operations", {"action": "list"}, token=credentials.collections_token))[0] == 401


async def test_capacity_refusal_has_bounded_same_intent_retry(proxy_with_runner, tmp_path):
    server, runner = proxy_with_runner
    state, caller, credentials, record = bind(server, tmp_path)
    runner.execute.side_effect = HostCapacityUnavailable("Queued behind another owned operation")
    request = {"script_name": "report", "invocation_id": str(uuid.uuid4()),
               "operation": {"key": "report", "input_fingerprint": "a" * 64}}
    status, body = await _post(server, "/script-execute-host", request, token=credentials.tool_token)
    assert status == 409 and body["retry_after_seconds"] == 30
    assert body["error"] == "operation_capacity_wait"
    # A refusal before launch releases invocation replay identity for an
    # eventual same-id transport retry; operation queue identity remains.
    assert state.get_operation(record["operation_id"]) is not None


async def test_operation_tool_catalog_and_local_dispatch_are_wired():
    from src._agent_image._mcp.tools_worker import get_worker_tools
    from pathlib import Path

    catalog = {tool["name"]: tool for tool in get_worker_tools()}
    for name in ("get_operation", "list_operations", "cancel_operation", "reconcile_operation"):
        assert catalog[name]["local"] is True
        assert catalog[name]["action"].startswith("operation_")
    assert "operation" in catalog["execute_script"]["inputSchema"]["properties"]
    dispatch = (Path(__file__).parents[1] / "src/_agent_image/mcp_tool_server.py").read_text()
    assert 'action.startswith("operation_")' in dispatch


async def test_reconciliation_capacity_refusal_retains_typed_retry(proxy_with_runner, tmp_path):
    server, runner = proxy_with_runner
    _, _, credentials, record = bind(server, tmp_path)
    runner.control_operation = AsyncMock(side_effect=HostCapacityUnavailable("Observer capacity unavailable"))
    status, body = await _post(server, "/operations", {
        "action": "reconcile", "operation_id": record["operation_id"],
    }, token=credentials.tool_token)
    assert status == 409 and body["error"] == "operation_capacity_wait"
    assert body["retry_after_seconds"] == 30 and body["retryable"] is True


async def test_capacity_retry_survives_in_container_operation_tool(mcp_script_exec, monkeypatch):
    import sys

    module = mcp_script_exec
    monkeypatch.setattr(module, "TOOL_PROXY_URL", "http://host.invalid")
    monkeypatch.setattr(module, "TASK_ID", "task")
    response = MagicMock(status=409)
    response.json = AsyncMock(return_value={
        "error": "operation_capacity_wait", "retryable": True, "retry_after_seconds": 30,
    })
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post.return_value = context
    backend = sys.modules["_mcp_backend"]
    monkeypatch.setattr(backend, "_get_session", AsyncMock(return_value=session))
    monkeypatch.setattr(backend, "_caller_envelope", lambda: {}, raising=False)
    result = await module._operation_call("reconcile", {"operation_id": "existing"})
    assert result["error"] is True and result["retry_after_seconds"] == 30
