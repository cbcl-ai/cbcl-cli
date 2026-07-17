"""Eval family: verify-verdict honesty pins (incident 2026-07-16).

The verify pipeline's "completed" signal is exit-code-shaped, so the only
thing standing between an ultracode-style clean exit and a verdictless
verify is the prompt contract. These pins lock the three load-bearing
rules in BOTH prompt layers (the per-consult session prompt and the
Planner's CLAUDE.md playbook):

  1. the verdict call is the LAST act of the MAIN session,
  2. it is NEVER delegated to a workflow subagent,
  3. a refused PASS is fixed and re-called — never left standing,
  4. (verify turn-end incident 2026-07-17) the session is ONE-SHOT — ending
     the turn kills pending background work, so the model must await
     in-turn (timeout-wrapped poll) instead of yielding to wait.

Each assertion targets a specific sentence; deleting that sentence from
the prompt fails the eval (mutation-checkable). The daemon-side backstops
(the post-verify honesty check + one-shot re-fire) are covered by
``tests/test_planner_verify_honesty.py``; the session-policy side (verify
keeps the Planner's configured ultracode by DEFAULT — these prompt pins
ARE the safety — with ``CBCL_VERIFY_FORCE_PLAIN_EFFORT`` as the operator
escape hatch to plain xhigh) by ``tests/test_session_policy.py``.
"""
from __future__ import annotations

from src.config_sync.claude_md_templates._system_agents._planner import (
    PLANNER_CLAUDE_MD,
)
from src.orchestrator.planner_prompt import build_planner_prompt


def _verify_prompt() -> str:
    return build_planner_prompt({
        "planner_consult": {
            "mode": "verify",
            "objective": "verify the auth scope",
            "workstream_id": "ws-1",
            "scope_id": "scope-1",
        },
    })


# ---------------------------------------------------------------------------
# Session prompt (planner_prompt.py verify-mode instructions)
# ---------------------------------------------------------------------------


def test_session_prompt_pins_verdict_is_last_act_of_main_session():
    prompt = _verify_prompt()
    assert "LAST act of YOUR main session" in prompt
    assert "MUST be made by YOU directly" in prompt


def test_session_prompt_pins_never_delegate_to_subagent():
    prompt = _verify_prompt()
    assert "NEVER delegate the verdict call to a workflow subagent" in prompt


def test_session_prompt_pins_verdictless_exit_is_a_failed_verify():
    prompt = _verify_prompt()
    assert (
        "a session that ends with no accepted verdict is a FAILED verify"
        in prompt
    )
    assert "re-run from scratch" in prompt


def test_session_prompt_pins_retry_on_refused_pass():
    prompt = _verify_prompt()
    assert "If a PASS is refused" in prompt
    assert "do not stop on a refused verdict" in prompt


def test_session_prompt_pins_one_shot_contract():
    """Verify turn-end incident (2026-07-17): `claude --print` exits at turn
    end and pending background workflows die with the process — the model
    must never yield to wait for a workflow. Pin the one-shot contract in
    the verify-mode session instructions."""
    prompt = _verify_prompt()
    assert "ONE-SHOT headless session" in prompt
    assert (
        "ending your turn EXITS the process and KILLS any still-running "
        "workflow subagents or background tasks" in prompt
    )
    assert "Background work will NEVER re-invoke you" in prompt
    assert "NEVER end your turn to wait" in prompt


def test_session_prompt_pins_sanctioned_wait_pattern():
    """The sanctioned in-turn wait must be spelled out: a timeout-wrapped
    poll loop (the bash guard's _TIMEOUT_OK allows timeout-prefixed
    until-loops) — without it the model has no legal way to await a
    spawned workflow."""
    prompt = _verify_prompt()
    assert "until <check>; do sleep 15; done" in prompt
    assert "timeout-prefixed" in prompt
    assert "size the work to complete synchronously" in prompt


def test_session_prompt_pins_fanout_sizing():
    """Long-verify incident (2026-07-16 follow-up): office containers are
    CPU-capped, so workflow subagents serialize — verify-mode instructions
    must carry the sizing guidance (direct checks for small scopes; capped
    fan-out when a workflow is used) while the verdict rules stay intact."""
    prompt = _verify_prompt()
    assert "read + judge" in prompt
    assert "≤5 tasks" in prompt
    assert "DIRECT evidence checks" in prompt
    assert "≤4 concurrent verification subagents" in prompt
    assert "CPU-capped" in prompt
    # The mandatory-verdict rules are UNCHANGED by the sizing guidance.
    assert "LAST act of YOUR main session" in prompt


def test_hard_rules_only_in_verify_mode():
    """The verdict hard rules belong to verify mode alone — a roadmap
    consult must not be told to call complete_scope_verification."""
    roadmap_prompt = build_planner_prompt({
        "planner_consult": {
            "mode": "roadmap", "objective": "o", "workstream_id": "ws-1",
        },
    })
    assert "NEVER delegate the verdict call" not in roadmap_prompt


