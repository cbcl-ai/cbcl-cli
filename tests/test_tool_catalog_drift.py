"""Drift guard for the per-role MCP tool catalog (TOOL-07).

These tests pin the EXACT set of tool names each role exposes. When a tool
is added, removed, or renamed in ``_mcp/tools_*.py``, the matching set here
must be updated in the SAME change — and that's the prompt to also reconcile
the Manager/Worker/Planner specs + the agent CLAUDE.md playbooks so the AI
agents stay aligned with what they can actually call.

If this test fails: update the expected set below AND the corresponding
spec/playbook, then re-run. Do not loosen the assertion to a subset — the
whole point is that drift is loud.
"""
from __future__ import annotations

from src._agent_image._mcp.tools_data_curator import get_data_curator_tools
from src._agent_image._mcp.tools_flow_architect import (
    get_flow_architect_tools,
)
from src._agent_image._mcp.tools_manager import get_manager_tools
from src._agent_image._mcp.tools_planner import get_planner_tools
from src._agent_image._mcp.tools_worker import (
    get_worker_subcatalog,
    get_worker_tools,
)


def _names(tools: list[dict]) -> set[str]:
    return {t["name"] for t in tools}


# ── Expected catalogs (the live surface as of cbcl 0.2.91) ──────────────

_MANAGER_EXPECTED = {
    # Board + scope writes
    "create_task", "update_task", "move_task", "add_activity",
    "archive_task", "delete_task", "retry_blocked_task",
    "decide_action_request",
    "create_scope", "update_scope", "activate_scope", "archive_scope",
    # Planner consult + plan reads + verification close + spec read/approve
    # + the ONE plan write (update_execution_plan — the chip-flip surface for
    # the escalated stuck-verify recovery; verify turn-end incident 2026-07-17).
    # Pivot-1 T6: get_workstream_plan retired — the spec's Milestones section
    # (get_spec) absorbed the roadmap. Manager count 36→35.
    "consult_planner", "get_execution_plan",
    "update_execution_plan",
    "complete_scope_verification", "get_spec", "approve_spec",
    # Pivot-2 P1: the chat choice-selector — ask the user a 2-4-option
    # question; asking ends the turn (the consult_planner async posture).
    # Manager count 35→36.
    "ask_user_choice",
    # Pivot-3 P2-2 (D3.3/D3.5): standing operations — assignment schedules.
    # Backend-owned rows swept on due → a REAL op-class task on the normal
    # rails (kind='agent_task'), or a scheduled Manager digest turn
    # (kind='manager_digest'); overlap-skip while the prior run is
    # non-terminal. Manager/MA-gated backend-side. Manager count 36→40.
    "schedule_assignment", "update_assignment_schedule",
    "delete_assignment_schedule", "list_assignment_schedules",
    # Pivot-4 flow-intake (spec §B/§C): amend ONE answer of an answered
    # intake record (open cards are answered, never amended); register /
    # PATCH first-class office-flow definitions (define_flow is
    # user-consent-first at the playbook level). Manager/MA-gated
    # backend-side; excluded from the Planner composition and the worker
    # pool. Manager count 40→43.
    "amend_intake", "define_flow", "update_flow",
    # Flow Studio (FS-P2.T9, spec §7.2): the Manager operates flow RUNS
    # — start (user-consented via the run_flow card / explicit ask),
    # stop (archives open run tasks, keeps the manifest), and the status
    # read. Manager/MA-gated backend-side; all three Planner-excluded
    # (runs are operations, the Planner plans). The two writes are
    # stripped in General Chat; get_flow_run stays readable.
    # Manager count 43→46.
    "start_flow_run", "stop_flow_run", "get_flow_run",
    # ui-ux-aug19 D4.7: collection READS — the "what did the script
    # save?" leg of webhook→script→collection→Manager. Schemas pulled by
    # name from the worker pool, descriptions Manager-voiced; ungated
    # backend-side. Both names also join the Planner exclusion set so
    # the v1 Planner-no-collection-reads pin stays green.
    # Manager count 46→48.
    "get_collection", "query_rows",
    # Board + KB + files + scripts + office-secret READS
    "get_board", "get_task_detail", "list_agents",
    "list_scopes", "get_scope",
    "search_kb", "get_kb_document",
    "save_file", "list_files", "get_file",
    "list_scripts", "get_script", "list_script_executions",
    "list_script_templates", "get_script_template",
    "list_office_secrets", "list_office_secret_usage",
}

