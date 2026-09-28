"""C4c-G4: every Cubicle tool reaches the model with its full description.

The pinned Claude CLI (2.1.259, bundled with claude-agent-sdk 0.2.152) runs
in tool-search mode and defers every MCP tool that is not marked
``alwaysLoad``: the model sees only tool names until it calls ToolSearch, so
the when-not-to-use rules, verdict shapes and turn-ending notes in the
Cubicle descriptions are absent while it chooses a tool. The server-level
``alwaysLoad`` key on the single ``--mcp-config`` entry loads them all.

Re-verify on any CLI bump: run the bundled binary against a stub MCP server
and a request-capturing fake API; a tool of an ``alwaysLoad`` server must be
sent in ``tools[]`` with its description, not listed as deferred.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src._agent_worker_mcp import build_mcp_config


def _worker(agent_name: str = "builder"):
    return SimpleNamespace(
        backend_url="http://host.docker.internal:8000",
        office_id="ofc-1",
        agent_name=agent_name,
    )


@pytest.mark.parametrize(
    "role,agent_name,task_mode,context_key",
    [
        ("manager", "", "manager", "general_chat"),
        ("manager", "", "manager", "workstream:abc"),
        ("worker", "builder", "execute", None),
        ("worker", "auditor", "review", None),
        ("worker", "manager-assistant", "triage", None),
        ("worker", "planner", "execute", None),
        ("worker", "flow-architect", "execute", None),
    ],
)
def test_cubicle_tools_server_is_always_loaded(role, agent_name, task_mode, context_key):
    config = build_mcp_config(
        _worker(agent_name), role, task_id="t-1", task_mode=task_mode,
        context_key=context_key,
    )
    servers = config["mcpServers"]
    # The only server this config carries is Cubicle's own; third-party
    # connectors keep the CLI's default deferral (they are configured in
    # ~/.claude.json, not here).
    assert set(servers) == {"cubicle-tools"}
    assert servers["cubicle-tools"]["alwaysLoad"] is True
    assert servers["cubicle-tools"]["type"] == "stdio"
