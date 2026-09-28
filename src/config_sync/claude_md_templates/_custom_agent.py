"""Custom-agent CLAUDE.md generator (split from claude_md_content.py).

Used when ``claude_md_content`` is null on the agent config — the
generator composes the agent's ``system_prompt`` with its skills,
connectors, subagents, and the shared worker boilerplate.
"""
from __future__ import annotations

from src.config_sync.claude_md_templates._shared_agent import (
    SHARED_AGENT_WORK_RULES,
)


def connector_enabled(connector: dict) -> bool:
    """A connector counts as enabled only when its config says so.

    ``is_enabled`` missing (the REST roster summary shape) means UNKNOWN,
    never enabled (X47).
    """
    return connector.get("is_enabled") is True


def _one_line(value: object) -> str:
    """Collapse whitespace (newlines included) so a field stays one line."""
    return " ".join(str(value or "").split())


def _parameter_entry(parameter: dict) -> str:
    name = _one_line(parameter.get("name"))
    description = _one_line(parameter.get("description"))
    label = f"`{name}` (secret — value not available)" if parameter.get(
        "is_secret"
    ) else f"`{name}`"
    return f"{label} — {description}" if description else label


def render_skill_index(skills: list[dict]) -> list[str]:
    """The assigned-skill index of an agent CLAUDE.md (D1/D2).

    Truthful about the runtime: the native ``Skill`` tool is disallowed, so
    skills are NOT auto-invoked — the agent Reads a listed ``SKILL.md`` when
    the task matches. One line per skill (display name, slug, description,
    path); parameters once per skill; ONE shared parameter footer. Secret
    parameter values are never delivered to sessions (D2).
    """
    if not skills:
        return []
    lines = [
        "## Skills",
        "",
        "Your assigned skills are listed below. Skills are not invoked "
        "automatically: when the current task matches a skill's \"use when\", "
        "`Read` its `SKILL.md` (paths are relative to your working directory) "
        "and apply it; assigned methods are not extra mandatory tasks. "
        "Links and commands inside a `SKILL.md` are relative to that skill's "
        "folder: run its scripts as `cd .claude/skills/<slug> && python3 "
        "scripts/<file>`, passing absolute paths for your inputs and outputs. "
        "`/workspace/.claude/skills/` is the office catalog, not your "
        "assignment — use only the skills listed here.",
        "",
    ]
    any_parameters = False
    for skill in skills:
        slug = _one_line(skill.get("name")) or "?"
        display = _one_line(skill.get("display_name")) or slug
        description = _one_line(skill.get("description"))
        entry = f"- **{display}** (`{slug}`)"
        if description:
            entry += f" — {description}"
        entry += f" — `.claude/skills/{slug}/SKILL.md`"
        lines.append(entry)
        parameters = [
            parameter
            for parameter in skill.get("parameter_schema") or []
            if isinstance(parameter, dict) and _one_line(parameter.get("name"))
        ]
        if parameters:
            any_parameters = True
            lines.append(
                "  Parameters: "
                + "; ".join(_parameter_entry(parameter) for parameter in parameters)
            )
    if any_parameters:
        lines.extend(
            [
                "",
                "Skill parameters: non-secret values live in "
                "`.claude/skills/<skill>/params.json`; a `{{NAME}}` placeholder "
                "in a playbook means read that value there. Secret parameter "
                "values are NOT available to agents — use Office Secrets or "
                "Connectors for credentials.",
            ]
        )
    lines.append("")
    return lines


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

    lines.extend(render_skill_index(agent.get("skills") or []))

    # Connectors (MCP services + API credentials). Only connectors the
    # config marks enabled are advertised (X47): a disabled connector is not
    # a "configured MCP connection", and a roster without the flag (the REST
    # summary shape) is unknown, not enabled.
    connectors = [
        conn for conn in agent.get("connectors") or [] if connector_enabled(conn)
    ]
    if connectors:
        lines.append("## Service Connectors")
        lines.append("")
        for conn in connectors:
            conn_name = conn.get("display_name") or conn.get("name", "?")
            conn_type = conn.get("connection_type", "")
            if conn.get("mcp_server_name"):
                lines.append(
                    f"- **{conn_name}** (🔗 {conn_type}) — "
                    "configured MCP connection; confirm required tools/access at use"
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

    # No subagents/"Helpers" block is rendered here. The static Helpers
    # feature was removed in the item-6 rework in favour of model-driven
    # dynamic workflows (the ``ultracode`` effort); ``claude_md_writer`` emits
    # no subagents section, and its ``_build_subagents_section`` builder was
    # deleted 2026-08-13. (A legacy in-template loop used to render
    # one here, assuming ``subagents`` was a dict-of-dicts; the backend ships
    # it as ``list[dict]``. Both the loop and the writer section are gone.)

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
        "2. Satisfy Execution checks under the task prompt's verification contract;",
        "   reuse applicable evidence and leave Independent review to the reviewer.",
        "3. Ensure THE deliverable(s) named in the Brief's Output Format —",
        "   normally ONE consolidated document — are written to disk and",
        "   registered via `mcp__cubicle-tools__save_file` (hard cap 3; see",
        "   'What counts as an artifact'). If `save_file` fails, post a",
        "   checkpoint with the file path and submit anyway.",
        "4. Call `mcp__cubicle-tools__update_status` with new_status `review`.",
        "5. **STOP IMMEDIATELY** — do not continue the session after.",
    ])

    return "\n".join(lines)
