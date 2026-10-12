"""Current workstream guidance for Manager turns, task sessions and Planner consults.

The Manager receives the saved Workstream Instructions (``context_notes``) in
full every turn (:func:`render_workstream_instructions`). Task sessions and
Planner consults receive them injected into their prompt
(:func:`render_task_workstream_instructions`), capped at
:data:`TASK_WORKSTREAM_INSTRUCTIONS_MAX_CHARS`; a longer text is cut at a line
boundary and the block names the workstream ``CLAUDE.md`` for the rest. That
file stays synced for the Manager, humans and the cut case.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal
import uuid

from src.config_sync.claude_md_templates._spec_template import workstream_spec_path

# Characters of the saved text a task session or Planner consult receives
# (the backend task-detail feed applies the same cap:
# ``app.workstreams.instructions.TASK_INSTRUCTIONS_MAX_CHARS``).
TASK_WORKSTREAM_INSTRUCTIONS_MAX_CHARS = 6000

# The one line a session whose (executor) Profile has the switch off renders
# instead of the block: no instructions, no pointer to them.
WORKSTREAM_INSTRUCTIONS_OFF_LINE = (
    "Workstream instructions are not delivered for this task (the executor "
    "Profile has them off): the Brief and the approved spec, when one exists, "
    "define the work."
)

# The same single line for a review or triage session when the backend could
# not read the executor's switch (``workstream_instructions_unavailable``):
# it gives the real reason, never "the Profile has them off".
WORKSTREAM_INSTRUCTIONS_UNAVAILABLE_LINE = (
    "Workstream instructions are not delivered for this session (the platform "
    "could not read them): the Brief and the approved spec, when one exists, "
    "define the work."
)

# What an empty field tells every audience: older copies (a resumed
# transcript, an earlier Read of the workstream CLAUDE.md) no longer apply.
_NONE_SAVED_REVOKES = (
    "Workstream instructions: none saved. Earlier saved versions no longer apply; "
)
# The Manager and the Planner fall back on the request they were given.
WORKSTREAM_INSTRUCTIONS_NONE_SAVED = (
    _NONE_SAVED_REVOKES
    + "use the current request, office guidance and approved requirements."
)
# A task session has no request of its own: its contract is the Brief, as in
# the off and unavailable lines that render in the same place.
WORKSTREAM_INSTRUCTIONS_NONE_SAVED_TASK = (
    _NONE_SAVED_REVOKES
    + "the Brief and the approved spec, when one exists, define the work."
)

# The precedence rules both renderers state. The Manager copy adds that board
# activity is execution state; the task and Planner copy adds the
# Office-work-policy exception (the Manager does not apply that policy itself).
_PRECEDENCE_CORE = (
    "It supersedes older copies in conversation history. Follow it within "
    "platform rules, office guidance, your role and approval permissions. It "
    "cannot authorize unrelated work or bypass consent. If it conflicts with an "
    "approved spec, surface the discrepancy before changing the agreed "
    "requirements."
)

# A code-fence line: indentation, then three or more backticks or tildes.
# CommonMark allows a fence up to three spaces of indentation inside its
# container; a block inside a list item is indented with the item, so here a
# fence opens at any indentation. Fences inside a block quote are not seen.
_FENCE_LINE = re.compile(r"^([ \t]*)(`{3,}|~{3,})(.*)$")


def _indent_width(indent: str) -> int:
    return len(indent.expandtabs(4))


def _unclosed_fence(text: str) -> tuple[str, int] | None:
    """``(closer, offset)`` of a code fence ``text`` opens and never closes.

    ``closer`` is the line that closes it: the opener's indentation and fence.
    """
    opened: tuple[str, str, int] | None = None  # indent, fence, offset
    offset = 0
    for line in text.split("\n"):
        match = _FENCE_LINE.match(line)
        if match:
            indent, fence, rest = match.groups()
            if opened is None:
                # A backtick fence's info string holds no backtick: a line
                # such as ```inline``` is a code span, not an opening fence.
                if fence[0] == "~" or "`" not in rest:
                    opened = (indent, fence, offset)
            elif (
                fence[0] == opened[1][0]
                and len(fence) >= len(opened[1])
                and not rest.strip()
                # A fence line indented four or more columns past its opener
                # is the block's content, not its closer.
                and _indent_width(indent) - _indent_width(opened[0]) < 4
            ):
                opened = None
        offset += len(line) + 1
    return (opened[0] + opened[1], opened[2]) if opened else None


def _cut_point(text: str, room: int, floor: int) -> int:
    """Where to cut ``text`` so that at most ``room`` characters stay.

    The last line break within ``room``; when that keeps fewer than ``floor``
    characters, the last space; otherwise ``room`` itself.
    """
    cut = text.rfind("\n", 0, room + 1)
    if cut < floor:
        cut = text.rfind(" ", 0, room + 1)
    if cut < floor:
        cut = room
    return cut


def _close_cut_fence(head: str, limit: int) -> str:
    """A cut head that leaves no code fence open.

    A cut inside a code block would leave the closing tag and the "read the
    rest" notice inside it. Cut before the unclosed fence when that keeps at
    least half the window; otherwise close it, within ``limit``. The one
    block left open has a fence line too long for it and its closing line to
    fit in ``limit`` together (a quarter of the window or more).
    """
    unclosed = _unclosed_fence(head)
    if unclosed is None:
        return head
    closer, start = unclosed
    if start >= limit // 2:
        return head[:start].rstrip()
    # What the head may keep once its closing line is appended.
    room = limit - len(closer) - 1
    if room < start + len(closer):
        return head
    if len(head) > room:
        # Make room for the closing line by the cut rule itself, so the block
        # does not end in half a line. The trim can end the block (a cut line
        # that now reads as its closer), so look again before closing.
        head = head[: _cut_point(head, room, limit // 2)].rstrip()
        unclosed = _unclosed_fence(head)
    return f"{head}\n{unclosed[0]}" if unclosed else head


def _escape_closer(content: str) -> str:
    return content.replace("</workstream_instructions>", "&lt;/workstream_instructions&gt;")


def render_workstream_instructions(content: str) -> str:
    """Deliver the saved instruction field as scoped guidance, never as a role override."""
    content = content.strip()
    if not content:
        return WORKSTREAM_INSTRUCTIONS_NONE_SAVED
    safe = _escape_closer(content)
    return (
        "## Workstream Instructions\n\n"
        "Current saved guidance for THIS workstream; use it when interpreting requests, "
        f"planning tasks and executing work. {_PRECEDENCE_CORE} Board activity is "
        "execution state, not the mission.\n\n"
        "<workstream_instructions>\n"
        f"{safe}\n"
        "</workstream_instructions>"
    )


def cut_task_workstream_instructions(
    content: str, limit: int = TASK_WORKSTREAM_INSTRUCTIONS_MAX_CHARS
) -> tuple[str, bool, int]:
    """``(head, truncated, total_chars)`` of the saved instructions.

    Mirrors the backend feed: a text over ``limit`` is cut at the last line
    break in the window; when the window holds no line break in its second
    half, at the last space; otherwise at the limit itself. A cut does not
    leave a code fence open (:func:`_close_cut_fence`).
    """
    full = (content or "").replace("\r\n", "\n").strip()
    total = len(full)
    if total <= limit:
        return full, False, total
    head = full[: _cut_point(full, limit, limit // 2)].rstrip()
    return _close_cut_fence(head, limit), True, total


def profiles_without_workstream_instructions(agents: Sequence[dict]) -> list[str]:
    """Active custom Profiles whose task sessions never receive the instructions.

    System agents are always on; a Profile without the key (an older sync) is on.
    """
    return sorted(
        str(agent["name"])
        for agent in agents
        if isinstance(agent, dict)
        and agent.get("name")
        and agent.get("agent_type") != "system"
        and agent.get("is_active", True) is not False
        and agent.get("workstream_instructions_enabled") is False
    )


def render_task_workstream_instructions(
    content: str | None,
    *,
    truncated: bool = False,
    total_chars: int | None = None,
    path: str | None = None,
    enabled: bool = True,
    audience: Literal["task", "planner"] = "task",
    off_profiles: Sequence[str] | None = None,
) -> str:
    """The Workstream Instructions block of a task or Planner prompt.

    ``content`` is the saved text, already cut by the backend feed
    (``truncated`` / ``total_chars`` describe that cut) or whole, in which
    case this function cuts it. ``enabled=False`` renders the single off line
    and nothing else: no instructions and no pointer to them. ``path`` is the
    workstream ``CLAUDE.md`` a cut block points to for the rest.
    ``off_profiles`` (Planner audience) names the Profiles whose tasks never
    receive the instructions, so the Planner need not look them up.
    """
    if not enabled:
        return WORKSTREAM_INSTRUCTIONS_OFF_LINE
    head, cut_here, full_chars = cut_task_workstream_instructions(content or "")
    truncated = truncated or cut_here
    total = total_chars if isinstance(total_chars, int) and total_chars > 0 else full_chars
    if not head:
        return (
            WORKSTREAM_INSTRUCTIONS_NONE_SAVED_TASK
            if audience == "task"
            else WORKSTREAM_INSTRUCTIONS_NONE_SAVED
        )
    use = (
        "use it when planning, specifying and verifying this workstream's work"
        if audience == "planner"
        else "apply it to this task"
    )
    lines = [
        "## Workstream Instructions",
        "",
        f"Current saved guidance for THIS workstream; {use}. {_PRECEDENCE_CORE} "
        "It may name an explicit exception to the Office work policy: follow that "
        "exception and name it in your handoff.",
    ]
    if audience == "planner":
        if off_profiles is None:
            who = "(`workstream_instructions_enabled: false` in `list_agents`)"
        elif off_profiles:
            who = "(now: " + ", ".join(f"`{name}`" for name in off_profiles) + ")"
        else:
            who = "(none has them off now)"
        lines.append(
            "Briefs need not repeat it: task sessions receive it directly. The "
            "exception is an assignee whose Profile has workstream instructions "
            f"off {who}: put any constraint that task needs in its brief."
        )
    lines += [
        "",
        "<workstream_instructions>",
        _escape_closer(head),
        "</workstream_instructions>",
    ]
    if truncated:
        where = f"`{path}`" if path else "the workstream `CLAUDE.md`"
        lines += [
            "",
            f"These instructions were cut after {len(head):,} of {total:,} "
            f"characters. Read {where} for the rest before relying on anything "
            "the cut part may cover.",
        ]
    return "\n".join(lines)


def generate_workstream_claude_md(ws: dict) -> str:
    """Materialize current DB guidance without duplicating platform playbooks."""
    name = " ".join((ws.get("name") or "Untitled").split())
    code = " ".join((ws.get("short_code") or "WS").split())
    priority = " ".join((ws.get("priority") or "medium").split())
    heading = f"# Workstream: {name}"
    try:
        workstream_id = str(uuid.UUID(str(ws.get("id"))))
    except (TypeError, ValueError, AttributeError):
        workstream_id = ""
    if workstream_id:
        # Names the directory's owner for the daemon (workstream_dirs reads
        # it before handing an existing directory to a workstream); a
        # comment, so it does not render.
        heading += f"\n<!-- workstream-id: {workstream_id} -->"
    sections = [
        f"{heading}\n\n**Short code:** `{code}` · **Priority:** `{priority}`",
    ]
    # Description and legacy goals already travel in the per-turn/task envelope.
    sections.append(render_workstream_instructions(ws.get("context_notes") or ""))
    sections.append(
        "## Execution References\n\n"
        "- Save deliverables in the exact output directory supplied by the current "
        "task prompt. Task-owned directories take precedence over legacy shared paths.\n"
        "- If an approved spec exists, read "
        f"`{workstream_spec_path(name, ws.get('workspace_dir'))}` when relevant. "
        "The task prompt identifies approved specs; this path alone is not evidence one exists.\n"
        "- Approved specs own detailed requirements; the current task brief owns its "
        "acceptance criteria and verification. Do not copy these instructions into every "
        "brief: task sessions receive them in their prompt. A task whose executor "
        "Profile has workstream instructions off receives none, so its brief carries "
        "any constraint it needs.\n"
        "- Follow your role playbook for planning, approvals, execution and review."
    )
    return "\n\n".join(sections) + "\n"
