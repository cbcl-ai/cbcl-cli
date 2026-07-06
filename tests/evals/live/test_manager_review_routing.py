"""Live eval: Manager routes review work to a non-executor (P6.1).

EVAL-02: drives the REAL production Manager prompt (via
``render_production_manager_prompt``) with two agents on the roster, and
asserts the emitted ``create_task`` payload splits executor vs reviewer — the
common regression where a Manager prompt rewrite drops the "reviewer must
differ from the executor" rule.
"""
from __future__ import annotations

import json
import re

import pytest

from tests.evals.live._harness import (
    call_claude,
    render_production_manager_prompt,
)


pytestmark = pytest.mark.live_eval


_FIXTURE_CTX = {
    "office_name": "Acme Web",
    "workstream_id": "11111111-1111-1111-1111-111111111111",
    "workstream_name": "Backend",
    "workstream_priority": "high",
    "workstream_description": "FastAPI backend work.",
    "workstream_goals": "Ship the API.",
    "team_roster": (
        "**Senior Python Developer** (python-developer) — 👩‍💻\n"
        "**Auditor** (auditor) — 📋"
    ),
    "board_summary": {},
    "scopes": [],
}

_EVAL_SUFFIX = (
    "## Eval mode\n"
    "This request comes over an API without tools. Produce the `create_task` "
    "payload as a single ```json fenced object that includes BOTH "
    "`assigned_agent` (the EXECUTOR) and `reviewer` (a DIFFERENT agent) — an "
    "agent never reviews its own work. No other prose."
)

_SYSTEM_PROMPT = render_production_manager_prompt(
    "workstream:11111111-1111-1111-1111-111111111111",
    _FIXTURE_CTX,
    eval_json_suffix=_EVAL_SUFFIX,
)

_REQUEST = "Add a /healthz endpoint to our FastAPI app."


def _extract_json(text: str) -> dict:
    m = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
    payload = m.group(1) if m else text
    return json.loads(payload)


async def test_reviewer_differs_from_executor() -> None:
    resp = await call_claude(
        system=_SYSTEM_PROMPT,
        user=_REQUEST,
        max_tokens=600,
    )
    brief = _extract_json(resp.text)
    assigned = brief.get("assigned_agent")
    reviewer = brief.get("reviewer")
    assert assigned, f"Brief missing assigned_agent: {brief}"
    assert reviewer, f"Brief missing reviewer: {brief}"
    assert assigned != reviewer, (
        f"Manager assigned the SAME agent as executor + reviewer "
        f"({assigned!r}). Plan violation."
    )
