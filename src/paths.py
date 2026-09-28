"""Centralized path resolution for Cubicle Communicator.

ALL Cubicle data lives under ``~/.cubicle/``.  This module is the single
source of truth for every path the Communicator uses.  No other module
should construct ``~/.cubicle/...`` paths directly — import from here.

Layout::

    ~/.cubicle/
    ├── config.yaml
    ├── credentials.env
    ├── communicator.pid
    ├── logs/
    │   └── communicator.log
    ├── secrets/
    │   └── skills/{name}/secrets.json   ← RETIRED (pre-D2, daemon-wide);
    │                                       no longer read or written
    ├── private-runtime/offices/{office-uuid}/
    │   └── skill-secrets/{skill}.json    ← office-scoped skill secrets
    ├── office-secrets/
    │   └── {office-slug}.json    ← MUST stay outside workspaces/
    ├── data/
    │   └── {office-slug}.sqlite  ← Flow Studio collections rows (local)
    └── workspaces/
        └── {office-slug}/
            ├── .claude/skills/{name}/SKILL.md
            ├── .scripts/{name}/...
            ├── .cubicle/
            │   ├── memory.json
            │   └── sessions.json
            └── outputs/

The ``office-secrets/`` directory sits OUTSIDE the per-office
workspace directory on purpose: the workspace is bind-mounted into
each agent container as ``/workspace`` (read-write), so any file
inside it is readable by every agent via the standard ``Read`` tool.
Putting office secret values inside the workspace would let agents
exfiltrate every credential the user has set. Office secrets live
in ``~/.cubicle/office-secrets/<slug>.json`` so the host-side
Script Runner can read them and inject specific values via
``docker exec -e KEY=VALUE`` at execute time, while the file
itself is never visible to the container.
"""

from __future__ import annotations

import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

CUBICLE_HOME = Path.home() / ".cubicle"


class SecureRotatingFileHandler(RotatingFileHandler):
    """``RotatingFileHandler`` that chmods every roll target to 0o600.

    The default handler creates the new log file under the process
    umask after rotation, so a log that started 0o600 would become
    0o644 on first roll. Cubicle logs contain token fingerprints,
    request ids, and other diagnostic strings worth keeping
    owner-readable only.

    Shared by the daemon log sink (``daemon.py``) and the per-agent
    subprocess log sink (``agent_worker.py``). Lives here in the
    stdlib-only ``paths`` module so the lean agent subprocess can
    import it without pulling in the heavy ``daemon`` dependency tree.
    """

    def doRollover(self) -> None:  # type: ignore[override]
        super().doRollover()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:
            # Don't fail the logging pipeline on a chmod race; the
            # initial setup chmod will be re-applied on the next
            # restart anyway.
            pass


# -- Top-level paths --------------------------------------------------------

def get_config_path() -> Path:
    """Return ``~/.cubicle/config.yaml``."""
    return CUBICLE_HOME / "config.yaml"


def get_credentials_path() -> Path:
    """Return ``~/.cubicle/credentials.env``."""
    return CUBICLE_HOME / "credentials.env"


def get_runtime_state_path() -> Path:
    """Return the host-only admission and recovery database path."""
    return CUBICLE_HOME / "runtime" / "control.sqlite3"


def get_pid_path() -> Path:
    """Return ``~/.cubicle/communicator.pid``."""
    return CUBICLE_HOME / "communicator.pid"


# -- Directory paths (create on access) -------------------------------------

def get_workspace_path(office_slug: str, *, create: bool = True) -> Path:
    """Return the office workspace, optionally without creating it for inspection."""
    path = CUBICLE_HOME / "workspaces" / office_slug
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_office_secrets_path(office_slug: str) -> Path:
    """Return ``~/.cubicle/office-secrets/{office_slug}.json``.

    Lives OUTSIDE the workspace dir on purpose — see the module
    docstring. The parent directory is created with mode 0700 so a
    misconfigured umask can't leave the directory world-readable.
    The file itself is created lazily on first write by
    :func:`src.office_secrets.store.set_office_secret`.
    """
    parent = CUBICLE_HOME / "office-secrets"
    parent.mkdir(parents=True, exist_ok=True)
    try:
        # 0700 — even though each per-office file is 0600, the dir
        # listing itself shouldn't be readable by other users.
        # Best-effort on macOS / bind-mount edge cases.
        import os as _os
        _os.chmod(parent, 0o700)
    except OSError:
        pass
    return parent / f"{office_slug}.json"


