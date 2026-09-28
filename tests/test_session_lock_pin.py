"""T5.1.4 (06/I-9) — the Manager prompt's session-lock trigger set must
match the code constant.

The MCP server locks the per-turn terminal action on a specific set of
``move_task`` statuses; the Manager CLAUDE.md describes the same lock. They
drifted (prompt said {done, ready, blocked}; code locks (done, ready)),
which could make the Manager believe its tools are dead after a manual
``move_task → blocked`` and skip the mandatory blocking-cause comment. This
pins both sides to one source of truth.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from src.config_sync.claude_md_templates._manager import MANAGER_CLAUDE_MD

_MCP_PATH = (
    Path(__file__).resolve().parent.parent
    / "src" / "_agent_image" / "mcp_tool_server.py"
)
_spec = importlib.util.spec_from_file_location("mcp_tool_server", _MCP_PATH)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _lock_section() -> str:
    # The raw template uses {{ }} escaping; render with empty fields so the
    # lock line reads as the agent will see it.
    rendered = MANAGER_CLAUDE_MD.replace("{office_name}", "X")
    start = rendered.index("## Per-Turn Session Lock")
    end = rendered.index("## ", start + 1)
    return rendered[start:end]


def test_lock_section_lists_exactly_the_code_constant():
    section = _lock_section()
    for status in _mod.SESSION_LOCK_MOVE_STATUSES:
        assert f"`{status}`" in section, (
            f"lock status {status!r} missing from the Manager prompt"
        )


def test_lock_section_excludes_blocked_from_the_trigger_enumeration():
    # ``blocked`` must NOT appear in the ``new_status in {...}`` enumeration
    # (the lock-trigger list). Explanatory prose elsewhere may mention it.
    section = _lock_section()
    after = section.split("new_status` in", 1)[1]
    enumeration = after.split("via the Manager", 1)[0]
    assert "blocked" not in enumeration.lower()


def test_code_constant_is_done_ready():
    assert _mod.SESSION_LOCK_MOVE_STATUSES == ("done", "ready")
    assert _mod.SESSION_LOCK_STATUS_UPDATE_STATUSES == ("review", "blocked")


# ── X45: the section must name EVERY Manager-mode turn-ending trigger ──
#
# The playbook introduces its list with "After you call any of:" and the
# eval docstring called it exhaustive, but only move_task and
# ask_user_choice were pinned — propose_configuration (which PRE-LOCKs the
# Manager turn too) had already drifted out. The trigger set is DERIVED
# here by executing every Manager-catalog tool against the real server in
# manager mode with a stubbed backend, so a new PRE-LOCK branch fails this
# test until the playbook names it.


def _manager_lock_triggers(monkeypatch) -> set[tuple[str, str | None]]:
    import asyncio
    from unittest.mock import AsyncMock

    from src._agent_image import mcp_tool_server as server_module
    from src._agent_image._mcp.tools_manager import get_manager_tools

    monkeypatch.setattr(server_module, "TASK_MODE", "manager")
    monkeypatch.setattr(server_module, "CONTEXT_KEY", "workstream:lock-pin")
    monkeypatch.setenv("CONTEXT_KEY", "workstream:lock-pin")
    monkeypatch.setattr(
        server_module, "_call_backend", AsyncMock(return_value={"ok": True})
    )
    tools = get_manager_tools()
    triggers: set[tuple[str, str | None]] = set()
    for tool in tools:
        schema = (tool.get("inputSchema") or {}).get("properties") or {}
        statuses = (schema.get("new_status") or {}).get("enum") or [None]
        for status in statuses:
            server = server_module.MCPServer(tools)
            arguments = {"task_id": "00000000-0000-0000-0000-000000000001"}
            if status is not None:
                arguments["new_status"] = status
            asyncio.run(server._execute_tool(tool["name"], arguments))
            if server._session_locked:
                triggers.add((tool["name"], status))
    return triggers


def test_derived_manager_lock_set_is_what_the_section_names(monkeypatch):
    triggers = _manager_lock_triggers(monkeypatch)
    # The code's Manager-mode trigger set today.
    assert triggers == {
        ("move_task", "done"),
        ("move_task", "ready"),
        ("ask_user_choice", None),
        ("propose_configuration", None),
    }
    section = _lock_section()
    for tool, status in triggers:
        assert f"`{tool}`" in section, f"lock trigger {tool} missing from the playbook"
        if status is not None:
            assert f"`{status}`" in section


def test_manager_turn_ending_actions_constant():
    # X45: the Manager-mode PRE-LOCK triggers beyond move_task live in ONE
    # constant; each carries its own lock reason.
    assert _mod.MANAGER_TURN_ENDING_ACTIONS == (
        "ask_user_choice",
        "propose_configuration",
    )
    assert set(_mod._MANAGER_TURN_END_REASONS) == set(
        _mod.MANAGER_TURN_ENDING_ACTIONS
    )


def _run_manager_action(action: str, backend_result: dict, monkeypatch):
    import asyncio

    async def _fake_backend(_action, _params):
        return backend_result

    monkeypatch.setattr(_mod, "TASK_MODE", "manager")
    monkeypatch.setattr(_mod, "CONTEXT_KEY", "workstream:00000000-0000-0000-0000-000000000001")
    monkeypatch.setattr(_mod, "_call_backend", _fake_backend)
    server = _mod.MCPServer([{"name": action, "action": action}])
    result = asyncio.run(server._execute_tool(action, {}))
    return server, result


def test_manager_turn_ending_actions_lock_on_success(monkeypatch):
    for action in _mod.MANAGER_TURN_ENDING_ACTIONS:
        server, result = _run_manager_action(
            action, {"status": "ok", "choice_id": "c1"}, monkeypatch,
        )
        assert not result.get("isError"), (action, result)
        assert server._session_locked is True, action
        assert server._lock_reason == _mod._MANAGER_TURN_END_REASONS[action]


def test_manager_turn_ending_actions_unlock_on_failure(monkeypatch):
    for action in _mod.MANAGER_TURN_ENDING_ACTIONS:
        server, result = _run_manager_action(
            action, {"error": "refused"}, monkeypatch,
        )
        assert result.get("isError") is True, action
        assert server._session_locked is False, action


def test_terminal_block_keeps_the_office_secret_names_warning(monkeypatch):
    """The terminal rewrite ("Session complete") must not swallow the
    backend's warning that a block's secret names were not recorded."""
    import asyncio
    import json

    warning = (
        "The task is blocked, but office_secret_names CRM_API_KEY were not "
        "recorded: another pending escalation keeps this task's decision."
    )

    async def _fake_backend(_action, _params):
        return {"new_status": "blocked", "office_secret_names_warning": warning}

    monkeypatch.setattr(_mod, "TASK_MODE", "execute")
    monkeypatch.setattr(_mod, "_call_backend", _fake_backend)
    server = _mod.MCPServer([{"name": "update_status", "action": "task_status_update"}])
    result = asyncio.run(
        server._execute_tool(
            "update_status",
            {
                "task_id": "00000000-0000-0000-0000-000000000001",
                "new_status": "blocked",
                "office_secret_names": ["CRM_API_KEY"],
            },
        )
    )
    assert server._session_locked is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "complete"
    assert payload["office_secret_names_warning"] == warning
