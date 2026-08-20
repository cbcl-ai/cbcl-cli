"""TS-M1: board-write actions (incl. consult_planner) are stripped in General Chat."""
from src._agent_image.mcp_tool_server import (
    _BOARD_WRITE_ACTIONS,
    filter_general_chat_tools,
)
from src._agent_image._mcp.tools_manager import get_manager_tools


def test_consult_planner_is_a_board_write_stripped_in_general_chat() -> None:
    # consult_planner engages the Planner against a workstream — it must be
    # treated as a workstream-planning write and stripped in General Chat.
    assert "consult_planner" in _BOARD_WRITE_ACTIONS
    # Sanity: the other planning/board writes remain in the set.
    for a in ("create_task", "create_scope", "activate_scope", "move_task"):
        assert a in _BOARD_WRITE_ACTIONS


def test_general_chat_strip_behavior_removes_writes_keeps_reads() -> None:
    """Exercise the actual strip FUNCTION main() applies
    (``filter_general_chat_tools``) against the REAL Manager toolset — not
    just set membership. Catches the regression where a board write is added
    to the toolset but forgotten in _BOARD_WRITE_ACTIONS."""
    tools = get_manager_tools()
    actions = {t.get("action") for t in tools}
    # The Manager really exposes these, so the strip has something to act on.
    assert "consult_planner" in actions
    assert "create_task" in actions
    assert "get_board" in actions

    surviving = {t.get("action") for t in filter_general_chat_tools(tools)}

    # Writes (incl. consult_planner) are gone; reads survive.
    for stripped in ("consult_planner", "create_task", "create_scope", "move_task"):
        assert stripped not in surviving
    for kept in ("get_board", "get_task_detail"):
        assert kept in surviving


# The genuine READ-ONLY manager actions (safe in General Chat). Everything else
# a manager tool exposes MUST be a board/planning write in _BOARD_WRITE_ACTIONS.
_READ_ONLY_MANAGER_ACTIONS = {
    "get_board",
    "get_task_detail",
    "get_scope",
    "list_scopes",
    "get_execution_plan",
    "get_spec",
    "list_agents",
    "kb_search",
    "kb_get_document",
    "list_scripts",
    "get_script",
    "list_script_executions",
    "list_script_templates",
    "get_script_template",
    "list_office_secrets",
    "list_office_secret_usage",
    "office_list_files",
    "office_get_file",
    # Pivot-3 P2-2: listing standing schedules is a pure read (optionally
    # filtered by workstream) — safe in General Chat; the three schedule
    # WRITES are workstream-scoped and live in _BOARD_WRITE_ACTIONS.
    "list_assignment_schedules",
    # Flow Studio (FS-P2.T9): reading one run's status is a pure read —
    # safe in General Chat; start/stop are workstream-scoped writes and
    # live in _BOARD_WRITE_ACTIONS.
    "get_flow_run",
    # ui-ux-aug19 D4.7: the Manager's collection reads are pure reads
    # (rows live on the user's machine; writes stay Curator-consult
    # surface) — safe in General Chat.
    "get_collection",
    "query_rows",
}


def test_approve_spec_is_stripped_in_general_chat() -> None:
    # TOOL-01/MGR-05 regression: approving a spec is a workstream-state write —
    # it must not survive the General-Chat strip.
    assert "approve_spec" in _BOARD_WRITE_ACTIONS
    surviving = {
        t.get("action") for t in filter_general_chat_tools(get_manager_tools())
    }
    assert "approve_spec" not in surviving


def test_manager_prompt_gc_strip_claims_match_code() -> None:
    """MGR-05: the Manager template's 'General Chat Tool Restrictions' section
    names specific tools as stripped-writes vs surviving-reads. Pin those
    claims to _BOARD_WRITE_ACTIONS so the prose can't drift from the guard
    (it previously understated the set, omitting consult_planner/approve_spec/
    decide_action_request/retry_blocked_task)."""
    from src.config_sync.claude_md_content import MANAGER_CLAUDE_MD

    section = MANAGER_CLAUDE_MD.split("General Chat Tool Restrictions", 1)[1]
    section = section.split("\n## ", 1)[0]
    # Writes the template must name as stripped — each MUST be in the guard set.
    for w in (
        "consult_planner",
        "approve_spec",
        "decide_action_request",
        "retry_blocked_task",
        "complete_scope_verification",
        # C-2 (pivot-2 review L-3): asking the user is a workstream-pinned
        # write — the prose must name it stripped, matching the guard set.
        "ask_user_choice",
        # Pivot-3 P2-2: the assignment-schedule writes are workstream-scoped
        # — the prose must name all three stripped (the list read survives).
        "schedule_assignment",
        "update_assignment_schedule",
        "delete_assignment_schedule",
        # Flow Studio (FS-P2.T9): flow-run start/stop are workstream-scoped
        # writes — the prose must name both stripped (get_flow_run survives).
        "start_flow_run",
        "stop_flow_run",
    ):
        assert f"`{w}`" in section, f"template should name {w} as a stripped write"
        assert w in _BOARD_WRITE_ACTIONS, f"{w} named as stripped but not in guard set"
    # Reads the template says survive — each must NOT be in the guard set.
    for r in ("get_board", "get_spec", "list_agents"):
        assert f"`{r}`" in section
        assert r not in _BOARD_WRITE_ACTIONS


def test_every_manager_tool_is_classified_read_or_write() -> None:
    """Fail-closed partition: EVERY manager tool action must be either a curated
    READ (allowed in General Chat) or a board/planning WRITE (stripped). A new
    tool that is neither would silently leak into General Chat — this is the
    guard that would have caught approve_spec (TOOL-01)."""
    actions = {t.get("action") for t in get_manager_tools() if t.get("action")}
    unclassified = actions - _BOARD_WRITE_ACTIONS - _READ_ONLY_MANAGER_ACTIONS
    assert not unclassified, (
        "manager tool action(s) not classified as read or write — a write "
        f"would leak into General Chat: {sorted(unclassified)}. Add each to "
        "_BOARD_WRITE_ACTIONS (if it mutates) or _READ_ONLY_MANAGER_ACTIONS "
        "(if it's a pure read)."
    )


def test_manager_prompt_rejection_markers_match_runtime_strings() -> None:
    """MGR-07: the Manager template describes the two runtime rejections
    semantically ('SESSION TERMINATED', 'DISABLED in General Chat'). Pin those
    markers to the ACTUAL strings the MCP server emits so the prose can't drift
    from the code (it previously quoted exact strings that no longer existed)."""
    import inspect
    import re

    from src.config_sync.claude_md_content import MANAGER_CLAUDE_MD
    from src._agent_image import mcp_tool_server as mts

    # Collapse whitespace so a marker that wraps across a line in the template
    # source still matches (the prose is line-wrapped).
    md = re.sub(r"\s+", " ", MANAGER_CLAUDE_MD)
    # General-Chat redirect: marker present in BOTH the emitted string + prompt.
    gc = mts._GENERAL_CHAT_REDIRECT("create_task")
    assert "DISABLED in General Chat" in gc
    assert "DISABLED in General Chat" in md
    # Session lock: the inline rejection begins "SESSION TERMINATED:".
    src = inspect.getsource(mts)
    assert "SESSION TERMINATED" in src
    assert "SESSION TERMINATED" in md
