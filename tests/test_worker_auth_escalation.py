"""Worker escalation carries the RICH error text (owner incident 2026-08).

When the container CLI's OAuth login dies, the useful wording ("Failed
to authenticate: OAuth session expired and could not be refreshed")
arrives on STDERR while the ``error`` frame's text is the synthetic
"Claude CLI exited with code 1". The escalation used to stamp the
synthetic line into ``original_error`` — so the ESCALATED comment the
backend keyword-routes on carried no auth wording and the blocker
landed in category=workstream (Manager auto-decide) instead of
credentials (user Inbox).

Contract under test (``_agent_worker_task.run_sdk_session``):

* the classification input (api error > stderr > synthetic line) is
  ALSO what lands in ``AgentErrorEscalation.original_error``;
* the OAuth expiry classifies ``auth_failed`` (non-retryable — no
  doomed fresh-session retries) and the escalation message names the
  Settings sign-in fix.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src._agent_worker_task import run_sdk_session
from src.agent_worker import AgentErrorEscalation
from src.docker import session_bridge
from src.docker.session_bridge import SessionMessage

OAUTH_STDERR = (
    "Failed to authenticate: OAuth session expired and could not "
    "be refreshed"
)


def _fake_worker() -> MagicMock:
    worker = MagicMock()
    worker.backend_url = ""
    worker.office_id = "office-1"
    worker.agent_name = "analyst"
    worker.workspace_path = "/tmp/cbcl-test-workspace"
    worker._send = MagicMock()
    worker._build_mcp_config = MagicMock(return_value={})
    return worker


def _task_data() -> dict:
    return {
        "task_id": "task-1",
        "readable_id": "WR-001.T01",
        "status": "ready",
        "brief": {"goal": "Ship the thing"},
        "agent_config": {},
    }


AGENT_CONFIG = {
    "_container_name": "cbcl-office-test",
    "model": "claude-opus-4-7",
}


def _patch_stream(monkeypatch, *, error: str, stderr: str) -> None:
    def factory(**kwargs):
        async def agen():
            yield SessionMessage(
                type="error", data={"error": error, "stderr": stderr},
            )

        return agen()

    monkeypatch.setattr(session_bridge, "stream_cli_session", factory)


@pytest.mark.asyncio
async def test_oauth_expiry_escalation_carries_stderr_not_synthetic_line(
    monkeypatch,
):
    worker = _fake_worker()
    _patch_stream(
        monkeypatch,
        error="Claude CLI exited with code 1",
        stderr=OAUTH_STDERR,
    )

    with pytest.raises(AgentErrorEscalation) as exc:
        await run_sdk_session(worker, AGENT_CONFIG, _task_data())

    # auth_failed is non-retryable — ONE attempt, no doomed
    # fresh-session retry burn.
    assert exc.value.error_class == "auth_failed"
    # The ESCALATED comment embeds original_error — it must carry the
    # OAuth wording (what the backend keyword router + MA triage read),
    # not just the synthetic exit line.
    assert "OAuth session expired" in exc.value.original_error
    # And the escalation names the user-side fix.
    assert "sign-in" in exc.value.escalation_message.lower()


@pytest.mark.asyncio
async def test_error_frame_text_still_used_when_no_stderr(monkeypatch):
    """Fallback intact: with no stderr/api enrichment the synthetic
    line remains the original_error (never empty)."""
    worker = _fake_worker()
    _patch_stream(
        monkeypatch, error="Claude CLI exited with code 1", stderr="",
    )

    with pytest.raises(AgentErrorEscalation) as exc:
        await run_sdk_session(worker, AGENT_CONFIG, _task_data())

    assert exc.value.error_class == "unknown_fatal"
    assert "exited with code 1" in exc.value.original_error
