"""WP3: the session-start wiring of Workstream Instructions in ``run_sdk_session``.

The prompt tests inject resolved fields directly; these pin the wiring
itself. The session-start ``get_task_detail`` asks for the instructions
(``include_workstream_instructions``), copies the feed fields onto the task
(clearing a stale copy when the backend sends none), and resolves the switch
before the prompt is built. A custom Profile pinned OFF stays off when the
backend sends no text (a failed lookup): the switch never fails open into the
Read pointer. When the backend reports the failed lookup
(``workstream_instructions_unavailable``), a review attempt is aborted for a
retry and triage gets one line and no pointer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src._agent_worker_task import run_sdk_session
from src.agent_worker import AgentErrorEscalation
from src.config_sync.claude_md_templates._workstream import (
    WORKSTREAM_INSTRUCTIONS_OFF_LINE,
    WORKSTREAM_INSTRUCTIONS_UNAVAILABLE_LINE,
)
from src.docker.session_bridge import SessionMessage

NOTES = "### Mission\nShip the pricing page without changing prices."
POINTER_PHRASE = "READ THIS BEFORE STARTING"
PHASE_READ_PHRASE = "Read current Workstream Instructions at"
FEED = {
    "workstream_instructions": NOTES,
    "workstream_instructions_truncated": False,
    "workstream_instructions_chars": len(NOTES),
    "executor_workstream_instructions_enabled": True,
}
CUSTOM_ON = {
    "model": "claude-opus-4-7",
    "agent_type": "custom",
    "workstream_instructions_enabled": True,
}
CUSTOM_OFF = {**CUSTOM_ON, "workstream_instructions_enabled": False}


def _worker(agent_name: str = "web-builder") -> MagicMock:
    worker = MagicMock()
    worker.backend_url = "http://backend.test:8000"
    worker.office_id = "office-1"
    worker.agent_name = agent_name
    worker.workspace_path = "/tmp/cbcl-test-workspace"
    worker._send = MagicMock()
    worker._build_mcp_config = MagicMock(return_value={})
    return worker


def _client(detail: dict) -> tuple[MagicMock, MagicMock]:
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value=detail)
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm), client


def _task(status: str, **extra) -> dict:
    return {
        "task_id": "task-1",
        "readable_id": "WB-001.T01",
        "status": status,
        "workstream_context": {"name": "Website"},
        **extra,
    }


def _detail(status: str, **extra) -> dict:
    return {
        "status": status,
        "assigned_agent": "web-builder",
        "reviewer": "auditor",
        "brief": {
            "goal": "Publish an about page.",
            "inputs": "Add an about page.",
            "acceptance_criteria": ["The page states the mission."],
            "verification_steps": "Execution checks: open the page.",
        },
        **extra,
    }


async def _run(worker, agent_config: dict, task: dict, detail: dict):
    factory, client = _client(detail)
    prompts: list[str] = []

    async def stream(**kwargs):
        prompts.append(kwargs["system_prompt"])
        yield SessionMessage(
            type="result", data={"session_id": "session-1", "cost_usd": 0.01}
        )

    with patch("httpx.AsyncClient", factory), patch(
        "src.docker.session_bridge.stream_cli_session",
        stream,
    ):
        assert await run_sdk_session(worker, agent_config, task) == (
            "session-1",
            0.01,
        )
    assert len(prompts) == 1
    return prompts[0], client


def _request_params(client: MagicMock) -> dict:
    body = client.post.await_args_list[0].kwargs["json"]
    assert body["action"] == "get_task_detail"
    return body["params"]


async def test_session_start_asks_for_and_renders_the_instructions():
    task = _task("in_progress")
    prompt, client = await _run(
        _worker(), CUSTOM_ON, task, _detail("in_progress", **FEED)
    )
    params = _request_params(client)
    assert params == {"task_id": "task-1", "include_workstream_instructions": True}
    assert task["workstream_instructions"] == NOTES
    assert task["workstream_instructions_enabled"] is True
    assert prompt.count("## Workstream Instructions") == 1
    assert NOTES in prompt
    assert POINTER_PHRASE not in prompt and "STEP 0.0" not in prompt


async def test_a_profile_pinned_off_gets_the_off_line_only():
    task = _task("in_progress")
    prompt, _ = await _run(_worker(), CUSTOM_OFF, task, _detail("in_progress", **FEED))
    assert task["workstream_instructions_enabled"] is False
    assert NOTES not in prompt
    assert prompt.count(WORKSTREAM_INSTRUCTIONS_OFF_LINE) == 1
    assert POINTER_PHRASE not in prompt and "STEP 0.0" not in prompt


async def test_a_feed_without_text_clears_a_stale_copy_and_keeps_the_pointer():
    # An older backend (or a failed lookup) sends none of the fields: a copy
    # carried on the queued task must not reach the prompt.
    task = _task("in_progress", **{**FEED, "workstream_instructions": "Stale rule."})
    prompt, _ = await _run(_worker(), CUSTOM_ON, task, _detail("in_progress"))
    for field in FEED:
        assert field not in task
    assert "Stale rule." not in prompt
    assert "## Workstream Instructions" not in prompt
    assert POINTER_PHRASE in prompt


async def test_a_profile_pinned_off_stays_off_without_the_feed():
    task = _task("in_progress")
    prompt, _ = await _run(_worker(), CUSTOM_OFF, task, _detail("in_progress"))
    assert prompt.count(WORKSTREAM_INSTRUCTIONS_OFF_LINE) == 1
    assert POINTER_PHRASE not in prompt and "STEP 0.0" not in prompt


@pytest.mark.parametrize("executor_on", [True, False])
async def test_review_follows_the_executor_from_the_feed(executor_on):
    # The reviewer's own Profile (on) does not decide: the executor's pin does.
    task = _task("review")
    feed = {**FEED, "executor_workstream_instructions_enabled": executor_on}
    prompt, _ = await _run(
        _worker("auditor"),
        {"model": "claude-opus-4-7", "agent_type": "system"},
        task,
        _detail("review", **feed),
    )
    assert task["workstream_instructions_enabled"] is executor_on
    assert (NOTES in prompt) is executor_on
    assert (WORKSTREAM_INSTRUCTIONS_OFF_LINE in prompt) is (not executor_on)


# A new backend that could not read the instructions says so
# (``workstream_instructions_unavailable``); an older backend sends nothing.
UNAVAILABLE = {"workstream_instructions_unavailable": True}
MA_CONFIG = {"model": "sonnet", "agent_type": "system"}


async def test_triage_does_not_fail_open_when_the_lookup_failed():
    # The executor may have had them off: triage gets one line with the real
    # reason, never the pointer to instructions the executor was not given.
    task = _task("blocked")
    prompt, _ = await _run(
        _worker("manager-assistant"), MA_CONFIG, task, _detail("blocked", **UNAVAILABLE)
    )
    assert prompt.count(WORKSTREAM_INSTRUCTIONS_UNAVAILABLE_LINE) == 1
    assert WORKSTREAM_INSTRUCTIONS_OFF_LINE not in prompt
    assert "## Workstream Instructions" not in prompt
    assert POINTER_PHRASE not in prompt and PHASE_READ_PHRASE not in prompt


async def test_triage_on_an_older_backend_keeps_the_pointer():
    task = _task("blocked")
    prompt, _ = await _run(
        _worker("manager-assistant"), MA_CONFIG, task, _detail("blocked")
    )
    assert task["workstream_instructions_unavailable"] is False
    assert POINTER_PHRASE in prompt and PHASE_READ_PHRASE in prompt
    assert WORKSTREAM_INSTRUCTIONS_UNAVAILABLE_LINE not in prompt


@pytest.mark.parametrize(
    ("agent_config", "pointer"), [(CUSTOM_ON, True), (CUSTOM_OFF, False)]
)
async def test_execution_follows_its_own_switch_when_the_lookup_failed(
    agent_config, pointer
):
    # An executor whose switch is on may read the file; one pinned off keeps
    # its off line. Neither renders the review/triage "could not read" line.
    task = _task("in_progress")
    prompt, _ = await _run(
        _worker(), agent_config, task, _detail("in_progress", **UNAVAILABLE)
    )
    assert (POINTER_PHRASE in prompt) is pointer
    assert (WORKSTREAM_INSTRUCTIONS_OFF_LINE in prompt) is (not pointer)
    assert WORKSTREAM_INSTRUCTIONS_UNAVAILABLE_LINE not in prompt


async def test_review_is_aborted_for_a_retry_when_the_lookup_failed():
    # A review judges against what the executor received. The abort is the
    # escalation a failed reviewer session raises, so the orchestrator retries
    # the review instead of holding it for a missing verdict
    # (test_worker_task_brief_abort pins that end to end).
    worker = _worker("auditor")
    task = _task("review")
    factory, _ = _client(_detail("review", **UNAVAILABLE))
    stream = MagicMock()
    with patch("httpx.AsyncClient", factory), patch(
        "src.docker.session_bridge.stream_cli_session", stream
    ):
        with pytest.raises(AgentErrorEscalation) as abort:
            await run_sdk_session(
                worker, {"model": "claude-opus-4-7", "agent_type": "system"}, task
            )
    assert abort.value.error_class == "workstream_instructions_unavailable"
    stream.assert_not_called()
    [event] = [
        call.args[0]
        for call in worker._send.call_args_list
        if call.args[0].get("event_type") == "error"
    ]
    assert event["details"] == {
        "error_class": "workstream_instructions_unavailable",
        "retryable": True,
    }
    assert "workstream instructions" in event["content"]
