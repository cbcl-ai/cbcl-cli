"""Unified secrets store for scripts and skills.

Manages secret values that never leave the user's machine:
- Script secrets: /workspace/.scripts/{name}/.secrets.json
- Skill secrets:  ~/.cubicle/secrets/skills/{name}/secrets.json
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from src.utils import validate_name

logger = logging.getLogger(__name__)


class SecretsStore:
    """Read and write secrets for scripts and skills.

    Parameters
    ----------
    workspace_path:
        Root workspace directory (e.g., ``/workspace``).
    config_dir:
        User config directory (default ``~/.cubicle``).
    """

    def __init__(
        self,
        workspace_path: str,
        config_dir: str | None = None,
    ) -> None:
        from src.paths import CUBICLE_HOME

        self._workspace = Path(workspace_path)
        self._config_dir = Path(config_dir) if config_dir else CUBICLE_HOME

    # ------------------------------------------------------------------
    # Script secrets — stored alongside scripts in the workspace
    # ------------------------------------------------------------------

    def get_script_secrets(self, script_name: str) -> dict:
        """Read all secrets for a script from ``.secrets.json``."""
        validate_name(script_name)
        return self._read_json(
            self._workspace / ".scripts" / script_name / ".secrets.json"
        )

    def set_script_secret(
        self, script_name: str, var_name: str, value: str
    ) -> None:
        """Write a single secret into a script's ``.secrets.json``."""
        validate_name(script_name)
        validate_name(var_name)
        secrets_file = (
            self._workspace / ".scripts" / script_name / ".secrets.json"
        )
        self._upsert_json(secrets_file, var_name, value)

    def delete_script_secret(
        self, script_name: str, var_name: str,
    ) -> None:
        """Remove a single secret from a script's ``.secrets.json``.

        No-op when the file or key doesn't exist — callers commonly
        invoke this defensively (e.g. after rebinding a secret
        variable from "Custom literal" to "Office Secret" via the
        Variables UI) and a missing entry isn't an error condition.

        Atomic write via tempfile + ``os.replace`` so a crash mid-
        write can't leave the secrets file partially-mutated.
        """
        validate_name(script_name)
        validate_name(var_name)
        secrets_file = (
            self._workspace / ".scripts" / script_name / ".secrets.json"
        )
        self._remove_json_key(secrets_file, var_name)

    # ------------------------------------------------------------------
    # Skill secrets — stored under ~/.cubicle/secrets/skills/
    # ------------------------------------------------------------------

    def get_skill_secrets(self, skill_name: str) -> dict:
        """Read all secrets for a skill."""
        validate_name(skill_name)
        return self._read_json(
            self._config_dir
            / "secrets"
            / "skills"
            / skill_name
            / "secrets.json"
        )

    def set_skill_secret(
        self, skill_name: str, param_name: str, value: str
    ) -> None:
        """Write a single secret for a skill."""
        validate_name(skill_name)
        validate_name(param_name)
        secrets_file = (
            self._config_dir
            / "secrets"
            / "skills"
            / skill_name
            / "secrets.json"
        )
        self._upsert_json(secrets_file, param_name, value)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _read_json(path: Path) -> dict:
        """Read a JSON file, returning ``{}`` if missing or invalid."""
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read secrets from %s: %s", path, exc)
            return {}

    @staticmethod
    def _upsert_json(path: Path, key: str, value: str) -> None:
        """Insert or update a key in a JSON file, creating parents."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("Cannot create directory %s: %s", path.parent, exc)
            return

        data: dict = {}
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except json.JSONDecodeError:
                # Back up the corrupt file before overwriting
                backup_path = path.with_suffix(".json.corrupt")
                logger.warning(
                    "Corrupt JSON in %s — backing up to %s before overwriting",
                    path,
                    backup_path,
                )
                try:
                    import shutil

                    shutil.copy2(str(path), str(backup_path))
                except OSError as backup_exc:
                    logger.error(
                        "Failed to back up corrupt file %s: %s",
                        path,
                        backup_exc,
                    )
            except OSError as exc:
                logger.warning(
                    "Failed to read %s: %s", path, exc
                )

        data[key] = value
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), suffix=".tmp"
            )
            try:
                os.write(fd, json.dumps(data, indent=2).encode())
                os.fchmod(fd, 0o600)
                os.close(fd)
                os.replace(tmp_path, str(path))
            except Exception:
                os.close(fd)
                os.unlink(tmp_path)
                raise
        except OSError as exc:
            logger.error("Failed to write secret to %s: %s", path, exc)

    @staticmethod
    def _remove_json_key(path: Path, key: str) -> None:
        """Remove ``key`` from a JSON file; no-op when absent.

        Atomic write via tempfile + ``os.replace`` so a crash mid-
        write can't leave the secrets file half-mutated. When
        removing the LAST key, the file is left as ``{}`` rather
        than deleted — keeps the layout self-evident on disk for
        debugging, and an empty secrets.json is well-defined input
        for ``get_script_secrets``.
        """
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Failed to read %s while removing key %r: %s",
                path, key, exc,
            )
            return
        if not isinstance(data, dict) or key not in data:
            return
        del data[key]
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), suffix=".tmp",
            )
            try:
                os.write(fd, json.dumps(data, indent=2).encode())
                os.fchmod(fd, 0o600)
                os.close(fd)
                os.replace(tmp_path, str(path))
            except Exception:
                os.close(fd)
                os.unlink(tmp_path)
                raise
        except OSError as exc:
            logger.error(
                "Failed to remove key %r from %s: %s", key, path, exc,
            )
