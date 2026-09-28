"""F01 — one lifecycle contract, pinned to the catalogs and to the runtime.

``src/_lifecycle_contract.py`` is the single maintained source of the facts
every prompt surface states about how a task session ENDS: which role/phase
holds which lifecycle tool, that ending a session is not completing a task,
that a managed script parks and resumes the SAME task, that a push is not a
handoff, and when to split work. These tests pin:

1. the derived authority to the LIVE MCP catalogs (never a hand list);
2. each canonical fact to the runtime behaviour it describes;
3. every consumer surface to the shared facts — and the retired
   "trigger task + consume task" split model to nowhere.
"""
from __future__ import annotations

import inspect
import re

import pytest

from src import _lifecycle_contract as contract
from src._lifecycle_contract import (
    ALL_FACTS,
    EXECUTE_BLOCKER_FACT,
    MANAGER_ASYNC_WORK_RULE,
    PLANNER_UPDATE_TASK_FIELDS,
    PLANNER_ASYNC_WORK_RULE,
    PUSH_IS_NOT_HANDOFF_FACT,
    SCRIPT_CORE,
    SCRIPT_HANDOFF_CORE,
    SCRIPT_HANDOFF_RESUME_FACT,
    SESSION_END_FACT,
    SESSIONS,
    SPLIT_RULE,
    holder_labels,
    holders,
    render_create_task_line,
    render_move_task_line,
    render_update_task_line,
    session_tools,
)
from src.config_sync.claude_md_content import (
    MANAGER_CLAUDE_MD,
    SHARED_AGENT_WORK_RULES,
    SHARED_OFFICE_CLAUDE_MD,
)
from src.config_sync.claude_md_templates._shared_agent import (
    BASH_CAPABILITY_RULES,
    LONG_RUNNING_BASH_RULE,
)
from src.config_sync.claude_md_templates._system_agents import (
    AUTOMATION_SCRIPT_DEV_CLAUDE_MD,
    MANAGER_ASSISTANT_CLAUDE_MD,
    PLANNER_CLAUDE_MD,
)
from src.config_sync.claude_md_writer import (
    ClaudeMdWriter,
    render_office_specs_index,
)
from src.execution_completion import completion_disposition
from src.orchestrator.planner_prompt import build_planner_prompt
from src.orchestrator.worker_prompt import build_worker_prompt
from src.runtime_state import RuntimeState
from tests.backend_boundary import import_backend


def _norm(text: str) -> str:
    return " ".join(text.split())


def _office() -> str:
    return SHARED_OFFICE_CLAUDE_MD.format(
        office_name="Test Office", office_specs_index=render_office_specs_index([])
    )


def _task(**overrides) -> dict:
    return {
        "task_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "readable_id": "LC-001.T01",
        "title": "Export the weekly report",
        "status": "in_progress",
        "assigned_agent": "builder",
        "reviewer": "auditor",
        "task_class": "assignment",
        "workstream_context": {"name": "Project"},
        "brief": {
            "goal": "Export the report",
            "inputs": "The request",
            "acceptance_criteria": ["The report exists"],
            "verification_steps": "Execution checks: open it.",
        },
        **overrides,
    }


# ── 1. Derived authority matches the live catalogs ─────────────────────


def test_every_session_shape_holds_its_own_resolution_tools():
    for session in SESSIONS:
        held = session_tools(session)
        missing = set(session.ends_with) - held
        assert not missing, (session.key, missing)


def test_session_catalog_facts_the_prompts_rely_on():
    tools = contract.role_tools()
    assert not {"move_task", "create_task", "update_task"} & tools["executor"]
    assert "move_task" in tools["ask_executor"]
    assert "move_task" in tools["reviewer"]
    assert not {"update_status", "request_user_action"} & tools["reviewer"]
    assert not {"update_status", "request_user_action"} & tools["ma_triage"]
    # retry_blocked_task is not a triage path: triage is not served it, while
    # the Manager Assistant keeps it in execute and review.
    assert "retry_blocked_task" not in tools["ma_triage"]
    assert "retry_blocked_task" in tools["ma_execute"]
    assert "retry_blocked_task" in tools["ma_review"]
    assert "move_task" not in tools["planner"]
    assert {"create_task", "update_task"} <= tools["planner"]


