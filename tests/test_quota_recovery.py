from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from src.orchestrator.error_classifier import (
    ErrorClass,
    _parse_reset_time,
    classify_error,
)
from src.quota_recovery import QuotaRecovery, defer_quota_task, public_quota_status
from src.runtime_state import AdmissionPaused, RuntimeState


@pytest.fixture
def runtime(tmp_path):
    return RuntimeState(tmp_path / "runtime.sqlite", "office-one")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("resets at 2026-09-14T20:30:00+03:00", "2026-09-14T17:30:00+00:00"),
        ("resets at 8:30pm (Europe/Kyiv)", "2026-09-14T17:30:00+00:00"),
        ("resets at 8:30pm (UTC+03:00)", "2026-09-14T17:30:00+00:00"),
        ("resets at 8:30pm (UTC+0300)", "2026-09-14T17:30:00+00:00"),
        ("resets at 8:30pm UTC+03:00", "2026-09-14T17:30:00+00:00"),
        ("resets at 8:30pm (Etc/GMT+3)", "2026-09-14T23:30:00+00:00"),
        ("resets at 8:30pm PDT", None),
        ("resets at 123:45", None),
        ("resets at 8:3", None),
        ("resets Sep 21 at 3am (America/New_York)", "2026-09-21T07:00:00+00:00"),
        ("resets at 8pm", "2026-09-14T20:00:00+00:00"),
        ("resets at 8am", "2026-09-15T08:00:00+00:00"),
        ("resets at 8am (CST)", None),
        ("resets at 25:90", None),
        ("resets Mar 8, 2026 at 2:30am (America/New_York)", None),
    ],
)
def test_provider_clock_uses_explicit_timezone(text, expected):
    result = _parse_reset_time(text, now=datetime(2026, 9, 14, 12, tzinfo=timezone.utc))
    assert (result.isoformat() if result else None) == expected


def test_real_limit_phrase_and_per_minute_throttle_are_distinct():
    assert (
        classify_error("You've hit your limit · resets 8pm (Europe/Kyiv)").error_class
        == ErrorClass.USAGE_LIMIT_EXCEEDED
    )
    assert (
        classify_error("429 rate limit resets in 30 seconds").error_class
        == ErrorClass.RATE_LIMITED
    )


def test_pause_is_durable_isolated_and_preserves_non_ai_work(runtime):
    runtime.pause_for_quota("Claude usage limit reached|2000000000", "opus", now=100)
    restored = RuntimeState(runtime.database_path, "office-one")
    assert restored.quota_status()["next_check_at"] == 2000000060
    assert (
        RuntimeState(runtime.database_path, "office-two").quota_status()["state"]
        == "running"
    )
    for kind in ("worker", "generation"):
        with pytest.raises(AdmissionPaused):
            restored.reserve(kind)
    restored.release(restored.reserve("script"))
    assert public_quota_status(restored.quota_status())["next_check_at"].endswith(
        "+00:00"
    )


def test_unknown_reset_does_not_erase_known_deadline(runtime):
    runtime.pause_for_quota("Claude usage limit reached|2000000000", "opus", now=100)
    runtime.pause_for_quota("Claude usage limit reached", "opus", now=200)
    assert runtime.quota_status()["next_check_at"] == 2000000060


async def test_timer_waits_then_probes_and_resumes_once(runtime):
    runtime.pause_for_quota("Claude usage limit reached|2000000000", "opus", now=100)
    clock = Mock(return_value=2000000059)
    dispatcher = Mock(_reconcile_once=AsyncMock())
    probe = AsyncMock(return_value=(True, ""))
    recovery = QuotaRecovery(
        runtime,
        container_id="container",
        dispatcher=dispatcher,
        probe=probe,
        clock=clock,
    )
    assert not await recovery.tick()
    probe.assert_not_called()
    clock.return_value += 1
    assert await recovery.tick()
    probe.assert_awaited_once_with("opus")
    dispatcher.wake.assert_called_once()
    assert runtime.quota_status()["state"] == "running"
    assert not await recovery.tick()


