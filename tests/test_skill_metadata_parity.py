"""The skill-metadata contract must be identical on both sides of the wire.

``src/skill_metadata.py`` is the canonical copy; the backend keeps a
byte-identical copy at ``backend/app/skills/skill_metadata.py``. Both suites
run the same fixture file, also kept byte-identical. In the monorepo a
missing or drifted backend copy fails this test; in a standalone CLI checkout
(no ``backend/`` directory) the parity checks skip, following the
``tests/backend_boundary.py`` rule.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from src import skill_metadata as sm
from tests import backend_boundary

COMMUNICATOR_ROOT = Path(__file__).resolve().parents[1]
PAIRS = {
    "module": (
        COMMUNICATOR_ROOT / "src" / "skill_metadata.py",
        Path("app") / "skills" / "skill_metadata.py",
    ),
    "fixtures": (
        COMMUNICATOR_ROOT / "tests" / "fixtures" / "skill_metadata_cases.json",
        Path("tests") / "fixtures" / "skill_metadata_cases.json",
    ),
}


def backend_file(relative: Path) -> Path:
    """Resolve a backend path, skipping only in a standalone CLI checkout.

    When ``backend/`` exists (the monorepo) the path is returned even if the
    file is missing, so reading it fails instead of hiding the drift.
    """
    root = backend_boundary.BACKEND_ROOT
    if not root.is_dir():
        pytest.skip("Private backend is absent from this standalone CLI checkout")
    return root / relative


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("name", sorted(PAIRS))
def test_copies_are_byte_identical(name: str) -> None:
    canonical, backend_relative = PAIRS[name]
    backend_copy = backend_file(backend_relative)
    assert _digest(canonical) == _digest(backend_copy), (
        f"{backend_copy} drifted from {canonical}; copy the communicator file "
        "verbatim (it is the canonical copy)"
    )


def test_backend_copy_exposes_the_same_contract() -> None:
    backend_module = backend_boundary.import_backend("app.skills.skill_metadata")
    assert backend_module.CONTRACT_VERSION == sm.CONTRACT_VERSION
    assert backend_module.__all__ == sm.__all__
    text = "---\nname: x\ndescription: >\n  Folded text that spans\n  two lines.\n---\n"
    assert backend_module.parse_skill_md(text, "x") == sm.parse_skill_md(text, "x")


def test_standalone_checkout_skips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(backend_boundary, "BACKEND_ROOT", tmp_path / "absent")
    with pytest.raises(pytest.skip.Exception, match="standalone CLI checkout"):
        backend_file(PAIRS["module"][1])


def test_monorepo_missing_copy_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend_root = tmp_path / "backend"
    backend_root.mkdir()
    monkeypatch.setattr(backend_boundary, "BACKEND_ROOT", backend_root)
    with pytest.raises(FileNotFoundError):
        _digest(backend_file(PAIRS["module"][1]))
