"""Workstream CLAUDE.md generator (split from claude_md_content.py).

The auto-section (name, priority, description, goals) is overwritten
on every sync. The Context Notes section preserves user-editable
content.
"""
from __future__ import annotations


def generate_workstream_claude_md(ws: dict) -> str:
    """Generate CLAUDE.md for a workstream.

    Auto-sections (name, priority, description, goals) are always
    overwritten. The Context Notes section preserves user-editable
    content.
    """
    auto_section = f"""# Workstream: {ws.get('name', 'Untitled')}

**Priority:** {ws.get('priority', 'medium')}

## About This Workstream

A workstream is an isolated project context. All tasks in this workstream share this
context — the description, goals, and notes below apply to every task created here.
Agents working on tasks in this workstream should read and respect these conventions.

## Description
{ws.get('description') or 'No description provided.'}

## Goals
{ws.get('goals') or 'No goals defined yet.'}

---

## Context Notes

"""
    context_notes = ws.get("context_notes") or (
        "No additional context yet. "
        "Edit this section in the Workstream Settings to add "
        "project-specific notes, conventions, references, and constraints "
        "that agents should know about when working on tasks "
        "in this workstream.\n\n"
        "Good things to put here:\n"
        "- Project-specific terminology and definitions\n"
        "- Technical conventions (coding style, architecture patterns, naming rules)\n"
        "- Key references (documentation links, design docs, API specs)\n"
        "- Constraints (budget limits, technology restrictions, timeline)\n"
        "- Stakeholder preferences and past decisions\n"
        "- Links to important KB documents or office files"
    )
    return auto_section + context_notes
