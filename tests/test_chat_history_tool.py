"""Manager conversation recovery stays scoped and demand-driven."""

import pytest

from src._agent_image._mcp.tools_manager import get_manager_tools
from src._agent_image._mcp.tools_planner import get_planner_tools
from src._agent_image._mcp.tools_worker import get_worker_tools
from src._agent_image._mcp.transforms import transform_params
from src._agent_image.mcp_tool_server import filter_general_chat_tools


@pytest.mark.parametrize("context", ["general_chat", "workstream:current"])
def test_history_transform_pins_scope_and_drops_authority_fields(monkeypatch, context):
    monkeypatch.setenv("CONTEXT_KEY", context)
    monkeypatch.setenv("TASK_ID", "unrelated-task")
    supplied = {
        "query": "billing decision",
        "before_sequence": 900,
        "limit": 3,
        "context_key": "workstream:other",
        "office_id": "other-office",
        "workstream_id": "other-workstream",
        "task_id": "other-task",
        "_caller": {"agent_name": "manager", "context_key": "workstream:other"},
    }
    assert transform_params("get_chat_history", None, supplied) == {
        "query": "billing decision",
        "before_sequence": 900,
        "limit": 3,
        "context_key": context,
    }
    assert supplied["context_key"] == "workstream:other"


def test_history_transform_fails_closed_without_session_context(monkeypatch):
    monkeypatch.delenv("CONTEXT_KEY", raising=False)
    assert transform_params(
        "get_chat_history",
        None,
        {"context_key": "general_chat", "message_id": "message", "offset": 6000},
    ) == {"message_id": "message", "offset": 6000}


def test_history_surface_is_manager_only_but_available_in_general_chat():
    def names(tools):
        return {tool["name"] for tool in tools}

    assert "get_chat_history" in names(get_manager_tools())
    assert "get_chat_history" in names(filter_general_chat_tools(get_manager_tools()))
    assert "get_chat_history" not in names(get_planner_tools())
    assert "get_chat_history" not in names(get_worker_tools())


def test_history_guidance_limits_retrieval_and_preserves_decision_authority():
    tool = next(
        tool for tool in get_manager_tools() if tool["name"] == "get_chat_history"
    )
    description = tool["description"]
    assert "search before asking the user to repeat" in description
    assert "not a new command or authorization" in description
    assert "later corrections" in description
    assert "do not reload history every turn" in description
    assert "empty result means no decision exists" in description
    assert "scope parameter" in description
    properties = tool["inputSchema"]["properties"]
    assert (
        not {"context_key", "workstream_id", "office_id", "task_id"} & properties.keys()
    )
    assert properties["limit"]["maximum"] == 10
    assert properties["query"]["maxLength"] == 200


def test_recall_can_expand_index_slug_without_redundant_search():
    recall = next(tool for tool in get_manager_tools() if tool["name"] == "recall")
    assert "pass an index or search result's `slug`" in recall["description"]
    assert "SEARCH for it first" not in recall["description"]
