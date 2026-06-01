"""The Manager MUST surface the plan reads + complete_scope_verification, and
its tool-call envelope MUST name it 'manager' so fail-closed backend gates
(complete_scope_verification) accept it. Regression for the prod incident
where the Manager couldn't close a stuck 'verifying' scope ('actor=(none)')."""
import os
from unittest import mock

from src._agent_image._mcp.tools_manager import get_manager_tools
from src._agent_image._mcp.tools_planner import get_planner_tools


def _actions(tools):
    return {t.get("action") for t in tools}


def test_manager_surface_has_plan_reads_and_complete_verification() -> None:
    actions = _actions(get_manager_tools())
    assert "complete_scope_verification" in actions
    assert "get_execution_plan" in actions      # review the skeleton
    assert "get_workstream_plan" in actions      # review the roadmap
    # The Manager does NOT author plans — the Planner does.
    assert "update_execution_plan" not in actions
    assert "update_workstream_plan" not in actions


def test_planner_surface_has_everything_and_no_dupes() -> None:
    tools = get_planner_tools()
    names = [t.get("name") for t in tools]
    for n in (
        "update_workstream_plan", "get_workstream_plan",
        "update_execution_plan", "get_execution_plan",
        "complete_scope_verification",
    ):
        assert n in names
    # dedup: the shared reads/verify must appear exactly once even though
    # they're now in BOTH the manager base and PLANNER_PLAN_TOOLS.
    assert len(names) == len(set(names)), "duplicate tool names in planner surface"


def _envelope_with(env: dict) -> dict:
    """Reload _mcp_backend under `env` and return its _caller envelope."""
    import importlib
    import src._agent_image._mcp_backend as be
    with mock.patch.dict(os.environ, env, clear=False):
        importlib.reload(be)
        try:
            return be._caller_envelope()
        finally:
            importlib.reload(be)  # restore to ambient env


def test_manager_caller_is_stamped_manager_when_task_mode_manager() -> None:
    """FIX A: a Manager session (empty AGENT_NAME, TASK_MODE=manager) stamps
    _caller.agent_name='manager' so resolve_effective_actor names it and the
    fail-closed complete_scope_verification gate accepts it."""
    env = {"AGENT_NAME": "", "TASK_MODE": "manager"}
    assert _envelope_with(env) == {"agent_name": "manager", "role": "manager"}


def test_worker_caller_unchanged() -> None:
    """A worker keeps its AGENT_NAME identity (the fix only fills the Manager gap)."""
    env = {"AGENT_NAME": "analyst", "TASK_MODE": "execute"}
    assert _envelope_with(env) == {"agent_name": "analyst", "role": "worker"}


def test_planner_caller_unchanged() -> None:
    env = {"AGENT_NAME": "planner", "TASK_MODE": "execute"}
    assert _envelope_with(env) == {"agent_name": "planner", "role": "worker"}