_WORKER_EXPECTED = {
    # Own-task tools
    "update_status", "add_activity", "get_my_brief", "get_task_detail",
    "update_task",
    # Board-operator (MA) writes — gated backend-side by _ROLE_GATES /
    # executor guard, NOT exposed to a normal executor at runtime.
    "create_task", "move_task",
    # Proposal family (route through the Action Request inbox)
    "propose_task", "propose_subtask", "propose_update_task",
    "propose_split_into_scope", "propose_artifact_handoff",
    "propose_spec_update",
    "escalate_blocker", "request_clarification", "request_review_check",
    # Scripts: execution + status + reads + AUTHORING (ASD-only; stripped
    # per-agent at runtime by _SCRIPT_AUTHOR_ONLY)
    "execute_script", "get_script_status",
    "list_scripts", "get_script", "list_script_executions",
    "list_script_crons",
    "list_script_templates", "get_script_template",
    "register_script", "clone_script", "install_script_from_template",
    "bind_script_variable", "schedule_script", "update_script_cron",
    "delete_script_cron",
    # KB + files + office-secret reads
    "search_kb", "get_kb_document",
    "save_file", "attach_to_task", "list_files", "get_file",
    "list_office_secrets", "list_office_secret_usage",
    # Flow Studio (FS-P3.T3): collection READS — the worker research
    # surface (a brief may reference collection data). Ungated
    # backend-side; the write tools live only in the Data Curator /
    # Flow Architect catalogs. Worker pool 39→41.
    "get_collection", "query_rows",
}

# The Planner gets the Manager board surface MINUS the destructive /
# manager-only verbs, PLUS the plan WRITE tools.
_PLANNER_EXCLUDED = {
    "consult_planner", "move_task", "delete_task", "archive_task",
    "retry_blocked_task", "decide_action_request",
    # approve_spec is Manager-only — the Planner authors the spec (update_spec)
    # but never approves it (the Manager reviews + signs off).
    "approve_spec",
    # ask_user_choice is Manager-only (pivot-2 P1) — the Planner never talks
    # to the user directly; its results arrive via the Manager poke.
    "ask_user_choice",
    # The assignment-schedule surface is Manager/MA-only (pivot-3 P2-2) —
    # standing-operation routing is the Manager's call; the Planner plans
    # programs, never operates them. All four excluded (the backend gates
    # the actions to manager/manager-assistant).
    "schedule_assignment", "update_assignment_schedule",
    "delete_assignment_schedule", "list_assignment_schedules",
    # Flows & intake records are Manager/MA-only (pivot-4 flow-intake):
    # amending a user's recorded decisions and registering office flows
    # take user-facing consent the Planner never holds. All three excluded
    # (the backend gates the actions to manager/manager-assistant).
    "amend_intake", "define_flow", "update_flow",
    # Flow runs are Manager/MA-only operations (Flow Studio FS-P2.T9):
    # starting rides user consent, stopping archives board tasks — the
    # Planner plans programs, never operates runs. All three excluded
    # (the backend gates the actions to manager/manager-assistant).
    "start_flow_run", "stop_flow_run", "get_flow_run",
    # ui-ux-aug19 D4.7: the collection reads joined the Manager catalog,
    # but the Flow Studio v1 pin stands — the Planner plans programs from
    # specs/board/KB and never reads collections
    # (test_planner_excludes_collection_reads_v1). Both excluded.
    "get_collection", "query_rows",
}
# update_spec is Planner-only (authors the spec + milestones); get_spec is
# shared (also in the Manager catalog, so the | with _MANAGER_EXPECTED already
# covers it). update_execution_plan is ALSO in the Manager base (the
# stuck-verify chip-flip surface) — it stays listed because
# PLANNER_PLAN_TOOLS carries it and the union is idempotent.
# Pivot-1 T6: update_workstream_plan retired with the roadmap artifact —
# update_spec's ``milestones`` param is the checklist write now.
# Planner count: 48 manager − 20 excluded + 1 net-new (update_spec) = 29
# (pivot-2 P1 added ask_user_choice, pivot-3 P2-2 the four
# assignment-schedule tools, pivot-4 flow-intake the three flow/intake
# tools, Flow Studio FS-P2.T9 the three flow-run tools, and ui-ux-aug19
# D4.7 the two collection reads, to both the Manager set and the
# exclusion set — the Planner surface is unchanged at 29).
_PLANNER_ADDED = {"update_execution_plan", "update_spec"}


