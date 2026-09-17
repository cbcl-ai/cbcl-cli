"""Configuration tools are orchestration proposals, never machine consent."""
from src._agent_image._mcp.tools_manager import get_manager_tools
from src._agent_image._mcp.tools_planner import get_planner_tools
from src._agent_image._mcp.tools_worker import get_worker_tools
from src.config_sync.claude_md_templates._manager import MANAGER_CLAUDE_MD


def test_configuration_tools_are_manager_only():
    names = {"inspect_configuration", "propose_configuration"}
    assert names <= {t['name'] for t in get_manager_tools()}
    assert not names & {t['name'] for t in get_worker_tools()}
    assert not names & {t['name'] for t in get_planner_tools()}
    assert not any('approve_configuration' == t['name'] for t in get_manager_tools())


def test_stewardship_preserves_consent_and_quality():
    for invariant in ('repeated concrete task evidence', 'Only its authenticated approval applies it',
                      'Preserve domain/quality/security requirements', 'Existing task briefs and running sessions keep their contracts',
                      'seven-day cooldown', 'never blindly restore'):
        assert invariant in MANAGER_CLAUDE_MD


def test_proposal_context_comes_from_session(monkeypatch):
    from src._agent_image._mcp import transforms
    monkeypatch.setenv("CONTEXT_KEY", "general_chat")
    result = transforms.transform_params("propose_configuration", None, {"context_key": "workstream:forged", "title": "Improve CI"})
    assert result["context_key"] == "general_chat"
    monkeypatch.delenv("CONTEXT_KEY")
    assert transforms.transform_params("propose_configuration", None, {"context_key": "forged"})["context_key"] == ""


def test_successful_proposal_ends_turn_and_failure_allows_retry(monkeypatch):
    import asyncio
    from src._agent_image import mcp_tool_server as server_module
    from unittest.mock import AsyncMock
    monkeypatch.setattr(server_module, "TASK_MODE", "manager")
    monkeypatch.setattr(server_module, "CONTEXT_KEY", "general_chat")
    monkeypatch.setenv("CONTEXT_KEY", "general_chat")
    backend = AsyncMock(return_value={"proposal_id": "proposal", "status": "pending"})
    monkeypatch.setattr(server_module, "_call_backend", backend)
    server = server_module.MCPServer(get_manager_tools())
    first = asyncio.run(server._execute_tool("propose_configuration", {"title": "CI policy"}))
    assert not first.get("isError")
    assert server._session_locked
    assert "proposal" in first["content"][0]["text"]
    assert asyncio.run(server._execute_tool("get_board", {})).get("isError")
    backend.return_value = {"error": "Settings changed; inspect again"}
    retry_server = server_module.MCPServer(get_manager_tools())
    assert asyncio.run(retry_server._execute_tool("propose_configuration", {})).get("isError")
    assert not retry_server._session_locked
