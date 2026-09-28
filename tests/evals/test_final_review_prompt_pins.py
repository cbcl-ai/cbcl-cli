"""Final-review prompt pins (P1–P16, C3–C6, C13; 2026-09-23).

Each pin checks the text a session actually receives — the composed worker,
Manager or Planner prompt rendered from production code
(``tests/evals/_prompt_composition.py``) — and would have failed before its
fix. Facts are pinned against their ground truth where one exists (served
catalogs, backend gates, the lifecycle contract).
"""

from __future__ import annotations

import pytest

from tests.evals._prompt_composition import (
    WORKER_SESSIONS,
    compose_worker_session,
    norm,
)

# ── P1: the stdin user turn matches the session's phase ────────────────────


@pytest.mark.parametrize("session", sorted(WORKER_SESSIONS))
def test_worker_user_turn_matches_the_phase(session):
    _, status, _, _ = WORKER_SESSIONS[session]
    turn = dict(compose_worker_session(session).parts)["user_turn"]
    if status == "review":
        assert turn.startswith("Review task ")
        assert "Do not execute the brief" in turn
        assert "Execute the task" not in turn
    elif status == "blocked":
        assert turn.startswith("Triage blocked task ")
        assert "Do not execute or unblock it" in turn
        assert "Execute the task" not in turn
    else:
        assert turn.startswith("Execute the task as described")


