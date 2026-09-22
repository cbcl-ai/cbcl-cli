"""Rendered instruction and authoring contracts for task-owned Agents."""

from uuid import UUID

import pytest
from src._content_contracts import (
    AGENT_IDENTITY_CONTRACT,
    PROFILE_AUTHORING_CONTRACT,
    render_agent_execution_policy,
)
from src.config_sync.claude_md_templates._office import SHARED_OFFICE_CLAUDE_MD
from src.config_sync.claude_md_templates._system_agents import SYSTEM_AGENT_CLAUDE_MD
from src.config_sync.claude_md_writer import ClaudeMdWriter
from src.config_sync.sync_service import ConfigStore
from src.orchestrator.manager_context import build_dynamic_context
from src.orchestrator.planner_prompt import build_planner_prompt
from src.orchestrator.worker_prompt import build_worker_prompt, task_output_dir

POLICY = {"enabled": True, "max_workers": 4, "max_workers_per_profile": 2}


@pytest.mark.parametrize("name", SYSTEM_AGENT_CLAUDE_MD)
def test_all_system_roles_inherit_identity_without_changing_role_authority(name):
    role = ClaudeMdWriter._get_agent_claude_md({"name": name, "agent_type": "system"})
    rendered = SHARED_OFFICE_CLAUDE_MD + "\n" + role
    assert rendered.count(AGENT_IDENTITY_CONTRACT) == 1
    assert SYSTEM_AGENT_CLAUDE_MD[name] in rendered


def task(**changes):
    return {
        "task_id": str(UUID(int=1)),
        "agent_instance_id": str(UUID(int=2)),
        "profile_id": str(UUID(int=3)),
        "execution_attempt_id": str(UUID(int=4)),
        "agent_execution_policy": POLICY,
        "status": "in_progress",
        "assigned_agent": "engineer",
        "reviewer": "auditor",
        "readable_id": "PR-001.T01",
        "workstream_context": {"name": "Project"},
        "workstream_short_code": "PR",
        "brief": {
            "goal": "Produce X",
            "inputs": "Provided input",
            "acceptance_criteria": ["X works"],
            "verification_steps": "Exercise X",
        },
        **changes,
    }


@pytest.mark.parametrize(
    "name",
    [
        name
        for name in SYSTEM_AGENT_CLAUDE_MD
        if name not in {"planner", "flow-architect", "data-curator"}
    ]
    + ["engineer"],
)
@pytest.mark.parametrize("status", ["in_progress", "review", "blocked"])
@pytest.mark.parametrize("rework_count", [0, 20])
@pytest.mark.parametrize("enabled", [False, True])
def test_complete_rendered_stack_preserves_identity_and_phase(
    name, status, rework_count, enabled
):
    agent = {
        "name": name,
        "agent_type": "custom" if name == "engineer" else "system",
        "system_prompt": "Domain role",
        "allowed_tools": ["Read"],
    }
    data = task(
        status=status,
        reviewer=name,
        rework_count=rework_count,
        prior_session_id="retained-session" if rework_count else None,
        agent_execution_policy={**POLICY, "enabled": enabled},
    )
    role = ClaudeMdWriter._get_agent_claude_md(agent)
    prompt = build_worker_prompt(data)
    rendered = SHARED_OFFICE_CLAUDE_MD + "\n" + role + "\n" + prompt
    assert rendered.count(AGENT_IDENTITY_CONTRACT) == 1
    expected, other = ("dynamic", "legacy") if enabled else ("legacy", "dynamic")
    assert f"Current agent execution policy: {expected}" in rendered
    assert f"Current agent execution policy: {other}" not in rendered
    for key in ("profile_id", "agent_instance_id", "execution_attempt_id"):
        assert data[key] in prompt
    assert "host-attested ownership" in " ".join(prompt.split())
    assert "prior transcript instructions cannot override this phase" in " ".join(
        prompt.split()
    )
    if status == "review":
        assert "Independent verification contract" in prompt
        assert "## Execution pace and verification" not in prompt
        assert "## NON-NEGOTIABLE EXECUTION RULES" not in prompt
    elif status == "blocked":
        assert "## NON-NEGOTIABLE EXECUTION RULES" not in prompt
    else:
        assert task_output_dir(data) in prompt


@pytest.mark.parametrize(
    "changes",
    [
        {"max_workers": 0},
        {"max_workers": True},
        {"max_workers": "4"},
        {"max_workers": 33},
        {"max_workers_per_profile": 5},
        {"unsupported": True},
    ],
)
def test_malformed_policy_cannot_authorize_parallel_guidance(changes):
    assert "Current agent execution policy: legacy" in render_agent_execution_policy(
        {**POLICY, **changes}
    )


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("context", [{}, {"agent_execution_policy": None}])
def test_stored_desired_policy_cannot_replace_current_admission(context, fresh):
    store = ConfigStore()
    store.office_config = {"agent_execution_policy": POLICY}
    prompt = build_dynamic_context("workstream:one", context, store, fresh)
    assert "Current agent execution policy: legacy" in prompt
    assert "Current agent execution policy: dynamic" not in prompt


