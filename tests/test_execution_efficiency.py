"""The T93 failure mode must not be the default for ordinary assignments."""
import json

import pytest

from src._session_policy import agent_config_for_assignment, build_session_policy
from src.orchestrator.worker_prompt import format_task_brief


@pytest.mark.parametrize("status", ["ready", "in_progress", "review", "blocked"])
def test_ultracode_agent_does_not_automatically_fan_out(status):
    original = {"model": "opus", "effort": "ultracode"}
    effective = agent_config_for_assignment(original, {"status": status})
    effort, settings, denied = build_session_policy(effective, "opus")
    assert original["effort"] == "ultracode"
    assert effort == "xhigh"  # preserve reasoning quality
    assert settings is None
    assert {"Agent", "Task", "Workflow"} <= set(denied)


@pytest.mark.parametrize("status", ["review", "blocked"])
def test_review_and_triage_never_inherit_execution_fanout(status):
    effective = agent_config_for_assignment(
        {"model": "opus", "effort": "ultracode"},
        {"status": status, "effort_hint": "ultracode"},
    )
    assert effective["effort"] == "xhigh"
    assert "Workflow" in build_session_policy(effective, "opus")[2]


def test_explicit_parallel_implementation_remains_available():
    effective = agent_config_for_assignment(
        {"model": "opus", "effort": "xhigh"},
        {"status": "in_progress", "effort_hint": "ultracode"},
    )
    effort, settings, denied = build_session_policy(effective, "opus")
    assert effort == "xhigh"
    assert json.loads(settings)["ultracode"] is True
    assert {"Workflow", "Agent", "Task"}.isdisjoint(denied)


def test_runtime_prompt_limits_extra_process_without_weakening_contract():
    prompt = format_task_brief({
        "task_id": "task-1", "status": "in_progress", "title": "Refine one widget",
        "brief": {"goal": "Improve the widget", "inputs": "Keep keyboard support",
                  "acceptance_criteria": ["All actions work from the keyboard"]},
    })
    for required in ("15–25 minutes", "never permission to skip requirements",
                     "designated reviewer", "review-of-review", "same revision",
                     "All actions work from the keyboard"):
        assert required in prompt


def test_board_health_distinguishes_slow_work_from_a_stalled_process():
    from src.config_sync.claude_md_content import (
        MANAGER_CLAUDE_MD, MANAGER_ASSISTANT_CLAUDE_MD,
    )
    for template in (MANAGER_CLAUDE_MD, MANAGER_ASSISTANT_CLAUDE_MD):
        prompt = " ".join(template.split())
        assert "25 minutes" in prompt
        assert "five recent messages" in prompt or "latest five messages" in prompt
        assert "handoff after required checks, never force Done" in prompt
    assert "liveness alone proves neither progress" in MANAGER_ASSISTANT_CLAUDE_MD.lower()
