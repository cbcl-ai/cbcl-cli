"""Workstream CLAUDE.md generator (split from claude_md_content.py).

The whole file is regenerated on every sync from the synced ``ws`` dict —
including the Context Notes section, whose content is the workstream's
``context_notes`` field (edited in Workstream Settings, delivered via
sync_config). There is NO on-disk marker-split preservation: the DB is the
source of truth for context_notes and the writer overwrites the file wholesale.
"""
from __future__ import annotations

from src.config_sync.claude_md_templates._spec_template import (
    workstream_spec_path,
)


def generate_workstream_claude_md(ws: dict) -> str:
    """Generate CLAUDE.md for a workstream.

    The whole file (auto-sections + Context Notes) is regenerated from ``ws``.
    Context Notes come from ``ws["context_notes"]`` (the DB, edited in Workstream
    Settings) — the writer does NOT read the old file to preserve edits.
    """
    short_code = ws.get("short_code") or "?"
    name = ws.get("name", "Untitled")
    description = ws.get("description") or "No description provided."
    goals = ws.get("goals") or "No goals defined yet."
    priority = ws.get("priority", "medium")
    spec_path = workstream_spec_path(name)

    auto_section = f"""# Workstream: {name}

**Short code:** `{short_code}`  ·  **Priority:** `{priority}`

## About This Workstream

A workstream is an **isolated project context**. Every task in this
workstream shares the description, goals, and notes below — agents
working tasks here should read and respect these conventions.

Tasks in this workstream get readable IDs of the form
`{short_code}-NNN.TXX` (e.g. `{short_code}-003.T14`). Output files
land under `/workspace/outputs/{short_code}/[<scope-readable-id>/]`
so cross-workstream artefacts never collide.

## Description

{description}

## Goals

{goals}

## How to know a task in this workstream is "done"

Every acceptance criterion for tasks here should be **objectively
checkable by a reviewer**. Generic criteria like "report is
thorough" fail this bar — the reviewer can't PASS/FAIL them without
re-doing the work. Good criteria are verification-shaped: "npm test
exits 0; /dashboard renders the new widget" or, for research,
"Findings doc at outputs/X.md answers questions A/B/C". Do not
require a report file unless the report itself is the deliverable.

Workstream-level goals (above) inform task-level acceptance
criteria but do not replace them. The Manager writes per-task
criteria when planning the scope.

## Working in this workstream

* **Scope-first workflow** applies to bodies of work with 4+ related
  tasks that need cross-task ordering or verification. 2-3 related
  tasks: create them as plain tasks chained with `depends_on` — no
  scope. (See the Manager's CLAUDE.md for the full protocol.)
* Task briefs reference workstream context implicitly — you do NOT
  re-paste this file's content into a task's `context` field. Your
  task's STEP 0.0 tells you to Read this file before acting (it is
  NOT auto-discovered — it lives outside your session's cwd).
* When the user describes a new project that doesn't fit here,
  the Manager offers a NEW workstream via the chat selector
  (option C) rather than mixing contexts — the backend creates
  it from the user's click; the Manager never creates it.
* **Durable requirements live in the spec, not here.** For multi-scope
  (Tier-3) work this workstream has a requirements **spec** at
  `{spec_path}` — the WHAT/WHY contract (`REQ-n`) that briefs cite and
  reviews verify against. The Context Notes below are for supplementary
  conventions/references only; requirement-level content belongs in the
  spec (the Planner migrates any existing notes into the spec's Goal /
  Constraints sections when it first drafts the spec).

---

## Context Notes

"""
    context_notes = ws.get("context_notes") or (
        "No additional context yet. "
        "Edit this section in the Workstream Settings to add "
        "project-specific notes, conventions, references, and "
        "constraints that agents should know about when working on "
        "tasks in this workstream.\n\n"
        "Good things to put here:\n"
        "- Project-specific terminology and definitions\n"
        "- Technical conventions (coding style, architecture patterns, "
        "naming rules)\n"
        "- Key references (documentation links, design docs, API specs)\n"
        "- Constraints (budget limits, technology restrictions, timeline)\n"
        "- Stakeholder preferences and past decisions\n"
        "- Links to important KB documents or office files\n\n"
        "**Tip:** when you find yourself repeating the same instruction "
        "in multiple task briefs in this workstream (e.g. \"always use "
        "snake_case in API responses\", \"the production deploy lives "
        "at https://...\"), promote it here so it applies to every "
        "future task without re-typing."
    )
    return auto_section + context_notes