def test_manager_tool_catalog_is_pinned() -> None:
    assert _names(get_manager_tools()) == _MANAGER_EXPECTED


def test_worker_tool_catalog_is_pinned() -> None:
    # This pins the unfiltered DEFINITION POOL. The served surface is one of
    # the three role sub-catalogs below (T5.1.1/T5.1.3).
    assert _names(get_worker_tools()) == _WORKER_EXPECTED


# ── Three named worker sub-catalogs (T5.1.1/T5.1.3) ─────────────────────
# The base pool carries create_task/move_task/update_task; registration-time
# filtering carves the role-appropriate surface. Pinned as EXACT sets, not
# subsets — drift must be loud.

_BOARD_WRITE = {"create_task", "move_task", "update_task"}
_MA_EXTRAS = {"retry_blocked_task", "get_board", "list_scopes"}


def test_executor_subcatalog_drops_all_board_writes() -> None:
    # A plain executor (TASK_MODE=execute, ordinary agent name) sees none of
    # the board-write tools — its only board-write path is the propose family.
    executor = _names(get_worker_subcatalog("execute", "senior-python-developer"))
    assert executor == _WORKER_EXPECTED - _BOARD_WRITE
    for forbidden in _BOARD_WRITE | _MA_EXTRAS | {"archive_task"}:
        assert forbidden not in executor, f"executor must not expose {forbidden}"


def test_ask_executor_subcatalog_keeps_move_task() -> None:
    # Pivot-1 T5 (C-3): an ask-class executor keeps move_task — ask tasks skip
    # Review, so the assignee closes its own task straight to done. The
    # runtime executor guard confines it to move_task(done) on the CURRENT
    # task. EXACT set: pool minus create_task/update_task only.
    ask = _names(
        get_worker_subcatalog(
            "execute", "senior-python-developer", task_class="ask"
        )
    )
    assert ask == _WORKER_EXPECTED - {"create_task", "update_task"}
    assert "move_task" in ask
    for forbidden in {"create_task", "update_task"} | _MA_EXTRAS | {"archive_task"}:
        assert forbidden not in ask, f"ask executor must not expose {forbidden}"


def test_ask_executor_move_task_is_ask_voiced() -> None:
    # 07 review (tool-descriptions group): the base pool's move_task
    # description is reviewer/Board-Operator-voiced and forbids moving your
    # OWN task — the exact use the ask registration exists for. The ask
    # branch must serve the ask-voiced description instead, so the ask
    # assignee is TOLD it closes its own task straight to done (no review
    # round), and the base pool definition must stay unmutated.
    ask_tools = {
        t["name"]: t
        for t in get_worker_subcatalog(
            "execute", "senior-python-developer", task_class="ask"
        )
    }
    desc = " ".join(ask_tools["move_task"]["description"].split())
    assert "move YOUR OWN task straight to done" in desc
    assert "NO review round" in desc
    assert "do NOT `update_status` to review" in desc
    # The executor-forbidding prose must not be served to the ask assignee.
    assert "Do not use to submit your OWN task for review" not in desc
    # The re-voicing is a copy — the shared base pool keeps the
    # reviewer/Board-Operator voice for every other role.
    pool = {t["name"]: t for t in get_worker_tools()}
    assert (
        "Do not use to submit your OWN task for review"
        in pool["move_task"]["description"]
    )


def test_non_ask_task_class_keeps_plain_executor_surface() -> None:
    # Graceful degrade: absent task_class (older payloads) and every non-ask
    # class produce today's plain executor surface.
    plain = _WORKER_EXPECTED - _BOARD_WRITE
    for task_class in (None, "assignment", "program", "op"):
        got = _names(
            get_worker_subcatalog(
                "execute", "senior-python-developer", task_class=task_class
            )
        )
        assert got == plain, f"task_class={task_class!r} changed the surface"
    # task_class never widens the reviewer / MA surfaces.
    reviewer = _names(get_worker_subcatalog("review", "auditor", task_class="ask"))
    assert reviewer == _WORKER_EXPECTED - {"create_task", "update_task"}
    ma = _names(
        get_worker_subcatalog("execute", "manager-assistant", task_class="ask")
    )
    assert ma == _WORKER_EXPECTED | _MA_EXTRAS


