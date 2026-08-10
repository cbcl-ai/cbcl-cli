"""WRK-09: the worker MCP env must carry TASK_READABLE_ID so the triage guard
can match a blocked-task move whether the MA passes the UUID or the readable
id (RC-001.T14). Without it the readable form bypasses the triage lock."""
from __future__ import annotations

from types import SimpleNamespace

from src._agent_worker_mcp import build_mcp_config


def _worker():
    return SimpleNamespace(
        backend_url="http://host.docker.internal:8000",
        office_id="ofc-1",
        agent_name="manager-assistant",
    )


def _env(cfg: dict) -> dict:
    return cfg["mcpServers"]["cubicle-tools"]["env"]


def test_task_readable_id_present_in_worker_mcp_env():
    cfg = build_mcp_config(
        _worker(),
        "worker",
        task_id="11111111-1111-1111-1111-111111111111",
        task_mode="triage",
        task_readable_id="RC-001.T14",
    )
    env = _env(cfg)
    assert env.get("TASK_ID") == "11111111-1111-1111-1111-111111111111"
    assert env.get("TASK_READABLE_ID") == "RC-001.T14"


def test_task_readable_id_omitted_when_absent():
    cfg = build_mcp_config(_worker(), "worker", task_id="abc", task_mode="execute")
    assert "TASK_READABLE_ID" not in _env(cfg)


def test_consult_refire_env_threaded_for_refired_consults():
    """Bubble honesty (owner directive 2026-08-04): a refired Planner
    consult session carries CONSULT_REFIRE=1 so the in-container MCP
    server stamps ``_caller.consult_refire`` and the backend's
    planner_completed bubbles can say "re-run after interruption"."""
    cfg = build_mcp_config(
        _worker(), "worker", task_id="planner-abc",
        task_mode="execute", consult_refire=True,
    )
    assert _env(cfg).get("CONSULT_REFIRE") == "1"


def test_consult_refire_env_absent_by_default():
    cfg = build_mcp_config(
        _worker(), "worker", task_id="planner-abc", task_mode="execute",
    )
    assert "CONSULT_REFIRE" not in _env(cfg)
