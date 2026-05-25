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
