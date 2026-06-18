"""Single source of truth for the communicator's fallback model IDs.

These constants mirror the curated entries in
``backend/app/ai_models/defaults.py``. The communicator subprocess can't
import the backend module (different package roots) so we keep a small
parallel constant set here. Update both files together when the
catalog moves.

Use:
    from src.orchestrator._model_defaults import FALLBACK_WORKER_MODEL
    model = agent_config.get("model") or FALLBACK_WORKER_MODEL

The fallback only fires when the upstream config is malformed; in normal
operation the backend always sends a model.
"""

from __future__ import annotations

# Platform standard: ALL agents (Manager, system agents, custom
# workers) run on the latest "thinking" Opus. Expressed as the Claude
# CLI's bare family ALIAS ``opus`` rather than a dated id —
# ``claude --print --model opus`` resolves to the CLI's current default
# Opus AT EXECUTION TIME inside the container, so it tracks the newest
# Opus automatically as Phase 1 keeps the CLI updated. No dated id to
# bump, no Anthropic key, no ``/v1/models`` call.
#
# These constants are the LAST-RESORT fallback: in normal operation the
# backend ships the resolved model (also an alias) in sync_config /
# per-agent config. The fallback only fires when upstream config is
# missing a model entirely. A literal here (not a synced cache) is
# deliberate — it must work pre-sync and in the minimal per-agent
# subprocess store.
_DEFAULT_CLAUDE_MODEL = "opus"

FALLBACK_WORKER_MODEL = _DEFAULT_CLAUDE_MODEL
FALLBACK_MANAGER_MODEL = _DEFAULT_CLAUDE_MODEL

# Setup-wizard generation currently DEFAULTS to the Opus tier (see
# ``_setup_cli._DEFAULT_GENERATION_MODEL``, which falls back to
# ``FALLBACK_MANAGER_MODEL``): the one-time office design pass is the
# highest-leverage moment in an office's life, so it gets the strongest
# model even though a full run costs ~15-20 min. This constant is the
# documented FASTER/cheaper opt-out value — the ``sonnet`` alias
# resolves to the CLI's current default Sonnet at execution time. An
# operator who needs a quicker (lower quality) setup sets
# ``CBCL_GENERATION_MODEL=sonnet``.
FALLBACK_WIZARD_MODEL = "sonnet"


# ── Tier detection (item-6 effort gating) ───────────────────────────────
#
# Reasoning-effort (``--effort``) is applied ONLY on the opus tier. The
# platform uses bare aliases (``opus``/``sonnet``/``haiku``), so a bare
# alias IS its tier; dated pins map by prefix. No catalog import needed
# (the communicator can't import the backend module).
def model_tier(model: str | None) -> str | None:
    """Return 'opus' | 'sonnet' | 'haiku' for a CLI model alias/id, else None."""
    if not model:
        return None
    m = model.strip().lower()
    if m in ("opus", "sonnet", "haiku"):
        return m
    if m.startswith("claude-opus"):
        return "opus"
    if m.startswith("claude-sonnet"):
        return "sonnet"
    if m.startswith("claude-haiku"):
        return "haiku"
    return None


def is_opus_tier(model: str | None) -> bool:
    """True when ``model`` resolves to the Opus tier (alias or dated pin)."""
    return model_tier(model) == "opus"
