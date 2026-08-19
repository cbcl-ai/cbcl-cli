"""T5.4.1 — prompt-references-reality (would have caught F2/F4/F5/F9/F12).

No test asserted that the tool tokens mentioned in the CLAUDE.md templates +
the worker task prompt actually exist in the live catalog — exactly how the
phantom-tool drift (F2/F4/F12) and the stale exhaustive lists (F5) shipped.

Phantom-tool guard. Two shapes are caught in every rendered prompt surface:
  1. any `mcp__cubicle-tools__X` whose X is not a real tool; and
  2. any bare-backtick `verb_noun` whose leading segment is a REAL tool-verb
     (create/move/update/get/…) but whose full name is not a real tool — the
     historical drift shape (e.g. the F9 `move`-to-backlog phantom).
Shape 2 is verb-anchored so it is self-maintaining (verbs derive from the live
catalog) and never false-positives on field names (`task_id`, `output_format`)
or statuses (`in_progress`). A phantom with an invented verb that matches no
real tool-verb is out of scope (it would also not mislead the model toward a
plausible real tool); explicit non-tool terms live on the short exemption list.
"""
from __future__ import annotations

import re

from src._agent_image._mcp.tools_manager import get_manager_tools
from src._agent_image._mcp.tools_planner import get_planner_tools
from src._agent_image._mcp.tools_data_curator import get_data_curator_tools
from src._agent_image._mcp.tools_flow_architect import get_flow_architect_tools
from src._agent_image._mcp.tools_worker import (
    get_worker_subcatalog,
    get_worker_tools,
)
from src.config_sync.claude_md_content import (
    ANALYST_CLAUDE_MD,
    AUDITOR_CLAUDE_MD,
    AUTOMATION_SCRIPT_DEV_CLAUDE_MD,
    MANAGER_ASSISTANT_CLAUDE_MD,
    MANAGER_CLAUDE_MD,
    SHARED_AGENT_WORK_RULES,
    SHARED_OFFICE_CLAUDE_MD,
)
from src.config_sync.claude_md_templates import (
    generate_custom_agent_claude_md,
    generate_workstream_claude_md,
)
from src.config_sync.claude_md_templates._system_agents import (
    BUILDER_CLAUDE_MD,
    DATA_CURATOR_CLAUDE_MD,
    FLOW_ARCHITECT_CLAUDE_MD,
    PLANNER_CLAUDE_MD,
)
from src.config_sync._tool_allowlist import render_manager_allowlist


# Representative renders of the two dict-driven templates so the phantom-tool
# scan covers the SAME artifacts a real agent auto-loads (EVAL-04). Fields are
# the ones the templates actually read; content is neutral so the scan sees only
# the template's own tool references, not the fixture's.
_WORKSTREAM_RENDER = generate_workstream_claude_md(
    {
        "name": "Recruitment",
        "short_code": "RC",
        "description": "Hire engineers.",
        "goals": "Ship the team.",
        "context_notes": "",
    }
)
_CUSTOM_AGENT_RENDER = generate_custom_agent_claude_md(
    {
        "name": "python-developer",
        "display_name": "Senior Python Developer",
        "system_prompt": "You are a senior Python developer.",
        "allowed_tools": ["Read", "Write", "Bash", "Glob", "Grep"],
        "skills": [],
    }
)


def _all_tool_names() -> set[str]:
    names: set[str] = set()
    for fn in (
        get_manager_tools,
        get_worker_tools,
        get_planner_tools,
        # 07/AI-01: the two Flow Studio consult catalogs are real tool
        # surfaces too — omitting them made every tool unique to them look
        # like a phantom, which is exactly the pressure that keeps new
        # playbooks OUT of this guard.
        get_flow_architect_tools,
        get_data_curator_tools,
    ):
        names |= {t["name"] for t in fn()}
    names |= {t["name"] for t in get_worker_subcatalog("triage", "manager-assistant")}
    return names


# Tokens that look like tool names but are NOT cubicle MCP tools — Claude
# built-ins, CLI-builtin disallows, or generic words. Excluded from the check.
_NON_TOOL_BACKTICKS = {
    "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "Bash", "WebSearch",
    "WebFetch", "Task", "TaskCreate", "CronCreate", "Skill", "AskUserQuestion",
}
# Tools named ONLY to say "you do NOT have / never call X" — legitimately
# referenced while absent from that role's catalog.
_NEGATIVE_MENTIONS = {"archive_task", "delete_task", "move_task", "create_task",
                      "execute_script", "register_script", "schedule_script",
                      "update_script_cron", "delete_script_cron",
                      "list_script_crons"}