def test_reviewer_subcatalog_keeps_only_move_task() -> None:
    # A reviewer (TASK_MODE=review) gains move_task as its verdict surface but
    # not create_task / update_task.
    reviewer = _names(get_worker_subcatalog("review", "auditor"))
    assert reviewer == _WORKER_EXPECTED - {"create_task", "update_task"}
    assert "move_task" in reviewer
    for forbidden in {"create_task", "update_task"} | _MA_EXTRAS | {"archive_task"}:
        assert forbidden not in reviewer, f"reviewer must not expose {forbidden}"


def test_manager_assistant_subcatalog_is_board_operator_set() -> None:
    # The MA keeps the full board-write set and gains the Board-Operator
    # reads/recovery. archive_task stays forbidden.
    for mode in ("execute", "review"):
        ma = _names(get_worker_subcatalog(mode, "manager-assistant"))
        assert ma == _WORKER_EXPECTED | _MA_EXTRAS, f"MA drift in mode={mode}"
        assert _BOARD_WRITE <= ma
        assert "archive_task" not in ma, "archive_task must stay forbidden for the MA"


def test_manager_assistant_triage_mode_drops_update_status() -> None:
    # TOOL-09: in triage, update_status is always refused at runtime (flipping
    # the current blocked task would bypass the bounce cap), so it is NOT
    # registered — the runtime guard stays only as defense-in-depth. Every other
    # Board-Operator tool remains.
    ma = _names(get_worker_subcatalog("triage", "manager-assistant"))
    assert "update_status" not in ma, "triage MA must not register update_status"
    assert ma == (_WORKER_EXPECTED - {"update_status"}) | _MA_EXTRAS
    assert "archive_task" not in ma


def test_planner_catalog_is_manager_minus_destructive_plus_plan_writes() -> None:
    planner = _names(get_planner_tools())
    # Never the destructive / manager-only verbs.
    assert planner.isdisjoint(_PLANNER_EXCLUDED), (
        f"Planner must not expose {planner & _PLANNER_EXCLUDED}"
    )
    # Has the plan-write tools.
    assert _PLANNER_ADDED <= planner
    # Has the core authoring surface it needs.
    assert {"create_task", "create_scope", "activate_scope", "update_task",
            "get_execution_plan", "complete_scope_verification"} <= planner
    # Is exactly the manager surface minus exclusions plus plan writes.
    assert planner == (_MANAGER_EXPECTED - _PLANNER_EXCLUDED) | _PLANNER_ADDED


def test_worker_never_has_manager_only_verbs() -> None:
    worker = _names(get_worker_tools())
    for forbidden in ("consult_planner", "decide_action_request",
                      "retry_blocked_task", "create_scope", "activate_scope",
                      "archive_scope", "delete_task", "archive_task",
                      # Pivot-4 flow-intake: workers surface workflow ideas
                      # via propose_action — never amend records or define
                      # flows themselves.
                      "amend_intake", "define_flow", "update_flow",
                      # Flow Studio (FS-P3.T3): flow design and collection
                      # WRITES belong to the Architect/Curator consult
                      # surfaces — workers hold only the collection READS
                      # (get_collection / query_rows).
                      "get_flow_graph", "update_flow_graph",
                      "write_template", "create_collection",
                      "update_collection_schema", "upsert_row",
                      "delete_row"):
        assert forbidden not in worker, f"worker must not expose {forbidden}"


# ── Flow Studio agent catalogs (FS-P3.T3) ───────────────────────────────
# The Flow Architect and Data Curator are consult-only agents selected by
# AGENT_NAME in mcp_tool_server.main (the Planner pattern). Both catalogs
# are pinned as EXACT sets — minimal and justified per tool.

_COLLECTION_WRITES = {
    "create_collection", "update_collection_schema", "upsert_row",
}
_COLLECTION_READS = {"list_collections", "get_collection", "query_rows"}
_KB_READS = {"search_kb", "get_kb_document"}

_FLOW_ARCHITECT_EXPECTED = (
    # The flow-authoring mandate.
    {"get_flow_graph", "update_flow_graph", "write_template"}
    # Extraction creates + populates the collections a flow reads
    # (spec §8.4) — but NOT delete_row: destructive row curation is the
    # Data Curator's consult surface.
    | _COLLECTION_WRITES | _COLLECTION_READS
    | _KB_READS
)

