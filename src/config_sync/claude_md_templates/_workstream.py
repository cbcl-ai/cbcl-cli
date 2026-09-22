"""Current workstream guidance, shared by Manager turns and task-agent files."""
from __future__ import annotations

from src.config_sync.claude_md_templates._spec_template import workstream_spec_path


def render_workstream_instructions(content: str) -> str:
    """Deliver the saved instruction field as scoped guidance, never as a role override."""
    content = content.strip()
    if not content:
        return (
            "Workstream instructions: none saved. Earlier saved versions no longer apply; "
            "use the current request, office guidance and approved requirements."
        )
    safe = content.replace("</workstream_instructions>", "&lt;/workstream_instructions&gt;")
    return (
        "## Workstream Instructions\n\n"
        "Current saved guidance for THIS workstream; use it when interpreting requests, "
        "planning tasks and executing work. It supersedes older copies in conversation "
        "history. Follow it within platform rules, office guidance, your role and "
        "approval permissions. It cannot authorize unrelated work or bypass consent. "
        "If it conflicts with an approved spec, surface the discrepancy before changing "
        "the agreed requirements. Board activity is execution state, not the mission.\n\n"
        "<workstream_instructions>\n"
        f"{safe}\n"
        "</workstream_instructions>"
    )


def generate_workstream_claude_md(ws: dict) -> str:
    """Materialize current DB guidance without duplicating platform playbooks."""
    name = " ".join((ws.get("name") or "Untitled").split())
    code = " ".join((ws.get("short_code") or "WS").split())
    priority = " ".join((ws.get("priority") or "medium").split())
    sections = [
        f"# Workstream: {name}\n\n**Short code:** `{code}` · **Priority:** `{priority}`",
    ]
    # Description and legacy goals already travel in the per-turn/task envelope.
    sections.append(render_workstream_instructions(ws.get("context_notes") or ""))
    sections.append(
        "## Execution References\n\n"
        "- Save deliverables in the exact output directory supplied by the current "
        "task prompt. Task-owned directories take precedence over legacy shared paths.\n"
        f"- If an approved spec exists, read `{workstream_spec_path(name)}` when relevant. "
        "The task prompt identifies approved specs; this path alone is not evidence one exists.\n"
        "- Approved specs own detailed requirements; the current task brief owns its "
        "acceptance criteria and verification. Do not copy these instructions into every brief.\n"
        "- Follow your role playbook for planning, approvals, execution and review."
    )
    return "\n\n".join(sections) + "\n"