def test_fanout_sizing_only_in_verify_mode():
    """The fan-out cap is verification sizing — a materialize consult
    (which legitimately authors many tasks) must not inherit it."""
    materialize_prompt = build_planner_prompt({
        "planner_consult": {
            "mode": "materialize", "objective": "o",
            "workstream_id": "ws-1", "scope_id": "scope-1",
        },
    })
    assert "concurrent verification subagents" not in materialize_prompt


# ---------------------------------------------------------------------------
# Playbook (PLANNER_CLAUDE_MD — Verify-mode section + Completion list)
# ---------------------------------------------------------------------------

# The playbook hard-wraps at ~78 cols, so pin against a whitespace-
# normalised view (same sentence, independent of where the wrap falls).
_PLAYBOOK_NORM = " ".join(PLANNER_CLAUDE_MD.split())


def test_playbook_pins_verdict_is_last_act():
    assert "The verdict call is the LAST act of YOUR main session" in (
        _PLAYBOOK_NORM
    )


def test_playbook_pins_never_delegate_to_subagent():
    assert (
        "NEVER delegate the verdict call to a workflow subagent"
        in _PLAYBOOK_NORM
    )


def test_playbook_pins_retry_on_refused_pass():
    assert "If a PASS is refused" in _PLAYBOOK_NORM
    assert "do not stop on a refused verdict" in _PLAYBOOK_NORM


def test_playbook_completion_list_pins_the_last_act_shape():
    assert "a refused PASS is fixed and re-called" in _PLAYBOOK_NORM
    assert "ending with no accepted verdict = a FAILED verify" in (
        _PLAYBOOK_NORM
    )


def test_playbook_pins_fanout_sizing():
    """Mirror of the session-prompt sizing pin — the playbook's Verify-mode
    section carries the same read+judge / capped-fan-out guidance."""
    assert "Verification is read + judge, not build" in _PLAYBOOK_NORM
    assert "≤5 tasks" in _PLAYBOOK_NORM
    assert "≤4 concurrent verification subagents" in _PLAYBOOK_NORM
    assert "CPU-capped" in _PLAYBOOK_NORM
    assert "the verdict rules below are unchanged" in _PLAYBOOK_NORM


def test_playbook_pins_one_shot_contract():
    """Mirror of the session-prompt one-shot pin — the playbook's Verify-mode
    section (2d) states the turn-end contract and the sanctioned wait."""
    assert "ONE-SHOT headless session" in _PLAYBOOK_NORM
    assert (
        "ending your turn EXITS the process and KILLS any still-running "
        "workflow subagents or background tasks" in _PLAYBOOK_NORM
    )
    assert "Background work will NEVER re-invoke you" in _PLAYBOOK_NORM
    assert "NEVER end your turn to wait" in _PLAYBOOK_NORM
    assert "until <check>; do sleep 15; done" in _PLAYBOOK_NORM
    assert "timeout-prefixed waits" in _PLAYBOOK_NORM


# ---------------------------------------------------------------------------
# Manager playbook — stuck-verifying recovery recipe (verify turn-end
# incident 2026-07-17: the escalation card used to lead to a dead end — the
# Manager had no chip-flip surface and no recovery guidance)
# ---------------------------------------------------------------------------


def test_manager_playbook_pins_stuck_verifying_recovery():
    """The Manager playbook must carry the escalated-scope recovery recipe:
    re-consult verify first; a human-verified manual close (chip-flip via
    update_execution_plan + complete_scope_verification) only as the LAST
    resort — and never a rubber-stamp."""
    from src.config_sync.claude_md_content import MANAGER_CLAUDE_MD

    norm = " ".join(MANAGER_CLAUDE_MD.split())
    assert "Scope stuck in `verifying` (escalated)" in norm
    assert "Re-consult verify" in norm
    assert "Human-verified manual close — the LAST resort" in norm
    assert "PERSONALLY evidence-check each remaining chip" in norm
    assert "`update_execution_plan`" in norm
    assert 'verified_by="manager"' in norm
    assert "NEVER mark a chip done without checking it" in norm
    assert "a rubber-stamp defeats the verification gate" in norm


def test_shared_worker_rules_pin_one_shot_contract():
    """Every ultracode-capable worker inherits the one-shot contract via the
    shared LONG_RUNNING_BASH_RULE fragment (which SHARED_AGENT_WORK_RULES,
    PLANNER_WORK_RULES, and the MA playbook all append) — a workflow-spawning
    custom agent must get the same 'never yield to wait' rule the Planner
    does."""
    from src.config_sync.claude_md_templates._shared_agent import (
        LONG_RUNNING_BASH_RULE,
        SHARED_AGENT_WORK_RULES,
    )

    for text in (LONG_RUNNING_BASH_RULE, SHARED_AGENT_WORK_RULES):
        norm = " ".join(text.split())
        assert "ONE-SHOT headless session" in norm
        assert "Background work will NEVER re-invoke you" in norm
        assert "NEVER end your turn to wait" in norm
        assert "until <check>; do sleep 15; done" in norm
        assert "timeout-prefixed waits" in norm