_DATA_CURATOR_EXPECTED = (
    _COLLECTION_WRITES | _COLLECTION_READS | {"delete_row"} | _KB_READS
)


def test_flow_architect_catalog_is_pinned() -> None:
    architect = _names(get_flow_architect_tools())
    assert architect == _FLOW_ARCHITECT_EXPECTED
    # Consult-only: no board, run, or file-registration surface — its
    # deliverables persist through the flow/collection tools.
    for forbidden in ("create_task", "move_task", "update_status",
                      "start_flow_run", "stop_flow_run", "save_file",
                      "delete_row", "define_flow", "update_flow"):
        assert forbidden not in architect, (
            f"flow-architect must not expose {forbidden}"
        )


def test_data_curator_catalog_is_pinned() -> None:
    curator = _names(get_data_curator_tools())
    assert curator == _DATA_CURATOR_EXPECTED
    # Consult-only, collections-only: no board tools, no flow-design
    # tools (get_flow_graph is gated flow-architect|manager backend-side
    # — impact statements ride the backend's teaching errors instead).
    for forbidden in ("create_task", "move_task", "update_status",
                      "get_flow_graph", "update_flow_graph",
                      "write_template", "save_file"):
        assert forbidden not in curator, (
            f"data-curator must not expose {forbidden}"
        )


def test_planner_excludes_collection_reads_v1() -> None:
    """Flow Studio v1 decision (FS-P3.T3): the spec is silent on whether
    the Planner reads collections — EXCLUDED for v1 (the Planner plans
    programs from specs/board/KB; collection data is execution-surface
    context workers read). Revisit only with a spec change — flipping
    this pin without one is drift, not a fix."""
    planner = _names(get_planner_tools())
    assert "query_rows" not in planner
    assert "get_collection" not in planner


def test_define_flow_description_pins_the_consent_first_sentence() -> None:
    """Program review #25: requirement 4's "definable on the fly WITH
    consent" has NO structural backend gate (unlike hire_agent's consent
    card) — the tool description + playbook rule ARE the enforcement, so
    the consent-first sentence is pinned here as a catalog invariant
    (beside the posture pins in tests/evals/test_flow_intake_pins.py).
    Removing or softening it silently removes the consent guarantee."""
    for tool in get_manager_tools():
        if tool["name"] == "define_flow":
            desc = " ".join(tool["description"].split())
            assert "USER CONSENT FIRST" in desc
            assert (
                "call this ONLY after the user agrees or explicitly asked"
                in desc
            )
            assert "NEVER define a flow silently" in desc
            return
    raise AssertionError("define_flow not found in the Manager catalog")


def _create_task_required(tools: list[dict]) -> set[str]:
    for t in tools:
        if t.get("name") == "create_task":
            return set(t["inputSchema"]["required"])
    raise AssertionError("create_task not found in tool list")


def test_create_task_requires_assignment_on_every_surface() -> None:
    """T4.1.1: every create_task surface (Manager, Planner, and the MA's
    Board-Operator worker pool) must make assigned_agent + reviewer REQUIRED
    so the model picks an owner deliberately — the backend MA default is the
    backstop, not the norm. Pins the prompt-fact against drift (R4)."""
    for tools in (get_manager_tools(), get_planner_tools(), get_worker_tools()):
        req = _create_task_required(tools)
        assert "assigned_agent" in req
        assert "reviewer" in req


def _create_task_props(tools: list[dict]) -> dict:
    for t in tools:
        if t.get("name") == "create_task":
            return t["inputSchema"]["properties"]
    raise AssertionError("create_task not found in tool list")


def test_create_task_scoping_params_parity_across_surfaces() -> None:
    """TOOL-08: the worker/MA create_task schema had drifted from the Manager's
    — it lacked ``scope_id`` and ``depends_on`` (so the Board Operator could not
    author scoped/ordered tasks) and its ``allowed_tools`` claimed an
    enforcement that does not exist. Pin that both scoping params are present on
    every writing surface and that allowed_tools carries the ADVISORY wording."""
    for tools in (get_manager_tools(), get_worker_tools()):
        props = _create_task_props(tools)
        assert "scope_id" in props, "create_task must expose scope_id"
        assert "depends_on" in props, "create_task must expose depends_on"
        # allowed_tools is advisory, not enforced — the wording must say so.
        assert "ADVISORY" in props["allowed_tools"]["description"]
