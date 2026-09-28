"""T5.4.3 — prompt-instructed transitions are legal (would have caught F9).

Templates instruct move_task / update_status targets in prose; nothing checked
them against the real backend VALID_TRANSITIONS — which is how "move to
backlog" (F9) survived. backlog is a SOURCE-only status (no transition targets
it), so it must NEVER appear as an instructed move target.

A bare "is this a legal target somewhere" check is vacuous (every status but
backlog is a target of something), so the checks here judge each instruction
from its SOURCE (X35):

* ``update_status`` is the executor's tool: from In Progress, and only to the
  targets its schema offers that the board allows from In Progress;
* a ``move_task`` instruction that names its source ("the in_progress task …
  to ready", "to ready on a blocked task") or uses ``source → target``
  notation is checked against ``VALID_TRANSITIONS[source]``;
* otherwise the surface's role supplies the source: the rendered reviewer
  prompt acts from Review, the rendered executor prompts from In Progress,
  mixed playbooks from either. Manager surfaces act from any state, so only
  named sources are judged there.

Each ``move_task`` instruction is judged as a full (source, target, actor)
triple: the surface's reader is the actor, and a ``MANAGER_ONLY_TRANSITIONS``
edge is legal only for the Manager actors — or, from Review, for the
designated reviewer (``board.validate_transition``). An executor prompt
therefore cannot instruct ``in_progress → archived`` even though the board
has that edge.

Tool mentions are recognised bare (``move_task``) and qualified
(``mcp__cubicle-tools__move_task``), with the target given by keyword, arrow,
parenthesis, ``new_status``, or positionally (``move_task(<id>, "ready")``).

The single class-conditional exception — an ask-class task moving
``in_progress → done`` — is accepted only when "ask" is stated nearby.
Negated prose ("there is no in_progress → ready") is not an instruction.
Mutation tests plant illegal instructions and require a failure.
"""
from __future__ import annotations

import re

import pytest

from src.config_sync.claude_md_content import (
    ANALYST_CLAUDE_MD,
    AUDITOR_CLAUDE_MD,
    AUTOMATION_SCRIPT_DEV_CLAUDE_MD,
    MANAGER_ASSISTANT_CLAUDE_MD,
    MANAGER_CLAUDE_MD,
    SHARED_AGENT_WORK_RULES,
    SHARED_OFFICE_CLAUDE_MD,
)
from src.config_sync.claude_md_templates._system_agents import PLANNER_CLAUDE_MD
from src.config_sync.claude_md_templates._manager_modules import (
    MANAGER_FLOW_PROCEDURES,
    MANAGER_FLOW_PROCEDURES_GENERAL_CHAT,
    MANAGER_PROGRAM_PROCEDURES,
    render_general_chat_procedures,
)
from src.config_sync.claude_md_templates._custom_agent import (
    generate_custom_agent_claude_md,
)
from src.config_sync._auto_decide_rows import (
    AUTO_DECIDE_ROWS,
    render_auto_decide_guidance,
)
from src.config_sync._tool_allowlist import render_manager_allowlist
from src.orchestrator.worker_prompt import build_worker_prompt
from src._agent_image._mcp.tools_manager import get_manager_tools
from src._agent_image._mcp.tools_worker import get_worker_tools
from src._agent_image._mcp.tools_planner import get_planner_tools
from tests.backend_boundary import import_backend

_BOARD = import_backend("app.tasks.board")
VALID_TRANSITIONS = _BOARD.VALID_TRANSITIONS
MANAGER_ONLY_TRANSITIONS = _BOARD.MANAGER_ONLY_TRANSITIONS
_LEGAL_TARGETS = {t for tos in VALID_TRANSITIONS.values() for t in tos}


def _tool_descriptions(tools: list[dict]) -> str:
    """Concatenated tool `description` prose — the highest-leverage prompt
    surface (per communicator/CLAUDE.md), so its move/status targets must be
    legal too."""
    return "\n\n".join(t.get("description", "") for t in tools)