def test_canonical_facts_are_brace_free():
    # Spliced into templates that pass through str.format().
    for fact in ALL_FACTS + (
        render_move_task_line(),
        render_create_task_line(),
        render_update_task_line(),
    ):
        assert "{" not in fact and "}" not in fact, fact


def test_office_primer_renders_the_derived_holder_lines():
    office = _office()
    assert render_move_task_line() in office
    assert render_create_task_line() in office
    assert render_update_task_line() in office
    # The retired blanket denials must not come back.
    assert "Workers do NOT call" not in office
    assert "(Manager only)" not in office
    line = _norm(render_move_task_line())
    for label in holder_labels("move_task"):
        assert label.split(" (")[0] in line
    assert "Planner" not in line  # the Planner holds no move_task
    create = _norm(render_create_task_line())
    for label in ("Manager", "Manager Assistant", "Planner"):
        assert label in create


def test_holder_line_follows_the_catalog_not_a_hand_list(monkeypatch):
    real = contract.role_tools()
    widened = {**real, "executor": real["executor"] | {"move_task"}}
    monkeypatch.setattr(contract, "role_tools", lambda: widened)
    assert "executor" in holders("move_task")
    assert "ordinary executors" in render_move_task_line()
    assert "use `update_status` instead" not in render_move_task_line()


def test_update_task_line_states_the_planner_field_subset():
    line = _norm(render_update_task_line())
    note = "/".join(PLANNER_UPDATE_TASK_FIELDS) + " of never-run drafts only"
    assert f"Planner: {note}" in line
    for manager_only in ("priority", "labels", "assigned_agent", "reviewer"):
        assert manager_only not in note


def test_planner_update_task_subset_matches_the_backend_gate():
    # The office line must not claim edits the backend refuses the Planner
    # (action_executors.action_update_task), nor omit ones it allows.
    import asyncio
    import uuid

    executors = import_backend("app.ws.action_executors")

    class _GatePassed(Exception):
        pass

    class _NoSession:
        def __getattr__(self, name):
            raise _GatePassed(name)

    def planner_edit(field: str, value) -> str:
        payload = {
            "task_id": str(uuid.uuid4()),
            field: value,
            "_caller": {"agent_name": "planner", "role": "worker"},
        }
        # T27: only reaching the session (``_GatePassed``) proves the gate let
        # the edit through; any other exception is a real failure, never a
        # silent pass, and a returned result must be the Planner refusal.
        try:
            result = asyncio.run(
                executors.action_update_task(_NoSession(), uuid.uuid4(), payload)
            )
        except _GatePassed:
            return "passed"
        message = str(result.get("message", "")) if isinstance(result, dict) else ""
        assert "Planner corrections are restricted" in message, result
        return "refused"

    values = {
        "brief": {"goal": "g"},
        "title": "t",
        "description": "d",
        "depends_on": ["X-001.T01"],
        "priority": "high",
        "labels": ["x"],
        "assigned_agent": "builder",
        "reviewer": "auditor",
    }
    for field in PLANNER_UPDATE_TASK_FIELDS:
        assert planner_edit(field, values[field]) == "passed", field
    for field in ("priority", "labels", "assigned_agent", "reviewer"):
        assert planner_edit(field, values[field]) == "refused", field


# ── 2. Canonical facts match the runtime ───────────────────────────────


