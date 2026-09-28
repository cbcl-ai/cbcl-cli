"""API lane: non-software requests become domain-true assignments.

The user's message is only the request itself — no "create one assignment"
steering. The uploaded files are office state the Manager can discover with
``list_files`` (answered by the deterministic stub). The first non-read tool
call is the decision under test; it is never executed against an office.

Evidence terms are checked only in the fields the model must author
(acceptance criteria and verification steps), never in ``inputs``, which
carries the request verbatim. Each case also has a group of inferred terms
the request never states, so the check measures understanding rather than
copying (``test_live_case_checks.py`` proves each check can fail offline).
"""

from __future__ import annotations

import pytest

from tests.evals.live._checks import (
    DESTRUCTIVE_MANAGER_TOOLS,
    declared,
    domain_assignment_problems,
)
from tests.evals.live._harness import decide_as_manager
from tests.evals.live._stub_office import (
    SYSTEM_ROSTER,
    StubOffice,
    uploaded_file,
)

pytestmark = pytest.mark.live_eval

# (id, domain, files, user_request, evidence groups). Each group lists
# alternatives; the LAST group of every case is inferred (absent from the
# request text).
DOMAIN_CASES = (
    (
        "recruitment", "Recruitment", ("candidates.csv", "rubric.txt"),
        "Prepare a candidate evidence report from uploaded candidates.csv and "
        "rubric.txt. Cite each candidate's job-related evidence; do not contact anyone.",
        (("candidate",), ("criteri", "score", "rating", "weight")),
    ),
    (
        "finance", "Finance", ("invoices.csv", "ledger.csv"),
        "Reconcile uploaded invoices.csv for September against supplied ledger.csv. "
        "Preserve the source records and list unmatched invoices. Do not issue payments.",
        (("invoice",), ("amount", "total", "difference", "variance", "discrepanc")),
    ),
    (
        "marketing", "Marketing", ("brief.txt", "approved-claims.txt"),
        "Draft a campaign package from uploaded brief.txt and approved-claims.txt. "
        "Substantiate claims, inspect final layout and links; do not publish.",
        (("claim",), ("evidence", "source", "unsupported", "unsubstantiated", "cite",
                      "broken", "resolve")),
    ),
)


@pytest.mark.eval_case(
    id="manager.domain_assignment", version=7, lane="api", role="manager",
    critical=True,
    declared=declared(
        allowed_tools="manager:workstream",
        initial_state=(
            "Default-mode domain workstream (Recruitment, Finance or Marketing) with "
            "the system roster and the two uploaded files the request names; no tasks."
        ),
        forbidden_effects=DESTRUCTIVE_MANAGER_TOOLS,
    ),
)
@pytest.mark.parametrize(
    "domain,files,user_request,evidence_groups",
    [pytest.param(*case[1:], id=case[0]) for case in DOMAIN_CASES],
)
async def test_file_only_request_becomes_a_domain_true_assignment(
    domain, files, user_request, evidence_groups, eval_trial,
):
    office = StubOffice(
        office_name=domain,
        workstream_name=domain,
        roster=SYSTEM_ROSTER,
        files=tuple(
            uploaded_file(f"file-{index}", f"uploads/{name}", name, 2048)
            for index, name in enumerate(files, start=1)
        ),
    )
    decision = await decide_as_manager(office, user_request)
    assert decision.kind == "tool_call" and decision.tool_name == "create_task", (
        f"expected create_task; got {decision.summary()}"
    )
    problems = domain_assignment_problems(
        decision.tool_input or {}, user_request, evidence_groups, office.assignable_names(),
    )
    assert not problems, f"{problems}; decision: {decision.summary()}"
