"""Live eval: Manager clarifies vague requests instead of guessing (P6.1).

A clear request → emit a Brief. An ambiguous one (no enough detail to
write acceptance criteria) → ask clarifying questions FIRST. This eval
catches the regression where a Manager rewrite makes the model
hallucinate detail rather than asking the user.
"""
from __future__ import annotations

import pytest

from tests.evals.live._harness import call_claude


pytestmark = pytest.mark.live_eval


_SYSTEM_PROMPT = """\
You are the AI Manager of a software office. The user sends you a
request. If the request is CLEAR — you can fill in all 9 Brief
fields confidently — respond with a JSON Brief wrapped in
```json ... ```.

If the request is AMBIGUOUS — you can't write specific acceptance
criteria without making things up — respond with PLAIN TEXT
asking 1-3 clarifying questions. Do NOT wrap clarifications in a
```json block.
"""

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
