"""Live eval: Manager routes review work to a non-executor (P6.1).

A common regression: the Manager forgets the "reviewer must be a
different agent than the executor" rule and assigns the same agent
to both fields. This eval feeds a prompt with two agents available
and asserts the response splits them correctly.
"""
from __future__ import annotations

import json
import re

import pytest

from tests.evals.live._harness import call_claude


pytestmark = pytest.mark.live_eval


_SYSTEM_PROMPT = """\
You are the AI Manager. Your office has two agents:
- python-developer: writes Python code
- auditor: reviews deliverables against acceptance criteria

When the user requests a coding task, produce a JSON Brief with
two REQUIRED fields:
  - assigned_agent: the agent that will EXECUTE the task
  - reviewer: a DIFFERENT agent that will review the deliverable

An agent can never review its own work. If you only have one
suitable agent, pick a fallback reviewer rather than reusing the
executor.

Wrap the JSON in ```json ... ``` and include NOTHING else.
"""

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