_TOKEN_RE = re.compile(r"`(?:mcp__cubicle-tools__)?([a-z][a-z0-9_]+)`")


def _tool_tokens(text: str, known: set[str]) -> set[str]:
    """Backtick tokens that match a known tool name (avoids false positives on
    arbitrary code spans)."""
    found = {m for m in _TOKEN_RE.findall(text) if m in known}
    return found - _NON_TOOL_BACKTICKS


# Bare backtick `snake_case` token (no mcp prefix), e.g. `move_to_backlog`.
_BARE_RE = re.compile(r"`([a-z][a-z0-9_]+)`")

# Bare-backtick verb_noun tokens that LOOK tool-shaped (their first segment is
# a real tool-verb) but are NOT MCP tools — backend internals / spec terms
# referenced in prose. Kept tiny + explicit; a phantom tool must NOT be parked
# here just to silence the guard.
_NON_TOOL_VERB_TOKENS = {
    "create_task_with_brief",  # backend service fn, named in Manager prose
    "request_type",    # a field name on an action request, not a tool.
    # CTX-08: ``propose_action`` was removed from this exemption. It is the
    # backend umbrella ACTION, NOT a callable tool, and two auto-loaded
    # playbooks referenced it in backticks as if it were one. With the
    # exemption gone, the guard now FAILS if `propose_action` reappears in a
    # model-facing playbook — cite a real typed tool (propose_subtask,
    # escalate_blocker, …) instead.
}


def _tool_verbs(known: set[str]) -> set[str]:
    """The leading `verb_` segment of every real tool — create/move/get/…"""
    return {n.split("_", 1)[0] for n in known if "_" in n}


def _scan_phantom_tokens(
    rendered: str, known: set[str], verbs: set[str], allowed: set[str],
) -> set[str]:
    """Return tool-shaped tokens in ``rendered`` that are NOT real tools.

    Two shapes: an `mcp__cubicle-tools__X` whose X isn't real, and a
    bare-backtick `verb_noun` whose leading segment is a REAL tool-verb but
    whose full name isn't a real tool / negative mention / known non-tool term
    (e.g. `move_to_backlog` — verb `move` is real). A phantom with an invented
    verb that matches no real tool-verb (e.g. `reassign_task`) is out of scope
    by design — see the module docstring.
    """
    found: set[str] = set()
    mcp_re = re.compile(r"mcp__cubicle-tools__([a-z][a-z0-9_]+)")
    for x in set(mcp_re.findall(rendered)):
        if x not in known and x not in _NEGATIVE_MENTIONS:
            found.add(x)
    for tok in set(_BARE_RE.findall(rendered)):
        if tok in allowed or "_" not in tok:
            continue
        if tok.split("_", 1)[0] in verbs:
            found.add(tok)
    return found


def _render(template: str) -> str:
    # Render the format placeholders the templates use.
    out = template
    if "{office_name}" in out:
        out = out.replace("{office_name}", "Test Office")
    if "{manager_tool_allowlist}" in out:
        out = out.replace("{manager_tool_allowlist}", render_manager_allowlist())
    if "{office_specs_index}" in out:
        out = out.replace("{office_specs_index}", "")
    return out


