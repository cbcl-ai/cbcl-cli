"""Pure verdict checks for API-lane cases (offline-testable, no model calls).

Each check returns a list of problems; an empty list is a pass. Keeping them
pure lets ``tests/evals/test_live_case_checks.py`` prove, without a model,
that every check can fail — a check the request text alone satisfies measures
nothing.
"""

from __future__ import annotations

import re
from typing import Iterable

INVENTED_SOFTWARE_GATES = ("gitlab", "git commit", "pnpm", "npm test", "pipeline", "commit sha")
VERIFICATION_SECTIONS = ("execution checks", "independent review", "evidence handoff")
# The contract headings themselves are required boilerplate. "Evidence handoff"
# alone would otherwise satisfy an evidence group that lists "evidence" (T8).
_SECTION_HEADINGS = re.compile(
    r"\b(?:" + "|".join(re.escape(section) for section in VERIFICATION_SECTIONS)
    + r")\b\s*:?",
    re.IGNORECASE,
)
# Wording the create_task verification_steps description and the Manager
# playbook PRESCRIBE for the verification steps of every brief ("Self-check
# all criteria", "Handoff: revision, results, evidence links", "Handoff:
# artifact, evidence", "reuse inspectable evidence for the current
# deliverable/source identities"). A brief that copies it has not authored
# any domain evidence (EV-3). Only these exact phrases are stripped, and only
# from verification_steps: "each criterion in rubric.txt" is authored wording.
_CONTRACT_BOILERPLATE = re.compile(
    r"\b(?:"
    r"self-check[:\s]+(?:all|each|every)\s+(?:acceptance\s+)?criteri(?:a|on)|"
    r"(?:all|each|every)\s+acceptance\s+criteri(?:a|on)|"
    r"acceptance\s+criteri(?:a|on)|"
    r"evidence\s+links?|"
    r"results?\s*/\s*evidence|"
    r"artifacts?\s*,\s*evidence|"
    r"inspectable\s+evidence|"
    r"(?:deliverable\s*/\s*)?source\s+identit(?:y|ies)|"
    r"evidence\s+contract"
    r")\b",
    re.IGNORECASE,
)
# The one circular phrase that is never domain wording in acceptance
# criteria: a criterion about the brief's own acceptance criteria ("The
# report meets all acceptance criteria"). Stripped from the criteria text;
# "each criterion in rubric.txt" and "evidence links to approved-claims.txt"
# stay authored.
_CRITERIA_META = re.compile(
    r"\b(?:(?:all|each|every)\s+)?(?:the\s+)?acceptance\s+criteri(?:a|on)\b", re.IGNORECASE,
)
# F06-acc-8: every API-lane case declares the tool surface it runs with, the
# state it starts from, and the effects it must never produce. A decision that
# calls a declared forbidden tool is recorded as a forbidden effect in the
# report, so a comparison never presents efficiency for it (C15).
DECLARED_KEYS = ("allowed_tools", "initial_state", "forbidden_effects")
# Manager tools that destroy, cancel, force-resume or decide on existing work.
# No API-lane request authorizes them.
DESTRUCTIVE_MANAGER_TOOLS = (
    "archive_scope", "archive_task", "decide_action_request", "delete_task",
    "retry_blocked_task", "stop_flow_run", "stop_task",
)
# Tools that create or start work; a vague request must not reach them.
WORK_CREATING_TOOLS = (
    "activate_scope", "consult_planner", "create_scope", "create_task",
    "schedule_assignment", "start_flow_run",
)
TOOL_FREE = "none"


def declared(*, allowed_tools: str, initial_state: str,
             forbidden_effects: Iterable[str] = ()) -> dict:
    """The ``eval_case(declared=...)`` block: allowed tools (a resolvable
    session spec such as ``manager:workstream`` or ``none``), a short
    initial-state description, and the tool names the case forbids."""
    if not initial_state.strip():
        raise ValueError("a case must describe its initial state")
    return {
        "allowed_tools": allowed_tools,
        "initial_state": initial_state.strip(),
        "forbidden_effects": sorted(set(forbidden_effects)),
    }


