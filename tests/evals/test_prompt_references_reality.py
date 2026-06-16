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
from src.config_sync._tool_allowlist import render_manager_allowlist


def _all_tool_names() -> set[str]:
    names: set[str] = set()
    for fn in (get_manager_tools, get_worker_tools, get_planner_tools):
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
    "propose_action",  # umbrella backend ACTION for the typed propose_* family
                       # (always shown as "typed `propose_action` (e.g.
                       # `propose_subtask`…)") — a concept, not a callable tool.
    "request_type",    # a field name on an action request, not a tool.
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