@pytest.mark.parametrize("status", ["in_progress", "review"])
def test_script_handoff_fact_matches_completion_disposition(status):
    # SCRIPT_HANDOFF_CORE: a script started BY THIS attempt parks the task
    # in the phase that launched it — including Review.
    task = {"status": status, "execution_cycle": 1, "execution_generation": 2}
    event = {"status": "review", "is_review_completion": status == "review",
             "_caller": {"execution_cycle": 1, "execution_generation": 2}}
    assert completion_disposition(
        task, event, active_scripts=True, started_script=True
    ) == "script_handoff"
    assert "phase that launched it" in SCRIPT_HANDOFF_CORE


def test_script_handoff_resumes_the_same_task_with_recorded_runs(tmp_path):
    runtime = RuntimeState(tmp_path / "runtime.sqlite3", "office")
    runtime.observe_cycle("task", 1)
    runtime.note_script("task", "exec-1", "running")
    runtime.park_script_handoff("task")
    assert runtime.script_wait("task")["state"] == "waiting"
    runtime.note_script("task", "exec-1", "completed")
    wait = runtime.script_wait("task")
    assert wait["state"] == "resumable"
    assert wait["executions"] == [{"execution_id": "exec-1", "state": "completed"}]
    assert "resumes the SAME task" in SCRIPT_HANDOFF_CORE


def test_accepted_capacity_wait_is_also_a_handoff():
    assert completion_disposition(
        {"status": "in_progress"}, {"status": "review"},
        active_scripts=False, capacity_wait=True,
    ) == "capacity_handoff"
    assert "`accepted_wait`" in SCRIPT_HANDOFF_RESUME_FACT


def test_session_end_fact_matches_the_executor_default_submission():
    # No terminal call, no script, no wait: the completion is "normal" and
    # the executor's clean end reports status "review".
    assert completion_disposition(
        {"status": "in_progress"}, {"status": "review"}, active_scripts=False,
    ) == "normal"
    import ast

    from src import _agent_worker_task

    # T27: read the executor's clean-end TASK_COMPLETE literal structurally
    # (formatting-independent): status "review", not a review completion.
    tree = ast.parse(inspect.getsource(_agent_worker_task))
    completions = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        fields = {
            key.value: value
            for key, value in zip(node.keys, node.values)
            if isinstance(key, ast.Constant)
        }
        comment = fields.get("comment")
        if isinstance(comment, ast.Constant) and comment.value == (
            "Task execution complete."
        ):
            completions.append(fields)
    clean_end = [
        fields for fields in completions
        if isinstance(fields.get("status"), ast.Constant)
        and isinstance(fields.get("is_review_completion"), ast.Constant)
    ]
    assert len(clean_end) == 1, completions
    assert clean_end[0]["status"].value == "review"
    assert clean_end[0]["is_review_completion"].value is False
    assert "submitted to Review" in SESSION_END_FACT


def test_manager_rule_and_planner_rule_share_the_async_facts():
    manager = _norm(MANAGER_CLAUDE_MD)
    planner = _norm(PLANNER_CLAUDE_MD)
    for fact in (SCRIPT_CORE, PUSH_IS_NOT_HANDOFF_FACT, SPLIT_RULE):
        assert _norm(fact) in manager
        assert _norm(fact) in planner
    assert _norm(MANAGER_ASYNC_WORK_RULE) in manager
    assert _norm(PLANNER_ASYNC_WORK_RULE) in planner


def test_shared_worker_rules_splice_the_contract_facts():
    # The shared worker rules take the handoff core and the execute blocker
    # fact from the contract — no hand-written copies that can drift.
    shared = _norm(SHARED_AGENT_WORK_RULES)
    assert _norm(SCRIPT_HANDOFF_CORE) in shared
    assert _norm(EXECUTE_BLOCKER_FACT) in shared
    # The MA appends the one-shot rule without the full shared rules.
    assert _norm(SCRIPT_HANDOFF_CORE) in _norm(LONG_RUNNING_BASH_RULE)
    assert _norm(SCRIPT_HANDOFF_CORE) in _norm(MANAGER_ASSISTANT_CLAUDE_MD)
    # The task prompt carries only the resume detail, never the core again.
    assert _norm(SCRIPT_HANDOFF_CORE) not in _norm(SCRIPT_HANDOFF_RESUME_FACT)


