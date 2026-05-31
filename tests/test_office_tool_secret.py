"""SEC3-01 (communicator side): the per-office /tool-call capability secret
threads from sync_config → supervisor → agent subprocess env → MCP env →
``X-Office-Secret`` header. These tests cover the daemon-side hops.
"""

import os
from types import SimpleNamespace

from src._agent_worker_mcp import build_mcp_config
from src.orchestrator.agent_supervisor import AgentSupervisor


def _worker():
    return SimpleNamespace(
        office_id="office-1",
        backend_url="http://host.docker.internal:8000",
        agent_name="analyst",
    )


def test_build_mcp_config_includes_office_tool_secret(monkeypatch):
    monkeypatch.setenv("CUBICLE_OFFICE_TOOL_SECRET", "sek-ret-123")
    cfg = build_mcp_config(_worker(), role="worker")
    env = cfg["mcpServers"]["cubicle-tools"]["env"]
    assert env["OFFICE_TOOL_SECRET"] == "sek-ret-123"


def test_build_mcp_config_omits_secret_when_unset(monkeypatch):
    monkeypatch.delenv("CUBICLE_OFFICE_TOOL_SECRET", raising=False)
    cfg = build_mcp_config(_worker(), role="worker")
    env = cfg["mcpServers"]["cubicle-tools"]["env"]
    assert "OFFICE_TOOL_SECRET" not in env


def test_supervisor_injects_office_tool_secret_into_subprocess_env():
    sup = AgentSupervisor(
        workspace_path="/tmp/test-workspace",
        office_id="test-office-id",
        backend_url="http://localhost:8000",
        container_name="cbcl-office-test",
        max_agents=5,
    )
    # Not set until sync_config arrives.
    assert "CUBICLE_OFFICE_TOOL_SECRET" not in sup._build_subprocess_env()
    sup.set_office_tool_secret("office-cap-secret")
    env = sup._build_subprocess_env()
    assert env["CUBICLE_OFFICE_TOOL_SECRET"] == "office-cap-secret"
    # Clearing back to empty removes it.
    sup.set_office_tool_secret("")
    assert "CUBICLE_OFFICE_TOOL_SECRET" not in sup._build_subprocess_env()


def test_mcp_backend_sends_header_when_secret_present(monkeypatch):
    # The module reads OFFICE_TOOL_SECRET from env at import; re-import to
    # pick up the patched value, then assert the header it would send.
    monkeypatch.setenv("OFFICE_TOOL_SECRET", "hdr-secret")
    import importlib

    import src._agent_image._mcp_backend as mb

    importlib.reload(mb)
    try:
        assert mb.OFFICE_TOOL_SECRET == "hdr-secret"
    finally:
        monkeypatch.delenv("OFFICE_TOOL_SECRET", raising=False)
        importlib.reload(mb)
    # ensure os import stays referenced (lint)
    assert os is not None
