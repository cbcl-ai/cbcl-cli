"""Custom-agent CLAUDE.md generator (split from claude_md_content.py).

Used when ``claude_md_content`` is null on the agent config — the
generator composes the agent's ``system_prompt`` with its skills,
connectors, subagents, and the shared worker boilerplate.
"""
from __future__ import annotations

from src.config_sync.claude_md_templates._shared_agent import (
    SHARED_AGENT_WORK_RULES,
)


def generate_custom_agent_claude_md(agent: dict) -> str:
    """Generate CLAUDE.md for a custom agent from its config.

    Combines the agent's ``system_prompt`` with skills, subagents, and
    standard delivery/completion sections.
    """
    lines = [
        f"# {agent.get('display_name', agent.get('name', 'Agent'))}",
        "",
    ]

    # Agent's system prompt (role, methodology, standards)
    if agent.get("system_prompt"):
        lines.append(agent["system_prompt"])
        lines.append("")

    # Skills (playbooks — SKILL.md files auto-discovered by Claude)
    skills = agent.get("skills", [])
    if skills:
        lines.append("## Skills")
        lines.append("")
        lines.append(
            "These SKILL.md playbooks are available in `.claude/skills/`."
        )
        lines.append(
            "Claude auto-discovers them — use the instructions inside each "
            "playbook."
        )
        lines.append("")
        for skill in skills:
            skill_name = skill.get("display_name", skill.get("name", "?"))
            desc = skill.get("description", "")
            desc_part = f" — {desc}" if desc else ""
            lines.append(f"### {skill_name}{desc_part}")
            lines.append(
                f"Playbook: `.claude/skills/{skill.get('name', '?')}/SKILL.md`"
            )
            params = skill.get("parameter_schema", [])
            if params:
                lines.append("")
                lines.append("**Parameters:**")
                for p in params:
                    p_name = p.get("name", "")
                    p_desc = p.get("description", "")
                    if p.get("is_secret"):
                        lines.append(
                            f"- `{p_name}` (secret) — {p_desc}"
                            if p_desc
                            else f"- `{p_name}` (secret)"
                        )
                    else:
                        lines.append(
                            f"- `{p_name}` — {p_desc}"
                            if p_desc
                            else f"- `{p_name}`"
                        )
                lines.append("")
                lines.append(
                    "Parameter values are stored in "
                    f"`.claude/skills/{skill.get('name', '?')}/params.json` "
                    "(non-secrets). Use `{{{{PARAM_NAME}}}}` syntax in playbooks."
                )
            lines.append("")

    # Connectors (MCP services + API credentials)
    connectors = agent.get("connectors", [])
    if connectors:
        lines.append("## Service Connectors")
        lines.append("")
        for conn in connectors:
            conn_name = conn.get("display_name") or conn.get("name", "?")
            conn_type = conn.get("connection_type", "")
            if conn.get("mcp_server_name"):
                lines.append(
                    f"- **{conn_name}** (🔗 {conn_type}) — "
                    "MCP tools available via `claude mcp`"
                )
            else:
                params = conn.get("parameter_schema", [])
                env_names = [
                    p.get("name", "") for p in params if p.get("name")
                ]
                if env_names:
                    lines.append(
                        f"- **{conn_name}** — credentials: "
                        + ", ".join(f"`{n}`" for n in env_names)
                    )
                else:
                    lines.append(f"- **{conn_name}**")
        lines.append("")

    # Subagents
    subagents = agent.get("subagents") or {}
    if subagents:
        lines.append("## Agent Helpers")
        lines.append(
            "You can spawn these subagents to decompose complex work:"
        )
        for name, sa in subagents.items():
            lines.append(
                f"- **{name}** — {sa.get('description', 'No description')}"
            )
        lines.append("")

    # Shared worker boilerplate — same block used by every system
    # agent's CLAUDE.md. Anything role-specific (output formats,
    # review approach, test protocols) belongs in the agent's own
    # ``system_prompt`` above.
    lines.append(SHARED_AGENT_WORK_RULES)

    # Completion (generic — custom agents do not have role-specific
    # pre-submission protocols, so we give the baseline flow here).
    lines.extend([
        "",
        "## Completion (when executing, not reviewing)",
        "",
        "1. Check your output against each acceptance criterion in the brief.",
        "2. Run the verification steps.",
        "3. Ensure deliverables are written to disk and registered via",
        "   `mcp__cubicle-tools__save_file`. If `save_file` fails, post a",
        "   checkpoint with the file path and submit anyway.",
        "4. Call `mcp__cubicle-tools__update_status` with new_status `review`.",
        "5. **STOP IMMEDIATELY** — do not continue the session after.",
    ])

    return "\n".join(lines)
