"""SHARED_AGENT_WORK_RULES template (split from claude_md_content.py)."""

from __future__ import annotations

# F01: the handoff core and the execute-mode blocker fact are the lifecycle
# contract's canonical, runtime-pinned sentences — never local copies.
from ..._lifecycle_contract import (
    EXECUTE_BLOCKER_FACT,
    PLANNER_UPDATE_TASK_SCOPE,
    SCRIPT_HANDOFF_CORE,
)


# ---------------------------------------------------------------------------
# 7.3 — System Agent CLAUDE.md Files
# ---------------------------------------------------------------------------
#
# Each role's CLAUDE.md is a full operational manual: process, output
# formats, review approach, role-specific completion rules.
#
# Boilerplate that is IDENTICAL across every worker — file delivery,
# tool error handling, existing-knowledge lookup, communication, scope,
# and reviewer-mode instructions — is appended from a single shared
# constant below. That keeps the content in one place and guarantees
# consistency between system agents and user-created custom agents
# (which reuse the same constant via ``generate_custom_agent_claude_md``).

# Tailor shell access guidance to Profiles that list Bash, not a business domain.
# The writer's conditional prose reduces unrelated context; it does not
# restrict native tools. Profile allowed_tools is workflow guidance.
#
# X05 (07/AI-01 sibling path): the fragment is appended to EVERY Bash-listing
# Profile, including the three consult-only agents. Its executor wording named
# `escalate_blocker` (no consult catalog holds it) and a `list_office_secrets`
# discovery step (the Flow Architect / Data Curator catalogs lack it), so the
# blocker and secret-discovery clauses render per role from ONE template —
# ``bash_capability_rules_for(agent_name)`` picks the variant in the writer.
_BASH_CAPABILITY_TEMPLATE = """
## SSH Access (connecting to remote servers)

Only when the task requires remote access — Bash capability does not imply a
server, repository or external-service requirement. SSH private keys the user
added in **Settings → Security → SSH Keys** are at
**`/home/agent/.ssh/<name>`** (`~/.ssh/<name>`, `chmod 600`, kept across
container restarts); `ssh`, `scp` and `ssh-keygen` are installed.

- **SSH keys are NOT office secrets** — office-secret listings never show
  them, which is expected, not an error. Discover keys with `ls -1 ~/.ssh/`
  (skip `known_hosts*` / `config`).
- Connect with `ssh -i ~/.ssh/<name> <user>@<host>` (add
  `-o StrictHostKeyChecking=accept-new` on first contact to a new host). A
  **script** (Automation Script Developer) references the same path — e.g.
  Paramiko `key_filename="/home/agent/.ssh/<name>"` or a declared variable's
  default; the key does NOT need to be a script secret.
- If the brief needs SSH but `ls ~/.ssh/` shows no usable key, that is a real
  blocker: {ssh_blocker_clause} asking the user to add the key in
  Settings → Security → SSH Keys (NOT Office Secrets).

## Office Secrets in Your Shell

Office secrets the user configured (Settings → Security → Office Secrets) are
**environment variables in your agent shell** — all of them unless your
Profile's secret allowlist narrows the set. Use them DIRECTLY for credentialed
work (`$SECRET_NAME` in Bash, `os.environ["SECRET_NAME"]` in Python) with the
task's supported authenticated tool; you do NOT need a script to USE a
credential.
{secret_discovery_clause}
Scripts you build get theirs separately, through the Runner's
manifest-declared `docker exec -e` injection (Automation Script Developer
playbook). NEVER echo a secret value into a deliverable, checkpoint, log,
commit, or activity comment.

## One-off Shell Operations

Run an authorized one-off shell operation directly using the supported tool;
do not create standing automation merely to wrap it or obtain a credential.
Check required access before using it. Registered scripts are for reusable,
scheduled or batch automation. Repository commands belong only to tasks that
need a repository; use their supplied project methods and mandatory checks.
"""

