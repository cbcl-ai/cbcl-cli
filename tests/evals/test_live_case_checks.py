"""Offline proof that the API-lane verdict checks can fail (no model calls).

A check the request text alone satisfies measures nothing (X39): the domain
case once searched the whole ``create_task`` payload for terms that were all
in the request, and the request is always in ``inputs``. The clarify case
once accepted any ``ask_user_choice`` kind (X40 follow-up), although only an
intake card is a clarification. These tests run the same pure checks the
live cases use against synthetic decisions.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.evals.live._checks import (
    authored_text,
    brief_criteria_problems,
    clarifying_question_problems,
    copied_request_sentences,
    domain_assignment_problems,
)
from tests.evals.live._harness import Decision
from tests.evals.live.test_domain_tool_decisions import DOMAIN_CASES
from tests.evals.live.test_readability_scenarios import CARD_REQUESTS

ROSTER = ("analyst", "auditor", "builder", "manager-assistant")
GOLDEN = Path(__file__).parent / "live" / "goldens" / "manager_brief_github_signin.json"
VERIFICATION = (
    "Execution checks: re-run the comparison.\n"
    "Independent review: the reviewer re-checks each row.\n"
    "Evidence handoff: attach the report."
)


def _brief(user_request: str, criteria: list[str]) -> dict:
    return {
        "title": "Report", "goal": "A report.", "inputs": user_request,
        "acceptance_criteria": criteria, "verification_steps": VERIFICATION,
        "assigned_agent": "analyst", "reviewer": "auditor",
    }


@pytest.mark.parametrize("case", DOMAIN_CASES, ids=[case[0] for case in DOMAIN_CASES])
def test_every_domain_case_has_an_inferred_evidence_group(case):
    _, _, _, user_request, groups = case
    request = user_request.lower()
    inferred = groups[-1]
    assert not any(term in request for term in inferred), (
        f"the inferred group {inferred} is stated in the request, so it measures copying"
    )


@pytest.mark.parametrize("case", DOMAIN_CASES, ids=[case[0] for case in DOMAIN_CASES])
def test_request_text_alone_does_not_satisfy_the_evidence_check(case):
    _, _, _, user_request, groups = case
    # The request sits verbatim in inputs; the authored fields are generic.
    output = _brief(user_request, ["The deliverable is complete and accurate."])
    problems = domain_assignment_problems(output, user_request, groups, ROSTER)
    assert any("never mention" in problem for problem in problems), problems
    assert "candidate" not in authored_text({**output, "inputs": "candidate invoice claim"})


@pytest.mark.parametrize("case", DOMAIN_CASES, ids=[case[0] for case in DOMAIN_CASES])
def test_a_domain_true_brief_passes_the_check(case):
    _, _, _, user_request, groups = case
    criteria = [f"Each {group[0]} is covered." for group in groups]
    output = _brief(user_request, criteria)
    assert domain_assignment_problems(output, user_request, groups, ROSTER) == []


@pytest.mark.parametrize("case", DOMAIN_CASES, ids=[case[0] for case in DOMAIN_CASES])
def test_only_the_inferred_group_unmet_is_reported(case):
    _, _, _, user_request, groups = case
    # Every group but the last is covered by authored criteria; the required
    # verification headings ("Evidence handoff", ...) must not stand in for
    # the inferred group (T8: "evidence" matched the heading).
    criteria = [f"Each {group[0]} is covered." for group in groups[:-1]]
    output = _brief(user_request, criteria)
    problems = domain_assignment_problems(output, user_request, groups, ROSTER)
    assert problems == [
        f"criteria and verification never mention any of {list(groups[-1])}"
    ], problems


def _tool_verification_description() -> str:
    from src._agent_image._mcp.tools_manager import get_manager_tools

    tool = next(tool for tool in get_manager_tools() if tool["name"] == "create_task")
    return tool["inputSchema"]["properties"]["verification_steps"]["description"]


def _playbook_verification_paragraph() -> str:
    from src.config_sync.claude_md_templates._manager import MANAGER_CLAUDE_MD

    start = MANAGER_CLAUDE_MD.index("**Verification Steps** (REQUIRED)")
    end = MANAGER_CLAUDE_MD.index("\n\n", start)
    return MANAGER_CLAUDE_MD[start:end]


@pytest.mark.parametrize("source", ["tool_description", "playbook"])
@pytest.mark.parametrize("case", DOMAIN_CASES, ids=[case[0] for case in DOMAIN_CASES])
def test_prescribed_verification_boilerplate_is_not_authored_evidence(case, source):
    """EV-3: copying the verification wording the create_task schema or the
    Manager playbook prescribes ("Self-check all criteria", "evidence links",
    "Handoff: artifact, evidence") must not satisfy the inferred group."""
    _, _, _, user_request, groups = case
    verification = (_tool_verification_description() if source == "tool_description"
                    else _playbook_verification_paragraph())
    assert "evidence" in verification.lower()
    criteria = [f"Each {group[0]} is covered." for group in groups[:-1]]
    output = {**_brief(user_request, criteria), "verification_steps": verification}
    problems = domain_assignment_problems(output, user_request, groups, ROSTER)
    assert f"criteria and verification never mention any of {list(groups[-1])}" in problems, (
        problems
    )


@pytest.mark.parametrize("criterion", [
    "Each candidate is assessed against each criterion in rubric.txt with cited job-related "
    "evidence.",
    "The report evaluates every candidate against all criteria in rubric.txt.",
    "For each candidate, the report cites job-related evidence for each criterion in rubric.txt.",
    "Every candidate in candidates.csv is assessed against all criteria in rubric.txt.",
])
def test_authored_criteria_wording_is_kept(criterion):
    """EVR-5 / T8-STRIP-AUTHORED: 'each criterion'/'all criteria' in an
    acceptance criterion is domain wording, not prescribed boilerplate."""
    _, _, _, user_request, groups = DOMAIN_CASES[0]
    output = _brief(user_request, [criterion, "No candidate is contacted."])
    assert domain_assignment_problems(output, user_request, groups, ROSTER) == []


def test_authored_verification_wording_is_kept_but_the_prescribed_phrase_is_not():
    _, _, _, user_request, groups = DOMAIN_CASES[0]
    authored = {**_brief(user_request, ["Each candidate is covered."]),
                "verification_steps": VERIFICATION + "\nThe reviewer re-checks each criterion "
                                                     "in rubric.txt."}
    assert domain_assignment_problems(authored, user_request, groups, ROSTER) == []
    prescribed = {**_brief(user_request, ["Each candidate is covered."]),
                  "verification_steps": VERIFICATION + "\nSelf-check all criteria."}
    assert domain_assignment_problems(prescribed, user_request, groups, ROSTER) == [
        f"criteria and verification never mention any of {list(groups[-1])}"
    ]
    _, _, _, marketing_request, marketing_groups = DOMAIN_CASES[2]
    linked = _brief(marketing_request, [
        "Each claim in the package includes evidence links to its approved-claims.txt entry."])
    assert domain_assignment_problems(linked, marketing_request, marketing_groups, ROSTER) == []


def test_boilerplate_stripping_keeps_authored_domain_evidence():
    kept = authored_text({
        "acceptance_criteria": ["Each score cites the rubric criterion it applies."],
        "verification_steps": "Evidence handoff: revision, results, evidence links, limitations; "
                              "every unsupported claim is listed with its source.",
    })
    assert "evidence links" not in kept and "score" in kept and "unsupported" in kept
    assert "source" in kept and "criterion" in kept


def test_headings_are_stripped_but_authored_evidence_still_counts():
    authored = authored_text({"acceptance_criteria": [], "verification_steps": VERIFICATION})
    assert "evidence" not in authored and "handoff" not in authored
    kept = authored_text({
        "acceptance_criteria": ["Each claim cites supporting evidence."],
        "verification_steps": VERIFICATION,
    })
    assert "supporting evidence" in kept


def test_domain_check_flags_roster_reviewer_and_software_gates():
    _, _, _, user_request, groups = DOMAIN_CASES[1]
    output = {
        **_brief(user_request, ["Each invoice amount is compared."]),
        "reviewer": "analyst", "inputs": "paraphrase",
        "verification_steps": VERIFICATION + "\nRun npm test in the pipeline.",
    }
    problems = " | ".join(domain_assignment_problems(output, user_request, groups, ROSTER))
    for expected in ("verbatim", "reviewer is the executor", "npm test", "pipeline"):
        assert expected in problems


def _golden() -> dict:
    return json.loads(GOLDEN.read_text())


def test_brief_criteria_accept_alternatives_to_the_literal_oauth_path():
    golden = _golden()
    authored = [
        "The login screen shows a 'Sign in with GitHub' button.",
        "Clicking the button starts the existing GitHub OAuth sign-in flow.",
        "After a successful sign-in the user is redirected to /.",
    ]
    assert brief_criteria_problems(
        authored, golden["request"], golden["acceptance_criteria_cover"],
    ) == []
    literal = [*authored[:1], "Clicking it opens /api/auth/oauth/github/start.", authored[2]]
    assert brief_criteria_problems(
        literal, golden["request"], golden["acceptance_criteria_cover"],
    ) == []


def test_brief_criteria_that_copy_the_request_are_reported():
    golden = _golden()
    request = golden["request"]
    # Each request sentence as a "criterion" covers every outcome by copying.
    copied = [part.strip() for part in request.split(". ")]
    problems = brief_criteria_problems(copied, request, golden["acceptance_criteria_cover"])
    assert problems and all("copies the request" in problem for problem in problems)
    assert len(problems) == 2
    assert copied_request_sentences([request], request) != []
    # A short shared phrase is not a copy.
    assert copied_request_sentences(["The button is on the login screen."], request) == []


def test_brief_criteria_report_an_uncovered_outcome():
    golden = _golden()
    criteria = ["The login screen shows a 'Sign in with GitHub' button."]
    problems = brief_criteria_problems(
        criteria, golden["request"], golden["acceptance_criteria_cover"],
    )
    assert problems == [
        "no acceptance criterion covers: starts the existing OAuth flow",
        "no acceptance criterion covers: success lands on /",
    ]


def test_brief_criteria_that_mention_but_do_not_state_the_outcomes_are_reported():
    """EV-6: a criterion that only mentions OAuth, or a failure return, does
    not cover "starts the flow" or "success lands on /"."""
    golden = _golden()
    weak = [
        "The login screen shows a Sign in with GitHub button.",
        "No changes are made to the existing OAuth backend.",
        "On failure the user returns to the login screen with an error.",
    ]
    assert brief_criteria_problems(
        weak, golden["request"], golden["acceptance_criteria_cover"],
    ) == [
        "no acceptance criterion covers: starts the existing OAuth flow",
        "no acceptance criterion covers: success lands on /",
    ]
    # Both groups must sit in ONE criterion, not be spread across two.
    split = [weak[0], "The OAuth flow is unchanged.", "Sign-in starts on click.",
             "After a successful sign-in the user is redirected to /."]
    assert brief_criteria_problems(
        split, golden["request"], golden["acceptance_criteria_cover"],
    ) == ["no acceptance criterion covers: starts the existing OAuth flow"]
    # "authorize" is not a term, so "unauthorized" cannot stand in for the flow.
    assert brief_criteria_problems(
        [weak[0], "Unauthorized users open the login screen.", split[-1]],
        golden["request"], golden["acceptance_criteria_cover"],
    ) == ["no acceptance criterion covers: starts the existing OAuth flow"]


_BUTTON = "The login screen shows a 'Sign in with GitHub' button."
_FLOW = "Clicking the button navigates to /api/auth/oauth/github/start."


@pytest.mark.parametrize("criterion", [
    "When sign-in succeeds the user lands on /.",
    "When the OAuth callback succeeds, the app redirects to /.",
    "Once GitHub authentication completes, the user is redirected to /.",
    "After signing in, the user is redirected to /.",
    "Once logged in, the user is redirected to /.",
    "After the OAuth callback completes, the user is redirected to /.",
    "The user is redirected to / after authenticating with GitHub.",
    "Once GitHub OAuth succeeded, the user is redirected to /.",
    "After GitHub authorization, the user is redirected to /.",
    "When the OAuth flow completes, the user is redirected to /.",
])
def test_success_outcome_accepts_natural_wordings(criterion):
    """EVR-6 / GOLDEN-SUCCESS-GROUP / GOLDEN-SUCCESS-STEM."""
    golden = _golden()
    assert brief_criteria_problems(
        [_BUTTON, _FLOW, criterion], golden["request"], golden["acceptance_criteria_cover"],
    ) == []


@pytest.mark.parametrize("criterion", [
    "On failure the user returns to the login screen with an error.",
    "If authentication fails, the user is redirected to the login screen.",
    "If the OAuth callback fails, the user is redirected back to the login screen with an error.",
    "Unauthenticated users are redirected to the login page.",
    "If sign-in does not succeed, the user is redirected to the login screen.",
    "Users who are not signed in are redirected to the login page.",
    "If the user cancels signing in, they are redirected to the login screen.",
])
def test_a_failure_path_does_not_cover_the_success_outcome(criterion):
    golden = _golden()
    assert brief_criteria_problems(
        [_BUTTON, _FLOW, criterion], golden["request"], golden["acceptance_criteria_cover"],
    ) == ["no acceptance criterion covers: success lands on /"]


@pytest.mark.parametrize("criterion", [
    "On success the user is redirected to /settings/billing.",
    "After sign-in the user is redirected to the login screen.",
    "On success the user is redirected to /api/auth/oauth/github/start.",
    "On success the user is redirected back to the app.",
    "On success the user is redirected to /.well-known/page.",
])
def test_success_outcome_requires_the_root_destination(criterion):
    """B7c-02: the request routes the user to /; a brief that names another
    page, or leaves the page vague, does not cover the success outcome."""
    golden = _golden()
    assert brief_criteria_problems(
        [_BUTTON, _FLOW, criterion], golden["request"], golden["acceptance_criteria_cover"],
    ) == ["no acceptance criterion covers: success lands on /"]


@pytest.mark.parametrize("criterion", [
    "On success the user is redirected to the root path (`/`).",
    "On success the user is redirected to '/'.",
    "After a successful sign-in the user lands on the home page.",
    "After a successful sign-in the user is redirected to the app root.",
    "On success the user is redirected to /?welcome=1.",
])
def test_success_outcome_accepts_root_destination_wordings(criterion):
    golden = _golden()
    assert brief_criteria_problems(
        [_BUTTON, _FLOW, criterion], golden["request"], golden["acceptance_criteria_cover"],
    ) == []


# R3-GOLDEN-NONE-OF (three reproductions): one criterion commonly states
# the success path AND the failure path or a negation. none_of is judged per
# clause, so these cover the success outcome.
_COMBINED_SUCCESS = [
    "On successful sign-in the user is redirected to /; if sign-in fails, an error is shown on "
    "the login screen.",
    "After successful sign-in the user is redirected to / and is not shown the login screen "
    "again.",
    "After successful sign-in the user lands on / and never sees the login screen again.",
    "After successful sign-in the user is redirected to / and does not need to log in again.",
    "After successful sign-in the user is redirected to /; cancelling on GitHub returns them to "
    "the login screen.",
    "Successful sign-in redirects to /; failures surface an error message.",
    "On success, the user is redirected to /; on failure, an error message is displayed.",
    "On successful sign-in the user is redirected to /; if sign-in fails or is cancelled, the "
    "user stays on the login screen with an error message.",
    "A successful OAuth callback redirects the user to / and does not show the login screen "
    "again.",
    "After a successful GitHub sign-in the user lands on / and is never returned to the login "
    "page.",
    "After successful authentication the user is routed to /, and the existing email/password "
    "sign-in is not affected.",
    "Successful sign-in redirects to /; an unauthenticated visitor still sees the login screen.",
    "On success the user is redirected to /; on failure an error is shown.",
    "On success the user is redirected to /; on failure they stay on the login screen with an "
    "error.",
    "After a successful GitHub sign-in the user is redirected to /, and a cancelled sign-in "
    "shows an error.",
    "Successful sign-in redirects to /, while a failed sign-in keeps the user on the login "
    "screen.",
    "On successful sign-in the user is redirected to /; failed sign-ins show an error message.",
    "A successful login routes the user to / and never shows the login screen again.",
    "After a successful sign-in the user lands on / and is not shown the login screen.",
]

# EV-6 and R3-GOLDEN-NONE-OF: failure-only criteria, including the
# "... completes with an error" wordings the success terms could pair with.
_FAILURE_ONLY = [
    "If the OAuth callback completes with an error, the user is redirected to the login screen.",
    "If authorization completes with an invalid state, the user is redirected back to the login "
    "screen.",
    "When the flow completes with an error, the user is routed to the login page.",
    "If the callback completes with an expired state, the user is redirected to the login "
    "screen.",
    "When the OAuth callback completes with an error, the user is redirected back to the login "
    "screen.",
    "After login errors, the user is redirected to the login screen with a message.",
    "If the OAuth flow completes with an error, the user is routed to the login page.",
]


@pytest.mark.parametrize("criterion", _COMBINED_SUCCESS)
def test_a_criterion_stating_both_paths_covers_the_success_outcome(criterion):
    golden = _golden()
    assert brief_criteria_problems(
        [_BUTTON, _FLOW, criterion], golden["request"], golden["acceptance_criteria_cover"],
    ) == []


@pytest.mark.parametrize("criterion", _FAILURE_ONLY)
def test_an_error_path_does_not_cover_the_success_outcome(criterion):
    golden = _golden()
    assert brief_criteria_problems(
        [_BUTTON, _FLOW, criterion], golden["request"], golden["acceptance_criteria_cover"],
    ) == ["no acceptance criterion covers: success lands on /"]


@pytest.mark.parametrize("criteria", [
    ["Every candidate in candidates.csv is assessed against the rubric.",
     "The report meets all acceptance criteria."],
    ["Every candidate in candidates.csv appears in the report.",
     "All acceptance criteria are verified before handoff."],
])
def test_a_circular_acceptance_criteria_reference_is_not_domain_evidence(criteria):
    """R3-T8-CRITERIA-BOILERPLATE: "meets all acceptance criteria" is the
    brief talking about itself, not rubric scoring."""
    _, _, _, user_request, groups = DOMAIN_CASES[0]
    output = _brief(user_request, criteria)
    assert f"criteria and verification never mention any of {list(groups[-1])}" in (
        domain_assignment_problems(output, user_request, groups, ROSTER)
    )


@pytest.mark.parametrize("criterion", [
    "Clicking the button initiates the sign-in flow through /api/auth/oauth/github/start.",
    "The button triggers the existing OAuth authorization flow.",
    "Pressing the button kicks off the GitHub OAuth flow.",
])
def test_brief_criteria_accept_flow_start_paraphrases(criterion):
    golden = _golden()
    criteria = ["The login screen shows a 'Sign in with GitHub' button.", criterion,
                "Once signed in, the user lands on /."]
    assert brief_criteria_problems(
        criteria, golden["request"], golden["acceptance_criteria_cover"],
    ) == []


@pytest.mark.parametrize("tool_input,ok", [
    ({"kind": "intake", "topic": "app-goals", "questions": [{"key": "a"}, {"key": "b"}]}, True),
    ({"kind": "execution_mode", "question": "How should I run it?", "options": []}, False),
    ({"kind": "informational", "question": "Which app?", "options": []}, False),
    ({"kind": "intake", "topic": "", "questions": [{"key": "a"}, {"key": "b"}]}, False),
    ({"kind": "intake", "topic": "app-goals", "questions": [{"key": "a"}]}, False),
    ({"kind": "intake", "topic": "app-goals"}, False),
], ids=["intake", "execution_mode", "informational", "no_topic", "one_question", "no_questions"])
def test_only_an_intake_card_counts_as_a_clarifying_question(tool_input, ok):
    decision = Decision(kind="tool_call", tool_name="ask_user_choice", tool_input=tool_input)
    assert (clarifying_question_problems(decision) == []) is ok


def test_plain_text_question_counts_and_work_does_not():
    assert clarifying_question_problems(
        Decision(kind="final_text", text="Which part of the app should improve first?")
    ) == []
    assert clarifying_question_problems(Decision(kind="final_text", text="I'll improve it.")) != []
    assert clarifying_question_problems(
        Decision(kind="tool_call", tool_name="create_task", tool_input={})
    ) != []


_FILE_REFERENCE = re.compile(r"\b[\w.-]+\.(?:csv|txt|md|json|xlsx|pdf)\b", re.IGNORECASE)


@pytest.mark.parametrize("user_request,files", CARD_REQUESTS)
def test_card_requests_only_reference_files_the_office_has(user_request, files):
    """T12: a request about an attached or uploaded file needs that file in
    the stub office, or the case measures a Manager that cannot find it."""
    listed = {item["title"] for item in files}
    named = set(_FILE_REFERENCE.findall(user_request))
    assert named <= listed, f"request names {sorted(named - listed)} the office does not list"
    if re.search(r"\b(?:attached|uploaded)\b", user_request, re.IGNORECASE):
        assert files, "request refers to an attached/uploaded file but the office has none"


# ── F06-acc-8: every API-lane case declares its surface, state and effects ──


def _api_cases() -> list[tuple[str, dict]]:
    """``(test name, eval_case kwargs)`` for every live API-lane test."""
    import importlib

    from tests.evals.live import __path__ as live_path

    cases = []
    for module_path in sorted(Path(live_path[0]).glob("test_*.py")):
        module = importlib.import_module(f"tests.evals.live.{module_path.stem}")
        for name, function in sorted(vars(module).items()):
            if not name.startswith("test_") or not callable(function):
                continue
            for mark in getattr(function, "pytestmark", []):
                if mark.name == "eval_case":
                    cases.append((f"{module_path.stem}::{name}", dict(mark.kwargs)))
    return cases


_API_CASES = _api_cases()


def test_every_api_case_is_found():
    assert len(_API_CASES) == 9
    assert all(kwargs.get("lane") == "api" for _, kwargs in _API_CASES)


@pytest.mark.parametrize("name,kwargs", _API_CASES, ids=[name for name, _ in _API_CASES])
def test_api_case_declares_allowed_tools_initial_state_and_forbidden_effects(name, kwargs):
    from tests.evals.live._checks import DECLARED_KEYS, DESTRUCTIVE_MANAGER_TOOLS
    from tests.evals.live._harness import resolve_allowed_tools

    block = kwargs.get("declared")
    assert isinstance(block, dict) and tuple(sorted(block)) == tuple(sorted(DECLARED_KEYS)), name
    assert block["initial_state"].strip()
    catalog = {tool["name"] for tool in resolve_allowed_tools(block["allowed_tools"])}
    forbidden = set(block["forbidden_effects"])
    if kwargs["role"] == "generator":
        assert block["allowed_tools"] == "none" and not catalog and not forbidden
    else:
        assert block["allowed_tools"] == "manager:workstream"
        # A forbidden effect must be a tool the session can actually call.
        assert forbidden and forbidden <= catalog, forbidden - catalog
        assert set(DESTRUCTIVE_MANAGER_TOOLS) <= forbidden


def test_only_the_vague_request_forbids_creating_work():
    from tests.evals.live._checks import WORK_CREATING_TOOLS

    forbidding = [
        kwargs["id"] for _, kwargs in _API_CASES
        if set(WORK_CREATING_TOOLS) & set(kwargs["declared"]["forbidden_effects"])
    ]
    assert forbidding == ["manager.clarify.vague_request"]


def test_allowed_tools_specs_resolve_to_the_production_selector():
    from src._agent_image.mcp_tool_server import select_session_tools
    from tests.evals.live._harness import resolve_allowed_tools

    workstream = {tool["name"] for tool in resolve_allowed_tools("manager:workstream")}
    general = {tool["name"] for tool in resolve_allowed_tools("manager:general_chat")}
    assert "create_task" in workstream and "create_task" not in general
    reviewer = resolve_allowed_tools("worker:review:finance-controller")
    assert reviewer == select_session_tools("worker", "finance-controller", "review", None, "")
    assert resolve_allowed_tools("none") == []
    for bad in ("manager", "manager:office", "worker:review", "anything"):
        with pytest.raises(ValueError):
            resolve_allowed_tools(bad)


def test_declared_requires_an_initial_state_and_sorts_effects():
    from tests.evals.live._checks import declared

    block = declared(allowed_tools="none", initial_state=" x ", forbidden_effects=("b", "a", "a"))
    assert block == {"allowed_tools": "none", "initial_state": "x", "forbidden_effects": ["a", "b"]}
    with pytest.raises(ValueError):
        declared(allowed_tools="none", initial_state="  ")
