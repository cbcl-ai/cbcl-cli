"""SHARED_AGENT_WORK_RULES template (split from claude_md_content.py)."""

from __future__ import annotations


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

# CTX-02: SSH / office-secrets-in-shell / direct-git guidance applies ONLY
# to agents that actually have the ``Bash`` tool (ASD, Manager Assistant,
# Auditor, and Bash-capable custom agents). It used to live in the SHARED
# office CLAUDE.md that EVERY agent loads — ~2.8k chars of dead context for
# the Manager, Analyst, and Planner (none of which can run a shell). The
# writer appends this fragment to a per-agent playbook only when that
# agent's allowed_tools includes ``Bash``.
BASH_CAPABILITY_RULES = """
## SSH Access (connecting to remote servers)

SSH private keys the user added in **Settings → Security → SSH Keys** are
written into this container at **`/home/agent/.ssh/<name>`** (i.e.
`~/.ssh/<name>`), already `chmod 600`. The `openssh-client` (`ssh`, `scp`,
`ssh-keygen`) is installed.

- **SSH keys are NOT office secrets.** Do NOT look for them with
  `list_office_secrets` — that tool only lists shared *named credentials*
  (API keys etc.). A missing SSH key will never show up there; that is
  expected, not an error. To discover what keys are actually present, run
  `ls -1 ~/.ssh/` (skip `known_hosts*` / `config`).
- To connect from a worker that has the `Bash` tool:
  `ssh -i ~/.ssh/<name> <user>@<host>` (add
  `-o StrictHostKeyChecking=accept-new` on first contact to a new host).
- From a **script** (Automation Script Developer), reference the same path —
  e.g. Paramiko `key_filename="/home/agent/.ssh/<name>"`, or pass the path as
  a declared variable's default. The key file is bind-mounted and survives
  container restarts; it does NOT need to be a script secret.
- If the brief needs SSH but `ls ~/.ssh/` shows no usable key, that is a real
  blocker: `escalate_blocker` with `blocker_class=missing_credential` asking
  the user to add the key in Settings → Security → SSH Keys (NOT Office Secrets).

## Office Secrets in Your Shell

Office secrets the user configured (Settings → Security → Office Secrets) —
API keys, `GITLAB_PAT`, etc. — are injected as **environment variables into
your agent shell**. Use them DIRECTLY for credentialed work during your task:

- Bash: `$SECRET_NAME` — e.g.
  `git push https://oauth2:$GITLAB_PAT@gitlab.com/group/repo.git HEAD`, or
  `curl -H "Authorization: Bearer $API_KEY" https://api.example.com/...`.
- Python: `os.environ["SECRET_NAME"]`.

You do NOT need to build or run a script to USE a credential. The
`mcp__cubicle-tools__list_office_secrets` tool still returns NAMES +
descriptions only (never values) — use it to discover which secrets exist.
The Runner's manifest-declared, `docker exec -e` injection (Automation Script
Developer playbook) is a SEPARATE path that applies only to *scripts you
build*. NEVER echo a secret value into a deliverable, checkpoint, log, commit,
or activity comment.

## Git is Direct, Not a Script

You have `git` + `openssh-client` + an SSH key in `~/.ssh/` + credentials in
your env. Clone / commit / push to GitLab/GitHub **directly** with `Bash` —
over SSH using the key (`git@gitlab.com:...`) or https using `$GITLAB_PAT`. Do
NOT route a one-off git operation through a registered automation script:
scripts are for reusable / scheduled / batch automation, never a git
chokepoint or a way to obtain a credential. A script touches git only when the
git step is itself part of repeatable/scheduled automation.
"""


