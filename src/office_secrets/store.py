"""Host-side storage for office secrets (GitLab-style shared store).

Single JSON file per office at
``~/.cubicle/office-secrets/<slug>.json`` holding a flat
``{NAME: VALUE}`` map. The Script Runner reads from this file at
``docker exec`` time and injects each referenced secret as a
``-e NAME=VALUE`` env flag to the script subprocess; the value never
appears in the image, in ``docker inspect``, or in ``ps``.

The path lives OUTSIDE the workspace directory (which is bind-mounted
into each agent container at ``/workspace``). If we kept the file
inside the workspace, every agent could read it via the Read tool —
defeating the entire feature's threat model. See ``paths.py`` module
docstring for the layout decision rationale.

Reading back the secret value from disk is intentionally NOT exposed
through any API — the UI / agents see only the metadata (name,
fingerprint, description) the backend persists.

Mirrors ``src/ssh_keys/store.py`` for the SSH-keys feature; the
difference is one-file-per-key vs one-file-with-many-entries here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path

from src.paths import get_office_secrets_path

logger = logging.getLogger(__name__)


# Env-var-safe identifier — must match the backend's
# ``app/office_secrets/schemas.py:_NAME_RE``. If one side relaxes
# this, both must change.
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

# Placeholder identifier persisted as ``container_path`` metadata
# on the backend so the schema stays stable. We do NOT bind-mount
# the host file into the container (the Runner injects values via
# ``-e KEY=VALUE`` at ``docker exec`` time and the bind would expose
# every value to the agents' Read tool). Keep the marker explicit so
# any UI surface that renders ``container_path`` can show the
# "host-only" hint instead of misleadingly pointing at a path that
# doesn't exist in the container.
_CONTAINER_SECRETS_PATH = "host-only:office-secrets"


class OfficeSecretStoreError(Exception):
    """User-actionable failure (bad name, file write failure)."""


class CorruptOfficeSecretsError(Exception):
    """The host secrets file exists but isn't parseable as JSON.

    Distinguishes "file unreadable" from "secret absent" so the
    Script Runner can refuse a launch with a clear "your office
    secrets file is corrupt, restore it from backup or re-add the
    values" message rather than silently emitting a
    ``setup_office_secret`` action_request for every reference (which
    would look to the user like every value was deleted).
    """


def _sanitize_name(name: str) -> str:
    """Refuse anything the backend's validator wouldn't have let
    through. Defence-in-depth against contract drift."""
    n = name.strip()
    if not n:
        raise OfficeSecretStoreError("secret name is required")
    if not _NAME_RE.match(n):
        raise OfficeSecretStoreError(
            f"invalid office secret name {name!r}: must match "
            "^[A-Z][A-Z0-9_]{0,63}$ — uppercase, digits, "
            "underscores; must start with a letter",
        )
    return n


def fingerprint_value(value: str) -> str:
    """SHA-256 of the value, first 16 hex chars. Matches what the
    backend stores in ``office_secrets.fingerprint`` — used by the
    UI so the user can verify "did I update the right one?" without
    revealing the value."""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return digest[:16]


def container_path_for_office_secrets() -> str:
    """The placeholder identifier used in the metadata column. The
    string starts with ``host-only:`` so any UI surface or audit
    grep can tell at a glance that this isn't a real container
    filesystem path."""
    return _CONTAINER_SECRETS_PATH


def _office_secrets_path(office_name: str) -> Path:
    """The host-side secrets file for this office.

    Resolves through ``paths.get_office_secrets_path(slugify(name))``,
    which keeps the file under ``~/.cubicle/office-secrets/`` —
    NEVER under the per-office workspace directory (that's bind-
    mounted into the agent container).
    """
    from src.paths import slugify
    return Path(get_office_secrets_path(slugify(office_name)))


def host_secrets_path_for_office(office_name: str) -> Path:
    """Return the host secrets file path, creating the parent
    directory if needed. The file itself is created lazily on first
    write."""
    return _office_secrets_path(office_name)


def _read_secrets_file(
    path: Path, *, strict: bool = False,
) -> dict[str, str]:
    """Read the full ``{NAME: VALUE}`` map.

    ``strict=False`` (default) — used internally by set/delete to
    operate on a best-effort baseline. Missing → ``{}``. Corrupt JSON
    → ``{}`` + warning log; the next write will atomically replace
    the file with a valid payload.

    ``strict=True`` — used by the Script Runner via
    :func:`read_office_secrets`. Distinguishes "file absent" (→
    ``{}``) from "file present but corrupt/wrong shape" (→ raise
    :class:`CorruptOfficeSecretsError`). The runner uses the
    distinction to decide whether to emit a ``setup_office_secret``
    action_request (absent secret, expected) or refuse the launch
    with a corruption diagnostic (file problem, unexpected).
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        if strict:
            raise CorruptOfficeSecretsError(
                f"office secrets file at {path} is unreadable: "
                f"{type(exc).__name__}",
            ) from exc
        logger.warning(
            "office secrets file at %s is unreadable (%s); treating "
            "as empty until the next write",
            path, exc,
        )
        return {}
    if not isinstance(raw, dict):
        if strict:
            raise CorruptOfficeSecretsError(
                f"office secrets file at {path} has unexpected shape "
                f"({type(raw).__name__}); expected JSON object",
            )
        logger.warning(
            "office secrets file at %s has unexpected shape (%s); "
            "treating as empty",
            path, type(raw).__name__,
        )
        return {}
    # Drop any non-string values defensively — the file format is
    # ``{str: str}`` only.
    return {
        k: v for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, str)
    }


