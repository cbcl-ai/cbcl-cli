"""T4.3.3 — startup reap of orphan agent CLI sessions (07/G12)."""
from __future__ import annotations

import asyncio
import pytest

from src import recovery


class _FakeProc:
    def __init__(self, rc):
        self.returncode = rc

    async def communicate(self):
        return (b"", b"")


@pytest.mark.asyncio
async def test_reap_issues_pkill_claude_print(monkeypatch):
    calls = {}

    async def _fake_exec(*args, **kw):
        calls["args"] = args
        return _FakeProc(0)

    monkeypatch.setattr(recovery.asyncio, "create_subprocess_exec", _fake_exec)
    rc = await recovery.reap_orphan_agent_sessions("cbcl-office-x")
    assert rc == 0
    # Exact, script-SAFE pattern: docker exec <name> pkill -f 'claude --print'
    assert calls["args"][:3] == ("docker", "exec", "cbcl-office-x")
    assert calls["args"][3:5] == ("pkill", "-f")
    assert calls["args"][5] == "claude --print"


@pytest.mark.asyncio
async def test_reap_pattern_cannot_match_script_subprocesses():
    # Scripts run as `python -m ... main.py`; the reap pattern must not match.
    assert "python" not in recovery._AGENT_CLI_REAP_PATTERN
    assert recovery._AGENT_CLI_REAP_PATTERN == "claude --print"


@pytest.mark.asyncio
async def test_reap_no_match_is_healthy(monkeypatch):
    async def _fake_exec(*args, **kw):
        return _FakeProc(1)  # pkill rc=1 → nothing matched

    monkeypatch.setattr(recovery.asyncio, "create_subprocess_exec", _fake_exec)
    assert await recovery.reap_orphan_agent_sessions("cbcl-office-x") == 1


@pytest.mark.asyncio
async def test_reap_container_down_logs_warning(monkeypatch, caplog):
    # docker exec rc=126 (container not running) must NOT be reported as a
    # clean "nothing to reap" — it means the reap did not actually run.
    async def _fake_exec(*args, **kw):
        return _FakeProc(126)

    monkeypatch.setattr(recovery.asyncio, "create_subprocess_exec", _fake_exec)
    import logging
    with caplog.at_level(logging.WARNING):
        rc = await recovery.reap_orphan_agent_sessions("cbcl-office-x")
    assert rc == 126
    assert any(
        "could not run" in r.message and r.levelno >= logging.WARNING
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_reap_swallows_docker_failure(monkeypatch):
    async def _boom(*args, **kw):
        raise FileNotFoundError("docker not found")

    monkeypatch.setattr(recovery.asyncio, "create_subprocess_exec", _boom)
    assert await recovery.reap_orphan_agent_sessions("x") == -1