async def test_early_recovery_cancels_deadline_and_keeps_maintenance(runtime):
    runtime.pause_for_quota("Claude usage limit reached|2000000000", "opus", now=100)
    runtime.set_maintenance(True)
    recovery = QuotaRecovery(
        runtime,
        container_id="container",
        dispatcher=Mock(_reconcile_once=AsyncMock()),
        probe=AsyncMock(return_value=(True, "")),
        clock=lambda: 200,
    )
    assert await recovery.tick(force=True)
    assert runtime.quota_status()["state"] == "running"
    with pytest.raises(AdmissionPaused):
        runtime.reserve("worker")
    assert not await recovery.tick()


async def test_probe_failure_never_claims_recovery(runtime):
    runtime.pause_for_quota("Claude usage limit reached", "opus", now=100)
    probe = AsyncMock(return_value=(False, "Claude usage limit reached|2000000000"))
    recovery = QuotaRecovery(
        runtime,
        container_id="container",
        dispatcher=Mock(),
        probe=probe,
        clock=lambda: 4000,
    )
    assert not await recovery.tick()
    assert runtime.quota_status()["next_check_at"] == 2000000060
    recovery.dispatcher.wake.assert_not_called()


async def test_old_success_cannot_clear_new_limit(runtime):
    runtime.pause_for_quota("Claude usage limit reached", "opus", now=100)

    async def probe(model):
        runtime.pause_for_quota(
            "Claude usage limit reached|2000000000", model, now=4000
        )
        return True, ""

    recovery = QuotaRecovery(
        runtime,
        container_id="container",
        dispatcher=Mock(),
        probe=probe,
        clock=lambda: 4000,
    )
    assert not await recovery.tick()
    assert runtime.quota_status()["state"] == "quota_paused"


@pytest.mark.parametrize("phase", ["review", "in_progress"])
def test_quota_completion_preserves_stage_session_and_review_budget(runtime, phase):
    task = {
        "id": "task",
        "status": phase,
        "assigned_agent": "analyst",
        "reviewer": "auditor",
        "execution_cycle": 2,
        "execution_generation": 4,
        "review_retry_epoch": 1,
    }
    event = {
        "session_id": "saved-session",
        "is_review_completion": phase == "review",
        "_caller": {
            "task_id": "task",
            "agent_name": "auditor" if phase == "review" else "analyst",
            "execution_cycle": 2,
            "execution_generation": 4,
            "review_retry_epoch": 1,
        },
    }
    assert defer_quota_task(runtime, task, event)
    assert runtime.quota_session(task) == "saved-session"
    assert runtime.review_state("task", 2, "reviewer", epoch=1)["failures"] == 0
    assert not defer_quota_task(runtime, {**task, "execution_generation": 5}, event)
    assert runtime.quota_session({**task, "execution_cycle": 3}) is None
    assert not defer_quota_task(runtime, {**task, "status": "blocked"}, event)


@pytest.mark.parametrize(
    "caller_change", [{"task_id": "other-task"}, {"agent_name": "other-agent"}]
)
def test_quota_session_cannot_be_saved_for_another_task_or_owner(
    runtime, caller_change
):
    task = {
        "id": "task",
        "status": "in_progress",
        "assigned_agent": "analyst",
        "execution_cycle": 1,
        "execution_generation": 2,
    }
    event = {
        "session_id": "foreign-session",
        "_caller": {
            "task_id": "task",
            "agent_name": "analyst",
            "execution_cycle": 1,
            "execution_generation": 2,
            **caller_change,
        },
    }
    assert not defer_quota_task(runtime, task, event)
    assert runtime.quota_session(task) is None


async def test_different_model_success_does_not_clear_capped_model(runtime):
    runtime.pause_for_quota("Claude usage limit reached", "sonnet", now=100)
    runtime.pause_for_quota("Claude usage limit reached", "opus", now=100)

    async def probe(model):
        return (
            (True, "") if model == "sonnet" else (False, "Claude usage limit reached")
        )

    recovery = QuotaRecovery(
        runtime,
        container_id="container",
        dispatcher=Mock(),
        probe=probe,
        clock=lambda: 4000,
    )
    assert not await recovery.tick()
    assert set(runtime.quota_status()["models"]) == {"opus"}


async def test_paused_watchdog_does_not_consume_crash_retries(runtime):
    from src.watchdog import TaskWatchdog

    runtime.pause_for_quota("Claude usage limit reached", "opus")
    watchdog = TaskWatchdog(
        ws=Mock(),
        executor=None,
        manager=Mock(),
        config_store=Mock(),
        task_queue=None,
        office_id="office-one",
        runtime_state=runtime,
    )
    await watchdog._handle_in_progress(
        {"id": "task", "assigned_agent": "engineer", "status": "in_progress"}
    )
    assert runtime.failure_count("task") == 0