# CTX-06: the no-blocking-Bash rule as a standalone constant so the Manager
# Assistant (a direct-Bash verification agent that does NOT load the full
# SHARED_AGENT_WORK_RULES) can include JUST this safety-critical section
# without the whole ~18k-char playbook. Referenced inline below so the
# text lives in exactly one place.
LONG_RUNNING_BASH_RULE = """
## Long-running waits & monitors — NEVER block in Bash

Do **NOT** run unbounded / open-ended commands inside the `Bash`
tool. They freeze your session inside one tool call: you post no
progress, the orchestrator can't tell the session apart from a real
hang, and the work-monitoring sweeper may raise a false "wedged
task" alarm. Forbidden patterns:

- `tail -f`, `docker logs -f`, `journalctl -f`, any `--follow`.
- `while true; do …; done`, `watch …`, open-ended `sleep` loops.
- "Wait until ready" polls with no cap:
  `until curl -sf URL; do sleep 5; done`.

**Do this instead:**

1. **Bound every wait.** Give it a hard ceiling and a finite retry
   count, then act on the result:
   ```bash
   # Wait up to ~2 min for a health endpoint, then decide.
   for i in $(seq 1 24); do
     curl -sf http://host:3000/health && break
     sleep 5
   done
   curl -sf http://host:3000/health || echo "NOT READY after 2m"
   ```
   Keep a single `Bash` call comfortably under a few minutes. If a
   wait legitimately needs longer, split it across separate bounded
   `Bash` calls and post an `add_activity` checkpoint between them so
   your liveness stays visible.
2. **For genuinely long monitoring** (a deploy that takes 10+ min, a
   log you must follow, a batch that runs for hours) — that's a
   **script**, not an in-session Bash loop. Hand it to the Automation
   Script Developer (or, if you ARE that agent, register a script):
   it runs in the background, writes `.progress.json`, and notifies
   the Manager on completion. Then poll its status with
   `mcp__cubicle-tools__get_script_status` between short, bounded
   steps — never sit blocked waiting for it.
3. **Grab a snapshot, not a stream.** Use `docker logs --tail 200`
   (no `-f`), `journalctl -n 200` (no `-f`), a single `curl` — read,
   reason, act, repeat. Never hold a stream open.

Rule of thumb: **no single `Bash` call should be expected to run more
than a couple of minutes.** If it would, bound it or move it to a
script.
"""


# WRK-03: the Planner is CONSULT-ONLY — it plans and verifies, never executes a
# deliverable task. Appending the full SHARED_AGENT_WORK_RULES gave it ~2.5k
# tokens of executor guidance it can't use (submit-for-review, the
# blocked/ESCALATED protocol, reviewer mode, the script-redirect) in the
# highest-recency slot. This is the capability-appropriate subset: tool-error
# posture, KB-first, output style, secret hygiene. It ALSO carries the
# no-blocking-Bash safety rule (appended below) because the Planner DOES have
# the ``Bash`` tool (it inspects the repo during research/verify), and CTX-02
# already gives it the SSH/git shell fragment — a Bash-capable agent must also
# get the "never freeze your session in an unbounded Bash command" rule.
PLANNER_WORK_RULES = """## Tool Error Handling — CRITICAL

Your plan/board tool calls (`create_task`, `create_scope`, `update_task`,
`update_execution_plan`, `complete_scope_verification`, …) may return errors.
An error means the server IS working and rejecting your input — NOT that the
bridge is down (a truly-down bridge returns nothing).

1. **READ the error.** `ValidationError` → a param is malformed; fix and retry
   ONCE. `Task/Scope not found` → re-check the id (UUID is always safe).
   `Invalid transition` → re-read the current state with a `get_*` tool.
2. **Retry at most once.** Two failures = the input is wrong; stop and adjust.
3. Never conclude "MCP unavailable" from an error response.

## Existing Knowledge — check BEFORE planning

Before researching, look for prior work: `mcp__cubicle-tools__search_kb` and
`mcp__cubicle-tools__list_files`. Cite what exists instead of re-deriving it —
duplicating work is waste, and your plan should build on prior scopes.

## Output Style (everything you write)

Every plan, roadmap, and verdict a human (or the Manager) reads MUST be
**scannable** — the full rules are in the office CLAUDE.md
(`/workspace/CLAUDE.md`, "Output Style — everything a human reads"). Essentials:

- **Summary first** — lead with the one-line outcome / verdict / key decision.
- **Real Markdown** — `##`/`###` headings, `-` bullets, **bold** labels, tables
  for comparisons. Write status as a WORD (PASS / FAIL), never ad-hoc markers.
- **Blank line between every block** — a single newline collapses into an
  unreadable run-on paragraph on render.

## Secret Hygiene

Never echo a credential value into a plan, checkpoint, chat message, or task
brief. Reference office secrets by NAME only (`list_office_secrets` returns
names + descriptions, never values).
""" + LONG_RUNNING_BASH_RULE


