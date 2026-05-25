"""Workstream CLAUDE.md generator (split from claude_md_content.py).

The auto-section (name, priority, description, goals, ID metadata,
working-conventions scaffolding) is overwritten on every sync. The
Context Notes section preserves user-editable content.
"""
from __future__ import annotations


def generate_workstream_claude_md(ws: dict) -> str:
    """Generate CLAUDE.md for a workstream.

    Auto-sections are always overwritten; the Context Notes section
    preserves user-editable content (the writer protects it by
    splitting on the marker at the bottom of the auto-section).
    """
    short_code = ws.get("short_code") or "?"
    name = ws.get("name", "Untitled")
    description = ws.get("description") or "No description provided."
    goals = ws.get("goals") or "No goals defined yet."
    priority = ws.get("priority", "medium")

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
re-doing the work. Good criteria name the artefact, the place, and
the verification method ("Report saved to outputs/X.md, contains
sections A/B/C, cites ≥8 sources with URLs").

Workstream-level goals (above) inform task-level acceptance
criteria but do not replace them. The Manager writes per-task
criteria when planning the scope.

## Working in this workstream

* **Scope-first workflow** is mandatory for any body of work with
  2+ related tasks (see the Manager's CLAUDE.md for the full
  protocol).
* Task briefs reference workstream context implicitly — you do NOT
  re-paste this file's content into a task's `context` field.
  Workers auto-load this CLAUDE.md alongside their task brief.
* When the user describes a new project that doesn't fit here,
  the Manager creates a NEW workstream rather than mixing
  contexts.

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
