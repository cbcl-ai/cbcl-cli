"""Variable Manager — handles script variable and secret management.

Manages the client-side variable files:
- variables.json: non-secret variable values (readable, editable)
- .secrets.json: secret variable values (never leave the client)

These files live in /workspace/.scripts/{script_name}/.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class VariableManager:
    """Manages script variables and secrets on the client side.

    Parameters
    ----------
    workspace:
        Path to the workspace root (e.g., ``/workspace``).
    """

    def __init__(self, workspace: str) -> None:
        self._workspace = workspace

    def get_variables(self, script_name: str) -> dict:
        """Read non-secret variables from variables.json.

        Parameters
        ----------
        script_name:
            Script directory name under .scripts/.

        Returns
        -------
        dict
            Variable key-value pairs. Empty dict if file is missing
            or unreadable.
        """
        var_file = (
            Path(self._workspace) / ".scripts" / script_name / "variables.json"
        )
        if var_file.exists():
            try:
                return json.loads(var_file.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "Failed to read variables for %s: %s",
                    script_name,
                    exc,
                )
        return {}