def test_worker_session_sends_the_phase_aware_user_turn():
    """The session builder takes its stdin turn from the helper, never a
    hard-coded execute instruction."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "src" / "_agent_worker_task.py"
    ).read_text()
    assert "prompt = build_worker_user_turn(task_data)" in source
    assert '"Execute the task as described' not in source


# ── P10: checkpoint guidance renders only for execution ─────────────────────


@pytest.mark.parametrize("session", sorted(WORKER_SESSIONS))
def test_progress_reporting_only_in_execution(session):
    _, status, _, _ = WORKER_SESSIONS[session]
    text = compose_worker_session(session).text
    heading = "## Progress Reporting — Substantive Checkpoints Only"
    if status in ("review", "blocked"):
        assert heading not in text
    else:
        assert text.count(heading) == 1


# ── C3: the worker memory guidance names the slug lookup ────────────────────


def test_worker_memory_guidance_uses_index_slugs():
    from src.orchestrator.worker_prompt import build_worker_prompt
    from tests.evals._prompt_composition import worker_task_data

    task = {
        **worker_task_data("execute"),
        "workstream_memory_index": (
            '- [lesson] Pin the TLS check — run openssl first. (slug="tls")'
        ),
    }
    text = norm(build_worker_prompt(task))
    assert "Each index line ends with its slug" in text
    assert "`recall(slug=…)`" in text
    assert "their bodies are inline" in text
    # The old instruction sent the model to search for what it already had.
    assert "search it with `recall`" not in text


# ── P12: no dangling cross-references ──────────────────────────────────────


_PREFLIGHT_STATES = {
    "artifacts": {"artifacts": True},
    "rework": {"rework_count": 1},
    "script_resume": {
        "script_handoff_results": [{"execution_id": "exec-1", "state": "completed"}],
    },
    "capacity_resume": {"capacity_wait_resume": {"operation_id": "op-1"}},
    "activity": {"recent_activities": [{"event_type": "checkpoint", "content": "x"}]},
    "fresh": {},
}


def _preflight(state: str) -> str:
    import re

    from src.orchestrator._execution_preflight import build_execution_preflight

    overrides = dict(_PREFLIGHT_STATES[state])
    artifacts = "- `report.md`" if overrides.pop("artifacts", False) else ""
    task = {
        "task_id": "t1", "readable_id": "WS-001.T01", "status": "in_progress",
        "rework_count": 0, **overrides,
    }
    text = "\n".join(build_execution_preflight(
        task, output_dir="/workspace/outputs/ws", artifacts_info=artifacts,
        workstream_claude_md_path=None, workstream_spec_md_path=None,
    ))
    assert re.search(r"BRANCH [A-Z0] \(", text), state
    return text


@pytest.mark.parametrize("state", sorted(_PREFLIGHT_STATES))
def test_preflight_states_the_orphan_rule_in_every_state(state):
    """R5: the orphan-file rule is stated where the Glob runs, so every
    branch (C, D, S and the capacity resume included) carries it."""
    import re

    text = norm(_preflight(state))
    assert (
        "treat them as orphan files: register one (per 0.5) only if it is a "
        "contracted deliverable matching the Brief's Output Format"
    ) in text
    assert "(see Branch B below)" not in text
    assert "the branch selected in 0.4 says how" not in text
    # Any cross-reference to a branch points at one that renders.
    for letter in re.findall(r"(?:see|per|in) Branch ([A-Z0])\b", text, re.IGNORECASE):
        assert f"BRANCH {letter.upper()} (" in text, (state, letter)


@pytest.mark.parametrize("session", sorted(WORKER_SESSIONS))
def test_preflight_never_points_at_an_unrendered_branch(session):
    import re

    text = compose_worker_session(session).text
    assert "(see Branch B below)" not in text
    for letter in re.findall(r"(?:see|per|in) Branch ([A-Z0])\b", text, re.IGNORECASE):
        assert f"BRANCH {letter.upper()} (" in text, (session, letter)


def test_ask_sessions_read_no_marker_as_their_closing_fallback():
    text = norm(compose_worker_session("ask_execute").text)
    assert "an ask writes no marker" in text
    # The assignment fallback still names the marker it relies on.
    assert "`COMPLETED.json` completion marker (STEP 0.7 of its task prompt)" in text


def test_flow_consult_log_fence_points_at_the_directive_above():
    from src.orchestrator.flow_consult_prompt import build_flow_architect_prompt

    prompt = build_flow_architect_prompt(
        {
            "directive": "Add an approval gate.",
            "design_log_tail": [{"role": "user", "text": "earlier idea"}],
        }
    )
    assert prompt.index("## Directive (your work order)") < prompt.index(
        "<design_log>"
    )
    assert "your work order is the Directive section above" in prompt
    assert "Directive section below" not in prompt


# ── P2: a no-scope research consult never de-approves the spec ─────────────


def test_unscoped_research_persists_to_a_file_not_an_approved_spec():
    from tests.evals._prompt_composition import compose_planner

    from tests.evals._prompt_composition import PLANNER_CONSULT_ID

    composed = compose_planner("research", scoped=False)
    prompt = norm(dict(composed.parts)["consult_prompt"])
    path = f"/workspace/workstreams/website/research/{PLANNER_CONSULT_ID}.md"
    assert f"`Write` them to `{path}` (create the `research/` folder)" in prompt
    assert "the Manager's result notice points at exactly that file" in prompt
    assert "Never put them in an APPROVED spec" in prompt
    assert "Only while `get_spec` shows the spec is still a DRAFT" in prompt
    # The old instruction sent research findings into the spec via update_spec
    # unconditionally — on an approved spec that starts a new draft.
    assert "write them into the workstream spec via `update_spec`" not in prompt
    playbook = norm(dict(composed.parts)["role"])
    assert "Never `update_spec` an approved spec from research" in playbook
    assert "none: the research file your consult prompt names" in playbook
    assert "else the spec's Open Questions" not in playbook


@pytest.mark.parametrize("row", [
    {"name": "Website"},
    {"name": "Website", "workspace_dir": "web-site"},
    {},
])
async def test_unscoped_research_poke_names_the_file_the_planner_wrote(row):
    """R1: the result poke recomputes the exact path the Planner prompt gave,
    from the same consult id and ConfigStore row — never the plan or spec."""
    from unittest.mock import AsyncMock, MagicMock

    from src.orchestrator import _manager_action_requests as mar
    from src.orchestrator.planner_prompt import build_planner_prompt, planner_research_path

    consult_id = "planner-abcdef012345"
    controller = MagicMock()
    controller._config.get_workstream = MagicMock(
        return_value={"id": "w3", **row} if row else None
    )
    controller._config.get_scopes_for_workstream = MagicMock(return_value=None)
    controller.handle_chat_message = AsyncMock(return_value=True)
    await mar.ingest_planner_result(
        controller,
        {
            "planner_consult": {"mode": "research", "workstream_id": "w3", "scope_id": ""},
            "task_id": consult_id,
        },
    )
    poke = norm(controller.handle_chat_message.await_args.args[0]["user_message"])
    path = planner_research_path(consult_id, row)
    assert path and path.endswith(f"/research/{consult_id}.md")
    assert f"Its findings are in `{path}` — `Read` it" in poke
    assert "get_execution_plan" not in poke
    assert "written findings into the" not in poke
    # The Planner was told the same path.
    prompt = norm(build_planner_prompt({
        "task_id": consult_id,
        "planner_consult": {"mode": "research", "workstream_id": "w3"},
        "workstream_context": dict(row),
    }))
    assert f"`Write` them to `{path}`" in prompt


async def test_scoped_research_poke_points_at_the_scope_plan():
    from unittest.mock import AsyncMock, MagicMock

    from src.orchestrator import _manager_action_requests as mar

    controller = MagicMock()
    controller._config.get_workstream = MagicMock(return_value={"id": "w3", "name": "Web"})
    controller._config.get_scopes_for_workstream = MagicMock(return_value=None)
    controller.handle_chat_message = AsyncMock(return_value=True)
    await mar.ingest_planner_result(
        controller,
        {
            "planner_consult": {"mode": "research", "workstream_id": "w3", "scope_id": "s1"},
            "task_id": "planner-abcdef012345",
        },
    )
    poke = norm(controller.handle_chat_message.await_args.args[0]["user_message"])
    assert "into the scope's execution plan. Read them via get_execution_plan" in poke
    assert "/research/" not in poke


def test_manager_program_module_says_where_research_lands():
    from tests.evals._prompt_composition import composed_manager_norm

    text = composed_manager_norm("program_workstream")
    assert (
        "`research` (consented programs only) — investigate a question; findings "
        "go to the scope's plan "
        "(`scope_id`) or to a research file the result poke names."
    ) in text
    assert "investigate a question and write findings into the plan" not in text


def test_backend_research_bubble_no_longer_claims_the_plan():
    from pathlib import Path

    handler = (
        Path(__file__).resolve().parents[3]
        / "backend" / "app" / "ws" / "tool_endpoint" / "_handlers_planner.py"
    )
    if not handler.exists():
        pytest.skip("backend tree not present (standalone communicator checkout)")
    source = handler.read_text()
    assert "research written into the plan" not in source
    assert "research findings recorded" in source


def test_update_spec_on_an_approved_spec_starts_a_draft_backend_ground_truth():
    """The fact P2 relies on, read from the backend service source."""
    from pathlib import Path

    service = Path(__file__).resolve().parents[3] / "backend" / "app" / "specs" / "service.py"
    if not service.exists():
        pytest.skip("backend tree not present (standalone communicator checkout)")
    source = service.read_text()
    assert 'if spec.status == "approved":' in source
    assert 'spec.status = "draft"' in source


# ── C4: the Planner reads the policy and Workstream Instructions ───────────


@pytest.mark.parametrize("mode", ["specify", "materialize", "verify"])
def test_planner_reads_work_policy_and_workstream_instructions(mode):
    from tests.evals._prompt_composition import compose_planner

    text = norm(compose_planner(mode).text)
    assert "Office work policy (in this CLAUDE.md)" in text
    assert "Workstream Instructions" in text
    # Office instructions (claude_md_content) are Manager-only.
    assert "Read current office and workstream instructions" not in text
    assert "current office/workstream instructions and approved spec" not in text


# ── P7: the Planner's consult Bash rule matches its catalog ────────────────


def test_planner_bash_rule_matches_its_add_activity_tool():
    from tests.evals._prompt_composition import compose_planner

    composed = compose_planner("materialize")
    assert "add_activity" in composed.tool_names
    text = norm(composed.text)
    assert "You hold no activity tool" not in text
    assert "`add_activity` posts only on real board tasks" in text


def test_consult_agents_without_add_activity_keep_the_no_tool_clause():
    from src._agent_image._mcp.tools_data_curator import get_data_curator_tools
    from src._agent_image._mcp.tools_flow_architect import get_flow_architect_tools
    from src.config_sync.claude_md_templates._system_agents import (
        DATA_CURATOR_CLAUDE_MD,
        FLOW_ARCHITECT_CLAUDE_MD,
    )

    for tools, playbook in (
        (get_flow_architect_tools(), FLOW_ARCHITECT_CLAUDE_MD),
        (get_data_curator_tools(), DATA_CURATOR_CLAUDE_MD),
    ):
        assert "add_activity" not in {tool["name"] for tool in tools}
        assert "You hold no activity tool" in norm(playbook)


# ── P3: a manager-approval draft never skips the program consent ───────────


class _EmptyStore:
    def get_workstream_list(self):
        return []

    def get_team_roster(self):
        return ""


def _draft_banner(work_mode: str | None) -> str:
    from src.orchestrator.manager_context import build_dynamic_context

    data = {
        "workstream_id": "w1",
        "workstream_name": "Recruitment",
        "spec_approval": "manager",
        "spec": {"status": "draft", "spec_approval": "manager", "revision": 1},
    }
    if work_mode is not None:
        data["work_mode"] = work_mode
    rendered = build_dynamic_context("workstream:w1", data, _EmptyStore())
    start = rendered.index("## Workstream Spec — DRAFT awaiting YOUR approval")
    end = rendered.find("\n## ", start + 1)
    return norm(rendered[start:] if end == -1 else rendered[start:end])


@pytest.mark.parametrize("work_mode", ["default", None])
def test_manager_approval_draft_asks_for_program_consent_first(work_mode):
    banner = _draft_banner(work_mode)
    consent = banner.index('`ask_user_choice(kind="execution_mode")`')
    assert consent < banner.index("**`approve_spec` (workstream_id=…)**")
    assert consent < banner.index("(`create_scope`)")
    assert "`approve_spec` never starts the program" in banner
    # The old banner denied any user gate, contradicting the program module.
    assert "there is NO user gate" not in banner
    assert "Do NOT ask the user to approve it" in banner


def test_program_workstream_draft_goes_straight_to_approval():
    banner = _draft_banner("program")
    assert "execution_mode" not in banner
    assert "4. If it's solid → **`approve_spec` (workstream_id=…)**" in banner


def test_approve_spec_never_starts_a_program_backend_ground_truth():
    """The facts P3 relies on: only the user's own click flips work_mode, and
    scope creation is refused outside program mode."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "backend" / "app"
    specs = root / "specs" / "service.py"
    if not specs.exists():
        pytest.skip("backend tree not present (standalone communicator checkout)")
    assert "if via_user_click and ws is not None:" in specs.read_text()
    scopes = (root / "scopes" / "service.py").read_text()
    assert '!= "program"' in scopes