def _worker_prompt(status: str, rework_count: int = 0) -> str:
    """Render a worker task prompt for a given dispatch status so the eval
    scans the move/status targets it actually instructs (T5.4.3 done-when:
    'worker_prompt.py blocks' + 'all templates scanned')."""
    return build_worker_prompt({
        "task_id": "00000000-0000-0000-0000-000000000001",
        "readable_id": "RC-001.T05",
        "title": "x", "status": status, "rework_count": rework_count,
        "recent_activities": [], "artifacts": [], "reviewer": "auditor",
        "assigned_agent": "dev",
        "brief": {
            "goal": "g", "context": "c", "inputs": "i",
            "output_format": "short", "acceptance_criteria": ["a"],
            "allowed_tools": [], "required_skills": [],
            "risks_and_edge_cases": "none", "verification_steps": "v",
        },
    })


_SURFACES = {
    "manager": MANAGER_CLAUDE_MD.replace(
        "{manager_tool_allowlist}", render_manager_allowlist()
    ).replace("{office_name}", "X"),
    # F07: the Manager procedure modules the dynamic context injects.
    "manager_program_procedures": MANAGER_PROGRAM_PROCEDURES,
    "manager_flow_procedures": MANAGER_FLOW_PROCEDURES,
    "manager_flow_procedures_general_chat": MANAGER_FLOW_PROCEDURES_GENERAL_CHAT,
    "manager_general_chat_procedures": render_general_chat_procedures(),
    "office": SHARED_OFFICE_CLAUDE_MD.replace("{office_name}", "X"),
    "manager_assistant": MANAGER_ASSISTANT_CLAUDE_MD,
    "analyst": ANALYST_CLAUDE_MD,
    "auditor": AUDITOR_CLAUDE_MD,
    "automation_script_developer": AUTOMATION_SCRIPT_DEV_CLAUDE_MD,
    "shared_agent": SHARED_AGENT_WORK_RULES,
    "planner": PLANNER_CLAUDE_MD,
    "worker_execute": _worker_prompt("ready"),
    "worker_review": _worker_prompt("review"),
    "worker_rework": _worker_prompt("in_progress", rework_count=1),
    # The custom-agent CLAUDE.md appends the shared delivery/completion
    # sections (which instruct update_status targets).
    "custom_agent": generate_custom_agent_claude_md(
        {"name": "x", "display_name": "X", "system_prompt": "Do work."}
    ),
    # The auto-decide guidance rows instruct move_task targets per type.
    "auto_decide": "\n\n".join(
        render_auto_decide_guidance(rt) for rt in AUTO_DECIDE_ROWS
    ),
    # Tool DESCRIPTIONS are prompt content the model reads at call time —
    # the update_status / move_task descriptions instruct status targets.
    "manager_tool_descriptions": _tool_descriptions(get_manager_tools()),
    "worker_tool_descriptions": _tool_descriptions(get_worker_tools()),
    "planner_tool_descriptions": _tool_descriptions(get_planner_tools()),
}

