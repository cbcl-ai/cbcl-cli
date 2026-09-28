"""Standalone mirror of the system agents' ``allowed_tools`` (X43/X38).

The Bash capability fragment the workspace writer appends depends on each
role's ``allowed_tools``, so every eval that renders a system-agent playbook
must use the real values. They live in the backend
(``app/agents/system_agents.py`` ``SYSTEM_AGENT_DEFAULTS``), which the
standalone CLI checkout does not ship — importing it there would skip the
evals. This mirror keeps the evals running in both checkouts;
``test_prompt_token_budget.test_role_allowed_tools_mirror_the_backend_system_agents``
enforces parity in the monorepo.
"""
from __future__ import annotations

# ALL EIGHT system agents ship WITH Bash ("platform policy: every agent can
# run commands"). FS-P3: the Architect writes templates via the filesystem
# (Write); the Curator's writes go through the gated collection tools, so its
# CLI toolset carries NO Write.
SYSTEM_AGENT_ALLOWED_TOOLS: dict[str, list[str]] = {
    "analyst": ["Read", "Write", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"],
    "auditor": ["Read", "Glob", "Grep", "Bash", "Write"],
    "automation-script-developer": [
        "Read", "Write", "Bash", "Glob", "Grep", "WebSearch", "WebFetch",
    ],
    "builder": [
        "Read", "Write", "Bash", "Glob", "Grep", "WebSearch", "WebFetch",
    ],
    "manager-assistant": [
        "Read", "Write", "Bash", "Glob", "Grep", "WebSearch", "WebFetch",
    ],
    "planner": ["Read", "Write", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"],
    "flow-architect": ["Read", "Write", "Bash", "Glob", "Grep"],
    "data-curator": ["Read", "Glob", "Grep", "Bash"],
}
