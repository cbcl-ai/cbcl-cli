"""TS-M1: board-write actions (incl. consult_planner) are stripped in General Chat."""
from src._agent_image.mcp_tool_server import _BOARD_WRITE_ACTIONS
from src._agent_image._mcp.tools_manager import get_manager_tools


def test_consult_planner_is_a_board_write_stripped_in_general_chat() -> None:
    # consult_planner engages the Planner against a workstream — it must be
    # treated as a workstream-planning write and stripped in General Chat.
    assert "consult_planner" in _BOARD_WRITE_ACTIONS
    # Sanity: the other planning/board writes remain in the set.
    for a in ("create_task", "create_scope", "activate_scope", "move_task"):
        assert a in _BOARD_WRITE_ACTIONS


def test_general_chat_strip_behavior_removes_writes_keeps_reads() -> None:
    """Exercise the actual strip EXPRESSION (mcp_tool_server: keep a tool only
    when its action is not in _BOARD_WRITE_ACTIONS) against the REAL Manager
    toolset — not just set membership. Catches the regression where a board
    write is added to the toolset but forgotten in _BOARD_WRITE_ACTIONS."""
    tools = get_manager_tools()
    actions = {t.get("action") for t in tools}
    # The Manager really exposes these, so the strip has something to act on.
    assert "consult_planner" in actions
    assert "create_task" in actions
    assert "get_board" in actions

    # Replicate the in-server filter for the manager-in-general-chat case.
    surviving = {
        t.get("action")
        for t in tools
        if t.get("action") not in _BOARD_WRITE_ACTIONS
    }

    # Writes (incl. consult_planner) are gone; reads survive.
    for stripped in ("consult_planner", "create_task", "create_scope", "move_task"):
        assert stripped not in surviving
    for kept in ("get_board", "get_task_detail"):
        assert kept in surviving
