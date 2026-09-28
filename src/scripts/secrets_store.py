"""Unified secrets store for scripts and skills.

Manages secret values that never leave the user's machine:

- Script secrets: ``/workspace/.scripts/{name}/.secrets.json`` (inside the
  bind-mounted workspace — readable by agents in the office container).
- Skill secret parameters (D2): host-only and office-scoped at
  ``~/.cubicle/private-runtime/offices/{office-uuid}/skill-secrets/{skill}.json``
  (directories 0700, file 0600). The private-runtime tree is never
  bind-mounted into a container (only its ``claude-auth`` / ``ssh-keys``
  subdirectories are), and office deletion removes it with the rest of the
  office's private runtime. The values are STORED ONLY: no session, script
  or prompt receives them — agents that need a credential use Office
  Secrets or a Connector.

The pre-D2 daemon-wide file ``~/.cubicle/secrets/skills/{skill}/secrets.json``
was keyed by skill name alone, so two offices with the same skill slug
overwrote each other. It is no longer read or written. It is deliberately
NOT migrated (the owning office cannot be proven) and NOT deleted (it may be
the only copy of a value an operator still needs); operators can re-enter
values, then remove the legacy directory by hand.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import uuid
from datetime import UTC, datetime
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
        User config directory (default ``~/.cubicle``, resolved at call
        time so tests that repoint ``paths.CUBICLE_HOME`` are honoured).
    office_id:
        The office's immutable UUID. Required for skill secrets, which are
        office-scoped; script secrets do not use it.
    """

    def __init__(
        self,
        workspace_path: str,
        config_dir: str | None = None,
        office_id: str | None = None,
    ) -> None:
        self._workspace = Path(workspace_path)
        self._config_dir_override = Path(config_dir) if config_dir else None
        self._office_id = office_id

    @property
    def _config_dir(self) -> Path:
        from src import paths

        return self._config_dir_override or paths.CUBICLE_HOME

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
    # Skill secrets — host-only, office-scoped private runtime (D2)
    # ------------------------------------------------------------------

    def skill_secrets_dir(self) -> Path:
        """Return this office's skill-secret directory (not created).

        Derived from ``office_runtime.office_runtime_dir`` so office deletion,
        which removes that directory, also removes these secrets. Raises
        ``ValueError`` for a missing or invalid office id.
        """
        from src import paths
        from src.office_runtime import RuntimeStorageError, office_runtime_dir

        if not self._office_id:
            raise ValueError(
                "Skill secrets are office-scoped; this store has no office id"
            )
        try:
            office_dir = office_runtime_dir(self._office_id)
        except RuntimeStorageError as exc:
            raise ValueError("Skill secrets need a valid office UUID") from exc
        if self._config_dir_override is not None:
            office_dir = self._config_dir_override / office_dir.relative_to(
                paths.CUBICLE_HOME
            )
        return office_dir / "skill-secrets"

    def legacy_skill_secrets_path(self, skill_name: str) -> Path:
        """The retired daemon-wide location (never read or written)."""
        validate_name(skill_name)
        return (
            self._config_dir / "secrets" / "skills" / skill_name / "secrets.json"
        )

    def get_skill_secrets(self, skill_name: str) -> dict:
        """Read this office's stored secrets for a skill (``{}`` if none).

        Nothing in the daemon delivers these values to a session today;
        the reader exists for tests and a future, explicitly designed
        delivery path.
        """
        validate_name(skill_name)
        path = self.skill_secrets_dir() / f"{skill_name}.json"
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            logger.warning("Failed to open skill secrets %s: %s", path, exc)
            return {}
        with os.fdopen(descriptor, "r", encoding="utf-8") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                logger.warning("Skill secrets path %s is not a regular file", path)
                return {}
            try:
                data = json.load(source)
            except (ValueError, OSError) as exc:
                logger.warning("Failed to read skill secrets %s: %s", path, exc)
                return {}
        return data if isinstance(data, dict) else {}

    def set_skill_secret(
        self, skill_name: str, param_name: str, value: str
    ) -> None:
        """Store one secret parameter value for a skill of THIS office.

        Raises ``ValueError`` for an invalid name or a store without an
        office id, and ``OSError`` when the private directory is unsafe or
        the write fails — a skill secret is never silently dropped.
        """
        validate_name(skill_name)
        validate_name(param_name)
        directory = self.skill_secrets_dir()
        self._ensure_private_dirs(directory)
        path = directory / f"{skill_name}.json"
        data = self.get_skill_secrets(skill_name)
        if not data:
            self._set_aside_unreadable(path)
        data[param_name] = value
        self._write_private_json(path, data)

    @staticmethod
    def _set_aside_unreadable(path: Path) -> None:
        """Keep an unreadable existing file instead of silently overwriting
        the other values it may still hold.

        Each backup gets a unique name (``<skill>.json.corrupt-<utc>-<id>``),
        so a file that becomes unreadable again later never replaces an
        earlier backup."""
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size == 0:
            return
        try:
            with open(path, encoding="utf-8") as source:
                if isinstance(json.load(source), dict):
                    return
        except (ValueError, OSError):
            pass
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        backup = path.with_name(f"{path.name}.corrupt-{stamp}-{uuid.uuid4().hex[:8]}")
        os.replace(path, backup)
        logger.warning("Unreadable skill secrets file moved to %s", backup)

    def _ensure_private_dirs(self, directory: Path) -> None:
        """Create ``directory`` and its private-runtime parents as 0700
        directories owned by this user, refusing symlinks anywhere below
        the config dir."""
        base = self._config_dir
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        current = base
        for part in directory.relative_to(base).parts:
            current = current / part
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            metadata = current.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise OSError(
                    f"Unsafe skill-secret directory {current}: not a "
                    "directory owned by the daemon user"
                )
            if part == "skill-secrets":
                os.chmod(current, 0o700)

    @staticmethod
    def _write_private_json(path: Path, data: dict) -> None:
        """Atomically write ``data`` as a 0600 file; raise on failure."""
        descriptor, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                os.fchmod(output.fileno(), 0o600)
                json.dump(data, output, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
            raise

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