def get_datastore_path(office_slug: str) -> Path:
    """Return ``~/.cubicle/data/{office_slug}.sqlite`` (Flow Studio FS-P1).

    The office-local collections datastore — collection ROWS never
    leave the user's machine (spec §5.2); the platform holds schemas
    only and reads rows through request-scoped ``data_*`` RPC proxies.
    Lives OUTSIDE the workspace dir on purpose: the workspace is
    bind-mounted read-write into the agent container, and business
    data should transit only through the schema-validated ``data_*``
    surface, not raw file reads. The parent directory is created with
    mode 0700 (best-effort) like ``office-secrets/``.

    The FILE itself is created lazily on first write by
    :class:`src.datastore.OfficeDatastore`.
    """
    parent = CUBICLE_HOME / "data"
    parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(parent, 0o700)
    except OSError:
        pass
    return parent / f"{office_slug}.sqlite"


def get_logs_path() -> Path:
    """Return ``~/.cubicle/logs/``, creating it."""
    path = CUBICLE_HOME / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


# -- Helpers ----------------------------------------------------------------

def slugify(name: str) -> str:
    """Convert an office name to a filesystem-safe slug.

    >>> slugify("Recruitment Office")
    'recruitment-office'
    >>> slugify("  Dev / QA  ")
    'dev-qa'
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "office"


def workstream_dir_slug(name: str | None, short_code: str | None) -> str:
    """Directory name of a workstream under ``/workspace/workstreams/``.

    MUST byte-match ``backend/app/core/utils.py:workstream_dir_slug`` (the
    backend materialises spec.md, plan.md and intake files into the same
    directory). Pinned by ``tests/test_workstream_dir_slug_parity.py``.

    The name part collapses every non ``[a-z0-9]`` run to one hyphen. A name
    with no ASCII letters or digits falls back to ``ws-<short_code
    lowercased>`` (short codes are unique per office), hex-encoding a
    non-ASCII short code so the directory stays a safe path segment. The old
    shared ``office`` fallback made every non-Latin workstream collide.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    if slug:
        return slug
    code = (short_code or "").strip().lower()
    if not code:
        return "ws"
    if re.fullmatch(r"[a-z0-9]+", code):
        return f"ws-{code}"
    return "ws-" + code.encode("utf-8").hex()


# A workstream directory name the daemon itself could produce (the output
# alphabet of ``workstream_dir_slug`` / ``slugify``).
WORKSPACE_DIR_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def legacy_workstream_dir_slug(name: str | None) -> str:
    """The pre-D5 directory of a workstream: ``slugify(name)``, with the
    shared ``office`` fallback for a name without ASCII letters/digits.

    What a backend that does not declare ``workspace_dir`` writes spec.md,
    plan.md and intake records to (``app/core/utils.py:
    legacy_workstream_dir_slug``).
    """
    return slugify(name or "")


def declared_workstream_dir(declared: object, name: str | None) -> str:
    """The workstream directory the backend declared, else the legacy one.

    A backend that knows ``workstream_dirs_v1`` sends ``workspace_dir``
    (``workstream_dir_slug`` for this daemon) with every workstream row and
    task context; an older backend sends none and keeps writing projections
    to the legacy layout, so the daemon must follow it there. A declared
    value outside the slug alphabet is ignored (never a path component
    that could escape ``workstreams/``).
    """
    if isinstance(declared, str) and WORKSPACE_DIR_RE.fullmatch(declared):
        return declared
    return legacy_workstream_dir_slug(name)


def ensure_cubicle_dirs() -> None:
    """Create the full ``~/.cubicle/`` directory structure.

    Called by ``cbcl setup`` and at startup.
    """
    CUBICLE_HOME.mkdir(parents=True, exist_ok=True)
    # Kept although nothing reads or writes it any more: the stopped-backup
    # plan requires this (empty) archive root (operations/backup_plan.py).
    (CUBICLE_HOME / "secrets").mkdir(exist_ok=True)
    (CUBICLE_HOME / "office-secrets").mkdir(exist_ok=True)
    (CUBICLE_HOME / "data").mkdir(exist_ok=True)
    (CUBICLE_HOME / "workspaces").mkdir(exist_ok=True)
    (CUBICLE_HOME / "logs").mkdir(exist_ok=True)


def is_safe_agent_name(name: str) -> bool:
    """True when ``name`` is one plain directory entry an agent may own.

    07/H-13: no separators, NUL bytes, ``.``/``..`` or leading ``.``/``~``/
    ``-``. The agent name arrives from the backend; rows created before the
    backend validated names, or reaching the daemon by another route, still
    land here. The workspace syncs skip an unsafe name with a warning and
    create agent directories relative to a no-follow directory descriptor.
    """
    if not name or not name.strip():
        return False
    if "/" in name or "\\" in name or "\x00" in name:
        return False
    return not (name in (".", "..") or name.startswith((".", "~", "-")))