def test_contract_module_has_no_dead_constants():
    # Every public fact is consumed by at least one rendered surface.
    rendered = " ".join(
        _norm(text) for text in _all_lifecycle_surfaces().values()
    )
    for fact in ALL_FACTS:
        assert _norm(fact) in rendered, fact[:60]
    assert not hasattr(contract, "LIFECYCLE_TOOLS")
    # A removed composite must not linger as a test-only constant.
    assert not hasattr(contract, "SCRIPT_HANDOFF_FACT")


def test_every_public_text_constant_is_a_listed_fact():
    """A public prose constant outside ALL_FACTS escapes the dead-constant
    check above and can drift into a template unnoticed (F07 review: the
    SCRIPT_HANDOFF_FACT composite outlived its last renderer)."""
    public_text = {
        name
        for name, value in vars(contract).items()
        if name.isupper() and not name.startswith("_") and isinstance(value, str)
    }
    listed = {
        name for name in public_text if getattr(contract, name) in ALL_FACTS
    }
    assert public_text == listed, sorted(public_text - listed)


# ── 3. No surface prescribes the retired split model ───────────────────

_RETIRED_SPLIT_PHRASES = (
    "TERMINAL at the trigger",
    "Author TWO tasks",
    "physically impossible",
    "Separate a managed trigger",
    "consume the result",
    "ENDS the worker's session at dispatch",
    "is the worker's LAST act",
    "treat the trigger as the END",
)

_ROLE_TOOLS = {
    "analyst": ["Read", "Write", "Bash"],
    "auditor": ["Read", "Write", "Bash"],
    "automation-script-developer": ["Read", "Write", "Bash"],
    "builder": ["Read", "Write", "Bash"],
    "manager-assistant": ["Read", "Write", "Bash"],
    "planner": ["Read", "Write", "Bash"],
    "flow-architect": ["Read", "Write", "Bash"],
    "data-curator": ["Read", "Bash"],
}


def _all_lifecycle_surfaces() -> dict[str, str]:
    surfaces = {
        "manager": MANAGER_CLAUDE_MD,
        "office": _office(),
        "shared": SHARED_AGENT_WORK_RULES,
    }
    # F07: the Manager procedure modules the dynamic context injects.
    from tests.evals._prompt_composition import manager_procedure_modules

    surfaces.update(manager_procedure_modules())
    for name, tools in _ROLE_TOOLS.items():
        surfaces[name] = ClaudeMdWriter._get_agent_claude_md(
            {"name": name, "agent_type": "system", "allowed_tools": tools}
        )
    for status, extra in (
        ("in_progress", {}),
        ("in_progress", {"task_class": "ask"}),
        ("review", {}),
        ("blocked", {"assigned_agent": "manager-assistant"}),
    ):
        key = f"worker:{status}:{extra.get('task_class', '')}"
        surfaces[key] = build_worker_prompt(_task(status=status, **extra))
    for mode in ("specify", "scope_plan", "materialize", "research", "verify"):
        surfaces[f"planner:{mode}"] = build_planner_prompt(
            {"planner_consult": {"mode": mode, "scope_id": "scope-1"}}
        )
    return surfaces


def test_no_surface_prescribes_the_trigger_consume_split():
    offenders = {
        name: phrase
        for name, text in _all_lifecycle_surfaces().items()
        for phrase in _RETIRED_SPLIT_PHRASES
        if phrase in _norm(text)
    }
    assert not offenders, offenders


# ── 4. Phase prompts: execute resume, review handoff, Branch S ─────────