@pytest.mark.asyncio
async def test_specify_success_poke_orders_consent_before_approval():
    from unittest.mock import AsyncMock, MagicMock

    from src.orchestrator import _manager_action_requests as mar

    controller = MagicMock()
    controller._config.get_workstream = MagicMock(
        return_value={"id": "w2", "name": "Hiring", "spec_approval": "manager"}
    )
    controller._config.get_scopes_for_workstream = MagicMock(return_value=None)
    controller.handle_chat_message = AsyncMock(return_value=True)
    await mar.ingest_planner_result(
        controller,
        {
            "planner_consult": {"mode": "specify", "workstream_id": "w2"},
            "task_id": "planner-p3",
        },
    )
    poke = norm(controller.handle_chat_message.await_args.args[0]["user_message"])
    consent = poke.index('`ask_user_choice(kind="execution_mode")` FIRST')
    assert "in a manager-approval workstream that is not a program yet" in poke
    assert consent < poke.index("approve it YOURSELF with `approve_spec`")
    assert consent < poke.index("(`create_scope`")


@pytest.mark.parametrize(
    ("work_mode", "work_line", "step_4"),
    [
        ("program", "Work mode: **program**", "4. If it's solid → **`approve_spec`"),
        (
            "default",
            "Work mode: **default**",
            "4. If it's solid → this workstream is NOT a program yet:",
        ),
    ],
)
@pytest.mark.asyncio
async def test_specify_poke_turn_carries_the_synced_work_mode(
    work_mode, work_line, step_4,
):
    """P3: the poke's context envelope passes work_mode through, so the
    draft-review chip shows the known dial instead of the 'if … not a
    program yet' hedge."""
    from unittest.mock import AsyncMock, MagicMock

    from src.config_sync.sync_service import ConfigStore
    from src.orchestrator import _manager_action_requests as mar
    from src.orchestrator.manager_context import build_dynamic_context

    controller = MagicMock()
    controller._config.get_workstream = MagicMock(
        return_value={
            "id": "w2", "name": "Hiring", "spec_approval": "manager",
            "work_mode": work_mode,
        }
    )
    controller._config.get_scopes_for_workstream = MagicMock(return_value=None)
    controller.handle_chat_message = AsyncMock(return_value=True)
    await mar.ingest_planner_result(
        controller,
        {
            "planner_consult": {"mode": "specify", "workstream_id": "w2"},
            "task_id": "planner-p3mode",
        },
    )
    message = controller.handle_chat_message.await_args.args[0]
    assert message["context_data"]["work_mode"] == work_mode
    text = norm(
        build_dynamic_context(
            message["context_key"], message["context_data"], ConfigStore(),
            is_fresh_session=False,
        )
    )
    assert work_line in text
    assert "Work mode: unknown this turn" not in text
    assert step_4 in text
    assert "if this workstream is not a program yet" not in text


# ── P6: only task-bound blocker approvals auto-unblock ─────────────────────


def test_invariant_4_does_not_promise_a_setup_secret_unblock():
    from tests.evals._prompt_composition import composed_manager_norm

    text = composed_manager_norm("default_workstream")
    invariant = text.split("4. **Blocked tasks never spontaneously auto-unblock.**")[1]
    invariant = invariant.split("5. **Action requests are deduped")[0]
    assert "`setup_office_secret`, (auto-promotes" not in invariant
    assert "or `setup_office_secret` (auto-promotes" not in invariant
    # fx-prompts C3e-G2: a missing secret now blocks through a
    # missing_credential escalation, and adding the secret resumes the task
    # (backend reconciliation, fx-lifecycle) — never the legacy request type.
    assert "`setup_office_secret`" not in invariant
    # Only an escalation that NAMES the secret (office_secret_names) is
    # reconciled on save, so the rule says "names".
    assert (
        "The user adding the secret a `missing_credential` escalation names "
        "closes it and resumes the task unless a gate refuses" in invariant
    )
    assert "A user's decision resets the counter, yours counts a bounce" in invariant
    paths = text.split("### Blocked tasks — paths out")[1]
    assert "(a) an approved `escalate_blocker` / `request_clarification` on that task" in (
        paths
    )
    assert "(a) an approved Inbox action_request" not in paths


