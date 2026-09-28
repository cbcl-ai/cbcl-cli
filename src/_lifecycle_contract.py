"""Single maintained source of lifecycle and tool-authority facts (F01).

Every prompt surface that tells an agent how a task session ENDS — the
Manager and Planner authoring rules, the office primer, the shared worker
rules, the phase-specific task prompt — used to carry its own hand-written
copy of these facts, and the copies drifted apart: the Manager was told that
``execute_script`` makes a task "TERMINAL at the trigger" (so author a second
"consume the result" task), while the runtime parks the SAME task and
resumes it in its phase with the recorded run; the office primer said
workers "do NOT call" ``move_task`` while reviewers, ask-class executors and
the Manager Assistant all hold it.

This module fixes the facts in ONE place:

* **Derived authority.** Which role/phase session holds which lifecycle tool
  is computed from the live MCP catalogs (``get_worker_subcatalog``,
  ``get_manager_tools``, ``get_planner_tools``) — never hand-listed — so a
  rendered holder line cannot drift from the served surface.
* **Canonical facts.** Short, brace-free sentences (they are spliced into
  templates that later pass through ``str.format``) pinned to the runtime by
  ``tests/test_lifecycle_contract.py`` (``execution_completion``
  dispositions, the executor's default Review submission, the MCP lock set).

Consumers (each pinned by ``tests/test_lifecycle_contract.py``):
``_manager.py`` (async-work rule), ``_planner.py`` (split rule),
``_office.py`` (``move_task``/``create_task``/``update_task`` holder lines,
session-end rule), ``_shared_agent.py`` (the script handoff core in the
one-shot rule, the execute blocker fact in Communication),
``orchestrator/worker_prompt.py`` (the execute resume detail).
``orchestrator/_execution_preflight.py`` imports nothing from here: its
managed-script resume branch (BRANCH S) is the step-by-step procedure, not a
restatement of a fact, and ``tests/evals/test_fx_prompts_pins.py`` pins that
it stays consistent with ``SCRIPT_HANDOFF_RESUME_FACT``.

Each worker session receives each fact ONCE (F07): the session-end fact
from the office file, the handoff core from the role file, the resume
detail from the execute task prompt — pinned per role and phase by
``tests/evals/test_prompt_composition.py``.

NOT consumers: the in-container tool descriptions and runtime messages
(``_agent_image/**``) cannot import this host-side module and carry their own
hand-maintained wording. Catalog imports stay inside functions so importing
this module never drags the tool catalogs in early.
"""
from __future__ import annotations

import textwrap
from dataclasses import dataclass


@dataclass(frozen=True)
class SessionShape:
    """One role/phase worker session whose tool surface is derived live."""

    key: str
    task_mode: str
    agent: str
    task_class: str | None
    # Tools the phase's own resolution instruction names (each must be
    # registered for the session — pinned by the contract tests).
    ends_with: tuple[str, ...]


SESSIONS: tuple[SessionShape, ...] = (
    SessionShape("executor", "execute", "builder", None, ("update_status",)),
    SessionShape("ask_executor", "execute", "builder", "ask", ("move_task",)),
    SessionShape("reviewer", "review", "auditor", None, ("move_task",)),
    SessionShape(
        "ma_execute", "execute", "manager-assistant", None, ("update_status",)
    ),
    SessionShape("ma_review", "review", "manager-assistant", None, ("move_task",)),
    SessionShape(
        "ma_triage",
        "triage",
        "manager-assistant",
        None,
        ("add_activity", "create_task", "update_task", "escalate_blocker"),
    ),
)

def session_tools(session: SessionShape) -> frozenset[str]:
    """Tool names actually registered for ``session`` (live catalog)."""
    from src._agent_image._mcp.tools_worker import get_worker_subcatalog

    return frozenset(
        tool["name"]
        for tool in get_worker_subcatalog(
            session.task_mode, session.agent, task_class=session.task_class
        )
    )


def role_tools() -> dict[str, frozenset[str]]:
    """Every worker session shape plus the Manager and Planner catalogs."""
    from src._agent_image._mcp.tools_manager import get_manager_tools
    from src._agent_image._mcp.tools_planner import get_planner_tools

    tools = {session.key: session_tools(session) for session in SESSIONS}
    tools["manager"] = frozenset(t["name"] for t in get_manager_tools())
    tools["planner"] = frozenset(t["name"] for t in get_planner_tools())
    return tools


def holders(tool: str) -> tuple[str, ...]:
    """Session/role keys whose live catalog registers ``tool``."""
    return tuple(key for key, names in role_tools().items() if tool in names)


# Human labels for the holder keys, in rendering order. Several keys can
# share one label (the Manager Assistant is served three sub-catalogs).
_HOLDER_LABELS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("manager",), "Manager"),
    (("ma_execute", "ma_review", "ma_triage"), "Manager Assistant"),
    (("planner",), "Planner"),
    (("reviewer",), "designated reviewers"),
    (("ask_executor",), "ask-class executors"),
    (("executor",), "ordinary executors"),
)


def holder_labels(
    tool: str,
    notes: dict[str, str] | None = None,
    note_format: str = "{label} ({note})",
) -> list[str]:
    """Labels of every role holding ``tool``; ``notes`` qualifies a label."""
    held = set(holders(tool))
    labels = []
    for keys, label in _HOLDER_LABELS:
        if held.intersection(keys):
            note = (notes or {}).get(label)
            labels.append(note_format.format(label=label, note=note) if note else label)
    return labels


def _join(labels: list[str]) -> str:
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def _wrap(line: str) -> str:
    return textwrap.fill(
        line, width=78, subsequent_indent="  ", break_on_hyphens=False
    )