async def test_restart_mid_probe_rechecks_after_lease_not_immediately(runtime):
    state = runtime.pause_for_quota("Claude usage limit reached", "opus", now=100)
    runtime.update_quota(
        state["revision"],
        {**state, "state": "checking_capacity", "next_check_at": 5000},
    )
    restored = RuntimeState(runtime.database_path, "office-one")
    probe = AsyncMock(return_value=(True, ""))
    recovery = QuotaRecovery(
        restored,
        container_id="container",
        dispatcher=Mock(_reconcile_once=AsyncMock()),
        probe=probe,
        clock=lambda: 4999,
    )
    assert not await recovery.tick()
    probe.assert_not_called()
    recovery.clock = lambda: 5000
    assert await recovery.tick()


@pytest.mark.parametrize("phase", ["review", "in_progress"])
async def test_real_worker_runner_releases_quota_in_one_attempt(monkeypatch, phase):
    from src._agent_worker_task import handle_assign_task, run_sdk_session
    from src.agent_worker import AgentErrorEscalation

    from tests.test_worker_auth_escalation import (
        AGENT_CONFIG,
        _fake_worker,
        _patch_stream,
        _task_data,
    )

    worker = _fake_worker()
    message = {**_task_data(), "status": phase, "agent_config": AGENT_CONFIG}
    _patch_stream(
        monkeypatch,
        error="Claude CLI exited with code 1",
        stderr="You've hit your limit · resets Sep 21 at 3am (America/New_York)",
    )
    with pytest.raises(AgentErrorEscalation) as error:
        await run_sdk_session(worker, AGENT_CONFIG, message)
    assert error.value.error_class == "usage_limit_exceeded"
    assert message["_quota_model"] == AGENT_CONFIG["model"]
    assert not any(
        "Recovering from" in str(call) for call in worker._send.call_args_list
    )
    worker._run_sdk_session = AsyncMock(side_effect=error.value)
    await handle_assign_task(worker, message)
    completion = next(
        call.args[0]
        for call in reversed(worker._send.call_args_list)
        if call.args[0].get("type") == "task_complete"
    )
    assert completion["details"]["quota_model"] == AGENT_CONFIG["model"]
    assert "America/New_York" in completion["details"]["usage_limit_error"]
    assert completion.get("is_review_completion", False) == (phase == "review")


async def test_review_reconciliation_does_not_exhaust_budget_on_repeated_quota(
    runtime, monkeypatch
):
    import uuid

    from src.review_completion import reconcile_review_completion

    publish_hold = AsyncMock()
    monkeypatch.setattr("src.review_completion._publish_review_hold", publish_hold)
    task = {
        "id": "task",
        "status": "review",
        "reviewer": "auditor",
        "execution_cycle": 1,
        "execution_generation": 4,
        "review_retry_epoch": 0,
    }
    for _ in range(5):
        event = {
            "task_id": "task",
            "is_review_completion": True,
            "details": {"error_class": "usage_limit_exceeded"},
            "_caller": {
                "execution_cycle": 1,
                "execution_generation": 4,
                "review_retry_epoch": 0,
                "attempt_id": str(uuid.uuid4()),
            },
        }
        assert (
            await reconcile_review_completion(
                task,
                event,
                "auditor",
                runtime_state=runtime,
                platform_url="http://platform",
                office_id="office-one",
                security_token="test",
            )
            == "quota_paused"
        )
    assert runtime.review_state("task", 1, "auditor")["failures"] == 0
    publish_hold.assert_not_awaited()


async def test_automatic_poke_is_safely_deferred_and_context_survives_restart(runtime):
    from src.config_sync.sync_service import ConfigStore
    from src.orchestrator.manager_controller import ManagerController

    controller = ManagerController(
        Mock(), Mock(), Mock(), ConfigStore(), office_id="office-one"
    )
    recovery = QuotaRecovery(runtime, container_id="container", dispatcher=Mock())
    controller.set_quota_recovery(recovery)
    runtime.pause_for_quota("Claude usage limit reached", "opus")
    outcome = {}
    message = {"context_key": "workstream:project", "_turn_outcome": outcome}
    assert not await controller.handle_chat_message(message, source="script")
    assert outcome["safe_to_retry"] is True
    restored = RuntimeState(runtime.database_path, "office-one")
    assert restored.quota_contexts() == ["workstream:project"]


