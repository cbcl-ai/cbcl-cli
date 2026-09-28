"""Backend parity guards fail closed in the monorepo (X44).

``tests/backend_boundary.import_backend`` skips only in the standalone
cbcl-cli mirror, where ``backend/`` is absent; in the monorepo a missing
backend install is an error. ``pytest.importorskip("app…")`` would instead
skip silently whenever the venv lacks the backend, so parity guards could
pass without running. This scan keeps that pattern out of the suite.
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parents[1]
_SILENT_BACKEND_SKIP = re.compile(r"importorskip\(\s*[\"']app(?:[.\"'])")


def test_no_test_skips_silently_when_the_backend_is_missing():
    offenders = [
        str(path.relative_to(TESTS_ROOT))
        for path in sorted(TESTS_ROOT.rglob("*.py"))
        if path.name != Path(__file__).name
        and _SILENT_BACKEND_SKIP.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        f"use tests.backend_boundary.import_backend instead of importorskip: {offenders}"
    )
