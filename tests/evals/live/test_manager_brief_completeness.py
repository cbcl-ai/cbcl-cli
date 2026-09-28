"""API lane: a clear request becomes a complete Brief 2.0 ``create_task`` call.

Golden v2 (``goldens/manager_brief_github_signin.json``) checks what the model
must PRODUCE, not phrases it can copy:

* every field the live ``create_task`` schema requires is present and non-empty
  (Brief 2.0: goal, inputs, acceptance criteria, verification steps — plus the
  routing fields); the five optional framing fields are NOT required;
* the user's request appears verbatim in ``inputs`` (production contract);
* inferred facts — the workstream id from the turn context, the one frontend
  profile as executor, a distinct roster reviewer — none of which is in the
  user's message;
* the verification steps carry the three contract headings;
* each requested outcome is covered by an acceptance criterion (checked in the
  criteria list, not in the verbatim copy); coverage terms are alternatives,
  so a criterion naming the OAuth flow without the literal path still counts;
* no acceptance criterion repeats a whole request sentence (a restatement is
  not a checkable criterion; ``_checks.brief_criteria_problems``).

Nothing is appended to the production prompt; the decision is never executed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src._agent_image._mcp.tools_manager import get_manager_tools
from tests.evals.live._checks import (
    DESTRUCTIVE_MANAGER_TOOLS,
    brief_criteria_problems,
    declared,
)
from tests.evals.live._harness import decide_as_manager
from tests.evals.live._stub_office import SYSTEM_ROSTER, RosterAgent, StubOffice

pytestmark = pytest.mark.live_eval

GOLDEN_PATH = Path(__file__).parent / "goldens" / "manager_brief_github_signin.json"


def _office() -> StubOffice:
    return StubOffice(
        office_name="Acme Web",
        workstream_name="Auth",
        workstream_description="Authentication and login work.",
        workstream_goals="Ship OAuth sign-in.",
        roster=SYSTEM_ROSTER + (
            RosterAgent(
                "web-developer", "Web Developer",
                "Frontend engineering — owns the web UI, its routes and UI tests.",
                avatar_emoji="🕸️",
            ),
            RosterAgent(
                "data-engineer", "Data Engineer",
                "Data pipelines — owns ETL jobs and warehouse models.",
                avatar_emoji="🧮",
            ),
        ),
    )


def _required_fields() -> list[str]:
    schema = next(tool for tool in get_manager_tools() if tool["name"] == "create_task")
    return list(schema["inputSchema"]["required"])


def _non_empty(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(isinstance(item, str) and item.strip() for item in value)
    return value is not None


@pytest.mark.eval_case(
    id="manager.brief_completeness", version=7, lane="api", role="manager",
    critical=True, fixtures=["goldens/manager_brief_github_signin.json"],
    declared=declared(
        allowed_tools="manager:workstream",
        initial_state=(
            "Default-mode 'Auth' workstream in office Acme Web; system roster plus "
            "web-developer and data-engineer Profiles; no tasks, files or spec."
        ),
        forbidden_effects=DESTRUCTIVE_MANAGER_TOOLS,
    ),
)
async def test_clear_request_becomes_a_complete_brief(eval_trial):
    golden = json.loads(GOLDEN_PATH.read_text())
    assert golden["schema_version"] == 7
    office = _office()
    decision = await decide_as_manager(office, golden["request"])
    assert decision.kind == "tool_call" and decision.tool_name == "create_task", (
        f"expected create_task; got {decision.summary()}"
    )
    brief = decision.tool_input or {}

    missing = [field for field in _required_fields() if not _non_empty(brief.get(field))]
    assert not missing, f"required create_task fields missing/empty: {missing}"

    assert golden["request"] in brief["inputs"], "request not preserved verbatim in inputs"

    inferred = golden["inferred"]
    assert brief["assigned_agent"] == inferred["assigned_agent"], (
        f"executor should be the UI profile; got {brief['assigned_agent']!r}"
    )
    if inferred["workstream_id_from_context"]:
        assert brief["workstream_id"] == office.workstream_id
    if inferred["reviewer_distinct_roster_profile"]:
        assert brief["reviewer"] in office.assignable_names()
        assert brief["reviewer"] != brief["assigned_agent"]

    verification = brief["verification_steps"].lower()
    for heading in golden["verification_headings"]:
        assert heading in verification, f"verification steps lack {heading!r}"

    problems = brief_criteria_problems(
        brief["acceptance_criteria"], golden["request"],
        golden["acceptance_criteria_cover"],
    )
    assert golden["criteria_must_not_copy_request"] is True
    assert not problems, f"{problems}; decision: {decision.summary()}"