def test_execute_prompt_states_the_resume_detail_once_with_its_siblings():
    # F07: the execute session reads the handoff core in its role file and
    # the session-end fact in the office file; the task prompt adds only the
    # resume detail, so no fact reaches the session twice. That each fact
    # still reaches every role/phase exactly once is pinned on the composed
    # prompt by tests/evals/test_prompt_composition.py.
    prompt = build_worker_prompt(_task())
    assert SCRIPT_HANDOFF_RESUME_FACT in prompt
    assert SCRIPT_HANDOFF_CORE not in prompt
    assert SESSION_END_FACT not in prompt
    assert _norm(SESSION_END_FACT) in _norm(_office())


def test_review_prompt_allows_a_verification_handoff_but_still_requires_a_verdict():
    prompt = _norm(build_worker_prompt(_task(status="review")))
    assert "## Managed verification runs" in prompt
    assert "accepted receipt is a durable review handoff" in prompt
    assert 'NEVER end your session with the task still in "review"' in prompt
    assert "The ONLY exception is an accepted `execute_script` receipt" in prompt
    assert "except right after an accepted verification-run receipt" in prompt


def test_designated_reviewer_must_resolve_with_move_task():
    prompt = _norm(build_worker_prompt(_task(status="review")))
    assert "Otherwise resolve with `move_task` before your session ends." in prompt


def test_ma_review_prompt_keeps_the_reassign_and_stop_path():
    # The MA (default reviewer) triages a review: Action A reassigns the
    # reviewer via update_task and leaves the task in Review. Its task prompt
    # must not demand an unconditional move_task verdict.
    prompt = _norm(build_worker_prompt(
        _task(status="review", reviewer="manager-assistant")
    ))
    assert "## Managed verification runs" in prompt
    assert "resolve with `move_task` before your session ends" not in prompt
    assert "a reviewer reassignment via `update_task`" in prompt
    assert "YOUR ROLE: DESIGNATED REVIEWER" not in prompt
    playbook = _norm(MANAGER_ASSISTANT_CLAUDE_MD)
    assert "**DO NOT move the task.** It stays in Review." in playbook


def test_blocked_triage_prompt_gets_no_script_handoff_line():
    prompt = build_worker_prompt(
        _task(status="blocked", assigned_agent="manager-assistant")
    )
    assert "Managed verification runs" not in prompt
    assert SCRIPT_HANDOFF_RESUME_FACT not in prompt
    # X02: the Path B helper is UNSCOPED (the sequential-addition guard
    # refuses an in-scope helper with empty depends_on, and depending on
    # the blocked task makes a cycle).
    assert "UNSCOPED helper task" in prompt


_RESULTS = [{"execution_id": "exec-2026-09-23-abc123", "state": "completed"}]


def test_script_resume_selects_branch_s_before_the_recorded_runs():
    prompt = build_worker_prompt(
        _task(script_handoff_results=_RESULTS, recent_activities=[
            {"event_type": "checkpoint", "actor": "system", "content": "handoff"},
        ])
    )
    assert "BRANCH S (MANAGED-SCRIPT RESUME)" in prompt
    for other in ("BRANCH A", "BRANCH B", "BRANCH C", "BRANCH D", "execute from scratch"):
        assert other not in prompt
    assert prompt.index("BRANCH S") < prompt.index(
        "## Managed script verification-resume"
    )
    assert "never relaunch a completed run" in prompt
    assert "the brief's next step" in prompt


def test_asd_script_resume_continues_the_two_run_protocol():
    prompt = build_worker_prompt(_task(
        assigned_agent="automation-script-developer",
        script_handoff_results=_RESULTS,
    ))
    assert "BRANCH S" in prompt
    assert "next required test run" in prompt


def test_no_branch_s_without_valid_recorded_runs():
    for results in (
        None,
        [],
        [{"execution_id": "../../etc"}],
        [{"state": "done"}],
        [{"execution_id": "exec-1", "state": "bogus"}],
    ):
        prompt = build_worker_prompt(_task(script_handoff_results=results))
        assert "BRANCH S" not in prompt
        assert "## Managed script verification-resume" not in prompt


