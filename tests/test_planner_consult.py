"""Phase 3 (communicator half) — Planner consult: toolset, prompt, poke.

The end-to-end spawn → docker-exec → IPC round-trip needs the live daemon
and is verified separately. These cover the host-testable units.
"""
from __future__ import annotations

import pytest

from src._agent_image._mcp.tools_manager import get_manager_tools
from src._agent_image._mcp.tools_planner import get_planner_tools
from src.orchestrator.planner_prompt import build_planner_prompt


def test_manager_has_consult_planner_tool() -> None:
    names = {t["name"] for t in get_manager_tools()}
    assert "consult_planner" in names


def test_planner_toolset_has_plan_tools_and_no_self_consult() -> None:
    names = {t["name"] for t in get_planner_tools()}
    # Plan-write + verify tools present.
    for n in (
        "update_workstream_plan", "get_workstream_plan",
        "update_execution_plan", "get_execution_plan",
        "complete_scope_verification",
    ):
        assert n in names, f"planner toolset missing {n}"
    # Manager-like board tools present (materialize scopes/tasks).
    assert "create_scope" in names
    assert "create_task" in names
    # The Planner does not consult itself.
    assert "consult_planner" not in names


def test_planner_plan_tools_map_to_backend_actions() -> None:
    by_name = {t["name"]: t for t in get_planner_tools()}
    assert by_name["update_workstream_plan"]["action"] == "update_workstream_plan"
    assert by_name["complete_scope_verification"]["action"] == "complete_scope_verification"


@pytest.mark.parametrize(
    "mode,needle",
    [
        ("roadmap", "update_workstream_plan"),
        ("scope_plan", "update_execution_plan"),
        ("materialize", "create_task"),
        ("research", "research"),
        ("verify", "complete_scope_verification"),
    ],
)
def test_planner_prompt_renders_each_mode(mode: str, needle: str) -> None:
    prompt = build_planner_prompt({
        "planner_consult": {
            "mode": mode,
            "objective": "Do the planning",
            "workstream_id": "WS-UUID",
            "scope_id": (
                "SC-UUID"
                if mode in ("scope_plan", "materialize", "verify")
                else ""
            ),
        },
    })
    assert "Do the planning" in prompt
    assert "WS-UUID" in prompt
    assert needle in prompt
    assert "never execute" in prompt.lower()


def test_planner_prompt_defaults_to_roadmap() -> None:
    prompt = build_planner_prompt({"planner_consult": {"objective": "x"}})
    assert "roadmap" in prompt.lower()


def test_materialize_prompt_locks_idempotent_rerun_protocol() -> None:
    """Lock the FIX-2 materialize re-run protocol so a refactor can't silently
    drop it: check-existing-via-get_board, idempotent fill, skip-if-complete."""
    prompt = build_planner_prompt({
        "planner_consult": {
            "mode": "materialize",
            "objective": "Author the scope",
            "workstream_id": "WS",
            "scope_id": "SC",
        },
    })
    low = prompt.lower()
    assert "re-run" in low  # explicit re-run awareness
    assert "get_board" in prompt  # check what already exists
    assert "idempotent" in low  # re-issue fills, never duplicates
    assert "brief_is_complete:false" in prompt  # correct empty-brief signal
    assert "skip it" in low  # don't re-touch complete tasks


async def test_ingest_planner_result_pokes_manager(monkeypatch) -> None:
    """A finished consult routes a [Planner] chat poke to the workstream."""
    from unittest.mock import AsyncMock, MagicMock

    import src.orchestrator._manager_action_requests as mar

    # Avoid config_store deps — stub the context envelope.
    monkeypatch.setattr(mar, "build_script_context_data", lambda c, k: {})

    controller = MagicMock()
    controller.handle_chat_message = AsyncMock()

    await mar.ingest_planner_result(
        controller,
        {"planner_consult": {
            "mode": "verify", "objective": "x",
            "workstream_id": "WS-1", "scope_id": "SC-1",
        }},
    )

    assert controller.handle_chat_message.await_count == 1
    sent = controller.handle_chat_message.await_args.args[0]
    assert sent["context_key"] == "workstream:WS-1"
    assert "[Planner]" in sent["user_message"]
    assert "verification" in sent["user_message"].lower()


async def _ingest(monkeypatch, message: dict) -> str:
    """Call ingest_planner_result with a stubbed controller; return the
    poked user_message."""
    from unittest.mock import AsyncMock, MagicMock

    import src.orchestrator._manager_action_requests as mar

    monkeypatch.setattr(mar, "build_script_context_data", lambda c, k: {})
    controller = MagicMock()
    controller.handle_chat_message = AsyncMock()
    await mar.ingest_planner_result(controller, message)
    assert controller.handle_chat_message.await_count == 1
    return controller.handle_chat_message.await_args.args[0]["user_message"]


