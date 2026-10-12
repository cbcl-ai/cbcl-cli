"""Daemon half of resource admission version 2 (parallel fix Phase 1a).

A task's script shares its parent attempt's receipt keys, so two agents'
scripts no longer exclude each other once undeclared tasks run side by
side; a run-alone (``shared-workspace``) request waits for every holder and
nothing starts beside a run-alone holder; version 1 keeps exact-key rules,
except that agent-keyed work still occupies the office after a switch back.
"""

import pytest

from src.orchestrator.agent_supervisor import AgentProcess, AgentState
from src.scripts.script_resources import script_lease_resources
from tests.test_dynamic_script_resources import context  # noqa: F401


def _worker(ctx, name, attempt, resources, task_id=None):
    worker = AgentProcess(
        agent_name=name,
        role="worker",
        state=AgentState.WORKING,
        current_task_id=task_id or f"task-{attempt}",
        execution_task_id=task_id or f"task-{attempt}",
        execution_attempt_id=attempt,
        agent_instance_id=f"instance-{attempt}",
        profile_id=f"profile-{name}",
        execution_mode="execute",
        execution_resources=resources,
    )
    ctx.supervisor._agents[attempt] = worker
    return worker


def _caller(attempt):
    return {"attempt_id": attempt, "role": "worker"}


def test_version_setter_accepts_only_two():
    from src.orchestrator.agent_supervisor import AgentSupervisor

    supervisor = AgentSupervisor("/tmp", "office")
    assert supervisor.resource_admission_version == 1
    supervisor.set_resource_admission_version(2)
    assert supervisor.resource_admission_version == 2
    for value in (None, "2", 3, 1):
        supervisor.set_resource_admission_version(value)
        assert supervisor.resource_admission_version == 1


async def test_two_agents_scripts_run_side_by_side(context):
    context.supervisor.set_resource_admission_version(2)
    _worker(context, "analyst", "a1", ["profile:analyst:execute"])
    _worker(context, "builder", "b1", ["profile:builder:execute"])
    await context.runner.execute("one", task_id="task-a1", execution_caller=_caller("a1"))
    await context.runner.execute("two", task_id="task-b1", execution_caller=_caller("b1"))
    leases = sorted(
        lease["resources"] for lease in context.state.active_script_resources()
    )
    assert leases == [
        ["profile:analyst:execute", "script:one"],
        ["profile:builder:execute", "script:two"],
    ]
    assert len(context.commands) == 2


async def test_the_same_agents_next_task_waits_for_its_running_script(context):
    context.supervisor.set_resource_admission_version(2)
    _worker(context, "analyst", "a1", ["profile:analyst:execute"])
    await context.runner.execute("one", task_id="task-a1", execution_caller=_caller("a1"))
    context.supervisor._agents.clear()
    profile = {"allowed_tools": ["Bash"]}
    assert not context.supervisor.resources_available(
        profile, {"effective_execution_resources": ["profile:analyst:execute"]}
    )
    assert context.supervisor.resources_available(
        profile, {"effective_execution_resources": ["profile:builder:execute"]}
    )


def test_lease_keys_fall_back_to_the_declaration_without_a_live_parent(context):
    _worker(context, "analyst", "a1", ["profile:analyst:execute"])
    undeclared = {"id": "task-a1", "execution_resources": None}
    assert script_lease_resources(context.runner, undeclared, _caller("a1")) == [
        "profile:analyst:execute"
    ]
    manager = {"attempt_id": "a1", "role": "manager"}
    assert script_lease_resources(context.runner, undeclared, manager) == [
        "shared-workspace"
    ]
    assert script_lease_resources(context.runner, undeclared, _caller("gone")) == [
        "shared-workspace"
    ]
    other_task = {"id": "another-task", "execution_resources": ["repo:x"]}
    assert script_lease_resources(context.runner, other_task, _caller("a1")) == [
        "repo:x"
    ]
    assert script_lease_resources(context.runner, None, None) == ["shared-workspace"]


