"""Pins for the per-turn Manager dynamic context (``build_dynamic_context``).

These guard the contract gaps where the context block dropped an id the tool
surface strictly requires, forcing the Manager into a failed call or a wasted
lookup.
"""
from __future__ import annotations

from src.config_sync.sync_service import ConfigStore
from src.orchestrator.manager_context import build_dynamic_context

_WS = "workstream:11111111-1111-1111-1111-111111111111"


def _ctx(**over) -> dict:
    base = {
        "workstream_id": "11111111-1111-1111-1111-111111111111",
        "workstream_name": "Recruitment",
        "workstream_priority": "high",
        "workstream_description": "",
        "workstream_goals": "",
        "team_roster": "**MA** (manager-assistant) — ⚡",
        "board_summary": {},
        "scopes": [],
    }
    base.update(over)
    return base


def test_scopes_block_carries_scope_uuid():
    # MGR-03: every scope tool requires the scope UUID; the block must render it
    # so the Manager can act without a get_scope/list_scopes round-trip.
    scope_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    out = build_dynamic_context(
        _WS,
        _ctx(
            scopes=[
                {
                    "id": scope_uuid,
                    "readable_id": "RC-001.S01",
                    "short_key": "Auth",
                    "name": "Auth epic",
                    "state": "executing",
                }
            ]
        ),
        ConfigStore(),
        True,
    )
    assert "## Scopes" in out
    assert scope_uuid in out, "scope UUID (scope_id) must appear in the scopes block"
    assert "RC-001.S01" in out


def test_workstream_block_carries_workstream_uuid():
    # Parity: the workstream block already renders its UUID (create_task needs
    # it). Guard it so the two id-carrying blocks stay consistent.
    out = build_dynamic_context(_WS, _ctx(), ConfigStore(), True)
    assert "11111111-1111-1111-1111-111111111111" in out


def test_board_summary_renders_a_markdown_line_not_a_dict_repr():
    # MGR-09: the backend carries task_summary as a status->count dict;
    # f-stringing it leaked a raw Python dict repr into the prompt. It must
    # render as a compact single line.
    out = build_dynamic_context(
        _WS,
        _ctx(task_summary={
            "backlog": 2, "ready": 1, "in_progress": 3,
            "blocked": 0, "review": 1, "done": 7,
        }),
        ConfigStore(),
        True,
    )
    assert "## Board Summary" in out
    assert "Backlog 2 · Ready 1 · In-progress 3" in out
    assert "{'backlog'" not in out, "must not leak a raw Python dict repr"


def test_spec_approval_mode_surfaced_unconditionally():
    # MGR-10: the Manager must know its spec-approval mode every turn, even
    # before a spec exists.
    mgr = build_dynamic_context(
        _WS, _ctx(spec_approval="manager"), ConfigStore(), True
    )
    assert "Spec approval: **manager**" in mgr
    assert "approve_spec" in mgr

    usr = build_dynamic_context(
        _WS, _ctx(spec_approval="user"), ConfigStore(), True
    )
    assert "Spec approval: **user**" in usr
    assert "must\nNOT call `approve_spec`" in usr or "must NOT call" in usr.replace("\n", " ")


def test_spec_approval_absent_key_is_fail_safe_not_user_mode():
    # MGR-10 follow-up: daemon-originated poke turns can lack spec_approval
    # entirely (ConfigStore lag / older backend). The absent key must NOT
    # assert user mode — the old ``or "user"`` default rendered "you must
    # NOT call `approve_spec`" on the exact specify-done poke instructing
    # the Manager to approve. Unknown → neutral fail-safe line only.
    out = build_dynamic_context(_WS, _ctx(), ConfigStore(), True)
    assert "must NOT call `approve_spec`" not in out
    assert "Spec approval: **user**" not in out
    assert "Spec approval mode: unknown" in out
    # The fail-safe still points at the real gate instead of prohibiting.
    assert "attempt `approve_spec`" in out


def test_pending_manager_decisions_surfaced_when_present():
    # MGR-10: pending auto-decide requests appear in the header so they don't
    # age out unseen; absent when zero.
    with_pending = build_dynamic_context(
        _WS,
        _ctx(pending_manager_decisions={"count": 3, "types": ["create_task"]}),
        ConfigStore(),
        True,
    )
    assert "3 pending action request" in with_pending
    assert "decide_action_request" in with_pending

    none_pending = build_dynamic_context(
        _WS, _ctx(pending_manager_decisions={"count": 0}), ConfigStore(), True
    )
    assert "pending action request" not in none_pending


def test_output_style_value_not_reinjected_into_dynamic_context():
    # MGR-09: the office Output Style VALUE lives in the office CLAUDE.md now;
    # the per-turn dynamic context must NOT deliver a second copy.
    out = build_dynamic_context(
        _WS,
        _ctx(output_style="Always answer in haiku."),
        ConfigStore(),
        True,
    )
    assert "Always answer in haiku." not in out
    assert "<output_style>" not in out