_SURFACES = {
    "manager": MANAGER_CLAUDE_MD,
    "office": SHARED_OFFICE_CLAUDE_MD,
    "shared_agent": SHARED_AGENT_WORK_RULES,
    "analyst": ANALYST_CLAUDE_MD,
    "auditor": AUDITOR_CLAUDE_MD,
    "asd": AUTOMATION_SCRIPT_DEV_CLAUDE_MD,
    "manager_assistant": MANAGER_ASSISTANT_CLAUDE_MD,
    # EVAL-04: the Planner playbook + the two dict-driven renders (workstream
    # CLAUDE.md, custom-agent CLAUDE.md) are auto-loaded prompt surfaces too;
    # the sibling transitions eval already scans planner + custom renders, so
    # the phantom-tool scan must not lag behind.
    "planner": PLANNER_CLAUDE_MD,
    # 07/AI-01: the three playbooks that shipped AFTER this guard was
    # written — Builder (pivot-1 T1), Flow Architect + Data Curator (Flow
    # Studio FS-P3). Each is an auto-loaded prompt surface; each was
    # outside every phantom-tool scan until now, which is precisely the
    # drift shape this eval exists to prevent.
    "builder": BUILDER_CLAUDE_MD,
    "flow_architect": FLOW_ARCHITECT_CLAUDE_MD,
    "data_curator": DATA_CURATOR_CLAUDE_MD,
    "workstream": _WORKSTREAM_RENDER,
    "custom_agent": _CUSTOM_AGENT_RENDER,
}


def test_no_phantom_tool_tokens_in_any_template():
    known = _all_tool_names()
    verbs = _tool_verbs(known)
    # Tokens legitimately allowed to appear in a tool-shaped backtick span.
    allowed = (
        known
        | _NEGATIVE_MENTIONS
        | {n.lower() for n in _NON_TOOL_BACKTICKS}
        | _NON_TOOL_VERB_TOKENS
    )
    offenders: dict[str, set[str]] = {}
    for name, template in _SURFACES.items():
        found = _scan_phantom_tokens(_render(template), known, verbs, allowed)
        if found:
            offenders[name] = found
    assert not offenders, f"phantom tool references: {offenders}"


def test_phantom_guard_catches_bare_backtick_phantom():
    # Mutation test: the guard must flag an invented `verb_noun` tool even in
    # the bare-backtick form (the shape the F-class drift actually took). If
    # this ever passes vacuously the guard has gone toothless again.
    known = _all_tool_names()
    verbs = _tool_verbs(known)
    allowed = (
        known | _NEGATIVE_MENTIONS
        | {n.lower() for n in _NON_TOOL_BACKTICKS} | _NON_TOOL_VERB_TOKENS
    )
    # Real-verb phantoms (the historical drift shape — invented tools whose
    # leading segment is a genuine tool-verb, e.g. the F9 `move`-to-backlog).
    injected = "If the task is stuck, call `move_to_backlog` or `update_to_done`."
    found = _scan_phantom_tokens(injected, known, verbs, allowed)
    assert "move_to_backlog" in found
    assert "update_to_done" in found
    # And a real tool + field names are NOT flagged.
    clean = "Use `move_task` and read `output_format` / `request_type`."
    assert _scan_phantom_tokens(clean, known, verbs, allowed) == set()


def test_manager_allowlist_is_reverse_complete():
    # The exhaustive generated allowlist must list every manager tool (the
    # bidirectional pin also lives in test_claude_md_writer; duplicated here as
    # the prompt-references-reality family's reverse-inclusion case).
    rendered = render_manager_allowlist()
    listed = set(re.findall(r"`([a-z_]+)`", rendered))
    catalog = {t["name"] for t in get_manager_tools()}
    assert catalog <= listed, f"allowlist missing: {catalog - listed}"


def test_mutation_a_fake_tool_token_is_caught():
    # Guard's own sanity: a fabricated mcp tool token must be flagged.
    known = _all_tool_names()
    fake = "mcp__cubicle-tools__totally_fake_tool"
    mcp_re = re.compile(r"mcp__cubicle-tools__([a-z][a-z0-9_]+)")
    hits = [x for x in mcp_re.findall(fake) if x not in known]
    assert hits == ["totally_fake_tool"]


# ── 07/AI-01: per-role catalog membership ──────────────────────────────
#
# The phantom guard above asks "is this a real tool ANYWHERE?" — a union
# check. It cannot see the sharper failure: a playbook that instructs its
# agent to call a tool that is real, but is not in THAT agent's catalog.
# The model complies, the MCP server has no such tool registered, and the
# turn burns a round trip on tool-not-found — at the exact moment the
# session is already slow, since the instruction usually fires on a long
# wait or an escalation.
#
# That is not hypothetical: the shared long-running-Bash rule told the
# three CONSULT-ONLY agents (Planner 29 tools, Flow Architect 11, Data
# Curator 9) to post an `add_activity` checkpoint and poll
# `get_script_status`, neither of which any of them holds — and to hand
# the work to the Automation Script Developer, which needs a `propose_*`
# tool none of them holds either.

