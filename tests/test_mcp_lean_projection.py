"""Unit tests for the lean board/task response projection.

``project_response`` (in ``_mcp.transforms``, surfaced on the MCP server
as ``_project_response``) trims ``get_board`` / ``get_task_detail`` reads
down to the fields the agent orchestrates on, BEFORE the result enters the
long-lived Manager conversation. This is the primary lever against
context bloat — see the docstring in ``transforms.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_MCP_PATH = (
    Path(__file__).resolve().parent.parent
    / "src" / "_agent_image" / "mcp_tool_server.py"
)
_spec = importlib.util.spec_from_file_location("mcp_tool_server", _MCP_PATH)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_project = _mod._project_response


def _full_task() -> dict:
    return {
        "id": "uuid-1",
        "readable_id": "WR-003.T14",
        "title": "Implement nav",
        "description": "x" * 5000,  # the big variable-length field
        "status": "in_progress",
        "assigned_agent": "python-developer",
        "reviewer": "auditor",
        "priority": "high",
        "labels": ["frontend"],
        "workstream_short_code": "WR",
        "scope_short_key": "Auth",
        "scope_readable_id": "WR-003.S01",
        "brief_is_complete": True,
        "depends_on": ["WR-003.T13"],
        # heavy/redundant — must be dropped:
        "office_id": "off-uuid",
        "workstream_id": "ws-uuid",
        "workstream_name": "Website Redesign",
        "assigned_agent_display_name": "Senior Python Developer",
        "assigned_agent_emoji": "👨‍💻",
        "parent_task_id": None,
        "rework_count": 0,
        "has_brief": True,
        "token_cost": 0.42,
        "created_at": "2026-03-12T09:00:00Z",
        "updated_at": "2026-03-13T10:00:00Z",
        "completed_at": None,
    }


def test_get_board_drops_heavy_fields_keeps_orchestration_fields():
    result = {"items": [_full_task()], "total": 1, "limit": 100, "offset": 0}
    lean = _project("get_board", result)
    assert lean["total"] == 1  # envelope preserved
    t = lean["items"][0]
    # kept
    for k in ("id", "readable_id", "title", "status", "assigned_agent",
              "reviewer", "priority", "labels", "workstream_short_code",
              "scope_short_key", "scope_readable_id", "brief_is_complete",
              "depends_on"):
        assert k in t, f"{k} should be kept"
    # dropped — these are the bloat
    for k in ("description", "office_id", "workstream_id", "workstream_name",
              "assigned_agent_display_name", "assigned_agent_emoji",
              "token_cost", "created_at", "updated_at", "rework_count",
              "has_brief", "parent_task_id", "completed_at"):
        assert k not in t, f"{k} should be dropped"


def test_get_task_detail_trims_activities_and_keeps_brief():
    acts = [
        {
            "event_type": "checkpoint",
            "actor": "python-developer",
            "content": "y" * 5000,
            "details": {"blocker_class": "auth_failed", "noise": "z" * 4000},
            "created_at": f"2026-03-12T10:{i:02d}:00Z",
        }
        for i in range(20)
    ]
    result = {
        "readable_id": "WR-003.T14",
        "title": "Implement nav",
        "description": "the detail view keeps this",
        "brief": {"goal": "g", "context": "c"},
        "recent_activities": acts,
        "artifacts": [{"id": "a1", "title": "nav.tsx"}],
        # heavy top-level — dropped:
        "office_id": "off", "workstream_id": "ws", "session_id": "s",
        "token_cost": 1.0, "assigned_agent_emoji": "👨‍💻",
        "assigned_agent_display_name": "Dev", "workstream_name": "WR",
    }
    lean = _project("get_task_detail", result)
    # detail view stays FAITHFUL — description + brief + structural fields
    # are all kept (we only trim the activity feed, the real bloat).
    assert lean["description"] == "the detail view keeps this"
    assert lean["brief"]["goal"] == "g"
    for k in ("office_id", "workstream_id", "session_id"):
        assert k in lean, f"{k} should be kept (faithful detail view)"
    # activities trimmed to last 10, content capped, details slimmed
    ra = lean["recent_activities"]
    assert len(ra) == 10
    assert len(ra[0]["content"]) <= 600 + len(" …(truncated)")
    assert ra[0]["details"] == {"blocker_class": "auth_failed"}  # noise dropped


def test_error_and_other_actions_pass_through_unchanged():
    err = {"error": "boom"}
    assert _project("get_board", err) is err
    other = {"items": [{"description": "keep me"}]}
    assert _project("create_task", other) is other  # not a read → untouched
    assert _project("get_board", "not-a-dict") == "not-a-dict"


def test_get_board_without_items_is_untouched():
    weird = {"unexpected": "shape"}
    assert _project("get_board", weird) is weird
