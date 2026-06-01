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

For tool calls that need a `task_id`, you can use EITHER the
**task UUID** (field labeled `Task UUID: <uuid>` near the top of
your prompt) OR the **readable_id** (the short ID like
`AX-003.T04`). Every task-scoped tool — `add_activity`,
`update_task`, `update_status`, `move_task`, `get_task_detail`,
`save_file`, `attach_to_task` — accepts both shapes. Prefer the
readable_id when copying from chat; the UUID when working from
your brief.

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
   `propose_action` (e.g. `propose_subtask` for the script-build task, or
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
- Configuration files (`yaml`, `json`, `toml`) — those are config,
  not scripts.

If in doubt, treat the work as a script task and propose
re-assignment. Over-redirecting costs one extra task; under-
redirecting produces orphan files that have to be cleaned up
later.

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
6. **Fallback for `update_status`**: post a `WORK COMPLETE` checkpoint
   via `add_activity`; the system auto-detects completion.

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

## Communication

- Post progress via `mcp__cubicle-tools__add_activity` (event_type
  `checkpoint`). Include specifics: "Reviewed 3 of 5 acceptance
  criteria. Found 1 critical issue." Not "Working on it."
- If blocked by a REAL issue (missing data, unclear requirements,
  broken dependency, missing credential, external outage), call
  `mcp__cubicle-tools__update_status` with status `blocked` AND a
  `comment` written using the EXACT template below. The Manager
  Assistant reads `details.blocker_class` to route the escalation
  to the right path (answer / helper-task / inbox), so the class
  enum + template are mandatory — free-form prose alone causes a
  bounce-and-retry loop.

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
5. Post your verdict as `add_activity` (event_type `comment`). For
   complex reviews, write a full report file and attach it.
6. **Follow the EXACT review instructions in your task prompt.** The
   prompt will tell you whether to call `move_task` (you're the
   designated reviewer) or `update_task` to unassign (you're a
   non-designated reviewer). Do NOT guess — follow the prompt.

## Scope

- You can only access your current task and the workspace files.
- Never include secret values in deliverables, activity text,
  checkpoints, or README content.
- Read the workstream CLAUDE.md (at `/workspace/workstreams/<slug>/`)
  for project-specific conventions and context.
"""


