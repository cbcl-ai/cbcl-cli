# Changelog

## 0.2.11 — 2026-05-23

Patch release — round-3 review fixes for the AI-skill generation
feature. Three bug fixes + one decomposition cleanup + extended
chown coverage.

### Fixed

- **Empty / punctuation-only skill names landed at `.claude/skills/office/`**
  (orphan file). `paths.slugify("!!!")` returns `"office"` as its
  workspace-naming fallback — wrong for skill names. New
  `_slugify_skill_name` helper has a skill-domain-correct fallback
  (`"new-skill"`) that NEVER lands two empty-slug skills in the same
  dir. The backend defends against the orphan-file scenario
  unconditionally (slug-drift invariant), but this fix prevents it
  at the source.

- **`/workspace/.claude` not chowned to agent**. The chown helper
  covered `/workspace`, `/workspace/.cubicle`, `/home/agent/.claude`,
  `/home/agent/.ssh` — but not `/workspace/.claude`, where AI-skill
  generation lands SKILL.md. Worked for user-run cbcl (file got the
  user's UID, which mapped through the bind mount); broke for `sudo
  cbcl start` (root-owned → agent user can't `mkdir -p`). Now
  covered.

### Changed

- **Extracted `write_skill_to_workspace` helper** from the WS
  dispatcher into `setup_generator.py` (next to the generation
  logic + the shared prompt constants). Slug-of-record + atomic
  write are unit-testable without the WS scaffold. New 10-test
  unit suite. Dispatcher kept to dispatch + serialize.

- **Per-error-class dispatcher messages**. The `generate_skill`
  action handler distinguishes `ValueError` (validate_name
  rejection) and `OSError` (disk full / permission denied) from
  the catchall, so the user-facing toast names the actual cause
  instead of "Skill generation failed."

## 0.2.10 — 2026-05-23

Patch release — round-2 review fixes for the 0.2.9 AI-skill generation
feature. Pure improvements, no breaking changes; previous platform
versions remain compatible via a backward-compatible fallback.

### Changed

- **Daemon writes SKILL.md inline + echoes `written_path`**. The
  `generate_skill` handler now lands the playbook file in the same
  RPC instead of forcing the backend to follow up with a separate
  `fs_write` call. Saves one full WS round-trip per skill generation
  (~30s timeout budget reclaimed). Backend's `fs_write` fallback
  still runs against pre-0.2.10 daemons via missing-`written_path`
  detection, so the upgrade is staged.

- **Shared skill-prompt fragments**. `SINGLE_SKILL_PROMPT` and
  `STANDALONE_SKILL_PROMPT` previously had two near-identical 30-line
  template blocks that had already drifted (one said 250-500 words,
  the other 250-600). Extracted three constants —
  `_SKILL_MD_TEMPLATE_BLOCK`, `_SKILL_BASE_RULES`,
  `_SKILL_JSON_OUTPUT_SHAPE` — both prompts compose them. Drift is
  now mechanically impossible.

## 0.2.9 — 2026-05-23

Patch release — ships the daemon-side handler for the platform's new
"Generate Skill with AI" flow.

### Added

- **`generate_skill` request action.** Backend `POST /api/offices/{oid}/skills/generate`
  round-trips to the daemon with the user's overview text; the
  daemon runs one `claude --print` inside the office container
  with a fresh `STANDALONE_SKILL_PROMPT` that bakes in Cubicle's
  SKILL.md best-practice rules (template, process-first-output-second,
  domain-specific anti-patterns, allowed-tools subset rule,
  parameter naming). Returns the full skill JSON; backend lands the
  SKILL.md on the workspace via `fs_write` and creates the DB row.

  Pre-0.2.9 daemons will respond "Unknown filesystem action:
  generate_skill" if the platform tries to call them — upgrade to
  unblock the Skills page's new AI option.

## 0.2.8 — 2026-05-23

Patch release — `cbcl status` / `cbcl stop` / `cbcl logs` now actually
find a daemon that was started in the foreground (the default mode).

### Fixed

- **`cbcl status` no longer lies "Not running" while the daemon is
  actively serving traffic.** Before this release, only `cbcl start
  --daemon` (the background fork path) wrote
  `~/.cubicle/communicator.pid`. The default `cbcl start` (foreground)
  did not. So every subsequent `cbcl status` from a different shell
  reported "Not running" even though the process was happily
  connected to the platform and handling chat / board WS traffic.
  Operators who started cbcl in tmux / screen and lost the pane had
  no way to stop it via cbcl tooling — only `ps aux` + manual `kill`.

  Two changes fix this:

  1. `_start_foreground` now writes the same PID file the daemon
     path writes, refuses to start if the file names a live process
     (collision check), and cleans up on every exit path.

  2. New `find_running_daemon_pid()` — defence-in-depth /proc scan
     that recognises the cbcl argv signature. Wired into both
     `status` and `stop` as a fallback when the PID file is missing
     — which is the situation EVERY currently-running pre-0.2.8
     daemon is in. So after you upgrade to 0.2.8, `cbcl stop` finds
     the running pre-0.2.8 daemon via /proc and stops it cleanly,
     even though that daemon never wrote a PID file. Linux-only
     (macOS / Windows dev runs unaffected — they wouldn't hit this
     path).

  After upgrade, `cbcl status` shows:

  ```
  Status:   Running (PID 1628742, discovered via /proc)
  Hint:     Started by older cbcl without PID file — next 'cbcl start' will write one
  ```

  …and `cbcl stop` works without a `ps aux` + `kill` dance.

## 0.2.7 — 2026-05-23

Patch release — completes the bind-mount ownership fix from 0.2.5 by
chowning `/workspace` too. Without this, every Manager chat turn dies
with "Failed to write system prompt file to container" because the
agent user can't write to a root-owned bind-mounted workspace.

### Fixed

- **Manager chat now actually works.** 0.2.5 chowned the auth and
  ssh bind-mount dirs but missed `/workspace`. Same root cause —
  the host workspace dir (`~/.cubicle/workspaces/<slug>/`) is
  created by cbcl as root, Docker bind mounts preserve host UIDs,
  the container runs as `USER agent`, and
  `session_bridge.stream_cli_session` writes the per-turn system
  prompt via `docker exec -u agent <ctr> tee /workspace/.cubicle/.prompt-<id>`
  — denied. After upgrading, `cbcl start` chowns `/workspace`
  (top-level only, to avoid silently rewriting user-dropped files
  in the bind-mounted workspace) and the platform-managed
  `/workspace/.cubicle/` subdir.

  Already-running containers self-heal on the next `cbcl start`
  via the early-return path. Or you can heal a live container
  without restarting cbcl:

  ```
  docker exec -u 0 cbcl-office-<slug> bash -c \
    "chown agent:agent /workspace && mkdir -p /workspace/.cubicle && \
     chown agent:agent /workspace/.cubicle"
  ```

## 0.2.6 — 2026-05-23

Patch release — surface the actual cause of the four "silent" periodic-loop errors
the previous release left undebuggable.

### Fixed

- **Empty `httpx.ReadTimeout` no longer masks the four periodic-loop
  errors.** During an office-creation wizard run the backend was getting
  hammered by 11 parallel agent-generation calls + 44 parallel
  skill-generation calls, each holding a worker thread until Claude
  responded. Under that load the daemon's 10s discovery poll, the
  task dispatcher's board fetch, and the cron scheduler's due fetch
  all repeatedly exceeded 10s and raised `httpx.ReadTimeout` — whose
  `str()` is the empty string. The operator saw:

      ERROR: Failed to discover offices:
      WARNING: Failed to fetch board tasks:
      WARNING: Failed to fetch due crons:

  …with literally nothing after the colon, so there was no way to
  tell whether the backend was down, the URL was wrong, or something
  else. A new `describe_exception()` helper now renders the exception
  class name plus the message (falling back to "no detail" when the
  message is empty) and is wired into all four sites:

      ERROR: Failed to discover offices: ReadTimeout: no detail
      ERROR: Failed to discover offices: ConnectError: Cannot connect

### Changed

- **Discovery HTTP timeout raised from 10 s → 30 s** on both the
  async daemon path and the sync CLI path, to give the backend room
  to breathe during a wizard run. Still well under the 60 s
  connector-presence TTL so a runaway hang still self-heals.

## 0.2.5 — 2026-05-23

Patch release — fixes a `cbcl auth` failure on Ubuntu 24.04 (and any
host where the daemon runs as root) that left every office stuck on
"Claude CLI returned empty output" with no path forward.

### Fixed

- **`cbcl auth` now actually writes credentials.** The persistent
  auth volume at `/home/agent/.claude` is bind-mounted from a host
  directory the daemon creates. Docker bind mounts preserve host
  UIDs, so when cbcl runs as root (e.g. on the server) the dir
  lands inside the container as `root:root` — but the container
  runs as `USER agent` (Claude CLI refuses root). Result: the
  OAuth exchange completed, then died writing
  `.credentials.json` with `Permission denied`. No credentials →
  `claude --print` exits 0 with empty stdout → every analyse /
  generate call comes back as "Claude CLI returned empty output"
  even though the haiku probe runs cleanly. After upgrading,
  `cbcl start` chowns the auth + ssh bind-mount dirs to the
  agent user on both the new-container path AND the
  already-running early-return path, so operators' existing
  containers self-heal on the next `cbcl start` — no manual
  `docker exec` needed.

## 0.2.4 — 2026-05-25

Patch release — empty-CLI diagnostic now disambiguates auth vs
model-alias failures, and Opus 4.7 is the platform-wide default
for EVERY agent class (Manager, system agents, custom workers).

### Fixed

- **Empty Claude CLI output diagnostic was misleading.** Previously
  always suggested ``cbcl auth``. Now runs a haiku probe to
  distinguish the two real causes:
  - Auth broken (probe also empty) → suggests ``cbcl auth``.
  - Model alias unrecognised (probe succeeds for haiku, fails for
    the configured model) → suggests rebuilding the agent image
    with ``cbcl setup --force-rebuild-image`` to refresh the
    bundled Claude CLI. The new error names the model alias and
    the exact ``docker exec`` test command.

### Changed

- **Opus 4.7 is now the default for custom worker agents too** (was
  Sonnet 4.6). Aligns with the platform standard already in place
  for Manager + system agents — operators run Opus across every
  agent. Per-agent tier-down still works via the Agents page.
- **``FALLBACK_WORKER_MODEL`` and ``FALLBACK_MANAGER_MODEL`` now
  share a single ``_DEFAULT_CLAUDE_MODEL`` constant** so a tier
  rollout edits one line in ``_model_defaults.py``.
- **Manager spawn fallback** in ``manager_controller.py`` and the
  agent-roster rendering in ``config_sync/sync_service.py`` both
  now reference the central fallback constants instead of hardcoded
  Sonnet strings.

### Added

- **``CBCL_GENERATION_MODEL`` env var** — advanced testing override
  for the setup-wizard's analyze/generate Claude CLI calls. Use
  to validate a new model alias before promoting it to the
  platform default; production operators should leave it unset.

## 0.2.3 — 2026-05-25

Patch release — three-agent code-review cleanup of the 0.2.0 →
0.2.2 phase. One real low-blast-radius bug closed plus quality
polish.

### Fixed

- **Same Ubuntu 24.04 ``"python"`` bug existed in
  ``scripts/deps_installer.py`` + ``scripts/script_runner.py``**
  host-fallback branches. The 0.2.2 fix only touched
  ``agent_supervisor.py``; the script-runner / deps-installer
  host paths (test-only on production but unit-test critical)
  still broke on Ubuntu 24.04+. Applied the same
  ``sys.executable`` swap.

### Changed

- **Single canonical "Claude CLI returned empty" error message.**
  ``_run_claude_cli`` and ``_parse_json_response`` now share one
  ``_empty_cli_output_error()`` helper instead of two divergent
  messages with the same root cause.
- **URL trailing-slash auto-heal.** ``_LEGACY_IP_URLS`` membership
  check now ``rstrip("/")``s the stored URL so a hand-typed
  ``http://46.224.71.1:3000/`` is also auto-healed.
- Trimmed verbose narrative comments in ``agent_supervisor.py``,
  ``config.py``, ``handlers.py``, and ``setup_generator.py``.
- Updated 2 unit-test assertions that hardcoded ``"python"`` to
  compare against ``sys.executable`` (matches the production
  argv now).

## 0.2.2 — 2026-05-25

Patch release — three server-runtime bugs caught from a fresh
Ubuntu-24.04 install. **Recommended upgrade for anyone on Ubuntu
24.04+ or running ``cbcl start`` in a pipx-managed install.**

### Fixed

- **Hardcoded ``"python"`` interpreter** in the agent supervisor.
  Ubuntu 24.04+ ships ``python3`` only — ``python`` is not on
  PATH. Every ``cbcl start`` failed to spawn the Manager subprocess
  with ``[Errno 2] No such file or directory: 'python'``. Now uses
  ``sys.executable`` so the agent process inherits whichever
  interpreter the daemon is running under (pipx venv, system
  python3, etc.).
- **``NameError: name 'delete_queue' is not defined``** on every
  ``office_deleted`` push from the backend.
  ``_register_process_model_handlers`` inner closures referenced
  ``delete_queue`` + ``create_queue`` but the outer function never
  threaded them through. The daemon stayed connected to deleted
  offices in a stale state. Plumbed both queues through as
  keyword arguments.
- **Opaque ``Expecting value: line 1 column 1 (char 0)``** when the
  Claude CLI returned empty output. Most common cause is an
  unauthenticated office container (the CLI's auth prompt wants
  a TTY; with ``--print`` and no terminal it silently exits 0
  producing nothing). The user saw a cryptic JSON decode error
  with zero clue what to fix. Two-stage fix:
  - ``_run_claude_cli`` detects rc=0 + empty stdout and raises a
    clear ``RuntimeError`` pointing at ``cbcl auth``.
  - ``_parse_json_response`` has a defence-in-depth empty-input
    check for the same message if a future caller bypasses
    ``_run_claude_cli``.

## 0.2.1 — 2026-05-25

Patch release — critical fix for the fresh-install default platform
URL.

### Fixed

- **Default platform URL was wrong on a fresh install.** Previously
  set to ``https://cbcl.io`` (TLD typo — should be ``.ai``; also
  the platform lives at the ``app`` subdomain, not the root). Now
  defaults to ``https://app.cbcl.ai``, where the public Cubicle
  platform actually lives. Operators running ``cbcl setup`` on a
  brand-new machine now hit the right endpoint without a manual
  override.
- **Auto-heal for operators stuck on the pre-cutover IP.** If
  ``~/.cubicle/config.yaml`` has a ``platform_url`` of
  ``http://46.224.71.1:3000`` (the pre-domain-cutover IP — port
  3000 is firewalled and TLS isn't terminated there now), it's
  silently treated as absent so the env var / new default takes
  over. Custom dev URLs (anything else) are still preserved.

### Notes for local development

The cbcl daemon still supports local-backend development:

```bash
CBCL_PLATFORM_URL=http://localhost:8000 cbcl setup
```

The env var beats the stored config beats the hardcoded default.
No code change here — just spelling it out in the new ``cbcl
setup`` help text and the ``config.py`` comment block.

## 0.2.0 — 2026-05-25

Mirror of the private Cubicle monorepo at v3.2.0. Focused on the
office-creation pipeline (vision-anchored generation, per-phase
parallelism, agent-gen prompt rewrites) plus a long tail of
correctness fixes flagged by parallel reviewer agents.

### Added

- **Vision-anchored office creation** — every wizard prompt now
  reads from a synthesised Office Vision Brief (a ~200-400 word
  doc tying the four analyzed requirement fields into one coherent
  statement). Downstream phases (instructions, roster, agent
  details, skills) all use it as their anchor so they can't drift
  into incompatible interpretations of the same office.
- **Cohesion + Gap review** — new final phase reads the entire
  generated config and produces a structured assessment
  (`confidence_score`, `coverage`, `identified_gaps`, `redundancies`,
  `suggested_additions`) the wizard's Review step surfaces in a
  "What the AI noticed" panel. Best-effort: ships the office
  without it if the review phase itself fails.
- **Parallel per-phase generation** — analyze field extraction (4
  fields), agent details, and skill playbooks all run concurrently
  via `asyncio.create_task` + `asyncio.as_completed`. Per-task
  failures are isolated; progress events stream as each task
  finishes so the UI's tile-by-tile counter advances live.
- **Shared `_AGENT_OUTPUT_CONTRACT`** — single source of truth for
  the `system_prompt` + `claude_md_content` spec used by BOTH
  agent-gen flows (wizard Phase 3 + Agents page "Create with AI").
  Required H3 outline complements (rather than duplicates) the
  shared baseline rules the writer appends. Stronger spec on
  identity / mission / role-specific principles / anti-patterns;
  generic guidance ("be thorough", "be helpful") is FORBIDDEN.
- **JSON parse resilience** — staged repair in `_parse_json_response`:
  bare → strip code fences → balanced-object slice → trailing-comma
  + missing-comma regex. Closes the recurring "Expecting ',' delimiter"
  failure mode on long Claude outputs.
- **System-agent boundaries on single-agent flow** —
  `AGENT_FROM_DESCRIPTION_PROMPT` now opens with the same
  `OFFICE_BUILD_FRAMING` block the wizard uses. Custom agents that
  duplicate Analyst / Auditor / Manager Assistant / Auto-Script-Dev
  are explicitly rejected.

### Changed

- **Opus-4.7-everywhere** — all four system agents (Analyst,
  Auditor, Manager Assistant, Automation Script Developer) now
  default to `claude-opus-4-7` with thinking mode. The earlier F18
  Opus/Sonnet split was reverted for uniform multi-step planning
  quality. Office-creation Claude CLI calls also switched to
  Opus 4.7.
- **Per-chunk timeout bumped 240s → 360s** for the slower
  thinking-mode Opus calls.
- **`SYSTEM_AGENT_SLUGS` canonical source** — module-scope constant
  sourced from `SYSTEM_AGENT_CLAUDE_MD` rather than a function-scope
  literal. Applied to both wizard + single-agent flows.
- **Tool name accuracy in generated CLAUDE.md** — generated agents
  no longer cite `propose_action(create_task)` (a backend wire-
  format, not a callable MCP tool) or `update_task(reviewer=...)`
  (the worker tool doesn't accept `reviewer`; only the Manager's
  does). Real worker-side tools enumerated explicitly:
  `propose_task`, `propose_subtask`, `propose_update_task`,
  `propose_artifact_handoff`, `escalate_blocker`,
  `request_clarification`.

### Fixed

- **`generate_agent_from_description` system-slug guard** — closed
  a hole where the AI could hallucinate `name: "auditor"` for a
  custom agent, silently creating a duplicate `agent_type='custom'`
  row that collided with the existing system row.
- **Cohesion review graded skills that wouldn't ship** —
  dangling-`skill_names` prune now runs BEFORE the cohesion prompt
  is built. The reviewer sees the actual shipped roster.
- **Outline collisions with shared baseline** — generated
  `claude_md_content` now uses H3 headers so they nest cleanly
  under the writer's `## Office-Specific Notes` H2 wrapper instead
  of rendering as siblings. `## Communication & Handoffs` renamed
  to `### Handoffs` to avoid collision with the baseline's
  `## Communication`.
- **DO-NOT-include list now matches REAL baseline H2 strings** —
  `When You Are a Reviewer` (not `Reviewer Mode`),
  `Completion (when executing, not reviewing)` (not just
  `Completion Checklist`), `STOP — If your task involves writing
  a Python script` added.

### Verification

228 backend tests pass across setup / office / system_agent /
agent surfaces. Smoke tests confirm: no `propose_action`, no
`update_task(reviewer=)`, real MCP tool names cited, H3 outline,
DO-NOT list matches baseline H2 headers verbatim.

## 0.1.0 — 2026-05-14

First public release of the cbcl CLI. Extracted from the private
Cubicle monorepo at v3.1.0; no functional changes from that
checkpoint, but several install / setup ergonomics for a public
audience:

### Added

- **Public install one-liner** — `curl -sSL <repo>/install.sh | bash`
  installs `cubicle-communicator` directly from this Git repository.
  Supports `--venv`, `--ref`, `--uninstall`.
- **Headless `cbcl setup`** — every prompt is now also a flag and an
  env var: `--platform-url` / `CBCL_PLATFORM_URL`, `--company-token` /
  `CBCL_COMPANY_TOKEN`, `--deployment-mode` / `CBCL_DEPLOYMENT_MODE`,
  `--anthropic-api-key` / `CBCL_ANTHROPIC_API_KEY`, `--non-interactive`
  / `CBCL_NON_INTERACTIVE`. Makes Ansible / cloud-init / CI install
  flows trivial.

### Inherited from v3.1.0

- Company-scoped tokens; each office bound to one daemon machine via
  `company_token_id`.
- WebSocket reconnect loop catches every exception class (no more
  silent stuck disconnects after backend restart).
- Manager Assistant escalates via typed Action Request tools
  (`escalate_blocker`, `request_clarification`, …) rather than the
  phantom `propose_action` verb.