def test_secret_save_resumes_the_source_task_through_gated_auto_unblock_backend_ground_truth():
    """R6, revised by C3e-automation-gaps:G2 (shared decision 2): the
    secret-SAVE path ``auto_resolve_setup_office_secret_for_name`` treats the
    user's save as the human decision and resumes a task-bound request's
    blocked source task through the decide route's gated auto-unblock
    (``_maybe_auto_unblock_source_task``), never a raw status write or a
    ``move_task``. Pin that body, not a nearby string. Behavior is covered
    by backend/tests/test_office_secrets.py and
    backend/tests/test_lifecycle_backstops.py. Manager invariant #4 states
    the same rule for a named ``missing_credential`` escalation."""
    import ast
    from pathlib import Path

    service = (
        Path(__file__).resolve().parents[3]
        / "backend" / "app" / "action_requests" / "service.py"
    )
    if not service.exists():
        pytest.skip("backend tree not present (standalone communicator checkout)")
    tree = ast.parse(service.read_text())
    funcs = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    resolve = funcs["auto_resolve_setup_office_secret_for_name"]
    called = {
        (node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", ""))
        for node in ast.walk(resolve)
        if isinstance(node, ast.Call)
    }
    assert "_maybe_auto_unblock_source_task" in called
    assert "apply_decision_side_effects" not in called
    assert "move_task" not in called
    assigned = {
        target.attr
        for node in ast.walk(resolve)
        if isinstance(node, (ast.Assign, ast.AugAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Attribute)
    }
    # The request row's own status is set; the TASK's fields only move
    # inside the gated auto-unblock helper.
    assert not assigned & {"blocked_bounce_count", "last_blocked_triage_at"}
    # The generic path really does unblock this type — so the prompt must
    # not claim a setup row can never resume a task.
    decisions = service.with_name("decisions.py")
    types = next(
        node.value
        for node in ast.walk(ast.parse(decisions.read_text()))
        if isinstance(node, ast.AnnAssign)
        and getattr(node.target, "id", "") == "AUTO_UNBLOCK_REQUEST_TYPES"
    )
    assert "setup_office_secret" in ast.literal_eval(types.args[0])
    from tests.evals._prompt_composition import composed_manager_norm

    text = composed_manager_norm("default_workstream")
    assert "A `setup_office_secret` request is office-wide:" not in text
    assert "The user adding a secret approves its `setup_office_secret` request" not in text


# ── P11: the self-check allows office files, forbids deliverables ──────────


def test_manager_self_check_is_scoped_to_deliverables():
    from tests.evals._prompt_composition import composed_manager_norm

    text = composed_manager_norm("default_workstream")
    assert (
        "1. Am I about to call `Bash`, a script-authoring tool, or `Write`/`Edit` "
        "for a task deliverable (office files per the built-ins list are fine)?"
    ) in text
    assert "1. Am I about to call `Write`, `Edit`, `Bash`" not in text
    assert "`Write`, `Edit` — **office files ONLY**" in text


# ── P13: a finished scope pokes the Manager to open the next milestone ─────


def test_scope_done_pokes_for_the_next_milestone():
    from tests.evals._prompt_composition import composed_manager_norm

    text = composed_manager_norm("program_workstream")
    assert (
        "**done** — verification PASSED; you're poked to open the next "
        "milestone's scope."
    ) in text
    assert "the next `ready` scope auto-promotes" not in text


# ── C13: the FLOW TIER passes attached files as materials ─────────────────


def test_flow_tier_passes_materials():
    from src._agent_image._mcp.tools_manager import get_manager_tools
    from tests.evals._prompt_composition import composed_manager_norm

    text = composed_manager_norm("program_flows")
    assert "Pass attached files as `materials` (workspace paths)" in text
    assert "(attached files as `inputs.materials`)" in text
    ask = next(t for t in get_manager_tools() if t["name"] == "ask_user_choice")
    assert "materials" in ask["inputSchema"]["properties"]


# ── P15: create_task says when NOT to use it ──────────────────────────────


def test_manager_create_task_names_when_not_to_use_it():
    from src._agent_image._mcp.tools_manager import get_manager_tools

    tool = next(t for t in get_manager_tools() if t["name"] == "create_task")
    description = norm(tool["description"])
    names = {t["name"] for t in get_manager_tools()}
    assert "NOT for: recurring work (`schedule_assignment`)" in description
    assert "schedule_assignment" in names
    assert "planner, flow-architect or data-curator as assignee/reviewer" in (
        description
    )
    assert "milestone's tasks (Planner `materialize` authors them" in description


# ── P16: General Chat names get_action_request once ───────────────────────


def test_general_chat_names_get_action_request_once():
    from src.config_sync.claude_md_templates._manager_modules import (
        render_general_chat_procedures,
    )

    assert render_general_chat_procedures().count("`get_action_request`") == 1


# ── P8: retry_blocked_task is not a triage path — one statement ───────────


def test_ma_triage_session_states_one_retry_rule():
    text = norm(compose_worker_session("ma_triage").text)
    assert "Path D" not in text
    assert "paths A–D" not in text
    assert "`retry_blocked_task` is not a triage path" in text
    # P8 review: a still-blocked approved task takes Path C, never "leave it"
    # (fz-prompts U01: except at the bounce cap, where a person decided).
    assert "take Path C — file `escalate_blocker` naming the remaining gate" in text
    assert "say so in your synthesis comment and leave it" not in text
    assert "The pending request stops the hourly re-triage" in text


def test_triage_refusal_matches_the_playbook():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "src" / "_agent_image" / "mcp_tool_server.py"
    spec = importlib.util.spec_from_file_location("_mts_p8", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    text = module.TRIAGE_PATHS_TEXT
    assert "Path D" not in text
    assert "when its gates allow" in text
    assert "otherwise escalate the remaining gate" in text
    assert "already returns the task to ready" not in text


# ── P9 / C5: the Manager Assistant closes per task class, ask first ───────


def test_ma_execute_mode_leads_with_the_ask_close():
    text = norm(compose_worker_session("ma_execute").text)
    mode = text.split("- **`execute`** — a quick task assigned to you (Role 1).")[1]
    mode = mode.split("- **`review`**")[0]
    assert mode.index("an `ask` closes with `move_task(done)`") < mode.index(
        "`update_status(review)`"
    )
    assert "`verification_input_fingerprint` only when the brief has a" in mode
    assert "(pivot-1 T5)" not in text
    assert "host key fingerprint, etc.). Then `update_status('review')` and STOP." not in (
        text
    )
    assert "Then close per your task class (an `ask`: `move_task(done)`;" in text
    assert "5. Close per your task class: an `ask` →" in text


# ── P4: custom-agent handoffs match the lifecycle contract and D1 ──────────


@pytest.mark.parametrize("surface", ["detail", "from_description", "instructions_gen"])
def test_custom_agent_generation_handoffs_match_the_contract(surface):
    from src._lifecycle_contract import EXECUTE_BLOCKER_FACT
    from src._setup_prompts import AGENT_DETAIL_PROMPT, AGENT_FROM_DESCRIPTION_PROMPT
    from src.setup_generator import AGENT_INSTRUCTIONS_GEN_PROMPT

    text = norm(
        {
            "detail": AGENT_DETAIL_PROMPT,
            "from_description": AGENT_FROM_DESCRIPTION_PROMPT,
            "instructions_gen": AGENT_INSTRUCTIONS_GEN_PROMPT,
        }[surface]
    )
    # D1: skills are read files — never "invoked".
    assert '"**{skill-name}** — Read its SKILL.md when {specific condition}."' in text
    assert "invoke when" not in text
    # The one blocking call, per EXECUTE_BLOCKER_FACT.
    assert "ONE `update_status(blocked)` call" in norm(EXECUTE_BLOCKER_FACT)
    assert (
        "in execute mode, the ONE call that blocks the task when the agent "
        "cannot proceed"
    ) in text
    # P4: a reviewer blocks with move_task — update_status is not in its catalog.
    assert (
        'as a designated reviewer, block with ``move_task(new_status="blocked")`` '
        "instead"
    ) in text
    assert "``escalate_blocker(...)`` — flag an issue for the Manager/user; it does NOT block the task" in text
    assert "to stop, use the blocking call above for your phase" in text
    assert "to stop, make the ``update_status(blocked)`` call above" not in text
    assert "question; it does NOT block the task either" in text
    assert "question that blocks progress" not in text
    # propose_subtask is a same-scope follow-up, never a decomposition.
    assert "propose a same-scope follow-up subtask" in text
    assert "propose decomposing the current task" not in text


def test_propose_subtask_is_a_follow_up_in_its_tool_description():
    from src._agent_image._mcp.tools_worker import get_worker_tools

    tool = next(t for t in get_worker_tools() if t["name"] == "propose_subtask")
    assert "follow-up SUBTASK that should run inside the same Scope" in norm(
        tool["description"]
    )


# ── P14: no unverifiable catalog promises; runtime-dependent entries flagged


def test_catalog_prompts_make_no_install_promise():
    import src._setup_prompts as setup_prompts
    from src.setup_generator import AGENT_INSTRUCTIONS_GEN_PROMPT

    texts = [
        norm(value)
        for value in vars(setup_prompts).values()
        if isinstance(value, str)
    ] + [norm(AGENT_INSTRUCTIONS_GEN_PROMPT)]
    for text in texts:
        assert "battle-tested" not in text
        assert "installs intact" not in text
        assert "arrive with reference files" not in text


def test_catalog_flags_entries_the_agent_image_cannot_run():
    from pathlib import Path

    from src._setup_prompts import _CATALOG_RUNTIME_PACKAGES, _format_catalog_for_prompt

    catalog = [
        {"id": "anthropic-webapp-testing", "display_name": "Web", "description": "d",
         "category": "Dev"},
        {"id": "anthropic-pdf", "display_name": "PDF", "description": "d", "category": "Docs"},
        {"id": "code-review", "display_name": "Review", "description": "d", "category": "Dev"},
    ]
    rendered = _format_catalog_for_prompt(catalog)
    assert (
        "``anthropic-webapp-testing`` (bundled) — Web: d [needs runtime packages: Playwright"
        in rendered
    )
    # The image ships the document skills' runtime, so they are not flagged.
    assert "``anthropic-pdf`` (bundled) — PDF: d\n" in rendered + "\n"
    assert "``code-review`` (bundled) — Review: d\n" in rendered + "\n"
    dockerfile_path = (
        Path(__file__).resolve().parents[2] / "src" / "_agent_image" / "Dockerfile.agent"
    )
    dockerfile = "\n".join(
        line
        for line in dockerfile_path.read_text().lower().splitlines()
        if not line.lstrip().startswith("#")
    )
    # Ground truth: the still-flagged runtime does not ship in the image (its
    # comments may name it); slack-gif-creator's imageio does, so it is not
    # flagged.
    assert "playwright" not in dockerfile
    assert "imageio" in dockerfile
    assert "anthropic-slack-gif-creator" not in _CATALOG_RUNTIME_PACKAGES
    templates = Path(__file__).resolve().parents[3] / "backend" / "app" / "skills" / "templates.py"
    if templates.exists():
        source = templates.read_text()
        for template_id in _CATALOG_RUNTIME_PACKAGES:
            assert f'"id": "{template_id}"' in source, template_id


# ── P5: secret tools state the one blocking call, role-neutrally ──────────


def _served(agent: str, name: str) -> dict:
    from src._agent_image.mcp_tool_server import select_session_tools

    tools = select_session_tools("worker", agent, "execute")
    return next(t for t in tools if t["name"] == name)


def test_missing_secret_is_one_blocked_status_update():
    from src.config_sync.claude_md_templates._system_agents import (
        AUTOMATION_SCRIPT_DEV_CLAUDE_MD,
    )

    bind = norm(_served("automation-script-developer", "bind_script_variable")["description"])
    assert "block with ONE ``update_status(blocked)``" in bind
    assert "``ESCALATED (missing_credential):``" in bind
    # fw3-automation: the one blocking call names the secret.
    assert "with the secret in ``office_secret_names``" in bind
    assert "escalate_blocker(" not in bind
    assert "then retry this tool" not in bind
    playbook = norm(AUTOMATION_SCRIPT_DEV_CLAUDE_MD)
    assert (
        "block with ONE `update_status(blocked, office_secret_names=[<Office "
        "Secret name>])` call whose comment starts `ESCALATED (missing_credential):`"
        in playbook
    )


def test_list_office_secrets_is_role_neutral_for_every_worker():
    for agent in ("builder", "analyst", "automation-script-developer"):
        text = norm(_served(agent, "list_office_secrets")["description"])
        assert "the Automation Script Developer binds a matching Office Secret" in text
        assert "bind it yourself" not in text
        assert "then YOU bind" not in text
        assert "escalate_blocker(" not in text


# ── T28: D1 — no surface claims skills load automatically, in any phrasing ─


@pytest.mark.parametrize("planted", [
    "Claude auto-discovers the skills in your folder.",
    "Skills are loaded automatically at session start.",
    "The platform auto-loads each playbook.",
    "Skills are discovered automatically by the CLI.",
    "The CLI will automatically invoke the matching skill.",
    "Skills get invoked automatically when relevant.",
])
def test_skill_autoload_detector_catches_every_phrasing(planted):
    from tests.evals._prompt_composition import skill_autoload_claims

    assert skill_autoload_claims("Intro line.\n" + planted + "\n") != []


@pytest.mark.parametrize("negated", [
    "(native skill auto-discovery is off).",
    "Skills are not invoked automatically.",
    "Nothing loads a skill automatically.",
])
def test_skill_autoload_detector_honours_negation(negated):
    from tests.evals._prompt_composition import skill_autoload_claims

    assert skill_autoload_claims(negated) == []


def test_no_composed_session_claims_skills_load_automatically():
    from tests.evals._prompt_composition import (
        MANAGER_CONTEXTS,
        PLANNER_MODES,
        compose_manager,
        compose_planner,
        skill_autoload_claims,
    )

    texts = {f"worker:{s}": compose_worker_session(s).text for s in WORKER_SESSIONS}
    texts |= {f"manager:{c}": compose_manager(c).text for c in MANAGER_CONTEXTS}
    texts |= {f"planner:{m}": compose_planner(m).text for m in PLANNER_MODES}
    offenders = {name: skill_autoload_claims(text) for name, text in texts.items()}
    assert not {name: claims for name, claims in offenders.items() if claims}


def test_review_and_triage_name_the_admitted_output_directory():
    """P10 removed the executor checkpoint example that used to carry the
    task directory into review/triage prompts; the admitted directory is
    still stated so a retained historical path cannot stand in for it."""
    for session in ("auditor_review", "ma_review", "ma_triage"):
        text = norm(compose_worker_session(session).text)
        assert "Task output directory: `" in text, session
        assert "where this task's requested documents live" in text, session
    execute = norm(compose_worker_session("builder_execute").text)
    assert "Task output directory: `" not in execute


def test_triage_names_the_gate_comments_the_backend_posts():
    """P8 review: the MA recognises a refused auto-unblock by the comments
    ``_maybe_auto_unblock_source_task`` really posts."""
    from pathlib import Path

    service = (
        Path(__file__).resolve().parents[3]
        / "backend" / "app" / "action_requests" / "service.py"
    )
    if not service.exists():
        pytest.skip("backend tree not present (standalone communicator checkout)")
    source = service.read_text()
    assert '"Auto-unblock refused' in source
    assert '"Auto-unblock skipped' in source
    text = norm(compose_worker_session("ma_triage").text)
    assert '**"Auto-unblock skipped"**' in text
    assert '**"Auto-unblock refused"**' in text


# ── R2 / P15: consult catalogs describe only tools they hold ──────────────


def _descriptions(schema) -> list[str]:
    found = []
    if isinstance(schema, dict):
        if isinstance(schema.get("description"), str):
            found.append(schema["description"])
        for value in schema.values():
            found += _descriptions(value)
    elif isinstance(schema, list):
        for value in schema:
            found += _descriptions(value)
    return found


def _all_tool_names() -> set[str]:
    from src._agent_image._mcp.tools_data_curator import get_data_curator_tools
    from src._agent_image._mcp.tools_flow_architect import get_flow_architect_tools
    from src._agent_image._mcp.tools_manager import get_manager_tools
    from src._agent_image._mcp.tools_planner import get_planner_tools
    from src._agent_image._mcp.tools_worker import get_worker_tools

    names: set[str] = set()
    for loader in (get_manager_tools, get_worker_tools, get_planner_tools,
                   get_flow_architect_tools, get_data_curator_tools):
        names |= {tool["name"] for tool in loader()}
    return names


@pytest.mark.parametrize("agent", ["planner", "flow-architect", "data-curator"])
def test_consult_catalog_descriptions_name_only_their_own_tools(agent):
    """Every backticked tool name in a consult catalog's descriptions (at any
    depth) is a tool that catalog serves."""
    import re

    from src._agent_image.mcp_tool_server import select_session_tools

    tools = select_session_tools("worker", agent, "execute")
    served = {tool["name"] for tool in tools}
    known = _all_tool_names()
    foreign = {}
    for tool in tools:
        for text in _descriptions(tool):
            for token in re.findall(r"`{1,2}([a-z][a-z_]*)", text):
                if token in known and token not in served:
                    foreign.setdefault(token, set()).add(tool["name"])
    assert not foreign, foreign


def test_planner_create_task_is_written_for_the_planner():
    from src._agent_image._mcp.tools_manager import get_manager_tools
    from src._agent_image._mcp.tools_planner import get_planner_tools

    planner = next(t for t in get_planner_tools() if t["name"] == "create_task")
    manager = next(t for t in get_manager_tools() if t["name"] == "create_task")
    text = norm(planner["description"])
    assert "milestone's tasks" not in text
    assert "schedule_assignment" not in text
    assert "in materialize the scope's breakdown tasks" in text
    assert "in a verify FAIL the rework tasks (with depends_on)" in text
    assert "planner, flow-architect or data-curator as assignee/reviewer is refused" in text
    # Same schema; the Manager's own P15 text is untouched.
    assert planner["inputSchema"] == manager["inputSchema"]
    assert "NOT for: recurring work (`schedule_assignment`)" in manager["description"]



# ── R3: every auto-unblock promise names the gates that can refuse it ─────


def test_auto_unblock_claims_name_the_bounce_cap_refusal():
    from src.config_sync._auto_decide_rows import AUTO_DECIDE_PREAMBLE, AUTO_DECIDE_ROWS
    from tests.backend_boundary import import_backend
    from tests.evals._prompt_composition import composed_manager_norm

    board = import_backend("app.tasks.board")
    assert board.MAX_BLOCKED_BOUNCES == 1  # the prompts say "the bounce cap is 1"
    text = composed_manager_norm("default_workstream")
    invariant = text.split("4. **Blocked tasks never spontaneously auto-unblock.**")[1]
    invariant = invariant.split("5. **Action requests are deduped")[0]
    assert "for YOUR approval only below the bounce cap" in invariant
    # fx-prompts C3b-G2/C4a-G1: one at-cap rule in the invariant AND in the
    # paths-out section — no second agent approval, one concrete change +
    # one retry, or a rejection that routes the request to the user.
    assert (
        'At the cap ("Auto-unblock refused" since the task last entered blocked) '
        "never approve" in invariant
    )
    assert "`retry_blocked_task` once naming it" in invariant
    assert "goes to the user's Inbox" in invariant
    # R00: the backend refuses an agent's second retry past the cap.
    assert "a second cap hit goes to the user's Inbox and retry is refused" in invariant
    # A helper task resumes the task by auto-promotion; retry_blocked_task
    # refuses unmet depends_on, so pairing the two wasted the at-cap turn.
    assert (
        "a brief edit or reassignment, then `retry_blocked_task` once naming it; "
        "or a helper task, whose completion auto-promotes the task (retry is "
        "refused until it is done)" in invariant
    )
    assert "reassignment or helper task) and" not in invariant
    assert "REJECT so the request" not in invariant
    assert "fix the cause, then `retry_blocked_task`" not in invariant
    blocked_paths = text.split("### Blocked tasks — paths out")[1].split("###")[0]
    assert "archive + recreate" not in blocked_paths
    assert "At the bounce cap follow Invariant #4" in blocked_paths
    paths = text.split("### Blocked tasks — paths out")[1][:400]
    assert "(auto-promotes it unless a gate in Invariant #4 refuses;" in paths
    # C3e-G2: the secret the user adds resumes a missing_credential escalation.
    assert (
        "so does the user adding the secret a `missing_credential` escalation names"
        in paths
    )
    assert "no MA triage until it is decided" in blocked_paths
    assert "(the approval auto-promotes it)" not in paths
    assert "unless an Invariant #4 gate refuses — check `get_task_detail`" in text
    preamble = norm(AUTO_DECIDE_PREAMBLE)
    assert "for YOUR approval — the bounce cap" in preamble
    assert "Check `get_task_detail` before reporting it resumed" in preamble
    for row in ("escalate_blocker", "request_clarification"):
        assert "unless a gate refuses" in norm(AUTO_DECIDE_ROWS[row]), row


def test_bounce_cap_refusal_applies_to_agent_decided_approvals_backend_ground_truth():
    import ast
    from pathlib import Path

    service = (
        Path(__file__).resolve().parents[3]
        / "backend" / "app" / "action_requests" / "service.py"
    )
    if not service.exists():
        pytest.skip("backend tree not present (standalone communicator checkout)")
    tree = ast.parse(service.read_text())
    decisions = ast.parse(service.with_name("decisions.py").read_text())
    actors = next(
        node.value
        for node in ast.walk(decisions)
        if isinstance(node, ast.AnnAssign)
        and getattr(node.target, "id", "") == "AGENT_DECIDER_ACTORS"
    )
    deciders = {c.value for c in ast.walk(actors) if isinstance(c, ast.Constant)}
    assert {"manager", "manager-assistant"} <= deciders
    unblock = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_maybe_auto_unblock_source_task"
    )
    body = ast.unparse(unblock)
    assert "if not _human_decided:" in body
    assert "bounces >= MAX_BLOCKED_BOUNCES" in body
    assert "Auto-unblock refused" in body


# ── SEC-2: missing-secret refusals end in the phase's ONE blocking call ────


@pytest.fixture
def script_exec(monkeypatch):
    import importlib
    from pathlib import Path

    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[2] / "src" / "_agent_image")
    )
    return importlib.import_module("_mcp_script_exec")