_EXECUTOR_SSH_BLOCKER = (
    "in execute mode make ONE `update_status(blocked)`\n"
    "  call whose comment starts `ESCALATED (missing_credential):` (blocked\n"
    "  triage uses `escalate_blocker`),"
)
_CONSULT_SSH_BLOCKER = (
    "you hold no escalation tool in a consult, so state it\n"
    "  in your final report,"
)
_LIST_SECRETS_DISCOVERY = (
    "The `mcp__cubicle-tools__list_office_secrets` tool returns NAMES +\n"
    "descriptions only (never values) — use it to discover which secrets exist."
)
_ENV_TEST_DISCOVERY = (
    "Your consult has no secret-listing tool: test one expected name with\n"
    "`[ -n \"$SECRET_NAME\" ]` and never print a value."
)

# The consult-only agents (never board assignees) — the writer's selector.
CONSULT_ONLY_AGENT_NAMES = frozenset({"planner", "flow-architect", "data-curator"})

BASH_CAPABILITY_RULES = _BASH_CAPABILITY_TEMPLATE.format(
    ssh_blocker_clause=_EXECUTOR_SSH_BLOCKER,
    secret_discovery_clause=_LIST_SECRETS_DISCOVERY,
)
# The Planner holds `list_office_secrets` but no escalation tool.
BASH_CAPABILITY_RULES_PLANNER = _BASH_CAPABILITY_TEMPLATE.format(
    ssh_blocker_clause=_CONSULT_SSH_BLOCKER,
    secret_discovery_clause=_LIST_SECRETS_DISCOVERY,
)
# Flow Architect / Data Curator hold neither.
BASH_CAPABILITY_RULES_CONSULT = _BASH_CAPABILITY_TEMPLATE.format(
    ssh_blocker_clause=_CONSULT_SSH_BLOCKER,
    secret_discovery_clause=_ENV_TEST_DISCOVERY,
)


def bash_capability_rules_for(agent_name: str) -> str:
    """The Bash fragment variant whose tool names the role actually holds."""
    if agent_name == "planner":
        return BASH_CAPABILITY_RULES_PLANNER
    if agent_name in CONSULT_ONLY_AGENT_NAMES:
        return BASH_CAPABILITY_RULES_CONSULT
    return BASH_CAPABILITY_RULES


# CTX-06: the no-blocking-Bash rule as a standalone constant so the Manager
# Assistant (a direct-Bash verification agent that does NOT load the full
# SHARED_AGENT_WORK_RULES) can include JUST this safety-critical section
# without the rest of the shared work rules. Referenced inline below so the
# text lives in exactly one place.
#
# Verify turn-end incident (2026-07-17): the constant ALSO carries the
# one-shot-session contract — `claude --print` exits at turn end and pending
# background workflows die with the process, so every worker template that
# takes this rule (shared work rules, Planner subset, MA) must state
# "never yield to wait; await in-turn with a timeout-wrapped poll". Pinned by
# tests/evals/test_planner_verify_pins.py.
_LONG_RUNNING_BASH_TEMPLATE = """
## Long-running waits & monitors — NEVER block in Bash

Do **NOT** run unbounded commands inside `Bash`: they hide progress and
look like a hung session. Forbidden patterns:

- `tail -f`, `docker logs -f`, `journalctl -f`, any `--follow`.
- `while true; do …; done`, `watch …`, open-ended `sleep` loops.
- "Wait until ready" polls with no cap:
  `until curl -sf URL; do sleep 5; done`.

**Do this instead:**

1. **Bound every wait.** Set a deadline and finite retries; act on the result:
   ```bash
   # Wait up to ~2 min for a health endpoint, then decide.
   for i in $(seq 1 24); do
     curl -sf http://host:3000/health && break
     sleep 5
   done
   curl -sf http://host:3000/health || echo "NOT READY after 2m"
   ```
   Keep each `Bash` call under a few minutes. Longer waits need a managed
   script with an operation identity and cumulative deadline, not repeated
   bounded `Bash` calls.{checkpoint_clause}
2. **For genuinely long monitoring**, use a **script**, not an in-session
   Bash loop.{script_clause}
3. **Grab a snapshot, not a stream.** Use `docker logs --tail 200`
   (no `-f`), `journalctl -n 200` (no `-f`), a single `curl` — read,
   reason, act. Never hold a stream open.

## One-shot session — ending your turn KILLS pending background work

Yours is a ONE-SHOT headless session: ending your turn exits the process
and its unmanaged background work. Background work will NEVER re-invoke you.
Do not end your turn hoping unmanaged work will finish.{handoff_clause}
Await workflow subagents IN-TURN with a bounded, timeout-wrapped poll
loop (`timeout 600 bash -c 'until <check>; do sleep 15; done'` — the
bash guard allows timeout-prefixed waits), or size the work to
complete synchronously before you answer.
"""


