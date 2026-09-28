"""The model must SEE a failed tool call's recovery guidance (X60).

The MCP server used to render every error as ``Error: {message}``, dropping
``retry_after_seconds`` / ``retry_same_operation_key`` / ``retryable`` and
backend ``code`` values — the only retry guidance a capacity refusal or a
stale-execution result carries. A 409 capacity refusal also read like an
accepted queue ("Managed operation queued …"), so a worker could end its
session as if the run had been handed off.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.agent_image_stub import stubbed_mcp_script_exec


@pytest.fixture(scope="module")
def mcp_script_exec():
    with stubbed_mcp_script_exec() as module:
        yield module


_MCP_PATH = (
    Path(__file__).resolve().parent.parent
    / "src" / "_agent_image" / "mcp_tool_server.py"
)
_spec = importlib.util.spec_from_file_location("mcp_tool_server_errors", _MCP_PATH)
assert _spec and _spec.loader
_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_server)


def test_error_text_keeps_recovery_fields_and_code() -> None:
    text = _server.format_error_text({
        "error": "Retry superseded this attempt",
        "code": "stale_execution",
    })
    assert text.startswith("Error: Retry superseded this attempt")
    assert '"code": "stale_execution"' in text

    capacity = _server.format_error_text({
        "error": True,
        "message": "Not accepted",
        "retryable": True,
        "retry_after_seconds": 30,
        "retry_same_operation_key": True,
    })
    assert '"retry_after_seconds": 30' in capacity
    assert '"retry_same_operation_key": true' in capacity
    assert '"retryable": true' in capacity


def test_error_text_bounds_details() -> None:
    text = _server.format_error_text({
        "error": "invalid decide payload",
        "details": [{"loc": ["decision"], "msg": "x" * 5000}],
    })
    assert "Details: " in text
    assert text.endswith("…(truncated)")
    assert len(text) < 2300


def test_plain_error_is_unchanged() -> None:
    assert _server.format_error_text({"error": "boom"}) == "Error: boom"


def test_capacity_refusal_says_not_accepted_and_same_key(mcp_script_exec) -> None:
    result = mcp_script_exec.capacity_refusal_result(
        {
            "error": "operation_capacity_wait",
            "message": "Managed operation queued for shared host/service capacity; no process started",
            "retryable": True,
            "retry_after_seconds": 45,
        },
        retry_hint="retry execute_script with the SAME operation key",
    )
    assert result["error"] is True
    assert result["code"] == "operation_capacity_wait"
    assert result["retry_after_seconds"] == 45
    assert result["retry_same_operation_key"] is True
    message = result["message"]
    assert message.startswith("Not accepted")
    assert "NOT a handoff" in message
    assert "SAME operation key after 45 s" in message
    # The host's "Managed operation queued …" text reads like an accepted
    # handoff; it must never be echoed into the refusal.
    assert "queued" not in message.lower()
    assert message.count("No process started") == 1
    assert "no process started" not in message


def test_capacity_refusal_clamps_bad_delay(mcp_script_exec) -> None:
    result = mcp_script_exec.capacity_refusal_result(
        {"retry_after_seconds": "soon"}, retry_hint="retry the same call",
    )
    assert result["retry_after_seconds"] == 30


@pytest.mark.asyncio
async def test_operation_capacity_refusal_is_explicit(mcp_script_exec, monkeypatch) -> None:
    import sys

    monkeypatch.setattr(mcp_script_exec, "TOOL_PROXY_URL", "http://host.invalid")
    monkeypatch.setattr(mcp_script_exec, "TASK_ID", "task")
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
    result = await mcp_script_exec._operation_call("reconcile", {"operation_id": "op"})
    assert result["retryable"] is True and result["retry_after_seconds"] == 30
    assert "reconcile_operation for the SAME operation_id" in result["message"]


def test_model_visible_text_of_a_capacity_refusal(monkeypatch) -> None:
    """End to end through ``_execute_tool``: the text the model receives."""

    async def _refuse(_params):
        return {
            "error": True,
            "code": "operation_capacity_wait",
            "retryable": True,
            "retry_after_seconds": 30,
            "retry_same_operation_key": True,
            "message": "Not accepted: no process started. Retry execute_script with the SAME operation key after 30 s.",
        }

    monkeypatch.setattr(_server, "_execute_script", _refuse)
    monkeypatch.setattr(_server, "TASK_MODE", "execute")
    server = _server.MCPServer([
        {"name": "execute_script", "action": "script_execute", "local": True},
    ])
    result = asyncio.run(server._execute_tool(
        "execute_script",
        {"script_name": "export", "operation": {"key": "k", "input_fingerprint": "a" * 64}},
    ))
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "Not accepted" in text
    assert '"retry_after_seconds": 30' in text
    assert '"retry_same_operation_key": true' in text
    assert server._session_locked is False


def _host_proxy_module(mcp_script_exec, monkeypatch, tmp_path):
    module = mcp_script_exec
    monkeypatch.setattr(module, "TOOL_PROXY_URL", "http://host.invalid")
    monkeypatch.setattr(module, "TASK_ID", "bound-task")
    monkeypatch.setattr(module, "TASK_MODE", "execute")
    monkeypatch.setattr(module, "Path", lambda path: tmp_path)
    monkeypatch.setattr(module, "_task_launch_refusal", AsyncMock(return_value=None))
    monkeypatch.setattr(module, "_check_bootstrap_status", AsyncMock(return_value=None))
    monkeypatch.setattr(module, "_parse_manifest", lambda path: {})
    import sys

    monkeypatch.setattr(
        sys.modules["_mcp_backend"], "_caller_envelope", lambda: {}, raising=False
    )
    return module


@pytest.mark.asyncio
async def test_execute_script_409_capacity_is_a_refusal_with_same_key(
    mcp_script_exec, monkeypatch, tmp_path
) -> None:
    import json

    module = _host_proxy_module(mcp_script_exec, monkeypatch, tmp_path)
    response = MagicMock(status=409)
    response.text = AsyncMock(return_value=json.dumps({
        "error": "operation_capacity_wait",
        "message": "Managed operation queued for shared host/service capacity; no process started",
        "retryable": True,
        "retry_after_seconds": 45,
    }))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post.return_value = context
    monkeypatch.setattr(module, "_get_session", AsyncMock(return_value=session))
    result = await module._execute_script({
        "script_name": "export",
        "operation": {"key": "k", "input_fingerprint": "a" * 64},
    })
    assert result["error"] is True
    assert "accepted_wait" not in result and "execution_id" not in result
    assert result["retry_after_seconds"] == 45
    assert result["retry_same_operation_key"] is True
    assert "SAME operation key after 45 s" in result["message"]
    assert "NOT a handoff" in result["message"]
    text = _server.format_error_text(result)
    assert '"retry_after_seconds": 45' in text
    assert "queued" not in text.lower()


@pytest.mark.asyncio
async def test_proxy_unreachable_uses_the_one_call_blocker_protocol(
    mcp_script_exec, monkeypatch, tmp_path
) -> None:
    import aiohttp

    module = _host_proxy_module(mcp_script_exec, monkeypatch, tmp_path)
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    session = MagicMock()
    session.post.side_effect = aiohttp.ClientConnectionError("refused")
    monkeypatch.setattr(module, "_get_session", AsyncMock(return_value=session))
    result = await module._execute_script({"script_name": "export"})
    assert result["error"] is True
    message = result["message"]
    # F01 design F: consistent with the ASD escalation rule — block the
    # task with ONE update_status(blocked) ESCALATED (external_outage)
    # call; never the old "don't escalate until the operator confirms".
    assert "Don't escalate" not in message
    assert "ESCALATED (external_outage):" in message
    assert 'ONE `update_status(new_status="blocked")` call' in message
    assert "Do NOT retry until the operator confirms" in message
    assert session.post.call_count == 3


@pytest.mark.asyncio
async def test_proxy_unreachable_in_triage_escalates_instead_of_blocking(
    mcp_script_exec, monkeypatch, tmp_path
) -> None:
    """B3-hygiene-2: triage cannot re-block its own task, so the refusal
    names escalate_blocker (triage path C), never update_status/move_task."""
    import aiohttp

    module = _host_proxy_module(mcp_script_exec, monkeypatch, tmp_path)
    monkeypatch.setattr(module, "TASK_MODE", "triage")
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    session = MagicMock()
    session.post.side_effect = aiohttp.ClientConnectionError("refused")
    monkeypatch.setattr(module, "_get_session", AsyncMock(return_value=session))
    message = (await module._execute_script({"script_name": "export"}))["message"]
    assert 'File `escalate_blocker(blocker_class="external_outage")`' in message
    assert "update_status" not in message and "move_task" not in message
    assert "Do NOT retry until the operator confirms" in message