def test_missing_secret_refusal_names_the_secrets_on_the_execute_block(script_exec):
    """fw3-automation: execute passes the names on its ONE blocking
    update_status call; the move_service backstop copies them into the
    escalation it files, so saving the secret resumes the task. No separate
    escalate_blocker call is needed."""
    text = script_exec.missing_secret_refusal("enrich", ["ENRICH_API_KEY"], "execute")
    call = (
        'update_status(new_status="blocked", '
        'office_secret_names=["ENRICH_API_KEY"])'
    )
    assert f"ONE `{call}` call" in text
    assert "`ESCALATED (missing_credential):`" in text
    assert "saving them resumes the task" in text
    assert "escalate_blocker" not in text
    assert "move_task" not in text
    assert "do not retry or wait in this session" in text


def test_missing_secret_refusal_names_the_secrets_on_the_review_block(script_exec):
    """L05: move_task carries office_secret_names too, so a reviewer names
    the secrets on its ONE blocking call — no separate escalate_blocker."""
    text = script_exec.missing_secret_refusal("enrich", ["ENRICH_API_KEY"], "review")
    call = (
        'move_task(new_status="blocked", '
        'office_secret_names=["ENRICH_API_KEY"])'
    )
    assert f"ONE `{call}` call" in text
    assert "`ESCALATED (missing_credential):`" in text
    assert "saving them resumes the task" in text
    assert "escalate_blocker" not in text
    assert "update_status" not in text
    assert "already emitted a setup_office_secret" not in text
    assert "wait for the user" not in text
    assert "do not retry or wait in this session" in text