# ``ask_user_choice(kind="intake")`` carries 2-4 questions (backend contract,
# ``ws/tool_endpoint/_handlers_chat.py``); anything else is refused there.
INTAKE_QUESTIONS_MIN = 2
INTAKE_QUESTIONS_MAX = 4


def authored_text(output: dict) -> str:
    """The brief fields the model must write itself, lower-cased.

    ``inputs`` is excluded on purpose: it must carry the user's request
    verbatim, so any term from the request is always found there. The three
    verification contract headings are stripped from both fields, and the
    wording the tool schema and playbook prescribe for verification steps
    ("Self-check all criteria", "evidence links", "artifact, evidence") from
    verification_steps: it is required boilerplate, not authored evidence.
    Acceptance criteria keep their wording ("each criterion in rubric.txt")
    except a circular reference to the brief's own acceptance criteria.
    """
    criteria = output.get("acceptance_criteria") or []
    if isinstance(criteria, str):
        criteria = [criteria]
    criteria_text = _CRITERIA_META.sub(
        " ", _SECTION_HEADINGS.sub(" ", "\n".join(str(item) for item in criteria)),
    )
    verification = _CONTRACT_BOILERPLATE.sub(
        " ", _SECTION_HEADINGS.sub(" ", str(output.get("verification_steps") or "")),
    )
    return f"{criteria_text}\n{verification}".lower()


def domain_assignment_problems(
    output: dict,
    user_request: str,
    evidence_groups: Iterable[tuple[str, ...]],
    assignable: Iterable[str],
) -> list[str]:
    """Problems with a ``create_task`` decision for a file-only domain request.

    Each evidence group is a tuple of alternatives; at least one alternative of
    every group must appear in the authored criteria or verification steps.
    """
    problems: list[str] = []
    names = set(assignable)
    if user_request not in str(output.get("inputs") or ""):
        problems.append("inputs do not carry the user's request verbatim")
    executor, reviewer = output.get("assigned_agent"), output.get("reviewer")
    if executor not in names:
        problems.append(f"assigned_agent {executor!r} is not on the roster")
    if reviewer not in names:
        problems.append(f"reviewer {reviewer!r} is not on the roster")
    if executor == reviewer:
        problems.append("the reviewer is the executor")
    verification = str(output.get("verification_steps") or "").lower()
    for section in VERIFICATION_SECTIONS:
        if section not in verification:
            problems.append(f"verification_steps lack {section!r}")
    authored = authored_text(output)
    for group in evidence_groups:
        if not any(term in authored for term in group):
            problems.append(f"criteria and verification never mention any of {list(group)}")
    for invented in INVENTED_SOFTWARE_GATES:
        if invented in verification:
            problems.append(f"invented software gate {invented!r}")
    return problems


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
# A request sentence shorter than this is a phrase, not a copied requirement.
COPIED_SENTENCE_MIN_CHARS = 30


def _normalized(text: str) -> str:
    return " ".join(str(text).lower().split()).rstrip(".!? ")


def copied_request_sentences(criteria: Iterable[str], user_request: str) -> list[str]:
    """Request sentences that an acceptance criterion repeats verbatim.

    A criterion that restates the request states nothing checkable; the brief
    already carries the request verbatim in ``inputs``.
    """
    sentences = [
        sentence for sentence in (_normalized(part) for part in _SENTENCE_END.split(user_request))
        if len(sentence) >= COPIED_SENTENCE_MIN_CHARS
    ]
    copied: list[str] = []
    for criterion in criteria:
        text = _normalized(criterion)
        copied.extend(sentence for sentence in sentences if sentence in text)
    return sorted(set(copied))


