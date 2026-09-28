"""T2.2.3 — per-agent office-secret allowlist filter.

Verifies the pure ``apply_secret_env_allowlist`` helper that the worker
task runner uses to scope which office secrets reach an agent's session
env. Default (``None``) must be byte-identical to today's "inject all"
behaviour so the user-mandated model is preserved. Office secrets named
like Claude sign-in settings never reach a session (subscription-only).
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock
from uuid import UUID

from src._agent_worker_task import (
    apply_secret_env_allowlist,
    drop_reserved_claude_env,
    run_sdk_session,
)
from src.docker import session_bridge
from src.docker.session_bridge import SessionMessage

_ENV = {
    "OPENAI_API_KEY": "sk-a",
    "GITLAB_PAT": "glpat-b",
    "SLACK_BOT_TOKEN": "xoxb-c",
}


def test_none_allowlist_injects_all_unchanged():
    # The default: no allowlist → identical dict, same values.
    out = apply_secret_env_allowlist(_ENV, None)
    assert out == _ENV


def test_empty_allowlist_injects_none():
    out = apply_secret_env_allowlist(_ENV, [])
    assert out == {}


def test_named_allowlist_filters_to_listed():
    out = apply_secret_env_allowlist(_ENV, ["OPENAI_API_KEY", "GITLAB_PAT"])
    assert out == {"OPENAI_API_KEY": "sk-a", "GITLAB_PAT": "glpat-b"}


def test_allowlist_name_not_in_store_is_skipped():
    # Listing a name the office doesn't have just yields nothing extra.
    out = apply_secret_env_allowlist(_ENV, ["DOES_NOT_EXIST"])
    assert out == {}


def test_allowlist_does_not_mutate_input():
    original = dict(_ENV)
    apply_secret_env_allowlist(_ENV, ["GITLAB_PAT"])
    assert _ENV == original


def test_empty_env_with_any_allowlist_is_empty():
    assert apply_secret_env_allowlist({}, ["OPENAI_API_KEY"]) == {}
    assert apply_secret_env_allowlist({}, None) == {}


# ── Subscription-only: Claude sign-in names never reach a session ────────

_WITH_RESERVED = {
    **_ENV,
    "ANTHROPIC_API_KEY": "sk-ant-api-x",
    "ANTHROPIC_AUTH_TOKEN": "bearer-x",
    "ANTHROPIC_BASE_URL": "https://proxy.example",
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth-x",
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "CLAUDE_CODE_USE_VERTEX": "1",
    "CLAUDE_CODE_USE_FOUNDRY": "1",
    "CLAUDE_CONFIG_DIR": "/workspace/other",
}


def test_reserved_claude_names_are_dropped_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="src._agent_worker_task"):
        out = drop_reserved_claude_env(_WITH_RESERVED)
    assert out == _ENV
    warning = caplog.text
    assert "ANTHROPIC_API_KEY" in warning and "CLAUDE_CODE_USE_BEDROCK" in warning
    for value in ("sk-ant-api-x", "bearer-x", "oauth-x"):
        assert value not in warning


def test_no_reserved_names_returns_the_same_env():
    assert drop_reserved_claude_env(_ENV) is _ENV


async def test_session_never_receives_reserved_claude_names(tmp_path, monkeypatch):
    """Even a stored ANTHROPIC_API_KEY that an allowlist names stays out."""
    monkeypatch.setattr(
        "src.office_secrets.store.read_office_secrets",
        lambda _slug: dict(_WITH_RESERVED),
    )
    worker = MagicMock()
    worker.backend_url = ""
    worker.office_id = "synthetic-office"
    worker.agent_name = "analyst"
    worker.workspace_path = str(tmp_path / "synthetic-office")
    worker._build_mcp_config.return_value = {}
    sessions: list[dict] = []

    async def stream(**kwargs):
        sessions.append(kwargs)
        yield SessionMessage(type="result", data={"session_id": "s1", "cost_usd": 0})

    monkeypatch.setattr(session_bridge, "stream_cli_session", stream)
    profile = {
        "name": "analyst",
        "agent_type": "custom",
        "model": "sonnet",
        "_container_name": "synthetic-container",
        "secret_env_allowlist": ["ANTHROPIC_API_KEY", "GITLAB_PAT"],
    }
    task = {
        "task_id": str(UUID(int=1)),
        "readable_id": "PR-001.T01",
        "status": "in_progress",
        "assigned_agent": "analyst",
        "workstream_context": {"name": "Project"},
        "brief": {"goal": "Validate the synthetic result"},
    }
    await run_sdk_session(worker, profile, task)
    assert sessions and sessions[0]["secret_env"] == {"GITLAB_PAT": "glpat-b"}