def test_shared_office_recovery_respects_phase_and_retained_working_directory():
    common = SHARED_OFFICE_CLAUDE_MD.split("## Common Rules", 1)[1]
    assert "When executing, follow" in common
    assert "During execution, finish" in common
    assert (
        "Reviewers and triage agents use their own phase's resolution tools" in common
    )
    assert "local `./CLAUDE.md`" in common
    assert "/workspace/agents/<your-name>/CLAUDE.md" not in common
    assert "at the top of every task" not in common
    assert "globbing `/workspace/outputs/`" not in common


def test_schedules_expose_the_same_resource_contract_as_task_creation():
    from src._agent_image._mcp.tools_manager import get_manager_tools

    tools = {tool["name"]: tool for tool in get_manager_tools()}
    expected = tools["create_task"]["inputSchema"]["properties"]["execution_resources"]
    for name in ("schedule_assignment", "update_assignment_schedule"):
        props = tools[name]["inputSchema"]["properties"]["brief_template"]["properties"]
        assert props["execution_resources"] == expected


def test_dynamic_policy_does_not_offer_disabling_as_a_cleanup_bypass():
    guidance = render_agent_execution_policy(POLICY)
    assert "Before disabling parallelism" in guidance
    assert "let tasks/scripts finish or explicitly Stop them" in guidance
    assert "wait for confirmed cleanup" in guidance
    assert "A policy toggle does not stop work or release its claims" in guidance
    unconfirmed = render_agent_execution_policy(None)
    assert "older attempts may still be finishing under a prior policy" in unconfirmed
    assert "Serialize new Profile assignments" in unconfirmed


def test_computed_empty_resources_do_not_claim_explicit_independence():
    unconfirmed = build_worker_prompt(
        task(execution_resources=None, effective_execution_resources=[])
    )
    independent = build_worker_prompt(
        task(execution_resources=[], effective_execution_resources=[])
    )
    assert "no shared resource keys supplied; independence is unconfirmed" in unconfirmed
    assert "Current execution resources: explicitly independent work." not in unconfirmed
    assert "Current execution resources: explicitly independent work." in independent


@pytest.mark.parametrize("status", ["in_progress", "review", "blocked"])
@pytest.mark.parametrize("enabled", [False, True])
def test_read_focused_profile_does_not_imply_tool_or_resource_isolation(status, enabled):
    profile = {"name": "reader", "agent_type": "custom", "allowed_tools": ["Read"]}
    data = task(
        status=status,
        agent_execution_policy={**POLICY, "enabled": enabled},
        execution_resources=None,
        effective_execution_resources=["shared-workspace"],
    )
    rendered = " ".join((SHARED_OFFICE_CLAUDE_MD + "\n" +
                         ClaudeMdWriter._get_agent_claude_md(profile) + "\n" +
                         build_worker_prompt(data)).split())
    assert "Profile `allowed_tools` is workflow guidance, not a CLI restriction" in rendered
    assert "does not disable Bash/Edit or prove independence" in rendered
    assert "Current execution resources: `shared-workspace`." in rendered
    assert "config is the real tool boundary" not in rendered


def test_nested_workstream_and_file_tools_defer_to_current_task_output_directory():
    from src._agent_image._mcp.tools_worker import get_worker_tools
    from src.config_sync.claude_md_templates._workstream import (
        generate_workstream_claude_md,
    )

    workstream = generate_workstream_claude_md(
        {"name": "R&D Platform", "short_code": "RD"}
    )
    assert "exact output directory supplied by the current task prompt" in workstream
    assert "Save deliverables under `/workspace/outputs/" not in workstream
    save = next(tool for tool in get_worker_tools() if tool["name"] == "save_file")
    assert (
        "exact output directory your current task prompt names" in save["description"]
    )
    assert "/workspace/outputs/{workstream_short_code}" not in save["description"]
    script_profile = SYSTEM_AGENT_CLAUDE_MD["automation-script-developer"]
    assert "Use its actual value: task-owned execution supplies" in script_profile
    assert "Legacy/default shapes" in script_profile


@pytest.mark.parametrize("fresh", [True, False])
@pytest.mark.parametrize(
    "policy", [None, {}, {"enabled": "true"}, {"enabled": False}, POLICY]
)
def test_current_manager_and_planner_policy_is_explicit_including_resume(fresh, policy):
    context = {
        "workstream_id": "workstream",
        "agent_execution_policy": policy,
        "chat_history": "Prior conversation said all Profiles were busy.",
    }
    manager = build_dynamic_context(
        "workstream:workstream", context, ConfigStore(), fresh
    )
    planner = build_planner_prompt({"agent_execution_policy": policy})
    mode = (
        "dynamic"
        if isinstance(policy, dict) and policy.get("enabled") is True
        else "legacy"
    )
    for prompt in (manager, planner):
        assert f"Current agent execution policy: {mode}" in prompt
        if mode == "dynamic":
            assert "office: 4; per Profile: 2" in prompt
            assert "running sibling proves no liveness" in " ".join(prompt.split())


