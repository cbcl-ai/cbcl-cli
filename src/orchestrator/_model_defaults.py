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
# workers) run on the latest "thinking" Opus by default. Operators
# can override per-agent via the Agents page; the fallback only
# fires when upstream config is missing a model entirely.
_DEFAULT_CLAUDE_MODEL = "claude-opus-4-7"

FALLBACK_WORKER_MODEL = _DEFAULT_CLAUDE_MODEL
FALLBACK_MANAGER_MODEL = _DEFAULT_CLAUDE_MODEL

# Setup-wizard generation now DEFAULTS to the Opus tier (see
# ``_setup_cli._DEFAULT_GENERATION_MODEL``): the one-time office design
# pass is the highest-leverage moment in an office's life, so it gets
# the strongest model even though a full run costs ~15-20 min. This
# constant is the documented FASTER/cheaper opt-out value — an operator
# who needs a quicker (lower quality) setup sets
# ``CBCL_GENERATION_MODEL=claude-sonnet-4-6``. Kept as a named anchor
# for that override; the wizard no longer references it directly.
FALLBACK_WIZARD_MODEL = "claude-sonnet-4-6"
