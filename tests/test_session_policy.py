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
# Per-assignment override (inverted 2026-07-21): consult modes specify /
# roadmap / verify run at PLAIN xhigh BY DEFAULT (spawn tools disallowed,
# no ultracode settings — no dynamic-workflow spin-up). The execution-shaped
# modes (scope_plan / materialize / research) keep the agent's configured
# effort, and non-consult assignments pass through untouched.
# CBCL_CONSULT_ULTRACODE=1 opts the three plain-by-default modes back INTO
# the configured ultracode; CBCL_VERIFY_FORCE_PLAIN_EFFORT still forces
# verify plain even over that opt-in (redundant under the default, kept as
# the conservative override). EXCEPTION (verify turn-end incident
# 2026-07-17): the one-shot verdictless REFIRE (marker carries
# ``_verdictless_refire``) is ALWAYS plain xhigh regardless of the opt-in —
# the first attempt already proved the one-shot turn-end trap on this
# scope, and the retry is the last chance before the sweeper ladder, so it
# must verify inline (spawn tools disallowed) and reach
# ``complete_scope_verification``.
# ---------------------------------------------------------------------------


def test_verify_consult_plain_xhigh_by_default(monkeypatch) -> None:
    monkeypatch.delenv("CBCL_CONSULT_ULTRACODE", raising=False)
    monkeypatch.delenv("CBCL_VERIFY_FORCE_PLAIN_EFFORT", raising=False)
    cfg = agent_config_for_assignment(
        {"effort": "ultracode"}, {"planner_consult": {"mode": "verify"}},
    )
    assert cfg["effort"] == "xhigh"
    # Composed: the verify session works ALONE by default — no ultracode
    # settings, spawn tools disallowed (no dynamic-workflow spin-up).
    effort, settings_json, disallowed = build_session_policy(cfg, "opus")
    assert effort == "xhigh"
    assert settings_json is None
    assert "Agent" in disallowed
    assert "Task" in disallowed


def test_specify_and_roadmap_plain_xhigh_by_default(monkeypatch) -> None:
    monkeypatch.delenv("CBCL_CONSULT_ULTRACODE", raising=False)
    monkeypatch.delenv("CBCL_VERIFY_FORCE_PLAIN_EFFORT", raising=False)
    for mode in ("specify", "roadmap"):
        cfg = agent_config_for_assignment(
            {"effort": "ultracode"}, {"planner_consult": {"mode": mode}},
        )
        assert cfg["effort"] == "xhigh", mode
        effort, settings_json, disallowed = build_session_policy(cfg, "opus")
        assert effort == "xhigh", mode
        assert settings_json is None, mode
        assert "Agent" in disallowed, mode
        assert "Task" in disallowed, mode


def test_consult_ultracode_opt_in_restores_configured_effort(
    monkeypatch,
) -> None:
    """CBCL_CONSULT_ULTRACODE=1 opts specify/roadmap/verify back INTO the
    agent's configured ultracode (identity pass-through — no needless
    copy)."""
    monkeypatch.setenv("CBCL_CONSULT_ULTRACODE", "1")
    monkeypatch.delenv("CBCL_VERIFY_FORCE_PLAIN_EFFORT", raising=False)
    base = {"effort": "ultracode"}
    for mode in ("specify", "roadmap", "verify"):
        assert agent_config_for_assignment(
            base, {"planner_consult": {"mode": mode}},
        ) is base, mode
    # Composed: the opted-in session gets the FULL ultracode posture —
    # settings payload present, spawn tools allowed.
    cfg = agent_config_for_assignment(
        base, {"planner_consult": {"mode": "verify"}},
    )
    effort, settings_json, disallowed = build_session_policy(cfg, "opus")
    assert effort == "xhigh"
    assert json.loads(settings_json) == {"ultracode": True}
    assert "Agent" not in disallowed
    assert "Task" not in disallowed


def test_opt_in_falsy_values_keep_the_plain_default(monkeypatch) -> None:
    monkeypatch.delenv("CBCL_VERIFY_FORCE_PLAIN_EFFORT", raising=False)
    for value in ("0", "false", "no", "off", "", "  "):
        monkeypatch.setenv("CBCL_CONSULT_ULTRACODE", value)
        for mode in ("specify", "roadmap", "verify"):
            cfg = agent_config_for_assignment(
                {"effort": "ultracode"}, {"planner_consult": {"mode": mode}},
            )
            assert cfg["effort"] == "xhigh", (value, mode)


def test_verify_force_flag_wins_over_the_opt_in(monkeypatch) -> None:
    """CBCL_VERIFY_FORCE_PLAIN_EFFORT keeps working: redundant under the
    plain-by-default posture, but it forces verify plain even when
    CBCL_CONSULT_ULTRACODE opted verify back into ultracode."""
    monkeypatch.setenv("CBCL_CONSULT_ULTRACODE", "1")
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
    # The force flag is verify-shaped: specify/roadmap stay opted in.
    base = {"effort": "ultracode"}
    for mode in ("specify", "roadmap"):
        assert agent_config_for_assignment(
            base, {"planner_consult": {"mode": mode}},
        ) is base, mode


