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

import pytest

from tests.agent_image_stub import stubbed_mcp_script_exec


@pytest.fixture(scope="module")
def mod():
    with stubbed_mcp_script_exec() as module:
        yield module


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
    # The text must bound the re-check and route a still-pending row to the
    # user's Retry bootstrap, never promise it "finishes on its own".
    # fw3-automation (scripts.md §2.2): the Retry is safe to repeat. C3e G3:
    # this very check already tried the automatic repair a transient
    # from-scratch failure or a stuck 'pending' row gets, and a reconnect
    # retries it too.
    message = refusal["message"]
    assert "re-check ONCE" in message
    assert "still 'pending', or it is 'failed'" in message
    assert "Scripts page 'Retry bootstrap' button, safe to repeat" in message
    assert "it writes only missing files" in message
    assert "This check already asked the backend to repair it" in message
    assert "stuck 'pending' for 10 minutes" in message
    assert "on every communicator reconnect" in message
    assert "no automatic recovery" not in message
    assert "on its own" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind", ["template", "clone"])
async def test_template_or_clone_refusal_names_the_reinstall(
    mod, monkeypatch, source_kind
) -> None:
    """L07: Retry cannot lay a template's or a clone source's files, so the
    refusal for those names the reinstall instead of the Retry button."""
    async def _cb(action, params):
        return {"script": {"bootstrap_status": "failed", "source_kind": source_kind}}
    _patch_call_backend(mod, monkeypatch, _cb)
    message = (await mod._check_bootstrap_status("s"))["message"]
    assert "deleting it and installing or duplicating it again" in message
    assert "button, safe to repeat" not in message
    assert "reconnect" not in message
    assert "ESCALATED (external_outage):" in message
    assert "names script 's' and the reinstall or re-duplication it needs" in message


@pytest.mark.asyncio
async def test_failed_status_refuses(mod, monkeypatch) -> None:
    async def _cb(action, params):
        return {"script": {"bootstrap_status": "failed"}}
    _patch_call_backend(mod, monkeypatch, _cb)
    refusal = await mod._check_bootstrap_status("s")
    assert refusal is not None and refusal["error"] is True
    # X65: no phantom ``retry_bootstrap`` tool — the Retry is the user's,
    # and a blocked task uses the ONE-call ESCALATED protocol.
    message = refusal["message"]
    assert "retry_bootstrap" not in message
    assert "Scripts page 'Retry bootstrap'" in message
    assert "no agent tool" in message
    assert "ESCALATED (external_outage):" in message
    assert 'ONE `update_status(new_status="blocked")` call' in message
    # The backend resumes the task on the script's completion only when its
    # escalation names the script, so the refusal asks for the name.
    assert "names script 's' and the Scripts page 'Retry bootstrap'" in message


_PHASE_CALLS = {
    "execute": 'ONE `update_status(new_status="blocked")` call',
    "review": 'ONE `move_task(new_status="blocked")` call',
    "triage": '`escalate_blocker(blocker_class="external_outage")`',
}


@pytest.mark.asyncio
@pytest.mark.parametrize("task_mode", sorted(_PHASE_CALLS))
async def test_refusal_names_the_blocking_call_for_the_phase(
    mod, monkeypatch, task_mode
) -> None:
    """B3-hygiene-2: the bootstrap refusal ends in the caller's phase call.
    Triage may not re-block its own task (update_status is not registered
    and move_task on it is refused), so it files escalate_blocker and stops."""
    async def _cb(action, params):
        return {"script": {"bootstrap_status": "failed"}}
    _patch_call_backend(mod, monkeypatch, _cb)
    monkeypatch.setattr(mod, "TASK_MODE", task_mode)
    message = (await mod._check_bootstrap_status("s"))["message"]
    assert _PHASE_CALLS[task_mode] in message
    for other, call in _PHASE_CALLS.items():
        if other != task_mode:
            assert call not in message
    if task_mode == "triage":
        assert "update_status" not in message and "move_task" not in message
        assert message.endswith("then stop.")
    else:
        assert "`ESCALATED (external_outage):`" in message


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
    # /workspace is not writable in the test env, so patch
    # _check_bootstrap_status to a refusal and assert _execute_script
    # returns it before touching disk.
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
