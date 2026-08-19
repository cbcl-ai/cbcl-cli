"""The Stop button's daemon half (D-08).

`ScriptRunner.kill` has existed since NEW-2 — it terminates the REAL
in-container process via its pidfile (killing the host-side docker-exec
client alone leaves the python running, since there is no TTY to forward
signals) and then runs the normal completion path so status, history and
board frames land as they would for a natural exit.

What never existed was a way to reach it: no `script_kill` handler, so the
frame the frontend once sent died at the gateway. This pins the handler,
and specifically its HONESTY — a Stop button whose failure mode is silence
is the bug that got the original removed.
"""
from __future__ import annotations

import logging

import pytest

from src.dispatch import handle_script_kill


class _Runner:
    def __init__(self, result=True, boom: Exception | None = None):
        self.result = result
        self.boom = boom
        self.calls: list[str] = []

    async def kill(self, execution_id: str) -> bool:
        self.calls.append(execution_id)
        if self.boom is not None:
            raise self.boom
        return self.result


@pytest.mark.asyncio
async def test_it_kills_the_named_execution():
    runner = _Runner()
    await handle_script_kill({"execution_id": "exec-abc"}, runner)
    assert runner.calls == ["exec-abc"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [{}, {"execution_id": ""}, {"execution_id": None}, {"execution_id": 7}],
)
async def test_a_malformed_message_never_reaches_the_runner(message: dict):
    runner = _Runner()
    await handle_script_kill(message, runner)
    assert runner.calls == []


@pytest.mark.asyncio
async def test_an_untracked_execution_is_reported_not_swallowed(caplog):
    """kill() returns False for a run that already finished, or that
    started before a daemon restart. That is not an error — but it must
    not be silent either, because 'nothing happened' is exactly what the
    broken Stop used to produce."""
    runner = _Runner(result=False)
    with caplog.at_level(logging.INFO, logger="cbcl.dispatch"):
        await handle_script_kill({"execution_id": "exec-gone"}, runner)
    assert runner.calls == ["exec-gone"]
    # getMessage() renders lazy %-args safely; r.message % r.args does not
    # when the record has none.
    assert any("not tracked" in r.getMessage() for r in caplog.records), caplog.text


@pytest.mark.asyncio
async def test_a_runner_failure_is_logged_loudly_and_does_not_propagate(caplog):
    """A raising kill must not tear down the message router — but the
    process may still be running, so the log has to say so."""
    runner = _Runner(boom=RuntimeError("docker exec failed"))
    with caplog.at_level(logging.ERROR, logger="cbcl.dispatch"):
        await handle_script_kill({"execution_id": "exec-boom"}, runner)
    assert any(r.levelno >= logging.ERROR for r in caplog.records)
    assert "may still be running" in caplog.text


def test_the_router_registers_it():
    """The handler is only reachable if it is wired — the original defect
    was a frame with no home, and a handler with no registration is the
    same defect wearing a different hat."""
    import inspect

    from src import handlers

    source = inspect.getsource(handlers)
    assert 'router.on(\n        "script_kill"' in source or '"script_kill",' in source
    assert "handle_script_kill" in source
