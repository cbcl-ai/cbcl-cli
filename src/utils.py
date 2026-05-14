"""Shared utilities for the Communicator."""

from __future__ import annotations

import re
from pathlib import Path

# Positive regex: alphanumeric start, then alphanumeric/underscore/hyphen.
# Max length 100. This is stricter and safer than a negative blacklist.
_VALID_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
_MAX_NAME_LENGTH = 100


def remove_dir(path: Path) -> None:
    """Remove a directory and all its contents recursively."""
    for child in path.iterdir():
        if child.is_dir():
            remove_dir(child)
        else:
            child.unlink()
    path.rmdir()


def validate_name(name: str) -> None:
    """Validate that a name is safe for use as a filesystem path component.

    Names must start with an alphanumeric character, contain only
    alphanumeric characters, hyphens, or underscores, and be at most
    100 characters long.

    Parameters
    ----------
    name:
        The name to validate (e.g., script name, skill name).

    Raises
    ------
    ValueError
        If the name does not match the allowed pattern.
    """
    if not name:
        raise ValueError("Name must not be empty")
    if len(name) > _MAX_NAME_LENGTH:
        raise ValueError(
            f"Name too long ({len(name)} chars, max {_MAX_NAME_LENGTH}): {name!r}"
        )
    if not _VALID_NAME_RE.match(name):
        raise ValueError(
            f"Invalid name: {name!r}. Names must match "
            f"^[a-zA-Z0-9][a-zA-Z0-9_-]*$ (max {_MAX_NAME_LENGTH} chars)."
        )