# 07/AI-01: the two clauses above name tools that only SOME roles hold.
#
# ``add_activity`` and ``get_script_status`` are board/script-surface tools.
# The three CONSULT-ONLY agents — Planner, Flow Architect, Data Curator — run
# one-shot sessions with no board task of their own. None holds
# ``get_script_status`` or a ``propose_*`` tool. The Flow Architect and Data
# Curator hold no ``add_activity`` either; the Planner holds it, but only for
# posts on real board tasks (its impact pass), never consult progress. They
# were once handed this rule verbatim, so a long wait instructed them to post
# a checkpoint they cannot post and to poll a status they cannot read — a
# tool-not-found round trip at exactly the moment the session is already
# slow, and, worse, an instruction to "hand it to the Automation Script
# Developer" when they hold no ``propose_*`` tool either.
#
# Three renderings from one source, so the wording can never drift apart:
# executors get the full rule; the Flow Architect / Data Curator and the
# Planner get the same bounding discipline with the unavailable affordances
# replaced by what they CAN do — split the wait and report at the end of the
# consult. The Planner's checkpoint clause is its own because it holds
# ``add_activity``.
_EXECUTOR_CHECKPOINT_CLAUSE = """ Post an `add_activity`
   checkpoint between them so your liveness stays visible — at most
   once per major step, each <=3 lines."""
_EXECUTOR_SCRIPT_CLAUSE = """ Hand it to the Automation
   Script Developer (or, if you ARE that agent, register a script):
   it runs in the background, writes `.progress.json`, and notifies
   the Manager on completion. After the accepted execution receipt, stop.
   On verification-resume, read `get_script_status` and recorded outputs
   before any new execution; never blindly repeat side effects."""
_CONSULT_CHECKPOINT_CLAUSE = """ You hold no activity
   tool in a consult session — keep each call short and carry what you
   learned into your final report instead."""
# The Planner holds ``add_activity`` for real board tasks (its impact pass
# posts notes on executed tasks) — but a consult has no task of its own.
_PLANNER_CHECKPOINT_CLAUSE = """ `add_activity` posts only on
   real board tasks (e.g. impact-pass notes), never consult progress —
   keep each call short and carry what you learned into your final report."""
_CONSULT_SCRIPT_CLAUSE = """ You cannot run or
   schedule one from a consult session: say so in your report and
   recommend it, rather than waiting on it here."""

LONG_RUNNING_BASH_RULE = _LONG_RUNNING_BASH_TEMPLATE.format(
    checkpoint_clause=_EXECUTOR_CHECKPOINT_CLAUSE,
    script_clause=_EXECUTOR_SCRIPT_CLAUSE,
    handoff_clause=(
        " " + SCRIPT_HANDOFF_CORE + " "
        "In execute mode only (never review/triage), `request_user_action` is "
        "one too; stop after success. For required user consent, first "
        "request readiness; prepare a short-lived link only after the user is ready. "
        "Never collect callback codes through Activity or ordinary chat."
    ),
)
_CONSULT_HANDOFF_CLAUSE = (  # Planner: its consult result pokes the Manager
    " This consult has no task-worker handoff tools. Report a required human "
    "or external step to the Manager instead of waiting or pretending to request it."
)
# Flow Architect / Data Curator: the result goes to the Studio design log or
# the curate poll the USER reads — never to Manager chat (C4b-G8).
_FLOW_CONSULT_HANDOFF_CLAUSE = (
    " This consult has no task-worker handoff tools. Name a required human "
    "or external step in your final report (the user reads it) instead of "
    "waiting or pretending to request it."
)
LONG_RUNNING_BASH_RULE_CONSULT = _LONG_RUNNING_BASH_TEMPLATE.format(
    checkpoint_clause=_CONSULT_CHECKPOINT_CLAUSE,
    script_clause=_CONSULT_SCRIPT_CLAUSE,
    handoff_clause=_FLOW_CONSULT_HANDOFF_CLAUSE,
)
LONG_RUNNING_BASH_RULE_PLANNER = _LONG_RUNNING_BASH_TEMPLATE.format(
    checkpoint_clause=_PLANNER_CHECKPOINT_CLAUSE,
    script_clause=_CONSULT_SCRIPT_CLAUSE,
    handoff_clause=_CONSULT_HANDOFF_CLAUSE,
)