async def test_safe_recovery_notification_failure_survives_restart_and_is_delayed(
    runtime,
):
    from src.config_sync.sync_service import ConfigStore
    from src.orchestrator.manager_controller import ManagerController

    clock = Mock(return_value=100)
    controller = ManagerController(
        Mock(), Mock(), Mock(), ConfigStore(), office_id="office-one"
    )
    recovery = QuotaRecovery(
        runtime, container_id="container", dispatcher=Mock(), clock=clock
    )
    controller.set_quota_recovery(recovery)
    runtime.defer_quota_context("general_chat")

    async def unavailable(message, **kwargs):
        message["_turn_outcome"]["safe_to_retry"] = True
        return False

    controller.handle_chat_message = AsyncMock(side_effect=unavailable)
    await controller._recover_quota_contexts()
    restored = RuntimeState(runtime.database_path, "office-one")
    assert restored.quota_contexts() == ["general_chat"]
    assert restored.quota_contexts(due_at=100) == []
    await controller._recover_quota_contexts()
    assert controller.handle_chat_message.await_count == 1
    assert not getattr(controller, "_pending_pokes", [])

    controller = ManagerController(
        Mock(), Mock(), Mock(), ConfigStore(), office_id="office-one"
    )
    controller.set_quota_recovery(
        QuotaRecovery(
            restored, container_id="container", dispatcher=Mock(), clock=clock
        )
    )
    controller.handle_chat_message = AsyncMock(return_value=True)
    clock.return_value = 700
    await controller._recover_quota_contexts()
    controller.handle_chat_message.assert_awaited_once()
    assert restored.quota_contexts() == []


async def test_ambiguous_recovery_notification_is_not_replayed(runtime):
    from src.config_sync.sync_service import ConfigStore
    from src.orchestrator.manager_controller import ManagerController

    controller = ManagerController(
        Mock(), Mock(), Mock(), ConfigStore(), office_id="office-one"
    )
    controller.set_quota_recovery(
        QuotaRecovery(runtime, container_id="container", dispatcher=Mock())
    )
    runtime.defer_quota_context("general_chat")
    controller.handle_chat_message = AsyncMock(return_value=False)
    await controller._recover_quota_contexts()
    await controller._recover_quota_contexts()
    controller.handle_chat_message.assert_awaited_once()
    assert runtime.quota_contexts() == []


def test_legacy_quota_context_schema_is_upgraded_without_losing_notifications(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE quota_contexts (office_id TEXT, context_key TEXT, PRIMARY KEY (office_id, context_key))"
        )
        connection.execute(
            "INSERT INTO quota_contexts VALUES ('office-one', 'general_chat')"
        )
    restored = RuntimeState(path, "office-one")
    assert restored.quota_contexts(due_at=0) == ["general_chat"]
    restored.delay_quota_context("general_chat", 600)
    assert RuntimeState(path, "office-one").quota_contexts(due_at=599) == []


def test_capacity_probe_rejects_quota_text_even_with_zero_cli_exit(monkeypatch):
    import subprocess

    from src._setup_cli import _probe_claude_works

    monkeypatch.setattr(
        subprocess,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess(
                [], 0, "You've hit your limit · resets 8pm (Europe/Kyiv)", ""
            )
        ),
    )
    errors = []
    assert _probe_claude_works("a" * 64, model="opus", error_sink=errors) is False
    assert classify_error(errors[0]).error_class == ErrorClass.USAGE_LIMIT_EXCEEDED


def test_dst_fold_uses_later_occurrence_and_milliseconds_are_utc():
    now = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
    assert (
        _parse_reset_time(
            "resets Nov 1 at 1:30am (America/New_York)", now=now
        ).isoformat()
        == "2026-11-01T06:30:00+00:00"
    )
    assert (
        _parse_reset_time(
            "Claude usage limit reached|2000000000000", now=now
        ).timestamp()
        == 2000000000
    )


@pytest.mark.parametrize(
    "duration", ["2 hours 30 minutes", "2h 30m", "2 hours and 30 minutes"]
)
def test_compound_relative_reset_keeps_minutes(duration):
    now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    assert (
        _parse_reset_time(f"resets in {duration}", now=now).isoformat()
        == "2026-09-14T14:30:00+00:00"
    )
