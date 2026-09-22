"""Sibling isolation, attempt fencing and honest snapshot freshness."""

import json
from unittest.mock import MagicMock, patch

import fakeredis.aioredis
import pytest

from src._handlers._agent_feed import push_agent_feed
from src._handlers._instance_telemetry import worker_status_events
from src.health.reporter import HealthReporter
from tests.test_review_circuit_breaker import build_harness, _httpx_mock


def caller(task="task-1", instance="instance-1", attempt="attempt-1", generation=1):
    return {
        "role": "worker",
        "agent_name": "builder",
        "profile_id": "profile-1",
        "agent_instance_id": instance,
        "task_id": task,
        "attempt_id": attempt,
        "execution_generation": generation,
        "execution_cycle": 1,
        "task_mode": "execute",
    }


def instance(identity=None, **overrides):
    identity = identity or caller()
    return {
        **identity,
        "execution_mode": identity["task_mode"],
        "status": "working",
        "current_task": identity["task_id"],
        "observed_at": 100.0,
        **overrides,
    }


def supervisor(states):
    value = MagicMock()
    value.get_instance_statuses.return_value = states
    value.get_all_statuses.return_value = {}
    return value


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.paths.get_runtime_state_path", lambda: tmp_path / "runtime.sqlite3"
    )
    monkeypatch.setattr("src.runtime_state._generation_controls", {})


def test_completion_preserves_working_sibling_and_original_observation():
    other = caller("task-2", "instance-2", "attempt-2")
    events = worker_status_events(
        "builder",
        {"_caller": caller()},
        "idle",
        supervisor(
            {
                "instance-1": instance(),
                "instance-2": instance(other),
            }
        ),
    )
    assert events[0]["agent_instance_id"] == "instance-1"
    assert events[0]["status"] == "idle"
    assert events[0]["observed_at"] == 100.0
    assert events[1]["status"] == "working"
    assert events[1]["current_task"] == "task-2"
    assert events[1]["running_count"] == 1


def test_late_attempt_cannot_clear_newer_attempt_profile_summary():
    current = caller(attempt="attempt-3", generation=3)
    events = worker_status_events(
        "builder",
        {"_caller": caller(), "observed_at": 10.0},
        "idle",
        supervisor(
            {
                "instance-1": instance(current),
            }
        ),
    )
    assert events[0]["execution_generation"] == 1
    assert events[0]["observed_at"] == 10.0
    assert events[1]["status"] == "working"
    assert events[1]["current_task"] == "task-1"


def test_two_working_siblings_have_no_ambiguous_profile_task():
    other = caller("task-2", "instance-2", "attempt-2")
    events = worker_status_events(
        "builder",
        {"_caller": caller()},
        "working",
        supervisor(
            {
                "instance-1": instance(),
                "instance-2": instance(other),
            }
        ),
    )
    assert events[1]["current_task"] is None
    assert events[1]["running_count"] == 2


def test_older_observation_in_same_attempt_cannot_replace_current_summary():
    events = worker_status_events(
        "builder",
        {"_caller": caller(), "observed_at": 50.0},
        "idle",
        supervisor(
            {
                "instance-1": instance(),
            }
        ),
    )
    assert events[0]["observed_at"] == 50.0
    assert events[1]["status"] == "working"


@pytest.mark.asyncio
async def test_health_keeps_stale_observation_and_requires_applied_config():
    sup = supervisor(
        {"instance-1": instance(status="crashed", execution_cleanup_pending=True)}
    )
    sup.config_ready = False
    sup.execution_policy = {
        "enabled": True,
        "max_workers": 4,
        "max_workers_per_profile": 2,
    }
    health = HealthReporter(redis=None, office_id="office-1", supervisor=sup)
    report = await health._build_report()
    assert "dynamic_agents_v1" not in report["capabilities"]
    assert "agent_execution_policy" not in report
    assert report["agent_instances"]["instance-1"]["observed_at"] == 100.0
    assert report["agent_instances"]["instance-1"]["status"] == "error"
    assert report["agent_instances"]["instance-1"]["execution_cleanup_pending"] is True
    sup.config_ready = True
    report = await health._build_report()
    assert "dynamic_agents_v1" in report["capabilities"]
    assert report["agent_execution_policy"] == sup.execution_policy
    assert report["agent_instances"]["instance-1"]["observed_at"] == 100.0