@pytest.mark.parametrize("task_mode", ["execute", "review"])
def test_missing_secret_refusal_without_names_goes_straight_to_the_block(
    script_exec, task_mode,
):
    text = script_exec.missing_secret_refusal("enrich", [], task_mode)
    assert "office_secret_names" not in text
    assert "escalate_blocker" not in text
    assert "Block the task with ONE `" in text


def test_missing_secret_refusal_in_triage_escalates_instead(script_exec):
    text = script_exec.missing_secret_refusal("enrich", ["A", "B"], "triage")
    assert (
        'escalate_blocker(blocker_class="missing_credential", '
        'office_secret_names=["A", "B"])'
    ) in text
    assert "update_status" not in text
    assert "A, B" in text
    bare = script_exec.missing_secret_refusal("enrich", [], "triage")
    assert 'escalate_blocker(blocker_class="missing_credential")' in bare
    assert "office_secret_names" not in bare


def test_named_escalation_is_the_one_reconciliation_reads_backend_ground_truth():
    """RR-4: reconciliation closes a request only from its named secrets
    (``office_secret_names``, with class ``missing_credential``). The
    blocked-move backstop records the class of an ``ESCALATED (<class>):``
    comment and copies names ONLY from the explicit ``office_secret_names``
    of an ``update_status`` / ``move_task`` missing_credential block — never
    guessed from the comment. (Backend pins:
    ``test_backstop_escalation_class_alone_is_never_superseded`` and
    ``test_missing_credential_block_resume.py``.)"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "backend" / "app"
    recon = root / "action_requests" / "credential_reconciliation.py"
    if not recon.exists():
        pytest.skip("backend tree not present (standalone communicator checkout)")
    source = recon.read_text()
    assert 'if payload.get("blocker_class") != "missing_credential":' in source
    assert 'ActionRequest.payload["office_secret_names"].contains([secret_name])' in source
    assert "if names:" in source  # a class without names is never superseded
    move = (root / "tasks" / "move_service.py").read_text()
    backstop = move.split("async def _ensure_escalation_for_blocked_task")[1]
    backstop = backstop.split("\nasync def ")[0].split("\ndef ")[0]
    assert '"auto_created_on_block": True' in backstop
    assert (
        'if blocker_class == "missing_credential" and data.office_secret_names'
        in backstop
    )
    assert 'payload["office_secret_names"] = secret_names' in backstop


def test_execute_script_description_matches_the_blocker_contract():
    from src._agent_image._mcp.tools_worker import get_worker_tools

    desc = norm(
        next(t for t in get_worker_tools() if t["name"] == "execute_script")[
            "description"
        ]
    )
    assert "``ESCALATED (missing_credential):`` blocker naming it" in desc
    assert "never wait in-session" in desc
    assert "surface as a ``setup_office_secret``" not in desc


def test_bind_script_variable_missing_secret_text_backend_ground_truth():
    from pathlib import Path

    handler = (
        Path(__file__).resolve().parents[3]
        / "backend" / "app" / "ws" / "tool_endpoint" / "_handlers_scripts.py"
    )
    if not handler.exists():
        pytest.skip("backend tree not present (standalone communicator checkout)")
    source = handler.read_text()
    assert "escalate via escalate_blocker with" not in source
    assert "category=credentials so the user adds it once" not in source
    # SEC2-bind-phase: in step with missing_secret_refusal — phase-aware,
    # and the escalation names the secret for reconciliation. Behavior is
    # covered by backend/tests/test_tool_endpoint_role_gates.py.
    helper = source.split("def _missing_secret_bind_error(")[1].split("\ndef ")[0]
    assert '.get("task_mode")' in helper
    # fw3-automation + L05: execute and review name the secret on their ONE
    # blocking call.
    assert 'call = "move_task" if task_mode == "review" else "update_status"' in helper
    assert '{call}(new_status="blocked"{named})' in helper
    assert "office_secret_names=" in helper
    assert '"ESCALATED (missing_credential):"' in helper


# ── RR-2: the domain doc agrees with the prompts on agent-decided approvals ──


def test_task_lifecycle_doc_qualifies_approval_unblock_for_agent_decisions():
    from pathlib import Path

    doc = Path(__file__).resolve().parents[3] / "docs" / "02-domain" / "task-lifecycle.md"
    if not doc.exists():
        pytest.skip("docs tree not present (standalone communicator checkout)")
    text = norm(doc.read_text())
    assert "Action-request approval auto-unblock — additionally **resets** the counter" not in text
    assert "and **resets `blocked_bounce_count` to 0**" not in text
    assert "`service.py:1276-1278`" not in text
    assert "An agent-decided approval (`handled_by` in `AGENT_DECIDER_ACTORS`" in text
    assert "resets `blocked_bounce_count` to 0 for a human decision" in text
    assert text.count('"Auto-unblock refused"') >= 2


# ── SEC-2-docs: no doc promises a Runner-filed setup_office_secret card ─────


def test_docs_do_not_promise_a_runner_filed_setup_secret_request():
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    docs = root / "docs"
    if not docs.exists():
        pytest.skip("docs tree not present (standalone communicator checkout)")
    # Ground truth first: no code path creates the request type.
    creators = []
    for base in (root / "backend" / "app", root / "communicator" / "src"):
        for path in base.rglob("*.py"):
            # Keyword-argument or dict-literal creation; the models.py SQL
            # index predicate (request_type = '…' inside a string) is not one.
            if re.search(
                r'request_type\s*=\s*"setup_office_secret"'
                r'|["\']request_type["\']\s*:\s*["\']setup_office_secret["\']',
                path.read_text(),
            ):
                creators.append(str(path.relative_to(root)))
    assert creators == [], creators
    stale = (
        "dispatch layer emits one `setup_office_secret`",
        "already-emitted `setup_office_secret` action_request",
        "and a `setup_office_secret` **action_request**",
        "409 + a `setup_office_secret` action request",
        "fail to launch with a `setup_office_secret` action request",
        "| Script Runner when a referenced Office Secret is missing |",
        "the Script Runner's `setup_office_secret`",
    )
    for rel in (
        "02-domain/scripts.md",
        "02-domain/secrets-and-credentials.md",
        "02-domain/action-requests.md",
        "03-contracts/rest-api.md",
        "04-components/communicator.md",
    ):
        text = norm((docs / rel).read_text())
        for phrase in stale:
            assert phrase not in text, (rel, phrase)
    scripts = norm((docs / "02-domain" / "scripts.md").read_text())
    assert "No `setup_office_secret` request is filed" in scripts
    assert "`ESCALATED (missing_credential):`" in scripts
