"""CLAUDE.md templates package (split from claude_md_content.py).

P3-E: every constant + generator that used to live in the
2777-line ``claude_md_content.py`` is split into a focused module
in this package. The original module path keeps working via the
back-compat shim at ``app.config_sync.claude_md_content``.

Layout:
- ``_office.py`` — SHARED_OFFICE_CLAUDE_MD
- ``_manager.py`` — MANAGER_CLAUDE_MD
- ``_shared_agent.py`` — SHARED_AGENT_WORK_RULES
- ``_system_agents/`` — analyst, manager_assistant, auditor,
  automation_script_developer + SYSTEM_AGENT_CLAUDE_MD dict
- ``_custom_agent.py`` — generate_custom_agent_claude_md
- ``_workstream.py`` — generate_workstream_claude_md
"""
from __future__ import annotations

from src.config_sync.claude_md_templates._custom_agent import (
    generate_custom_agent_claude_md,
)
from src.config_sync.claude_md_templates._manager import (
    MANAGER_CLAUDE_MD,
)
from src.config_sync.claude_md_templates._office import (
    SHARED_OFFICE_CLAUDE_MD,
)
from src.config_sync.claude_md_templates._shared_agent import (
    BASH_CAPABILITY_RULES,
    PLANNER_WORK_RULES,
    SHARED_AGENT_WORK_RULES,
)
from src.config_sync.claude_md_templates._system_agents import (
    ANALYST_CLAUDE_MD,
    AUDITOR_CLAUDE_MD,
    AUTOMATION_SCRIPT_DEV_CLAUDE_MD,
    MANAGER_ASSISTANT_CLAUDE_MD,
    SYSTEM_AGENT_CLAUDE_MD,
)
from src.config_sync.claude_md_templates._workstream import (
    generate_workstream_claude_md,
)


__all__ = [
    "ANALYST_CLAUDE_MD",
    "AUDITOR_CLAUDE_MD",
    "AUTOMATION_SCRIPT_DEV_CLAUDE_MD",
    "BASH_CAPABILITY_RULES",
    "MANAGER_ASSISTANT_CLAUDE_MD",
    "MANAGER_CLAUDE_MD",
    "PLANNER_WORK_RULES",
    "SHARED_AGENT_WORK_RULES",
    "SHARED_OFFICE_CLAUDE_MD",
    "SYSTEM_AGENT_CLAUDE_MD",
    "generate_custom_agent_claude_md",
    "generate_workstream_claude_md",
]