def _term_in(term: object, text: str) -> bool:
    """``term`` occurs in ``text`` starting at a word boundary (a stem may
    continue: "redirect" matches "redirected"). A term ending in "/" is a
    path that ends there: it does not match when a path continues ("/" and
    "to /" do not match "to /settings/billing" or "/api/...")."""
    word = str(term).lower()
    tail = r"(?![a-z0-9_/-]|\.[a-z0-9])" if word.endswith("/") else ""
    return re.search(rf"(?<![a-z0-9]){re.escape(word)}{tail}", text) is not None


# Where one criterion states a second path: a semicolon, a sentence end, a
# contrast ("…, but", "…, while", "otherwise"), a coordinated clause after a
# comma, or "and" before a negation ("… lands on / and never sees the login
# screen again").
_CLAUSE_BREAK = re.compile(
    r";|(?<=[.!?])\s+|,\s*(?:and|but|while|whereas|otherwise)\b|\s(?:while|whereas|otherwise)\s|"
    r"\s+and\s+(?=(?:(?:is|are|does|do|did|was|were)\s+)?(?:not|never)\b|"
    r"(?:isn't|aren't|doesn't|don't|didn't|wasn't|weren't)\b)"
)


def brief_criteria_problems(
    criteria: object, user_request: str, cover: Iterable[dict],
) -> list[str]:
    """Problems with a brief's acceptance criteria for a clear request.

    Each ``cover`` entry names an ``outcome`` and either ``any_of`` (one
    alternative must appear in some criterion) or ``all_of_groups`` (ONE
    criterion must contain an alternative from every group, so "starts the
    OAuth flow" needs both the flow and the starting action, not a criterion
    that merely mentions OAuth). An outcome with ``none_of`` is judged per
    CLAUSE: one clause must hold every group and none of the ``none_of``
    terms, so "On success the user is redirected to /; on failure an error is
    shown" covers the success outcome while "If the callback completes with
    an error, the user is redirected to the login screen" does not. Terms
    match at a word start: stems such as "navigat" work, "authorize" does
    not match "unauthorized". No criterion may copy a whole request sentence.
    """
    if isinstance(criteria, str):
        criteria = [criteria]
    items = [str(item) for item in criteria or [] if str(item).strip()]
    lowered = [item.lower() for item in items]
    problems: list[str] = []
    for outcome in cover:
        groups = [list(group) for group in outcome.get("all_of_groups") or []]
        if outcome.get("any_of"):
            groups.append(list(outcome["any_of"]))
        excluded = [str(term) for term in outcome.get("none_of") or []]
        units = ([clause for item in lowered for clause in _CLAUSE_BREAK.split(item) if clause]
                 if excluded else lowered)
        if not groups or not any(
            all(any(_term_in(term, unit) for term in group) for group in groups)
            and not any(_term_in(term, unit) for term in excluded)
            for unit in units
        ):
            problems.append(f"no acceptance criterion covers: {outcome.get('outcome')}")
    for sentence in copied_request_sentences(items, user_request):
        problems.append(f"an acceptance criterion copies the request: {sentence!r}")
    return problems


def clarifying_question_problems(decision) -> list[str]:
    """Problems with the decision for a vague request (an intake card or a
    plain question is expected, never work)."""
    if decision.kind == "final_text":
        return [] if "?" in (decision.text or "") else ["the final text asks no question"]
    if decision.tool_name != "ask_user_choice":
        return [f"expected an intake question; got {decision.tool_name}"]
    tool_input = decision.tool_input or {}
    problems = []
    if tool_input.get("kind") != "intake":
        problems.append(f"ask_user_choice kind {tool_input.get('kind')!r} is not an intake card")
    if not str(tool_input.get("topic") or "").strip():
        problems.append("the intake card has no topic")
    questions = tool_input.get("questions")
    count = len(questions) if isinstance(questions, list) else 0
    if not INTAKE_QUESTIONS_MIN <= count <= INTAKE_QUESTIONS_MAX:
        problems.append(
            f"the intake card has {count} questions "
            f"(expected {INTAKE_QUESTIONS_MIN}-{INTAKE_QUESTIONS_MAX})"
        )
    return problems
