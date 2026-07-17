"""Item-6: unit tests for the session-policy decision logic — reasoning-effort
+ ``ultracode`` (dynamic-workflow) orchestration. Pure functions, no Docker.

Contract: ``build_session_policy`` returns ``(effort, settings_json,
disallowed_tools)``. The single orchestration knob is the agent's ``effort``:
``ultracode`` => dynamic workflows (``--settings '{"ultracode": true}'``,
sub-agent tools allowed, no ``--effort``); a raw level => ``--effort`` +
work-alone (sub-agent tools disallowed); else CLI-default + work-alone.
"""
import json

from src._agent_worker_mcp import _CLAUDE_CLI_BUILTIN_DISALLOW
from src._session_policy import (
    agent_config_for_assignment,
    build_session_policy,
    is_unknown_flag_error,
)


def test_ultracode_on_opus_sets_settings_and_xhigh() -> None:
    effort, settings_json, disallowed = build_session_policy(
        {"effort": "ultracode"}, "opus"
    )
    # Documented recipe: --settings '{"ultracode": true}' AND --effort xhigh.
    # The explicit xhigh is the belt-and-suspenders so an older CLI that
    # ignores the ultracode key still lands xhigh (not default effort).
    assert effort == "xhigh"
    assert settings_json is not None
    assert json.loads(settings_json) == {"ultracode": True}
    # Sub-agent tools are AVAILABLE so the workflow runtime can orchestrate.
    assert "Agent" not in disallowed
    assert "Task" not in disallowed
    # The builtin scrub still applies.
    assert "TaskCreate" in disallowed


def test_plain_effort_disallows_subagents() -> None:
    effort, settings_json, disallowed = build_session_policy(
        {"effort": "xhigh"}, "opus"
    )
    assert effort == "xhigh"
    assert settings_json is None
    # Work-alone: both names of the spawn tool are blocked.
    assert "Agent" in disallowed
    assert "Task" in disallowed
    for tool in _CLAUDE_CLI_BUILTIN_DISALLOW:
        assert tool in disallowed


def test_no_effort_works_alone() -> None:
    effort, settings_json, disallowed = build_session_policy(
        {"effort": None}, "opus"
    )
    assert effort is None
    assert settings_json is None
    assert "Agent" in disallowed
    assert "Task" in disallowed


def test_effort_ignored_on_non_opus() -> None:
    effort, settings_json, disallowed = build_session_policy(
        {"effort": "xhigh"}, "sonnet"
    )
    assert effort is None  # effort is opus-tier only
    assert settings_json is None
    assert "Agent" in disallowed


def test_ultracode_ignored_on_non_opus() -> None:
    # ultracode (= xhigh + workflows) is opus-tier only; on a non-opus model
    # it degrades to work-alone with no settings. (The backend rejects this
    # config at create/update time; this is daemon-side defense-in-depth.)
    effort, settings_json, disallowed = build_session_policy(
        {"effort": "ultracode"}, "sonnet"
    )
    assert effort is None
    assert settings_json is None
    assert "Agent" in disallowed
    assert "Task" in disallowed


def test_effort_on_opus_dated_pin() -> None:
    effort, settings_json, _dis = build_session_policy(
        {"effort": "max"}, "claude-opus-4-8"
    )
    assert effort == "max"
    assert settings_json is None


def test_ultracode_on_opus_dated_pin() -> None:
    effort, settings_json, disallowed = build_session_policy(
        {"effort": "ultracode"}, "claude-opus-4-8"
    )
    assert effort == "xhigh"
    assert settings_json is not None
    assert "Agent" not in disallowed


def test_defaults_when_keys_absent() -> None:
    effort, settings_json, disallowed = build_session_policy({}, "opus")
    assert effort is None
    assert settings_json is None
    assert "Agent" in disallowed


# ---------------------------------------------------------------------------
# Per-assignment override: by DEFAULT verify consults keep the agent's
# configured effort — ultracode included (2026-07-17 user decision: verdict
# safety comes from the verdictless-exit honesty check + prompt pins, not
# from downgrading effort). CBCL_VERIFY_FORCE_PLAIN_EFFORT is the operator
# escape hatch that restores the conservative plain-xhigh verify posture.
# ---------------------------------------------------------------------------