@pytest.mark.parametrize(
    "version,held,requested,available",
    [
        # Version 2: a run-alone request waits for every holder ...
        (2, ["profile:analyst:execute"], ["shared-workspace"], False),
        (2, ["repo:x"], ["shared-workspace"], False),
        (2, [], ["shared-workspace"], False),
        # ... and nothing starts beside a run-alone holder.
        (2, ["shared-workspace"], ["profile:builder:execute"], False),
        (2, ["shared-workspace"], [], False),
        (2, ["profile:analyst:execute"], ["profile:builder:execute"], True),
        (2, ["profile:analyst:execute"], ["profile:analyst:review"], True),
        (2, ["profile:analyst:execute"], ["profile:analyst:execute"], False),
        (2, ["repo:x"], ["repo:x"], False),
        (2, ["repo:x"], ["repo:y"], True),
        # Version 1 keeps exact keys, plus the switch-back rule.
        (1, ["profile:analyst:execute"], ["shared-workspace"], False),
        (1, ["repo:x"], ["shared-workspace"], True),
        (1, ["shared-workspace"], ["repo:x"], True),
        (1, ["shared-workspace"], ["shared-workspace"], False),
    ],
)
def test_local_rules_for_busy_workers(context, version, held, requested, available):
    context.supervisor.set_resource_admission_version(version)
    _worker(context, "analyst", "a1", held)
    assert (
        context.supervisor.resources_available(
            {"allowed_tools": ["Bash"]}, {"effective_execution_resources": requested}
        )
        is available
    )


@pytest.mark.parametrize("version,available", [(1, True), (2, False)])
async def test_a_run_alone_request_waits_for_every_running_script(
    context, version, available
):
    context.supervisor.set_resource_admission_version(version)
    context.runner._assert_task_runnable.return_value = {"execution_resources": ["repo:x"]}
    await context.runner.execute("one", task_id="task")
    assert (
        context.supervisor.resources_available(
            {}, {"effective_execution_resources": ["shared-workspace"]}
        )
        is available
    )


def test_policy_off_never_checks_resources(context):
    context.supervisor.set_resource_admission_version(2)
    context.supervisor.set_execution_policy(
        {"enabled": False, "max_workers": 4, "max_workers_per_profile": 2}
    )
    _worker(context, "analyst", "a1", ["shared-workspace"])
    assert context.supervisor.resources_available(
        {}, {"effective_execution_resources": ["shared-workspace"]}
    )


def test_an_idle_office_admits_a_run_alone_request(context):
    """No unleased script answers ``[]``, which is not a holder: noting it
    once made every run-alone request wait in an idle version 2 office."""
    context.supervisor.set_resource_admission_version(2)
    assert context.supervisor.resources_available(
        {}, {"effective_execution_resources": ["shared-workspace"]}
    )


@pytest.mark.parametrize(
    "version,expected", [(1, ["shared-workspace"]), (2, ["script:one"])]
)
async def test_a_script_without_a_task_holds_only_its_name(context, version, expected):
    context.supervisor.set_resource_admission_version(version)
    await context.runner.execute("one")
    assert [
        lease["resources"] for lease in context.state.active_script_resources()
    ] == [expected]


async def test_scripts_without_a_task_run_beside_an_agents_work(context):
    from src.script_resource_state import ScriptResourceConflict

    context.supervisor.set_resource_admission_version(2)
    _worker(context, "analyst", "a1", ["profile:analyst:execute"])
    await context.runner.execute("one")
    await context.runner.execute("two")
    assert len(context.commands) == 2
    with pytest.raises(ScriptResourceConflict):
        await context.runner.execute("one")
    profile = {"allowed_tools": ["Bash"]}
    assert context.supervisor.resources_available(
        profile, {"effective_execution_resources": ["profile:builder:execute"]}
    )
    assert not context.supervisor.resources_available(
        profile, {"effective_execution_resources": ["shared-workspace"]}
    )


async def test_a_script_without_a_task_waits_for_a_run_alone_task(context):
    from src.script_resource_state import ScriptResourceConflict

    context.supervisor.set_resource_admission_version(2)
    _worker(context, "analyst", "a1", ["shared-workspace"])
    with pytest.raises(ScriptResourceConflict):
        await context.runner.execute("one")
    assert context.commands == []


async def test_after_a_switch_back_a_script_without_a_task_keeps_the_office(context):
    context.supervisor.set_resource_admission_version(2)
    await context.runner.execute("one")
    context.supervisor.set_resource_admission_version(1)
    assert not context.supervisor.resources_available(
        {}, {"effective_execution_resources": ["shared-workspace"]}
    )
    assert context.supervisor.resources_available(
        {}, {"effective_execution_resources": ["repo:x"]}
    )


