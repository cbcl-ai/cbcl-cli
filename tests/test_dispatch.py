"""Script-webhooks S3: unit coverage for ``handle_script_execute``.

The handler is the daemon-side consumer of the backend's
``script_execute`` command. Historically it hardcoded
``triggered_by="user"`` — correct for the only producer at the time
(the manual Run button) but wrong once the backend attributes runs to
other origins (webhooks stamp ``webhook:{name}``, mirroring cron's
``cron:{name}`` convention). These tests lock down the passthrough
contract:

  * a wire-supplied ``triggered_by`` reaches ``ScriptRunner.execute``
    verbatim (and the synthetic refusal event mirrors it);
  * an absent/falsy field degrades to the legacy ``"user"`` so manual
    runs and older backends keep their historical label;
  * garbage values are stringified and capped at 100 chars (the
    ``script_executions.triggered_by`` column width) — never a crash.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.dispatch import handle_script_execute


def _make_runner() -> MagicMock:
    """Build a ScriptRunner double whose ``execute`` records kwargs.

    ``_router`` carries the publish_event surface the refusal path
    uses; a MagicMock base keeps attribute access permissive like the
    real runner.
    """
    runner = MagicMock()
    runner.execute = AsyncMock(return_value="exec-2026-07-16T10-00-00-abc123")
    runner._router = MagicMock()
    runner._router.publish_event = AsyncMock()
    return runner


def _base_message(**extra: object) -> dict:
    message = {
        "type": "script_execute",
        "script_name": "demo",
        "variable_overrides": {},
        "task_id": None,
    }
    message.update(extra)
    return message


# ── AC1: passthrough + default ──────────────────────────────────


async def test_wire_triggered_by_reaches_runner():
    runner = _make_runner()
    await handle_script_execute(
        _base_message(triggered_by="webhook:deploy-hook"), runner,
    )
    assert runner.execute.await_count == 1
    kwargs = runner.execute.await_args.kwargs
    assert kwargs["triggered_by"] == "webhook:deploy-hook"


async def test_absent_triggered_by_defaults_to_user():
    runner = _make_runner()
    await handle_script_execute(_base_message(), runner)
    kwargs = runner.execute.await_args.kwargs
    assert kwargs["triggered_by"] == "user"


async def test_falsy_triggered_by_defaults_to_user():
    """Explicit ``None`` / empty string on the wire behave like an
    absent field — older backends that don't know the key and
    defensive producers sending null both land on the legacy label."""
    for falsy in (None, ""):
        runner = _make_runner()
        await handle_script_execute(
            _base_message(triggered_by=falsy), runner,
        )
        kwargs = runner.execute.await_args.kwargs
        assert kwargs["triggered_by"] == "user", repr(falsy)


# ── AC2: sanitization — stringified + length-capped ─────────────


async def test_overlong_triggered_by_capped_at_column_width():
    runner = _make_runner()
    await handle_script_execute(
        _base_message(triggered_by="webhook:" + "x" * 200), runner,
    )
    kwargs = runner.execute.await_args.kwargs
    assert len(kwargs["triggered_by"]) == 100
    assert kwargs["triggered_by"].startswith("webhook:xxx")


async def test_garbage_triggered_by_stringified_no_crash():
    """A non-str value (a dict, a number) must not crash the handler
    — it is stringified and capped, whatever it was."""
    for garbage in ({"nested": "dict"}, 12345, ["list"]):
        runner = _make_runner()
        await handle_script_execute(
            _base_message(triggered_by=garbage), runner,
        )
        kwargs = runner.execute.await_args.kwargs
        assert isinstance(kwargs["triggered_by"], str), repr(garbage)
        assert len(kwargs["triggered_by"]) <= 100


# ── refusal event mirrors the attribution ───────────────────────


async def test_refusal_event_mirrors_triggered_by():
    """When the runner refuses (script missing on disk), the
    synthetic ``script_status: failed`` event must carry the SAME
    attribution as the attempted run — a webhook-triggered refusal
    showing up as ``"user"`` in Execution History would be a lie."""
    runner = _make_runner()
    runner.execute = AsyncMock(side_effect=FileNotFoundError("gone"))
    await handle_script_execute(
        _base_message(triggered_by="webhook:nightly"), runner,
    )
    assert runner._router.publish_event.await_count == 1
    event = runner._router.publish_event.await_args.args[0]
    assert event["type"] == "script_status"
    assert event["status"] == "failed"
    assert event["triggered_by"] == "webhook:nightly"


async def test_refusal_event_defaults_to_user_without_attribution():
    runner = _make_runner()
    runner.execute = AsyncMock(side_effect=FileNotFoundError("gone"))
    await handle_script_execute(_base_message(), runner)
    event = runner._router.publish_event.await_args.args[0]
    assert event["triggered_by"] == "user"
