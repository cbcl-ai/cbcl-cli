"""Session policy: reasoning-effort + ``ultracode`` (dynamic-workflow)
orchestration for a worker's ``claude --print`` invocation.

Pure decision logic (no IO) so it is unit-testable without Docker. The
session_bridge applies the returned flags to the docker-exec command line;
``_agent_worker_task`` threads them through its retry loop and degrades
gracefully if an older CLI build doesn't recognise them.

## The orchestration model (Claude Code, as of v2.1.154+)

There is exactly ONE orchestration knob: the agent's ``effort`` value.

* ``effort == "ultracode"`` — Claude Code's ``ultracode`` setting: it sends
  ``xhigh`` to the model AND lets Claude orchestrate **dynamic workflows**
  (autonomously fanning work out to many parallel sub-agents in the
  background, folding only the final answer back). Headless, this is enabled
  via ``--settings '{"ultracode": true}'`` (there is NO ``--ultracode`` flag,
  and ``--effort`` does NOT accept the literal ``ultracode`` value). We pass
  the ultracode setting AND ``--effort xhigh`` together — the documented
  headless recipe. Passing both is deliberate belt-and-suspenders: if an
  OLDER container CLI accepts ``--settings`` but silently ignores the unknown
  ``ultracode`` key (no error → the worker degrade path can't see it), the
  ``--effort xhigh`` still lands, so the agent degrades to
  xhigh-without-workflows instead of silently dropping to DEFAULT effort. On
  a current CLI both target xhigh (consistent) and dynamic workflows turn on.
  The sub-agent spawn tools (``Task``/``Agent``) are left ALLOWED so the
  workflow runtime can orchestrate.
* ``effort in {low, medium, high, xhigh, max}`` — a plain reasoning-effort
  level via ``--effort`` (opus-tier only). The agent **works alone**: the
  sub-agent spawn tools are DISALLOWED.
* ``effort`` unset / non-opus — CLI default effort; works alone.

The static ``--agents`` "Helpers" mechanism was removed — dynamic workflows
(ultracode) are the single, model-driven orchestration path. The Manager is
NEVER given ultracode and additionally runs with ``CLAUDE_CODE_DISABLE_WORKFLOWS=1``
+ ``Task``/``Agent``/``Bash`` disallowed (sole-orchestrator invariant; see
``_agent_worker_manager.py``).
"""
from __future__ import annotations

import json
import os

from src._agent_worker_mcp import _CLAUDE_CLI_BUILTIN_DISALLOW
from src.orchestrator._model_defaults import is_opus_tier

# The ``ultracode`` sentinel — stored in the agent's ``effort`` field, it
# selects "xhigh + dynamic workflows" rather than a raw effort level.
ULTRACODE = "ultracode"

# SES-05: the reasoning-effort the Manager session runs at. The Manager is the
# highest-leverage reasoning surface in the office, so it is pinned to xhigh
# explicitly (opus-tier) rather than drifting with the container CLI's default.
# It is NEVER ultracode — the Manager is hard-blocked from dynamic workflows
# (sole-orchestrator invariant: CLAUDE_CODE_DISABLE_WORKFLOWS=1 + the sub-agent
# spawn tools disallowed in every case).
DEFAULT_OPUS_EFFORT = "xhigh"

# Both names for the native sub-agent spawn tool: ``Task`` (legacy) and
# ``Agent`` (renamed in Claude CLI v2.1.63). Disallow BOTH for a worker that
# is NOT in ultracode mode so it works alone. (The Manager disallows these in
# every case — see ``_agent_worker_manager.py``.)
_SUBAGENT_TOOLS = ("Task", "Agent")

# Headless ``--settings`` payload that turns on ``ultracode`` (xhigh + dynamic
# workflows). Mirrors the docs: pass ``{"ultracode": true}`` via ``--settings``.
_ULTRACODE_SETTINGS = json.dumps({"ultracode": True})


