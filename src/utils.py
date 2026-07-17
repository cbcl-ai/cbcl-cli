"""Shared utilities for the Communicator."""

from __future__ import annotations

import functools
import re
from pathlib import Path


@functools.cache
def get_daemon_version() -> str:
    """Return the installed cbcl daemon version (``cubicle-communicator``).

    Resolved via ``importlib.metadata`` from the installed package
    metadata (``pyproject.toml``'s ``version`` — there is no
    ``__version__`` attribute anywhere in this codebase). Surfaced in
    ``cbcl status`` and in every ``health_report`` payload
    (``daemon_version``) so the platform's Connection tab can show
    which daemon build is serving an office. ``"unknown"`` when the
    package metadata is unavailable (e.g. running from a raw source
    tree without an editable install) — never raises.
    """
    try:
        from importlib.metadata import version

        return version("cubicle-communicator")
    except Exception:
        return "unknown"


def describe_exception(exc: BaseException) -> str:
    """Render an exception for log lines that won't silently lose context.

    httpx's TimeoutException family (ReadTimeout, ConnectTimeout,
    PoolTimeout, WriteTimeout) all stringify to the empty string —
    ``"Failed to discover offices: %s" % exc`` produces literally
    ``"Failed to discover offices: "`` and the operator has no clue
    whether the backend is down, the daemon's clock is wrong, the
    connection pool is exhausted, or the URL is wrong. Same problem
    bites every place we log an HTTP exception inside ``except
    Exception``.

    Always include the class name so a timeout is recognisable as
    one even when the message is empty. Falls back to ``"no detail"``
    for unrelated empty exceptions so the trailing punctuation
    doesn't dangle.
    """
    msg = str(exc).strip()
    return f"{type(exc).__name__}: {msg or 'no detail'}"

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