@pytest.mark.parametrize("version", [1, 2])
def test_a_task_script_without_a_live_parent_holds_the_tasks_keys(context, version):
    context.supervisor.set_resource_admission_version(version)
    manager = {"attempt_id": "", "role": "manager"}
    undeclared = {
        "id": "task-u",
        "execution_resources": None,
        "effective_execution_resources": ["profile:analyst:execute"],
    }
    assert script_lease_resources(context.runner, undeclared, manager) == (
        ["profile:analyst:execute"] if version == 2 else ["shared-workspace"]
    )
    declared = {
        "id": "task-d",
        "execution_resources": ["repo:x"],
        "effective_execution_resources": ["repo:x"],
    }
    assert script_lease_resources(context.runner, declared, manager) == ["repo:x"]
    older_backend = {"id": "task-o", "execution_resources": None}
    assert script_lease_resources(context.runner, older_backend, manager) == [
        "shared-workspace"
    ]


@pytest.mark.parametrize("version", [1, 2])
def test_an_agent_reviews_while_it_executes_only_under_version_two(context, version):
    context.supervisor.set_resource_admission_version(version)
    context.supervisor.set_execution_policy(
        {"enabled": True, "max_workers": 4, "max_workers_per_profile": 1}
    )
    _worker(context, "analyst", "a1", ["profile:analyst:execute"])
    can_spawn = context.supervisor.profile_can_spawn
    two = version == 2
    assert can_spawn("analyst", mode="execute") is False
    assert can_spawn("analyst", mode="review") is two
    assert can_spawn("analyst", mode="triage") is two
    assert can_spawn("analyst") is two
    assert can_spawn("builder", mode="execute") is True


def test_an_unresolved_review_claim_counts_in_the_review_class(context):
    context.supervisor.set_resource_admission_version(2)
    context.supervisor.set_execution_policy(
        {"enabled": True, "max_workers": 4, "max_workers_per_profile": 1}
    )
    context.state.begin_worker_claim(
        "analyst",
        "task-r",
        {
            "attempt_id": "r1",
            "execution_mode": "review",
            "expected_assigned_agent": "builder",
        },
    )
    can_spawn = context.supervisor.profile_can_spawn
    assert can_spawn("analyst", mode="review") is False
    assert can_spawn("analyst", mode="triage") is False
    assert can_spawn("analyst", mode="execute") is True
    assert can_spawn("analyst", mode="review", excluded_attempt_id="r1") is True


def test_the_office_limit_counts_every_role(context):
    context.supervisor.set_resource_admission_version(2)
    context.supervisor.set_execution_policy(
        {"enabled": True, "max_workers": 1, "max_workers_per_profile": 1}
    )
    _worker(context, "analyst", "a1", ["profile:analyst:execute"])
    assert context.supervisor.profile_can_spawn("analyst", mode="review") is False
    assert context.supervisor.profile_can_spawn("builder", mode="execute") is False


async def test_two_runs_of_one_script_never_overlap_under_version_two(context):
    """Every version 2 run holds ``script:<name>``: an agent's run and a
    cron run, or two agents' runs, of one script do not share its folder."""
    from src.script_resource_state import ScriptResourceConflict

    context.supervisor.set_resource_admission_version(2)
    _worker(context, "analyst", "a1", ["profile:analyst:execute"])
    _worker(context, "builder", "b1", ["profile:builder:execute"])
    await context.runner.execute("one", task_id="task-a1", execution_caller=_caller("a1"))
    with pytest.raises(ScriptResourceConflict):
        await context.runner.execute("one")
    with pytest.raises(ScriptResourceConflict):
        await context.runner.execute(
            "one", task_id="task-b1", execution_caller=_caller("b1")
        )
    await context.runner.execute("two", task_id="task-b1", execution_caller=_caller("b1"))
    assert len(context.commands) == 2


async def test_after_a_switch_back_a_task_declaring_the_script_key_waits(context):
    context.supervisor.set_resource_admission_version(2)
    await context.runner.execute("one")
    context.supervisor.set_resource_admission_version(1)
    profile = {"allowed_tools": ["Bash"]}
    assert not context.supervisor.resources_available(
        profile, {"effective_execution_resources": ["script:one"]}
    )
    assert context.supervisor.resources_available(
        profile, {"effective_execution_resources": ["script:two"]}
    )


async def test_a_task_declaring_sixteen_keys_can_still_run_a_script(context):
    """The platform's script key may go beyond a task's 16 keys."""
    context.supervisor.set_resource_admission_version(2)
    keys = [f"repo:k{index}" for index in range(16)]
    _worker(context, "analyst", "a1", keys)
    await context.runner.execute("one", task_id="task-a1", execution_caller=_caller("a1"))
    (lease,) = context.state.active_script_resources()
    assert lease["resources"] == [*keys, "script:one"]
    assert len(context.commands) == 1
