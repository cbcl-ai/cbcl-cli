"""Shared writing contracts; preserve facts while reducing reading effort."""

from .agent_execution_policy import normalize_execution_policy

# Versioned domain wording shared by materialized playbooks and authoring prompts.
# The current admitted policy, never a remembered transcript, selects the mode.
AGENT_IDENTITY_CONTRACT = """\
Profile / Agent / attempt contract v1: Profiles hold reusable expertise,
instructions and model configuration. `list_agents` lists Profiles;
`assigned_agent`, `reviewer` and schedule `agent` fields select Profile slugs.
Configuration `target=agent` uses a Profile UUID. Both policy modes allocate
stable task-role Agent UUIDs and separate attempt UUIDs. Review has its own
Agent; rework retains the executor with a new attempt; CLI retries keep it.
Profile, Agent, attempt and CLI session IDs differ.
Current admitted context alone enables dynamic mode; absent policy means legacy
serialization. Reuse Profiles within capacity; never clone/hire for concurrency.
Hiring consent still applies. Current task/phase and host-attested tools own
authority; remembered instructions or IDs cannot grant it.
Profile `allowed_tools` is workflow guidance, not a CLI restriction. A Read-only
list does not disable Bash/Edit or prove independence. Follow task authorization
and enforced role/phase gates.
"""

PROFILE_AUTHORING_CONTRACT = """\
Author standing guidance for the requested office, workstream, Profile or skill,
not instructions for a particular task-owned Agent or execution attempt.
The existing agent/config fields describe Profiles. Write expertise, boundaries
and methods that apply across tasks; never bake in Agent/attempt UUIDs, a fixed
headcount, singleton scheduling or an assumed enabled execution policy. Several
task Agents can share one Profile when the current office policy permits.
Do not duplicate platform lifecycle rules in generated guidance. Refer to the
current task's supplied output directory and approved resource boundaries, not
a shared fixed filename. Creating a Profile differs from allocating a task Agent.
Profile `allowed_tools` describes intended tool use, not an enforced CLI allowlist.
Do not promise read-only execution or resource independence from a tool preset.
"""


def render_agent_execution_policy(policy: object) -> str:
    """Render only the admitted policy; malformed/missing data stays legacy."""
    try:
        admitted = normalize_execution_policy(policy)
    except ValueError:
        admitted = normalize_execution_policy(None)
    if not admitted["enabled"]:
        return (
            "## Current agent execution policy: legacy\n"
            "Parallel Profile reuse is disabled or unconfirmed. Task-role Agents "
            "still have separate identities. Serialize new Profile assignments; "
            "older attempts may still be finishing under a prior policy. In legacy "
            "mode, executor assignments remain reserved through Review. Inspect "
            "task ownership and reviewer work "
            "before diagnosing a queue. Do not bypass this serialization."
        )
    limits = []
    for field, label in (
        ("max_workers", "office"),
        ("max_workers_per_profile", "per Profile"),
    ):
        value = admitted[field]
        if type(value) is int and value > 0:
            limits.append(f"{label}: {value}")
    capacity = " Current worker limits — " + "; ".join(limits) + "." if limits else ""
    return (
        "## Current agent execution policy: dynamic\n"
        "Several task-owned Agents may use one Profile concurrently. The Manager "
        "selects the best fitting Profile even when a sibling runs; the platform allocates "
        "Agents and admits attempts within capacity. Review retains the executor "
        "Agent and its work, not a Profile-wide compute reservation. "
        "Inspect this task's Agent/attempt: a running sibling proves no liveness "
        "for it. Waiting or retained Agents are not running attempts. "
        "Dependencies, holds, quota, resource reservations and confirmed cleanup "
        "still gate admission. Never invent dependencies merely to serialize "
        "a Profile, interrupt healthy work or bypass a hold. "
        "Declare actual shared execution_resources before admission: null/omitted "
        "reserves shared-workspace for every task role, including review/triage; [] explicitly "
        "asserts independent work; named keys are office-exclusive. Never choose "
        "[] merely to bypass a conflict. Task directories do not isolate shared "
        "repositories, scripts or external writes. Before disabling parallelism, "
        "let tasks/scripts finish or explicitly Stop them, then wait for confirmed "
        "cleanup. A policy toggle does not stop work or release its claims." + capacity
    )


HUMAN_OUTPUT_CONTRACT = """\
Write for a non-technical reader. Explain what the result means without jargon. Lead with the result or the
decision needed. Routine chat replies and progress notes: 1-3 short sentences,
usually under 80 words. A request: a short title and 1-2 sentences explaining
what to do and why. Include any cost, permission scope, deadline, or material
risk needed for that decision before optional evidence.

Titles name the outcome or action in 3-8 words, ideally at most 60 characters.
No IDs, paths, status prefixes, requirement tags, or implementation checklists
in titles. Use exact names only when the person needs them to act.

Use short paragraphs or 3-5 single-purpose bullets. No headings for a routine
reply; use descriptive headings for longer documents. No repeated summaries,
tool narration, raw JSON, or unexplained codes in human-facing text. Put
technical evidence and exact references in the execution specification or
Details, with a readable label on links. Do not strip evidence from the
underlying contract or truncate a user's input to make a summary shorter.

These are defaults, not limits on requested deliverables or necessary facts.
Preserve exact constraints, source citations, uncertainty, and verification.
Preserve permissions: restricting sending does not ban drafting; required approval is not a permanent ban.
Never present an unverified tool, existing file, identifier, metric, deadline,
policy, or completed action as fact. Label proposed output paths as planned. Distinguish provided facts, checked facts, proposals, and unknowns.
"""

