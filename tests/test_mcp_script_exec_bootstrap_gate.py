"""ADD-C3: the agent ``execute_script`` path must gate on
``bootstrap_status`` like the manual-UI and cron paths.

An agent launching a ``pending`` / ``failed`` script would hit a
confusing ModuleNotFoundError at runtime instead of the actionable
"retry the bootstrap" guidance. ``_check_bootstrap_status`` queries the
backend and refuses a non-complete script, fail-OPEN on any backend
error.

Module import uses the ``_mcp_backend`` stub + sys.path shim.
"""
from __future__ import annotations

import importlib
import pathlib
import sys
import types

import pytest


_AGENT_IMAGE_DIR = (
    pathlib.Path(__file__).resolve().parent.parent / "src" / "_agent_image"
)


@pytest.fixture(scope="module")
def mod():
    # Complete, order-independent stub (both symbols the module imports).
    stub = sys.modules.get("_mcp_backend")
    if stub is None:
        stub = types.ModuleType("_mcp_backend")
        sys.modules["_mcp_backend"] = stub
    if not hasattr(stub, "_get_session"):
        stub._get_session = lambda *a, **k: None
    if not hasattr(stub, "_call_backend"):
        async def _call_backend(action, params):  # overridden per-test
            return {}
        stub._call_backend = _call_backend
    added = False
    if str(_AGENT_IMAGE_DIR) not in sys.path:
        sys.path.insert(0, str(_AGENT_IMAGE_DIR))
        added = True
    try:
        module = importlib.import_module("_mcp_script_exec")
    finally:
        if added:
            sys.path.remove(str(_AGENT_IMAGE_DIR))
    return module


def _patch_call_backend(mod, monkeypatch, fn):
    monkeypatch.setattr(mod, "_call_backend", fn)


@pytest.mark.asyncio
async def test_complete_status_proceeds(mod, monkeypatch) -> None:
    async def _cb(action, params):
        return {"script": {"bootstrap_status": "complete"}}
    _patch_call_backend(mod, monkeypatch, _cb)
    assert await mod._check_bootstrap_status("s") is None


@pytest.mark.asyncio
async def test_pending_status_refuses(mod, monkeypatch) -> None:
    async def _cb(action, params):
        return {"script": {"bootstrap_status": "pending"}}
    _patch_call_backend(mod, monkeypatch, _cb)
    refusal = await mod._check_bootstrap_status("s")
    assert refusal is not None and refusal["error"] is True
    assert "bootstrap_status='pending'" in refusal["message"]
    assert "retry" in refusal["message"].lower()


@pytest.mark.asyncio
async def test_failed_status_refuses(mod, monkeypatch) -> None:
    async def _cb(action, params):
        return {"script": {"bootstrap_status": "failed"}}
    _patch_call_backend(mod, monkeypatch, _cb)
    refusal = await mod._check_bootstrap_status("s")
    assert refusal is not None and refusal["error"] is True


@pytest.mark.asyncio
async def test_backend_error_fails_open(mod, monkeypatch) -> None:
    async def _cb(action, params):
        return {"error": True, "message": "boom"}
    _patch_call_backend(mod, monkeypatch, _cb)
    assert await mod._check_bootstrap_status("s") is None


@pytest.mark.asyncio
async def test_backend_exception_fails_open(mod, monkeypatch) -> None:
    async def _cb(action, params):
        raise RuntimeError("network down")
    _patch_call_backend(mod, monkeypatch, _cb)
    assert await mod._check_bootstrap_status("s") is None


@pytest.mark.asyncio
async def test_missing_status_fails_open(mod, monkeypatch) -> None:
    async def _cb(action, params):
        return {"script": {"name": "s"}}  # no bootstrap_status
    _patch_call_backend(mod, monkeypatch, _cb)
    assert await mod._check_bootstrap_status("s") is None


@pytest.mark.asyncio
async def test_execute_script_refuses_when_not_bootstrapped(
    mod, monkeypatch, tmp_path
) -> None:
    """End-to-end: _execute_script returns the refusal (and never spawns)
    when the script isn't bootstrapped."""
    # Build a fake script dir so the dir check passes.
    script_dir = pathlib.Path("/workspace/.scripts/gate-test")
    # We can't write to /workspace in the test env; instead patch Path so
    # the gate is reached. Simpler: patch _check_bootstrap_status to a
    # refusal and assert _execute_script returns it before touching disk.
    async def _refuse(name):
        return {"error": True, "message": "not ready"}

    monkeypatch.setattr(mod, "_check_bootstrap_status", _refuse)
    # Make the directory check pass without real disk.

    class _FakePath:
        def __init__(self, *a):
            pass

        def is_dir(self):
            return True

    monkeypatch.setattr(mod, "Path", lambda *a, **k: _FakePath())
    monkeypatch.setattr(mod, "TASK_MODE", "execute")

    result = await mod._execute_script({"script_name": "gate-test"})
    assert result == {"error": True, "message": "not ready"}
