"""Config sync service — stores and provides office configuration.

When the platform sends a sync_config message, the ConfigStore holds
the agents, workstreams, and office settings in memory for use by the
Manager controller and other components.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _normalize_mounts(mounts: list[dict]) -> list[tuple[str, str, bool]]:
    """Canonical comparison form for extra_mounts.

    Order-independent (sorted by host+container) and strips keys that
    don't affect Docker semantics so cosmetic payload differences
    (e.g. dict-key ordering, missing read_only flag) don't trigger
    a spurious drift warning.
    """
    out: list[tuple[str, str, bool]] = []
    for m in mounts:
        if not isinstance(m, dict):
            continue
        host = str(m.get("host_path") or "").strip()
        container = str(m.get("container_path") or "").strip()
        if not host or not container:
            continue
        out.append((host, container, bool(m.get("read_only", True))))
    return sorted(out)


class ConfigStore:
    """Holds the synced office configuration in memory."""

    def __init__(self) -> None:
        self.office_config: dict | None = None
        self.agents: list[dict] = []
        self.workstreams: list[dict] = []
        self.scopes: list[dict] = []
        self.scripts: list[dict] = []
        self.connectors: list[dict] = []
        # Snapshot of extra_mounts at container-start time. Used to
        # detect drift on subsequent sync_config messages — if the
        # user changes the mount config after the container is
        # already running, Docker can't add mounts to a live
        # container, so we emit a warning telling them to restart
        # the office to apply.
        self._extra_mounts_applied: list[dict] | None = None

    async def update_from_sync(self, message: dict) -> None:
        """Handle sync_config message — store config locally.

        Called as a WS message handler so it receives the full message dict.
        """
        config = message.get("config", {})
        self.office_config = config
        self.agents = config.get("agents", [])
        self.workstreams = config.get("workstreams", [])
        self.scopes = config.get("scopes", [])
        self.scripts = config.get("scripts", [])
        self.connectors = config.get("connectors", [])

        office_name = config.get("office_name", "unknown")
        logger.info(
            "Config synced for '%s': %d agents, %d workstreams, %d scopes, "
            "%d scripts, %d connectors",
            office_name,
            len(self.agents),
            len(self.workstreams),
            len(self.scopes),
            len(self.scripts),
            len(self.connectors),
        )
        self._detect_extra_mounts_drift(config, office_name)

    def mark_extra_mounts_applied(self, mounts: list[dict] | None) -> None:
        """Called by the container manager AFTER it brings the office
        container up. Captures the mount set that's actually live in
        Docker so subsequent ``update_from_sync`` calls can detect
        drift and warn the user to restart."""
        self._extra_mounts_applied = list(mounts or [])

    def _detect_extra_mounts_drift(
        self, config: dict, office_name: str,
    ) -> None:
        incoming = config.get("extra_mounts") or []
        if self._extra_mounts_applied is None:
            # Container hasn't started yet (or this is the bootstrap
            # sync). No drift to report.
            return
        if _normalize_mounts(incoming) == _normalize_mounts(
            self._extra_mounts_applied,
        ):
            return
        logger.warning(
            "Office '%s': extra_mounts config changed but the container "
            "is already running. Docker can't add or remove mounts on a "
            "live container — restart the office (cbcl stop && cbcl "
            "start, or restart the office container) to apply the new "
            "mount set.", office_name,
        )

    def update_from_agent_config(self, agent_config: dict) -> None:
        """Populate the store from a single agent config dict.

        Used by agent_worker.py when running inside a subprocess. The
        subprocess does not receive a full sync_config message — instead
        the Orchestrator embeds the relevant agent config in the
        ChatMessage or AssignTask IPC message. This method creates a
        minimal ConfigStore from that config so that prompt builders
        (e.g., build_dynamic_context) can call get_team_roster() etc.
        """
        if not agent_config:
            return
        # Treat the single agent config as the only agent in the roster
        self.agents = [agent_config]
        # Extract office-level fields if embedded
        self.office_config = agent_config.get("_office_config", {})
        self.workstreams = agent_config.get("_workstreams", [])

    def get_team_roster(self) -> str:
        """Build a formatted team roster string for the Manager's prompt.

        Includes each agent's skills and their connection_types so the
        Manager knows which agents can access which external services.
        """
        if not self.agents:
            return "No agents configured."

        system_agents = [a for a in self.agents if a.get("agent_type") == "system"]
        custom_agents = [a for a in self.agents if a.get("agent_type") == "custom"]

        lines: list[str] = []

        if system_agents:
            lines.append("## System Agents (always active)")
            lines.append("")
            for agent in system_agents:
                lines.extend(self._format_agent(agent))
                lines.append("")

        if custom_agents:
            lines.append("## Custom Agents")
            lines.append("")
            for agent in custom_agents:
                lines.extend(self._format_agent(agent))
                lines.append("")

        return "\n".join(lines)

    def _format_agent(self, agent: dict) -> list[str]:
        """Format a single agent entry for the team roster."""
        emoji = agent.get("avatar_emoji", "🤖")
        display_name = agent.get("display_name", agent.get("name", "Unknown"))
        name = agent.get("name", "unknown")
        lines = [f"**{display_name}** ({name}) — {emoji}"]

        role = agent.get("role_description", "")
        if role:
            lines.append(f"- Role: {role}")

        # Platform-wide default is Opus 4.7 (the latest "thinking"
        # Opus); imported from the single source of truth so a tier
        # rollout updates this fallback too.
        from src.orchestrator._model_defaults import FALLBACK_WORKER_MODEL
        model = agent.get("model", FALLBACK_WORKER_MODEL)
        lines.append(f"- Model: {model}")

        tools = agent.get("allowed_tools", [])
        if tools:
            lines.append(f"- Tools: {', '.join(tools)}")

        skills = agent.get("skills", [])
        if skills:
            skill_names = [s.get("name", "") for s in skills if s.get("name")]
            if skill_names:
                lines.append(f"- Skills: {', '.join(skill_names)}")

        connectors = agent.get("connectors", [])
        if connectors:
            conn_parts = []
            for c in connectors:
                c_name = c.get("display_name") or c.get("name", "")
                c_type = c.get("connection_type", "")
                if c_type and c_type != "generic":
                    conn_parts.append(f"{c_name} (🔗 {c_type})")
                else:
                    conn_parts.append(c_name)
            lines.append(f"- Connectors: {', '.join(conn_parts)}")

        if not agent.get("is_active", True):
            lines.append("- Status: INACTIVE")

        return lines

    def get_agent(self, agent_name: str) -> dict | None:
        """Get an agent config by name."""
        return next(
            (a for a in self.agents if a.get("name") == agent_name),
            None,
        )

    def get_agent_names(self) -> list[str]:
        """Get names of all active agents."""
        return [
            a.get("name", "")
            for a in self.agents
            if a.get("name") and a.get("is_active", True)
        ]

    def get_workstream_list(self) -> list[dict]:
        """Return workstream summaries for General Chat context."""
        return [
            {
                "id": ws.get("id", ""),
                "name": ws.get("name", ""),
                "description": ws.get("description", ""),
                "priority": ws.get("priority", "medium"),
                "task_count": ws.get("task_count", 0),
            }
            for ws in self.workstreams
        ]

    def get_manager_skills(self) -> list[dict]:
        """Return skills assigned to the Manager (if any).

        The Manager doesn't have skill assignments in the current data
        model, but this method supports a future extension. Returns an
        empty list when no Manager skills are configured.
        """
        if not self.office_config:
            return []
        return self.office_config.get("manager_skills", [])

    def get_workstream(self, workstream_id: str) -> dict | None:
        """Get a specific workstream's full details."""
        return next(
            (ws for ws in self.workstreams if ws.get("id") == workstream_id),
            None,
        )

    def get_scope(self, scope_id: str) -> dict | None:
        """Get a specific scope by id."""
        return next(
            (s for s in self.scopes if s.get("id") == scope_id),
            None,
        )

    def get_executing_scope(self, workstream_id: str) -> dict | None:
        """Return the currently-executing scope in a workstream, if any."""
        return next(
            (
                s for s in self.scopes
                if s.get("workstream_id") == workstream_id
                and s.get("state") == "executing"
            ),
            None,
        )
