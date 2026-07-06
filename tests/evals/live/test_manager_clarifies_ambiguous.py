"""Live eval: Manager clarifies vague requests instead of guessing (P6.1).

EVAL-02: drives the REAL production Manager prompt (via
``render_production_manager_prompt``). A clear request → emit a Brief; an
ambiguous one (not enough detail to write acceptance criteria) → ask
clarifying questions FIRST. Catches the regression where a Manager prompt
rewrite makes the model hallucinate detail rather than asking the user.
"""
from __future__ import annotations

import pytest

from tests.evals.live._harness import (
    call_claude,
    render_production_manager_prompt,
)


pytestmark = pytest.mark.live_eval


_FIXTURE_CTX = {
    "office_name": "Acme Web",
    "workstream_id": "11111111-1111-1111-1111-111111111111",
    "workstream_name": "General Improvements",
    "workstream_priority": "medium",
    "workstream_description": "Miscellaneous product work.",
    "workstream_goals": "Improve the product.",
    "team_roster": "**Manager Assistant** (manager-assistant) — ⚡",
    "board_summary": {},
    "scopes": [],
}

_EVAL_SUFFIX = (
    "## Eval mode\n"
    "This request comes over an API without tools. If the request is CLEAR "
    "enough to write all 9 Brief fields, produce the `create_task` payload as "
    "a ```json fenced block. If it is AMBIGUOUS (you'd have to invent "
    "acceptance criteria), respond with PLAIN TEXT asking 1-3 clarifying "
    "questions and NO ```json block."
)

_SYSTEM_PROMPT = render_production_manager_prompt(
    "workstream:11111111-1111-1111-1111-111111111111",
    _FIXTURE_CTX,
    eval_json_suffix=_EVAL_SUFFIX,
)

_VAGUE_REQUEST = "Make the app better."


async def test_manager_asks_clarifying_questions_on_vague_request() -> None:
    resp = await call_claude(
        system=_SYSTEM_PROMPT,
        user=_VAGUE_REQUEST,
        max_tokens=400,
    )
    text = resp.text.strip()

    # The response should NOT be a JSON Brief.
    assert "```json" not in text, (
        f"Manager fabricated a Brief on a vague request — should "
        f"have asked clarifying questions instead.\n"
        f"Response: {text[:500]}"
    )
    # Should look like a question (heuristic: a ? somewhere).
    assert "?" in text, (
        f"Manager response on vague request contains no question:\n{text}"
    )