SHARED_AGENT_WORK_RULES = """## Delivering Your Work — IMPORTANT

### What counts as an artifact (read this FIRST)

An **artifact** is one of the concrete output files named in the
Brief's `Output Format` field — the thing the reviewer will open to
decide PASS/FAIL. It is NOT a log of every file you touched.

The test is simple: **if the reviewer had to evaluate your task
without reading any code, which file(s) would they open?** Those are
the artifacts. Everything else is a side effect of the work and
belongs in `git` / the PR / activity checkpoints, not in `save_file`.

Register these as artifacts:
- A research report, design doc, spec, decision log, summary,
  comparison table, or any standalone document the brief asked for.
- A generated output the task exists to produce (a CSV export, a
  rendered diagram, a finished translation, a chapter draft).
- A self-contained review / audit report that the Manager will read.
- A PR-description-style **change summary** when the deliverable is a
  code change spanning many files — ONE markdown file that lists the
  files touched, the rationale, the test evidence, and any follow-ups.

Do NOT register as artifacts:
- Source files you edited or created while implementing a feature,
  refactor, or bug fix. They live in `git` — the reviewer reads the
  diff. Listing 30 `.ts`/`.py` files as artifacts is noise, not signal.
- Files written by tools as a side effect of your work (build output,
  caches, temp files, generated lockfiles, screenshots taken for
  debugging).
- Intermediate scratch notes, planning files, or working drafts you
  used during execution but that aren't the contracted output.
- Configuration changes, migrations, or test files attached to a
  feature task — they are PART OF the change, not separate deliverables.
- Files that exist solely to document what you just did at the file
  level (a per-file "I changed this" note). Use ONE summary instead.

Rule of thumb: **the count of artifacts should match the count of
distinct outputs named in the Brief's `Output Format`, not the count
of files you happened to write.** A task whose output_format says
"a markdown report and a CSV export" → 2 artifacts. A task whose
output_format says "implement the auth endpoint with tests" → 1
artifact (the PR-summary markdown), even if you touched 12 files.

If the brief is silent on output format, default to ONE summary
markdown describing what you did and where the change lives. Ask the
Manager via an activity question if you genuinely can't tell what
the deliverable should be.

### Delivery process

For every artifact identified above:

1. **Write the file** — use the `Write` tool (or your role's usual
   writing tool) to create the deliverable at a clear path inside
   the output directory the prompt named for you. Under the per-
   workstream layout this is
   `/workspace/outputs/{workstream_short_code}/[{scope_readable_id}/]{descriptive-name}.md`
   (STEP 0.3 of your task prompt lists the exact per-workstream
   directory via the `Glob` patterns). Do NOT write to the flat
   `/workspace/outputs/` root — it is reserved for legacy artifacts.
2. **Register with the office** — call `mcp__cubicle-tools__save_file`
   with a descriptive title and the file_path. This creates a permanent
   record AND auto-attaches the file to your current task.
3. **Re-attach an existing file** — if you reference a file that was
   already saved in an earlier task, call
   `mcp__cubicle-tools__attach_to_task` to link it to this task as
   well. `save_file` on a path that already has a file record is
   idempotent (it re-attaches), so prefer `save_file` for your OWN
   deliverables and `attach_to_task` only for linking someone else's
   output.
4. If `save_file` fails, DO NOT PANIC (see Tool Error Handling below).
   The file still exists on disk. Post a checkpoint noting the path and
   move on.

Task artifacts are how the Manager and reviewers find your work. Files
saved but NOT attached are invisible during review. Activity checkpoints
are **progress notes**, not deliverables. Source files touched during
implementation are evidence of work, not artifacts — leave them in `git`.

For tool calls that need a `task_id`, every task-scoped tool —
`add_activity`, `update_task`, `update_status`, `move_task`,
`get_task_detail`, `attach_to_task` — accepts EITHER the **task
UUID** (field labeled `Task UUID: <uuid>` near the top of your
prompt) OR the **readable_id** (the short ID like `AX-003.T04`).
The UUID from your brief is always safe; the readable_id is
convenient when copying from chat.

## STOP — If your task involves writing a Python script

This applies to **every agent that is NOT `automation-script-developer`**.

If your task asks you to deliver a `.py` script, generate Python
automation, build a converter / scraper / exporter, or write any
code that would run as a standalone program, **DO NOT use `Write`
or `Edit` to drop a flat `.py` file**. The office has a strict
mini-project pipeline (`script.yaml` + `main.py` + `lib/` +
deps + DB registration via `register_script`) and only the
`automation-script-developer` agent is set up to execute it.

A flat `.py` written outside the mini-project layout:
- Doesn't appear in the Scripts page
- Has no execution history
- Has no variable / secret schema (so secrets get hardcoded)
- Cannot be run from the UI or scheduled via cron
- Cannot call back to the Manager via `cubicle.notify_manager`
- Will be FAILed by the Auditor's script-check protocol
  whether or not your task was officially "a script task"

### What to do instead

1. **Stop writing the file.** If you've already created a flat
   `.py`, you have NOT completed the task — you've made it worse
   (orphan files clutter the workspace).
2. **Post a checkpoint** explaining: "This task requires a
   registered script, which is outside my scope. Proposing
   re-assignment to automation-script-developer."
3. **Propose the re-assignment to the Manager.** Prefer a typed
   proposal tool (e.g. `propose_subtask` for the script-build task, or
   `propose_split_into_scope` for a larger body of work) — those carry
   structured fields the Manager acts on directly. `propose_task` is the
   simple fallback when no typed variant fits. Either way, give a brief that:
   - Names the script's purpose, inputs, outputs.
   - References any spec or requirements you produced (these ARE
     valid deliverables for you — a `.md` algorithm spec, an API
     contract, a data-format definition).
   - Includes the Manager's standard script acceptance criteria
     (registered, mini-project layout, test evidence — see your
     Manager CLAUDE.md if you have access; otherwise just state
     "follow the mini-project + register_script protocol").
4. **Submit your task** via `update_status` with status `review`
   and your spec deliverable attached. The reviewer will see the
   propose_task and route the script work correctly.

### When does this apply?

The same five detection signals from the Manager apply to you:
- Verb is "generate", "process", "convert", "extract",
  "transform", "automate", "scrape", "sync", "export".
- Object is a file format (PDF, CSV, JSON, XML, ZIP, image).
- The action repeats over a list (per-chapter, per-row, …).
- The task implies running again later.
- The user mentioned "script" or "automation" anywhere.

If two or more apply, you are looking at a script task — STOP
and redirect.

### When this does NOT apply

- One-off `Read`+`Write` to transform a single file using your
  workspace tools (e.g. reformat ONE markdown file). That's a
  document edit, not a script.
- Inline shell commands via `Bash` for one-time operations during
  your own task (e.g. `git status`). Those aren't deliverables.
- A one-off **credentialed** CLI/API call or git operation during
  your task — `git clone/commit/push` (over SSH with the key in
  `~/.ssh/`, or https with `$GITLAB_PAT`), a single authenticated
  `curl`/CLI call reading an office secret from `$VAR`. Office-secret
  VALUES are in your shell env (see the office CLAUDE.md "Office
  Secrets in Your Shell" + "Git is Direct" sections) — run it
  directly with `Bash`. You build a registered script ONLY when the
  work is reusable / scheduled / batch; git is never funneled
  through a "commit script".
- Configuration files (`yaml`, `json`, `toml`) — those are config,
  not scripts.

If in doubt, treat the work as a script task and propose
re-assignment. Over-redirecting costs one extra task; under-
redirecting produces orphan files that have to be cleaned up
later.

""" + LONG_RUNNING_BASH_RULE + """

## Tool Error Handling — CRITICAL

MCP tool calls may occasionally return errors. This is NORMAL — an
error response means the server IS working and rejecting your input.

When a tool call returns an error:

1. **READ the error message.** It tells you exactly what's wrong:
   - `File is already attached` → already linked, move on.
   - `ValidationError` → a parameter is in the wrong format. Fix it
     and retry ONCE.
   - `Task not found` → double-check the task_id / readable_id.
   - `Invalid transition` → the task is not in the state you assumed.
     Call `get_my_brief` to re-check.
2. **Retry at most once.** If it fails twice, the input is wrong —
   do not keep trying.
3. **An error response is NOT "the server is down".** Never conclude
   "MCP unavailable" from an error response. If the MCP bridge were
   truly down, you'd get no response at all.
4. **Never move a task to Blocked because of a tool error.** Tool
   errors are input issues. Real blockers are missing information,
   unclear requirements, or broken dependencies — not retryable
   plumbing problems.
5. **Fallback for `save_file`**: file still exists on disk, note the
   path in a checkpoint and submit anyway — the reviewer can find it.
6. **Fallback for `update_status`**: you will already have written the
   `COMPLETED.json` completion marker (STEP 0.7 of your task prompt)
   immediately before submitting, so a transient `update_status` failure
   is recoverable — your NEXT session's BRANCH 0 reads that marker and
   submits without redoing the work. Post a `WORK COMPLETE` checkpoint via
   `add_activity` and exit; do NOT loop-retry.

**Common parameter fixes:**
- `labels` must be a JSON array: `["tag1", "tag2"]` — not a comma string.
- `task_id` accepts BOTH the UUID from the brief AND the
  readable_id (e.g. `AX-003.T04`) on every task-scoped tool.
- `file_path` is a full path starting with `/workspace/`.

## Existing Knowledge — check BEFORE starting

Before any research or analysis task, check for relevant existing work:

- `mcp__cubicle-tools__search_kb` — find existing knowledge documents.
- `mcp__cubicle-tools__list_files` — find deliverables from prior tasks
  (filter by `source_agent` or `tags`).

If prior work covers part of what you were asked to do, cite it in
your deliverable instead of repeating it. Duplicating work is waste.

## Output Style (everything you write)

Every checkpoint, comment, verdict, and document a human reads MUST be
**scannable** — the full rules are in the office CLAUDE.md
(`/workspace/CLAUDE.md`, "Output Style — everything a human reads"). The
essentials:

- **Summary first** — lead with the one-line outcome / verdict / key result.
- **Real Markdown** — `##`/`###` headings, `-` bullet lists, **bold** labels,
  tables for comparisons. NEVER ad-hoc markers (bullet dots, the section sign,
  check emoji, a bare `[REQ-7]` prefix); write status as a WORD (PASS / FAIL).
- **Blank line between every block** — a single newline collapses on render and
  turns your text into one unreadable run-on paragraph. This is the #1 cause of
  the "wall of text" complaint — always separate blocks with a blank line.
- **Bounded** — keep the body short; push long evidence into a `### Detailed
  evidence` tail or a saved report file you reference by name.

## Communication

- Post progress via `mcp__cubicle-tools__add_activity` (event_type
  `checkpoint`). Include specifics: "Reviewed 3 of 5 acceptance
  criteria. Found 1 critical issue." Not "Working on it."
- If blocked by a REAL issue (missing data, unclear requirements,
  broken dependency, missing credential, external outage), make ONE
  call: `mcp__cubicle-tools__update_status` with status `blocked` AND
  a `comment` written using the EXACT template below. The backend
  routes the escalation from the `ESCALATED (<class>)` PREFIX in your
  comment (credential/permission/outage classes → the user inbox; the
  rest → the Manager), so the class enum + template are mandatory —
  free-form prose alone falls back to fuzzy keyword routing and can
  misroute. Do NOT post a separate `add_activity` or `question` first;
  the class travels in this one comment.

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

  Then STOP. Do NOT post a separate `question` checkpoint — the
  ``update_status`` comment IS the canonical "this task is blocked
  because X" entry. You do NOT come back to this task on your own;
  it returns to your queue only after a human (or a helper task
  created by the Manager Assistant) resolves the blocker.
- Tool errors are NOT blocking issues — handle them and continue.
- If you discover related work that should be done, use
  `mcp__cubicle-tools__propose_task` — do not create it yourself.

## When You Are a Reviewer

The Manager sometimes assigns you a task that is already in `review`.
You are REVIEWING another agent's work, not executing new work.

1. Call `mcp__cubicle-tools__get_my_brief` to read the brief + activity.
2. Locate and read deliverables via `list_files` + `get_file` + `Read`.
3. Check each Acceptance Criterion explicitly: PASS / FAIL / PARTIAL
   with evidence (file path, line number, quoted text).
4. Run any Verification Steps from the brief.
5. Compose your verdict in the summary-first shape: a bold
   `**VERDICT: PASS/FAIL/CONDITIONAL**` line + a one-sentence rationale, a blank
   line, then a `### Criteria` list (one line per criterion: name — status —
   terse evidence), then a `### Required fixes` section on a FAIL. Keep it
   bounded — long evidence and logs go in a saved report file you reference,
   not inline.
6. **Resolve the task with ONE `move_task` call** — approve to `done` (PASS /
   CONDITIONAL) or return to `ready` (FAIL, for rework). Pass your verdict on
   this call: `comment` = the Markdown verdict from step 5, and `verdict` = the
   structured object `{overall, rationale, criteria, required_fixes}` so the UI
   renders a card. You are the reviewer; you have the authority. NEVER touch
   `assigned_agent`: the
   task stays bound to the agent that EXECUTED it for its whole
   lifecycle, so a FAIL return lands straight back on that executor
   (the no-unassign-after-Ready invariant; the backend rejects clearing
   it anyway). Do NOT end your session with the task still in `review`
   (unless escalating at the rework cap) — an unresolved review gets
   re-dispatched to you in a loop.

## Scope

- You can only access your current task and the workspace files.
- Never include secret values in deliverables, activity text,
  checkpoints, or README content.
- Read the workstream CLAUDE.md (at `/workspace/workstreams/<slug>/`)
  for project-specific conventions and context.
"""


