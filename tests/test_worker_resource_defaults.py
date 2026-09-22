"""Advisory Profile tools must never bypass conservative workspace admission."""

import pytest
from unittest.mock import MagicMock

from src.agent_execution_policy import execution_resources


@pytest.mark.parametrize("task", [{}, {"execution_resources": None}])
def test_omitted_or_null_resources_reserve_workspace(task):
    assert execution_resources(task) == ["shared-workspace"]


@pytest.mark.parametrize("declared", [[], ["checkout:task", "database:test"]])
def test_explicit_independence_and_named_reservations_are_preserved(declared):
    assert execution_resources({"execution_resources": declared}) == declared


def test_authoritative_receipt_projection_preserves_pinned_resource_contract():
    # Do not rewrite a known attempt's explicit resources during recovery.
    assert execution_resources({
        "execution_resources": None,
        "effective_execution_resources": [],
    }) == []


async def test_read_profile_remains_advisory_at_real_worker_session_boundary(monkeypatch):
    from src._agent_worker_task import run_sdk_session
    from src.docker import session_bridge
    from src.docker.session_bridge import SessionMessage

    worker = MagicMock()
    worker.backend_url = ""
    worker.office_id = "office"
    worker.agent_name = "reader"
    worker.workspace_path = "/tmp/cbcl-test-workspace"
    worker._build_mcp_config.return_value = {}
    calls = []

    async def stream(**kwargs):
        calls.append(kwargs)
        yield SessionMessage(type="result", data={"session_id": "session", "cost_usd": 0})

    monkeypatch.setattr(session_bridge, "stream_cli_session", stream)
    profile = {"model": "sonnet", "allowed_tools": ["Read"], "_container_name": "synthetic"}
    task = {"task_id": "task", "status": "ready", "brief": {"goal": "Read a synthetic artifact"}}
    session_id, _ = await run_sdk_session(worker, profile, task)
    assert session_id == "session"
    assert calls[0]["allowed_tools"] is None
    assert "Bash" not in calls[0]["disallowed_tools"]
    assert "TaskCreate" in calls[0]["disallowed_tools"]
    assert execution_resources(task) == ["shared-workspace"]