def test_execution_consult_modes_keep_configured_effort(monkeypatch) -> None:
    # scope_plan / materialize / research keep the agent's configured
    # effort whatever either env flag says.
    for opt_in in (None, "1"):
        if opt_in is None:
            monkeypatch.delenv("CBCL_CONSULT_ULTRACODE", raising=False)
        else:
            monkeypatch.setenv("CBCL_CONSULT_ULTRACODE", opt_in)
        for force in (None, "1"):
            if force is None:
                monkeypatch.delenv(
                    "CBCL_VERIFY_FORCE_PLAIN_EFFORT", raising=False
                )
            else:
                monkeypatch.setenv("CBCL_VERIFY_FORCE_PLAIN_EFFORT", force)
            for mode in ("scope_plan", "materialize", "research"):
                base = {"effort": "ultracode"}
                assert agent_config_for_assignment(
                    base, {"planner_consult": {"mode": mode}},
                ) is base, (opt_in, force, mode)


def test_non_consult_assignment_passes_through(monkeypatch) -> None:
    # Unaffected whatever either env flag says.
    base = {"effort": "ultracode"}
    for opt_in in (None, "1"):
        if opt_in is None:
            monkeypatch.delenv("CBCL_CONSULT_ULTRACODE", raising=False)
        else:
            monkeypatch.setenv("CBCL_CONSULT_ULTRACODE", opt_in)
        for force in (None, "1"):
            if force is None:
                monkeypatch.delenv(
                    "CBCL_VERIFY_FORCE_PLAIN_EFFORT", raising=False
                )
            else:
                monkeypatch.setenv("CBCL_VERIFY_FORCE_PLAIN_EFFORT", force)
            # No consult marker at all, and the legacy bare-truthy marker
            # shape: both untouched (identity — no needless copy).
            assert agent_config_for_assignment(
                base, {"task_id": "t1"},
            ) is base
            assert agent_config_for_assignment(
                base, {"planner_consult": True},
            ) is base


def test_consult_override_does_not_mutate_the_source_config(
    monkeypatch,
) -> None:
    monkeypatch.delenv("CBCL_CONSULT_ULTRACODE", raising=False)
    monkeypatch.delenv("CBCL_VERIFY_FORCE_PLAIN_EFFORT", raising=False)
    for mode in ("specify", "roadmap", "verify"):
        base = {"effort": "ultracode"}
        agent_config_for_assignment(base, {"planner_consult": {"mode": mode}})
        assert base["effort"] == "ultracode", mode


def test_verdictless_refire_auto_degrades_to_plain_xhigh(monkeypatch) -> None:
    """AREA-1 fix 2 (verify turn-end incident 2026-07-17): the one-shot
    verdictless REFIRE runs plain xhigh even when CBCL_CONSULT_ULTRACODE
    opted verify back into ultracode — the retry must survive the proven
    turn-end trap."""
    monkeypatch.setenv("CBCL_CONSULT_ULTRACODE", "1")
    monkeypatch.delenv("CBCL_VERIFY_FORCE_PLAIN_EFFORT", raising=False)
    cfg = agent_config_for_assignment(
        {"effort": "ultracode"},
        {"planner_consult": {"mode": "verify", "_verdictless_refire": True}},
    )
    assert cfg["effort"] == "xhigh"
    # Composed: the refired verify works ALONE — no ultracode settings,
    # spawn tools disallowed — so it can never yield on a live workflow.
    effort, settings_json, disallowed = build_session_policy(cfg, "opus")
    assert effort == "xhigh"
    assert settings_json is None
    assert "Agent" in disallowed
    assert "Task" in disallowed


def test_first_verify_attempt_keeps_ultracode_under_the_opt_in(
    monkeypatch,
) -> None:
    """Under CBCL_CONSULT_ULTRACODE the first verify attempt keeps the
    configured ultracode: only the refire (``_verdictless_refire``)
    degrades."""
    monkeypatch.setenv("CBCL_CONSULT_ULTRACODE", "1")
    monkeypatch.delenv("CBCL_VERIFY_FORCE_PLAIN_EFFORT", raising=False)
    base = {"effort": "ultracode"}
    assert agent_config_for_assignment(
        base, {"planner_consult": {"mode": "verify"}},
    ) is base


def test_refire_flag_on_execution_mode_does_not_degrade(monkeypatch) -> None:
    """The refire degrade is verify-shaped — an (impossible today) refire
    flag on an execution-shaped mode must not silently strip that mode's
    ultracode; the same holds for an opted-in roadmap."""
    monkeypatch.delenv("CBCL_VERIFY_FORCE_PLAIN_EFFORT", raising=False)
    monkeypatch.delenv("CBCL_CONSULT_ULTRACODE", raising=False)
    base = {"effort": "ultracode"}
    assert agent_config_for_assignment(
        base,
        {
            "planner_consult": {
                "mode": "scope_plan", "_verdictless_refire": True,
            },
        },
    ) is base
    monkeypatch.setenv("CBCL_CONSULT_ULTRACODE", "1")
    assert agent_config_for_assignment(
        base,
        {"planner_consult": {"mode": "roadmap", "_verdictless_refire": True}},
    ) is base


def test_monitor_is_scrubbed_from_every_session() -> None:
    """AREA-1 fix 5b (verify turn-end incident 2026-07-17): ``Monitor`` is
    the CLI's cross-turn background-task watcher — useless and a trap
    under one-shot ``--print`` (the InputValidationError in the incident
    proves models reach for it). It must ride the builtin scrub in BOTH
    postures, including ultracode where the spawn tools stay allowed."""
    assert "Monitor" in _CLAUDE_CLI_BUILTIN_DISALLOW
    for cfg in ({"effort": "ultracode"}, {"effort": "xhigh"}, {}):
        _effort, _settings, disallowed = build_session_policy(cfg, "opus")
        assert "Monitor" in disallowed, cfg


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
