"""Live eval: Manager produces a complete Brief on a clear request.

Given a hardcoded user message and the Manager system prompt, the
Manager must emit a create_task tool call whose Brief contains every
one of the 9 mandatory fields. This eval guards against drift in the
system-prompt that would silently start producing under-specified
briefs.

Skipped when ANTHROPIC_API_KEY is absent (see conftest.py).
"""
from __future__ import annotations

import pytest

from tests.evals.live._harness import call_claude


pytestmark = pytest.mark.live_eval


# Minimal Manager system prompt distilled from the live one — just
# enough context that the model knows to produce a Brief in the
# response. The real Manager prompt includes board state, team
# roster, etc.; for an eval we only need the contract.
_MANAGER_SYSTEM_PROMPT = """\
You are the AI Manager of a software office. The user is asking you
to create a task. Respond with a JSON object that includes ALL nine
required Brief fields:
  goal, context, inputs, output_format, acceptance_criteria (array),
  allowed_tools (array), required_skills (array),
  risks_and_edge_cases, verification_steps.

Wrap the JSON in a ```json fenced code block. Do NOT include any
other commentary outside the block.
"""

_USER_REQUEST = (
    "Please add a 'Sign in with GitHub' button to the login screen. "
    "It should kick off our existing OAuth flow at "
    "/api/auth/oauth/github/start and route the user to / on "
    "success."
)

_REQUIRED_BRIEF_FIELDS = (
    "goal",
    "context",
    "inputs",
    "output_format",
    "acceptance_criteria",
    "allowed_tools",
    "required_skills",
    "risks_and_edge_cases",
    "verification_steps",
)


def _extract_json(text: str) -> dict:
    """Pull the first ```json ... ``` block out of the response."""
    import json
    import re

    m = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
    if not m:
        # Fall back: try parsing the whole thing.
        return json.loads(text)
    return json.loads(m.group(1))


async def test_manager_emits_complete_brief() -> None:
    """Golden-file regression eval (P6.1).

    Compares the live response against
    `goldens/manager_brief_github_signin.json`. Strict on structure
    (every required field present, every must-contain phrase appears,
    minimum criteria count, total chars floor); loose on actual text
    since LLMs are non-deterministic even at temperature=0.
    """
    import json as _json
    from pathlib import Path

    golden_path = (
        Path(__file__).parent / "goldens" / "manager_brief_github_signin.json"
    )
    golden = _json.loads(golden_path.read_text())

    resp = await call_claude(
        system=_MANAGER_SYSTEM_PROMPT,
        user=_USER_REQUEST,
        max_tokens=1500,
    )
    brief = _extract_json(resp.text)

    # Structural: every required field present.
    missing = [
        f for f in golden["must_have_brief_fields"] if f not in brief
    ]
    assert not missing, (
        f"Manager left out required Brief fields: {missing}\n"
        f"Got keys: {sorted(brief.keys())}\n"
        f"Model: {resp.model}, tokens in={resp.input_tokens} "
        f"out={resp.output_tokens}"
    )

    # Acceptance-criteria count.
    ac = brief["acceptance_criteria"]
    assert (
        isinstance(ac, list)
        and len(ac) >= golden["min_acceptance_criteria_count"]
    ), f"acceptance_criteria too short: {ac!r}"

    # Phrase presence somewhere in the JSON (any field).
    serialized = _json.dumps(brief).lower()
    for phrase in golden["must_contain_phrases"]:
        assert phrase.lower() in serialized, (
            f"Brief missing required phrase {phrase!r}.\n"
            f"Brief was: {brief}"
        )

    # Total content floor — guards against terse one-line regressions.
    total_chars = sum(len(str(v)) for v in brief.values())
    assert total_chars >= golden["min_total_token_chars"], (
        f"Brief content too sparse: {total_chars} chars "
        f"(<{golden['min_total_token_chars']})"
    )
