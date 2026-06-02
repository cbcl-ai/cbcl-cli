"""ADD-A6: Manager-Assistant quick-decision tool-call budget (triage / MA review).

A generous code ceiling that stops a runaway MA spraying tool calls, WITHOUT
budgeting designated reviewers (who legitimately read many deliverables).
"""
from __future__ import annotations

import pytest

import src._agent_image.mcp_tool_server as mcp_mod
from src._agent_image.mcp_tool_server import MCPServer


def test_budget_applies_only_to_ma_quick_modes(monkeypatch):
    # MA triaging a blocked task → budgeted.
    monkeypatch.setattr(mcp_mod, "TASK_MODE", "triage")
    monkeypatch.setattr(mcp_mod, "AGENT_NAME", "manager-assistant")
    assert MCPServer([])._ma_budget_applies is True

    # MA reviewing → budgeted.
    monkeypatch.setattr(mcp_mod, "TASK_MODE", "review")
    assert MCPServer([])._ma_budget_applies is True

    # Designated reviewer (custom agent) in review mode → NOT budgeted.
    monkeypatch.setattr(mcp_mod, "AGENT_NAME", "auditor")
    assert MCPServer([])._ma_budget_applies is False

    # Executor → NOT budgeted.
    monkeypatch.setattr(mcp_mod, "TASK_MODE", "execute")
    monkeypatch.setattr(mcp_mod, "AGENT_NAME", "auditor")
    assert MCPServer([])._ma_budget_applies is False


@pytest.mark.asyncio
async def test_budget_locks_session_past_ceiling(monkeypatch):
    monkeypatch.setenv("CUBICLE_MA_TRIAGE_TOOL_BUDGET", "3")
    # M1 fix: the budget is counted AFTER the unknown-tool check, so the test
    # must register a KNOWN tool for the budget block to be reached.
    tool = {
        "name": "mcp__cubicle-tools__get_task_detail",
        "action": "get_task_detail",
    }
    server = MCPServer([tool])
    server._ma_budget_applies = True
    server._tool_call_count = 3  # ceiling already consumed

    res = await server._execute_tool(
        "mcp__cubicle-tools__get_task_detail", {}
    )

    assert res.get("isError") is True
    assert "budget" in res["content"][0]["text"].lower()
    assert server._session_locked is True


@pytest.mark.asyncio
async def test_budget_not_enforced_when_not_applicable(monkeypatch):
    """A non-budgeted session never trips the ceiling (counter stays 0)."""
    monkeypatch.setenv("CUBICLE_MA_TRIAGE_TOOL_BUDGET", "1")
    server = MCPServer([])
    server._ma_budget_applies = False
    server._tool_call_count = 50  # would exceed if it were enforced

    # The budget block is skipped; the call falls through to normal handling
    # (here an unknown tool → a non-budget error), and the session is NOT
    # locked by the budget.
    res = await server._execute_tool("mcp__cubicle-tools__nonexistent", {})
    assert "budget" not in res["content"][0]["text"].lower()
    assert server._session_locked is False


@pytest.mark.asyncio
async def test_unknown_tool_does_not_consume_budget(monkeypatch):
    """M1: a call refused BEFORE dispatch (unknown tool / guard) must not
    consume a budget slot."""
    monkeypatch.setenv("CUBICLE_MA_TRIAGE_TOOL_BUDGET", "5")
    server = MCPServer([])  # no tools registered
    server._ma_budget_applies = True
    server._tool_call_count = 4  # one slot left

    res = await server._execute_tool("mcp__cubicle-tools__nope", {})

    assert "Unknown tool" in res["content"][0]["text"]
    # The unknown-tool refusal returns BEFORE the budget block → no increment.
    assert server._tool_call_count == 4
    assert server._session_locked is False


def test_is_terminal_verdict_only_exempts_terminal_statuses():
    """L2/F2: only a SESSION-ENDING verdict is budget-exempt. A non-terminal
    verdict (move_task→blocked, update_status→in_progress) is NOT exempt — so a
    runaway MA can't bypass the budget by spraying non-terminal moves."""
    from src._agent_image.mcp_tool_server import _is_terminal_verdict

    # Terminal verdicts → exempt.
    assert _is_terminal_verdict("move_task", "done") is True
    assert _is_terminal_verdict("move_task", "ready") is True
    assert _is_terminal_verdict("update_status", "review") is True
    assert _is_terminal_verdict("update_status", "blocked") is True
    # Non-terminal verdicts → NOT exempt (consume budget).
    assert _is_terminal_verdict("move_task", "blocked") is False
    assert _is_terminal_verdict("move_task", "in_progress") is False
    assert _is_terminal_verdict("update_status", "in_progress") is False
    assert _is_terminal_verdict("update_status", "") is False
    # Non-verdict tools → never exempt.
    assert _is_terminal_verdict("get_task_detail", "done") is False
    assert _is_terminal_verdict("add_activity", "review") is False


@pytest.mark.asyncio
async def test_budget_wiring_non_terminal_verdict_consumes_and_locks(monkeypatch):
    """F2 WIRING coverage (not just the pure helper): in MA-review mode a
    NON-terminal move_task→blocked past the ceiling must consume budget and
    lock — exercises arguments.get('new_status') + _is_terminal_verdict at the
    call site. (Budget refuses before dispatch, so no backend is needed.)"""
    import src._agent_image.mcp_tool_server as mcp_mod

    monkeypatch.setattr(mcp_mod, "TASK_MODE", "review")
    monkeypatch.setattr(mcp_mod, "AGENT_NAME", "manager-assistant")
    monkeypatch.setenv("CUBICLE_MA_TRIAGE_TOOL_BUDGET", "1")
    server = MCPServer([
        {"name": "mcp__cubicle-tools__move_task", "action": "move_task"},
    ])
    assert server._ma_budget_applies is True
    server._tool_call_count = 1  # ceiling already consumed

    res = await server._execute_tool(
        "mcp__cubicle-tools__move_task", {"new_status": "blocked"}
    )

    assert res.get("isError") is True
    assert "budget" in res["content"][0]["text"].lower()
    assert server._session_locked is True
