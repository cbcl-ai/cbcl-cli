"""Backwards-compatible shim for the pre-P3-E ``claude_md_content`` module.

The 2777-line monolith was split into the
``src.config_sync.claude_md_templates`` package in P3-E. Every name
that used to live here is re-exported below so existing importers
(``from src.config_sync.claude_md_content import SHARED_OFFICE_CLAUDE_MD``
etc.) keep working unchanged.

The reason for keeping this shim instead of asking call sites to
re-target the new package: a single broken import would crash the
communicator daemon at startup, and we don't have a static-analysis
pass on the communicator yet. The shim makes the split risk-free
for now; a follow-up sweep can update the imports and delete this
file.
"""
from __future__ import annotations

from src.config_sync.claude_md_templates import (  # noqa: F401
    ANALYST_CLAUDE_MD,
    AUDITOR_CLAUDE_MD,
    AUTOMATION_SCRIPT_DEV_CLAUDE_MD,
    MANAGER_ASSISTANT_CLAUDE_MD,
    MANAGER_CLAUDE_MD,
    SHARED_AGENT_WORK_RULES,
    SHARED_OFFICE_CLAUDE_MD,
    SYSTEM_AGENT_CLAUDE_MD,
    generate_custom_agent_claude_md,
    generate_workstream_claude_md,
)


__all__ = [
    "ANALYST_CLAUDE_MD",
    "AUDITOR_CLAUDE_MD",
    "AUTOMATION_SCRIPT_DEV_CLAUDE_MD",
    "MANAGER_ASSISTANT_CLAUDE_MD",
    "MANAGER_CLAUDE_MD",
    "SHARED_AGENT_WORK_RULES",
    "SHARED_OFFICE_CLAUDE_MD",
    "SYSTEM_AGENT_CLAUDE_MD",
    "generate_custom_agent_claude_md",
    "generate_workstream_claude_md",
]
