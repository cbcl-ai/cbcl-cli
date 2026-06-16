"""T5.4.3 — prompt-instructed transitions are legal (would have caught F9).

Templates instruct move_task / update_status targets in prose; nothing checked
them against the real backend VALID_TRANSITIONS — which is how "move to
backlog" (F9) survived. backlog is a SOURCE-only status (no transition targets
it), so it must NEVER appear as an instructed move target.
"""
from __future__ import annotations

import re

from app.tasks.board import VALID_TRANSITIONS
from src.config_sync.claude_md_content import (
    ANALYST_CLAUDE_MD,
    AUDITOR_CLAUDE_MD,
    AUTOMATION_SCRIPT_DEV_CLAUDE_MD,
    MANAGER_ASSISTANT_CLAUDE_MD,
    MANAGER_CLAUDE_MD,
    SHARED_AGENT_WORK_RULES,
    SHARED_OFFICE_CLAUDE_MD,
)
from src.config_sync.claude_md_templates._system_agents import PLANNER_CLAUDE_MD
from src.config_sync.claude_md_templates._custom_agent import (
    generate_custom_agent_claude_md,
)
from src.config_sync._auto_decide_rows import (
    AUTO_DECIDE_ROWS,
    render_auto_decide_guidance,
)
from src.config_sync._tool_allowlist import render_manager_allowlist
from src.orchestrator.worker_prompt import build_worker_prompt
from src._agent_image._mcp.tools_manager import get_manager_tools
from src._agent_image._mcp.tools_worker import get_worker_tools
from src._agent_image._mcp.tools_planner import get_planner_tools

_LEGAL_TARGETS = {t for tos in VALID_TRANSITIONS.values() for t in tos}


def _tool_descriptions(tools: list[dict]) -> str:
    """Concatenated tool `description` prose — the highest-leverage prompt
    surface (per communicator/CLAUDE.md), so its move/status targets must be
    legal too."""
    return "\n\n".join(t.get("description", "") for t in tools)


def _worker_prompt(status: str, rework_count: int = 0) -> str:
    """Render a worker task prompt for a given dispatch status so the eval
    scans the move/status targets it actually instructs (T5.4.3 done-when:
    'worker_prompt.py blocks' + 'all templates scanned')."""
    return build_worker_prompt({
        "task_id": "00000000-0000-0000-0000-000000000001",
        "readable_id": "RC-001.T05",
        "title": "x", "status": status, "rework_count": rework_count,
        "recent_activities": [], "artifacts": [], "reviewer": "auditor",
        "assigned_agent": "dev",
        "brief": {
            "goal": "g", "context": "c", "inputs": "i",
            "output_format": "short", "acceptance_criteria": ["a"],
            "allowed_tools": [], "required_skills": [],
            "risks_and_edge_cases": "none", "verification_steps": "v",
        },
    })


_SURFACES = {
    "manager": MANAGER_CLAUDE_MD.replace(
        "{manager_tool_allowlist}", render_manager_allowlist()
    ).replace("{office_name}", "X"),
    "office": SHARED_OFFICE_CLAUDE_MD.replace("{office_name}", "X"),
    "manager_assistant": MANAGER_ASSISTANT_CLAUDE_MD,
    "analyst": ANALYST_CLAUDE_MD,
    "auditor": AUDITOR_CLAUDE_MD,
    "automation_script_developer": AUTOMATION_SCRIPT_DEV_CLAUDE_MD,
    "shared_agent": SHARED_AGENT_WORK_RULES,
    "planner": PLANNER_CLAUDE_MD,
    "worker_execute": _worker_prompt("ready"),
    "worker_review": _worker_prompt("review"),
    "worker_rework": _worker_prompt("in_progress", rework_count=1),
    # The custom-agent CLAUDE.md appends the shared delivery/completion
    # sections (which instruct update_status targets).
    "custom_agent": generate_custom_agent_claude_md(
        {"name": "x", "display_name": "X", "system_prompt": "Do work."}
    ),
    # The auto-decide guidance rows instruct move_task targets per type.
    "auto_decide": "\n\n".join(
        render_auto_decide_guidance(rt) for rt in AUTO_DECIDE_ROWS
    ),
    # Tool DESCRIPTIONS are prompt content the model reads at call time —
    # the update_status / move_task descriptions instruct status targets.
    "manager_tool_descriptions": _tool_descriptions(get_manager_tools()),
    "worker_tool_descriptions": _tool_descriptions(get_worker_tools()),
    "planner_tool_descriptions": _tool_descriptions(get_planner_tools()),
}

# move_task(... "X") / move_task → X / update_status(... blocked)
_MOVE_TARGET_RE = re.compile(
    r"(?:move_task|update_status)[^\n]{0,80}?[`\"'(→>= ]"
    r"(backlog|ready|in_progress|blocked|review|done|archived)\b"
)


def test_backlog_is_never_an_instructed_move_target():
    # backlog has no inbound transition; instructing a move to it is the F9 bug.
    assert "backlog" not in _LEGAL_TARGETS  # sanity: board really forbids it
    offenders = {}
    for name, text in _SURFACES.items():
        for m in re.finditer(
            r"(?:move_task|update_status)[^\n]{0,60}?backlog", text
        ):
            # Allow explicit "no transition into backlog" disclaimers.
            window = text[max(0, m.start() - 40): m.end() + 10].lower()
            if "no transition into" in window or "not move" in window:
                continue
            offenders.setdefault(name, []).append(m.group(0))
    assert not offenders, f"prompt instructs a move to backlog (F9): {offenders}"


def test_instructed_targets_are_legal_statuses():
    for name, text in _SURFACES.items():
        for m in _MOVE_TARGET_RE.finditer(text):
            target = m.group(1)
            if target == "backlog":
                continue  # handled by the dedicated test above
            assert target in _LEGAL_TARGETS, (
                f"{name}: instructed move target {target!r} is not a legal "
                f"transition target {sorted(_LEGAL_TARGETS)}"
            )
