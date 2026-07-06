"""FX-24.T01 — Planner→Manager poke must run in a resolvable workstream context.

Regression coverage for prod bug "B" (and, downstream, "A"): a Planner consult
poke that ran with an empty/degraded context_data made ``manager_context``
render "Workstream: Unknown / UUID: (empty)", stripping the ``workstream_id`` the
Manager needs to pass to ``get_spec`` / ``approve_spec`` — so the Manager was
stranded in an unbound turn and punted spec approval to the user even in
manager-approval mode.

The fix: ``build_script_context_data`` binds by ``workstream_id`` at minimum even
when the daemon ConfigStore hasn't synced the (often brand-new) workstream yet,
instead of collapsing to ``{}``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import src.orchestrator._manager_action_requests as mar
from src.orchestrator.manager_context import build_dynamic_context


def _controller(get_workstream_return=None, scopes_return=None) -> MagicMock:
    controller = MagicMock()
    controller._config.get_workstream = MagicMock(
        return_value=get_workstream_return
    )
    controller._config.get_scopes_for_workstream = MagicMock(
        return_value=scopes_return
    )
    return controller


class TestBuildScriptContextData:
    def test_missing_workstream_still_binds_workstream_id(self):
        """The core fix: a ConfigStore miss must NOT drop the binding."""
        ctrl = _controller(get_workstream_return=None)
        ws_id = "11111111-1111-1111-1111-111111111111"
        data = mar.build_script_context_data(ctrl, f"workstream:{ws_id}")
        assert data == {"workstream_id": ws_id}, (
            "a missing workstream must still bind workstream_id so the "
            "Manager poke turn isn't stranded as 'Workstream: Unknown'"
        )

    def test_general_chat_returns_empty(self):
        ctrl = _controller(get_workstream_return=None)
        assert mar.build_script_context_data(ctrl, "general_chat") == {}

    def test_non_workstream_context_key_returns_empty(self):
        ctrl = _controller(get_workstream_return=None)
        assert mar.build_script_context_data(ctrl, "something-else") == {}

    def test_empty_workstream_id_returns_empty(self):
        """``workstream:`` with no id is not a real binding → no degraded
        'UUID: ``' header."""
        ctrl = _controller(get_workstream_return=None)
        assert mar.build_script_context_data(ctrl, "workstream:") == {}
        assert mar.build_script_context_data(ctrl, "workstream:   ") == {}

    def test_present_workstream_returns_full_envelope(self):
        ws_id = "22222222-2222-2222-2222-222222222222"
        ws = {
            "id": ws_id,
            "name": "Recruitment",
            "description": "All recruitment",
            "goals": "Hire 10 engineers",
            "priority": "high",
        }
        ctrl = _controller(get_workstream_return=ws, scopes_return=None)
        data = mar.build_script_context_data(ctrl, f"workstream:{ws_id}")
        assert data["workstream_id"] == ws_id
        assert data["workstream_name"] == "Recruitment"
        assert data["workstream_priority"] == "high"

    def test_lookup_exception_still_binds_workstream_id(self):
        """A raising get_workstream must degrade to the bound envelope, not
        crash and not collapse to {}."""
        ctrl = MagicMock()
        ctrl._config.get_workstream = MagicMock(side_effect=RuntimeError("boom"))
        ws_id = "33333333-3333-3333-3333-333333333333"
        data = mar.build_script_context_data(ctrl, f"workstream:{ws_id}")
        assert data == {"workstream_id": ws_id}


class _Store:
    """Minimal ConfigStore stand-in for build_dynamic_context."""

    def get_workstream_list(self):
        return []

    def get_team_roster(self):
        return ""


@pytest.mark.asyncio
async def test_specify_poke_binds_workstream_and_self_approves_on_configstore_miss():
    """FX-24.T01 + T02 end-to-end (the exact prod bug A/B turn): a specify
    success poke for a workstream the daemon ConfigStore hasn't synced yet
    still BINDS workstream_id (so the Manager can get_spec/approve_spec) AND
    tells the Manager to approve it ITSELF, not defer to the user."""
    ws_id = "44444444-4444-4444-4444-444444444444"
    controller = MagicMock()
    # ConfigStore miss — the brand-new-workstream case that triggered prod bug B.
    controller._config.get_workstream = MagicMock(return_value=None)
    controller.handle_chat_message = AsyncMock(return_value=True)

    await mar.ingest_planner_result(
        controller,
        {
            "planner_consult": {
                "mode": "specify", "workstream_id": ws_id, "scope_id": "",
            },
            "task_id": "planner-abc123",
        },
    )

    controller.handle_chat_message.assert_awaited_once()
    msg = controller.handle_chat_message.await_args.args[0]

    # T01: the poke turn binds the workstream id even on a ConfigStore miss.
    assert msg["context_key"] == f"workstream:{ws_id}"
    assert msg["context_data"] == {"workstream_id": ws_id}
    # T02: the poke (the bug-trigger turn) tells the Manager to self-approve.
    assert "Do NOT ask the user to approve it" in msg["user_message"]

    # T01 acceptance: feeding that context_data through the prompt builder
    # renders the real UUID in the header (NOT a degraded empty 'UUID: ``').
    rendered = build_dynamic_context(
        msg["context_key"], msg["context_data"], _Store(),
    )
    assert ws_id in rendered
