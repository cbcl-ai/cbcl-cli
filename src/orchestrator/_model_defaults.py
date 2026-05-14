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

# Mirrors ``backend/app/ai_models/defaults.py`` first ``recommended_for=worker``
# entry. Used as the silent-bug fallback in agent_worker / worker_prompt.
FALLBACK_WORKER_MODEL = "claude-sonnet-4-6"

# Mirrors ``backend/app/ai_models/defaults.py`` first ``recommended_for=manager``
# entry. Used as the silent-bug fallback in manager_controller.
FALLBACK_MANAGER_MODEL = "claude-opus-4-7"