def build_session_policy(
    agent_config: dict, model: str,
) -> tuple[str | None, str | None, list[str]]:
    """Return ``(effort, settings_json, disallowed_tools)`` for a worker session.

    * ``effort`` — the CLI ``--effort`` value, or ``None`` to use the CLI
      default. Applied ONLY on the opus tier (defense-in-depth behind the
      backend's opus-tier validation). For ``ultracode`` this is ``"xhigh"``
      (the literal ``ultracode`` is NOT a valid ``--effort`` value; xhigh is
      its underlying level and is sent alongside the ultracode setting).
    * ``settings_json`` — JSON for ``--settings`` (``{"ultracode": true}``)
      when the agent's effort is ``ultracode``, else ``None``.
    * ``disallowed_tools`` — the base builtin scrub, PLUS the sub-agent spawn
      tools when the agent is NOT in ultracode mode (so it works alone).
    """
    raw_effort = agent_config.get("effort")
    opus = is_opus_tier(model)

    # Ultracode: xhigh + dynamic workflows. Opus-tier only (defense-in-depth;
    # the backend already validates this). Pass BOTH --effort xhigh AND the
    # ultracode setting (the documented headless recipe) so an older CLI that
    # ignores the unknown ultracode key still gets xhigh, not default effort.
    # Allow the sub-agent spawn tools so the workflow runtime can orchestrate.
    if raw_effort == ULTRACODE and opus:
        return "xhigh", _ULTRACODE_SETTINGS, list(_CLAUDE_CLI_BUILTIN_DISALLOW)

    # Plain effort level (opus-tier only) — or no orchestration at all. The
    # worker works alone: disallow the sub-agent spawn tools.
    effort = raw_effort if (raw_effort and raw_effort != ULTRACODE and opus) else None
    disallowed = [*_CLAUDE_CLI_BUILTIN_DISALLOW, *_SUBAGENT_TOOLS]
    return effort, None, disallowed


def _verify_force_plain_effort() -> bool:
    """Truthiness of the ``CBCL_VERIFY_FORCE_PLAIN_EFFORT`` escape hatch."""
    raw = (os.environ.get("CBCL_VERIFY_FORCE_PLAIN_EFFORT") or "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def agent_config_for_assignment(agent_config: dict, task_data: dict) -> dict:
    """Per-assignment override of the agent's orchestration config.

    DEFAULT POSTURE (2026-07-17 user decision): dynamic workflows /
    subagents (``effort="ultracode"``) stay ENABLED for every agent
    session, INCLUDING Planner VERIFY consults — the agent's configured
    effort passes through untouched. Verdict safety comes from the
    verdictless-exit honesty check + one-shot re-fire (``handlers.py``) and
    the prompt pins (verdict is the main session's own LAST act, never
    delegated to a workflow subagent — ``tests/evals/
    test_planner_verify_pins.py``), NOT from downgrading effort.

    ``CBCL_VERIFY_FORCE_PLAIN_EFFORT`` (env, default OFF) is the operator
    escape hatch for the conservative mode: when truthy, ``mode=verify``
    consults are forced to plain ``xhigh`` — ``build_session_policy`` then
    disallows the ``Task``/``Agent`` spawn tools and drops the ultracode
    settings, so the verify session works alone (the pre-2026-07-17
    incident posture). Every other consult mode (roadmap / scope_plan /
    materialize / research) and every non-consult assignment passes
    through untouched regardless of the flag.

    AUTO-DEGRADE ON THE VERDICTLESS REFIRE (verify turn-end incident
    2026-07-17): a verify consult whose marker carries
    ``_verdictless_refire`` is the ONE-SHOT retry of a verify session
    that already ended without a verdict — the proven one-shot turn-end
    trap (``fable/specs/verify-turnend/00-research.md``): under
    ``claude --print`` the process exits the moment the model ends its
    turn, and any still-running workflow subagents die with it, so an
    ultracode verify that spawns a workflow and yields to "wait" can
    NEVER record its verdict. The FIRST attempt keeps the configured
    ultracode (the 2026-07-17 posture is unchanged — most verifies
    complete inline), but the retry is the last chance before the
    sweeper/escalation ladder, so it MUST survive: force plain
    ``xhigh`` (spawn tools disallowed, no ultracode settings) so the
    refired session verifies inline and can reach
    ``complete_scope_verification``.
    """
    consult = task_data.get("planner_consult")
    if not isinstance(consult, dict):
        return agent_config
    if (consult.get("mode") or "").strip() != "verify":
        return agent_config
    if _verify_force_plain_effort() or consult.get("_verdictless_refire"):
        return {**agent_config, "effort": DEFAULT_OPUS_EFFORT}
    return agent_config


_UNKNOWN_FLAG_MARKERS = (
    "unknown option",
    "unrecognized",
    "unknown argument",
    "unexpected argument",
    "invalid option",
    "no such option",
    "unknown flag",
    "unknown setting",
    "invalid setting",
)


def is_unknown_flag_error(error_text: str | None) -> bool:
    """True when a CLI error looks like it rejected ``--effort`` / ``--settings``
    (ultracode) on an older container CLI build.

    Used by the worker retry loop to drop the orchestration flags and retry on
    an older CLI that doesn't support them — so a flag-support gap can never
    block a task. ``ultracode`` itself is matched too in case the CLI rejects
    the unknown settings key by name.
    """
    t = (error_text or "").lower()
    if "--effort" not in t and "--settings" not in t and "ultracode" not in t:
        return False
    return any(marker in t for marker in _UNKNOWN_FLAG_MARKERS)
