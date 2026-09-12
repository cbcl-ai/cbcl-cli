"""Standalone test guards must not hide broken monorepo imports."""

from types import ModuleType
from unittest.mock import Mock

import pytest

from tests import backend_boundary


def test_standalone_missing_backend_skips(monkeypatch, tmp_path):
    monkeypatch.setattr(backend_boundary, "BACKEND_ROOT", tmp_path / "absent")
    monkeypatch.setattr(
        backend_boundary, "import_module", Mock(side_effect=ModuleNotFoundError(name="app")),
    )
    with pytest.raises(pytest.skip.Exception, match="standalone CLI checkout"):
        backend_boundary.import_backend("app.tasks.board")


def test_monorepo_missing_backend_import_is_failure(monkeypatch, tmp_path):
    backend_root = tmp_path / "backend"
    backend_root.mkdir()
    monkeypatch.setattr(backend_boundary, "BACKEND_ROOT", backend_root)
    monkeypatch.setattr(
        backend_boundary, "import_module", Mock(side_effect=ModuleNotFoundError(name="app")),
    )
    with pytest.raises(ModuleNotFoundError):
        backend_boundary.import_backend("app.tasks.board")


@pytest.mark.parametrize("missing_name", ["sqlalchemy", "app.tasks"])
def test_other_missing_modules_are_not_hidden(monkeypatch, tmp_path, missing_name):
    monkeypatch.setattr(backend_boundary, "BACKEND_ROOT", tmp_path / "absent")
    monkeypatch.setattr(
        backend_boundary, "import_module", Mock(side_effect=ModuleNotFoundError(name=missing_name)),
    )
    with pytest.raises(ModuleNotFoundError):
        backend_boundary.import_backend("app.tasks.board")


def test_available_backend_module_is_returned(monkeypatch):
    module = ModuleType("app.tasks.board")
    importer = Mock(return_value=module)
    monkeypatch.setattr(backend_boundary, "import_module", importer)
    assert backend_boundary.import_backend("app.tasks.board") is module
    importer.assert_called_once_with("app.tasks.board")