_STATUS = r"(backlog|ready|in_progress|blocked|review|done|archived)"
# Bare or MCP-qualified (``mcp__cubicle-tools__move_task``): the qualified
# spelling has no word boundary before the tool name (``_`` is a word char).
_TOOL_MENTION = re.compile(
    r"(?:(?<=mcp__cubicle-tools__)|\b)(move_task|update_status)\b"
)
# A status in TARGET position right after the tool mention: new_status = X,
# to X, → X, (X), (<task id>, X), with status X — optionally quoted or
# set-bracketed.
_TARGET = re.compile(
    r"(?:new_status\s*(?:=|:|\bin\b)?\s*\{*\s*|\bto\s+|→\s*|->\s*"
    r"|\(\s*(?:[^(),\n]{1,60},\s*)?|with status\s*)"
    r"[`\"']*" + _STATUS + r"\b"
)
_ARROW = re.compile(_STATUS + r"`?\s*(?:→|->)\s*`?" + _STATUS + r"\b")
_FOLLOWED_BY_ARROW = re.compile(r"^`?\s*(?:→|->)")
_SOURCE_THEN_TARGET = re.compile(
    r"\b(?:the|an?|this|that|your|its)\s+`?" + _STATUS + r"`?\s+task\b[^\n]{0,60}?"
    r"\bto\s+[`\"']*" + _STATUS + r"\b"
)
_TARGET_THEN_SOURCE = re.compile(
    r"\bto\s+[`\"']*" + _STATUS + r"[`\"']*\s+(?:on|for)\s+(?:a|an|the|this)\s+`?"
    + _STATUS + r"`?\s+task\b"
)
_NEGATION = re.compile(
    r"\b(?:no|not|never|cannot|can't|refused|removed|forbidden|bounced)\b", re.IGNORECASE,
)
_SEGMENT = 120
# The executor's submit targets: what update_status offers AND the board
# allows from In Progress (both read from code).
_UPDATE_STATUS_ENUM = next(
    tool for tool in get_worker_tools() if tool["name"] == "update_status"
)["inputSchema"]["properties"]["new_status"]["enum"]
SUBMIT_TARGETS = set(_UPDATE_STATUS_ENUM) & VALID_TRANSITIONS["in_progress"]
_EXECUTE = frozenset({"in_progress"})
_MIXED = frozenset({"in_progress", "review"})
# Default source(s) per surface when an instruction does not name one.
# ``None`` = the surface may act from any state (the Manager); only named
# sources are judged there.
SURFACE_SOURCES: dict[str, frozenset[str] | None] = {
    "manager": None,
    "auto_decide": None,
    "manager_tool_descriptions": None,
    "planner": None,
    "planner_tool_descriptions": None,
    "office": _MIXED,
    "manager_assistant": _MIXED,
    "analyst": _MIXED,
    "auditor": _MIXED,
    "automation_script_developer": _MIXED,
    "shared_agent": _MIXED,
    "custom_agent": _MIXED,
    "worker_tool_descriptions": _MIXED,
    "worker_execute": _EXECUTE,
    "worker_rework": _EXECUTE,
    "worker_review": frozenset({"review"}),
}


# Who reads each surface and so performs its ``move_task`` instructions:
# "manager" = the Manager actors (Manager, Manager Assistant) — may use
# every board edge; "reviewer" = the designated reviewer — may additionally
# take manager-only edges out of Review; "executor" = the assigned worker —
# no manager-only edge; "worker" = a playbook read in both executor and
# reviewer roles (the reviewer allowance applies); "any" = read by the
# Manager too (office CLAUDE.md) or describing the Manager's moves (the
# Planner has no move_task), so the actor is not judged there.
SURFACE_ACTORS: dict[str, str] = {
    "manager": "manager",
    "auto_decide": "manager",
    "manager_tool_descriptions": "manager",
    "manager_assistant": "manager",
    "planner": "any",
    "planner_tool_descriptions": "any",
    "office": "any",
    "analyst": "worker",
    "auditor": "worker",
    "automation_script_developer": "worker",
    "shared_agent": "worker",
    "custom_agent": "worker",
    "worker_tool_descriptions": "worker",
    "worker_execute": "executor",
    "worker_rework": "executor",
    "worker_review": "reviewer",
}


def _actor_may(actor: str, source: str, target: str) -> bool:
    """Mirror of the ``MANAGER_ONLY_TRANSITIONS`` actor gate in
    ``board.validate_transition`` (the reviewer arm applies from Review)."""
    if actor in ("manager", "any") or (source, target) not in MANAGER_ONLY_TRANSITIONS:
        return True
    return actor in ("reviewer", "worker") and source == "review"


def _window(text: str, start: int, end: int) -> str:
    return text[max(0, start - 150): end + 150]


def _legal(
    sources: frozenset[str] | set[str], target: str, window: str, actor: str = "any",
) -> bool:
    if any(
        target in VALID_TRANSITIONS.get(source, set())
        and _actor_may(actor, source, target)
        for source in sources
    ):
        return True
    # The one class-conditional edge: an ask-class task's own completion.
    return "in_progress" in sources and target == "done" and bool(
        re.search(r"\bask\b|ask-class", window, re.IGNORECASE)
    )


# A negation governs the instruction only inside its own clause: a sentence
# end, a "; ", ": " or ", " before the verb starts a new clause, so "If the
# reviewer does not answer, call move_task to done" is still an instruction.
_CLAUSE_BREAK = re.compile(r"[.;:!?](?:\s|$)|,\s|\n")


