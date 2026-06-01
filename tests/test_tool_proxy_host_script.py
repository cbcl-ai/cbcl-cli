"""Tests for the tool-proxy host-side script-execute delegation.

Locks the contract introduced when ``execute_script`` started
refusing scripts with ``from_office_secret`` references inside the
container. The MCP server now delegates to this endpoint; the
endpoint resolves office secrets on the host and runs the script
via the local ``ScriptRunner``.

These tests drive a real ``ToolProxyServer`` bound to localhost:0
and POST against it via aiohttp — closer to production than mocking
``web.Request`` directly, and works without ``pytest-aiohttp``.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from src.tool_proxy_server import ToolProxyServer


@pytest.fixture
async def proxy_with_runner():
    runner = MagicMock()
    runner.execute = AsyncMock(return_value="exec-2026-05-18T17-00-00")

    ws_client = MagicMock()
    ws_client.connected = True

    # Bind to localhost so the test doesn't leak a 0.0.0.0 listener.
    server = ToolProxyServer(
        ws_client=ws_client, port=0, host="127.0.0.1",
        script_runner=runner,
    )
    await server.start()
    try:
        yield server, runner
    finally:
        await server.stop()


@pytest.fixture
async def proxy_without_runner():
    ws_client = MagicMock()
    ws_client.connected = True
    server = ToolProxyServer(
        ws_client=ws_client, port=0, host="127.0.0.1",
    )
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def _post(server, path, body, *, token: str | None = "auto"):
    """POST helper that auto-passes the proxy's bearer token unless
    the caller explicitly overrides (used to test the 401 path)."""
    headers: dict[str, str] = {}
    if token == "auto":
        headers["Authorization"] = f"Bearer {server.token}"
    elif token is not None:
        headers["Authorization"] = f"Bearer {token}"
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{server.port}{path}",
            json=body,
            headers=headers,
        ) as resp:
            return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_script_execute_host_happy_path(proxy_with_runner):
    """Successful execute returns an ``execution_id``. Runner is
    called with the exact kwargs from the request body."""
    server, runner = proxy_with_runner
    status, body = await _post(server, "/script-execute-host", {
        "script_name": "gitlab-register",
        "variable_overrides": {"DRY_RUN": "false"},
        "task_id": "task-uuid",
        "triggered_by": "automation-script-developer",
        "workstream_short_code": "TO",
        "scope_readable_id": "TO-007.S05",
    })
    assert status == 200
    assert body == {"execution_id": "exec-2026-05-18T17-00-00"}
    runner.execute.assert_awaited_once_with(
        script_name="gitlab-register",
        variable_overrides={"DRY_RUN": "false"},
        task_id="task-uuid",
        triggered_by="automation-script-developer",
        workstream_short_code="TO",
        scope_readable_id="TO-007.S05",
    )


@pytest.mark.asyncio
async def test_script_execute_host_missing_office_secret(
    proxy_with_runner,
):
    """``MissingOfficeSecretError`` returns a typed 409 the agent
    can pattern-match on."""
    from src.scripts.script_runner import MissingOfficeSecretError
    server, runner = proxy_with_runner
    runner.execute.side_effect = MissingOfficeSecretError(
        ["OPENAI_API_KEY", "ANTHROPIC_KEY"],
        script_name="needs-keys",
    )
    status, body = await _post(server, "/script-execute-host", {
        "script_name": "needs-keys",
    })
    assert status == 409
    assert body["error"] == "missing_office_secret"
    assert sorted(body["missing"]) == ["ANTHROPIC_KEY", "OPENAI_API_KEY"]


@pytest.mark.asyncio
async def test_script_execute_host_corrupt_office_secrets(
    proxy_with_runner,
):
    # ``OfficeSecretsCorruptError`` is now an alias for
    # :class:`CorruptOfficeSecretsError` from the store module — it
    # takes a single positional message, not ``script_name``/``detail``
    # kwargs. The handler stringifies the exception into ``detail`` on
    # the response.
    from src.scripts.script_runner import OfficeSecretsCorruptError
    server, runner = proxy_with_runner
    runner.execute.side_effect = OfficeSecretsCorruptError(
        "JSONDecodeError: bad JSON in office-secrets file",
    )
    status, body = await _post(server, "/script-execute-host", {
        "script_name": "x",
    })
    assert status == 409
    assert body["error"] == "office_secrets_corrupt"
    assert "JSONDecodeError" in body["detail"]


@pytest.mark.asyncio
async def test_script_execute_host_generic_failure_carries_real_message(
    proxy_with_runner,
):
    """A generic runner failure (e.g. DepsInstallError when paramiko
    won't install) returns 500 with the REAL cause in ``body["error"]``
    — NOT an empty body. The in-container forwarder reads this field
    (``err_msg or err_kind``) so the agent sees the actual error instead
    of the literal 'unknown' that previously masked every host 500."""
    server, runner = proxy_with_runner
    runner.execute.side_effect = RuntimeError(
        "deps install failed: paramiko build error",
    )
    status, body = await _post(server, "/script-execute-host", {
        "script_name": "hetzner-ssh-check",
    })
    assert status == 500
    assert body["error"] == "deps install failed: paramiko build error"


@pytest.mark.asyncio
async def test_script_execute_host_empty_message_falls_back_to_type(
    proxy_with_runner,
):
    """An exception whose ``str()`` is empty still yields a non-empty
    ``error`` (the type name), so the agent never sees a blank cause."""
    server, runner = proxy_with_runner
    runner.execute.side_effect = RuntimeError()  # str(exc) == ""
    status, body = await _post(server, "/script-execute-host", {
        "script_name": "hetzner-ssh-check",
    })
    assert status == 500
    assert body["error"] == "RuntimeError"


@pytest.mark.asyncio
async def test_script_execute_host_script_not_found(proxy_with_runner):
    server, runner = proxy_with_runner
    runner.execute.side_effect = FileNotFoundError(
        "Script directory not found: /workspace/.scripts/ghost",
    )
    status, body = await _post(server, "/script-execute-host", {
        "script_name": "ghost",
    })
    assert status == 404
    assert body["error"] == "script_not_found"


@pytest.mark.asyncio
async def test_script_execute_host_unwired(proxy_without_runner):
    """If the proxy was constructed without a ScriptRunner (older
    cbcl release), the endpoint returns 503 with a clear message."""
    server = proxy_without_runner
    status, body = await _post(server, "/script-execute-host", {
        "script_name": "anything",
    })
    assert status == 503
    assert "ScriptRunner" in body["error"]


@pytest.mark.asyncio
async def test_script_execute_host_missing_script_name(
    proxy_with_runner,
):
    server, runner = proxy_with_runner
    status, body = await _post(server, "/script-execute-host", {})
    assert status == 400
    runner.execute.assert_not_awaited()


# ── Bearer-auth contract (W2 hardening) ───────────────────────────────


@pytest.mark.asyncio
async def test_script_execute_host_unauthorized_no_token(
    proxy_with_runner,
):
    """Missing Authorization header → 401, runner not called.

    Closes the gap where any local process on the cbcl host could
    POST to ``/script-execute-host`` and trigger script execution
    with office-secret injection.
    """
    server, runner = proxy_with_runner
    status, body = await _post(
        server, "/script-execute-host",
        {"script_name": "anything"},
        token=None,  # explicitly suppress the auto-token
    )
    assert status == 401
    assert body == {"error": "unauthorized"}
    runner.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_script_execute_host_unauthorized_wrong_token(
    proxy_with_runner,
):
    """Bearer token that doesn't match the server's mint → 401."""
    server, runner = proxy_with_runner
    status, body = await _post(
        server, "/script-execute-host",
        {"script_name": "anything"},
        token="not-the-real-token",
    )
    assert status == 401
    runner.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_call_unauthorized_no_token(proxy_with_runner):
    """Missing Authorization header on /tool-call also 401."""
    server, _ = proxy_with_runner
    status, body = await _post(
        server, "/tool-call",
        {"action": "create_task", "params": {}},
        token=None,
    )
    assert status == 401
    assert body == {"error": "unauthorized"}


@pytest.mark.asyncio
async def test_health_endpoint_does_not_require_auth(proxy_with_runner):
    """``/health`` stays open so operator probes work without the token."""
    server, _ = proxy_with_runner
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"http://127.0.0.1:{server.port}/health",
        ) as resp:
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "ok"
