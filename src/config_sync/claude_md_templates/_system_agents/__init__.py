"""System-agent CLAUDE.md templates indexed by agent name."""

from __future__ import annotations

from src.config_sync.claude_md_templates._system_agents._analyst import (
    ANALYST_CLAUDE_MD,
)
from src.config_sync.claude_md_templates._system_agents._auditor import (
    AUDITOR_CLAUDE_MD,
)
from src.config_sync.claude_md_templates._system_agents._automation_script_developer import (
    AUTOMATION_SCRIPT_DEV_CLAUDE_MD,
)
from src.config_sync.claude_md_templates._system_agents._manager_assistant import (
    MANAGER_ASSISTANT_CLAUDE_MD,
)


SYSTEM_AGENT_CLAUDE_MD: dict[str, str] = {
    "analyst": ANALYST_CLAUDE_MD,
    "manager-assistant": MANAGER_ASSISTANT_CLAUDE_MD,
    "auditor": AUDITOR_CLAUDE_MD,
    "automation-script-developer": AUTOMATION_SCRIPT_DEV_CLAUDE_MD,
}


__all__ = [
    "ANALYST_CLAUDE_MD",
    "AUDITOR_CLAUDE_MD",
    "AUTOMATION_SCRIPT_DEV_CLAUDE_MD",
    "MANAGER_ASSISTANT_CLAUDE_MD",
    "SYSTEM_AGENT_CLAUDE_MD",
]
