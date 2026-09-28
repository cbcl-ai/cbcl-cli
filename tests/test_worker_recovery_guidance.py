"""C4c-G9 / C4c-G10: recovery guidance and narration are never silently lost.

* A base worker prompt larger than the system-prompt cap used to make every
  retry drop its ``AUTOMATIC RECOVERY`` block. The cap now bounds growth
  across retries only: the newest remedy is always kept, older blocks are
  rotated out oldest-first, and the base prompt is never cut.
* A streamed narration block longer than the checkpoint excerpt is cut with
  an explicit marker instead of reappearing in later sessions as complete.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src._agent_worker_task import (
    _RECOVERY_MARKER,
    _append_recovery_guidance,
    _narration_excerpt,
    run_sdk_session,
)
from src.docker import session_bridge
from src.docker.session_bridge import SessionMessage


def _block(text: str) -> str:
    return f"{_RECOVERY_MARKER}## AUTOMATIC RECOVERY — READ THIS\n{text}"


def test_guidance_appends_under_the_cap():
    prompt, dropped = _append_recovery_guidance("base", "base", _block("one"), cap=200)
    assert prompt == "base" + _block("one")
    assert dropped == 0


def test_oversized_base_prompt_still_gets_the_newest_remedy():
    base = "B" * 300  # alone over the cap
    first, _ = _append_recovery_guidance(base, base, _block("first"), cap=200)
    assert first == base + _block("first")
    second, dropped = _append_recovery_guidance(first, base, _block("second"), cap=200)
    # At most one block is kept: the newest; the base is never cut.
    assert second == base + _block("second")
    assert dropped == 1


def test_rotation_drops_oldest_blocks_first_and_never_touches_the_base():
    # A brief that happens to contain the marker text must not be rotated.
    base = "brief" + _RECOVERY_MARKER + "quoted marker in the brief"
    blocks = [_block(f"guidance-{index}" + "x" * 40) for index in range(3)]
    prompt = base + "".join(blocks)
    newest = _block("guidance-new")
    cap = len(base) + len(blocks[2]) + len(newest)
    result, dropped = _append_recovery_guidance(prompt, base, newest, cap=cap)
    assert result == base + blocks[2] + newest
    assert dropped == 2


def test_narration_excerpt_marks_a_cut():
    assert _narration_excerpt("short") == "short"
    exact = "y" * 500
    assert _narration_excerpt(exact) == exact
    long_text = "z" * 1200
    excerpt = _narration_excerpt(long_text)
    assert excerpt.endswith(" …(truncated)")
    assert excerpt.startswith("z" * 500)


def _fake_worker() -> MagicMock:
    worker = MagicMock()
    worker.backend_url = ""
    worker.office_id = "office-1"
    worker.agent_name = "analyst"
    worker.workspace_path = "/tmp/cbcl-test-workspace"
    worker._send = MagicMock()
    worker._build_mcp_config = MagicMock(return_value={})
    return worker


@pytest.mark.asyncio
async def test_streamed_long_narration_checkpoint_is_marked(monkeypatch):
    text = "Narration " + "n" * 1190

    def factory(**kwargs):
        async def stream():
            yield SessionMessage(
                type="assistant",
                data={"message": {"content": [{"type": "text", "text": text}]}},
            )
            yield SessionMessage(
                type="result", data={"session_id": "sess-1", "cost_usd": 0.01}
            )

        return stream()

    monkeypatch.setattr(session_bridge, "stream_cli_session", factory)
    worker = _fake_worker()
    task = {
        "task_id": "task-1",
        "readable_id": "WR-001.T01",
        "status": "ready",
        "brief": {"goal": "Ship the thing"},
        "agent_config": {},
    }
    await run_sdk_session(
        worker,
        {"_container_name": "cbcl-office-test", "model": "claude-opus-4-7"},
        task,
    )
    checkpoints = [
        frame
        for frame in (call.args[0] for call in worker._send.call_args_list)
        if frame.get("event_type") == "checkpoint"
        and (frame.get("content") or "").startswith("Narration")
    ]
    assert len(checkpoints) == 1
    assert checkpoints[0]["content"] == text[:500] + " …(truncated)"
