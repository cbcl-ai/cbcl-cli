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
}
# update_spec is Planner-only (authors the spec + milestones); get_spec is
# shared (also in the Manager catalog, so the | with _MANAGER_EXPECTED already
# covers it). update_execution_plan is ALSO in the Manager base (the
# stuck-verify chip-flip surface) — it stays listed because
# PLANNER_PLAN_TOOLS carries it and the union is idempotent.
# Pivot-1 T6: update_workstream_plan retired with the roadmap artifact —
# update_spec's ``milestones`` param is the checklist write now.
# Planner count: 36 manager − 8 excluded + 1 net-new (update_spec) = 29
# (pivot-2 P1 added ask_user_choice to both the Manager set and the
# exclusion set, so the Planner surface is unchanged).
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
                      "archive_scope", "delete_task", "archive_task"):
        assert forbidden not in worker, f"worker must not expose {forbidden}"


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
