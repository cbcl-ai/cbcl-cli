"""``select_session_tools`` is the ONE catalog-selection path (F06 A).

``mcp_tool_server.main()`` registers exactly what ``select_session_tools``
returns, and the behavioral evals import the same function, so an eval can
never offer the model a friendlier hand-picked subset than production. These
tests pin the selection against the pre-refactor composition rules
(role → catalog, ASD-only authoring strip, General-Chat write strip).
"""

from __future__ import annotations

import pytest

from src._agent_image._mcp.tools_data_curator import get_data_curator_tools
from src._agent_image._mcp.tools_flow_architect import get_flow_architect_tools
from src._agent_image._mcp.tools_manager import get_manager_tools
from src._agent_image._mcp.tools_planner import get_planner_tools
from src._agent_image._mcp.tools_worker import get_worker_subcatalog
from src._agent_image import mcp_tool_server
from src._agent_image.mcp_tool_server import (
    filter_general_chat_tools,
    filter_script_author_tools,
    select_session_tools,
)


def _names(tools: list[dict]) -> list[str]:
    return [tool["name"] for tool in tools]


def _legacy_selection(role, agent_name, task_mode, task_class, context_key):
    """The composition ``main()`` performed inline before the extraction."""
    if role == "manager":
        tools = get_manager_tools()
    elif agent_name == "planner":
        tools = get_planner_tools()
    elif agent_name == "flow-architect":
        tools = get_flow_architect_tools()
    elif agent_name == "data-curator":
        tools = get_data_curator_tools()
    else:
        tools = get_worker_subcatalog(task_mode, agent_name, task_class or None)
    if role == "worker":
        tools = filter_script_author_tools(tools, agent_name)
    if role == "manager" and context_key == "general_chat":
        tools = filter_general_chat_tools(tools)
    return tools


CASES = [
    ("manager", "", "manager", None, "general_chat"),
    ("manager", "", "manager", None, "workstream:11111111-1111-1111-1111-111111111111"),
    ("worker", "planner", "execute", None, ""),
    ("worker", "flow-architect", "execute", None, ""),
    ("worker", "data-curator", "execute", None, ""),
    ("worker", "automation-script-developer", "execute", None, ""),
    ("worker", "finance-analyst", "execute", None, ""),
    ("worker", "finance-analyst", "execute", "ask", ""),
    ("worker", "auditor", "review", None, ""),
    ("worker", "manager-assistant", "triage", None, ""),
    ("worker", "manager-assistant", "review", None, ""),
    ("worker", "", "execute", None, ""),
]


@pytest.mark.parametrize("role,agent,mode,task_class,context", CASES)
def test_selection_matches_the_pre_refactor_composition(
    role, agent, mode, task_class, context
):
    assert _names(select_session_tools(role, agent, mode, task_class, context)) == (
        _names(_legacy_selection(role, agent, mode, task_class, context))
    )


def test_general_chat_manager_loses_board_writes_but_workstream_keeps_them():
    general = set(_names(select_session_tools("manager", "", "manager", None, "general_chat")))
    workstream = set(_names(select_session_tools(
        "manager", "", "manager", None, "workstream:abc",
    )))
    assert "create_task" in workstream and "create_task" not in general
    assert "get_board" in general


def test_only_the_script_developer_keeps_authoring_tools():
    authoring = {"register_script", "clone_script"}
    asd = set(_names(select_session_tools(
        "worker", "automation-script-developer", "execute",
    )))
    other = set(_names(select_session_tools("worker", "finance-analyst", "execute")))
    empty = set(_names(select_session_tools("worker", "", "execute")))
    assert authoring <= asd
    assert not authoring & other
    assert not authoring & empty


def test_selection_is_pure_and_does_not_read_the_process_environment(monkeypatch):
    # main() passes its env values in; the function itself must not consult
    # module-level env snapshots (an eval passes different identities).
    monkeypatch.setattr(mcp_tool_server, "AGENT_NAME", "automation-script-developer")
    monkeypatch.setattr(mcp_tool_server, "CONTEXT_KEY", "general_chat")
    monkeypatch.setattr(mcp_tool_server, "TASK_MODE", "review")
    tools = _names(select_session_tools(
        "manager", "", "manager", None, "workstream:abc",
    ))
    assert "create_task" in tools


def test_main_registers_exactly_the_selected_catalog(monkeypatch):
    captured = {}

    class _Server:
        def __init__(self, tools):
            captured["tools"] = tools

        async def run(self):
            return None

    monkeypatch.setattr(mcp_tool_server, "MCPServer", _Server)
    monkeypatch.setattr(mcp_tool_server, "OFFICE_ID", "office-1")
    monkeypatch.setattr(mcp_tool_server, "AGENT_NAME", "auditor")
    monkeypatch.setattr(mcp_tool_server, "TASK_MODE", "review")
    monkeypatch.setattr(mcp_tool_server, "TASK_CLASS", "")
    monkeypatch.setattr(mcp_tool_server, "CONTEXT_KEY", "")
    monkeypatch.setattr("sys.argv", ["mcp_tool_server.py", "--role", "worker"])
    mcp_tool_server.main()
    assert _names(captured["tools"]) == _names(
        select_session_tools("worker", "auditor", "review", None, "")
    )


@pytest.mark.parametrize("role,agent,mode,task_class,context", CASES)
def test_receipt_sentence_names_only_a_served_write(
    role, agent, mode, task_class, context
):
    """A receipt-carrying read says which write must pass its receipt. The
    Manager holds get_spec without update_spec (Planner-only), and General
    Chat strips update_execution_plan, so the served description must not
    point at a tool the session cannot call."""
    from src._agent_image._mcp.read_receipts import (
        GUARDED_WRITES,
        read_receipt_guidance,
    )

    tools = select_session_tools(role, agent, mode, task_class, context)
    served = {tool["name"]: tool for tool in tools}
    for write, (read, _) in GUARDED_WRITES.items():
        if read not in served:
            continue
        mentions = read_receipt_guidance(read) in served[read]["description"]
        assert mentions == (write in served), (read, write, sorted(served))


def test_manager_get_spec_no_longer_names_update_spec():
    workstream = {
        tool["name"]: tool
        for tool in select_session_tools(
            "manager", "", "manager", None, "workstream:abc"
        )
    }
    general = {
        tool["name"]: tool
        for tool in select_session_tools("manager", "", "manager", None, "general_chat")
    }
    planner = {
        tool["name"]: tool
        for tool in select_session_tools("worker", "planner", "execute")
    }
    assert "update_spec" not in workstream["get_spec"]["description"]
    assert "update_execution_plan" in workstream["get_execution_plan"]["description"]
    assert "update_execution_plan" not in general["get_execution_plan"]["description"]
    assert "which update_spec must pass" in planner["get_spec"]["description"]
    assert (
        "which update_execution_plan must pass"
        in planner["get_execution_plan"]["description"]
    )
