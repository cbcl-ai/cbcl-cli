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
    │   └── skills/{name}/secrets.json
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


def get_pid_path() -> Path:
    """Return ``~/.cubicle/communicator.pid``."""
    return CUBICLE_HOME / "communicator.pid"


# -- Directory paths (create on access) -------------------------------------

def get_workspace_path(office_slug: str) -> Path:
    """Return ``~/.cubicle/workspaces/{office_slug}/``, creating it."""
    path = CUBICLE_HOME / "workspaces" / office_slug
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_secrets_path() -> Path:
    """Return ``~/.cubicle/secrets/``, creating it."""
    path = CUBICLE_HOME / "secrets"
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


def ensure_cubicle_dirs() -> None:
    """Create the full ``~/.cubicle/`` directory structure.

    Called by ``cbcl setup`` and at startup.
    """
    CUBICLE_HOME.mkdir(parents=True, exist_ok=True)
    (CUBICLE_HOME / "secrets").mkdir(exist_ok=True)
    (CUBICLE_HOME / "office-secrets").mkdir(exist_ok=True)
    (CUBICLE_HOME / "data").mkdir(exist_ok=True)
    (CUBICLE_HOME / "workspaces").mkdir(exist_ok=True)
    (CUBICLE_HOME / "logs").mkdir(exist_ok=True)


def safe_agent_dir(agents_dir: Path, name: str) -> Path | None:
    """Return ``agents_dir / name``, or None when ``name`` is unsafe.

    07/H-13: the agent name arrives from the backend and is joined straight
    into a HOST path that is then ``mkdir``-ed and written to. It carried no
    charset validation, so a name like ``../..`` escaped the workspace and
    could overwrite the office's own Manager CLAUDE.md on the operator's
    machine.

    The backend validates new names now, but rows created before that — and
    anything that reaches the daemon by another route — still land here, so
    this is the half that covers existing data. Returns None (caller skips
    with a warning) rather than raising: one bad agent must not abort the
    whole workspace sync.
    """
    if not name or not name.strip():
        return None
    if "/" in name or "\\" in name or "\x00" in name:
        return None
    if name in (".", "..") or name.startswith((".", "~", "-")):
        return None
    candidate = agents_dir / name
    try:
        candidate.resolve().relative_to(agents_dir.resolve())
    except (ValueError, OSError):
        return None
    return candidate