def test_verify_consult_preserves_ultracode_by_default(monkeypatch) -> None:
    monkeypatch.delenv("CBCL_VERIFY_FORCE_PLAIN_EFFORT", raising=False)
    cfg = agent_config_for_assignment(
        {"effort": "ultracode"}, {"planner_consult": {"mode": "verify"}},
    )
    assert cfg["effort"] == "ultracode"
    # Pass-through is the identity — no needless copy.
    base = {"effort": "ultracode"}
    assert agent_config_for_assignment(
        base, {"planner_consult": {"mode": "verify"}},
    ) is base
    # Composed: the verify session gets the FULL ultracode posture —
    # settings payload present, spawn tools allowed.
    effort, settings_json, disallowed = build_session_policy(cfg, "opus")
    assert effort == "xhigh"
    assert json.loads(settings_json) == {"ultracode": True}
    assert "Agent" not in disallowed
    assert "Task" not in disallowed


def test_flag_on_forces_plain_xhigh_for_verify(monkeypatch) -> None:
    monkeypatch.setenv("CBCL_VERIFY_FORCE_PLAIN_EFFORT", "1")
    cfg = agent_config_for_assignment(
        {"effort": "ultracode"}, {"planner_consult": {"mode": "verify"}},
    )
    assert cfg["effort"] == "xhigh"
    # Composed: the verify session works ALONE — no ultracode settings,
    # spawn tools disallowed (the conservative escape-hatch posture).
    effort, settings_json, disallowed = build_session_policy(cfg, "opus")
    assert effort == "xhigh"
    assert settings_json is None
    assert "Agent" in disallowed
    assert "Task" in disallowed


def test_flag_falsy_values_keep_the_default(monkeypatch) -> None:
    for value in ("0", "false", "no", "off", "", "  "):
        monkeypatch.setenv("CBCL_VERIFY_FORCE_PLAIN_EFFORT", value)
        cfg = agent_config_for_assignment(
            {"effort": "ultracode"}, {"planner_consult": {"mode": "verify"}},
        )
        assert cfg["effort"] == "ultracode", value


def test_non_verify_consults_keep_ultracode(monkeypatch) -> None:
    # Unaffected whether the escape hatch is off OR on.
    for flag in (None, "1"):
        if flag is None:
            monkeypatch.delenv("CBCL_VERIFY_FORCE_PLAIN_EFFORT", raising=False)
        else:
            monkeypatch.setenv("CBCL_VERIFY_FORCE_PLAIN_EFFORT", flag)
        for mode in (
            "specify", "roadmap", "scope_plan", "materialize", "research",
        ):
            cfg = agent_config_for_assignment(
                {"effort": "ultracode"}, {"planner_consult": {"mode": mode}},
            )
            assert cfg["effort"] == "ultracode", (flag, mode)


def test_non_consult_assignment_passes_through(monkeypatch) -> None:
    # Unaffected whether the escape hatch is off OR on.
    base = {"effort": "ultracode"}
    for flag in (None, "1"):
        if flag is None:
            monkeypatch.delenv("CBCL_VERIFY_FORCE_PLAIN_EFFORT", raising=False)
        else:
            monkeypatch.setenv("CBCL_VERIFY_FORCE_PLAIN_EFFORT", flag)
        # No consult marker at all, and the legacy bare-truthy marker shape:
        # both untouched (identity — no needless copy).
        assert agent_config_for_assignment(base, {"task_id": "t1"}) is base
        assert agent_config_for_assignment(
            base, {"planner_consult": True},
        ) is base


def test_verify_override_does_not_mutate_the_source_config(monkeypatch) -> None:
    monkeypatch.setenv("CBCL_VERIFY_FORCE_PLAIN_EFFORT", "1")
    base = {"effort": "ultracode"}
    agent_config_for_assignment(base, {"planner_consult": {"mode": "verify"}})
    assert base["effort"] == "ultracode"


def test_is_unknown_flag_error() -> None:
    assert is_unknown_flag_error("error: unknown option '--effort'")
    assert is_unknown_flag_error("unrecognized arguments: --settings")
    assert is_unknown_flag_error("invalid option --effort")
    assert is_unknown_flag_error("unknown setting 'ultracode'")
    # Not our flags / not a flag error:
    assert not is_unknown_flag_error("API Error: overloaded")
    assert not is_unknown_flag_error("unknown option '--foo'")
    assert not is_unknown_flag_error("--effort was set fine, but rate limited")
    assert not is_unknown_flag_error(None)
    assert not is_unknown_flag_error("")
