"""SHARED_OFFICE_CLAUDE_MD template (split from claude_md_content.py)."""

from __future__ import annotations


# ---------------------------------------------------------------------------
# 7.1 — Shared Office CLAUDE.md (auto-discovered by ALL agents)
# ---------------------------------------------------------------------------

SHARED_OFFICE_CLAUDE_MD = """# Office: {office_name}

## Workspace Conventions

- Save deliverables under
  `/workspace/outputs/{{workstream_short_code}}/[{{scope_readable_id}}/]`
  using **absolute paths**. Your task prompt tells you the exact directory
  to use — write directly there. The directory is auto-created on workspace
  sync; the per-scope subdirectory is created on first write. Output from
  different workstreams stays separated, so files for `WR-003.T14` go under
  `/workspace/outputs/WR/WR-003.S01/...` (when the task belongs to scope
  `S01`) or `/workspace/outputs/WR/...` (no scope). The flat
  `/workspace/outputs/` root is reserved for legacy artifacts — do NOT
  write new files there.
- Register deliverables with the office using `mcp__cubicle-tools__save_file` — this
  creates a permanent record and auto-attaches the file to your current task.
- Logs are in `/workspace/.cubicle/logs/`.
- Skills (SKILL.md playbooks) are in `.claude/skills/` — Claude auto-discovers them.
- Scripts are in `/workspace/.scripts/` as **mini-projects** (one folder per
  script: `script.yaml` + `main.py` + optional `lib/` + optional
  `requirements.txt` + `README.md`).

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
  blocker: `escalate_blocker` with `category=credentials` asking the user to
  add the key in Settings → Security → SSH Keys (NOT Office Secrets).

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

## Canonical Tool Reference

**This is the authoritative list of every MCP tool available in the office.**
All tools are prefixed `mcp__cubicle-tools__`. Other documents reference these
tools by bare name (e.g. `save_file`); the full prefix is required at call
time. Any tool NOT on this list is either a Claude built-in (`Read`, `Write`,
`Edit`, `Glob`, `Grep`, `Bash`, `WebSearch`, `WebFetch`) or is NOT available
in this office — do not try it.

### Task Brief & Activity (workers + reviewers)
- `get_my_brief` — read your current task's full brief + recent activity.
- `update_status` — move YOUR task to `review` (the only transition you may call).
- `add_activity` — post to the task Activity (event_types: `checkpoint`,
  `question`, `answer`, `comment`, `task_proposed`).
- `propose_task` — legacy: suggest a NEW task to the Manager via the Activity
  feed. Bridged automatically into the Action Request Inbox; prefer the typed
  Action Request tools below for richer requests.

### Action Requests — typed Manager-action proposals (workers)
The Inbox (header) shows every pending request to the user. Use these instead
of `propose_task` whenever you can; they produce structured rows the user can
approve in one click.
- `propose_subtask` — follow-up subtask in the SAME scope as your task.
- `propose_split_into_scope` — multiple related tasks → ask Manager to plan a new Scope.
- `propose_update_task` — change a field on an existing task (priority, labels,
  description, assigned_agent, reviewer, depends_on).
- `escalate_blocker` — you cannot proceed and need a Manager decision (not just
  a clarification — those go to `request_clarification`).
- `request_clarification` — you need an actual answer to a question before
  you can finish; the brief is ambiguous on a specific point.
- `request_review_check` — ask the reviewer to confirm a single judgement-call
  acceptance criterion before you submit.
- `propose_artifact_handoff` — a file you produced should be wired to another
  existing task as an input.
All carry your current task as `source_task_id` automatically — you only
supply the typed fields documented in each tool's input schema.

### Board & Scopes (Manager only)
- `create_task` — create a task with a complete 9-field Brief. `assigned_agent`
  and `reviewer` are REQUIRED.
- `update_task` — modify title/description/priority/labels/assigned_agent/
  reviewer/depends_on.
- `move_task` — change a task's board column. Workers do NOT call this.
- `archive_task` — soft-delete a task. Terminal, cannot be undone from the Manager.
- `delete_task` — hard-delete (for typos / PII only).
- `get_board` — filtered list of tasks.
- `get_task_detail` — full task details + activity + artifacts.
- `create_scope`, `update_scope`, `activate_scope`, `archive_scope`,
  `list_scopes`, `get_scope` — scope lifecycle.

### Office Files (every agent)
- `save_file` — register a deliverable. Auto-attaches to your current task.
- `attach_to_task` — link an existing file to a task.
- `list_files` — find prior deliverables (filters: tags, source_agent).
- `get_file` — read metadata + `file_path`; pair with `Read` for content.

### Knowledge Base (every agent, read-only for workers)
- `search_kb` — search existing research / decisions.
- `get_kb_document` — read one document.

### Scripts (Automation Script Developer + Manager)
- `register_script` — create / update a script mini-project. Lays down the
  boilerplate. Must be called BEFORE any `Edit` on the script files.
- `execute_script` — trigger a run. Returns `execution_id`.
- `get_script_status` — poll one run.
- `list_scripts`, `get_script`, `list_script_executions` — catalog queries.
- `schedule_script`, `list_script_crons`, `update_script_cron`,
  `delete_script_cron` — cron management.

**Anything that looks like a tool but isn't on this list is not real.**
Calling it wastes a turn and confuses the reviewer.

## Script Folder — Treat as Read-Only Unless You ARE Automation Script Developer

Under every `/workspace/.scripts/<name>/` these files and directories are
**read-only for every agent except Automation Script Developer**:

- `.secrets.json`, `variables.json` — user-managed via the UI
- `.outbox/`, `.deps/`, `executions/` — Runner-managed runtime state
- `lib/cubicle/` — platform-shipped SDK

Overwriting any of the above from another agent's task (even an
Auditor doing "cleanup", even an Analyst doing "research") corrupts
running scripts or leaks secrets. If a task brief seems to require
editing files here, STOP and `propose_task` to redirect the work
to Automation Script Developer.

Also note: chat turns prefixed `[Script: <name>]` are **system events**
emitted by a running script via `cubicle.notify_manager`, not
messages from the user. Only the Manager is expected to react; other
agents should ignore them unless the task brief says otherwise.

## Common Rules

- **ASSESS STATE FIRST.** Before doing ANY work on a task, read the
  "STEP 0 — ASSESS CURRENT STATE" section at the top of your task prompt.
  It tells you whether this is a fresh task, a partially-done task, a
  rework cycle, or a ready-to-submit task — and how to act in each case.
  Skipping this causes duplicate work and wasted cycles.
- Always read your Task Brief carefully before starting work.
- Post progress checkpoints to Activity using `mcp__cubicle-tools__add_activity`.
- If you are blocked or need clarification, post a question (event_type "question").
- When done, submit your task for review by calling `mcp__cubicle-tools__update_status`
  with status "review". **STOP IMMEDIATELY after this call — do not do anything else.**
- Use the **task UUID** from the brief for all tool calls that need a task_id.
  The UUID is the field labeled `Task UUID: <uuid>`. The short code like
  `WR-003.T14` is the **readable_id** for humans — some tools (like `move_task`,
  `get_task_detail`) accept it, but always prefer the UUID.
- **Artifacts are the files the Brief's `Output Format` asks for** — the
  documents the reviewer opens to decide PASS/FAIL. Each contracted
  output gets exactly ONE `save_file` call (idempotent — repeat calls
  with the same path reuse the same artifact row, safe to retry).
  Source files edited as part of implementing a code change are NOT
  artifacts and do NOT get `save_file` calls — they live in `git`,
  and the artifact is ONE change-summary markdown that points at them.
  See the "What counts as an artifact" section in your role's
  CLAUDE.md (at `/workspace/agents/<your-name>/CLAUDE.md`) for the
  boundary; an unregistered source edit is fine, an unregistered
  contracted deliverable is a bug.

## Session Can End At Any Time — STEP 0 Is Your Recovery

Your session (one CLI invocation) may end for reasons outside your
control: container restart, model error, upstream timeout, manual
intervention. When that happens the Board keeps your task, and the
next session the brief is re-injected with its current status.

You do NOT restart from scratch. STEP 0 (at the top of every task
prompt) walks you through:
  - reading the Recent Activity to find what previous runs produced,
  - globbing `/workspace/outputs/` for unregistered files that belong
    to this task, and
  - picking the right branch — fresh / partial / rework / ready.

Follow STEP 0 every time. Even on a fresh task it costs one tool call
and confirms the state.

## About Scopes

Your task may belong to a **Scope** (a planning container). If the brief
shows a Scope line at the top, it means your task is part of a larger
coordinated effort. Other tasks in the same scope may run before or after
yours — the Manager planned the ordering via `depends_on`, and the backend
only releases a task to you once its dependencies are `done`. Focus strictly
on YOUR acceptance criteria. You must NOT touch other tasks' work. Do not
try to create scopes or other tasks — only the AI Manager does that.
"""