def _atomic_write(path: Path, data: dict[str, str]) -> None:
    """Write the full secrets map atomically with 0600 perms.

    Tempfile in the same directory + rename guarantees the file
    either has the full content or doesn't appear at all — no
    partial-write window where a script subprocess could read a
    truncated mapping.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Lock the directory down to the owner too (mirrors the ssh-keys store):
    # the 0600 file protects the values, but a group/world-traversable parent
    # would still let another local user list per-office secret filenames.
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.write(fd, json.dumps(data, indent=2, sort_keys=True).encode("utf-8"))
        os.fchmod(fd, 0o600)
        os.close(fd)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_office_secrets(office_name: str) -> dict[str, str]:
    """Public reader used by the Script Runner. Returns the live
    ``{NAME: VALUE}`` map for the office. The map MUST NOT be logged
    by the caller — only individual values may be injected via
    ``docker exec -e``.

    Runs in ``strict=True`` mode: file-absent returns ``{}``, but
    file-present-and-corrupt raises :class:`CorruptOfficeSecretsError`
    so the runner can surface a "your secrets file is corrupt" error
    instead of looking like every secret was deleted.
    """
    return _read_secrets_file(_office_secrets_path(office_name), strict=True)


def set_office_secret(
    office_name: str, name: str, value: str,
) -> str:
    """Insert / replace a single secret entry. Returns the fingerprint
    (so the handler can echo it in the ``office_secret_added`` reply
    for the backend to persist). NEVER logs the value.
    """
    safe_name = _sanitize_name(name)
    if not value:
        raise OfficeSecretStoreError("secret value cannot be empty")

    path = _office_secrets_path(office_name)
    secrets = _read_secrets_file(path)
    secrets[safe_name] = value
    try:
        _atomic_write(path, secrets)
        os.chmod(path, 0o600)
    except OSError as exc:
        # Note: the exception message is INTENTIONALLY NOT embedded
        # in the user-facing error. OSError messages typically
        # include the offending path; a future custom storage
        # backend could also include the value. Keep the user
        # message constant; the stack trace lives in the handler's
        # ``logger.exception`` call where ``exc.args`` doesn't carry
        # the value either.
        logger.error(
            "office secret write failed (office=%s, name=%s, "
            "exc_type=%s)",
            office_name, safe_name, type(exc).__name__,
        )
        raise OfficeSecretStoreError(
            "failed to write office secret to disk",
        ) from exc

    return fingerprint_value(value)


def delete_office_secret(office_name: str, name: str) -> None:
    """Remove a single secret entry. Idempotent: missing → no-op."""
    safe_name = _sanitize_name(name)
    path = _office_secrets_path(office_name)
    secrets = _read_secrets_file(path)
    if safe_name not in secrets:
        return
    secrets.pop(safe_name)
    try:
        _atomic_write(path, secrets)
        os.chmod(path, 0o600)
    except OSError as exc:
        logger.error(
            "office secret delete failed (office=%s, name=%s, "
            "exc_type=%s)",
            office_name, safe_name, type(exc).__name__,
        )
        raise OfficeSecretStoreError(
            "failed to update office secrets file",
        ) from exc


def list_office_secret_names(office_name: str) -> list[str]:
    """Sorted list of names currently stored on the host. Used by
    reconcile / drift checks; doesn't expose any value."""
    return sorted(_read_secrets_file(_office_secrets_path(office_name)).keys())