def test_timed_out_run_is_listed_under_branch_s():
    # The runner records "timed_out" (terminal in RuntimeState.script_wait);
    # BRANCH S and the run list share ONE gate, so the id must render.
    prompt = build_worker_prompt(_task(script_handoff_results=[
        {"execution_id": "exec-timeout-1", "state": "timed_out"},
    ]))
    assert "BRANCH S (MANAGED-SCRIPT RESUME)" in prompt
    assert "- Execution exec-timeout-1: timed_out." in prompt
    assert prompt.index("BRANCH S") < prompt.index("exec-timeout-1")


def test_capacity_wait_resume_selects_branch_s_not_from_scratch():
    # SCRIPT_HANDOFF_RESUME_FACT promises an accepted_wait resumes the SAME task
    # with the wait listed — STEP 0 must route there first.
    prompt = build_worker_prompt(_task(capacity_wait_resume={
        "wait_id": "wait-1",
        "operation_id": "op-1",
        "operation_key": "report-2026-09-23",
        "action": "start",
        "phase": "execute",
    }))
    assert "BRANCH S (MANAGED-SCRIPT RESUME)" in prompt
    for other in ("BRANCH A", "BRANCH B", "BRANCH C", "BRANCH D", "execute from scratch"):
        assert other not in prompt
    branch = prompt[prompt.index("BRANCH S"):prompt.index("## Resume after capacity waiting")]
    assert "`get_operation`" in branch
    assert "recorded operation key" in branch
    assert "## Managed script verification-resume" not in prompt


# ── 5. Executor escalation guidance actually blocks the task ───────────


def test_executor_blocker_guidance_names_the_one_blocking_call():
    # escalate_blocker / proposals never change task status; ending an
    # execute session then submits the task to Review.
    assert "ONE `update_status(blocked)`" in _norm(BASH_CAPABILITY_RULES)
    asd = _norm(AUTOMATION_SCRIPT_DEV_CLAUDE_MD)
    # fw3-automation: the missing-credential block names its Office Secret.
    assert (
        "ONE `update_status(blocked, office_secret_names=[<Office Secret name>])` "
        "call whose comment starts `ESCALATED (missing_credential):`" in asd
    )
    assert "ONE `update_status(blocked)` call whose comment starts `ESCALATED (external_outage):`" in asd
    office = _norm(_office())
    assert "does NOT change task status" in office
    shared = _norm(SHARED_AGENT_WORK_RULES)
    # X03: draft-mode approval is TWO calls — the request alone never blocks.
    assert "`update_status(blocked)` whose comment starts `ESCALATED (missing_data):" in shared
    assert "the request alone does not block the task" in shared


# ── 6. The backend MA system prompt matches the playbook ───────────────


def test_ma_system_prompt_paths_match_the_playbook():
    system_agents = import_backend("app.agents.system_agents")
    ma = next(
        agent for agent in system_agents.SYSTEM_AGENT_DEFAULTS
        if agent["name"] == "manager-assistant"
    )
    prompt = _norm(ma["system_prompt"])
    playbook = _norm(MANAGER_ASSISTANT_CLAUDE_MD)
    for letter, playbook_heading in (
        ("A", "**A. The worker asked a clarification question"),
        ("B", "**B. Worker is blocked by a MISSING PREREQUISITE**"),
        ("C", "**C. Decision needs the USER's authority**"),
    ):
        assert playbook_heading in playbook
        assert re.search(rf"\({letter}[:)]", prompt), letter
    # Final review P8: ONE statement on both surfaces — retry_blocked_task
    # is not a triage path (an approved escalation already unblocks).
    assert "`retry_blocked_task` is not a triage path" in prompt
    assert "`retry_blocked_task` is not a triage path" in playbook
    assert "Path D" not in prompt and "Path D" not in playbook
    # Same FAIL recipe: the verdict rides ONE move_task.
    assert "`add_activity` feedback" not in prompt
