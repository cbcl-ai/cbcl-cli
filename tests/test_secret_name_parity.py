"""The backend relays a skill secret only under a name the daemon stores.

The backend refuses a skill or parameter name the daemon cannot store with
``app/skills/secret_params.secret_name_storable`` (``DAEMON_NAME_PATTERN``),
before relaying ``skill_secret_update``; the daemon stores the value with
``SecretsStore.set_skill_secret``, which checks both names with
``src/utils.validate_name``. The two rules are separate copies on either
side of the platform boundary: a name only the backend accepts is relayed
and then dropped by the daemon, and a name only the daemon accepts is
refused for no reason. In a standalone CLI checkout the backend comparisons
skip.
"""

from __future__ import annotations

import pytest

from src.utils import validate_name
from tests import backend_boundary

NAMES = [
    # Storable.
    "API_TOKEN",
    "api-token",
    "code-review",
    "a",
    "9lives",
    "a" * 100,
    # Not storable.
    "",
    "a" * 101,
    "_leading",
    "-leading",
    ".hidden",
    "has space",
    "dotted.name",
    "slash/name",
    "é",
    "trailing\n",
    "a" * 100 + "\n",
    "nul\x00",
]


def _daemon_stores(name: str) -> bool:
    try:
        validate_name(name)
    except ValueError:
        return False
    return True


def _backend():
    return backend_boundary.import_backend("app.skills.secret_params")


@pytest.mark.parametrize("name", NAMES)
def test_both_sides_agree_on_each_name(name):
    assert _backend().secret_name_storable(name) == _daemon_stores(name), name


def test_the_corpus_covers_both_outcomes():
    assert {_daemon_stores(name) for name in NAMES} == {True, False}