def _negated(text: str, start: int) -> bool:
    window_start = max(0, start - 80)
    prefix = text[window_start:start]
    breaks = list(_CLAUSE_BREAK.finditer(prefix))
    clause = prefix[breaks[-1].end():] if breaks else prefix
    return bool(_NEGATION.search(clause))


def transition_violations(surfaces: dict[str, str]) -> list[str]:
    """Every instructed transition that is illegal from its source."""
    found: list[str] = []
    for name, text in surfaces.items():
        default_sources = SURFACE_SOURCES.get(name, _MIXED)
        actor = SURFACE_ACTORS.get(name, "worker")
        for mention in _TOOL_MENTION.finditer(text):
            tool = mention.group(1)
            segment = text[mention.end(): mention.end() + _SEGMENT].split("\n")[0]
            captured = _TARGET.search(segment)
            if captured is None or _FOLLOWED_BY_ARROW.match(segment[captured.end():]):
                continue  # no target, or ``tool(source → target)`` pair notation
            target = captured.group(1)
            start = mention.start()
            window = _window(text, start, mention.end() + captured.end())
            if _negated(text, start + 1):
                continue
            if tool == "update_status":
                if target not in SUBMIT_TARGETS:
                    found.append(f"{name}: update_status → {target!r} (allowed {sorted(SUBMIT_TARGETS)})")
                continue
            explicit = _SOURCE_THEN_TARGET.search(segment)
            if explicit:
                sources, target = {explicit.group(1)}, explicit.group(2)
            else:
                reversed_pair = _TARGET_THEN_SOURCE.search(segment)
                if reversed_pair:
                    sources, target = {reversed_pair.group(2)}, reversed_pair.group(1)
                elif default_sources is None:
                    continue
                else:
                    sources = default_sources
            if not _legal(sources, target, window, actor):
                found.append(
                    f"{name}: move_task {sorted(sources)} → {target!r} as {actor}"
                )
        for pair in _ARROW.finditer(text):
            source, target = pair.group(1), pair.group(2)
            if _negated(text, pair.start()):
                continue
            if not _legal({source}, target, _window(text, pair.start(), pair.end())):
                found.append(f"{name}: {source} → {target}")
    return found


def test_backlog_is_never_an_instructed_move_target():
    # backlog has no inbound transition; instructing a move to it is the F9 bug.
    assert "backlog" not in _LEGAL_TARGETS  # sanity: board really forbids it
    offenders = {}
    for name, text in _SURFACES.items():
        for m in re.finditer(
            r"(?:move_task|update_status)[^\n]{0,60}?backlog", text
        ):
            # Allow explicit "no transition into backlog" disclaimers.
            window = text[max(0, m.start() - 40): m.end() + 10].lower()
            if "no transition into" in window or "not move" in window:
                continue
            offenders.setdefault(name, []).append(m.group(0))
    assert not offenders, f"prompt instructs a move to backlog (F9): {offenders}"


def test_instructed_transitions_are_legal_from_their_source():
    assert SUBMIT_TARGETS == {"review", "blocked"}  # sanity: read from code
    violations = transition_violations(_SURFACES)
    assert not violations, f"prompts instruct illegal transitions: {violations}"


