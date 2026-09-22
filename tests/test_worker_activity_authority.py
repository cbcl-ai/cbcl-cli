"""Worker IPC cannot manufacture trusted operation or transition evidence."""

import pytest

from tests.test_review_circuit_breaker import build_harness


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.paths.get_runtime_state_path", lambda: tmp_path / "runtime.sqlite3"
    )
    monkeypatch.setattr("src.runtime_state._generation_controls", {})


@pytest.mark.parametrize(
    "payload",
    [
        {"event_type": "execution_progress"},
        {"details": {"execution_event": {"state": "succeeded"}}},
        {"details": {"execution_source": "communicator"}},
        {"details": {"move_invocation": {"new_status": "done"}}},
    ],
)
async def test_real_worker_callback_rejects_reserved_proof(payload):
    harness = await build_harness()
    harness.router.publish_event.reset_mock()
    await harness.on_event(
        "builder",
        {"type": "progress", "task_id": "task-1", "content": "done", **payload},
    )
    harness.router.publish_event.assert_not_awaited()


async def test_real_worker_callback_preserves_normal_progress_and_attested_identity():
    harness = await build_harness()
    identity = {"task_id": "task-1", "attempt_id": "attempt-1", "role": "worker"}
    await harness.on_event(
        "builder",
        {
            "type": "progress",
            "task_id": "task-1",
            "actor": "script-runner",
            "content": "Inspected report sources",
            "details": {"tool": "Read", "summary": "Source report"},
            "_caller": identity,
        },
    )
    activities = [
        call.args[0]
        for call in harness.router.publish_event.await_args_list
        if call.args[0].get("type") == "task_activity"
    ]
    assert len(activities) == 1
    assert activities[0]["actor"] == "builder"
    assert activities[0]["event_type"] == "checkpoint"
    assert activities[0]["_caller"] == identity
    assert activities[0]["details"] == {"tool": "Read", "summary": "Source report"}
