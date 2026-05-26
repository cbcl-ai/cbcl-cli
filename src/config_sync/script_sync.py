"""Script sync — writes script files from synced config to the workspace.

When a ``sync_config`` message arrives, this module ensures that script
code and default variable values are written to the workspace so the
Script Runner can find them at execution time.

Script secrets (``.secrets.json``) are never overwritten if they already
exist on disk.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from src._chown import chown_to_agent
from src.utils import remove_dir

logger = logging.getLogger(__name__)


class ScriptSyncer:
    """Sync script files from server config into the workspace.

    Parameters
    ----------
    workspace_path:
        Root workspace directory (e.g., ``/workspace``).
    """

    def __init__(self, workspace_path: str) -> None:
        self._workspace = Path(workspace_path)

    async def sync_from_config(self, message: dict) -> None:
        """Handle a ``sync_config`` message and write script files.

        Extracts scripts directly from the message payload (does not
        depend on ConfigStore being updated first).
        """
        scripts = message.get("config", {}).get("scripts", [])
        await self.sync_scripts(scripts)

    async def sync_scripts(self, scripts: list[dict]) -> None:
        """Ensure script directories exist and sync variable defaults.

        Mini-project files (main.py, script.yaml, lib/, requirements.txt,
        README.md) live on the filesystem — laid down by the backend
        bootstrap on create and edited by agents or users via the Files
        tree. This sync only keeps the directory skeleton and variable
        defaults in step with the DB; it never rewrites project files.

        For each script:
        - Creates ``/workspace/.scripts/{name}/``
        - Writes ``variables.json`` with non-secret default values
        - Creates an empty ``.secrets.json`` only if one doesn't exist
        - Creates the ``executions/`` directory
        - Does NOT overwrite main.py, script.yaml, lib/, etc.

        Removes stale script directories not in the current config.
        """
        scripts_root = self._workspace / ".scripts"
        scripts_root.mkdir(parents=True, exist_ok=True)
        chown_to_agent(scripts_root)

        seen_names: set[str] = set()
        written = 0

        for script in scripts:
            name = script.get("name", "")
            if not name:
                continue
            seen_names.add(name)

            script_dir = scripts_root / name
            script_dir.mkdir(parents=True, exist_ok=True)
            chown_to_agent(script_dir)

            # variables.json is user-managed via the UI and stores
            # non-secret variable values for this script. Only
            # create it when MISSING so a sync never clobbers the
            # user's edits. The manifest's ``default:`` field on
            # each variable is authoritative for runtime defaults;
            # variables.json just carries user overrides.
            variables_file = script_dir / "variables.json"
            if not variables_file.exists():
                try:
                    variables_file.write_text("{}")
                    chown_to_agent(variables_file)
                except OSError as exc:
                    logger.error(
                        "Failed to create variables.json for %s: %s",
                        name, exc,
                    )

            # .secrets.json is user-managed; NEVER overwrite.
            secrets_file = script_dir / ".secrets.json"
            if not secrets_file.exists():
                try:
                    secrets_file.write_text("{}")
                    chown_to_agent(secrets_file)
                except OSError as exc:
                    logger.error(
                        "Failed to create .secrets.json for %s: %s",
                        name, exc,
                    )

            # Ensure executions directory exists
            executions_dir = script_dir / "executions"
            executions_dir.mkdir(exist_ok=True)
            chown_to_agent(executions_dir)

            written += 1

        # Clean up stale script directories
        if scripts_root.exists():
            for child in scripts_root.iterdir():
                if child.is_dir() and child.name not in seen_names:
                    remove_dir(child)
                    logger.info(
                        "Removed stale script directory: %s", child.name
                    )

        if written:
            logger.info("Synced %d script directories to %s", written, scripts_root)

