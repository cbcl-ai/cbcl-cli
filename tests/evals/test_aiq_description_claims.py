"""AI-quality review — tool-description CLAIMS match the code (2026-07-29).

The T5.3.7 failure class: a tool ``description`` states a fact (a
side-effect list, a guard's state set, an enum, a filter surface) and the
code drifts — the model then acts on the stale claim. Following the
numeric-invariant pattern (test_numeric_invariant_pins.py), each pin
imports the code-side truth and asserts the description against it — no
hard-coded expectation on the code side where a constant exists.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

# Cross-component guard: these pins assert tool descriptions against the
# BACKEND's code-side truth, so they only run in the monorepo layout (the
# cbcl-cli mirror has no `app` package — same posture as the roster-parity
# and numeric-invariant families).
pytest.importorskip(
    "app",
    reason="backend package required — monorepo layout only",
)

from app.action_requests.schemas import REQUEST_TYPES
from app.action_requests.service import _AUTO_UNBLOCK_REQUEST_TYPES
from app.tasks.board import VALID_TRANSITIONS

from src._agent_image._mcp.tools_manager import get_manager_tools
from src._agent_image._mcp.tools_planner import get_planner_tools
from src._agent_image._mcp.tools_worker import get_worker_tools

_LEGAL_TARGETS = {t for tos in VALID_TRANSITIONS.values() for t in tos}


def _manager_tool(name: str) -> dict:
    for t in get_manager_tools():
        if t["name"] == name:
            return t
    raise AssertionError(f"{name} not in manager catalog")


# ---------------------------------------------------------------------------
# 1. decide_action_request — the auto-fire list matches the backend
# ---------------------------------------------------------------------------


def test_decide_action_request_auto_fire_list_matches_backend():
    """The description's "Approval side effects" sentence must name exactly
    the types that fire automatically on approval: ``create_task`` (the
    typed create side effect) plus the blocker-shaped auto-unblock set
    (``_AUTO_UNBLOCK_REQUEST_TYPES``). Naming fewer hides a side effect
    (the Manager double-fires a move_task); naming more invents one."""
    desc = _manager_tool("decide_action_request")["description"]
    assert "Approval side effects" in desc
    segment = desc.split("Approval side effects", 1)[1]
    segment = segment.split("every other type", 1)[0]
    # Parentheticals are guidance ("never ALSO move_task it"), not
    # side-effect claims — exclude them from the scan.
    segment = re.sub(r"\([^)]*\)", "", segment)
    named = {rt for rt in REQUEST_TYPES if rt in segment}
    expected = set(_AUTO_UNBLOCK_REQUEST_TYPES) | {"create_task"}
    assert named == expected, (
        f"description names {sorted(named)}, backend auto-fires "
        f"{sorted(expected)}"
    )
    # The never-also-move guidance rides the same sentence.
    assert "never ALSO move_task it" in desc
    # The old dangling pointer must stay dead.
    assert "Phase A side-effect scope" not in desc


# ---------------------------------------------------------------------------
# 2. create_scope — the "live scope" claim matches the backend guard
# ---------------------------------------------------------------------------


def test_create_scope_live_scope_claim_matches_guard_states():
    """The one-live-scope guard blocks on states
    (preparing/ready/executing/verifying) — the description must say "live
    scope" and name the SAME state set the backend's live_check uses."""
    desc = _manager_tool("create_scope")["description"]
    assert "live scope" in desc
    service_src = (
        Path(__file__).resolve().parents[3]
        / "backend" / "app" / "scopes" / "service.py"
    ).read_text(encoding="utf-8")
    m = re.search(
        r"Scope\.state\.in_\(\((?P<states>[^)]+)\)\)", service_src
    )
    assert m, "one-live-scope guard state set not found in scopes/service.py"
    guard_states = set(re.findall(r'"(\w+)"', m.group("states")))
    for state in guard_states:
        assert state in desc, (
            f"create_scope description omits guard state {state!r}"
        )
    # And the retired narrower claim must not resurface.
    assert "Max one `preparing` scope" not in desc


# ---------------------------------------------------------------------------
# 3. move_task enums offer only legal transition targets
# ---------------------------------------------------------------------------


def test_move_task_enums_are_subset_of_legal_targets():
    catalogs = {
        "manager": get_manager_tools(),
        "worker": get_worker_tools(),
        "planner": get_planner_tools(),
    }
    checked = 0
    for cat_name, tools in catalogs.items():
        for t in tools:
            if t["name"] != "move_task":
                continue
            enum = t["inputSchema"]["properties"]["new_status"]["enum"]
            checked += 1
            assert set(enum) <= _LEGAL_TARGETS, (
                f"{cat_name}.move_task offers illegal targets: "
                f"{set(enum) - _LEGAL_TARGETS}"
            )
    assert checked >= 2  # manager + worker (planner excludes move_task)


# ---------------------------------------------------------------------------
# 4. list_files — every described param is actually read by the handler
# ---------------------------------------------------------------------------


def test_list_files_described_params_are_handler_read():
    """Fix 10 closed the phantom-filter gap (tags/source_agent were
    advertised but ignored). Pin it structurally: every inputSchema param
    on BOTH catalogs' list_files must be read by the backend handler."""
    from app.ws.office_file_handler import request_office_list_files

    handler_src = inspect.getsource(request_office_list_files)
    for tools in (get_manager_tools(), get_worker_tools()):
        lf = next(t for t in tools if t["name"] == "list_files")
        for param in lf["inputSchema"]["properties"]:
            assert f'params.get("{param}"' in handler_src, (
                f"list_files describes param {param!r} the handler never "
                "reads — the T5.3.7 phantom-filter class"
            )
    # The AND-match contract is stated, and identically on both catalogs.
    mgr = next(t for t in get_manager_tools() if t["name"] == "list_files")
    wrk = next(t for t in get_worker_tools() if t["name"] == "list_files")
    assert "AND-match" in mgr["description"]
    assert mgr["description"] == wrk["description"]
    assert mgr["inputSchema"] == wrk["inputSchema"]


# ---------------------------------------------------------------------------
# 5. create_scope short_key carries the milestone-link rule
# ---------------------------------------------------------------------------


def test_short_key_description_carries_milestone_link_rule():
    props = _manager_tool("create_scope")["inputSchema"]["properties"]
    desc = props["short_key"]["description"]
    assert "MUST equal the milestone key exactly" in desc
    assert "coverage gate" in desc


# ---------------------------------------------------------------------------
# 6. consult_planner carries the consent-refusal recovery
# ---------------------------------------------------------------------------


def test_consult_planner_carries_consent_refusal_recovery():
    desc = _manager_tool("consult_planner")["description"]
    # Pivot-3: consent rides the spec approval; drafting (specify) is
    # free in default mode; the bubble is the manager-approval fallback.
    assert "mode='specify' works in default mode" in desc
    assert "get it APPROVED" in desc
    assert "approval starts the program" in desc
    assert "ask_user_choice(kind='execution_mode')" in desc  # fallback
    assert "you never self-consent" in desc
    assert "NEVER surface the refusal error" in desc
    # The single-scope framing is the SINGLE-SCOPE COLLAPSE — one
    # consistent story, not the old "don't use for single scope" vs
    # "shortcut: use materialize" contradiction.
    assert "SINGLE-SCOPE COLLAPSE" in desc
    assert "3+ scope projects" not in desc
