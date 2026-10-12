"""Workstream spec template + the `/workspace/specs/` convention (Phase 10 S-A).

The **spec** is the durable WHAT/WHY requirements contract for a body of
work — drafted by the Planner, approved by the user, and the thing every
downstream artifact (roadmap, scope plans, task briefs, verification)
references. It is deliberately small and modular: requirements as stable,
append-only `REQ-n` ids, not designs (the plan owns HOW).

## Convention paths

- **Office shared specs** — `/workspace/specs/office/<stem>.md`. Domain
  truths, integration contracts, and flows reusable across workstreams.
  Indexed (name + one-liner + path) in the office CLAUDE.md, never inlined.
  The path comes from the backend (`sync_config.specs[].path`,
  `app/core/utils.py:office_spec_file_slug`); the daemon never derives it.
- **Workstream spec** — `/workspace/workstreams/<dir>/spec.md`, alongside
  the workstream CLAUDE.md (the file a cut task-prompt instruction block names).
  `<dir>` is the backend-declared `workspace_dir`
  (`src.paths.declared_workstream_dir`) — identical to the workstream
  CLAUDE.md directory so the two live side by side.

## Authority order (stated once, here and in the office CLAUDE.md)

For BEHAVIOR: platform rules > office CLAUDE.md > specs > brief.
For TASK-LOCAL acceptance detail: brief > spec (the brief is this task's
contract; the spec is the world it lives in).

## Size discipline

Target **≤1–2k tokens** per workstream spec. Requirements are one sentence
+ an acceptance note each; full client documents stay in the workspace/KB as
inputs, NOT pasted into the spec. The Planner distils, it does not transcribe.

In S-A (prompt-only) the Planner writes this skeleton's filled form to the
convention path via the `Write` tool. In S-B the spec becomes a first-class
DB entity materialised to the same path via the `fs_write` pipeline.
"""
from __future__ import annotations

from src.paths import declared_workstream_dir


def workstream_spec_path(
    workstream_name: str, workspace_dir: str | None = None
) -> str:
    """Convention path for a workstream's spec.md.

    Mirrors the workstream CLAUDE.md location
    (``/workspace/workstreams/<dir>/CLAUDE.md``) so the spec sits beside the
    workstream instruction file. Pass the
    backend-declared ``workspace_dir``; without it (an older backend) the
    legacy ``slugify(name)`` directory that backend writes to applies.
    """
    return (
        "/workspace/workstreams/"
        f"{declared_workstream_dir(workspace_dir, workstream_name)}/spec.md"
    )


# ---------------------------------------------------------------------------
# The seven-section skeleton the Planner fills (specify mode).
# ---------------------------------------------------------------------------
#
# Sections (stable order — downstream tooling and the Planner playbook
# reference these headings by name):
#   1. Goal & Why            2. Requirements (REQ-n)   3. User Flows (FLOW-n)
#   4. Non-goals             5. Constraints            6. Open Questions
#   7. Status                (REQ → delivered/in-flight/deferred)

_SPEC_SKELETON_BODY = """\
> **Spec** — the WHAT/WHY contract for this workstream. Requirements, not
> designs (the plan owns HOW). Keep it ≤1–2k tokens; link or reference
> source documents rather than pasting them. REQ/FLOW ids are **append-only**
> — never renumber an existing id; referenced from briefs, activity, and
> verification.
>
> Authority: platform rules > office CLAUDE.md > **this spec** > task brief
> for behavior; brief > spec for task-local acceptance detail.

## Goal & Why

<2–4 sentences: what this body of work achieves and why it matters. The
durable answer to "what are we building, and to what end?">

## Requirements

Each requirement is one sentence + an acceptance note. Append-only ids.

- **REQ-1** <one-sentence requirement> — _acceptance:_ <how a reviewer
  objectively confirms it is met>
- **REQ-2** <…> — _acceptance:_ <…>

## User Flows

(Where relevant — the concrete paths a user takes. Omit if not applicable.)

- **FLOW-1** <step → step → outcome>

## Non-goals

(What is explicitly OUT of scope — the section that ends scope-creep
arguments later.)

- <non-goal>

## Constraints

(Tech, budget, tone, compliance, timeline — the boundaries the work runs in.)

- <constraint>

## Open Questions

(Drafted by the Planner; resolved by the user at the approval gate. Empty
this list before the spec is approved, or convert each into a REQ / Non-goal /
Constraint.)

- <question for the user>

## Status

Coverage of every requirement — kept current as scopes complete.

| REQ | Status | Notes |
|-----|--------|-------|
| REQ-1 | planned | <delivered / in-flight / deferred / planned> |
| REQ-2 | planned | |
"""


def render_spec_template(workstream_name: str, *, revision: int = 1) -> str:
    """Render the empty spec skeleton for a workstream.

    The Planner replaces the ``<…>`` placeholders with real content; the
    section structure and the REQ/FLOW id convention are fixed.
    """
    title = workstream_name.strip() or "Untitled Workstream"
    header = (
        f"# Spec: {title}\n\n"
        f"**Revision:** {revision}  ·  **Status:** `draft`\n\n"
    )
    return header + _SPEC_SKELETON_BODY


# Exposed for the Planner playbook reference + the token-budget test.
WORKSTREAM_SPEC_TEMPLATE = render_spec_template("<workstream name>")

# The seven canonical section headings, in order. Imported by the eval that
# pins the template structure and by any renderer that needs to locate
# sections.
SPEC_SECTION_HEADINGS = (
    "Goal & Why",
    "Requirements",
    "User Flows",
    "Non-goals",
    "Constraints",
    "Open Questions",
    "Status",
)
