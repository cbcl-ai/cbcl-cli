"""Script sync — writes script files from synced config to the workspace.

When a ``sync_config`` message arrives, this module ensures that script
code and default variable values are written to the workspace so the
Script Runner can find them at execution time.

Script secrets (``.secrets.json``) are never overwritten if they already
exist on disk.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from src._chown import chown_to_agent
from src.utils import remove_dir

logger = logging.getLogger(__name__)


def _archive_before_delete(script_dir: Path) -> bool:
    """Move ``script_dir`` to ``.scripts/.removed_by_sync/<timestamp>/<name>``
    instead of nuking it outright.

    Belt-and-suspenders defence against any future code path that gets
    the "this dir is stale" decision wrong — the script's source files
    (main.py / script.yaml / lib/ / requirements.txt / README.md) are
    irreplaceable when removed (the backend stores only metadata, not
    file content). The archive lives in the same workspace so the
    operator can ``mv`` it back without leaving the host. Pruned by
    age out of band; for now it's monotonically-growing but the
    per-deletion size is tiny.

    Returns True on archive, False on direct-remove fallback. Either
    way the caller can assume the directory is gone from
    ``script_dir.parent``.
    """
    archive_root = script_dir.parent / ".removed_by_sync"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    # Append the script name AFTER the timestamp dir so two removals
    # of differently-named scripts on the same second co-exist.
    archive_dir = archive_root / today
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(script_dir), str(archive_dir / script_dir.name))
        chown_to_agent(archive_root)
        chown_to_agent(archive_dir)
        return True
    except OSError as exc:
        logger.warning(
            "Could not archive %s before removal: %s — falling back "
            "to direct delete",
            script_dir, exc,
        )
        try:
            remove_dir(script_dir)
        except OSError:
            logger.exception(
                "Direct delete of %s also failed", script_dir,
            )
        return False


# Sentinel file written into ``.scripts/`` so the sync can detect a
# workspace-path collision (two offices that happen to slugify to the
# same name share the same ``~/.cubicle/workspaces/<slug>/``). If we
# blindly removed "stale" directories at the end of every sync, the
# second office's sync would wipe out the first office's scripts —
# which is exactly what happened in production when two offices were
# both named "SMM & Copywriting". Keeping the sentinel lets us refuse
# the destructive cleanup step when the workspace is shared.
_OFFICE_ID_SENTINEL = ".synced_by_office_id"


class ScriptSyncer:
    """Sync script files from server config into the workspace.

    Parameters
    ----------
    workspace_path:
        Root workspace directory (e.g., ``/workspace``).
    office_id:
        UUID of the office that owns this sync. Used as a sentinel
        in ``.scripts/.synced_by_office_id`` to detect workspace
        sharing (slug collisions). When the sentinel exists and
        doesn't match, the destructive cleanup step is skipped.
        Optional for back-compat — sync still works, just falls
        back to non-pruning behaviour when ``office_id`` is empty.
    """

    def __init__(
        self, workspace_path: str, office_id: str = "",
    ) -> None:
        self._workspace = Path(workspace_path)
        self._office_id = (office_id or "").strip()

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

        # Workspace-collision guard before cleanup. If two offices
        # share the same workspace dir (slug collision — see e.g.
        # two offices both named "SMM & Copywriting" both resolving
        # to ``workspaces/smm-copywriting/``), the second office's
        # sync would otherwise wipe the first's scripts here. The
        # sentinel file records which office first claimed the dir;
        # subsequent syncs from a DIFFERENT office skip cleanup but
        # still upsert their own scripts (idempotent).
        sentinel = scripts_root / _OFFICE_ID_SENTINEL
        owning_office_id = ""
        if sentinel.is_file():
            try:
                owning_office_id = sentinel.read_text().strip()
            except OSError:
                pass

        cleanup_ok = True
        if self._office_id:
            if not owning_office_id:
                # First sync to this workspace — claim it for this
                # office so future syncs can detect collisions.
                try:
                    sentinel.write_text(self._office_id)
                    chown_to_agent(sentinel)
                except OSError as exc:
                    logger.warning(
                        "Could not write %s: %s — collision detection "
                        "disabled for this sync", sentinel, exc,
                    )
            elif owning_office_id != self._office_id:
                # Workspace is shared. Refuse cleanup; the other
                # office's scripts on disk are NOT stale from our
                # perspective even though our sync didn't list them.
                cleanup_ok = False
                logger.warning(
                    "Workspace %s is shared between offices %s and %s "
                    "(slug collision). Skipping stale-script cleanup "
                    "to preserve sibling office's data. Rename one of "
                    "the offices so each gets a unique workspace slug.",
                    scripts_root, owning_office_id[:8],
                    self._office_id[:8],
                )

        # Empty-sync sanity check. If the backend returns ZERO scripts
        # (transient backend hiccup, bad auth, etc.) but the disk has
        # them, we'd otherwise wipe everything. Refuse cleanup in that
        # case — a real "user deleted all scripts" scenario is rare
        # enough that the operator can clean up manually if needed.
        if cleanup_ok and len(seen_names) == 0 and scripts_root.exists():
            has_existing = any(
                c.is_dir() and not c.name.startswith(".")
                for c in scripts_root.iterdir()
            )
            if has_existing:
                cleanup_ok = False
                logger.warning(
                    "Sync returned 0 scripts but disk has script "
                    "directories at %s. Refusing cleanup — assuming "
                    "transient backend error. If the user truly "
                    "deleted all scripts, restart cbcl to re-trigger "
                    "the cleanup.",
                    scripts_root,
                )

        # Clean up stale script directories — only if we own the workspace
        # AND the sync looks healthy. Stale dirs are ARCHIVED to
        # ``.removed_by_sync/<ts>/<name>`` instead of being nuked, so a
        # mistaken cleanup decision is recoverable. The script's source
        # files (main.py / script.yaml / lib/ / requirements.txt /
        # README.md) are irreplaceable from the backend — only metadata
        # lives in the DB, not file content — so a destructive delete
        # without archive is the worst kind of data loss.
        if cleanup_ok and scripts_root.exists():
            for child in scripts_root.iterdir():
                if (
                    child.is_dir()
                    and child.name not in seen_names
                    # Defensive: never sweep dotfile dirs or the
                    # sentinel itself (it's a file, but belt-and-
                    # suspenders against future shape changes).
                    and not child.name.startswith(".")
                ):
                    archived = _archive_before_delete(child)
                    logger.info(
                        "Removed stale script directory: %s "
                        "(%s)",
                        child.name,
                        "archived to .removed_by_sync/" if archived
                        else "archive failed — deleted in place",
                    )

        if written:
            logger.info("Synced %d script directories to %s", written, scripts_root)