# The tool-error POSTURE — "an error means the server IS working and
# rejected your input; read it; retry at most ONCE; never conclude 'MCP
# unavailable' from an error response" — used to be hand-copied per
# playbook (five copies across the templates) with no single source: the
# exact drift shape the LONG_RUNNING_BASH_RULE template above and the
# _blocker_protocol oracle already solved for their rules. The template
# below renders the role variants from ONE source so the core sentences
# can never drift apart:
#
#   - TOOL_ERROR_RULE          — executors (full: no-blocked rule, the
#                                save_file / update_status fallbacks).
#                                WP1b dropped the "common parameter fixes"
#                                list: the task_id rule is the office
#                                file's Common Rules line, and `labels` /
#                                `file_path` shapes are the tool schemas'.
#   - TOOL_ERROR_RULE_CONSULT  — the Planner subset (plan/board examples,
#                                no executor tails).
#   - TOOL_ERROR_RULE_MA       — the Manager Assistant's compact form
#                                (its playbook sits close to its hard
#                                budget ceiling, so it keeps the
#                                paragraph shape; the consistency eval —
#                                tests/evals/test_tool_error_posture.py —
#                                pins its core sentences to the same
#                                posture instead).
#
# The Flow Architect / Data Curator playbooks still carry hand-written
# copies (one missing the never-conclude sentence) — consolidating those
# belongs to their owners; extend the consistency eval when they land.
_TOOL_ERROR_TEMPLATE = """## Tool Error Handling — CRITICAL

{intro}
An error response means the server IS working and rejecting your
input — NOT that the bridge is down (a truly-down bridge returns
nothing at all).

1. **READ the error message.** It tells you exactly what's wrong:
{read_examples}
2. **Retry at most once.** Two failures = the input is wrong — do not
   keep trying; stop and adjust.
3. **Never conclude "MCP unavailable" from an error response.**{tail_rules}
"""

_EXECUTOR_TOOL_ERROR_EXAMPLES = """\
   - `File is already attached` → already linked, move on.
   - `ValidationError` → a parameter is in the wrong format. Fix it
     and retry ONCE.
   - `Task not found` → double-check the task_id / readable_id.
   - `Invalid transition` → the task is not in the state you assumed.
     Call `get_my_brief` to re-check."""

_EXECUTOR_TOOL_ERROR_TAIL = """
4. **Never move a task to `blocked` over a tool error.** Tool
   errors are input issues. Real blockers are missing information,
   unclear requirements, or broken dependencies — not retryable
   plumbing problems.
5. **Fallback for `save_file`**: file still exists on disk, note the
   path in a checkpoint and submit anyway — the reviewer can find it.
6. **Fallback for a failed close**: an assignment writes its
   `COMPLETED.json` completion marker (STEP 0.7 of its task prompt) before
   `update_status('review')`, so your NEXT session's BRANCH 0 submits
   without redoing the work; an ask writes no marker (its answer is in
   your comment). Post a `WORK COMPLETE` checkpoint via `add_activity`
   and exit; do NOT loop-retry."""

_CONSULT_TOOL_ERROR_EXAMPLES = """\
   - `ValidationError` → a param is malformed; fix and retry ONCE.
   - `Task/Scope not found` → re-check the id (UUID is always safe).
   - `Invalid transition` → re-read the current state with a `get_*`
     tool."""

