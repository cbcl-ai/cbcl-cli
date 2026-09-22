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
                     "designated reviewer", "review-of-review", "exact delivered revision",
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


def test_board_health_distinguishes_queued_review_from_started_review():
    """Do not present the Review column or elapsed age as an active session."""
    from src.config_sync.claude_md_content import (
        MANAGER_CLAUDE_MD, MANAGER_ASSISTANT_CLAUDE_MD,
    )

    manager = " ".join(MANAGER_CLAUDE_MD.split())
    assistant = " ".join(MANAGER_ASSISTANT_CLAUDE_MD.split())
    for prompt in (manager, assistant):
        assert "`get_board`" in prompt
        assert "`get_task_detail`" in prompt
        assert "queued" in prompt and "active" in prompt and "hold" in prompt
        assert "runtime ownership is unavailable" in prompt
        assert "unconfirmed" in prompt
        assert "healthy busy reviewer" in prompt
        assert "missing/unsuitable reviewer with a qualified independent one" in prompt
    assert "Review is a column, not proof review started" in manager
    assert "column alone does not prove a reviewer started" in assistant
    assert "YOU investigate operational stalls" in manager
    assert "infer PASS from time spent" in manager
    assert "not automatic approval" in assistant
    assert "**NOTHING** — the reviewer handles everything" not in manager


def test_review_ownership_and_capacity_follow_current_execution_policy():
    from src.config_sync.claude_md_content import MANAGER_ASSISTANT_CLAUDE_MD
    from src.config_sync.claude_md_templates._system_agents import PLANNER_CLAUDE_MD

    assistant = " ".join(MANAGER_ASSISTANT_CLAUDE_MD.split())
    planner = " ".join(PLANNER_CLAUDE_MD.split())
    assert (
        "In legacy mode the executor Profile stays reserved throughout Review"
        in assistant
    )
    assert (
        "In dynamic mode a retained executor Agent does not reserve its whole Profile"
        in assistant
    )
    assert "task's Agent/attempt and capacity" in assistant
    assert "confirmed cleanup still gate admission" in assistant
    assert "Returned work never interrupts a current session" in assistant
    assert "qualified independent reviewers" in planner
    assert "expertise and board workload" in planner
    assert "Keep executor assignment through Review" in planner
    assert "dependencies wait for Done" in planner
    assert "dynamic mode permits separate task Agents from the same Profile" in planner
    assert "Never invent dependencies to serialize a Profile" in planner
