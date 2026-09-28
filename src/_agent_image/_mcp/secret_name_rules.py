"""Office-secret names no office secret can have.

The backend refuses two kinds of office-secret name: names that change how
Claude signs in (``claude_auth_env``) and dynamic-loader names
(``host_loader_env``). A script binding or legacy ``from_office_secret``
stored before those refusals can therefore never resolve. The daemon's host
runner, the manifest parser and the in-container executor all refuse such a
reference with the fix instead of asking the user for a secret they cannot
add; this module gives them one answer.
"""

from __future__ import annotations

from collections.abc import Iterable

from .claude_auth_env import RESERVED_REASON, is_reserved_claude_env_name
from .host_loader_env import LOADER_REASON, is_loader_env_name


def refused_secret_references(references: Iterable[object]) -> tuple[list[str], str]:
    """Return the references no office secret can match, and why.

    The names come back sorted and de-duplicated; the reason is empty when
    none is refused.
    """
    refused = sorted(
        {
            reference
            for reference in references
            if isinstance(reference, str)
            and (
                is_reserved_claude_env_name(reference)
                or is_loader_env_name(reference)
            )
        }
    )
    reasons = []
    if any(is_reserved_claude_env_name(name) for name in refused):
        reasons.append(RESERVED_REASON)
    if any(is_loader_env_name(name) for name in refused):
        reasons.append(LOADER_REASON)
    return refused, "; ".join(reasons)