async def test_ingest_planner_result_failure_poke(monkeypatch) -> None:
    """An explicit planner_error routes a FAILURE poke, not a success body."""
    body = await _ingest(monkeypatch, {
        "planner_consult": {"mode": "scope_plan", "workstream_id": "WS-1",
                            "scope_id": "SC-1"},
        "planner_error": "the Planner is already running another consult",
    })
    assert "[Planner]" in body
    assert "did not finish" in body.lower()
    assert "already running another consult" in body
    # It must NOT masquerade as the scope_plan success message.
    assert "skeleton execution plan" not in body.lower()
    # BUG A fix: a failed Planner-authoring consult must tell the Manager to
    # RE-CONSULT, not hand-author the work itself.
    assert "do not hand-author" in body.lower()
    assert "re-consult" in body.lower()


async def test_ingest_planner_result_blocked_status_is_failure(monkeypatch) -> None:
    """A terminal 'blocked' status (crash/escalation) → failure poke.
    For materialize specifically, the poke must say a re-run is SAFE/idempotent
    and forbid hand-authoring (BUG A + duplicate-task fix)."""
    body = await _ingest(monkeypatch, {
        "planner_consult": {"mode": "materialize", "workstream_id": "WS-1",
                            "scope_id": "SC-1"},
        "status": "blocked",
        "comment": "ESCALATED (timeout): ran out of budget",
    })
    assert "did not finish" in body.lower()
    assert "materialize" in body.lower()
    assert "idempotent" in body.lower()
    assert "do not hand-author" in body.lower()


async def test_ingest_planner_result_stall_cap_steers_away_from_reconsult(
    monkeypatch,
) -> None:
    """Incident 2026-06-23 (respawn loop): a repeated-stall cap
    (``planner_stall_cap``) must NOT tell the Manager to immediately
    re-consult — that re-spawned a fresh Planner and restarted the whole
    stall cycle. The body must say the consult is wedged, a cooldown is
    active, and steer toward splitting / escalating instead."""
    body = await _ingest(monkeypatch, {
        "planner_consult": {"mode": "scope_plan", "workstream_id": "WS-1",
                            "scope_id": "SC-1"},
        "planner_stall_cap": True,
        "planner_error": (
            "stalled with no result after ~30 min across 3 attempts "
            "(auto-restart cap reached)"
        ),
    })
    low = body.lower()
    assert "[planner]" in low
    assert "repeatedly stalled" in low
    assert "cooldown" in low
    assert "not a user cancel" in low
    # Must steer AWAY from an immediate re-consult (that was the loop).
    assert "do not immediately re-consult" in low
    assert "split" in low


async def test_ingest_planner_result_specify_failure_steers_to_approve(
    monkeypatch,
) -> None:
    """Incident 2026-06-23: a dropped/failed SPECIFY consult must not just say
    'nothing changed; re-consult' — a prior specify may already have produced
    the draft. The body must steer the Manager to get_spec and review+approve
    an existing draft, only re-consulting if no draft exists."""
    body = await _ingest(monkeypatch, {
        "planner_consult": {"mode": "specify", "workstream_id": "WS-1"},
        "planner_error": "the Planner is already running another consult",
    })
    low = body.lower()
    assert "did not finish" in low
    assert "get_spec" in low
    assert "approve_spec" in low
    # If a draft exists, do NOT re-consult — review it.
    assert "if a draft" in low or "draft exists" in low
    assert "do not hand-author" in low


async def test_ingest_planner_result_materialize_success(monkeypatch) -> None:
    """A clean materialize consult → 'authored the tasks' review poke."""
    body = await _ingest(monkeypatch, {
        "planner_consult": {"mode": "materialize", "workstream_id": "WS-1",
                            "scope_id": "SC-1"},
    })
    assert "[Planner]" in body
    assert "authored" in body.lower() and "activate_scope" in body


async def test_ingest_planner_result_specify_success(monkeypatch) -> None:
    """A clean specify consult → a SPEC poke that tells the Manager to check
    the spec status and branch (approve-gated vs auto-approved → roadmap).

    Regression guard: ``specify`` used to have NO branch and fell through to
    the ``research`` message ("finished research"), so the Manager never knew
    a spec was drafted and stalled. It must NOT masquerade as research.
    """
    body = await _ingest(monkeypatch, {
        "planner_consult": {"mode": "specify", "objective": "draft the spec",
                            "workstream_id": "WS-1"},
    })
    assert "[Planner]" in body
    low = body.lower()
    assert "spec" in low
    assert "get_spec" in body  # told to READ + review the actual spec
    assert "review" in low and "requirement" in low  # proactive review
    assert "approve_spec" in body  # manager signs off
    assert "consult_planner" in body and "roadmap" in low  # then proceed
    # Must NOT fall through to the research fallback message.
    assert "finished research" not in low

