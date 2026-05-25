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

import re
from pathlib import Path

CUBICLE_HOME = Path.home() / ".cubicle"


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
    (CUBICLE_HOME / "workspaces").mkdir(exist_ok=True)
    (CUBICLE_HOME / "logs").mkdir(exist_ok=True)
