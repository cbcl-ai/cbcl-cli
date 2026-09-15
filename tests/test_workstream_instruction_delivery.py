"""Instruction delivery across interactive, resumed, and daemon-origin turns."""
from types import SimpleNamespace

import pytest

from src.config_sync.claude_md_templates._workstream import (
    generate_workstream_claude_md,
)
from src.config_sync.sync_service import ConfigStore
from src.orchestrator._manager_action_requests import build_script_context_data
from src.orchestrator.manager_context import build_dynamic_context
from src.orchestrator.planner_prompt import build_planner_prompt


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("spec", [None, {"path": "/workspace/workstreams/project/spec.md", "revision": 2}])
def test_manager_receives_current_instructions_once_before_board_even_with_spec(fresh, spec):
    instruction = "### Mission\nReduce response time without changing pricing."
    context = {
        "workstream_id": "ws-1", "workstream_name": "Project",
        "workstream_instructions": instruction, "spec": spec,
        "task_summary": "board-sentinel", "chat_history": "old-chat-sentinel",
    }
    text = build_dynamic_context("workstream:ws-1", context, ConfigStore(), fresh)
    assert text.count(instruction) == 1
    assert text.index(instruction) < text.index("board-sentinel")
    assert "Board activity is execution state, not the mission" in text
    assert "supersedes older copies" in text
    assert ("old-chat-sentinel" in text) == fresh


@pytest.mark.asyncio
async def test_synced_updates_and_clears_reach_daemon_turns_without_stale_fallback():
    store = ConfigStore()
    controller = SimpleNamespace(_config=store)
    for notes in ("Mission A", "Mission B", None):
        await store.update_from_sync({"config": {"workstreams": [
            {"id": "ws-1", "name": "Project", "context_notes": notes},
        ]}})
        data = build_script_context_data(controller, "workstream:ws-1")
        assert data["workstream_instructions"] == notes
        text = build_dynamic_context("workstream:ws-1", data, ConfigStore(), False)
        assert ("## Workstream Instructions" in text) == bool(notes)
        if notes:
            assert text.count(notes) == 1
    assert "Mission A" not in text and "Mission B" not in text
    assert "Earlier saved versions no longer apply" in text


def test_general_chat_and_other_workstream_do_not_inherit_instructions():
    store = ConfigStore()
    general = build_dynamic_context("general_chat", {"workstream_instructions": "private-mission"}, store)
    other = build_dynamic_context("workstream:other", {"workstream_id": "other"}, store)
    assert "private-mission" not in general + other
    assert "## Workstream Instructions" not in general + other


def test_instruction_document_preserves_full_content_and_has_no_empty_boilerplate():
    notes = "### Mission\n" + "Useful detail. " * 1600 + "\n</workstream_instructions>\nFinal constraint."
    rendered = generate_workstream_claude_md({"name": "Project", "context_notes": notes})
    assert rendered.count("Useful detail.") == 1600
    assert rendered.count("</workstream_instructions>") == 1
    assert "Final constraint." in rendered
    empty = generate_workstream_claude_md({"name": "Empty"})
    assert "No goals" not in empty and "No additional context" not in empty
    assert len(empty.split()) < 150


def test_planner_reads_workstream_instructions_before_planning():
    text = build_planner_prompt({
        "planner_consult": {"workstream_id": "ws-1", "mode": "specify", "objective": "Plan release"},
        "workstream_context": {"name": "Product Launch"},
    })
    assert "Before planning, Read `/workspace/workstreams/product-launch/CLAUDE.md`" in text
    assert "current workstream mission and instructions" in text
