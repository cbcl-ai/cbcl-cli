"""Live eval: Manager produces a complete Brief on a clear request.

EVAL-02: this drives the REAL production Manager system prompt
(``MANAGER_CLAUDE_MD`` + ``build_dynamic_context``, via
``render_production_manager_prompt``) — not a distilled stub — so a regression
in the SHIPPED prompt that starts producing under-specified briefs actually
fails here. The plain /v1/messages API has no tools, so an eval-only suffix
asks the model to emit the payload it WOULD pass to ``create_task`` as JSON.

Skipped when ANTHROPIC_API_KEY is absent (see conftest.py).
"""
from __future__ import annotations

import pytest

from tests.evals.live._harness import (
    call_claude,
    render_production_manager_prompt,
)


pytestmark = pytest.mark.live_eval


# Fixture office/workstream context fed to build_dynamic_context so the Manager
# runs with a realistic per-turn context block.
_FIXTURE_CTX = {
    "office_name": "Acme Web",
    "workstream_id": "11111111-1111-1111-1111-111111111111",
    "workstream_name": "Auth",
    "workstream_priority": "high",
    "workstream_description": "Authentication and login work.",
    "workstream_goals": "Ship OAuth sign-in.",
    "team_roster": (
        "**Senior Developer** (senior-developer) — 👩‍💻\n"
        "**Auditor** (auditor) — 📋\n"
        "**Manager Assistant** (manager-assistant) — ⚡"
    ),
    "board_summary": {},
    "scopes": [],
}

_EVAL_JSON_SUFFIX = (
    "## Eval mode\n"
    "This request comes over an API without tools, so you cannot call "
    "`create_task`. Instead, produce the EXACT payload you would pass to "
    "`create_task` for this request as a single JSON object with all 9 Brief "
    "fields (goal, context, inputs, output_format, acceptance_criteria (array), "
    "allowed_tools (array), required_skills (array), risks_and_edge_cases, "
    "verification_steps). Wrap it in a ```json fenced block; no other prose."
)

_MANAGER_SYSTEM_PROMPT = render_production_manager_prompt(
    "workstream:11111111-1111-1111-1111-111111111111",
    _FIXTURE_CTX,
    eval_json_suffix=_EVAL_JSON_SUFFIX,
)

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