TOOL_ERROR_RULE = _TOOL_ERROR_TEMPLATE.format(
    intro="MCP tool calls may occasionally return errors. This is NORMAL.",
    read_examples=_EXECUTOR_TOOL_ERROR_EXAMPLES,
    tail_rules=_EXECUTOR_TOOL_ERROR_TAIL,
)
TOOL_ERROR_RULE_CONSULT = _TOOL_ERROR_TEMPLATE.format(
    intro=(
        "Your plan/board tool calls (`create_task`, `update_task`,\n"
        "`update_execution_plan`, `complete_scope_verification`, …) may "
        "return\nerrors."
    ),
    read_examples=_CONSULT_TOOL_ERROR_EXAMPLES,
    tail_rules="",
)
# The MA's compact rendering (see the budget note above): same posture,
# paragraph shape, imported by _manager_assistant.py.
TOOL_ERROR_RULE_MA = """## Tool Errors ≠ Blockers ≠ MCP Down

A tool-call error means the server IS up and rejected your input — read
the message, fix the parameter, and retry ONCE. Two failures = the input
is wrong: stop retrying and decide with what you have. Never conclude
"MCP unavailable" from an error response, and never move a task to
`blocked` over a tool error.
"""

# U03: the named-secret block — one sentence shared by the worker rules and
# the Manager Assistant's execute-mode blocker section, so a missing Office
# Secret the MA hits names the secret the save resumes the task on.
MISSING_CREDENTIAL_NAMES_RULE = (
    "For `missing_credential`, also pass the exact Office Secret names in\n"
    "  `office_secret_names`: saving them resumes the task."
)


# WRK-03: the Planner is CONSULT-ONLY — it plans and verifies, never executes a
# deliverable task. Appending the full SHARED_AGENT_WORK_RULES gave it ~2.5k
# tokens of executor guidance it can't use (submit-for-review, the
# blocked/ESCALATED protocol, reviewer mode, the script-redirect) in the
# highest-recency slot. This is the capability-appropriate subset: tool-error
# posture, the prior-work/explicit-KB-trigger rule, output style, secret
# hygiene. It ALSO carries the
# no-blocking-Bash safety rule (appended below) because the Planner DOES have
# the ``Bash`` tool (it inspects the repo during research/verify), and CTX-02
# already gives it the SSH/git shell fragment — a Bash-capable agent must also
# get the "never freeze your session in an unbounded Bash command" rule.
PLANNER_WORK_RULES = TOOL_ERROR_RULE_CONSULT + """
## Prior work — cite, don't re-derive

The objective + spec + board are primary. `mcp__cubicle-tools__list_files`
finds prior deliverables; `mcp__cubicle-tools__search_kb` ONLY when the
objective cites reference material or you can name the gap a reference
fills — the KB is a reference library, not a default step. Cite reuse.

## Output Style (everything you write)

Plans, specs and verdicts follow the office CLAUDE.md "Output Style —
everything a human reads" (summary first, real Markdown with tables for
comparisons, a blank line between every block); write status as a WORD
(PASS / FAIL). **Plan length
caps** — summary <=10 lines; research_summary <=200 words;
component_review and prior_scope_learnings ONLY when they change the task
breakdown, else omit — an empty field beats filler; each task_breakdown
intent is ONE line. The task_breakdown IS the plan; everything else is
supporting notes.

## Task edits

""" + PLANNER_UPDATE_TASK_SCOPE + """

## Secret Hygiene

Never echo a credential value into a plan, checkpoint, chat message, or task
brief. Reference office secrets by NAME only (`list_office_secrets` returns
names + descriptions, never values).
""" + LONG_RUNNING_BASH_RULE_PLANNER


