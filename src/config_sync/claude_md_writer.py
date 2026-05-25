"""CLAUDE.md writer — writes CLAUDE.md files for office, manager, agents, workstreams.

Responsible for:
- Writing the shared office-level CLAUDE.md (auto-discovered by ALL agents)
- Writing the Manager-specific CLAUDE.md (in agents/manager/)
- Writing per-agent CLAUDE.md files (system agents from constants, custom from config)
- Writing per-workstream CLAUDE.md files
- Cleaning up orphan directories when agents/workstreams are removed
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from src.config_sync.claude_md_content import (
    SHARED_OFFICE_CLAUDE_MD,
    MANAGER_CLAUDE_MD,
    SYSTEM_AGENT_CLAUDE_MD,
    generate_custom_agent_claude_md,
    generate_workstream_claude_md,
)
from src.paths import slugify

logger = logging.getLogger(__name__)


def _build_subagents_section(subagents: list[dict]) -> str:
    """Render a ``## Your Subagents`` block from the agent config.

    Empty list → empty string (no section added). Each entry is
    expected to carry at minimum ``name`` + ``description``; the
    ``allowed_tools`` and ``model`` fields are surfaced too when
    present so the agent knows the constraints before spawning.

    Without this section the agent's CLAUDE.md never tells it that
    subagents exist, so the SDK's Task tool stays unused and the
    cost-optimised decomposition pattern (delegate research to a
    cheap sonnet subagent; keep the main session on opus for
    decision-making) never happens.
    """
    if not subagents:
        return ""

    lines: list[str] = [
        "",
        "",
        "---",
        "",
        "## Your Subagents",
        "",
        "You may spawn the following pre-configured subagents via the",
        "Task tool when the work decomposes naturally (e.g. parallel",
        "research across N sources, parallel test runs, parallel",
        "cross-checks). Each subagent runs in its own session — they",
        "do NOT share context with you or each other — so brief them",
        "fully and aggregate their reports yourself. Subagents",
        "CANNOT spawn their own subagents (flat hierarchy by SDK",
        "limitation).",
        "",
    ]
    for sub in subagents:
        name = sub.get("name") or "(unnamed)"
        desc = (sub.get("description") or "").strip() or "(no description)"
        allowed = sub.get("allowed_tools") or []
        model = sub.get("model") or ""
        lines.append(f"### `{name}`")
        lines.append(desc)
        if allowed:
            lines.append("")
            lines.append(f"- **Tools**: {', '.join(allowed)}")
        if model:
            lines.append(f"- **Model**: {model}")
        when = (sub.get("when_to_use") or "").strip()
        if when:
            lines.append(f"- **When to use**: {when}")
        lines.append("")
    return "\n".join(lines)


class ClaudeMdWriter:
    """Writes and manages CLAUDE.md files in the workspace."""

    def __init__(self, workspace_path: str) -> None:
        self._workspace = Path(workspace_path)

    def sync_all(self, config: dict) -> None:
        """Full sync — write all CLAUDE.md files from config."""
        self.ensure_directory_structure()
        self.write_office_claude_md(config)
        self.write_manager_claude_md(config)
        self.sync_agent_directories(config.get("agents", []))
        self.sync_workstream_directories(config.get("workstreams", []))

    def ensure_directory_structure(self) -> None:
        """Create base directories for agents and workstreams."""
        (self._workspace / "agents").mkdir(parents=True, exist_ok=True)
        (self._workspace / "agents" / "manager").mkdir(parents=True, exist_ok=True)
        (self._workspace / "workstreams").mkdir(parents=True, exist_ok=True)

    def write_office_claude_md(self, config: dict) -> None:
        """Write the shared office-level CLAUDE.md.

        This file is auto-discovered by ALL agents (it's in the parent
        directory of each agent's working directory).  Contains only
        shared workspace conventions — no Manager-specific rules.
        """
        office_name = config.get("office_name", "Office")
        content = SHARED_OFFICE_CLAUDE_MD.format(office_name=office_name)
        path = self._workspace / "CLAUDE.md"
        path.write_text(content)
        logger.info("Wrote shared office CLAUDE.md for '%s'", office_name)

    def write_manager_claude_md(self, config: dict) -> None:
        """Write the Manager-specific CLAUDE.md to agents/manager/.

        Auto-discovered when Manager runs from /workspace/agents/manager/.
        Contains orchestration rules, tools, delegation patterns, etc.

        Office customisation
        --------------------
        ``config["claude_md_content"]`` carries office-owner-supplied
        context (business purpose, domain glossary, house rules).
        Older versions of this writer REPLACED the system Manager
        CLAUDE.md with that custom content — which silently dropped
        every orchestration rule (scope workflow, forbidden tools,
        review semantics, archive guidance) and made customised
        offices misbehave in subtle ways.

        The custom content is now APPENDED as a dedicated
        "Office-Specific Context" section BELOW the authoritative
        system template. System rules remain canonical; office context
        enriches them without overriding.
        """
        office_name = config.get("office_name", "Office")
        custom_content = (config.get("claude_md_content") or "").strip()

        base = MANAGER_CLAUDE_MD.format(office_name=office_name)
        if custom_content:
            content = (
                f"{base}\n\n"
                "---\n\n"
                "# Office-Specific Context\n\n"
                "The section below is office-owner content for THIS office "
                "(business purpose, domain glossary, house rules). It is an "
                "enrichment — it does NOT override the orchestration rules "
                "above. If there is ever a conflict between this section "
                "and a rule above, the rule above wins.\n\n"
                f"{custom_content}\n"
            )
        else:
            content = base

        manager_dir = self._workspace / "agents" / "manager"
        manager_dir.mkdir(parents=True, exist_ok=True)
        (manager_dir / "CLAUDE.md").write_text(content)
        logger.info("Wrote Manager CLAUDE.md for '%s'", office_name)

    def sync_agent_directories(self, agents: list[dict]) -> None:
        """Create/update/delete agent directories and CLAUDE.md files.

        - System agents: ALWAYS overwritten from SYSTEM_AGENT_CLAUDE_MD constants.
        - Custom agents: use claude_md_content if set, else generate from config.
        - Orphan directories (agents no longer in config) are removed.
        - The 'manager' directory is never cleaned up (handled separately).
        """
        agents_dir = self._workspace / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)

        seen_names: set[str] = {"manager"}  # Protect manager dir from orphan cleanup

        for agent in agents:
            name = agent.get("name", "")
            if not name:
                continue
            seen_names.add(name)

            agent_dir = agents_dir / name
            agent_dir.mkdir(exist_ok=True)

            content = self._get_agent_claude_md(agent)
            (agent_dir / "CLAUDE.md").write_text(content)

        # Clean up orphan agent directories
        for child in agents_dir.iterdir():
            if child.is_dir() and child.name not in seen_names:
                shutil.rmtree(child)
                logger.info("Removed orphan agent directory: %s", child.name)

        if seen_names:
            logger.info("Synced %d agent CLAUDE.md files", len(seen_names))

    def sync_workstream_directories(self, workstreams: list[dict]) -> None:
        """Create/update/delete workstream directories and CLAUDE.md files."""
        ws_dir = self._workspace / "workstreams"
        ws_dir.mkdir(parents=True, exist_ok=True)

        seen_slugs: set[str] = set()

        for ws in workstreams:
            name = ws.get("name", "")
            if not name:
                continue
            slug = slugify(name)
            seen_slugs.add(slug)

            workstream_dir = ws_dir / slug
            workstream_dir.mkdir(exist_ok=True)

            content = generate_workstream_claude_md(ws)
            (workstream_dir / "CLAUDE.md").write_text(content)

        # Clean up orphan workstream directories
        for child in ws_dir.iterdir():
            if child.is_dir() and child.name not in seen_slugs:
                shutil.rmtree(child)
                logger.info("Removed orphan workstream directory: %s", child.name)

        if seen_slugs:
            logger.info("Synced %d workstream CLAUDE.md files", len(seen_slugs))

    @staticmethod
    def _get_agent_claude_md(agent: dict) -> str:
        """Get the CLAUDE.md content for an agent.

        System agents: always use the platform-owned template (the
        per-role CLAUDE.md). Those are the source of truth and must
        not be customised by office owners.

        Custom agents: compose the generator's baseline (role
        signature + SHARED_AGENT_WORK_RULES + completion block) and,
        if ``claude_md_content`` is set, append it as an enrichment
        section. Earlier behaviour REPLACED the generated baseline
        with the user string and silently lost the delivery / tool
        error / reviewer guidance — this caused custom agents to
        routinely fail to register deliverables and to misinterpret
        tool errors as server outages. The enrichment model keeps
        the baseline authoritative and lets office owners layer
        project-specific rules on top.
        """
        name = agent.get("name", "")
        agent_type = agent.get("agent_type", "custom")

        if agent_type == "system" and name in SYSTEM_AGENT_CLAUDE_MD:
            base = SYSTEM_AGENT_CLAUDE_MD[name]
        else:
            base = generate_custom_agent_claude_md(agent)

        # Subagent surface: workers can have AgentDefinition-style
        # subagents wired in via ``worker_prompt.build_subagent_definitions``.
        # Without surfacing them in CLAUDE.md the agent has no idea
        # which subagents exist or when to spawn them — the SDK
        # makes the Task tool available but the agent has no playbook
        # for "use my web-researcher subagent for cross-source
        # comparison, but only on tasks tagged research:multi-source".
        # Append a stable section so the agent always sees the menu.
        subagents = agent.get("subagents") or []
        subagents_section = _build_subagents_section(subagents)

        custom_content = (agent.get("claude_md_content") or "").strip()
        if agent_type == "system" and name in SYSTEM_AGENT_CLAUDE_MD:
            # System agents: only append subagents block (the platform
            # template is authoritative; no office-owner customisation
            # for system agents).
            return base + subagents_section

        if not custom_content:
            return base + subagents_section

        return (
            f"{base}{subagents_section}\n\n"
            "---\n\n"
            "## Office-Specific Notes\n\n"
            "The section below is office-owner content for this agent in "
            "THIS office (project conventions, house rules, domain terms). "
            "It is an enrichment — it does NOT override the rules above. "
            "If there is a conflict, the rules above win.\n\n"
            f"{custom_content}\n"
        )
