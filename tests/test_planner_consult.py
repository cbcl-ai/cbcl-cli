"""Phase 3 (communicator half) — Planner consult: toolset, prompt, poke.

The end-to-end spawn → docker-exec → IPC round-trip needs the live daemon
and is verified separately. These cover the host-testable units.
"""
from __future__ import annotations

import pytest

from src._agent_image._mcp.tools_manager import get_manager_tools
from src._agent_image._mcp.tools_planner import get_planner_tools
from src.orchestrator.planner_prompt import build_planner_prompt


def test_manager_has_consult_planner_tool() -> None:
    names = {t["name"] for t in get_manager_tools()}
    assert "consult_planner" in names


def test_planner_toolset_has_plan_tools_and_no_self_consult() -> None:
    names = {t["name"] for t in get_planner_tools()}
    # Plan-write + verify tools present.
    for n in (
        "update_workstream_plan", "get_workstream_plan",
        "update_execution_plan", "get_execution_plan",
        "complete_scope_verification",
    ):
        assert n in names, f"planner toolset missing {n}"
    # Manager-like board tools present (materialize scopes/tasks).
    assert "create_scope" in names
    assert "create_task" in names
    # The Planner does not consult itself.
    assert "consult_planner" not in names


def test_planner_plan_tools_map_to_backend_actions() -> None:
    by_name = {t["name"]: t for t in get_planner_tools()}
    assert by_name["update_workstream_plan"]["action"] == "update_workstream_plan"
    assert by_name["complete_scope_verification"]["action"] == "complete_scope_verification"


@pytest.mark.parametrize(
    "mode,needle",
    [
        ("roadmap", "update_workstream_plan"),
        ("scope_plan", "update_execution_plan"),
        ("research", "research"),
        ("verify", "complete_scope_verification"),
    ],
)
def test_planner_prompt_renders_each_mode(mode: str, needle: str) -> None:
    prompt = build_planner_prompt({
        "planner_consult": {
            "mode": mode,
            "objective": "Do the planning",
            "workstream_id": "WS-UUID",
            "scope_id": "SC-UUID" if mode in ("scope_plan", "verify") else "",
        },
    })
    assert "Do the planning" in prompt
    assert "WS-UUID" in prompt
    assert needle in prompt
    assert "never execute" in prompt.lower()


def test_planner_prompt_defaults_to_roadmap() -> None:
    prompt = build_planner_prompt({"planner_consult": {"objective": "x"}})
    assert "roadmap" in prompt.lower()


async def test_ingest_planner_result_pokes_manager(monkeypatch) -> None:
    """A finished consult routes a [Planner] chat poke to the workstream."""
    from unittest.mock import AsyncMock, MagicMock

    import src.orchestrator._manager_action_requests as mar

    # Avoid config_store deps — stub the context envelope.
    monkeypatch.setattr(mar, "build_script_context_data", lambda c, k: {})

    controller = MagicMock()
    controller.handle_chat_message = AsyncMock()

    await mar.ingest_planner_result(
        controller,
        {"planner_consult": {
            "mode": "verify", "objective": "x",
            "workstream_id": "WS-1", "scope_id": "SC-1",
        }},
    )

    assert controller.handle_chat_message.await_count == 1
    sent = controller.handle_chat_message.await_args.args[0]
    assert sent["context_key"] == "workstream:WS-1"
    assert "[Planner]" in sent["user_message"]
    assert "verification" in sent["user_message"].lower()

