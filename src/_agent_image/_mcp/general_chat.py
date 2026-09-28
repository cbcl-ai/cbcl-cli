"""General Chat tool strip — the pure set and filter (no side effects).

The in-container MCP server (``mcp_tool_server.py``) applies this strip to a
``general_chat`` Manager session, and the host-side Manager procedure module
(``config_sync/claude_md_templates/_manager_modules.py``) renders the list of
unavailable tools from the same filter. Keeping both in a pure module lets the
daemon import it without loading the in-container entry script, which puts its
own directory on ``sys.path`` and reads container environment variables.
``mcp_tool_server`` re-exports both names for its runtime guard and tests.
"""

from __future__ import annotations

# Actions / bare tool names that mutate the board, scopes, or
# workspace. Blocked in General Chat mode. The guard at
# ``_execute_tool`` checks BOTH ``tool["action"]`` and the bare tool
# name against this set, so the set legitimately mixes "actions" and
# "names" that are not 1-to-1 (e.g. the ``archive_task`` tool dispatches
# to action ``move_task`` with a transform).
#
# Several entries are belt-and-suspenders for actions that ONLY exist
# on the worker side today. They are kept here so a future change that
# accidentally exposes one to the Manager (e.g. by promoting a worker
# tool into the manager_tools list) still gets blocked in General Chat
# rather than silently letting the Manager mutate while in
# "general_chat" context. If you add a worker-only mutation, ALSO add
# its action / bare name here — that's cheaper than a runtime audit.
BOARD_WRITE_ACTIONS: frozenset[str] = frozenset({
    # G1: reading a decision envelope persists a context-bound receipt; its
    # backend requires the request's current workstream, absent in General Chat.
    "get_action_request",
    # Manager tool actions (from ``_get_manager_tools``).
    "create_task",
    "update_task",
    "move_task",
    "add_activity",
    "delete_task",
    "stop_task",
    "create_scope",
    "update_scope",
    "activate_scope",
    "archive_scope",
    # TS-M1: engaging the Planner is a workstream-planning write — strip it in
    # General Chat (which has no workstream context) so the Manager can't
    # consult the Planner against an arbitrary workstream from general chat.
    "consult_planner",
    # Closing a scope's verification is a scope state change — strip it in
    # General Chat (no scope context there), same as the other scope writes.
    # Plan READS (get_execution_plan / get_spec) stay available;
    # they're harmless and the Manager has no scope to read in General Chat
    # anyway.
    "complete_scope_verification",
    # The Manager's chip-flip surface for the escalated stuck-verify recovery
    # (verify turn-end incident 2026-07-17) is a scope-plan WRITE — stripped in
    # General Chat like the other scope writes the moment it joined
    # MANAGER_PLAN_TOOLS (the approve_spec lesson below: a tool shipped in the
    # Manager base but missing here escapes the strip).
    "update_execution_plan",
    # TOOL-01/MGR-05: approving a workstream spec flips draft→approved and
    # unblocks the entire downstream automation chain (milestones → scopes →
    # tasks). It is a workstream-state WRITE — same class as consult_planner —
    # and must be stripped in General Chat, which has the LEAST workstream
    # context. (spec READS: get_spec stays available.) It shipped in
    # MANAGER_PLAN_TOOLS but was never added here, so it escaped the strip.
    "approve_spec",
    "office_save_file",
    # Pivot-2 P1: asking the user a choice question is a workstream-
    # conversation write (the choice row pins to ONE workstream context,
    # and General Chat has nothing to decide — no tasks, no programs).
    # Stripped in General Chat like consult_planner; the backend handler
    # refuses general_chat contexts as defense-in-depth.
    "ask_user_choice",
    # Pivot-3 P2-2: assignment-schedule WRITES are workstream-scoped (a
    # schedule pins to ONE workstream and mints op tasks there) — stripped in
    # General Chat like the other workstream writes. The read
    # (list_assignment_schedules) stays available.
    "schedule_assignment",
    "update_assignment_schedule",
    "delete_assignment_schedule",
    # Pivot-4 flow-intake: amending an intake record is a workstream-record
    # write (records live in a workstream General Chat doesn't have), and
    # flow definitions shape how every workstream's work routes — all three
    # are writes, stripped like the other planning writes. General Chat
    # keeps only reads.
    "amend_intake",
    "define_flow",
    "update_flow",
    # Flow Studio (FS-P2.T9): starting/stopping a flow RUN is a
    # workstream-scoped write (a run rides ONE workstream's chat and
    # board) — both stripped in General Chat like the other workstream
    # writes. The read (get_flow_run) stays available.
    "start_flow_run",
    "stop_flow_run",
    # Office-memory v1 (T3.1): writing a memory record is a
    # workstream-conversation write (the default scope is the current
    # workstream, and even ``office_wide=true`` is deliberately invoked
    # FROM a workstream context — the General-Chat carve-out is NOT
    # built v1, per the roadmap). Stripped like the other workstream
    # writes; the read (memory_recall) stays available (General Chat
    # recall serves the office-level slice, derived backend-side).
    "memory_remember",
    # Bare tool names — Manager tools whose ``action`` aliases a less
    # specific verb (the bare-name check still trips the guard).
    "archive_task",  # tool name; action is move_task + transform
    # Escape hatch for the blocked-bounce-cap deadlock. Manager / MA
    # only; the General-Chat guard still blocks it (Manager in chat
    # has no business unblocking a stuck task without context).
    "retry_blocked_task",
    # Action-request decisions are workstream state changes — blocked
    # in General Chat so the Manager doesn't accidentally approve a
    # request without the workstream-context that informs the call.
    "decide_action_request",
    # Worker-only actions/names (defense-in-depth — see header above).
    "office_attach_to_task",
    "register_script",
    "clone_script",
    "install_script_from_template",
    "task_status_update",
    # F21 (audit): ``kb_save`` removed — no such tool is registered on
    # either Manager or Worker. Was a dead defense-in-depth entry.
})


def filter_general_chat_tools(tools: list[dict]) -> list[dict]:
    """Exclude board/planning writes and workstream-bound decision receipts.

    The registration-time General-Chat strip ``mcp_tool_server.main()``
    applies to a ``general_chat`` Manager session. Extracted as a pure
    function (the ``filter_script_author_tools`` idiom) so tests exercise
    the REAL filter instead of mirroring its expression, and so the
    host-side General Chat procedure module renders from it.
    """
    return [t for t in tools if t.get("action") not in BOARD_WRITE_ACTIONS]
