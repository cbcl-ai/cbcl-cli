"""T5.1.4 (06/I-9) — the Manager prompt's session-lock trigger set must
match the code constant.

The MCP server locks the per-turn terminal action on a specific set of
``move_task`` statuses; the Manager CLAUDE.md describes the same lock. They
drifted (prompt said {done, ready, blocked}; code locks (done, ready)),
which could make the Manager believe its tools are dead after a manual
``move_task → blocked`` and skip the mandatory blocking-cause comment. This
pins both sides to one source of truth.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from src.config_sync.claude_md_templates._manager import MANAGER_CLAUDE_MD

_MCP_PATH = (
    Path(__file__).resolve().parent.parent
    / "src" / "_agent_image" / "mcp_tool_server.py"
)
_spec = importlib.util.spec_from_file_location("mcp_tool_server", _MCP_PATH)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _lock_section() -> str:
    # The raw template uses {{ }} escaping; render with empty fields so the
    # lock line reads as the agent will see it.
    rendered = MANAGER_CLAUDE_MD.replace("{office_name}", "X")
    start = rendered.index("## Per-Turn Session Lock")
    end = rendered.index("## ", start + 1)
    return rendered[start:end]


def test_lock_section_lists_exactly_the_code_constant():
    section = _lock_section()
    for status in _mod.SESSION_LOCK_MOVE_STATUSES:
        assert f"`{status}`" in section, (
            f"lock status {status!r} missing from the Manager prompt"
        )


def test_lock_section_excludes_blocked_from_the_trigger_enumeration():
    # ``blocked`` must NOT appear in the ``new_status in {...}`` enumeration
    # (the lock-trigger list). Explanatory prose elsewhere may mention it.
    section = _lock_section()
    after = section.split("new_status` in", 1)[1]
    enumeration = after.split("via the Manager", 1)[0]
    assert "blocked" not in enumeration.lower()


def test_code_constant_is_done_ready():
    assert _mod.SESSION_LOCK_MOVE_STATUSES == ("done", "ready")
    assert _mod.SESSION_LOCK_STATUS_UPDATE_STATUSES == ("review", "blocked")