# How each holder uses ``move_task`` — the hand-written half of the office
# line; WHO holds it is derived from the catalogs.
_MOVE_TASK_NOTES = {
    "Manager": "overrides",
    "designated reviewers": "verdicts",
    "ask-class executors": "own task → done",
}


def render_move_task_line() -> str:
    """The office primer's ``move_task`` line, rendered from the catalogs."""
    line = (
        "- `move_task` — change a board column: "
        + _join(holder_labels("move_task", _MOVE_TASK_NOTES))
        + "."
    )
    if "executor" not in holders("move_task"):
        line += " Ordinary executors use `update_status` instead."
    return _wrap(line)


def render_create_task_line() -> str:
    """The office primer's ``create_task`` line, holders from the catalogs."""
    return _wrap(
        "- `create_task` — ("
        + ", ".join(holder_labels("create_task"))
        + ") create a task with a complete Brief (goal / verbatim inputs / "
        "acceptance criteria / verification steps; optional framing fields "
        "only when they add signal). `assigned_agent` and `reviewer` are "
        "REQUIRED."
    )


# The backend's Planner field gate (``action_executors.action_update_task``;
# ``spec_revision``/``execution_resources`` ride the brief edit) — the office
# line must not claim priority/assignment edits for the Planner.
PLANNER_UPDATE_TASK_FIELDS: tuple[str, ...] = (
    "brief",
    "title",
    "description",
    "depends_on",
)


def render_update_task_line() -> str:
    """The office primer's ``update_task`` line, holders from the catalogs."""
    note = "/".join(PLANNER_UPDATE_TASK_FIELDS) + " of never-run drafts only"
    return _wrap(
        "- `update_task` — ("
        + ", ".join(
            holder_labels(
                "update_task", {"Planner": note}, note_format="{label}: {note}"
            )
        )
        + ") modify title/description/priority/labels/assigned_agent/"
        "reviewer/depends_on or the nested `brief`."
    )


# ── Canonical facts (brace-free; pinned to runtime by the contract tests) ──

# _agent_worker_task.py: a clean executor session end sends task_complete
# with status "review" (ipc-protocol.md "Task execution complete.").
SESSION_END_FACT = (
    "Ending your session is not completing the task: an execute session "
    "that ends without a terminal call or an accepted handoff is submitted "
    "to Review as-is."
)

# execution_completion.completion_disposition: a script started by THIS
# attempt parks the task ("script_handoff") in in_progress / review /
# blocked; an accepted capacity wait is a "capacity_handoff". The
# dispatcher re-dispatches the SAME task in its phase with the results.
SCRIPT_HANDOFF_CORE = (
    "An accepted `execute_script` receipt is a durable handoff in the phase "
    "that launched it (durable platform receipts own the wait): stop; the "
    "platform resumes the SAME task in that phase with the recorded run."
)
# The execute task prompt's resume detail. Every worker role file already
# carries SCRIPT_HANDOFF_CORE (the one-shot-session rule), so the task prompt
# renders only this remainder (F07 — each fact reaches a session once).
SCRIPT_HANDOFF_RESUME_FACT = (
    "An `accepted_wait` capacity receipt counts too (the wait is listed "
    "instead). On resume inspect the listed run before any new side effect "
    "and never relaunch a completed run. An error or capacity refusal is not "
    "an accepted run or handoff."
)
SCRIPT_CORE = (
    "After an accepted `execute_script` receipt the worker stops; the "
    "platform parks that task and resumes the SAME task in its phase with "
    "the recorded run to inspect and verify."
)

PUSH_IS_NOT_HANDOFF_FACT = (
    "A `git push`, CI trigger or other Bash command is not a platform "
    "handoff: the session continues and nothing re-invokes it."
)

SPLIT_RULE = (
    "Split tasks only for a separate deliverable, a real dependency, a "
    "different owner or independent review — never because a tool uses a "
    "subprocess or a result arrives later."
)

# propose_action never changes task status; move_service files the Inbox
# escalation for a worker's move to blocked.
EXECUTE_BLOCKER_FACT = (
    "In execute mode a blocker is ONE `update_status(blocked)` call with the "
    "ESCALATED comment; it files the Inbox escalation. `escalate_blocker` or "
    "a proposal alone does not block the task — ending the session then "
    "submits it to Review."
)

MANAGER_ASYNC_WORK_RULE = (
    "**Async results stay in the SAME task.** "
    + SCRIPT_CORE
    + " So ONE task owns run → verify → submit; put the run's checks in its "
    "acceptance criteria. "
    + PUSH_IS_NOT_HANDOFF_FACT
    + " The worker checks a short result in-session, or hands a long "
    "pipeline's ID to the reviewer or configured automation that owns that "
    "check. "
    + SPLIT_RULE
    + " Repeated identical failures after a run: inspect that run and the "
    "brief before retrying."
)

PLANNER_ASYNC_WORK_RULE = (
    "- **Managed scripts park; they do not split.** "
    + SCRIPT_CORE
    + " Keep launch + verify in ONE task. "
    + PUSH_IS_NOT_HANDOFF_FACT
    + " "
    + SPLIT_RULE
)

ALL_FACTS: tuple[str, ...] = (
    SESSION_END_FACT,
    SCRIPT_HANDOFF_CORE,
    SCRIPT_HANDOFF_RESUME_FACT,
    SCRIPT_CORE,
    PUSH_IS_NOT_HANDOFF_FACT,
    SPLIT_RULE,
    EXECUTE_BLOCKER_FACT,
    MANAGER_ASYNC_WORK_RULE,
    PLANNER_ASYNC_WORK_RULE,
)
