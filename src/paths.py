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
    └── workspaces/
        └── {office-slug}/
            ├── .claude/skills/{name}/SKILL.md
            ├── .scripts/{name}/...
            ├── .cubicle/
            │   ├── memory.json
            │   └── sessions.json
            └── outputs/
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
    (CUBICLE_HOME / "workspaces").mkdir(exist_ok=True)
    (CUBICLE_HOME / "logs").mkdir(exist_ok=True)
