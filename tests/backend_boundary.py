"""Backend parity remains mandatory in monorepos and optional in CLI checkouts."""

from importlib import import_module
from pathlib import Path
from types import ModuleType

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[2] / "backend"


def import_backend(module_name: str) -> ModuleType:
    try:
        return import_module(module_name)
    except ModuleNotFoundError as error:
        if BACKEND_ROOT.is_dir() or error.name != "app":
            raise
        pytest.skip("Private backend is absent from this standalone CLI checkout", allow_module_level=True)