@pytest.mark.parametrize("surface,planted", [
    ("manager_assistant",
     "If it stalls, call `move_task` on the in_progress task and send it back to `ready`."),
    ("shared_agent", "When you finish, call `update_status(done)` and stop."),
    ("office", "The board allows `in_progress → ready` when a redo is needed."),
    ("worker_review", 'Or call `move_task` with new_status = "review" to hold it.'),
    ("worker_execute", "When the work is complete, call `move_task` to `done`."),
    # Qualified MCP spelling — the form most templates use.
    ("manager_assistant",
     "If it stalls, call `mcp__cubicle-tools__move_task` on the in_progress "
     "task and send it back to `ready`."),
    ("worker_execute",
     "When done, call `mcp__cubicle-tools__move_task` to `ready`."),
    ("shared_agent",
     "Submit with `mcp__cubicle-tools__update_status` with new_status `done`."),
    # Actor: legal board edges an executor may not take (manager-only).
    ("worker_execute", "If the brief is wrong, call `move_task` to `archived`."),
    ("worker_rework",
     "Call `mcp__cubicle-tools__move_task` with new_status = \"archived\"."),
    ("shared_agent",
     "Unblock it yourself: call `move_task` on the blocked task to `ready`."),
    # Positional target: move_task(<id>, "<status>").
    ("worker_execute", 'Then call `move_task(task_id, "ready")` to requeue it.'),
    ("worker_review",
     'Hold it with `mcp__cubicle-tools__move_task(task_id, "review")`.'),
    # T10: a negation that does not govern the verb must not hide it.
    ("worker_execute",
     "If the reviewer does not answer, call `move_task` to `done`."),
    ("shared_agent", "No reviewer is needed here; call `update_status(done)`."),
    ("worker_execute", "Never wait. Call `move_task` to `ready`."),
], ids=["in_progress_to_ready", "update_status_done", "arrow_in_progress_ready",
        "review_to_review", "executor_move_done", "qualified_ma_in_progress_ready",
        "qualified_executor_ready", "qualified_update_status_done",
        "actor_executor_archive", "actor_rework_archive_qualified",
        "actor_worker_blocked_to_ready", "positional_executor_ready",
        "positional_reviewer_review", "negation_in_subordinate_clause",
        "negation_before_semicolon", "negation_in_previous_sentence"])
def test_planted_illegal_instructions_are_caught(surface, planted):
    surfaces = {**_SURFACES, surface: _SURFACES[surface] + "\n\n" + planted + "\n"}
    violations = transition_violations(surfaces)
    assert any(violation.startswith(f"{surface}:") for violation in violations), violations


@pytest.mark.parametrize("surface,planted", [
    ("office", "There is no `in_progress → ready` transition."),
    ("worker_execute", "For an ask-class task, call `move_task` to `done` with the answer."),
    ("worker_review", 'Return it: call `move_task` with new_status = "ready".'),
    # The designated reviewer may take manager-only edges out of Review.
    ("worker_review",
     'Return it: call `mcp__cubicle-tools__move_task(task_id, "ready")`.'),
    ("worker_review", "Obsolete work: call `move_task` to `archived`."),
    # The Manager actors may take every board edge.
    ("manager_assistant",
     "Archive it: call `mcp__cubicle-tools__move_task` on the blocked task to `archived`."),
    ("worker_execute",
     "Submit with `mcp__cubicle-tools__update_status(task_id, \"review\")`."),
])
def test_legal_or_negated_instructions_are_accepted(surface, planted):
    surfaces = {surface: _SURFACES[surface] + "\n\n" + planted + "\n"}
    assert transition_violations(surfaces) == []


@pytest.mark.parametrize("planted", [
    "Never call `move_task` to `ready`.",
    "Do NOT call `mcp__cubicle-tools__move_task` to `ready` on a blocked task.",
])
def test_negation_at_the_start_of_a_surface_is_honoured(planted):
    """T10: the old window started at -1 when no sentence break preceded the
    mention, reading an empty slice — a leading negation was missed."""
    assert transition_violations({"worker_execute": planted}) == []


def test_new_status_enums_offer_only_legal_targets():
    """AIQ fix 12 (2026-07-29): schema enums are prompt content too — the
    Manager move_task enum offered "backlog" even though NO transition
    targets backlog (source-only status), inviting a guaranteed-reject
    round-trip. Every ``new_status`` enum value in every catalog must be a
    legal transition target."""
    catalogs = {
        "manager": get_manager_tools(),
        "worker": get_worker_tools(),
        "planner": get_planner_tools(),
    }
    offenders: dict[str, dict[str, list[str]]] = {}
    seen_enums = 0
    for cat_name, tools in catalogs.items():
        for tool in tools:
            props = (tool.get("inputSchema") or {}).get("properties") or {}
            enum = (props.get("new_status") or {}).get("enum")
            if not enum:
                continue
            seen_enums += 1
            illegal = [v for v in enum if v not in _LEGAL_TARGETS]
            if illegal:
                offenders.setdefault(cat_name, {})[tool["name"]] = illegal
    assert seen_enums >= 3, "expected new_status enums in every catalog"
    assert not offenders, (
        f"new_status enum offers a non-target status (no inbound edge): "
        f"{offenders}; legal targets: {sorted(_LEGAL_TARGETS)}"
    )