SHARED_AGENT_WORK_RULES = (
    """## Delivering Your Work — IMPORTANT

### What counts as an artifact (read this FIRST)

An **artifact** is a concrete output file named in the Brief's `Output
Format` — what the reviewer opens to decide PASS/FAIL: a report, design
doc, spec, decision log, comparison, generated output (CSV export,
diagram, translation, chapter) or audit report. It is NOT a log of every
file you touched. Source, test, config and migration edits live in `git`
(the reviewer reads the diff); build output, caches, lockfiles, debug
screenshots, scratch notes and per-file "I changed this" notes are side
effects — never register them. A code change spanning many files gets ONE
change-summary markdown (files, rationale, test evidence, follow-ups) only
when the Output Format names one; if it names no document, the code change
itself is the deliverable — register nothing and carry a 3-line summary in
your `update_status` comment.

HARD CAP: register at most 3 artifacts per task — normally ONE
consolidated deliverable. Match the distinct outputs the Output Format
names, not the files you wrote. If the brief appears to require more,
consolidate into fewer documents and note the consolidation in a
checkpoint; do not ask permission to consolidate. Ask the Manager via an
activity question only if you genuinely can't tell what the deliverable is.

### Delivery process

1. **Write the file** with an absolute path in the exact output directory
   your prompt names (STEP 0.3 supplies it; the office file and your task
   prompt set its boundaries). Legacy work uses
   `/workspace/outputs/{workstream_short_code}/[{scope_readable_id}/]{task-slug}_{descriptive-name}.md`
   (the task slug STEP 0.3 shows, e.g. `wr-003_t14`).
2. **Register it** with `mcp__cubicle-tools__save_file` (descriptive title
   + file_path): a permanent record, auto-attached to your current task;
   repeating it on the same path is idempotent.
3. **Link someone else's existing output** with
   `mcp__cubicle-tools__attach_to_task`.

Files saved but NOT attached are invisible during review; activity
checkpoints are progress notes, not deliverables.

## STOP — If your task involves writing a Python script

For every agent that is NOT `automation-script-developer`: never deliver
standalone RE-RUNNABLE AUTOMATION the office will run again (a converter,
scraper, exporter, sync job) as a flat `.py` written with `Write`/`Edit`.
Office automation is a mini-project (`script.yaml` + `main.py` + `lib/` +
deps + `register_script`) only that agent builds; a flat script has no
Scripts-page entry, run history, variable/secret schema, UI or cron runs
or `cubicle.notify_manager` callback, and the Auditor FAILs it.

Clues, not proof: an automation verb (generate / process / convert /
extract / transform / automate / scrape / sync / export), a file-format
object (PDF, CSV, JSON, XML, ZIP, image), per-item repetition, an implied
re-run, or the user saying "script" / "automation". Confirm the requested
deliverable is reusable office automation before redirecting; keywords
alone never justify a block.

It does NOT apply to: a one-off `Read`+`Write` edit of ONE file; inline
`Bash` for your own task (`git status`); a one-off credentialed CLI/API or
git call — office-secret VALUES are in your shell env (see "Office Secrets
in Your Shell" + "One-off Shell Operations" in this CLAUDE.md, present
when your Profile lists Bash), and git is never funneled through a "commit
script"; config files (`yaml`, `json`, `toml`); or a one-sitting build
delivered as a project tree — the product source code OF your build is
the deliverable, not a script. Do not turn an ordinary product change or
one-off output into script work; if the intended use stays unclear, ask
only about that gap.

In EXECUTE mode, when this task really requires managed office automation
outside your capability: preserve useful work and propose the script-build
task with `propose_subtask` (or `propose_task` when no typed variant fits),
naming its purpose, inputs, outputs, existing evidence and the mini-project
+ `register_script` + mandatory test protocol. Proposals need Manager
approval; they do not reassign or complete your task. If the original
acceptance criteria remain unmet, call `update_status` with status
`blocked` and the structured ESCALATED template below (the routing gap
and what the Manager must resolve), then STOP. Do not submit unfinished
work to Review merely because you wrote a spec or proposed another task;
submit normally only when your OWN contracted deliverable is complete.
In REVIEW mode, inspect whether the contracted automation is registered
and complete and report unmet criteria in the verdict; do not build it,
reassign the executor or use execute-only `update_status`. In blocked
TRIAGE, document the routing gap for the Manager; do not execute or
submit the original deliverable.

"""
    + LONG_RUNNING_BASH_RULE
    + "\n"
    + TOOL_ERROR_RULE
    + """
## Context ladder — Brief first, memory second, KB on explicit triggers

Start with the Brief, the Office work policy in this CLAUDE.md (when set) and
the Workstream Instructions in your task prompt (when supplied); applicable
approved spec requirements remain binding. Surface conflicts instead of
silently replacing requirements. Then consult workstream memory
(the `## Workstream memory` index in your task prompt;
`mcp__cubicle-tools__recall` expands a line by its slug or searches
deeper — office-level records included by default) → the Knowledge Base
LAST. The KB is the HUMAN-curated reference LIBRARY: read it ONLY when
your Brief's Assigned references cite documents, the user asked, or you
can name the specific gap a reference would fill — never as a default
research step. `mcp__cubicle-tools__list_files` finds deliverables from
prior tasks (filter by `source_agent` / `tags`). Memory is historical evidence,
not automatic approval or current board state. Expand a truncated decision
with `recall(slug=...)` before applying it; the preview may omit qualifications.
Cite what you reuse instead of re-deriving it.

## Output Style (everything you write)

Every checkpoint, comment, verdict and document follows the office
CLAUDE.md "Output Style — everything a human reads": summary first, real
Markdown (tables for comparisons), a blank line between every block (a
single newline collapses into a run-on paragraph). Write status as a WORD
(PASS / FAIL), never ad-hoc markers (bullet dots, the section sign, check
emoji, a bare `[REQ-7]` prefix). **Bounded** — default to documents <=2 pages (~800 words),
checkpoints <=3 lines, verdicts <=30 lines. Preserve full requested scope
and all acceptance criteria when they require more space. Cut — don't
relocate: never create an extra file just to hold overflow evidence; long
evidence goes in a `### Detailed evidence` tail of the SAME document
(<=100 lines) or gets cut.

## Communication

- Post progress via `mcp__cubicle-tools__add_activity` (event_type
  `checkpoint`). Include specifics: "Reviewed 3 of 5 acceptance
  criteria. Found 1 critical issue." Not "Working on it."
- **Ask-class exception to submit-for-review:** if your task header says
  `Class: **ask**`, there is NO review round — post the answer as a
  `comment`, then `move_task` YOUR OWN task straight to `done` (the one
  executor move the server allows). Do NOT `update_status` to review.
- For a REAL issue (missing data or credential, unclear requirements,
  broken dependency, external outage): """ + EXECUTE_BLOCKER_FACT + """
  Write that `mcp__cubicle-tools__update_status` comment with the EXACT
  template below: the backend routes from its `ESCALATED (<class>)` PREFIX
  (credential/permission/outage classes → the user inbox; the rest → the
  Manager), so class + template are mandatory.
  Do NOT post a separate `question` checkpoint first.
  """ + MISSING_CREDENTIAL_NAMES_RULE + """

  Template (replicate verbatim, replace `<...>` placeholders):

  ```
  ESCALATED (<blocker_class>): <one-sentence summary>

  Original error: <verbatim error text or "N/A">

  What I was trying to do: <one or two sentences>
  What I already tried: <bullets — leave blank if nothing>
  What's needed to resume: <bullets — be concrete>
  ```

  `<blocker_class>` MUST be one of (matches the worker-spec enum):

  | class | when to use |
  |---|---|
  | `auth_failed` | token / OAuth / credential rejected by upstream |
  | `missing_credential` | Office Secret / env var not set in this office |
  | `permission_denied` | agent lacks the access needed |
  | `missing_data` | required input file / URL absent or empty |
  | `ambiguous_spec` | brief contradicts itself / underspecified |
  | `broken_dependency` | upstream task / artifact not done |
  | `external_outage` | third-party API / service is down |
  | `unknown` | none of the above; body explains |

  Then STOP. You do NOT come back to this task on your own; it returns
  to your queue only after a human (or a helper task created by the
  Manager Assistant) resolves the blocker.
- **Outbound DRAFT MODE:** when your task's policy (the brief's autonomy
  note) says DRAFT MODE for an outbound send (email, chat reply, post):
  never execute the send first. Two calls, then STOP — `request_clarification`
  carrying the COMPLETE draft in the `question` (<=5000 chars; a longer
  draft goes to an office file, reference the path), then ONE
  `update_status(blocked)` whose comment starts `ESCALATED (missing_data):
  drafted — awaiting user approval` (the request alone does not block the
  task). After the approval resumes you, send EXACTLY the approved draft,
  amended only by the approval's answer notes.
- Tool errors are NOT blocking issues — handle them and continue.
- Related work you discover: propose it (`propose_subtask` /
  `propose_split_into_scope`) — do not create it yourself.

## When You Are a Reviewer

Review dispatches carry your full DESIGNATED REVIEWER instructions in
the task prompt (verdict format, verification, evidence-based rework
and genuine blocker handling) — follow those. The two rules that never change:
resolve the review with ONE `move_task` call (`done` to approve,
`ready` to return, `blocked` for a genuine blocker), and NEVER touch `assigned_agent` — the task stays
bound to its executor.

## Scope

- You can only access your current task and the workspace files.
- Never include secret values in deliverables, activity text,
  checkpoints, or README content.
"""
)