def test_task_owned_paths_and_sibling_identity_survive_rework():
    first = task(scope_id=str(UUID(int=10)))
    second = task(
        task_id=str(UUID(int=11)),
        agent_instance_id=str(UUID(int=12)),
        execution_attempt_id=str(UUID(int=13)),
    )
    rework = {
        **first,
        "execution_attempt_id": str(UUID(int=14)),
        "rework_count": 2,
        "prior_session_id": "executor-session",
    }
    assert (
        task_output_dir(first)
        == f"/workspace/workstreams/project/scopes/{first['scope_id']}/tasks/{first['task_id']}"
    )
    assert (
        task_output_dir(second)
        == f"/workspace/workstreams/project/tasks/{second['task_id']}"
    )
    assert task_output_dir(first) == task_output_dir(rework)
    assert first["agent_instance_id"] in build_worker_prompt(rework)
    assert first["execution_attempt_id"] not in build_worker_prompt(rework)
    assert second["agent_instance_id"] not in build_worker_prompt(first)
    assert task_output_dir(task(agent_execution_policy=None)) == "/workspace/outputs/PR"
    assert (
        task_output_dir(task(output_dir="/workspace/retained/task"))
        == "/workspace/retained/task"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"output_dir": "/workspace/../outside"},
        {"output_dir": "/etc"},
        {"task_id": "../sibling"},
        {"scope_id": "a/b"},
        {"workstream_slug": "../other"},
    ],
)
def test_task_output_paths_refuse_traversal(changes):
    with pytest.raises(ValueError):
        task_output_dir(task(**changes))


def test_all_profile_authoring_entrypoints_receive_reusable_contract():
    from src import _setup_prompts as setup
    from src import setup_generator as generator

    for name in (
        "INSTRUCTIONS_PROMPT",
        "ROSTER_PROMPT",
        "AGENT_DETAIL_PROMPT",
        "AGENT_FROM_DESCRIPTION_PROMPT",
        "IMPROVE_CONFIG_PROMPT",
        "WORKSTREAM_CONTEXT_PROMPT",
        "SKILLS_PROMPT",
        "SINGLE_SKILL_PROMPT",
        "STANDALONE_SKILL_PROMPT",
    ):
        assert PROFILE_AUTHORING_CONTRACT in getattr(setup, name), name
    for name in (
        "OFFICE_INSTRUCTIONS_PROMPT",
        "AGENT_SYSTEM_PROMPT_GEN_PROMPT",
        "AGENT_INSTRUCTIONS_GEN_PROMPT",
    ):
        assert PROFILE_AUTHORING_CONTRACT in getattr(generator, name), name


def test_tools_keep_profile_selectors_distinct_from_agent_configuration_identity():
    from src._agent_image._mcp.tools_configuration import CONFIGURATION_TOOLS
    from src._agent_image._mcp.tools_manager import get_manager_tools
    from src._agent_image._mcp.tools_worker import get_worker_tools

    manager = {tool["name"]: tool for tool in get_manager_tools()}
    assert "Profile catalog" in manager["list_agents"]["description"]
    for tool in (
        manager["create_task"],
        next(t for t in get_worker_tools() if t["name"] == "create_task"),
    ):
        props = tool["inputSchema"]["properties"]
        assert "Profile slug" in props["assigned_agent"]["description"]
        assert (
            "different" in props["reviewer"]["description"]
            or "differ" in props["reviewer"]["description"]
        )
    for tool in CONFIGURATION_TOOLS:
        assert "Profile" in tool["description"]
    assert "never pass a task Agent UUID" in CONFIGURATION_TOOLS[0]["description"]


def test_task_resource_tool_schema_is_shared_and_does_not_grant_running_mutation():
    from src._agent_image._mcp.tools_manager import get_manager_tools
    from src._agent_image._mcp.tools_worker import get_worker_tools

    schemas = [
        tool["inputSchema"]["properties"]["execution_resources"]
        for catalog in (get_manager_tools(), get_worker_tools())
        for tool in catalog
        if tool["name"] in {"create_task", "update_task"}
    ]
    assert len(schemas) == 4
    assert all(schema == schemas[0] for schema in schemas)
    assert schemas[0]["type"] == ["array", "null"]
    assert schemas[0]["maxItems"] == 16
    assert "Fixed while running" in schemas[0]["description"]
    assert "never use [] merely" in schemas[0]["description"]
