"""The workstream directory slug has ONE definition in two runtimes (D5/X48).

The daemon writes ``/workspace/workstreams/<dir>/CLAUDE.md`` and workers read
spec.md, plan.md, intake files and task outputs there, while the backend
materialises spec.md/plan.md/intake into the same directory. The two copies
(``communicator/src/paths.py`` and ``backend/app/core/utils.py``) must agree
byte-for-byte or a file lands where nothing reads it.
"""

from __future__ import annotations

import pytest

from src.paths import (
    declared_workstream_dir,
    legacy_workstream_dir_slug,
    workstream_dir_slug,
)
from tests.backend_boundary import import_backend

backend_utils = import_backend("app.core.utils")

CASES = [
    ("Auth Project", "AP", "auth-project"),
    ("R&D Platform", "RP", "r-d-platform"),
    ("Dev / QA", "DQ", "dev-qa"),
    ("Data_Model v2", "DMV", "data-model-v2"),
    ("  spaced  ", "SP", "spaced"),
    # No ASCII letters/digits: the short-code fallback, not a shared "office".
    ("Продажі", "PR", "ws-pr"),
    ("!!!", "WS", "ws-ws"),
    ("", "AB1", "ws-ab1"),
    # A non-ASCII short code is hex-encoded to stay a safe path segment.
    ("Маркетинг", "М", "ws-d0bc"),
    # The legacy fallback name is an ordinary name now.
    ("Office", "OF", "office"),
    (None, None, "ws"),
]


@pytest.mark.parametrize("name,code,expected", CASES)
def test_communicator_slug(name, code, expected) -> None:
    assert workstream_dir_slug(name, code) == expected


@pytest.mark.parametrize("name,code,expected", CASES)
def test_backend_slug_matches_communicator(name, code, expected) -> None:
    assert backend_utils.workstream_dir_slug(name, code) == expected
    assert backend_utils.workstream_dir_slug(name, code) == workstream_dir_slug(
        name, code
    )


def test_two_non_latin_workstreams_get_distinct_directories() -> None:
    first = workstream_dir_slug("Продажі", "П")
    second = workstream_dir_slug("Маркетинг", "М")
    assert first != second
    assert "office" not in (first, second)


@pytest.mark.parametrize("name", ["Auth Project", "R&D", "Продажі", "", None, "!!!"])
def test_legacy_slug_matches_backend(name) -> None:
    """An older backend declares no ``workspace_dir`` and writes projections
    to its legacy directory; the daemon's fallback must name the same one."""
    assert legacy_workstream_dir_slug(name) == backend_utils.legacy_workstream_dir_slug(
        name
    )


def test_declared_directory_is_used_only_when_slug_shaped() -> None:
    assert declared_workstream_dir("ws-pr", "Продажі") == "ws-pr"
    assert declared_workstream_dir(None, "Продажі") == "office"
    for bad in ("../agents", "Alpha", "a/b", "", "-x", 5):
        assert declared_workstream_dir(bad, "Alpha") == "alpha"