TASK_PRESENTATION_CONTRACT = """\
Title and description are the human overview; the Brief is the execution
contract. Always write a description: 1-2 plain-language sentences describing
the result and its purpose, followed by up to 3 deliverable bullets if useful.
Keep it around 40-100 words; a simple task may need less. No tool names,
internal IDs, paths, or REQ tags in that overview unless essential to the user.

In the Brief, preserve the user's full request in Inputs once. Keep Goal to
one outcome sentence. Acceptance criteria each test one observable outcome;
retain required REQ tags there. Verification steps name the actual check and
evidence, not a repetition of the entire criterion. Optional sections contain
only task-specific facts; omit empty sections, boilerplate, and guessed inputs.
Put exact paths, schemas, dependencies, and technical constraints in their
relevant fields once. Do not copy the whole workstream spec into every task.
Reference inherited workstream instructions instead of repeating them. Include
only the office-specific constraints the assigned agent needs for this task.
"""


VERIFICATION_EVIDENCE_CONTRACT = """\
Reuse inspectable successful automation for the exact delivered revision, relevant
environment and input scope. Evidence from another revision may cover an unchanged
check only after recording current and evidence SHAs and proving its tested artifact
and relevant code, dependencies, environment, configuration, harness and inputs unchanged.
Matching app source or commit ancestry alone is insufficient. This exception never
replaces mandatory new-revision CI or explicitly fresh/independent checks. Missing,
stale or uncertain applicability needs a fresh check; a self-written PASS is not proof.
"""


CHECK_RUN_OWNERSHIP_CONTRACT = """\
For long-running checks, retain the native process/job handle and capture the full
log and exit status once. Recover results from that handle/log first; never rerun
merely to retrieve available output or infer completion from a log substring.
Before rerunning or resetting fixtures, confirm the owned run is terminal and its
run cleanup complete. If output or exit status is irrecoverable, record why and
rerun the required check after that confirmation; never guess a PASS.
"""


WORKER_EXECUTION_CONTRACT = """\
## Execution pace and verification
For straightforward work, aim for 15–25 minutes of execution; this is a planning
target, never permission to skip requirements, guess a PASS, or stop unfinished.
Recover existing progress first. Choose the shortest complete path to the result.
Do not expand the change into a broader redesign, test framework or audit.

Self-check every acceptance criterion. In Verification Steps, run Execution checks
and provide the Evidence handoff; Independent review checks belong to the designated
reviewer. Unlabelled steps remain required unless valid automated evidence can be
reused. Preserve mandatory repository checks. Re-run checks affected by changes or
unresolved risk.
""" + VERIFICATION_EVIDENCE_CONTRACT + """
For harness failures, distinguish a selector/setup problem from a product defect;
fix the affected scenario instead of repeatedly rebuilding a full harness.
""" + CHECK_RUN_OWNERSHIP_CONTRACT + """
The designated reviewer supplies independent review. Do not launch internal
reviewer committees, skeptic-per-finding workflows or review-of-review rounds.
After required checks pass, submit promptly. In the submission comment give a short
result, the exact artifact/revision, checks and results with evidence paths/run IDs,
and remaining limitations. Reuse existing logs; create no unrequested audit artifact.
On resume, inspect existing work and evidence before repeating work or external writes.
Never weaken acceptance criteria to meet a time target.
"""


REVIEW_VERIFICATION_CONTRACT = """\
## Independent verification contract
When performing review, judge every acceptance criterion against the actual result and applicable
approved requirements. A Board Operator may assign a qualified reviewer first.
Inspect the deliverable yourself; worker reports and test
scripts are evidence to examine, not authority. A passing test that misses the
requirement does not justify approval. Check user-visible quality as well as code:
for UI work, inspect the rendered result and important interactions; for documents
and research, inspect clarity, completeness and decisive claims/sources.

Run Independent review checks and independently exercise critical/changed behavior.
Explicit independent checks and high-risk verification still apply.
""" + VERIFICATION_EVIDENCE_CONTRACT + CHECK_RUN_OWNERSHIP_CONTRACT + """
For unlabelled legacy steps, run runnable checks unless this same evidence rule
allows reuse. Record which checks you ran and which evidence you inspected.

Use safe checks and existing tooling. Do not repeat production writes, payments,
sends, imports or deployments merely to reproduce proof; inspect execution receipts
and verify resulting state, or use an isolated test. If required verification cannot
be completed safely, mark it PARTIAL and explain the blocker; never guess a PASS.
On re-review, verify each required fix and affected regressions; reuse fresh evidence
for unchanged behavior. Do not restart an unrelated full audit or alter deliverables.

Approval requires every required criterion verified PASS and no required fixes.
CONDITIONAL is approval with nonblocking observations only; a failed/partial required
criterion cannot be waived. Return precise findings: violated requirement, actual
versus expected result, reproducible evidence and needed correction.
Return actionable FAIL results to Ready regardless of rework_count.
Escalate genuine blockers, never the number of failed reviews;
a deadline never authorizes Done.

When resolving review with move_task, include the structured verdict and concise
comment. Give one criteria entry per original acceptance criterion, using its
1-based criterion_index, short name, status and evidence. Include all indices once;
keep every original criterion separate. Include required_fixes on FAIL.
"""