@pytest.mark.asyncio
async def test_dynamic_feeds_keep_exact_task_identity_and_aggregate_profile():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    sup = supervisor({})
    sup._agents = {
        "instance-1": MagicMock(
            current_readable_id="DEV-001.T01", current_task_id="task-1"
        ),
        "instance-2": MagicMock(
            current_readable_id="DEV-001.T02", current_task_id="task-2"
        ),
        "builder": MagicMock(current_readable_id="WRONG", current_task_id="wrong-task"),
    }
    try:
        for identity in (caller(), caller("task-2", "instance-2", "attempt-2")):
            await push_agent_feed(
                "builder",
                {"type": "progress", "_caller": identity},
                office_id="office-1",
                redis_client=redis,
                supervisor=sup,
            )
        rows = [
            json.loads(row)
            for row in await redis.lrange("office:office-1:agent_feed:builder", 0, -1)
        ]
        assert {row["task_id"] for row in rows} == {"task-1", "task-2"}
        first = json.loads(
            (await redis.lrange("office:office-1:agent_feed:instance-1", 0, -1))[0]
        )
        assert first["agent_name"] == "builder"
        assert first["profile_id"] == "profile-1"
        assert first["attempt_id"] == "attempt-1"
        assert first["readable_id"] == "DEV-001.T01"
        await push_agent_feed(
            "builder",
            {"type": "progress", "_caller": caller("task-3", "missing", "attempt-3")},
            office_id="office-1",
            redis_client=redis,
            supervisor=sup,
        )
        missing = json.loads(
            (await redis.lrange("office:office-1:agent_feed:missing", 0, -1))[0]
        )
        assert missing["readable_id"] == ""
        assert missing["task_id"] == "task-3"
    finally:
        await redis.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        {"type": "task_complete", "status": "in_progress", "execution_deferred": True},
        {
            "type": "task_complete",
            "status": "in_progress",
            "details": {"cancellation_source": "shutdown"},
        },
        {"type": "error", "fatal": True},
    ],
)
async def test_dynamic_cleanup_carries_exact_attempt_for_cas(event):
    harness = await build_harness()
    harness.supervisor.get_instance_statuses.return_value = {"instance-1": instance()}
    client, client_class = _httpx_mock({"id": "task-1", "status": "done"})
    with patch("httpx.AsyncClient", client_class):
        await harness.on_event("builder", {**event, "_caller": caller()})
    harness.queue_manager.clear_active.assert_awaited_once_with(
        "builder", "task-1", expected_attempt_id="attempt-1"
    )
    if event["type"] == "error":
        harness.queue_manager.get_active.assert_awaited_once_with("builder", "task-1")


@pytest.mark.asyncio
async def test_completion_dispatch_and_live_telemetry_keep_exact_identity():
    harness = await build_harness()
    harness.supervisor.get_instance_statuses.return_value = {"instance-1": instance()}
    client, client_class = _httpx_mock({"id": "task-1", "status": "done"})
    with patch("httpx.AsyncClient", client_class):
        await harness.on_event(
            "builder", {"type": "task_complete", "status": "done", "_caller": caller()}
        )
    harness.dispatcher.on_agent_complete.assert_awaited_once_with(
        "builder", "task-1", "attempt-1"
    )
    updates = [call.args[0] for call in harness.router.publish_event.await_args_list]
    status = next(
        update
        for update in updates
        if update["type"] == "agent_instance_status_changed"
    )
    assert status["task_id"] == "task-1"
    assert status["attempt_id"] == "attempt-1"
    assert status["status"] == "idle"