# Tools a playbook may legitimately NAME while not holding: mentions that
# exist to say "this is Manager-only" / "you do NOT have this" / to
# describe what another role does. Curated per role, so a genuine new
# violation cannot hide behind a blanket allowlist.
_ALLOWED_FOREIGN_MENTIONS = {
    # "a scheduled ASSIGNMENT instead — schedule_assignment, Manager-owned"
    "analyst": {"move_task", "schedule_assignment", "update_task"},
    "auditor": {"update_task"},
    "asd": {"move_task", "schedule_assignment", "update_task"},
    # "The decide_action_request tool is Manager-only"; archive_task likewise.
    "manager_assistant": {"archive_task", "decide_action_request"},
    "builder": {"move_task", "update_task"},
    # Describing what a WORKER's task does / what a worker filed.
    "planner": {"execute_script", "propose_spec_update", "schedule_assignment"},
    "flow_architect": set(),
    "data_curator": set(),
}


def _role_catalogs() -> dict[str, set[str]]:
    def sub(mode: str, agent: str) -> set[str]:
        return {t["name"] for t in get_worker_subcatalog(mode, agent)}

    return {
        "analyst": sub("execute", "analyst"),
        "auditor": sub("review", "auditor"),
        "asd": sub("execute", "automation-script-developer"),
        # The MA is served three different sub-catalogs depending on how the
        # task reaches it; a mention is fair if ANY of them carries it.
        "manager_assistant": (
            sub("triage", "manager-assistant")
            | sub("execute", "manager-assistant")
            | sub("review", "manager-assistant")
        ),
        "builder": sub("execute", "builder"),
        "planner": {t["name"] for t in get_planner_tools()},
        "flow_architect": {t["name"] for t in get_flow_architect_tools()},
        "data_curator": {t["name"] for t in get_data_curator_tools()},
    }


def test_no_playbook_instructs_a_tool_its_role_does_not_hold():
    known = _all_tool_names()
    catalogs = _role_catalogs()
    offenders: dict[str, list[str]] = {}
    for role, own in catalogs.items():
        rendered = _render(_SURFACES[role])
        mentioned = {t for t in _TOKEN_RE.findall(rendered) if t in known}
        stray = sorted(mentioned - own - _ALLOWED_FOREIGN_MENTIONS[role])
        if stray:
            offenders[role] = stray
    assert not offenders, (
        "playbooks name tools their role's catalog does not serve "
        f"(add to _ALLOWED_FOREIGN_MENTIONS only if the mention is "
        f"explicitly negative): {offenders}"
    )


def test_the_membership_guard_catches_a_planted_foreign_instruction():
    """Mutation: the guard must fail when a consult playbook is handed an
    executor-only tool. Without this the test above can rot into a
    tautology as catalogs grow."""
    known = _all_tool_names()
    catalogs = _role_catalogs()
    planted = _render(_SURFACES["data_curator"]) + "\nThen call `move_task`.\n"
    mentioned = {t for t in _TOKEN_RE.findall(planted) if t in known}
    stray = mentioned - catalogs["data_curator"] - _ALLOWED_FOREIGN_MENTIONS["data_curator"]
    assert "move_task" in stray


def test_the_consult_roles_are_not_told_to_checkpoint_or_poll_scripts():
    """The specific 07/AI-01 regression, pinned by name.

    Asserted on the RENDERED playbook rather than on the constant, because
    the bug was in which variant each role was handed — a check against the
    constant would have passed throughout.
    """
    for role in ("flow_architect", "data_curator"):
        rendered = _render(_SURFACES[role])
        assert "add_activity" not in rendered, (
            f"{role} holds no activity tool in a consult session"
        )
        assert "get_script_status" not in rendered, (
            f"{role} cannot poll script status"
        )
    # The Planner DOES hold add_activity (verify mode operates on tasks),
    # but not the script-status tool.
    assert "get_script_status" not in _render(_SURFACES["planner"])
    # ...and the executor rule must keep both — the split must not have
    # quietly downgraded the roles that legitimately use them.
    assert "add_activity" in _render(_SURFACES["analyst"])
    assert "get_script_status" in _render(_SURFACES["analyst"])
