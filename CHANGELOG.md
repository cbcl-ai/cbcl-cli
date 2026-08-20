# Changelog

## 0.5.2 — 2026-08-20 — Scripts can read and write Collections

Companion to platform v4.5.0. Upgrade alongside it: the SDK's
`cubicle.collections` needs both halves (the platform ships the Manager's
collection reads and the freshness event handler).

**Collections access for scripts**

- `POST /collections/rpc` on the per-office tool proxy: the script lane
  into the office-local datastore. The ws-protocol `data_*` envelope,
  five whitelisted actions (`data_rows_list/get/upsert/delete/count` —
  no `data_import`), full-body reads with a spoof-proof 4 MB cap, and
  DatastoreError statuses passed through verbatim.
- A SECOND, narrow bearer token (`CUBICLE_COLLECTIONS_TOKEN`) valid ONLY
  on that route — script code can reach rows and nothing else (never
  `/tool-call`, never `/script-execute-host`). Injected on both launch
  paths via the name-only `-e KEY` mechanism and reserved in the
  manifest; the agent's own proxy token still never reaches scripts.
- `cubicle.collections` in the stdlib SDK: `query / get / upsert /
  delete / count`, `CollectionsError(status, message)`, a proxy-free
  urllib opener (a script's declared `HTTP_PROXY` can no longer hijack
  the host-local call or see the bearer), response-phase OSErrors mapped
  into the error contract, and a teaching 401 naming the per-restart
  token re-mint. SDK sentinel v3.
- `script_sync` backfills the SDK into EXISTING scripts on config sync
  (atomic pid-suffixed tmp + chown-before-replace — a crash can never
  strand a truncated file carrying a current sentinel).
- After a successful script write the proxy sends a debounced
  `collection_rows_changed` event so the platform's row-count cache and
  open Data pages stay fresh; older backends log it as unknown and the
  ~30s heartbeat stays the backstop.

**Manager + prompts**

- The Manager catalog gains the collection READS `get_collection` +
  `query_rows` (46 → 48) so a webhook → script → collection → Manager
  pipeline closes without delegation; the Planner exclusion set grows to
  keep its catalog at 29; the playbook allowlist gains a "Collections
  (read-only)" category surviving the General-Chat strip.
- The Automation Script Developer playbook documents
  `cubicle.collections` and the corrected reserved-variable list.


## 0.5.1 — 2026-08-19 — Stop for scripts; prompt + reliability fixes

Companion to platform v4.4.0. Upgrade alongside it: the Stop button needs
both halves.

**Scripts**

- `script_kill` — the daemon half of the UI's Stop button (D-08). It had
  been documented in a deleted spec for years while existing in no
  component, so a hung script could only be ended by the 4-hour duration
  cap, a daemon restart, or manual `docker exec`. `ScriptRunner.kill` had
  always done the hard part — terminate the REAL in-container process via
  its pidfile (killing the host-side docker-exec client alone leaves the
  python running, since no TTY forwards signals) and then run the normal
  completion path, so status, history and the board frame land exactly as
  for a natural exit. This adds the handler that reaches it, and it
  reports BOTH outcomes: an execution this daemon does not track (already
  finished, or started before a restart) logs plainly instead of
  returning silently.

**Agent prompts**

- The three CONSULT-ONLY agents — Planner, Flow Architect, Data Curator —
  are no longer told to post `add_activity` checkpoints or poll
  `get_script_status` during long waits. Neither tool is in any of their
  catalogs (29 / 11 / 9 tools), so complying cost a tool-not-found round
  trip at exactly the moment a session was already slow. The shared
  long-running-Bash rule now renders in two variants from one template.
- The shared task-id note no longer advertises `update_task` / `move_task`
  to plain executors, whose sub-catalogs have not served them since the
  role-filtered split.
- Manager playbook: the scope rule now matches the platform docs (a scope
  is a Tier-3 program MILESTONE, not a container for 4+ related tasks),
  and System Invariant #4 names all three request types that auto-unblock
  a task — it listed two of three, so the Manager expected tasks to stay
  blocked that the platform will move.

**Reliability**

- Three last-resort paths that failed silently now leave a trace: the
  Manager give-up escalation (whose own comment said the error log was
  the only user signal — inside a block that swallowed it), the task
  dispatcher's config retry (a silent failure left it dispatching against
  a stale agent list, so every dispatch failed "unknown agent" with
  nothing pointing at the cause), and the post-move agent kill.

**Removed**

- `src/mcp/` — a dead package with no importers.
- The in-app OAuth leftovers in `cli_auth.py` (~80 lines): a fake-browser
  URL interceptor and container-local callback forwarder with no caller
  since the OAuth machinery was deleted, still described in the module
  docstring as part of the flow.
- `ScriptRunner._untrack_execution` — its logic had been inlined into
  `script_execution.on_complete`; the dead copy claimed to be safe to
  call from paths that no longer called it.

## 0.5.0 — 2026-08-10 — Flow Studio: the flow engine daemon half

Companion to platform v4.3.0. Supersedes the never-published 0.4.0
(pivots 3+4), so this release carries BOTH: upgrade straight from
0.3.1.

**Flow Studio (new)**

- Office datastore (`src/datastore.py`) — a per-office SQLite store at
  `~/.cubicle/data/<office-slug>.sqlite` holding collection ROWS. Row
  data never leaves the operator's machine; the platform holds schemas
  only and reaches rows through the request-scoped `data_*` RPC family
  (list/get/upsert/delete/count/import).
- Flow-block executors (`src/flow_blocks.py`) — the daemon half of the
  flow engine: `ai` (one-shot schema-validated generation session),
  `generate` (doc.yaml + section templates to markdown/HTML, PDF when
  weasyprint imports), `action` (run_script / save_snapshot / notice /
  webhook_out, the last with http(s)-only, a method allowlist and a
  streamed 256 KB response cap), and the `collect` derive pass.
  Executions are deduped on an in-flight marker and cached, so a
  backend re-fire never double-executes.
- Two consult handlers — Flow Architect (flow design and extraction)
  and Data Curator (collections stewardship), both consult-only.
- Live agent channels (`src/agent_channel.py`) — consult sessions
  stream as `agent_channel` events (coalesced chunks, paired tool
  telemetry, a terminal state on every death path).
- The `flow_studio` capability flag on the health report. Backends gate
  run-starts on it, so an older daemon refuses honestly at the door
  instead of wedging a run.

**From the unpublished 0.4.0 (pivots 3+4)**

- Standing operations, the reactive department, hire-on-the-fly, the
  role-shape presets, Manager Assistant on the Sonnet tier, planner
  efficiency and observability work, and the foundry-wedge fixes.

**Upgrade note.** `cbcl stop` SIGKILLs in-flight agent sessions — pick a
quiet moment. The restart both loads this code and rebuilds the office
container's tool layer (the image layer invalidates on tool-file
change), which is how the new MCP catalogs and agent playbooks reach
agent sessions. About 30 s after start the first health report
advertises `flow_studio` and flow runs unlock.

## 0.3.1 — 2026-07-29 — AI-quality remediation: Manager voice, real smoke reviews, verification teeth

Companion to platform v3.22.1. Prompt-layer release — no wire changes;
fully compatible with v3.22.0 backends.

- Manager playbook: "Your voice" rules for non-technical users
  (outcomes not mechanism, expectation durations on every dispatch,
  plain failure ownership), course-correction recipe, composite-request
  routing, program-completion REQ reconciliation, milestone short_key
  rule; Turn-Lifecycle duplication paid down (net smaller playbook,
  ceiling ratcheted DOWN).
- Manager Assistant: the Tier-1b SMOKE REVIEW now exists (Action S);
  Auditor gained a depth dial (smoke vs audit from Verification Steps).
- Builder: deliver-like-a-product (RUN.md, zero-setup tech), one
  project tree per build, verify-with-commands + honest not-verified
  list; anti-script machinery no longer misfires on fat-build Python
  source.
- Planner: single-pass materialize now does compressed research +
  reads prior verification notes and learnings.md (cross-milestone
  learning restored); chips defined as observable evidence (the verify
  gate's teeth); final-milestone deferral ban; spec written for the
  approver.
- Tool descriptions: truth pass (consult_planner restructure,
  decide_action_request rewrite, move_task illegal-target removed,
  create_scope live-scope guard, search_kb published-collections) + a
  new description-claims-vs-backend eval family.
- Worker prompts: reviewer STEP-0 contradiction removed, ask-class
  prompts fully branched, workstream-meta fenced on the worker surface,
  ask answers threaded into the completion poke.

## 0.3.0 — 2026-07-29 — The AI-Office pivots: Builder, adaptive execution, consent-in-chat, Console telemetry

Companion to platform v3.22.0 (both pivots + the comprehensive-review
hardening). Upgrade both together; older daemons degrade gracefully on
every new wire field.

### Pivot 1 — the AI Office execution model
- Builder system agent playbook (ultracode generalist for one-sitting
  builds); Manager playbook tier ladder (Tier 1b fat tasks, 4+ scope
  threshold, ask-class Tier 0), Brief 2.0 four-part contract rendering,
  per-task effort_hint applied at session spawn (Opus-tier, skipped on
  review/blocked dispatches).
- Planner: five consult modes (roadmap absorbed into specify), milestones
  ride the spec, single-pass materialize default, retired-roadmap copy
  fully removed.

### Pivot 2 — consent replaces the work_mode dial
- ask_user_choice Manager tool (option bubbles in chat; asking ends the
  turn via a PRE-LOCK; General-Chat stripped; idempotent re-asks); the
  program boundary is conversational (anti-nag hard rules, teaching
  errors as the cue); env CONTEXT_KEY always wins; option C hand-off
  copy (the backend creates the workstream from the user's click).

### Console telemetry + fixes
- Worker tool telemetry at full Manager parity via the shared builder:
  start/end pairs, duration_ms, output previews (same redaction/caps),
  and dynamic-workflow subagent attribution (details.sidechain +
  parent_tool_use_id) — powers the platform's per-task Console tab.
- Post-terminal cancellation noise fix: a session cancelled AFTER its
  terminal board action (verdict delivered / submitted to review) no
  longer stamps a user-visible error row; clean slot-freeing completion
  instead. Pre-terminal crash recovery byte-identical.
- Manager session policy: xhigh pinned, consent-flow sync pushes,
  hand-off turns dispatched in the new context with the verbatim request.

## 0.2.109 — 2026-07-17 — One-shot turn-end fix for verify workflows, heartbeat lifecycle, recovery UX

Companion to platform v3.21.1. Fixes the incident where large-scope
verifications died verdictless on every cycle.

### The one-shot turn-end trap (root cause, proven)
- Headless (`--print`) sessions exit when the model ends its turn —
  pending dynamic-workflow subagents die with the process. Verify
  prompts + the shared ultracode agent template now state the contract
  explicitly (never yield to wait; await in-turn with timeout-wrapped
  polls), eval-pinned.
- A verdictless verify REFIRE auto-degrades to plain effort so the
  retry always survives; the FIRST attempt keeps configured ultracode.
- `Monitor` is scrubbed from container sessions (cross-turn watcher —
  void and an error trap in headless mode).
- Sessions ending with unresolved subagent spawns are detected and the
  honest poke names it: "ended its turn with a workflow still running".

### Heartbeat lifecycle
- Per-consult heartbeat ownership (no more interleaved 49m/6m elapsed
  counters), cancel-on-completion, single-flight verify per scope, and
  honest cumulative elapsed across retries ("~45m across 3 attempts").

### Recovery
- Works with platform v3.21.1: forensic Inbox escalations (distinguish
  "verdict never recorded — likely size/resource" from refused-PASS),
  sweeper budget reset on deliberate re-verify, and the Manager's
  manual-close path (update_execution_plan now in its catalog) for
  human-verified deadlocks.

Upgrade: `pipx upgrade cbcl && cbcl stop && cbcl start`.


## 0.2.108 — 2026-07-17 — Per-office container resources (UI-managed) + daemon version surfacing

Companion to platform v3.21.0. Remote-server-friendly resource
management: office CPU/memory limits are now set from the web app
(Office Settings → Mounts → Resources), no config-file access needed.

### Per-office container resources
- The daemon consumes `container_cpus` / `container_memory` per office
  from the platform (discovery payload + sync_config). Precedence:
  per-office UI value > `CBCL_OFFICE_CPUS`/`CBCL_OFFICE_MEMORY` env >
  `config.yaml` `office_cpus`/`office_memory` > 4 CPUs / 8g defaults.
- A limits reconciler recreates the office container when the desired
  limits differ from what the running container has — immediately when
  the office is idle, deferred until idle when agents are mid-task
  (always logged). Running work resumes via the normal recovery
  machinery.
- Older backends without the fields behave exactly as before.

### Version surfacing
- `cbcl status` prints the daemon version.
- `health_report` carries `daemon_version`; the app shows it in
  Office Settings → Connection ("Cubicle CLI (cbcl)") and in the
  office status popover.

Upgrade: `pipx upgrade cbcl`, then `cbcl stop && cbcl start`.


## 0.2.107 — 2026-07-17 — Configurable office resources, long-verify notices, verify fan-out sizing

Follows 0.2.106 same-day: the "why is the container slow" round.

### Configurable office-container resources
- `office_cpus` (default 4) and `office_memory` (default "8g") in
  `~/.cubicle/config.yaml`, env overrides `CBCL_OFFICE_CPUS` /
  `CBCL_OFFICE_MEMORY`. The old hard-coded 4-CPU quota capped
  dynamic-workflow subagent concurrency at ~2; raising CPUs raises the
  in-container workflow pool (≈ cores − 2) and speeds tool execution.
- Limits apply on container recreate: `cbcl stop && cbcl start`.
  Remember Docker Desktop's VM allocation is the ceiling.
- `cbcl setup` no longer clobbers hand-added config keys
  (merge-preserving config writes — previously `redis_url` and any
  custom entries were silently dropped on rewrite).

### Long-verify visibility
- A still-running scope verification now posts a chat-visible notice at
  15 and 30 minutes ("still running — large scope or constrained
  resources; it will report when done") — once per threshold, durable,
  never on consults that finish sooner.

### Verify fan-out sizing (eval-pinned)
- Scopes of ≤5 tasks: verify with direct evidence checks, no workflow.
- When a workflow is used: ≤4 concurrent verification subagents
  (CPU-capped containers serialize larger fan-outs).
- Mandatory-verdict rules (0.2.106) unchanged.

Upgrade: `pipx upgrade cbcl`, then `cbcl stop && cbcl start` (recreate
containers so new resource limits apply).


## 0.2.106 — 2026-07-17 — Honest verify completion, infra auto-recovery ladders, durable agent feed

Mirrors the monorepo communicator forward (platform v3.16.0 → v3.20.0).
The theme: outcome-gated completion + bounded, user-visible auto-recovery
from provider failures (429/529/timeouts) across every session type —
plus the design-canon activity/feed work those incidents exposed.

### Honest scope-verify completion (incident 2026-07-16)
- A Planner verify consult's clean exit is no longer trusted: the daemon
  re-fetches the scope and, if it is still `verifying` with a `pending`
  verdict, replaces the false "verification completed" poke with an honest
  "ended WITHOUT recording a verdict" failure poke and re-fires the SAME
  consult once (`_verdictless_refire` loop guard; the backend sweeper
  remains the durable backstop). Fails open on fetch errors.
- Verify-mode prompt hardening (eval-pinned): `complete_scope_verification`
  is the MAIN session's own LAST act — never delegated to a workflow
  subagent; a refused PASS is fixed (chips, coverage_map) and re-called.
- Ultracode (dynamic workflows/subagents) stays ENABLED for verify;
  `CBCL_VERIFY_FORCE_PLAIN_EFFORT=1` is the conservative operator flag.

### Claude-side failure auto-recovery (bounded, visible, escalating)
- Workers: infra-classed retry exhaustion (`api_overloaded`,
  `rate_limited`, `timeout`, `connection_lost`) defers on a 15/30-min
  auto-resume ladder with visible "⏸ Paused — provider outage,
  auto-resuming at HH:MM" activities BEFORE escalating to blocked.
- Manager turns: upfront retryable errors retry in place (≤3) behind an
  honest "API busy — retrying…" status pill; ONE mid-stream continuation
  retry resumes the interrupted transcript without repeating tool calls;
  surfaced errors carry classified actionable copy; usage-limit turns
  name the parsed reset time and wake the pill when the window reopens.
- Planner consults: infra-classed deaths re-fire once (generalized from
  the verdictless posture); consult pokes that fail to deliver are queued
  and redelivered; success pokes are outcome-gated per consult mode
  (specify/roadmap/scope_plan/materialize verify their write landed).

### Durable agent activity feed
- Last-N feed persistence independent of the 300 s sliding TTL + a
  "dynamic workflow running" heartbeat keepalive row — silent ultracode
  phases render as visible work instead of an empty feed.
- Fixed a cross-scope NameError that silently killed BOTH the feed
  keepalive AND the stall-watchdog loop on first pulse (runtime
  regression test added).

### Manager activity parity + spec approval
- Manager tool usage is enriched like worker feeds (tool_start/tool_end
  with redacted command summaries, output previews, durations) so the
  Manager's Activity tab shows real work.
- `spec_approval` rides sync_config + all 9 daemon-originated poke
  contexts; an absent value renders a neutral fail-safe line instead of
  asserting the user-mode prohibition (fixes "Manager refuses to
  auto-approve after a mid-chat settings flip").

### Scripts
- `script_execute` honors a wire-supplied `triggered_by` (sanitized,
  100-char cap) so webhook-triggered runs (platform v3.17.0) attribute
  as `webhook:{name}` in execution history; absent → "user".

Upgrade: `pipx upgrade cbcl` (or the install one-liner), then
`cbcl restart`. No config changes required; all behaviors degrade
gracefully against older backends.


## 0.2.105 — 2026-07-06 — Direct connector OAuth + P1–P9 prompt/setup adversarial-review remediation

Mirrors the monorepo communicator forward (platform v3.15.0). A large release:
a new direct-connector-OAuth path, the AI generate/improve generation-flow fix
from v3.14.2, the structured-output readability overhaul, and the full P1–P9
adversarial-review remediation of the prompt / setup / session / escalation /
injection surfaces (each phase followed by a verification round that fixed the
bugs the fixes introduced).

### New — direct connector OAuth from cbcl
- Add / login / remove an MCP connector directly from the daemon
  (`src/_handlers/_mcp_login.py`, `_mcp.py`), including `claude mcp login` for
  OAuth connectors and removal of CLI-named connectors that contain spaces
  (e.g. `claude.ai Google Drive`).

### Generation flow (v3.14.2 carry-forward)
- AI generate/improve office + agent surfaces: speed + timeout alignment so the
  "Improve with AI" path no longer 504s (daemon ceiling kept under the backend
  budget), plus the 26-finding generation-flow review remediation (GEN prompts,
  AIGEN runtime, setup-wizard, agent field-gen).

### Chat / output readability
- Structured, scannable AI output overhaul + its adversarial-review remediation;
  typed `manager_session_rotated` frame + fresh-session chip; board summary
  rendered as a markdown line; output-style dedup.

### P1 — eval suite is now a real CI gate (EVAL-01..08)
- Prompt-content invariants (tool lists, routing rules, transitions, counts)
  are pinned by evals that fail CI on drift; new eval families for generation
  prompts, live-prompt-is-production, output style, and token budget.

### P2 — truthfulness / generation drift
- Generated content is no longer delivered under the never-follow fence
  (GEN-01/03); spec-approval guard hole + roster/scope contract gaps closed;
  batch tool/prompt drift cleanups.

### P3 — worker context delivery
- Workstream context now reaches workers, with freshness/heal on stale context.

### P4 — session resilience (SES-01..10)
- Rotation over-fire + retry double-execute fixed; resilience during API
  outages + extended-thinking handling; streaming perf + reduced-motion.

### P5 — escalation ladder (BE-01/02/03/07, WRK-01/02, TOOL-04)
- Blocker class routed correctly; rework-cap honored; AR ager re-poke;
  bounce-cap reset; dead validation removed.

### P6 — prompt-injection defense (INJ-01/02/03)
- Untrusted-content directive; the auto-decide turn and wizard generation inputs
  are fenced; script log output is tagged.

### P7–P9 — setup tool-surface economy, context scoping, agent best-practices
- Tool-surface economy (TOOL-05/08/09/11); role-scoped office files + per-role
  budgets (CTX-02/06/07/11); Planner/MA consult-scoped rules; durable
  per-workstream learnings loop + explicit reasoning-effort pins
  (BEST-01/03/05, SES-05, graceful-degrade on older CLIs); Improve pass gets the
  skill catalog; budget-permitted retry for cheap parse failures; salient-section
  excerpts; setup-truthfulness fixes (GEN-02/07/09/11).

Requires the matching backend release (monorepo v3.15.0).

## 0.2.104 — 2026-06-23 — Planner stall-watchdog overhaul + spec/chat audit remediation

Mirrors the monorepo communicator forward. Carries the Toddo/Presale incident
fixes + the comprehensive chat-duplication/ordering/WS-singleton audit
remediation.

- **Planner stall watchdog**: ultracode-aware stall ceiling
  (`CUBICLE_PLANNER_STALL_SECONDS_ULTRACODE`, default 2400s) so a healthy long
  ultracode consult (silent background sub-agent orchestration) is no longer
  false-positive-killed at the 600s wall-clock mark; a single authoritative
  poke per stall (watchdog-killed worker `task_complete`/synthesized-fatal
  suppressed via `_watchdog_killed`); a post-cap cooldown
  (`CUBICLE_PLANNER_CAP_COOLDOWN_SECONDS`, default 1800s) that breaks the
  respawn-after-cap loop; corrected the false "kill_initiated suppresses the
  poke" comment; auto-restart race guard (re-check after the publish-await);
  cooldown leak prune.
- **Spec manager-approve**: a pending DRAFT spec + the workstream's
  `spec_approval` mode now surface in the Manager's standing context (a draft no
  longer hides until approved), with proactive review→approve_spec guidance in
  manager-approval mode and a "user approves" note otherwise; a dropped
  `specify` consult now steers the Manager to review+approve an existing draft.

Requires the matching backend release (monorepo v3.12.0).

## 0.2.103 — 2026-06-22 — Rework-feedback fix (designated reviewer)

Mirrors the monorepo communicator forward. Carries the rework-path fix from
the capstone E2E review.

- The rework dispatch now forwards `blocked_bounce_count` + `recent_activities`
  to the worker (parity with the initial dispatch), so the worker prompt's
  "previously BLOCKED — retry the failing operation first" note and the Recent
  Activity context fire on the rework path too. Pairs with the backend fix
  where `send_task_rework` stopped filtering review feedback by
  `actor=='manager'` — reviews are automated by the DESIGNATED REVIEWER
  (Auditor / Manager Assistant / a custom reviewer), so the executor now
  actually receives the reviewer's feedback on a review→in_progress return.

## 0.2.102 — 2026-06-22 — Planner hang watchdog + usage-limit auto-resume

Mirrors the monorepo communicator forward. Completes the agent-resilience
work: a hung agent no longer stalls indefinitely, and a usage window
exhaustion auto-resumes when it reopens.

### Planner stall watchdog (auto-restart)
- The Claude CLI has no built-in hang timeout, so a Planner consult that
  produces nothing could wedge indefinitely (the reported 30-min stall:
  scope skeleton null, 0 tasks, no poke). The per-consult heartbeat is now
  a STALL WATCHDOG: at `CUBICLE_PLANNER_STALL_SECONDS` (default 600s) of no
  completion it kills the hung session and re-fires the SAME consult
  (skeleton/materialize/roadmap/scope_plan authoring is overwrite-safe — it
  converges, doesn't duplicate). Capped at `CUBICLE_PLANNER_MAX_RESTARTS`
  (default 2), after which the Manager is poked to re-consult/escalate.
  Re-confirms the agent is still busy right before intervening so a consult
  that finished at the boundary isn't falsely restarted. Verify mode keeps
  its backend stuck-verifying sweeper (no double-recovery).

### Usage-limit auto-resume (worker tasks)
- When a worker task hits the usage cap with a parsed reset time, the retry
  loop now WAITS until the window reopens then resumes the same session
  (posting a visible "⏸ Paused — resuming at <HH:MM UTC>" activity) rather
  than burning a fixed backoff. A reset beyond the task's remaining
  wall-clock budget escalates immediately with the reset time named. The
  interactive Manager surfaces usage limits to the user instead.

## 0.2.101 — 2026-06-19 — Agent resilience: rate-limit / 529 / usage-limit handling

Mirrors the monorepo communicator forward. Hardens every agent (including
the AI Manager) against transient API errors so a busy moment no longer
kills in-flight work.

### Rate-limit + overload retry (incl. the Manager)
- The **Manager chat session had NO retry loop** — a 429/529/transient
  drop mid-turn surfaced the raw error and ended the turn. This was the
  reported bug: a Planner finished a scope skeleton, the Manager's pick-up
  turn hit "API Error: 529 Overloaded … exited with code 1", and died
  instead of reviewing. The Manager now wraps its CLI stream in a
  per-attempt retry loop: a retryable upfront API error (rate limit ~60s,
  overload ~180s, transient drop) BEFORE any visible output waits the
  backoff and re-runs the same turn (resuming the session); gated on
  no-visible-output so it can't duplicate text; capped at 3 attempts.
- Rate-limit backoff bumped 15s → **60s** for every agent (a per-minute
  429 bucket clears within a minute; the CLI/stream-json doesn't expose
  `retry-after`, so a fixed minute is the safe resume interval).

### Usage / session limit classification
- New `USAGE_LIMIT_EXCEEDED` error class (the rolling 5-hour / weekly
  subscription cap), matched ABOVE `RATE_LIMITED` so "usage limit reached
  … resets at <time>" is no longer mistaken for a transient 429. The
  remedy carries a parsed `reset_at` (epoch / ISO / "resets in N hours" /
  "reset at 11pm") so a future deferred-resume can wake the work exactly
  when the window reopens. (The resume scheduler lands in a follow-up; the
  classification + reset parsing ship here.)

### Tests
- +7 Manager-retry tests (429/529/post-output-no-retry/usage-limit-no-
  retry/cap/fatal/clean) + usage-limit classification + ordering
  regressions. 167 resilience tests green.

## 0.2.100 — 2026-06-19 — Scope-verification coverage gate + execution planning always-on

Mirrors the monorepo communicator forward. Closes the gap where a scope
reaching "all tasks done" was treated as "the scope's goal is delivered" —
verification is now a real coverage gate, and execution planning (the
verification flow itself) is on for every office.

### Planner verify mode is now a coverage gate
- `complete_scope_verification` gains a **`coverage_map`** parameter
  (REQ-id → `delivered`|`deferred: <reason>`). The backend REFUSES a PASS
  while any execution-plan chip is undone, or any approved-spec requirement
  the scope covers is missing/empty in the map. The verify-mode prompt is
  rewritten to a concrete sequence: read the plan + tasks → mark every chip
  done (`update_execution_plan`) → `get_spec` and account for every covered
  REQ → submit `coverage_map`. The Planner playbook's verify section matches.
- The roadmap now carries a **structured `covers: ["REQ-…"]`** field per
  planned scope (the coverage map over the spec the gate checks), instead of
  a free-text `covers:` tag in notes. The `roadmap`-mode prompt + playbook +
  `update_workstream_plan` description are aligned.
- The backend enriches the auto-fired verify consult with the approved-spec
  REQ list + the scope's `covers`, surfaced in the Planner's session prompt
  (`approved_spec_reqs` / `scope_covers`) so the coverage contract is in
  front of it at session start, not behind optional tool calls.

### Notes
- Execution planning is always-on now (the per-office opt-out was removed),
  so the verifying gate + the "Planner engaged / verdict" workstream-chat
  bubbles fire for every office. Backend-only; no CLI flag.
- Tests/evals updated in lock-step (`test_spec_driven_planning`,
  `test_planner_consult`, `test_tool_catalog_drift`).

## 0.2.99 — 2026-06-18 — Proactive Manager spec/roadmap review (`approve_spec`)

Mirrors the monorepo communicator forward. Closes the gap where a
`spec_approval='manager'` workstream drafted a spec and then stalled —
the AI Manager wasn't actually involved in the review, and the roadmap
could start while the spec still showed "blocked until approved".

### Manager now reviews + approves the spec/roadmap proactively
- New **`approve_spec`** Manager tool (in `MANAGER_PLAN_TOOLS`). The
  Manager reads the draft spec (`get_spec`), checks it against the
  user's requirements, consults the Planner (`specify`) to revise any
  gaps, and then approves it — a real review step, not a silent
  auto-approve. Gated to manager/MA actors AND refused in
  `spec_approval='user'` workstreams (the user's gate is not bypassed).
- `approve_spec` is excluded from the Planner toolset
  (`_PLANNER_EXCLUDED_MANAGER_TOOLS`) — the Planner drafts, the Manager
  approves.
- `_manager_action_requests.py`: the `specify` consult-completion now
  drives the proactive review loop (read → check requirements → revise
  via `specify` → `approve_spec` → roadmap), and the `roadmap`
  completion reviews REQ-coverage/gaps before opening scopes. `specify`
  added to the failure-poke set so a dropped consult never hangs the
  Manager.
- Manager playbook (`_manager.py`) + tool-allowlist (`_tool_allowlist.py`)
  updated to document the review responsibility and surface the tool.
- Roadmap consult is refused while the spec is an unapproved draft in
  BOTH approval modes (mode-aware error), killing the inconsistent
  "roadmap running while spec unapproved" state.

### Notes
- The companion backend change (surface the reviewer's return note as the
  prominent `## REWORK REQUIRED` block on the `review → ready` rework
  path) lives in the monorepo backend; no CLI change needed for it.
- Tests: `test_planner_consult.py` + `test_tool_catalog_drift.py` updated
  and green.

## 0.2.98 — 2026-06-18 — Ultracode (dynamic workflows) + per-field agent AI generation

Mirrors the monorepo communicator forward. This release also catches the
public CLI up to the previously-unmirrored 0.2.97 monorepo state (the
in-app OAuth machinery removal — see "Catch-up" below).

### Orchestration: ultracode (dynamic workflows) replaces the workflows toggle
- Dynamic workflows are governed by Claude Code's `ultracode` setting (xhigh +
  autonomous parallel sub-agent orchestration), NOT a separate flag. The agent
  `effort` field is now the single orchestration knob
  (`low|medium|high|xhigh|max|ultracode`); `ultracode` is emitted headless as
  `--settings '{"ultracode": true}'` **plus** `--effort xhigh` (so an older CLI
  that ignores the unknown ultracode key still lands xhigh, never default
  effort). The Planner ships `effort: "ultracode"`.
- Plain effort levels still map to `--effort <level>`; non-ultracode workers
  work alone (the `Agent`/`Task` sub-agent tools are disallowed). The static
  `--agents` "Helpers" mechanism + the CLAUDE.md "Your Subagents" section were
  removed in favour of model-driven dynamic workflows.
- The Manager is hard-blocked from orchestrating in every case:
  `Bash`/`Task`/`Agent` disallowed **plus** `CLAUDE_CODE_DISABLE_WORKFLOWS=1`.
- Graceful-degrade: a container CLI that rejects `--effort`/`--settings`
  strips them and retries; no task is ever blocked by a flag-support gap.
  Requires a container Claude CLI ≥ v2.1.154 for dynamic workflows to activate.
- AI generators (office instructions, workstream context, agent fields, office
  setup) run at xhigh reasoning effort.

### Per-field agent AI generation
- New `generate_agent_field` RPC: generate or improve a single agent field (the
  system prompt or the agent's CLAUDE.md instructions) from a user directive +
  the live agent context (role, tools, skills). Powers the agent dialog's
  "Update with AI" buttons. Input is fenced + length-capped.

### Catch-up to monorepo 0.2.97 (previously unmirrored)
- Removed the in-app OAuth machinery (`mcp_auth.py`, `_handlers/_oauth.py`);
  MCP connectors are now instruction-based (`_handlers/_mcp.py`,
  `_mcp_listing.py`, `handlers.py` updated). This shipped on the monorepo at
  0.2.97 but was not mirrored to the public CLI until now.

## 0.2.97 — 2026-06-16 — Phase 8 resilience + Phase 10 spec-driven planning + reviewer hardening

Large roll-up of the communicator work accumulated since 0.2.96. The
monorepo carries the authoritative commit history; this mirrors it forward.

### Spec-driven planning (Phase 10) — flag-gated, inert unless enabled
- New `specify` Planner consult mode, spec materialization, and a
  per-workstream `spec.md` convention; spec-change discipline + traceability;
  spec-template playbook (`_spec_template.py`).

### Reliability & resilience (Phase 8)
- Supervised long-lived loops (dispatcher / script-monitor / watchdog) so a
  crash always restarts; per-office bring-up parallelized.
- RPC futures fail-fast with `ConnectionError` on disconnect; request frames
  never replay stale; a failed replay head is re-queued.
- Oversized NDJSON line skips instead of killing the reader; the CLI proc is
  terminated on `GeneratorExit`; bounded stderr + token redaction in
  connect-error logs.
- Cron: no double-fire on a failed `/fired` notify; `/fired` checks HTTP status.
- Health surfaces crashed agents as `error` (not idle); `script_status` maps
  `timed_out → failed`.
- Startup reaps orphan agent CLI sessions; Manager give-up self-heal +
  escalation action-request; completion fence (`COMPLETED.json` marker).

### Reviewer / KB hardening
- A PARTIAL `get_task_detail` artifact fetch now aborts + re-queues a REVIEW
  dispatch instead of letting the reviewer render a blind verdict on an
  incomplete deliverable list (ADD-D1). Covers the empty-reviewer
  Manager-Assistant fallback. (The KB itself stays text/markdown-only; the
  dead agent KB-write path was removed platform-side.)

### Execution policies + AI prompt system (Phase 4/5)
- Execution-policy + prompt-system re-review hardening; expanded eval coverage
  (tool-catalog drift, transitions-legal, numeric-invariant pins, rework-cap).

## 0.2.96 — 2026-06-08 — worker-session-churn: Bash-monitor playbook + PreToolUse guard

Tiers 1 & 3 of the worker-session-churn fix (the backend liveness-aware
sweeper is the platform-side half). Addresses worker sessions hanging on
unbounded in-Bash poll loops / monitors and the false-positive "wedged task"
escalations they triggered.

- **Worker playbook (Tier 1).** The shared agent playbook
  (`_shared_agent.py`, delivered to every agent incl. custom ones) now
  forbids unbounded in-Bash monitors (`tail -f`, `while true`,
  `docker logs -f`, `watch`, uncapped health polls) and mandates bounded
  waits + the Script system for genuinely long monitoring.
- **PreToolUse Bash guard (Tier 3).** A hard runtime backstop:
  `_agent_image/bash_guard.py` (baked into the agent image at
  `/opt/cubicle/bash_guard.py`) is wired via a per-agent
  `.claude/settings.json` PreToolUse hook (matcher `Bash`). It denies
  open-ended monitors with a remedy message and allows everything else;
  strips quoted spans (so `grep 'while true'` is fine), exempts
  `timeout N …` and `while read`, and fails OPEN on any parse error.
  Image-cache lockstep maintained (`Dockerfile.agent` COPY +
  `container_manager._mcp_server_source_files`).

## 0.2.95 — 2026-06-04 — Manager-Assistant orphan-triage playbook (no in_progress→ready)

- **MA Board-Operator playbook fix.** The Manager Assistant's orphan-triage
  steps told it to move an `in_progress` orphan back to `ready` before
  re-assigning. That `in_progress → ready` move is now forbidden backend-side
  (it stranded a live worker — see the platform fix for PE-001.T139), so the
  playbook now says: for an `in_progress` orphan, just (re)assign the agent —
  the dispatcher re-spawns the worker IN PLACE (the task stays `in_progress`).
  Pairs with the backend change that removed the `in_progress → ready`
  transition entirely.

## 0.2.94 — 2026-06-04 — Dash-leading chat prompts no longer crash the turn

- **Fix: a chat message starting with `-` killed the whole Manager turn.**
  When a user pasted a message whose first line was a markdown bullet
  (e.g. `- SMTP_CREDENTIALS is in the Office secrets …`), the daemon passed
  the prompt to `claude --print` as `["-p", prompt]`, so the CLI's commander
  parser saw the leading `-` and aborted with
  `error: unknown option '- …'` (exit 1) — the user saw the raw CLI error
  instead of a Manager reply. `session_bridge` now passes the prompt as a
  POSITIONAL argument after a `--` end-of-options separator (and drops the
  redundant `-p`; print mode stays on via the existing `--print`). Anything
  the user types — leading dashes, bullets, flag-like text — is now taken
  verbatim as the prompt. Regression test:
  `tests/test_session_bridge_prompt.py`.

## 0.2.93 — 2026-06-04 — Planner consult heartbeat (visibility)

- **Planner consult "working" pulse.** A consult is async and can run for
  minutes (a `materialize` of a 10-task scope took ~6 min), during which the
  Manager is idle and the chat is silent — only an "engaged" bubble, then
  nothing until the result poke. Users read that as "the Planner stopped" and
  nudge the Manager. The daemon now pulses a `🗺️ Planner <verb> — Nm elapsed…`
  manager_state to the workstream every 75s while a consult runs, so the UI
  shows it's alive. Stops as soon as the Planner is no longer busy. (The
  companion fix — the right-rail Manager activity timeline refreshing live —
  is backend/frontend-side.)

## 0.2.92 — 2026-06-04 — Tool-proxy bind knob (tool-calling audit remediation)

- **`CUBICLE_TOOL_PROXY_BIND`** — the host tool-proxy (which hosts the
  secret-injecting `/script-execute-host`) now reads its bind address from
  this env (default `0.0.0.0`, unchanged). Operators on a SHARED host can set
  it to the docker bridge IP (`172.17.0.1`) so only containers on that bridge
  — not every local process — can reach the proxy. Part of the
  `docs/review_for_improvements/07-tool-calling.md` remediation (TOOL-05); the
  rest of that audit (role gates, fail-closed `/tool-call` auth) is backend-side.

## 0.2.91 — 2026-06-04 — Orchestration robustness + CLI-style live agent activity

Daemon-side fixes for issues hit on a live multi-scope workstream:

- **Planner/Manager `complete_scope_verification` no longer rejected as
  `actor='(none)'`.** The proxy→WS tool-call path dropped the daemon-attested
  `_caller`; `tool_proxy_server` now folds `_caller` into `params` before
  forwarding, so plan/verify role gates (which carry no `actor` field of
  their own) resolve the caller on every path. Incidentally closes a
  worker-gate bypass on the WS path.
- **Transient socket/CLI drops no longer escalate a task to `blocked`.**
  New retryable `CONNECTION_LOST` error class (resumes the session — work
  isn't lost) matches connection-reset/closed/broken-pipe/ECONNRESET/etc.
  A bare `exited with code 1` with no connection marker stays fatal.
- **CLI-style live agent activity.** Tool calls now emit a "running" start
  row (command shown live, even for a multi-minute Bash) + an enriched
  "end" row (command + redacted, truncated output preview), correlated by
  `tool_use_id`. Secret-shaped substrings are scrubbed host-side and the
  output of a Read of a `.secrets.json` is never captured.

## 0.2.90 — 2026-06-04 — Review-pass hardening of the context-management fixes

Self-review of 0.2.87–0.2.89 found and fixed:

- **Faithful `get_task_detail`.** The lean projection no longer drops
  structural top-level fields (office_id/workstream_id/session_id/…) from
  a single-task read — it trims ONLY the activity feed (the real bloat).
  Avoids breaking any follow-up action that referenced those fields.
- **`workstream_short_code` kept** in the lean `get_board` whitelist, so
  the Manager can still map tasks → workstream in a multi-workstream
  (General Chat) board read.
- **Accurate reset notice.** The session-reset message now matches the
  cause ("no longer available" for an expired/wiped session vs "grew too
  large" for an oversized one).

(Paired with a backend fix bounding the inline fs_write retry by a total
wall-clock budget so create_script can't exceed the proxy timeout on a
wedged daemon — backend-side, not in this CLI package.)

## 0.2.89 — 2026-06-03 — Lean tool reads + native Compact instructions (replaces 0.2.88's /compact trigger)

Root-cause fix for Manager context bloat, the "send less, steer native"
approach (no added layer, no extra prompt into the session):

- **Lean board/task reads.** `get_board` returned the full record for
  every task — `description`, UUIDs, timestamps, display metadata — and
  that landed in the Manager's resumed transcript every turn (~3 MB of
  board dumps on the wedged session). The MCP server now trims
  `get_board` to an orchestration whitelist and `get_task_detail` (last
  10 activities, capped content, slimmed details) BEFORE the result
  enters context. REST API + UI unaffected.
- **`# Compact instructions` in the Manager CLAUDE.md.** Native Claude
  Code reads this to steer compaction — keep the live board state / open
  request / session decisions, drop stale board dumps and superseded
  steps.
- **Dropped the 0.2.88 proactive `/compact` trigger.** Native auto-compact
  already runs in our flow; feeding it less + steering it makes the extra
  headless call unnecessary. Kept the per-turn input-token log for
  observability.

## 0.2.88 — 2026-06-03 — Proactively reuse the CLI's native /compact

Builds on 0.2.87. Verified on prod (CLI 2.1.161) that Claude Code's
**native** auto-compact already runs in our headless `claude --print
--resume` flow, and that `/compact` works as a headless `--print` prompt
(recognized, executed, persisted to the same session). So we reuse the
CLI's OWN compaction — no custom summarizer.

Native auto-compact alone fires at ~95% of the window, so a long
workstream chat rides the edge until one big turn tips it irrecoverably
into "prompt is too long". Now: after a Manager turn whose effective
input context (input + cache-creation + cache-read tokens, from the CLI's
own usage report) crosses `CUBICLE_MANAGER_COMPACT_THRESHOLD_TOKENS`
(default 150 000 ≈ 75% of 200K), we fire one headless `/compact` so the
next turn resumes from the CLI's summary instead of the full transcript.
This prevents the wedge proactively **and** cuts per-turn token spend on
long chats. Best-effort — a /compact failure never fails the turn, and
the 0.2.87 reset still catches any later overflow.

## 0.2.87 — 2026-06-03 — Auto-heal a wedged Manager session

A long-lived workstream chat grows a large resumed Claude session
transcript (400–625 KB observed on prod). Once `claude --print --resume`
can't load it, the CLI exits non-zero and the chat wedged into a
permanent "Claude CLI exited with code 1" loop on every action until
`cbcl stop/start`. Three coordinated fixes:

- The manager worker now folds the CLI **stderr** ("prompt is too long:
  N tokens > M maximum") into the raised error so the classifier sees the
  real cause instead of only the synthetic exit-code string.
- `classify_error`'s CONTEXT_TOO_LARGE pattern now matches the CLI's
  actual phrasing (`prompt IS too long`) plus the `N tokens > M maximum`
  numeric tail — previously it slipped through as UNKNOWN_FATAL.
- New per-context consecutive-failure backstop
  (`CUBICLE_MANAGER_CONTEXT_RESET_AFTER_ERRORS`, default 2): after N
  failed turns in a row on one context, the stored session is dropped so
  the next turn starts fresh. A clean turn resets the streak. Board and
  workstream state are unaffected — only in-CLI conversation continuity
  resets.

## 0.2.86 — 2026-06-03 — Supervise the connector WS loop (reconnect-hardening)

Closes the root cause of the earlier outage. The connector connect() loop
auto-reconnects on every Exception, but it ran as a bare fire-and-forget task:
if it died on an uncaught BaseException (a stray CancelledError from a partial
teardown, or a library bug past the catch-all), nothing restarted it — the
office connector went silently dead with ZERO reconnect attempts and every
backend->daemon push 503'd until a full cbcl restart.

- New _supervise_connector wraps router.start(): a crash (non-graceful exit
  while should_run is True) re-launches the loop with a 5s backoff; a graceful
  stop or a CancelledError (shutdown) ends supervision without a restart.
- Added should_run to PlatformWSClient + WsTransport to distinguish a graceful
  stop from a crash.


## 0.2.85 — 2026-06-03 — Agent playbooks consistent with shipped mechanics

Consistency pass over every agent CLAUDE.md/playbook after the bash-default,
office-secrets-in-shell, direct-git, and no-unassign-after-Ready changes.

- Removed the dead "non-designated reviewer -> unassign yourself" model from the
  Auditor playbook; reviewers now always resolve via move_task and never touch
  assigned_agent (a FAIL returns to the same executor).
- Manager Assistant: Action A designates a reviewer via the reviewer field (not
  assigned_agent); Action B no longer reassigns the executor; blocked path uses
  update_status(blocked)+blocker_class; lists Bash + readable_id.
- New office-level sections (inherited by all agents): "Office Secrets in Your
  Shell" (secrets are env vars; use $GITLAB_PAT / API keys directly) and "Git is
  Direct, Not a Script". Shared script-redirect carve-out for one-off
  credentialed/git ops.
- Manager playbook: System Invariants for workers-have-bash/git/secrets/SSH and
  no-unassign-after-Ready; scope state machine shows the verifying state.
- Analyst/Planner: Bash awareness (no longer read-only); Analyst tries gh/git
  before flagging a connector gap; Planner rework tasks assign to the same
  executor. Automation Script Developer right-sizing covers git/credentialed
  one-shots.


## 0.2.84 — 2026-06-03 — Direct bash+git+secrets workflow (retire the script chokepoint)

Companion to the backend bash-by-default change. Lets agents work directly —
build, git push over SSH, use API keys — without routing git/credentials through
registered scripts.

- **Office secrets in the agent shell**: worker sessions now inject the office's
  host-only secrets store into the CLI session as env vars, so agents use
  GITLAB_PAT / API keys directly. Injected via NAME-ONLY `docker exec -e KEY`
  with the value riding the subprocess env — never the host argv (ps /
  docker-inspect safe), the same posture as the Script Runner. `session_bridge`
  gains a `secret_env` param; `_agent_worker_task` reads the office secrets and
  passes them per worker session. Best-effort: a missing/corrupt store never
  blocks the task.
- **SSH key ownership fix**: keys added via Settings → Security → SSH Keys landed
  `root:root 0600` on the bind-mounted host file, so the in-container agent user
  (uid 1000) couldn't read them — `git clone git@gitlab.com:...` failed with a
  permission error. `write_key` now chowns the host key file to the agent uid,
  and `_ensure_ssh_dir` chowns the whole `ssh-keys` tree on container start
  (heals previously-stranded keys on the next office restart).


## 0.2.83 — 2026-06-03 — Review never routes to the executor (no self-review)

Follow-up to the no-unassign-after-Ready work. Now that the executor stays
assigned through review, a review task with no designated reviewer but an
assigned executor would have routed to the EXECUTOR (self-review) — the old
flow relied on the executor having unassigned itself first.

- `agent_queue` full_sync + reconcile: a `review` task now routes to its
  `reviewer`, else the Manager Assistant — NEVER the assigned executor. Every
  task auto-gets `reviewer=manager-assistant` when none is set, so this is the
  legacy/edge guard.


## 0.2.82 — 2026-06-03 — No-unassign-after-Ready: reviewers never unassign; single reviewer playbook

Companion to the backend no-unassign-after-Ready invariant. Fixes the reviewer
routing-loop + stranded-Ready bug: a returned task (review → ready) used to land
unassigned and never get picked up, and a designated reviewer could be
re-dispatched the same already-PASSed task repeatedly.

- **Single reviewer playbook**: any non-MA agent reviewing a task gets the
  DESIGNATED REVIEWER block and resolves the task DIRECTLY with move_task. The
  old "non-designated reviewer: post verdict + unassign so the Board Operator
  closes the loop" block is removed — the executor stays statically assigned
  (reviews are routed by the `reviewer` field, not by unassigning), so a FAIL
  return lands straight back on the executor.
- **Reviewer must resolve**: the prompt now hard-requires the reviewer to end
  with move_task (done on PASS / ready on FAIL) or escalate at the cap — NEVER
  leave the task in review. Leaving a PASS unresolved was the #1 cause of the
  re-dispatch loop.
- handlers review-completion case-C no longer writes the now-dead
  `update_task(assigned_agent="")`; the worker `update_task` descriptor +
  shared-agent playbook updated to reflect that workers/reviewers never unassign.


## 0.2.81 — 2026-06-02 — Self-heal agents stuck "working" (review/task starvation)

Fixes the report where tasks in Review assigned to active reviewers never got
picked up — a reviewer showed "working" with last activity "Worker session for
… was cancelled (shutdown)" on an OLD task, and its real task was never dispatched.

Root cause: a worker/reviewer session cancelled or shut down mid-flight could
leave the agent's in-memory supervisor state at WORKING with no live process
(the reader-loop's IDLE transition gets skipped by a CancelledError, and the
kill path didn't reset state). ``is_agent_busy()`` then returned True forever,
so every dispatch was a no-op and the agent's queued task never drained — with
no self-healing path.

- **New supervisor self-heal**: any agent in SPAWNING/READY/WORKING whose
  process is gone (None/exited) is reset to IDLE (task pointer cleared) at the
  start of every dispatch cycle — a stuck reviewer recovers within one tick
  instead of needing a daemon restart.
- **Kill now resets state directly** instead of relying solely on the
  exit-monitor / reader loop (which the teardown race can cancel).

Note: a task becoming unassigned when it moves to Review is BY DESIGN — the
worker self-releases and the separate ``reviewer`` field drives review.


## 0.2.80 — 2026-06-02 — Script execution, cron & outbox hardening

Closes the "Section C" robustness gaps across the three script trigger paths
(manual UI, cron, agent `execute_script`). Hardened across two adversarial
review loops to zero findings.

- **In-container script env was corrupted (NEW-1).** The agent `execute_script`
  path stringified raw `variables.json` *binding objects* into the child env,
  so a script saw `os.environ[VAR] == "{'kind':'literal','value':100}"` instead
  of `100` — silently breaking every literal-bound variable on the primary
  path. Bindings are now resolved to their literal value before injection.
- **Kill / timeout / shutdown actually stop the in-container process now
  (NEW-2).** `docker exec` doesn't forward signals without a TTY, so
  terminating the host client left the in-container Python running (orphaned).
  The launch records the in-container PID and `docker exec ... kill`s it
  (TERM→grace→KILL); the in-container path enforces a max-duration and reaps
  the whole process group.
- **Orphaned runs are reconciled honestly on restart (ADD-C1).** The office
  container is reused across daemon restarts, so a previous run keeps going.
  Instead of blindly marking it `failed` (which made the Manager rework a run
  that actually succeeded), the daemon checks the real in-container PID, kills
  a live orphan, and marks it failed with an honest message.
- **Transient script→Manager notifications are retried, not dropped (ADD-C2).**
  A `[Script: …]` callback that failed to deliver because the Manager was
  respawning/busy was permanently archived as rejected. It now retries with
  backoff (and is re-tried by the startup reaper), giving up only after a
  bounded number of attempts.
- **Agent `execute_script` gates on bootstrap status (ADD-C3).** Running a
  half-bootstrapped script now returns actionable "retry the bootstrap"
  guidance instead of a confusing `ModuleNotFoundError`.
- **Cron failures are visible (ADD-C5).** A refused cron run (missing/corrupt
  office secret, broken deps) now shows a failed run in the history with an
  actionable message instead of silently advancing the schedule.
- **Missed cron slots are signalled (ADD-C6).** When the daemon was down across
  scheduled slots, the gap is counted + logged (no surprising backfill).
- **Atomic progress reporting (ADD-C7).** New `cubicle.report_progress()` SDK
  helper writes `.progress.json` atomically so the poller never reads a torn
  file. Scripts should use it instead of hand-rolling the write.
- **Secret values no longer appear in the host process table (NEW-4).** The
  Docker launch switched from `-e KEY=VALUE` (value visible in `ps`) to
  `-e KEY` with the value supplied in the client's environment — restoring the
  documented "never in `ps`" guarantee for office/per-script secrets.

## 0.2.79 — 2026-06-02 — Review → Done routing & reviewer-orchestration hardening

Closes the "Section A" robustness gaps in automated review (falsely-`done` /
stuck-review / reviewer ping-pong). Hardened across 5 adversarial review loops.

- **Rework cap is now enforced, not prompt-only (ADD-A1).** An honest-FAIL
  reviewer that correctly returns `review→ready` every cycle no longer
  ping-pongs forever burning tokens — the `rework_count` ceiling is enforced
  in code, with escalation at the cap.
- **Designated-reviewer rework resumes its session (ADD-A2).** Rework no longer
  always starts a cold session up to 60s late; the resume/continuity path is
  wired for the automated-review flow.
- **`task_kill` no longer races the reviewer's queue (ADD-A3).** Review-submission
  teardown is agent-scoped, so it can't yank the just-enqueued review entry out
  from under a busy reviewer (no more ~60s reconcile stall).
- **Reviews no longer starve when the reviewer is inactive/deleted (ADD-A4).**
  A task whose designated reviewer was deactivated/deleted now falls back to the
  Manager Assistant instead of being re-queued forever to a queue nobody visits.
- **MA auto-approve requires a real verdict (ADD-A5).** The Manager Assistant no
  longer treats ANY clean session end as APPROVE — a skipped/empty/silently-
  failed review run can no longer ship unreviewed work to Done.
- **MA review-mode tool-call budget is enforced (ADD-A6).** A hard per-turn
  ceiling (env-tunable `CUBICLE_MA_TRIAGE_TOOL_BUDGET`, default 20) replaces the
  prompt-only "≤2 tool calls" guidance.


## 0.2.78 — 2026-06-02 — Async/script task boundary + reroute-before-archive

Authoring fixes from a live incident where a task asked one worker to run a
script/CI-push AND then read its result — impossible, because `execute_script`
ends the worker session at the trigger (3 identical failures before escalation).

- **Manager + Planner now bound async/script work into two tasks.** Any task
  that triggers `execute_script` (or a CI pipeline / background batch whose
  result lands out-of-band) is TERMINAL at the trigger; reading the log,
  verifying, filling the brief and submitting belong to a SEPARATE `depends_on`
  task. The completion notification bridges the sessions. This is now in both
  the Manager brief-authoring playbook and the Planner materialize playbook, so
  it generalizes to every backend MR / script scope.
- **Reroute-before-archive guidance.** Archiving a task auto-promotes +
  dispatches its blocked dependents — so when splitting/replacing a task, the
  Manager reroutes dependents' `depends_on` to the replacement FIRST and
  archives the old task LAST, avoiding a dependent firing against a stale premise
  mid-restructure.


## 0.2.77 — 2026-06-02 — Planner materialize re-run safety + audit-remediation fixes

Fixes runtime issues seen in live use plus a batch of AI-audit hardening.

Planner / Manager flow:
- **Materialize is now safe to re-run.** A partial/failed materialize used to
  leave duplicate, empty-brief task rows and could push the Manager to
  hand-author the scope. The Planner now checks `get_board(scope_id=…)` first
  and re-issues `create_task` with the same title to FILL incomplete-brief rows
  (idempotent on (scope, title) — never duplicates) or skip finished ones; the
  failure poke tells the Manager to **re-consult, not hand-author**; and the
  Manager playbook forbids hand-authoring a Planner-owned scope.
- Empty-brief signal corrected to `brief_is_complete:false` in the prompts.

Audit-remediation (parallel hardening pass):
- **MGR-01**: Manager reads the team roster from `context_data` (was blank every
  turn from the empty subprocess store).
- **MGR-02**: emit `manager_action_error` on the live tool-call path.
- **SYS-01**: Auditor output scan recurses so scoped `.py` dumps are caught.
- **BRD-03**: backend rework_count ceiling to stop infinite review loops.
- **ADD-E1**: recover stale `--resume` Manager sessions instead of wedging chat.
- **CMD-01**: XML-fence office-owner content in static CLAUDE.md.
- **ADD-F6**: atomic CLAUDE.md writes (temp+rename) so a mid-session read never
  sees a half-written file.

## 0.2.76 — 2026-06-02 — Manager can close scope verification + Planner visibility

Fixes a wedge where a scope stuck in `verifying` could not be closed, and adds
Manager-side visibility into Planner work.

- **The Manager can now close a scope's verification.**
  `complete_scope_verification` and the plan READS (`get_workstream_plan`,
  `get_execution_plan` — the two-pass flow's "review the skeleton" step) were
  only ever on the **Planner's** MCP surface, never the Manager's. They're now
  on the Manager surface too (shared `_mcp/tools_plan.py`; the Planner keeps the
  plan WRITES). This is why the Manager reported "the tool isn't on my surface".
- **Fixed the `actor='(none)'` rejection.** The Manager session runs with an
  EMPTY `AGENT_NAME`, so the backend tool-call envelope stamped
  `_caller.agent_name=''` and `complete_scope_verification`'s fail-closed role
  gate rejected the Manager. The `_caller` now resolves the Manager via
  `TASK_MODE=='manager'` → `agent_name='manager'`. Workers and the Planner are
  unaffected. `complete_scope_verification` is stripped from the Manager in
  General Chat (no scope context there).
- **Manager keeps the user informed about Planner work.** New playbook rule:
  announce every `consult_planner`, and summarize every `[Planner]` poke
  (including the backend-auto-fired scope verification) to the user before
  acting — so the Planner never looks like it acted unprompted.

(Backend-side, paired with this release: `planner_consulted` / `planner_completed`
activity events now show Planner engage + completion in the Workstream Events /
AI-Manager activity list.)

## 0.2.75 — 2026-06-01 — Planner two-pass authoring flow

Makes the AI Planner the **authoring engine** for multi-scope work so the
Manager is freed to manage, review, and verify. Multi-phase feature.

- **Skeleton-first, two-pass authoring.** For a real body of work the Manager
  consults the Planner (`consult_planner`, async). The Planner builds the
  workstream roadmap (right-sized scopes — **never more than 13 tasks each**),
  then for the next scope writes a SKELETON (`scope_plan` → titles + intents +
  deps + chips, no board rows). The Manager reviews; the Planner then
  `materialize`s the scope into tasks with full 9-field briefs. The Manager
  spot-reviews and activates. Each task is sized for a single focused AI
  session — solid + detailed, not fragmented, not bloated.
- **Five consult modes**: `roadmap` / `scope_plan` / `materialize` / `research`
  / `verify`. The Planner playbook + the Manager "Working with the Planner"
  section document the flow as default system behavior.
- **Robustness**: every dropped/failed consult pokes the Manager so it never
  hangs (Phase 3a) — EXCEPT backend-fired `verify` drops, which are logged
  only (the stuck-`verifying` sweeper re-fires/escalates instead of poking).
  A periodic sweeper rescues scopes wedged in `verifying`, and standalone
  (non-scope) task completions now poke the Manager (Phase 3b).
- **Scope-size soft cap (≤13)**: `activate_scope` attaches a non-blocking
  `size_warning` when a scope exceeds the ceiling — never refuses.
- **Backend role gates** for `consult_planner` (manager / MA only) and script
  authoring (Automation Script Developer only); board-write tools (incl.
  `consult_planner`) are stripped from the Manager in General Chat.
- **Prompt hygiene**: fixed the Auditor self-contradiction (it IS the
  designated reviewer and acts on its verdict), the custom-agent param-hint
  brace escaping, and assorted CLAUDE.md consistency gaps across the whole
  intake → plan → execute → verify → deliver lifecycle.

## 0.2.74 — 2026-06-01 — `cbcl stop` reliably stops + removes ALL office containers

Fixes the reported bug where office containers kept running after `cbcl stop`.

- **`cbcl stop` now ALWAYS runs a Docker-level sweep** after signaling the
  daemon — `stop_and_remove_managed_containers()` stops + removes every
  container labeled `cbcl.managed=true` (with a `cbcl-office-` name-prefix
  fallback), using its own Docker client. Works even on SIGKILL / crash /
  orphaned containers the daemon never tracked in memory. Graceful-shutdown
  window raised 10s → 25s.
- **Containers are now labeled `cbcl.managed=true` + `cbcl.office_id`** at
  creation so the sweep (and future tooling) can find them reliably.
- **Daemon teardown reordered**: containers torn down FIRST (kills the
  in-container `claude`, so agents exit in ~8s instead of 30s), agent grace
  30s → 8s, plus a label-based backstop sweep at the end.
- **`start_office` recreates a running container whose image is stale**
  (image id ≠ `cbcl-agent:latest`). Fixes silently-stale agent images after a
  cbcl upgrade — the root cause of "Planner tool disabled" where an old baked
  image lacked `consult_planner`. A `cbcl restart` after upgrade now self-heals.

Pairs with platform backend v3.6.4 (per-office Execution Planning gate —
`offices.execution_planning_enabled`, replacing the global env).



## 0.2.73 — 2026-06-01 — Planner consult flow as default system behavior

The Manager now reliably uses the Planner via the `consult_planner` tool
instead of rationalizing it away as "shorthand for a board task":

- **Manager CLAUDE.md** gains a dedicated "Working with the Planner" section:
  `consult_planner` is a REAL async tool (returns `{status: engaged}`, pokes the
  Manager back in chat), documents the four modes (roadmap/scope_plan/research/
  verify) and the end-to-end multi-scope flow, and states the hard rule —
  NEVER `create_task` assigned to the Planner / set it as a reviewer.
- **Team roster** annotates the Planner as consult-only so the Manager sees it
  at turn start, not as an assignable worker.

Pairs with the platform backend, which now ENFORCES this (create_task /
update_task reject `assigned_agent`/`reviewer == "planner"`). Deploy the
matching backend alongside this cbcl version.


## 0.2.72 — 2026-06-01 — Right-size the work (stop over-engineering simple tasks)

Playbook overhaul so the AI does simple things simply. Previously a one-shot
check (verify an SSH connection, confirm a GitLab PAT is valid) was turned into
an Automation Script Developer script + scope. Now:

- **Manager Assistant** is the light path for **direct one-shot verifications**
  (ssh/curl/git/openssl connectivity + credential checks): it runs the command
  and reports PASS/FAIL — never builds a script, never loops/schedules/mutates,
  always redacts secrets. (Pairs with the backend giving MA the `Bash` tool.)
- **Manager** gains a "Right-size the work" effort ladder: Tier 0 one-shot → MA
  (no scope/script/Planner); Tier 1 small multi-step; Tier 2 reusable → ASD
  script; Tier 3 multi-scope → Planner. One-off standalone tasks are now
  first-class, not a "rare" exception.
- **Automation Script Developer** right-sizes before building: one command
  answers it → run-and-report, don't scaffold a mini-project; build only for
  reusable / repeatable / scheduled / iterative work.
- **Planner** is never the tool for a one-shot job → recommends routing to MA.

Requires the matching platform backend (Manager Assistant with the `Bash`
tool) — deploy backend alongside this cbcl version.


## 0.2.71 — 2026-06-01 — AI Planner consult path (Execution Plan feature)

Adds the communicator half of the **Execution Plan + Planner** feature. The
Manager consults the office **Planner** (5th system agent) asynchronously via
the `consult_planner` tool; the Planner spawns as a one-shot worker session
(`AGENT_NAME=planner`) with a planning toolset (board reads + create_task/
create_scope/update_task/activate_scope + the plan-write/verify tools), writes
the workstream roadmap + per-scope Execution Plan, verifies a completed scope,
and pokes the Manager in chat when done.

- New: `_agent_image/_mcp/tools_planner.py`, `orchestrator/planner_prompt.py`,
  `config_sync/.../_system_agents/_planner.py` (Planner CLAUDE.md playbook).
- `mcp_tool_server` selects the planner toolset + exempts it from the executor
  guard, keyed on `AGENT_NAME=="planner"`.
- `handlers.py` `consult_planner` command → spawns the Planner; `task_complete`
  carrying `planner_consult` routes to the Manager poke (`ingest_planner_result`).
- The Planner toolset deliberately EXCLUDES destructive/manager-only tools
  (move_task, delete_task, archive_task, retry_blocked_task, decide_action_request).

Requires the platform backend with the matching Execution Plan endpoints +
`EXECUTION_PLANNING_ENABLED` to exercise the full scope-verification flow.


## 0.2.70 — 2026-06-01 — SSH-key awareness + un-mask host script-execute errors + corrected /tool-call auth

Two production fixes plus the corrected SEC3-01 auth.

**SSH keys now visible to agents.** Keys added in Settings → Security → SSH
Keys land at `/home/agent/.ssh/<name>` in the office container, but no
agent-facing CLAUDE.md ever said so — a worker asked to SSH looked in office
secrets, found nothing, and reported "SSH is not in the Secret". Added an
"SSH Access" section to the office-wide `SHARED_OFFICE_CLAUDE_MD` (every agent
reads it): keys live at `~/.ssh/<name>`, are NOT office secrets, `ls ~/.ssh/`
to discover them, `ssh -i ~/.ssh/<name> user@host` to connect (openssh-client
installed), `escalate_blocker category=credentials` when genuinely missing.

**`execute_script` no longer reports "status 500: unknown".** The in-container
forwarder read `body["message"]` on a generic host 500, but the host runner's
catch-all puts the cause in `body["error"]` and sets no `message` — so the
real error (e.g. a `DepsInstallError` when a requirement like paramiko fails
to install) was masked as "unknown". Now falls back to `err_msg or err_kind`,
so the actual host error reaches the agent. +2 proxy tests lock it in.

**SEC3-01 corrected (`/tool-call` auth, daemon side).** The host callers that
POST to `/tool-call` — `task_dispatcher` (`_move_and_assign`/`_assign_only`),
`handlers.py` review/recovery paths (6 sites), and `watchdog.py` — now send
`auth_headers(security_token)` (Company-Token bearer). The worker subprocess
sends `X-Office-Secret` from its env. This is the daemon half of the corrected
fix: the backend accepts EITHER channel, gated behind
`CUBICLE_TOOL_CALL_REQUIRE_AUTH` (default off) for a daemon-first rollout, so
no repeat of the 2026-05-31 dispatch regression.

src/ + tests/ identical to the monorepo communicator/.


## 0.2.69 — 2026-05-31 — Security: authenticate the direct /tool-call path (SEC3-01)

Closes a cross-tenant hole where the backend's HTTP `/tool-call` endpoint was
gated only by "is the daemon online?". The daemon now receives a per-office
capability secret in `sync_config`, threads it into each agent's MCP env
(`CUBICLE_OFFICE_TOOL_SECRET` → `OFFICE_TOOL_SECRET`), and the in-container MCP
server sends it as the `X-Office-Secret` header on its direct tool-call POSTs.

* This is distinct from the Company Token (host-only — never enters a
  container). The primary proxy→WS tool path is unchanged (office-pinned).
* Restart-safe: the secret is re-minted by the backend on every (re)connect
  and re-delivered via sync_config, so a `cbcl` restart with this version
  picks it up automatically.

src/ + tests/ identical to the monorepo communicator/.

## 0.2.68 — 2026-05-31 — Security: script-failure log no longer leaves the host

Synced from the monorepo security-audit fixes.

* **Secret-containment fix:** on a script's non-zero exit, the host runner
  no longer ships the `log.txt` tail (which can contain an injected secret
  value) to the platform backend as `error_message` — it sends a generic
  "exit code N; see local log" and keeps the full log host-local. Upholds
  "credentials never leave the user's machine".

(Backend + frontend security fixes in the same audit live in the monorepo;
the open CRITICAL `/tool-call` per-office-auth item is tracked in
docs/improvements_v3/security.md.)

## 0.2.67 — 2026-05-31 — Comprehensive review: security + bug fixes

Synced from the GitLab monorepo `communicator/` after a comprehensive
daemon review (resolves a 6-file src drift where the monorepo was ahead).

### Security
* Removed `session_bridge.execute_script_in_container` — dead function that
  f-string-interpolated JSON into a `python3 -c` source string (code-injection
  footgun; zero callers).
* In-container path-traversal guard (`_is_safe_path_segment`) on
  `script_name` / `execution_id` in `_mcp_script_exec.py`.
* `chmod 0700` on the office-secrets parent dir (mirrors the ssh-keys store).

### Bug fixes
* `ws_client._mark_disconnected` now fails in-flight RPC futures fast (was
  hanging 30s on every transient WS drop).
* Forward-declared `router` in `handlers.init_office_process_model` (latent
  NameError if an agent event fires during init).
* `agent_queue._extract_agent_from_key` reconstructs colon-containing names.
* `watchdog.safe_send` logs swallowed errors instead of silent pass.

### Hygiene
* ruff clean (unused imports / empty f-strings / a redefinition removed; dead
  `TOKEN_ENDPOINT` removed; `cli_commands` E402 cleared). Full suite green.

## 0.2.66 — 2026-05-31 — Office-deletion cleanup + hide ssh-keys from Files tree

Bug fixes for per-office host state surviving deletion (keyed by the
office NAME's slug, so a new same-name office inherited the old data).

### What changed

* **Office deletion now wipes the per-office workspace dir.** Previously
  `~/.cubicle/workspaces/<slug>/` (outputs/, .scripts/, the Claude-auth
  backing `.claude-auth/`, and `ssh-keys/`) survived deletion — a NEW
  office with the same name reused it, showing stale files AND being
  silently pre-authenticated to Claude. A new teardown phase rmtree's
  the workspace after the container is removed; new same-name offices
  now start clean and require Claude re-auth.
* **Fixed: office-secrets cleanup never actually ran.** It used
  `from src.utils import slugify` (slugify lives in `src.paths`), so it
  raised an ImportError that was silently swallowed — the host
  office-secrets file was never removed on delete. Corrected the import.
* **Destructive cleanup is gated to TRUE deletions** (the office_deleted
  push). An office merely missing from discovery — PARKED (token
  revoked) or REASSIGNED to another daemon — keeps its workspace +
  secrets. No data loss on park/reassign.
* **Files tree hides `ssh-keys/` and `.claude-auth/`** — cubicle-internal
  mount-backing dirs, not office files.

## 0.2.65 — 2026-05-30 — Internal guards + dead-code cleanup (no behavior change)

Maintenance release synced from the monorepo. **No operator-facing
behavior change** — upgrading is optional.

### What changed

* **Import/parse smoke test** (`tests/test_imports_smoke.py`): imports every
  daemon `src/` module and `ast.parse`-checks the in-container
  `_agent_image/` modules, so a syntax/import error in any inline-imported
  module fails CI immediately (the class of bug behind the 5-week "manual
  scripts stuck on running" outage).
* **Agent-image COPY-sync guard** (`tests/test_agent_image_copy_sync.py`):
  asserts the cache-hash file set (`container_manager._mcp_server_source_files`)
  stays in lockstep with the `Dockerfile.agent` COPY lines, so the image
  can't silently ship stale MCP code.
* **ManagerController**: removed the never-wired single-process WS-transport
  fallback (`ws_client`/`container_name` params + `_ws` branches) — dead code.
* **container_manager**: extracted `_mcp_server_source_files()` as the single
  source of truth for the hashed COPY set (hash output unchanged).

## 0.2.64 — 2026-05-29 — Authoritative AI office generation

The office-setup wizard now **designs the best office from any level of
input** — a single sentence or a full spec — instead of transcribing
what the user typed and tacking on suggestions.

### What changed

* **Prompts (`_setup_prompts.py`) flipped to "principal architect".**
  The generator now DECIDES and BUILDS: it fills gaps, overrides weak
  or under-scoped ideas, and commits to a complete design. Removed all
  "propose / flag / rationale / gaps / to-be-refined" machinery and the
  0–2 proactive-agent cap. The vision is the authoritative complete
  brief; the office instructions must state decided conventions (no
  placeholders). **No workstreams are generated** — those are the
  user's concern after setup.
* **Design pass now runs on the Opus tier** (`_setup_cli.py`,
  `_DEFAULT_GENERATION_MODEL` → Opus). The one-time office design is the
  highest-leverage moment in an office's life, so it gets the strongest
  model (~15–20 min/run). Operators who want a faster/cheaper setup can
  set `CBCL_GENERATION_MODEL=claude-sonnet-4-6`.
* **Generated agents are forced onto the canonical worker model (Opus)**
  in both the wizard roster and the single-agent "Create with AI" flow
  — decoupled from the design-pass model, so a future tier bump
  propagates without editing prompt literals.
* **Duplicate custom agent slugs are de-duped** in the roster pass so a
  model hiccup can't produce two agents with the same slug (which would
  otherwise fail the backend's atomic office apply).
* Plus the previously un-mirrored monorepo communicator drift (wave
  audit fixes across `manager_context.py`, `agent_supervisor.py`,
  `manager_controller.py`, `handlers.py`, `config.py`, `cli_commands.py`,
  the manager CLAUDE.md template, and worker MCP tools) — this release
  brings the public CLI fully in sync with the monorepo.

Requires the matching platform release (GitLab `v3.2.5`) for the new
`POST /api/offices/{oid}/apply-config` endpoint the wizard now uses.

### Tests

Full communicator unit suite green (1026 passed). New
`tests/test_setup_authoritative.py` locks the prompt + model invariants
(authoritative framing present; no proposed/workstream/rationale fields;
generation + agents on Opus).

## 0.2.63 — 2026-05-29 — CRITICAL hotfix

**Manual script runs were stuck on "running" forever.** Tiny scripts
with one `notify_manager` call sat at status=running for 20+ minutes.

### Root cause

Commit `22a8efb` in the monorepo (April 21, "delete v1 legacy path
end-to-end") removed the `finally:` block in
`script_execution.on_complete` — the block used to clean up `_run.py`
which v1 wrote with inlined secrets. The matching `try:` keyword
was left dangling, so the file has been syntactically broken since
April 21 and `script_execution.py` couldn't be imported.

It wasn't caught because every caller uses INLINE
`from src.scripts.script_execution import ...` inside function bodies
(not at module top-level). The SyntaxError only fired at the first
CALL of `monitor_all`, the asyncio background task crashed silently
(no exception ever surfaced — asyncio's "fire-and-forget" lifecycle
only logs at GC time, which doesn't happen during normal daemon
operation), and the host-side script monitor was effectively dead.

Symptoms:
* Manual UI runs → status=running forever; the script actually ran
  and wrote its log + outbox file, but the monitor that would call
  `on_complete` was dead → status.json never flipped, `.outbox/`
  never drained, `notify_manager` never delivered.
* Agent-triggered runs → unaffected; they use the in-container MCP
  server's separate monitor path.

### Fix

1. Dropped the dangling `try:` (and unindented its block) in
   `on_complete`. Original `finally:` body was already deleted in
   22a8efb so the wrapper served no purpose.
2. Module-level smoke test (`TestScriptExecutionParsesCleanly`)
   that runs `ast.parse` + `importlib.exec_module` so any future
   dangling-try in this file fails CI immediately rather than
   hiding behind inline imports.
3. Defence-in-depth fallback: `_resolve_exit_code_via_waitpid`
   probes `os.waitpid(pid, WNOHANG)` when
   `Process.returncode` stays None — catches future cases where
   asyncio's child watcher genuinely drops a SIGCHLD under heavy
   subprocess concurrency. Plus `_infer_exit_code_from_log` as a
   last-ditch heuristic when something else reaped first. 9 new
   regression tests.

### Operator action

```bash
pipx upgrade cubicle-communicator
cbcl stop && cbcl start -d
```

0.2.62 is broken on manual script runs — upgrade is required.


## 0.2.62 — 2026-05-29

Wave 6 audit follow-up — covers the daemon-side fixes from the
W6-P2/P3/P1/P5 sweep. The backend-only fixes ship separately
in the monorepo at the same commit hash.

### Security hardening (this repo)

* **Script cron-mutation tools now restricted to the Automation
  Script Developer.** ``_SCRIPT_AUTHOR_ONLY`` previously covered
  authoring tools (``register_script``, ``clone_script``,
  ``install_script_from_template``, ``bind_script_variable``) but
  missed the cron-mutation family. Any non-ASD worker could
  schedule the ASD's scripts to run hourly with arbitrary
  ``variable_overrides``. Now ``schedule_script`` /
  ``update_script_cron`` / ``delete_script_cron`` are author-only.
  (W6-A5-HIGH-6)

## 0.2.61 — 2026-05-29

Wave-5 follow-up sweep. Covers everything between v0.2.60 and the
2026-05-29 walkthrough — security + data-integrity hardening
(W5-P1), AI execution + chat reliability (W5-P2), AI prompt
hygiene (W5-P3), a /simplify cleanup pass on the 0.2.0 → 0.2.2
phase, plus the three bugs the user surfaced live (Discussion-tab
tool-use noise, bootstrap RPC false-positive timeout, and the
notify_manager → workstream chat regression).

### Daemon-side (this repo)

* **Process IPC reader UTF-8 hygiene** — all three NDJSON reader
  sites (``orchestrator/agent_supervisor.py``, ``agent_worker.py``,
  ``docker/session_bridge.py``) now decode with
  ``errors="replace"``. A single malformed UTF-8 byte from a buggy
  producer used to kill the reader loop → the heartbeat died →
  the supervisor reaped the agent. A one-bad-byte DoS vector.
  (W5-P2-H1)
* **Per-turn context lock in ManagerController** — mid-turn
  ``handle_switch_context`` no longer overwrites the active
  context key. In-flight response chunks / manager_action cards /
  manager_state heartbeats keep routing to the chat where the
  turn started; the switch applies when the turn ends.
  (W5-P2-C2)
* **Cancel-mid-turn doc fix** — agent worker's cancel-path comment
  used to claim "next turn will start fresh"; actual behaviour
  preserves the prior turn's session_id (the receiver's empty-id
  guard skips the save, history survives). Comment updated to
  match. (W5-P2-C1)
* **Shutdown narrow-catch** — ``AgentSupervisor.shutdown``'s
  bare ``except Exception`` narrowed to
  ``(RuntimeError, OSError, BrokenPipeError)`` so a parent
  ``CancelledError`` no longer gets swallowed mid-shutdown.
  (simplify pass)
* **Background-task retention** — ``_run_history_backfill`` +
  ``_refresh_mcp_list`` startup kicks now register in
  ``_BACKGROUND_TASKS`` (with done-callback discard) so Python's
  GC can't collect them mid-execution. (simplify pass)
* **Config + discovery helpers** — ``_LEGACY_IP_URLS`` set
  collapsed to ``_is_legacy_platform_url`` via
  ``urlparse().hostname``; new ``_discovery_url()`` extracted to
  dedupe two call sites; ``agent_supervisor`` cleanup of stale
  comment. (simplify pass)
* **B2b: agent-triggered scripts now route notify_manager to the
  task's workstream chat** — ``_agent_image/_mcp_script_exec.py``
  was missing the ``CUBICLE_WORKSTREAM_SHORT_CODE`` +
  ``CUBICLE_SCOPE_READABLE_ID`` env-var injection that the
  host-side ``script_runner.py:_build_env`` has had since v0.2.23.
  Agent-triggered runs were silently routing to general_chat
  while UI-triggered runs went to the workstream — divergence
  the user noticed live.

### AI prompt content (this repo)

* **Manager self-check restructured as numbered checklist** in
  ``MANAGER_CLAUDE.md``. Models parse checklists more reliably
  than the previous OR-joined paragraph; the restructure also
  spells out the escalation path explicitly. (W5-P3-H1)
* **Worker blocked-task playbook aligned with the spec** —
  ``_shared_agent.py`` now ships the verbatim
  ``ESCALATED (<blocker_class>): ...`` template + the full
  8-value enum table. Workers had no way to learn the template
  before, so blocks came out unstructured and the MA fell back
  to its "unknown" routing path. (W5-P3-H2)
* **Priority-emoji hints stripped** from
  ``orchestrator/worker_prompt.py:_PRIORITY_HINT``. Literal words
  ("URGENT" / "High" / "Medium" / "Low") carry the urgency
  without the 🔥 / 🟠 / 🟢 / ⚪ glyphs. Per the no-emoji project
  directive. (W5-P3-H4)

### MCP tool surface (this repo)

* **Manager tool description trim** — 7 of the heaviest
  descriptions in ``_agent_image/_mcp/tools_manager.py``
  tightened. Tool descriptions are loaded into context on every
  Manager session; the trim shaves ~30% prose-token weight off
  the descriptors. Operational guidance + rationale already
  lives in MANAGER_CLAUDE.md; the tool descriptions only need
  to disambiguate at tool-choice time. (D3)

### Test additions (this repo)

* 5 new test files locking in W5-P2 + W5-P3 + B2b:
  ``test_manager_controller::TestContextSwitchLock`` (4),
  ``test_reader_utf8_hygiene`` (5),
  ``test_claude_md_writer`` checklist + blocker-template pins
  (9), ``test_worker_prompt`` no-emoji pin (1),
  ``test_mcp_script_exec_env`` env-injection pins (4).

## 0.2.60 — 2026-05-28

Wave-4 audit cycle. 5 fresh agents covered AI prompts + generation,
setup wizard, performance + scaling, test failures, and
documentation drift. This release ships the highest-impact items.
**Test suite now goes from 8 pre-existing failures → 0 failures.**

### Daemon-side (this repo)

* **AI-gen handlers — length caps + prompt-injection fencing**
  (`src/_handlers/_requests.py`). Three AI generation actions
  (`generate_agent_config`, `generate_workstream_context`,
  `generate_skill`) now cap user-supplied free-text inputs at
  10K chars and fence the fence-closing tokens (``</user_input>``
  etc.) so a malicious description can't break out of its data
  fence and inject instructions the model would follow.
  ``generate_agent_config`` additionally caps AI-output
  ``display_name`` to 255 chars / ``name`` to 100 chars and
  validates ``skill_names`` / ``connector_names`` against the
  catalog so a hallucinated slug doesn't silently null out the
  agent's assignment.

### Companion server-side + frontend (in platform monorepo)

* **8 pre-existing test failures fixed** — slug-collision guard
  in ``create_office`` (commit 6bf1d00) was rejecting the second
  office-create in cross-tenant assertion tests. Test helpers
  now suffix office names with a per-call uuid.
* **ASD short prompt credential model** aligned with the playbook
  + Phase-1.5 reality: ``bind_script_variable`` is canonical;
  ``from_office_secret:`` is deprecated for new scripts.
* **F18 model-split comment** removed (the per-agent Sonnet split
  was reverted to uniform Opus a release ago — comment was stale).
* **Setup wizard creates proposed workstreams on Accept**
  (`useSetup.ts`). The AI proposes starter workstreams but the
  Accept handler never created them — user landed on the Manager
  page with an empty sidebar.
* **ConnectingStep 60s timeout + diagnostic copy** — pre-fix the
  spinner ran forever on container-build failures.
* **list_executions skip daemon RPC when DB rows < 30s old** —
  cuts ~12 daemon RPCs/min per open script-detail tab to ~0 in
  steady state.
* **Partial JSONB index on ``setup_office_secret`` payload name**
  (Alembic ``e4f5a6b7c8d9``) — every script launch missing a
  secret hit this query as a sequential scan.
* **Negative-cache test** updated to lock in v0.2.58 behavior.
* **Documentation drift** — `manager_action_error` frame added
  to ws-protocol spec; `list_agents` tool added to manager-spec;
  Integrations REST API removed from rest-api spec (module is
  deleted); root CLAUDE.md monorepo structure points at
  ``/frontend_v2.1`` (current SPA) not legacy ``/frontend``.

## 0.2.59 — 2026-05-28

Wave-3 audit fixes — 5 fresh parallel agents covered persistence,
auth/multi-tenancy, frontend state, KB/Files, and ops/observability
(scopes the prior waves didn't deeply touch). Shipping the 11
highest-impact findings; the rest queued.

### Daemon-side (this repo)

* **fs_handler path validator hardened** (`fs_handler.py`). The
  pre-fix ``_safe_resolve`` only split on ``/`` for traversal
  detection — ``..\\..\\etc\\passwd`` and embedded NUL bytes
  slipped through. Now rejects NUL bytes, control bytes (\\x01-\\x1f),
  and normalises backslashes to forward slashes BEFORE the
  traversal check. Closes a class of path-bypass vectors that
  matter on Windows-mounted Docker workspaces and downstream
  syscalls that truncate at NUL.

### Companion server-side + frontend (in platform monorepo)

* **EMERGENCY_DISABLE_AUTH multi-Company guard** — refuses to
  boot when the DB has more than one Company. Pre-fix the flag
  silently impersonated whichever Owner row Postgres returned
  first → cross-tenant data exposure.
* **CSRF compare uses hmac.compare_digest** — timing-leak hardening.
* **HSTS bumped to 1 year + includeSubDomains** on landing nginx
  (was 5 minutes from the pre-launch ramp).
* **REST /fs/write|mkdir|rename|delete now broadcast
  filesystem_changed** — other open tabs no longer show stale
  file trees after a mutation.
* **Frontend scope-detail invalidation** — `useScope` /
  `useScopeTasks` use the singular "scope" key; the WS handler +
  activate/archive mutations now invalidate it.
* **setConnectionDetails field-compare guard** — heartbeats no
  longer create new object refs that cascade re-renders.
* **DiscussionTab scroll-to-bottom guard** — checks last-id +
  near-bottom heuristic; no more user-yanking on every refetch.
* **Backend prod healthcheck** — closes the 10-30s 502 window
  during lifespan startup.
* **Log scrubber processor** — structlog now redacts any field
  matching ``password|token|secret|api_key|authorization|cookie``
  as defense-in-depth.
* **KnowledgeFolder uniqueness** — replaced the misleading broad
  UniqueConstraint (NULL parents bypassed it) with two partial
  unique indexes covering both NULL and non-NULL parent cases.
  New Alembic migration ``d3e4f5a6b7c8``.

## 0.2.58 — 2026-05-28

Wave-2 follow-up to v0.2.57's comprehensive pre-launch audit.
Picks up 14 of the deferred MEDIUM + remaining HIGH findings.

### Daemon-side (this repo)

* **GIVING UP now emits sticky error to UI** (`docker/container_health.py`,
  `docker/container_manager.py`, `daemon.py`). v0.2.54 stopped the
  infinite restart spam after 10 failed attempts, but the loop went
  silent and the UI just showed "disconnected". Now publishes a
  ``health_status`` event so the backend stamps
  ``connector_statuses.last_error`` with actionable copy (try
  ``docker logs``, try ``cbcl stop && cbcl start``).
* **office_deleted secrets cleanup tries both captured AND current
  name** (`daemon.py`). Rename-then-delete used to leak the host
  secrets file because the slug derived from the captured-at-
  connect name. Best-effort: still derives both candidates and
  cleans each.
* **Outbox watcher scan-lock dict prunes opportunistically**
  (`scripts/outbox_watcher.py`). The dict accumulated dead entries
  across script renames; now walks every 200 new-key inserts and
  drops entries whose script_dir no longer exists. Bounded
  growth on long-running daemons.
* **find_status_on_disk documents O(N) fallback semantics**
  (`scripts/script_notifier.py`). The agent-side `get_script_status`
  cold lookup is genuinely O(N-scripts) when the in-memory cache
  misses — kept the function shape but added clear docstring on
  the contract so future readers don't try to "optimize" by
  requiring script_name (would break the cold-lookup callers).

### Companion server-side (in platform monorepo)

* `ActionRequestStatus` Literal now derives from STATUS_VALUES
  with a runtime assert — the v0.2.57-class drift cannot recur.
* CLI-auth Redis TTL 300s → 600s with new actionable error copy.
* `mark_thinking_after_action` now fires on Manager-action error
  path too — UI "Updating task" pill no longer stuck for 5 min
  after a failed action.
* ExtraMounts validator now refuses `host_path` inside
  `~/.cubicle/` so accidentally exposing Office Secrets via a
  custom mount is impossible (SECURITY).
* Status-update Redis-Streams path now sends `task_moved`
  command to the daemon (matches legacy WS path) so the
  dispatcher releases the agent immediately on submit.
* `auto_resolve_setup_office_secret_for_name` broadcasts
  `action_request_decided` for each row — inbox updates live.
* `set_connector_agents` triggers `push_sync_config_to_daemon`
  so connector-agent assignment changes propagate immediately.
* `create_script` re-locks the row before bootstrap to serialise
  against a concurrent Retry click.
* Connector OAuth defers the `mcp/add` until token exchange
  succeeds — no more stranded "needs_auth" entries on failure.
* GitHub fetcher: 5-min negative cache for failed fetches so a
  GitHub rate-limit doesn't get hammered by every new install
  attempt.

## 0.2.57 — 2026-05-28

Wave-1 pre-launch comprehensive audit (5 parallel specialised
agents). This release ships the highest-impact fixes from ~50
findings across Scripts, Task execution, Connectors/Skills/MCP,
Docker infrastructure, and cross-component WS/RPC drift.

### Critical — daemon-side

1. **`OfficeSecretsCorruptError.detail` AttributeError on refusal**
   (`src/dispatch.py`). The corrupt-office-secrets handler tried to
   read `exc.detail` on a class that has no such attribute — the
   bare `except Exception` 5 lines below caught the AttributeError
   and produced the useless toast "Unexpected error:
   'CorruptOfficeSecretsError' object has no attribute 'detail'".
   Replaced with `str(exc)`.

2. **Cron overlap-skip silently advanced `next_run_at`** (`src/scripts/cron_scheduler.py`).
   A 5-min cron whose script takes 6 min lost the next scheduled
   fire EVERY overlap cycle and never caught up. Now leaves
   `next_run_at` alone; the next tick re-evaluates and fires the
   moment the long-running execution finishes.

3. **`outputs/` + `.scripts/` chown was not recursive on container
   start** (`src/docker/container_manager.py`). Old root-owned files
   from a sudo-cbcl history wedged agent writes with confusing
   EACCES errors deep into a 30-minute task. Now recursive on those
   two platform-managed dirs while leaving the rest of `/workspace`
   alone.

### Companion server-side fixes (in platform monorepo)

* **ActionRequest Pydantic Literal missing `acknowledged`** —
  every `GET /action-requests` that returned an acknowledged
  informational row 500'd.
* **`list_agents` Manager tool had NO backend handler** — Manager
  calls returned "Unknown action: list_agents" every time.
* **`manager_action_error` chat frame silently dropped** — backend
  emitted it, frontend had no dispatcher case; Manager actions
  that failed showed NOTHING in the UI.
* **`read_manifest` / `write_manifest` broken on split-host prod**
  — same root cause as v0.2.55 executions fix; backend reads local
  disk that's empty in prod. Now routes through `fs_read`/`fs_write`
  bridge with local fallback for same-host dev.
* **OAuth code-paste path completely dead** — read wrong Redis key
  and dereferenced wrong dict field. Both writers + readers now
  agree on `mcp_oauth_connect:{office}:{name}` pointer +
  `mcp_oauth_state:{state}` payload + `redirect_uri` field alias.
* **`task_status_update` Redis-Streams path didn't broadcast
  worker comments live** — Manager Assistant lost `blocker_class`
  signal until the next REST refetch; Discussion tab stayed empty
  during real work.
* **Bootstrap retry fell back to DESTRUCTIVE bootstrap when bridge
  unreachable** — the v0.2.56 non-destructive fix was inert when
  the daemon was briefly offline. Now refuses with a clear error
  instead.
* **Auto-unblock didn't reset `blocked_bounce_count`** — second
  re-block after user approval deadlocked the task at the cap.
* **Connector WS disconnect had no immediate chat broadcast** —
  30s gap before UI learned about it.
* **Skill secret params had no warning banner** — values persist
  but don't actually flow anywhere; banner steers users to Office
  Secrets / Connectors for real credentials.

## 0.2.56 — 2026-05-28

Three user-reported bug fixes from a hands-on test of the
script flow. None of these symptoms would have been caught by
unit tests; all three required driving the live UI to trip.

### Critical — manual "Run" button did nothing visible

User clicked Run, saw a "queued" toast, then silence. No
execution-history row, no chat notification, no terminal
event. The script DID start (status.json landed on disk) but
the backend never heard about it.

Root cause: `ScriptRunner._router` was **never wired**. The
daemon's init path called `script_runner.set_manager(mgr)` after
construction but no equivalent `set_router(router)`. Every
`if self._router is not None:` guard inside execute() and the
monitor loop silently skipped the publish path. Agent-triggered
runs use the in-container MCP's direct HTTP POST so they
bypassed this bug — that's why only the manual UI Run path
was visibly broken.

Fix: added `ScriptRunner.set_router(router)`, called from
`handlers.py` immediately after `mgr.set_router(router)`.

### Critical — Manager Notifications popup empty (v0.2.55 regression)

The v0.2.55 daemon RPC for notifications returned wrong field
shape — frontend's `ScriptNotification` interface expects
`rejected`, `reason`, `script_name`, but my enumerator returned
`day`, `task_id`, plus no `emitted_at_ms` derivation from the
filename ms-epoch. Frontend dropped every row.

Fix: rewrote `list_notifications_on_disk` to mirror the backend's
`_collect_notifications` shape exactly. Added `_collect_notify_dir`
helper that handles both successful (`.processed/<day>/`) and
rejected (`.processed/<day>/rejected/`) sub-trees, same as the
backend. Sorted newest-first by ms-epoch.

### High — Bootstrap RPC timeout banner + destructive Retry

User opened a freshly-created script. Banner: "Bootstrap RPC timed
out. The communicator didn't respond within 15s per file."
Underneath: a "Retry Bootstrap" button that **overwrote main.py
with empty boilerplate** when clicked, destroying every edit the
user / agent had made between the (transient) timeout and the
retry.

Backend-side fixes (in the platform monorepo, not this repo):

* `service.get_script` now auto-reconciles `bootstrap_status` to
  `complete` when every template file is present on disk via an
  `fs_tree` check. The banner stops showing without anyone needing
  to click anything destructive.
* `retry_bootstrap` is now NON-DESTRUCTIVE: it calls `fs_tree`,
  diffs against `BOOTSTRAP_FILES`, and writes ONLY the files that
  are missing. Existing files are preserved. If every file is
  present, it's a pure DB-status flip with no `fs_write` call.

## 0.2.55 — 2026-05-28

Pre-launch comprehensive audit. Four parallel review agents combed
the Scripts, Onboarding, Chat/Board, and Connections subsystems.
This release ships the high-impact fixes; the rest are tracked in
the monorepo issue tracker.

### Critical — script history + Manager notifications were empty on prod

Root cause: backend's `_scan_disk_executions` and
`list_notifications` read `~/.cubicle/workspaces` directly, but on a
split-host production deployment (backend + daemon on different
machines) that directory doesn't exist on the backend host. Any
`record_script_execution` HTTP call that failed silently (transient
backend issue) was lost forever from the UI's perspective. The user
reported "5 execution dirs on disk but only 1 in the History
popup" — this was the cause.

Fix: two new daemon RPCs over the existing connector WS:
- `script_list_executions` — enumerates `.scripts/<name>/executions/`
- `script_list_notifications` — enumerates `.scripts/<name>/.outbox/.processed/`

Daemon-side enumerators live in `src/scripts/_disk_enumerators.py`
and mirror the backend's local-disk scan shape exactly so the
existing DB-backfill / merge / sort paths work unchanged. Backend
falls back to its local-disk path if the bridge is unreachable
(works on same-host dev).

### Companion server-side fixes (in platform monorepo, not this repo)

- Activity `details` field added to `activity_added` broadcasts —
  "Using Bash" / "Using Read" badges no longer vanish on the next
  render.
- Manager-action error envelopes no longer broadcast as success
  cards with empty fields.
- Discussion tab now surfaces `tool_run` / `file_saved` /
  `files_listed` events so the conversation reflects what the
  worker is actually doing.
- Archived workstreams filtered from Manager General Chat context.
- Workstream creation rejects whitespace-only names.
- Token revocation now closes connector WS immediately (was a
  no-op until the daemon disconnected for some other reason).

## 0.2.54 — 2026-05-28

### Critical — container restart escalation was a no-op

The health-check loop logged "ESCALATION: Attempting forced restart"
every 90 seconds for any exited container, but the restart never
actually happened. ``ContainerManager.health_check_all`` was calling
``container_health.health_check_all(self._containers, on_crash)`` —
passing only the on_crash callback positionally, leaving on_restart
defaulted to None. So the loop:

1. Detected exit at counter=1, then 2, then 3.
2. Logged "Attempting forced restart" (no actual restart).
3. Reset counter to 0.
4. Repeated forever — perfect 90-second log spam, container stays
   offline indefinitely until an operator manually intervened.

Reported on cbcl-stg at 12:50–13:06 against the Development office.

Fix: added ``ContainerManager.force_restart_office(office_id)`` which
calls ``container.start()`` in place (Docker preserves the launch
config so no slug / workspace lookup needed), and wired it as the
``on_restart`` callback. Also wired the previously-dropped
``on_crash`` arg through.

### Defence — escalation cap stops infinite restart spam

If the container exits again immediately after each restart attempt
(structurally broken: image gone, OOM-on-start, port conflict), the
loop would now restart it forever. Added ``_MAX_ESCALATIONS=10``: after
10 unsuccessful restart attempts, the loop logs one loud GIVING UP
message naming the office and goes silent for that office until either
(a) an operator brings the container back manually and the next health
check observes it ``running``, or (b) the daemon restarts. Recovery
clears the "given up" flag so the office re-enters normal monitoring.

### Manager prompt — System Invariants block

Added a "System Invariants — current platform truths (read EVERY
turn)" section to MANAGER_CLAUDE_MD (six numbered facts that the
Manager kept getting wrong in Task Briefs because old chat history
contradicted current behaviour):

1. ``register_script`` is safe to re-invoke — metadata-only on
   existing rows, source files never touched.
2. Workers edit script source directly via Write/Edit after
   bootstrap.
3. ``cubicle.notify_manager()`` payload caps at ~8 KB.
4. Blocked tasks never auto-unblock; bounce cap is 1.
5. Action requests dedupe by ``(source_task_id, request_type)``.
6. System agents are all Opus-tier.

The user found a recent Task Brief whose "Risks & Edge Cases" warned
"do NOT re-invoke register_script — it overwrites with boilerplate"
— factually wrong since v0.2.51, but the Manager kept pattern-
matching off the old v0.2.50 incident in chat history and propagating
the outdated warning into new briefs. The invariants block is the
"current truths" channel the Manager was missing.

## 0.2.53 — 2026-05-28

Post-v0.2.52 audit fixes — three parallel review agents identified
bugs introduced by yesterday's "demo-readiness" change set. The
critical fixes were silently breaking script-flow behaviour in
production.

### Critical — spawn-time `script_status:running` race

Sub-50ms scripts (`echo hello` smoke tests, dry-run no-op
invocations, cron health-checks) could race the spawn-time
`running` publish against the monitor's terminal event. Terminal
arrived first, then the late fire-and-forget `running` publish
landed, leaving the History row flipped back from `completed` to
`running` in the UI.

* Fixed in `src/scripts/script_runner.py` (Redis Streams path) and
  `src/_agent_image/_mcp_script_exec.py` (in-container HTTP path).
* The `running` publish is now AWAITED INLINE before spawning the
  monitor task — back-pressure cost on the spawn path is
  negligible (one Redis XADD / one localhost HTTP POST) compared
  with the visible UI bug.
* Removed the now-unused `_RUNNING_PUBLISH_TASKS` strong-ref set.

### Defence-in-depth — `_consecutive_failures` comment

Replaced the misleading "leak is bounded — acceptable" note on
`_consecutive_failures` in `src/scripts/cron_scheduler.py` with one
that explains WHY we don't prune. A naive "remove keys not in
current due set" would reset the backoff counter every tick for
crons whose `next_run_at` sits between ticks, defeating
`_BACKOFF_DISABLE_AT` entirely. The actual leak is bounded by
uuid4 cron-id uniqueness.

### Companion server-side fixes (not in this repo)

The other critical findings landed in the platform backend +
frontend monorepo (not vendored into the cbcl mirror): a missing
`await get_command_sender()` that broke EVERY in-container
`request_outbox_scan` call, a `broadcast_chat()` typo that
silently failed live broadcasts of script-trigger chat rows, an
SQL-error-message leak in the WS request handler, and a frontend
filter that hid the very rows v0.2.52 persisted to prevent
orphan-replies. Together with the daemon fixes in this release,
the script flow is end-to-end correct again.

## 0.2.52 — 2026-05-28

Pre-investor-demo comprehensive review pass. Three parallel audit
agents surfaced 30+ findings across MCP tools, AI prompts, and the
script subsystem. Ships the high-impact fixes.

### AI prompts (would have misled agents on screen)

* **ASD CLAUDE.md** "Updating an Existing Script" — was still
  warning that ``register_script`` after the initial bootstrap
  "risks overwriting the SDK". v0.2.51 truth: metadata-only on
  existing rows, source files never touched. Rewrote to make the
  safe-to-call contract explicit + documented the
  ``bootstrap_needs_retry`` informational flag.
* **ASD default prompt hard rule #1** — same outdated warning,
  same fix. Workers/ASDs would have been overly cautious about
  re-registering after a variable_schema change.
* **Shared agent CLAUDE.md task_id** — said "UUID is always
  safe, some tools accept readable_id". Since v0.2.51 every
  task-scoped tool accepts BOTH; rewrote to remove the
  unnecessary lookup-step bias.
* **Auditor script-evidence check** — referenced
  ``list_script_executions`` MCP tool (Manager-only). Auditor is
  a worker role and didn't have it. Rewrote to use Bash + ls/cat
  on the executions/ dir.

### MCP tool schema + description fixes

* **escalate_blocker** — added ``blocker_class`` as a REQUIRED
  enum (auth_failed, missing_credential, permission_denied,
  missing_data, ambiguous_spec, broken_dependency,
  external_outage, unknown). Manager Assistant routing needs
  this field; workers were stuffing it into ``blocker_summary``
  text and the MA had to regex-extract.
* **update_status** — documented the canonical 4-section blocked
  comment template inline.
* **execute_script** — rewrote description to make the
  fire-and-forget async model explicit (your session ends, Manager
  gets notified out-of-band, do NOT poll).
* **get_script_status** — rewrote as "rarely the right tool"
  caveat with explicit legitimate-use list.
* **task_id field descriptions** updated across all worker tools
  to say "UUID or readable_id".

### Script-subsystem reliability

* **In-container MCP fire-and-forget GC anchor** — both
  ``_monitor_script`` and the spawn-time
  ``_report_status_to_backend`` tasks now strong-ref'd. Without
  this, a fast-exiting script (~100ms) could race Python's GC
  and silently drop the spawn-time "running" event.
* **notify_manager system-message persistence** — new
  ``script_chat_trigger`` event published before
  ``ingest_script_message``. Backend EventDispatcher persists a
  ``role='system'`` chat row + broadcasts live. Without this,
  the Manager's reply appeared orphaned in chat history.

### Operator action

Standard upgrade:

    ssh root@<daemon-host>
    pipx install --force git+https://github.com/cbcl-ai/cbcl-cli.git@v0.2.52
    export PATH=/root/.local/bin:$PATH
    cbcl stop && sleep 3 && cbcl start --daemon

After upgrade:
* Manager + worker agents have accurate, current playbooks
  (no outdated warnings about register_script).
* Workers can correctly classify blockers via the new
  ``blocker_class`` enum.
* ``execute_script`` is no longer tight-polled by accident.
* ``notify_manager`` chat history shows the script's trigger
  message + the Manager's reply together (not orphaned).
* Fast scripts always record their "running" row.

977 unit tests + 148 backend tests pass.

## 0.2.51 — 2026-05-28

Two critical user-impacting bugs reported by the AI Manager from
production (2026-05-28T08:08–08:20Z).

### Bug 1: register_script destroyed user-edited source files

Backend's ``_handle_register_script`` called ``retry_bootstrap``
whenever an existing row had ``bootstrap_status != "complete"``,
which silently overwrote main.py / script.yaml / requirements.txt /
lib/__init__.py with template boilerplate. Trigger: a transient
daemon disconnect during initial creation left the row in
``failed`` status; the agent then ``Edit``ed the source into its
real form; a later ``register_script`` call (e.g. to update
``variable_schema``) triggered the destructive path; the next
cron tick ran the boilerplate and silently failed.

**Fix**: ``register_script`` is now METADATA-ONLY on existing
rows. NEVER rewrites source files. Response carries
``bootstrap_needs_retry: true`` + a loud ``warning`` field when
the existing row's bootstrap_status drifted, so callers can
explicitly invoke the destructive retry path (POST
``/scripts/{id}/bootstrap``, ``retry_bootstrap`` MCP tool, UI
BootstrapBanner) only if they really want to discard local
edits. Worker-MCP tool descriptor rewritten to make the contract
unambiguous.

### Bug 2: add_activity returned opaque "Failed to process request"

``action_add_activity`` did unguarded ``uuid.UUID(payload["task_id"])``
and crashed with ``ValueError`` on readable_ids. ``request_handler.py``
swallowed the error into ``"Failed to process request:
add_activity"``. The Manager couldn't post clarifying answers to
the worker, causing a bounce-loop on CO-001.T04.

**Fix**:
* ``action_add_activity`` now accepts BOTH UUID and readable_id
  (matches sister actions ``move_task`` / ``update_task`` /
  ``get_task_detail``). Unknown id returns
  ``{"error": "Task not found: <id>"}``.
* ``request_handler.py`` no longer swallows exceptions into a
  generic string. Surfaces ``{type(exc).__name__}: {exc}`` so
  future silent failures are debuggable from the AI Manager's
  view.

Regression tests added in ``backend/tests/test_action_requests.py``.

### Operator action

Standard upgrade:

    ssh root@<daemon-host>
    pipx install --force git+https://github.com/cbcl-ai/cbcl-cli.git@v0.2.51
    export PATH=/root/.local/bin:$PATH
    cbcl stop && sleep 3 && cbcl start --daemon

After upgrade:
* register_script on existing scripts will never destroy your
  source edits.
* add_activity calls with readable_id work end-to-end.
* Future MCP errors surface the real exception instead of
  "Failed to process request: <action>".

977 unit tests + 2 new regression tests pass.

## 0.2.50 — 2026-05-28

Comprehensive script subsystem fix. Addresses 9 user-facing bugs +
5 audit-pass items.

### User-facing fixes

* **Execution History live updates** — running rows now appear +
  update in real time. A 3-hour script no longer looks frozen.
* **Running executions persist to DB** — backend writes "running"
  rows at spawn, not just terminal. Disk row appears immediately.
* **Variables drawer read-back** — new GET ``/scripts/{sid}/bindings``
  endpoint forwards to daemon's ``script_get_bindings`` RPC. The
  drawer now shows the actual binding state (Custom / Office Secret
  + values) so users + agents can see what's bound. Without this,
  every drawer open could silently CLEAR bindings on Save.
* **Boot-time orphan-notify reaper** — startup scans every
  ``.outbox/*.json`` for files orphaned by a previous-process MCP
  crash and delivers them once script_runner is wired.
* **Cron retry-with-backoff** — broken cron (persistent dispatch
  error, e.g. DepsInstallError) now advances ``next_run_at`` so it
  doesn't hammer the daemon every 60s indefinitely. After 5
  consecutive failures, ERROR-level log surfaces the problem.
* **Cron duplicate-fire guard** — 30-second idempotency window via
  ``UPDATE ScriptCron WHERE last_run_at IS NULL OR < now - 30s``.
  Two daemons racing during token reassignment no longer both
  advance + both stamp.
* **Cron overlap-skip** — host-tracked running execution of the
  same script blocks the next cron tick (advances ``next_run_at``
  without spawning).
* **Manual UI trigger surfaces errors** — dispatch handler publishes
  ``script_status: failed`` on FileNotFoundError / MissingOfficeSecretError /
  corrupt-secrets-file / unexpected exception. UI sees the failure
  instead of silent click-and-nothing-happens.
* **Cron UI improvements** — NewCronDialog has client-side
  expression validation, a variable_overrides JSON textarea, and
  proper error-display in the dialog body.

### Audit-pass critical fixes

* **Secret literal redaction** — backend's GET ``/bindings`` now
  cross-references ``script.variable_schema`` and replaces
  ``value`` with ``__redacted__`` for is_secret=true entries BEFORE
  responding. Frontend redaction is now the second line of defence.
* **mark_cron_fired real idempotency** — initial implementation
  used ``IS DISTINCT FROM exec_id`` which only protected
  same-exec_id retries. Switched to a 30s time window which
  catches the actual two-daemons-race case.
* **Background task GC anchoring** — fire-and-forget tasks now
  held in ``set[asyncio.Task]`` containers with
  ``add_done_callback(discard)`` so Python's GC doesn't collect
  them mid-execution.
* **Type narrowing on bindings reads** — ``isPlainObject`` guard
  rules out arrays (``typeof [] === "object"``).
* **One-shot init on Variables drawer** — background TanStack
  refetches no longer wipe a user's unsaved literal-input text.

### Architecture decision

Audit Agent 1 suggested collapsing the in-container subprocess
spawn path into the host-delegated path. **Rejected** because the
in-container path is the only one that works without
``TOOL_PROXY_URL`` reachable — eliminating it would REMOVE the
graceful-degradation property the non-secret ``execute_script``
case currently has.

### Operator action

Standard upgrade:

    ssh root@<daemon-host>
    pipx install --force git+https://github.com/cbcl-ai/cbcl-cli.git@v0.2.50
    export PATH=/root/.local/bin:$PATH
    cbcl stop && sleep 3 && cbcl start --daemon

After upgrade:
* Existing executions on disk get backfilled to the History tab
  on next view.
* Orphan notify-*.json files get delivered to the Manager on next
  daemon restart.
* Cron schedules with persistent dispatch failures no longer
  thrash every 60s.
* Variables drawer now shows actual bound state instead of blank
  inputs (no more accidental binding-clear on Save).

977 unit tests pass.

## 0.2.49 — 2026-05-28

Proper-architecture replacement for the 0.2.48 ``global_sweep``
band-aid. The agent's ``script_execute`` MCP tool now natively
records execution history + delivers ``notify_manager`` via the
same reliable backend path every other tool action uses.

### What changed

The in-container MCP's two silent-failure delivery functions
(``_report_status_to_backend`` and ``_trigger_outbox_scan``) are
now thin wrappers around ``_call_backend`` — the same helper the
MCP uses for ``create_task`` / ``move_task`` / etc. ``_call_backend``
tries the local tool proxy first, falls back to direct backend
HTTP with 3 retries when the proxy is unreachable.

Backend gained two new tool-call actions:

* ``record_script_execution`` — wraps the existing
  ``handle_script_status`` handler so DB writes + board broadcasts
  go through the same code path the daemon's
  ``notify_completion`` uses.
* ``request_outbox_scan`` — forwards a ``scan_outbox`` message
  over the connector WS to the daemon, which calls
  ``script_runner.scan_outbox_for(name)``. Same target function
  the old tool-proxy ``/outbox-scan`` endpoint called.

The 30s ``global_sweep`` background loop from 0.2.48 is REMOVED.
The primary path is now reliable enough that a fallback sweep
would only ever find ``.reported`` markers and do nothing.

### Before vs after

Before (0.2.48):
* Primary path: in-container MCP → bare ``aiohttp.post(TOOL_PROXY_URL)``.
  Single point of failure with silent error swallow.
* Safety net: 30s ``global_sweep`` walking workspace dirs.

After (0.2.49):
* Primary path: in-container MCP → ``_call_backend`` → proxy
  (first try) OR direct backend HTTP (fallback, 3 retries) →
  backend dispatcher → daemon command (for outbox) or DB write
  (for execution status).
* No band-aid sweep. Same retry + fallback infrastructure as
  every other tool call.

### Operator action

Standard upgrade:

    ssh root@<daemon-host>
    pipx install --force git+https://github.com/cbcl-ai/cbcl-cli.git@v0.2.49
    export PATH=/root/.local/bin:$PATH
    cbcl stop && sleep 3 && cbcl start --daemon

After upgrade, AI-test runs are recorded in the Execution History
tab in real time AND ``notify_manager`` drops reach the Manager
the moment they're written — both via the primary MCP path's
proxy → direct-backend fallback, no 30s wait.

## 0.2.48 — 2026-05-27

Three user-reported bugs, fixed together.

### Bug 1 + 2 — empty Execution History + lost ``notify_manager`` drops

User: "The news-intelligence-scanner script had 4 AI-test
executions but Execution History is empty." AND "I had a script
that called ``cubicle.notify_manager(...)`` but nothing changed —
no record in the Manager Notifications popup, no messages from
the AI Manager."

Root cause: the agent-triggered ``execute_script`` MCP path (used
when an AI agent tests a script during creation) reports execution
status and triggers outbox scans via an HTTP POST from the agent
container to the host tool proxy. EVERY failure mode of that POST
is silently swallowed — missing ``TOOL_PROXY_URL`` env, UFW
blocking docker0 → host, transport error, non-200 response. The
host-side ``monitor_all`` loop only scans for scripts with a
TRACKED host-side execution; agent-triggered runs have none, so
the existing safety net never kicks in for them.

**Fix**: New ``ScriptRunner.global_sweep()`` background loop that
runs every 30s INDEPENDENTLY of host-active executions. Walks
``/workspace/.scripts/*/.outbox/`` and ``executions/*/status.json``
for EVERY script:

* Outbox: delivers any unprocessed ``cubicle.notify_manager()``
  JSON drops via the existing ``outbox_watcher.scan_and_dispatch``
  path. Files move to ``.outbox/.processed/`` so the Manager
  Notifications popup populates immediately. Manager receives the
  message as a ``[Script: <name>]`` prefixed chat.
* Status: re-publishes any terminal ``status.json`` rows that
  haven't been reported to the backend yet (sentinel marker
  ``.reported`` next to the status file prevents re-publishing
  on every sweep). Goes through the existing ``notify_completion``
  → router → backend dispatcher → DB chain — no backend changes
  needed.

The sweep is silent when nothing's pending (no log spam). Bounded
cost (~30s tick × sub-100ms disk I/O per tick on typical offices).

### Bug 3 — phantom "Using Bash" / "Using Read" comments in Discussion tab

User: "In the task details popup, in the Discussion tab I see AI
agent comments like 'Using Bash' or 'Using Read', but after
refreshing the page these comments disappear. I don't understand
what they are or why they're in the Discussion feed."

Root cause: the frontend's WS handler for ``activity_added`` used
a prefix-match ``setQueriesData`` that wrote every new activity
into ALL category caches under
``["offices", oid, "tasks", tid, "activities", *]`` — including
``discussion``, ``event``, AND ``all``. The backend's REST endpoint
filters by category (Discussion only shows comment-like types,
Events only shows system lifecycle types). ``tool_run`` events
("Using Bash" / "Using Read") leaked into Discussion via the live
WS handler then vanished on refresh when the REST refetch filtered
them out.

**Fix**: Mirror the backend's ``COMMENT_EVENT_TYPES`` +
``SYSTEM_EVENT_TYPES`` sets in the WS handler. Route each new row
only to the matching category cache (plus ``all``). Consistent
both ways now — what you see live is what the refetch returns.

### Operator action

Standard upgrade:

    ssh root@<daemon-host>
    pipx install --force git+https://github.com/cbcl-ai/cbcl-cli.git@v0.2.48
    export PATH=/root/.local/bin:$PATH
    cbcl stop && sleep 3 && cbcl start --daemon

After upgrade, every script's outbox + execution-status rows get
swept and reported within 30s regardless of whether the in-
container HTTP POST round-trip works. 977 unit tests pass.

## 0.2.47 — 2026-05-27

Post-0.2.46 audit pass — three classes of correctness fix across
the wizard pipeline. No API surface changes.

### Daemon (setup_generator.py)

* **Leaked task on Phase 1/2 exception** — when ``instructions_task``
  or ``roster_task`` raised, the surviving task was never awaited
  or cancelled. The orphan ``docker exec ... claude --print`` kept
  burning Claude API spend for up to 6 min on a doomed run.
  Explicit cancel + ``asyncio.gather`` in the ``finally`` block.
* **Orphan agent tasks on first-agent-failure** — the fail-fast
  path cancelled only the skill tasks. Still-running agent siblings
  ran to completion past the failure surfacing. Now cancels BOTH
  agent and skill pools on first agent failure.
* **``agent_task_set`` closure brittleness** — moved both sentinel
  set definitions ABOVE the ``_safe_await`` closure that references
  them.
* **``agents = None`` crash** — model occasionally emits
  ``{"agents": null}``; added ``or []`` guard.
* **Roster preview enriched** — added ``skill_template_ids`` +
  ``skill_names`` so the frontend "What the AI proposed" panel
  shows what each agent will be equipped with during the 5-min
  Phase 3+4 wait.
* **Heartbeat cancel awaited** — fire-and-forget cancel could let
  a mid-publish heartbeat emit a stale step_number=1 frame AFTER
  step 2 advanced, briefly rewinding the progress bar.

### Backend (event_dispatcher.py)

* Narrowed exception clause in the payload-merge path (``TypeError``
  added).
* Documented the serial-per-``request_id`` concurrency invariant.

### Frontend (GeneratingStep.tsx + api/setup.ts)

* **Agents tile flicker fix** — when an "Authoring skill" message
  landed mid-pool, the agents tile demoted to "pending" even
  though agents were still running. Tracks ``agentsDone`` /
  ``skillsDone`` separately so each tile stays active until its
  own counter completes.
* **``inParallelBlock`` simplification** — redundant
  ``stepNumber >= 2 && stepNumber < 3`` collapsed to
  ``stepNumber === 2``.
* **Render-phase setState guard tightened** — added ``liveMessage &&``
  so the first render doesn't fire a redundant ``setPrevMessage("")``.
* **Type tightening** — ``payload?: GenerationPayload | null`` →
  ``payload?: GenerationPayload``.
* **TileState declaration** — moved ABOVE first use.

### Operator action

Standard upgrade:

    ssh root@<daemon-host>
    pipx install --force git+https://github.com/cbcl-ai/cbcl-cli.git@v0.2.47
    export PATH=/root/.local/bin:$PATH
    cbcl stop && sleep 3 && cbcl start --daemon

After upgrade, the wizard's parallel pipeline is correctness-clean
under failure paths and the UI no longer flickers between agent /
skill tiles mid-pool. 977 unit tests pass.

## 0.2.46 — 2026-05-27

HUGE wizard refactor: parallel phases, concurrency cap, live
content preview. User report: a 6-agent office took 24 min with
sequential-ish timings and a spinner-only UI.

### Daemon

* **Skip Phase 0 vision regen** when the analyzer's vision exists
  (saves ~60s every run).
* **Phase 1 (instructions) ‖ Phase 2 (roster) — PARALLEL** via
  ``asyncio.wait(FIRST_COMPLETED)``. Wall-clock collapses from
  (instructions + roster) ≈ 12 min to max(...) ≈ 8 min.
* **Phase 3+4 parallel pool: concurrency cap** —
  ``CBCL_WIZARD_PARALLEL_CAP`` (default 6) ``asyncio.Semaphore``.
  Critical fix: firing 24 concurrent ``docker exec`` calls overran
  the Anthropic API tier and silently serialized the pool. With
  cap 6 we stay under the tier limit so parallelism actually
  shows in the wall-clock.
* **Heartbeat progress emitter** — fires "Still ... ({elapsed_s}s)"
  every 12-15s during long calls so the user sees live signal.
* **Live payload events** — every progress event now ships an
  optional ``payload`` dict (``vision``, ``instructions``,
  ``agents`` slug preview, ``proposed_workstreams``) the frontend
  renders progressively.

### Backend (event_dispatcher.py + setup_router.py)

* Added ``payload`` to ``GenerationStatusResponse``.
* Merges payload fields across consecutive progress events so the
  cached status carries the full accumulated snapshot.

### Frontend (GeneratingStep.tsx + api/setup.ts)

* New ``RosterPreviewAgent`` + ``GenerationPayload`` types.
* Complete UI rewrite — 5-tile strip + LIVE CONTENT PANEL that
  fills in as payload events arrive (vision / roster / instructions
  / workstreams / skills progress cards).
* Live ELAPSED TIMER so the user knows how long they've been
  waiting.

### Expected wall-clock

For a 6-agent / 18-skill office:
* Before: ~24 min (sequential vision + instructions + roster +
  serialized agents + serialized skills)
* After: ~8-10 min (vision skipped + max(instructions, roster) +
  capped-parallel pool)

### Operator action

    pipx install --force git+https://github.com/cbcl-ai/cbcl-cli.git@v0.2.47
    cbcl stop && sleep 3 && cbcl start --daemon

(0.2.47 supersedes 0.2.46 — install 0.2.47 directly.)

## 0.2.45 — 2026-05-27

Three independent user complaints, fixed together: cohesion review
removed, agent + skill phases parallelised (initial implementation),
office deletion now actually deletes everything.

### Daemon

* Phase-5 cohesion pass deleted — was a read-only "AI noticed
  these gaps" panel the user couldn't act on. Pure latency tax
  (~2:20 min on a 7-agent office).
* Phase 3 + Phase 4 merged into one ``asyncio.as_completed`` loop.
  Wall-clock collapses from (agent_phase + skill_phase) to
  max(longest agent call, longest skill call). NOTE: this still
  hit silent serialization without the concurrency cap added in
  0.2.46.
* Office deletion: ``await cron_scheduler.stop()`` (was missing
  ``await`` — the user-reported "cron jobs still showing"
  complaint), Manager subprocess clean shutdown, comprehensive
  Redis cleanup (per-agent queues, sessions, streams, agent feeds,
  task locks), office-secrets host-file cleanup.

### Bonus: simplify-pass on the 0.2.0→0.2.2 work

* ``ConnectingStep.tsx`` — ``companyId`` guard on header "Change"
  button.
* ``CommunicatorTokenPicker.tsx`` — stripped history reference
  from comment.
* ``agent_supervisor.py`` — extracted ``_resolve_agent_argv()`` +
  ``_build_subprocess_env()`` helpers to dedupe worker/manager
  spawn paths.

### Opus-4.7-everywhere policy

Per user directive: all system agents + system prompts run on the
latest Opus tier (``claude-opus-4-7``). DB defaults, Pydantic
schemas, wizard-generated agent defaults, and the ROSTER_PROMPT
updated to require uniform Opus across the entire orchestration
surface.

## 0.2.44 — 2026-05-27

Audit-pass hardening for the history-backfill timing window.

### `_run_history_backfill` — WS-ready poll loop

Replaced the fixed-15s ``asyncio.sleep`` startup wait with a 60s poll
loop (0.5s ticks) that exits the moment the WS is ready OR the 60s
budget runs out. A slow-reconnecting daemon (cold network, backend
warming up) would otherwise miss the connect window and publish
backfill events to a disconnected router — every status_event drops
silently and the user sees no rows.

Falls back to a clear WARNING + early-return if the WS is still
down after 60s; next daemon restart retries automatically.

### Operator action

Standard upgrade:

    ssh root@<daemon-host>
    pipx install --force git+https://github.com/cbcl-ai/cbcl-cli.git@v0.2.44
    export PATH=/root/.local/bin:$PATH
    cbcl stop && sleep 3 && cbcl start --daemon

After upgrade, the history backfill is reliable across slow-network
startups. 963 unit tests pass.

## 0.2.43 — 2026-05-27

Adds the "Improve with AI" iteration path to the setup wizard's
Review step. User types a free-text adjustment ("add a content
strategist", "make the writers more formal"), the AI applies it to
the current draft, and the Review preview refreshes in place.

### Daemon

* New ``improve_office_config()`` in ``setup_generator.py`` — one
  Claude call (Sonnet by default, see ``CBCL_GENERATION_MODEL``)
  that takes the current draft + the user's directive and returns
  the revised config. Same ``setup_generation_complete`` event
  shape the frontend already polls, so no new transport plumbing.
* New ``IMPROVE_CONFIG_PROMPT`` designed for iteration: catalogs
  the common directive patterns (add / remove / adjust agent, add
  / remove / adjust skill, workstream changes, tone sweeps,
  combined) and explicit rules (don't regenerate, don't change
  vision, don't invent template IDs).
* New WS handler ``improve_office_config`` routed via the
  ``_handlers/_setup.run_improve_office_config`` bridge.

### Operator action

Standard upgrade:

    ssh root@<daemon-host>
    pipx install --force git+https://github.com/cbcl-ai/cbcl-cli.git@v0.2.43
    export PATH=/root/.local/bin:$PATH
    cbcl stop && sleep 3 && cbcl start --daemon

After upgrade, the new ``Improve with AI`` button on the wizard's
Review step is functional. Backend endpoint is in the matching
platform release.

## 0.2.42 — 2026-05-27

Two more safety nets for ``ScriptSyncer`` cleanup. The 0.2.41
sentinel guard stops cross-office wipes, but two adjacent failure
modes could still nuke script source files irrecoverably.

### Empty-sync sanity check

If the backend returns ZERO scripts (transient auth gap, network
hiccup, malformed payload) on a sync where the disk has scripts,
the OLD cleanup loop would wipe everything. Now we detect this
case, refuse cleanup, and log a clear WARNING. A genuine "user
deleted all scripts" case requires a daemon restart to re-trigger.

### Archive-before-delete

Stale-script removals now MOVE the directory to
``.scripts/.removed_by_sync/<UTC-timestamp>/<script-name>/``
instead of deleting it outright. Script source files
(``main.py``, ``script.yaml``, ``lib/``, ``requirements.txt``,
``README.md``) are irreplaceable from the backend — only metadata
lives in the DB. A mistaken cleanup decision is now recoverable
with a single ``mv`` on the host.

### Operator action

Standard upgrade:

    ssh root@<daemon-host>
    pipx install --force git+https://github.com/cbcl-ai/cbcl-cli.git@v0.2.42
    export PATH=/root/.local/bin:$PATH
    cbcl stop && sleep 3 && cbcl start --daemon

To recover scripts that the 0.2.41-or-earlier daemon already
deleted:

* Check ``~/.cubicle/workspaces/<slug>/.scripts/.removed_by_sync/``
  — empty if the loss predates 0.2.42.
* For losses BEFORE 0.2.42 ship, the source files are gone. Click
  "Retry bootstrap" in the script's detail page in the UI — the
  backend re-creates the boilerplate scaffold from the stored
  template. Any agent-authored customisations on top of the
  scaffold are unfortunately lost; an agent can be tasked to
  re-author them.

## 0.2.41 — 2026-05-27

**CRITICAL fix** — script-sync was deleting another office's scripts
when two offices shared a workspace slug.

### The bug

User reported: "Failed to load files — Could not read
.scripts/tech-insights-collector." Investigation found the daemon
log line ``Removed stale script directory: tech-insights-collector``
on startup. Two offices named "SMM & Copywriting" both slugified to
``smm-copywriting`` and so shared
``~/.cubicle/workspaces/smm-copywriting/``. Each office's
``sync_scripts`` pass treated the OTHER office's scripts as stale
and wiped them.

### The fix (daemon side)

``ScriptSyncer.sync_scripts`` writes a sentinel file
``.scripts/.synced_by_office_id`` on first sync of a workspace.
Subsequent syncs from a DIFFERENT office see the mismatched
sentinel and SKIP the destructive cleanup step (idempotent
upsert of own scripts still runs). Logs a clear warning so the
operator knows the workspace is shared.

Pair with the backend's new slug-uniqueness guard (in 0.2.41
backend release) — that one rejects NEW offices that would
collide; the sentinel guard saves existing offices that are
already colliding.

### Operator action

Standard upgrade:

    ssh root@<daemon-host>
    pipx install --force git+https://github.com/cbcl-ai/cbcl-cli.git@v0.2.41
    export PATH=/root/.local/bin:$PATH
    cbcl stop && sleep 3 && cbcl start --daemon

After upgrade, look for ``Workspace .../.scripts is shared between
offices ... and ...`` warnings in the log for any colliding offices
— rename one of them in the UI to fix.

## 0.2.40 — 2026-05-27

**CRITICAL hotfix** — daemon refused to start on 0.2.39 with
``ModuleNotFoundError: No module named 'src._handlers._agent_feed'``.

### The bug

Wave 13 (cbcl 0.2.36 → 0.2.37 phase) extracted ``handlers.py``'s
``_push_agent_feed`` helper into a new sibling module
``src/_handlers/_agent_feed.py``. The monorepo commit (``39b458d``)
included both the new file AND the updated ``handlers.py`` import,
but the GitHub-mirror sync at the time copied only the changed
``handlers.py`` — the new sibling file was missed because the sync
script copies only files listed in each release's diff.

The mirror's ``handlers.py`` from cbcl 0.2.37 / 0.2.38 / 0.2.39
imports ``from src._handlers._agent_feed import push_agent_feed as
_push_agent_feed_impl`` but the mirror never had the file. Every
office tried to connect and crashed at the import:

    Failed to connect office 'Development': No module named
    'src._handlers._agent_feed'

ALL offices failed; the daemon stayed up but served zero traffic.

### The fix

Bundled the missing ``src/_handlers/_agent_feed.py`` (unchanged
since wave 13). No code change required — just the sync gap.

### Operator action

Standard upgrade:

    ssh root@<daemon-host>
    pipx install --force git+https://github.com/cbcl-ai/cbcl-cli.git@v0.2.40
    export PATH=/root/.local/bin:$PATH
    cbcl stop && sleep 3 && cbcl start --daemon

After upgrade the daemon log should show
``Office '<name>' (...) connected (process model)`` for every office
instead of the import error.

## 0.2.39 — 2026-05-27

Two daemon-side fixes (the backend connector delete fix lives in the
platform, not in cbcl).

### Office wizard 4x faster

The wizard's "Generating configuration" step was taking 15-20 min for
a simple 4-agent office (was a few minutes baseline). Root cause: the
platform-wide Opus-thinking rollout (0.2.30+) made every fallback
model default to ``claude-opus-4-7``. Live agent reasoning benefits
from Opus-thinking, but the wizard fires ~6 sequential generation
calls (vision → instructions → roster → agent details → skill
playbooks → cohesion review) and each Opus-thinking call adds 60-120s.

Fix: new ``FALLBACK_WIZARD_MODEL = "claude-sonnet-4-6"`` constant.
``_setup_cli.py`` defaults wizard generation to Sonnet.
``CBCL_GENERATION_MODEL`` env var still wins for operators who want
to force Opus per-install. Expected speedup: 8-10 min per office
creation.

### Script Execution History backfill on startup

Historical script runs (anything that ran BEFORE 0.2.38's in-container
reporter shipped) never made it to the backend DB. They sit on the
daemon's workspace volume but in split-host production the backend
has no fs access, so the disk-scan fallback in ``list_executions``
is a no-op there. Users opening the Execution History panel for an
older script saw an empty list.

Fix: new ``_run_history_backfill`` async task fires from
``init_office_process_model`` after the WS transport is created.
Sleeps 15s for the WS to connect, then scans
``{workspace}/.scripts/*/executions/*/status.json`` and POSTs every
terminal-state row as a ``script_status`` event. Idempotent via the
backend's ``(script_id, execution_id)`` upsert — re-running on
daemon restart is safe. Best-effort: WS not connected → logs +
drops; next daemon restart retries.

### Operator action

Standard upgrade:

    ssh root@<daemon-host>
    pipx install --force git+https://github.com/cbcl-ai/cbcl-cli.git@v0.2.39
    export PATH=/root/.local/bin:$PATH
    cbcl stop && sleep 3 && cbcl start --daemon

The next office connect will fire the backfill and historical runs
will start showing up in the Execution History panel within ~15-30s.

## 0.2.38 — 2026-05-27

**CRITICAL fix** — ``cubicle.notify_manager()`` callbacks from agent-
triggered in-container scripts were never delivered to the Manager.

### The bug

The Manager reported in chat: "the notify_manager outbox payload
landed (the audit confirms it) — apparently the synthetic chat-turn
delivery skipped me, but the scope-completion turn caught me up."

``outbox_watcher.scan_and_dispatch`` is invoked ONLY from the
host-side monitor loop in ``script_execution.py``. That loop tracks
scripts spawned via the host-side ``ScriptRunner`` (UI runs, cron
runs, office-secret runs). The in-container MCP path
(``_mcp_script_exec._execute_script`` — the common case for
agent-triggered runs WITHOUT office-secret refs) writes
``.outbox/notify-*.json`` on the bind-mounted workspace but the
host-side monitor knows nothing about that execution. The scanner
never runs; the notify file sits in ``.outbox/`` forever.

### The fix

Three pieces:

* ``script_runner.py`` — new public
  ``ScriptRunner.scan_outbox_for(script_name)`` method.
* ``tool_proxy_server.py`` — new ``POST /outbox-scan`` endpoint
  (auth'd with the same bearer token as ``/tool-call``).
* ``_agent_image/_mcp_script_exec.py`` — new
  ``_trigger_outbox_scan`` helper called from ``_monitor_script``
  after the subprocess exits. Best-effort POST to the proxy.

### Operator action

Standard upgrade:

    ssh root@<daemon-host>
    pipx install --force git+https://github.com/cbcl-ai/cbcl-cli.git@v0.2.38
    export PATH=/root/.local/bin:$PATH
    cbcl stop && sleep 3 && cbcl start --daemon

963 unit tests pass.

## 0.2.37 — 2026-05-27

**Fix** — empty Execution History panel for agent-triggered scripts.

### The bug

In split-host production (backend on cbcl-v2, daemon on cbcl-stg) the
``ScriptExecution`` DB rows for agent-triggered runs never appeared.
Scripts run via the in-container MCP path (the common case: agent
calls ``execute_script`` for a script with no office-secret refs)
wrote ``status.json`` on the daemon-host workspace volume but NEVER
published a ``script_status`` event back to the backend. The backend
has no filesystem access to the daemon's volume, so the disk-scan
fallback in ``list_executions`` is a no-op there — the DB never
learned an execution happened, the History panel stayed blank.

The host-side ``ScriptRunner`` (UI manual runs, cron runs,
office-secret runs) already publishes ``script_status`` via WS, so
those paths worked. The gap was specifically the in-container MCP
runner.

### The fix

Two coordinated changes:

* ``tool_proxy_server.py`` — new ``POST /script-status`` endpoint.
  Auth'd with the same bearer token as ``/tool-call``. Forwards the
  payload to the backend via the proxy's WebSocket so the existing
  ``handle_script_status`` handler can write the row via
  ``store_script_execution`` on terminal states.
* ``_agent_image/_mcp_script_exec.py`` — new helper
  ``_report_status_to_backend`` + a single new call from
  ``_monitor_script`` after the subprocess exits. Best-effort:
  a WS disconnection logs and drops the event but the row still
  lives on disk for any future backfill path to pick up.

### Operator action

Standard pipx-upgrade + daemon restart:

    ssh root@<daemon-host>
    pipx install --force git+https://github.com/cbcl-ai/cbcl-cli.git@v0.2.37
    export PATH=/root/.local/bin:$PATH
    cbcl stop && sleep 3 && cbcl start --daemon

The next ``cbcl start`` will detect the
``_mcp_script_exec.py`` hash change and rebuild the agent image
automatically. Existing office containers will be recreated on
their next office-start with the fixed image.

963 unit tests pass (2 skipped — pre-existing ssh-keygen env gap).

## 0.2.36 — 2026-05-27

AI prompt + MCP tool-descriptor audit fixes. Four-area parallel
audit (packaging / AI prompts / AI mechanics / MCP tool surface)
flagged 4 high-confidence issues — all addressed here.

### Fixes

1. **Manager CLAUDE.md misleading 2-rework rule.** Two spots
   ("After 2 rework cycles the reviewer auto-approves") directly
   contradicted ``worker_prompt.py`` and the Auditor CLAUDE.md,
   both of which say reviewers at ``rework_count >= 2`` must
   ESCALATE via ``escalate_blocker`` (category ``user_input``).
   Silent auto-approval of failing work was never the intent;
   the Manager could otherwise tell users "no worries, after 2
   reworks it auto-approves" when the reviewer is actually
   supposed to escalate. Updated both occurrences.

2. **Worker ``move_task`` schema missing ``enum``.** Claude could
   attempt invalid moves (``archived``, ``review``, ``backlog``)
   and only fail after a backend round-trip. Added
   ``enum: ["done", "ready", "blocked", "in_progress"]`` to lock
   the surface to what a non-executor (reviewer / MA / Board
   Operator) can drive.

3. **``update_status`` description silent on triage-mode block.**
   When the Manager Assistant is dispatched to a still-blocked
   task (TASK_MODE=triage), the MCP server refuses
   ``update_status`` to prevent circumventing the bounce cap.
   Added a TRIAGE MODE EXCEPTION paragraph naming the three
   legitimate triage resolution paths (B / C / D) and the
   bounce-cap rationale.

4. **``decide_action_request`` description silent on dedup.**
   ``setup_office_secret`` and a few other request types
   deduplicate at propose-time on ``(office_id, payload key
   fields)``. The Manager could otherwise try to reject one of N
   "duplicates" and inadvertently close a request multiple
   workers are waiting on. Added DEDUP paragraph.

### Verified-OK (audit false positives)

* Wave-11 packaging closure (0.2.35 fix held).
* Designated-reviewer dispatch is correctly implemented in the
  daemon (``_tasks.py:111`` enqueues review tasks to the
  ``reviewer`` agent's queue, not just to the Manager Assistant
  fallback).
* MA cooldown clear-on-transition-out-of-blocked is correctly
  implemented in the backend (``board.py:176`` resets
  ``last_blocked_triage_at`` whenever status leaves ``blocked``).

963 unit tests pass (2 skipped — pre-existing ssh-keygen env gap
on the slim test image, unrelated).

## 0.2.35 — 2026-05-27

**CRITICAL hotfix** — the Wave-11 mcp_tool_server decomposition (0.2.32)
extracted ``_mcp_backend.py`` and ``_mcp_script_exec.py`` as sibling
modules but only updated the entrypoint. ``Dockerfile.agent``'s ``COPY``
block still bundled just ``mcp_tool_server.py`` + the ``_mcp/`` package,
so every spawned MCP server inside an agent container crashed at import
time:

    File "/opt/cubicle/mcp_tool_server.py", line 109, in <module>
        from _mcp_script_exec import (  # noqa: E402
    ModuleNotFoundError: No module named '_mcp_script_exec'

The crash kills the JSON-RPC handshake before ``tools/list`` runs, so
Claude CLI sees the cubicle-tools MCP server as "disconnected" and the
AI Manager surfaces "I can't act right now — the cubicle-tools MCP
server is currently disconnected on my end" in chat. Every board action
(``get_task_detail``, ``update_task``, ``add_activity``, ``move_task``,
``retry_blocked_task``, ``archive_task``) became unavailable. Worker
script tools (``execute_script`` / ``script_get_status``) were also
gone.

### The fix

1. ``Dockerfile.agent`` — added ``COPY`` lines for both missing sibling
   modules.
2. ``container_manager._compute_mcp_server_hash`` — added both files to
   the image-cache-invalidation hash. Without this, a future change to
   ``_mcp_script_exec.py`` would ship a stale agent image that imports
   the OLD copy at runtime.

The Dockerfile change ALSO bumps the cache hash, so the next
``cbcl start`` on 0.2.35 automatically rebuilds the agent image with
the correct file map. No ``--force-rebuild-image`` flag needed.

### Operator action — upgrade required

    ssh root@<daemon-host>
    pipx install --force git+https://github.com/cbcl-ai/cbcl-cli.git@v0.2.35
    export PATH=/root/.local/bin:$PATH
    cbcl stop && sleep 3 && cbcl start --daemon

The daemon will detect the hash change and rebuild the agent image
automatically. Existing office containers will be recreated on next
office-start with the fixed image.

## 0.2.34 — 2026-05-27

Pure refactor — no behaviour changes, no user action needed. Wave 12
of the decomposition program (refactoring-plan.md target #3).

### manager_controller.py decomposition (1422 → 893 lines)

The third class-coupled big file in the daemon (after wave-10
agent_worker and wave-11 mcp_tool_server) split into two focused
sibling modules using the method-extraction-with-owner-param pattern.

* ``_manager_events.py`` (253 lines) — Manager-subprocess streaming
  event handlers: ``handle_manager_event`` dispatcher,
  ``on_response_chunk`` / ``on_response_final`` text streaming,
  ``on_activity`` tool-use pulse, ``on_progress``, ``on_error``.
* ``_manager_action_requests.py`` (421 lines) — synthetic-chat-turn
  ingest paths for script + scope + action-request events:
  ``ingest_script_message``, ``ingest_scope_completed``,
  ``ingest_action_request_decided``,
  ``ingest_action_request_auto_decide``, plus the shared
  ``build_script_context_data`` helper.
* ``manager_controller.py`` (residual) — lifecycle, chat dispatch,
  publish helpers, ``is_busy``, ``cancel_current_turn``,
  ``handle_switch_context``. Adapter methods route every extracted
  method through the class so test monkeypatches keep working.

Naming convention matches wave-10 / wave-11: extracted functions
drop the leading underscore (``handle_manager_event``,
``ingest_script_message``, …) to match
``run_sdk_session`` / ``handle_chat_message`` /
``build_mcp_config``. Class adapters keep the underscore-prefixed
name to signal internal-private.

963 unit tests pass (2 skipped — pre-existing ssh-keygen env gap on
the slim test image, unrelated).

## 0.2.33 — 2026-05-27

CRITICAL hotfix — every worker task dispatch failed on 0.2.30 / 0.2.31 /
0.2.32 because the Wave 10 ``agent_worker.py`` decomposition shipped
with a broken import name. **Upgrade immediately** if any office is
running tasks on this daemon.

### The bug

``communicator/src/_agent_worker_task.py`` defined the extracted
function as ``async def _run_sdk_session(self, ...)`` (underscore
prefix + ``self`` param), but the adapter at
``agent_worker.py::AgentWorker._run_sdk_session`` imports it as
``from ._agent_worker_task import run_sdk_session`` (no underscore).
Every worker process spawn — Manager Assistant triage, Automation
Script Developer, every custom worker — died at task pickup with:

    cannot import name 'run_sdk_session' from 'src._agent_worker_task'

Visible to users as: "agent picked up task" event posts to the
board, then the task sits forever with no further events. The
daemon's worker subprocess crashes silently after the ImportError;
the dispatcher logs ``Worker <name> error (fatal=False)`` and
re-queues the task, which re-fires the same crash.

### The fix

Three coordinated changes inside ``_agent_worker_task.py``:

1. Rename the function from ``_run_sdk_session`` to
   ``run_sdk_session`` to match the import name the adapter uses
   AND the naming convention of the three sibling modules
   (``handle_chat_message``, ``run_manager_session``,
   ``build_mcp_config`` — all without leading underscores).
2. Rename the function's first parameter from ``self`` to
   ``worker`` so the body's existing ``worker.x`` references
   resolve (the function was a free-function-with-owner-param
   per the refactoring-plan.md pattern — the ``self`` name was a
   leftover from the pre-extraction class method).
3. Add five missing imports for the constants the function body
   references: ``_MAX_SESSION_ATTEMPTS``,
   ``_MAX_SESSION_WALLCLOCK_SECONDS``, ``_MAX_SYSTEM_PROMPT_SIZE``,
   ``_ERROR_PREVIEW_LENGTH``, ``_ESCALATION_ORIGINAL_LENGTH``.
   These live in ``agent_worker.py``; the extracted module needs
   them at module scope. Without these imports the second-stage
   failure (after the import-name fix) would have been NameError
   on first task execution.

### Verification

* 963 unit tests pass (2 skipped — pre-existing ssh-keygen env gap).
* Module surface check: ``run_sdk_session`` is importable from
  ``_agent_worker_task`` with the right name.
* Smoke confirmed by replaying the daemon log path the original
  bug exposed (``TO-001.T01`` dispatch fails on 0.2.32, succeeds
  on this version).

### Operator action

    ssh root@<daemon-host>
    pipx install --force git+https://github.com/cbcl-ai/cbcl-cli.git@v0.2.33
    export PATH=/root/.local/bin:$PATH
    cbcl stop && sleep 3 && cbcl start --daemon

## 0.2.32 — 2026-05-27

Pure refactor — no behaviour changes, no user action needed. Wave 11
of the decomposition program (refactoring-plan.md target #2).

### mcp_tool_server.py decomposition (1427 → 571 lines)

The in-container MCP tool server split into two focused sibling
modules using the re-export-from-parent pattern.

* ``_mcp_backend.py`` (118 lines) — HTTP layer for every backend-
  backed tool call. Owns the singleton aiohttp session, the
  proxy-first/direct-fallback path, and the 3x retry.
* ``_mcp_script_exec.py`` (822 lines) — local script-execution
  path (the heaviest concern in the original file). Manifest
  parse → env build → docker-internal subprocess spawn, plus
  the fire-and-forget completion monitor. Includes
  ``compute_output_dir`` and ``_RESERVED_ENV_NAMES`` since the
  only runtime caller is ``_execute_script``.
* ``mcp_tool_server.py`` (residual) — JSON-RPC dispatch
  (``MCPServer`` class), General-Chat / triage / executor
  guards, tool filtering, ``main()``. Re-exports every
  extracted name so test surfaces that load the parent via
  importlib keep finding them.

Each sibling reads its own ``os.environ`` config at import time;
values match across modules because both import once per agent
process and the env is fixed at process start. The
``_http_session`` singleton moves with the functions that own it
(only ``_mcp_backend`` ever creates / closes it).

963 communicator unit tests pass (2 skipped — pre-existing
ssh-keygen env gap on the slim test image, unrelated). 77 of 77
MCP-specific tests pass (1 skipped).

## 0.2.31 — 2026-05-27

Simplify pass — small cleanups, no user action needed. Picks up four
items the prior review (0.2.28 + 0.2.30) missed.

### What changed

- ``orchestrator/agent_supervisor.py`` — removed dead
  ``set_tool_proxy_url()`` back-compat method. No callers remained
  after the ``set_tool_proxy(url, token)`` migration, and the
  back-compat method had a stale-token defect (cleared the URL but
  kept a bearer token from a prior call) anyway. Deletion closes
  both at once.
- ``config.py`` — hoisted the legacy-IP auto-heal set to a
  module-level ``frozenset`` constant. Was rebuilt as a local
  ``set`` on every ``load_config()`` call.

### What did NOT change

No public API change. No behaviour change for end users. The
``set_tool_proxy_url()`` removal only affects code paths inside the
daemon itself — no external integration calls it.

Verified: 963 unit tests pass (5 ssh-keys env tests skipped — the
slim test image has no ``ssh-keygen``, unrelated).

## 0.2.30 — 2026-05-27

Pure refactor — no behaviour changes, no user action needed.

### agent_worker.py decomposition (1776 → 738 lines)

The second-biggest file in the daemon (after the Wave 4 setup_generator
decomposition) split into three focused sibling modules using the
method-extraction-with-worker-param pattern documented in
``docs/handbook/06-conventions/refactoring-plan.md``.

- ``_agent_worker_mcp.py`` (148 lines) — MCP config builder + the
  ``_CLAUDE_CLI_BUILTIN_DISALLOW`` catalog.
- ``_agent_worker_manager.py`` (290 lines) — Manager chat handler +
  CLI streaming runner.
- ``_agent_worker_task.py`` (798 lines) — Worker task handler +
  ``run_sdk_session``. The biggest single concern in the daemon, now
  isolated.

Each ``AgentWorker`` method that was extracted is now a one-line
adapter that delegates to the extracted free function with ``self``
as the first arg. Tests that monkeypatch the instance methods still
work — the handlers route back through the worker's adapter rather
than calling the extracted function directly.

953 unit tests pass; verified surface intact (and ran the full sweep
between each of the three extractions, not just at the end).

## 0.2.29 — 2026-05-26

Pure refactor — no behaviour changes, no user action needed.

### setup_generator.py decomposition (2758 → 1351 lines)

The biggest file in the daemon split into four focused sibling
modules. Re-exported from ``setup_generator`` so every existing
caller keeps working unchanged.

- ``_setup_json.py`` (177 lines) — tolerant JSON parsing for
  Claude CLI responses. Pure stdlib, no async, no docker.
- ``_setup_cli.py`` (261 lines) — Claude CLI runners, constants,
  the empty-output disambiguation probe.
- ``_setup_skill_io.py`` (99 lines) — skill filesystem write
  helpers (slug-of-record + atomic SKILL.md write + chown).
- ``_setup_prompts.py`` (1032 lines) — all prompt constants and
  small string-builder helpers. Pure data with zero behaviour.

``setup_generator.py`` now contains only the orchestration
functions — the actual control flow is readable end-to-end
without scrolling past 1k lines of prompt text.

953 unit tests pass; verified surface intact.

## 0.2.28 — 2026-05-26

Root-cause fix for the recurring "external_outage / runner
unreachable" production issue + AI playbook updates so the agent
escalates with actionable diagnostics next time.

### UFW preflight on `cbcl start`

The chronic symptom on cbcl-stg was `ConnectionTimeoutError` from
inside the office container on every `execute_script`. The proxy
WAS running, bound to 0.0.0.0, and reachable from localhost. The
container CAN resolve `host.docker.internal → 172.17.0.1`. But the
connection timed out.

Root cause: UFW's default-DROP INPUT policy silently blocked
docker bridge → host packets. Invisible from daemon logs.

Fix in code: `cbcl start` now runs a preflight on Linux and warns
loudly when UFW is active without `docker0` allowed, naming the
exact fix command:

```
sudo ufw allow in on docker0
sudo ufw reload
```

No-op on macOS / Windows / no-ufw Linux. Pure diagnostic — never
modifies firewall state.

### in-tool retry + actionable error message

`execute_script` delegation through the host proxy now retries 3×
with exponential backoff (2s, 4s) before declaring the proxy
unreachable. Was 1-shot — masked transient blips as outages, made
the agent escalate on every cold-start race.

When all retries fail, the error message now quotes the UFW fix
command verbatim and the verification curl, so the agent's
escalation surfaces the right next step:

> Could not reach the host-side script runner via the tool proxy
> after 3 attempts (TimeoutError). Most common cause on Linux:
> UFW's default-deny policy is blocking the docker bridge.
> Operator fix: `sudo ufw allow in on docker0 && sudo ufw reload`.

### AI playbook updates

- **Automation Script Developer** — new "When execute_script
  fails" subsection: the tool already retried, treat first error
  as terminal, don't double-escalate. Distinguishes transport
  failures (escalate as `external_outage` / `infrastructure`) from
  typed-envelope failures (`missing_office_secret` →
  `missing_credential`, `office_secrets_corrupt` →
  `broken_dependency`).
- **Manager Assistant** — new "Infrastructure outages" subsection
  under Path D: explicit guidance on when `retry_blocked_task` is
  appropriate for the `external_outage` class. Operator's
  `decision_notes` must name a specific fix; vague notes like "try
  again" trigger an ASK-in-comments before the retry.

### Upgrade

```bash
pipx install --force git+https://github.com/cbcl-ai/cbcl-cli.git@v0.2.28
cbcl stop && cbcl start --daemon
# Watch for the UFW warning on Linux hosts.
```

## 0.2.27 — 2026-05-26

Comprehensive review pass — security hardening, dead-code removal,
test fixes.

### Security: tool-proxy bearer auth

`/script-execute-host` and `/tool-call` now require a bearer token.
The `ToolProxyServer` mints a per-process random token at startup;
the daemon plumbs it through `AgentSupervisor.set_tool_proxy()` →
`CUBICLE_TOOL_PROXY_TOKEN` env on every spawned agent container.
The in-container MCP sends it as `Authorization: Bearer <token>` on
every POST.

Closes the gap where any local process on the cbcl host could POST
to `/script-execute-host` and trigger script execution with
office-secret injection. The 0.0.0.0 bind in 0.2.26 (required so
Linux Docker can reach the host via `host.docker.internal:host-
gateway`) is now safe because callers without the token get 401.

`/health` stays unauthenticated so operator probes (`curl
localhost:.../health`) still work.

### Security: fs_handler symlink rejection

`_safe_resolve` switched from `str.startswith` to
`Path.is_relative_to(workspace.resolve())` and now refuses paths
that are symlinks. An agent that writes `pwn -> /etc/passwd` and
asks `fs_read` for `pwn` now gets `Symlinks are not allowed in
workspace paths` instead of the host's password file.

### Dispatcher: drop dead+dangerous blocked branch

`task_dispatcher._move_and_assign` had a `from_status="blocked"`
branch with no caller. If reached it would have burned the
`blocked_bounce_count` cap (see `docs/specs/task-spec.md` rule
#11). Dropped + docstring updated.

### Logging: SecureRotatingFileHandler

`_setup_logging_daemon` now uses a `_SecureRotatingFileHandler`
subclass that chmods every rolled file to 0o600 in
`doRollover`. The previous code only chmodded the initial file,
so rolled logs inherited umask (typically 0o644) and leaked
token fingerprints + diagnostic strings to anyone with shell
access.

### Cleanup

- `OfficeSecretsCorruptError` deduplicated to an alias for
  `CorruptOfficeSecretsError` (the canonical class in
  `office_secrets.store`). Flipped naming collapses into one;
  `tool_proxy_server` uses `str(exc)` instead of `exc.detail`.
- Deleted dead `_LEGACY_ANALYZE_SYSTEM_PROMPT` alias (comment
  admitted no live callers).
- Synced `scripts/templates/cubicle_helper.py` comment to match
  the backend's improved version (template-drift guard now passes).

### Tests

- `test_tool_proxy_host_script.py`: 4 new tests lock the bearer-
  auth contract (no-token 401, wrong-token 401, /tool-call 401,
  /health open). `_post` helper auto-passes the token.
- `test_task_dispatcher.py`: fixture stubs `_move_and_assign` so
  the v0.2.26 hardening (correctly returning False without a real
  backend) doesn't roll back every dispatched test.
- `test_script_runner.py`: updated for `OfficeSecretsCorruptError`
  dedup — uses `str(exc)` instead of the old `.script_name`/
  `.detail` attributes.

### Upgrade

```bash
pipx install --force git+https://github.com/cbcl-ai/cbcl-cli.git@v0.2.27
cbcl stop && cbcl start --daemon
```

## 0.2.26 — 2026-05-26

Three user-reported critical fixes from a live cbcl-stg session.

### Tool proxy unreachable from agent containers on Linux

`ScriptRunner.execute_script` was failing with
`ConnectionTimeoutError` from inside the office container when the
script's manifest referenced an office secret. Root cause: the
local HTTP proxy that brokers host-side script execution
(`tool_proxy_server.py`) was binding `127.0.0.1` only. On Linux
Docker, the container reaches the host via
`host.docker.internal:host-gateway` which routes to the docker
bridge IP (typically `172.17.0.1`), NOT loopback — so the TCP
connect just hung until timeout.

The proxy now binds `0.0.0.0` so it accepts connections on both
the loopback AND the bridge interface. The threat-model note
inline in the file explains why this is safe in cbcl's
single-tenant deployment posture (the proxy hosts a host-side
script runner endpoint that spawns `docker exec` with
caller-controlled env, so the bind address is a defence-in-depth
concern, not a confidentiality boundary). Operators wanting
tighter isolation can override `host=` to the specific docker
bridge IP.

### Dispatch swallowed HTTP errors on ready→in_progress

`TaskDispatcher._move_and_assign` was posting three HTTP calls
(activity, agent assignment, status move) without checking
response status or body error envelopes. Any 4xx/5xx or
`{"error": ...}` body was silently swallowed, the worker was
spawned anyway, and the board stayed at the source column with
no visible failure mode.

The dispatcher now checks `resp.status_code` AND JSON
`{"error": ...}` envelopes on every step. If any step fails, the
active marker is cleared so the reconciler re-queues the task on
the next tick instead of leaking a "claimed but not moved"
state.

### Other

`pyproject.toml` bumped to `0.2.26`.

## 0.2.24 — 2026-05-26

`cubicle.notify_manager()` now auto-routes to the task's
workstream chat — the scriptmaker no longer has to thread the
workstream value through their own code.

### New signature

```python
cubicle.notify_manager("Sourced 42 profiles")  # auto-routes
cubicle.notify_manager(
    "Cross-post to general chat",
    workstream="general_chat",
)
```

`message` is now positional; `workstream` is optional. When
omitted, the helper reads the Runner-injected
`CUBICLE_WORKSTREAM_SHORT_CODE` env var and falls back to
`general_chat` for manual UI runs. Caller-supplied value always
wins.

### What changed under the hood

- **Script Runner** now injects `CUBICLE_WORKSTREAM_SHORT_CODE` +
  `CUBICLE_SCOPE_READABLE_ID` into the script subprocess env. The
  Runner had these as input parameters but only used them to
  compute the output dir; now they're also visible to the script.
- **Outbox watcher's `_resolve_context_key`** accepts short_code
  as a 4th match path (after `general_chat` / UUID / short_code /
  name). Without this the SDK's auto-route would inject "TO" and
  the watcher would reject as unknown workstream.
- **Payload now carries `task_id`** for downstream debugging.
- Both SDK copies updated together — `backend/app/scripts/
  _bootstrap.py:CUBICLE_HELPER_SOURCE` (bootstraps new scripts)
  and `communicator/src/scripts/templates/cubicle_helper.py`.

Backward-compat: the OLD positional shape
`notify_manager("Sales", "Done")` still works — "Sales" matches
the workstream by name. Existing scripts continue to function;
the auto-route is purely additive.

## 0.2.23 — 2026-05-26

Fix: office containers on Linux daemons can now resolve
`host.docker.internal`, so `execute_script` for scripts that
reference Office Secrets actually reaches the host-side runner.

### Root cause

`host.docker.internal` is a Docker Desktop (Mac/Windows)
convenience name. On Linux it does NOT resolve unless the
container is started with `--add-host=host.docker.internal:
host-gateway` (Docker 20.10+).

The tool proxy listens on the host at
`http://host.docker.internal:<port>` and the in-container MCP
server posts to it for Office-Secret-using scripts. Without the
add-host mapping, in-container DNS for `host.docker.internal`
fails. The agent's `execute_script` errors with
`ClientConnectorDNSError: Could not reach the host-side script
runner via the tool proxy. Is cbcl running?` — even though cbcl
was running fine.

Other MCP tools survived because `_call_backend` falls back to
the public `BACKEND_URL=https://app.cbcl.ai` on proxy failure;
`execute_script` for Office-Secret scripts has no fallback
(host runner is the ONLY path that can read secret values).

### Fix

`containers.run()` now passes
`extra_hosts={"host.docker.internal": "host-gateway"}`. New
containers resolve the host on first attempt.

**Existing containers need a recreate** — Docker config (extra_hosts,
volumes, env, etc.) takes effect at container *create* time, not
restart time. The daemon's office-bring-up path recreates the
container on demand, but for live offices the manual fix is:

```
cbcl stop
docker rm -f cbcl-office-<slug>
cbcl start --daemon
```

The next office-connect creates the container fresh with the
host-gateway mapping in place.

### Also

`bind_script_variable` (shipped in 0.2.22) is now gated to the
Automation Script Developer in `_SCRIPT_AUTHOR_ONLY`. Random
workers shouldn't be moving wiring decisions on scripts they
don't own. New test pins the gating.

## 0.2.22 — 2026-05-26

The Automation Script Developer can now bind variables to Office
Secrets directly — no more "user, please open Settings, then the
Variables panel, then pick the secret name" round-trip.

### Why

The credential blocker that surfaced ESCALATED requests like:

> ESCALATED (missing_credential): PERPLEXITY_API_KEY Office Secret
> exists but is not yet BOUND to this script's variable

was almost always a wiring decision the AI had all the data to
make. The user just had to point and click. Five clicks for a
wiring decision the AI already knew. Worse: a second credential
bounce on the same task hit the blocked-bounce cap.

### What changed

* **MCP tool ``bind_script_variable``** in the worker tool list
  (``_agent_image/_mcp/tools_worker.py``). Takes ``script_name`` +
  ``variable_name`` + ``office_secret_name``; idempotent.
* **Daemon RPC action ``script_set_binding``** in
  ``_handlers/_requests.py``. Writes via the same
  ``VariableManager.set_binding`` primitive the chat WS uses for
  user-driven UI binds.
* **Automation Script Developer playbook** rewritten with a
  worked example of the new sequence: ``list_office_secrets`` →
  ``register_script`` (matching variable name) →
  ``bind_script_variable``. The legacy escalate-to-user path is
  preserved ONLY for the case where the secret doesn't yet
  exist; adding it remains user-only by policy.

The matching backend tool-call dispatch + HTTP endpoint live in
the cubicle monorepo (commit 954dbea).

## 0.2.21 — 2026-05-26

Fix: every host-side write that lands in the bind-mounted office
workspace now `chown`s to the in-container agent uid (1000:1000)
so the agent can actually edit / write what the daemon laid down.

### Root cause

The daemon runs as root on the host. Files/dirs it creates on the
bind-mounted workspace inherit root:root ownership. Bind mounts
preserve numeric uid, so root:root on host = root:root in the
agent container = no write access for the agent user (uid 1000).

Agents reported EACCES on:
- `Edit` against `/workspace/.scripts/<name>/` boilerplate files
- `Write` against `/workspace/outputs/<workstream>/<scope>/` MD outputs
- `Edit` against per-agent `/workspace/agents/<name>/CLAUDE.md`
- Any new dir or file the daemon created during config sync

### Fix

New `src/_chown.py` module — `chown_to_agent(path)` and
`chown_tree_to_agent(path)`. Best-effort: silently swallows
PermissionError so single-host dev (macOS, non-root daemon)
doesn't error out.

Applied at every host-side write site:

| Module | Calls |
|---|---|
| `fs_handler._write` | file + every new parent dir from `mkdir(parents=True)` |
| `fs_handler._mkdir` | new dir + every new parent dir |
| `workspace_setup` | base structure, outputs roots, per-workstream / per-scope dirs, per-agent dirs |
| `claude_md_writer` | shared / manager / per-agent / per-workstream dirs + CLAUDE.md files |
| `script_sync` | .scripts root, per-script dir, variables.json, .secrets.json, executions/ |

`AGENT_UID=1000` / `AGENT_GID=1000` mirror the `USER agent` line
in `Dockerfile.agent`. A test pins them in lock-step so a future
Dockerfile bump fails CI loudly.

## 0.2.20 — 2026-05-26

THE fix for the Skills page showing "No files". v0.2.19 added the
daemon-side `skills_discovered` action but it never worked
because the backend's request dispatcher only routes actions
starting with `fs_` to the FsHandler. Every request timed out
after 15s and the UI saw an empty list.

### Fix

Rename `skills_discovered` → `fs_list_skills` so the existing
`fs_*` dispatch rule in `_handlers/_requests.py` catches it.
Backend ref + tests updated together. New end-to-end test
`test_fs_list_skills_routes_through_handle_request` pins the
dispatch routing in place so a future rename can't break it
silently again.

### Why this kept slipping

The action name change is small but the failure mode was silent:

- Daemon log: zero `skills_discovered` lines (it was never invoked).
- Backend log: every probe ended with `daemon unreachable: timed
  out after 15.0s`.

A request that "times out because no handler exists" looks
identical to "daemon offline" in the backend log. The new test
catches the routing miss directly.

## 0.2.19 — 2026-05-26

Fixes the Skills page showing every skill with an empty file tree
in the split-host deployment topology.

### Root cause

The platform backend (cbcl-v2) was reading
`~/.cubicle/workspaces/<slug>/.claude/skills/` from ITS OWN host.
In the prod topology the workspace files live on the daemon
machine (cbcl-stg) — the backend's local scan found nothing and
the frontend rendered skill folders with no SKILL.md and no
resources.

### Fix

New daemon-side `skills_discovered` filesystem action that scans
the local workspace and returns the same structure the backend's
`DiscoveredSkill` schema expects. Backend's `/discovered` endpoint
delegates to the daemon via `request_bridge` first, falling back
to local-disk scan when the daemon is unreachable (single-host
dev case still works).

Same daemon-first / local-fallback treatment applied to every
disk-touching skill endpoint:

- `GET  /skills/{id}/content` — read SKILL.md via `fs_read`
- `PUT  /skills/{id}/content` — write SKILL.md via `fs_write`
- `GET  /skills/fs/{name}/files/{path}` — read resource via `fs_read`
- `POST /skills/fs/{name}/files` — create file/folder
- `PUT  /skills/fs/{name}/files/{path}` — write via `fs_write`
- `DELETE /skills/fs/{name}/files/{path}` — via `fs_delete`

Old daemons (pre-0.2.19) return "Unknown filesystem action" for
`skills_discovered` and the backend transparently falls back to
local-disk — split-host prod will keep showing empty trees until
cbcl is upgraded.

## 0.2.18 — 2026-05-26

THE actual fix. v0.2.16 and v0.2.17 were patching downstream
symptoms — every stdio `claude mcp add` call has been silently
failing since the feature shipped.

### Root cause

The daemon built:

```
claude mcp add --scope user --env KEY=VAL perplexity -- npx ...
```

`-e / --env` is a Commander VARIADIC option that consumes every
positional arg until the next flag. Because `perplexity` (the
name) came AFTER `--env`, claude tried to parse the name itself
as another env-var entry and exited 1 with:

```
Invalid environment variable format: perplexity,
environment variables should be added as: -e KEY1=value1 -e KEY2=value2
```

The daemon logged `rc=1` but **never logged stderr**, so the
failure was invisible. v0.2.16's `mcp_add_result` WS event did
carry the stderr to the UI as a red toast — but in the noise it
was easy to miss, and we kept patching the wrong layer.

### Fix

Reorder the argv so env flags come AFTER the name (the order
shown in `claude mcp add --help`'s example and in the user's
working manual command):

```
claude mcp add --scope user perplexity -e KEY=VAL -- npx ...
```

### Also

* Log stderr on `rc != 0` so the next bug like this isn't invisible.
* New regression test `test_env_flags_come_after_name_not_before`
  is an explicit guard against a future refactor flipping the
  order back.

## 0.2.17 — 2026-05-26

Patch release — finishes the v0.2.16 fix: custom-added MCPs were
still missing because the `claude mcp list` parser didn't recognise
the newer CLI output format.

### Fixed

Two compounding causes for "I added a custom MCP and it doesn't
appear":

1. **Parser only knew the old `name: url - status` CLI format.**
   Newer claude CLI versions print
   `name: url (transport) ✓ Connected` — no dash separator — so the
   parser fell through to `status="unknown"` for every line. The
   frontend's connector sidebar had filter buckets for `connected` /
   `needs_auth` / `failed` only, and `unknown` servers were dropped
   from the rendered list even though they were live in the
   container.

2. **No "Other" group in the UI.** Even when the parser had reason
   to mark a server `unknown` (a future CLI format we don't yet
   handle), the UI silently hid it.

### Changes

* `parse_mcp_list` now detects status by substring anywhere in the
  line ("Connected" / "Failed" / "Needs authentication"). Robust to
  ✓ / ✗ glyphs, ANSI color escapes, and the historical dash.
* Transport detection is case-insensitive — newer CLIs print
  `(HTTP)` capped.
* URL extraction strips ANSI escapes, glyphs, transport tag, and
  status keywords so the cached payload is clean for display.
* Status precedence: `Needs authentication` outranks `Failed` when
  both appear in one line (CLI sometimes prints
  `✗ Failed: needs authentication`).
* Frontend adds an "Other" group that surfaces ANY server whose
  status doesn't match the three known buckets. Neutral icon
  instead of the misleading red X for unknown.
* 11 new parser unit tests cover both format variants so a future
  CLI bump that flips back doesn't silently break the UI.

## 0.2.16 — 2026-05-23

Patch release — user-added MCPs now actually appear in the
Connectors list, and the user always sees what happened.

### Fixed

Three things were conspiring to hide newly-added MCPs from the UI:

1. **5 s debounce in `refresh_mcp_list` killed post-add refreshes.**
   The debounce was designed to throttle periodic refreshes — but
   it also no-op'd the explicit post-mutation refresh if office
   startup happened to have just run one. No `mcp_list_updated`
   event → no React-Query invalidation → UI never saw the new
   server. Fix: `refresh_mcp_list` now accepts `force=True` to
   bypass the debounce; `run_mcp_add` and `run_mcp_remove` pass
   it. Periodic refreshes still respect the debounce.

2. **30 s subprocess timeout was tight for stdio installs.**
   `npx -y @perplexity-ai/mcp-server` includes an npm install on
   first run — dependency download + native-dep compile easily
   exceeds 30 s on slow networks. Subprocess timed out, handler
   returned silently, no refresh, no UI signal. Fix: stdio mode
   gets a 120 s budget; HTTP keeps 30 s (no install).

3. **No success / failure event back to the UI.** The user clicked
   Add, saw the dialog close, and stared at an empty list with no
   idea whether anything happened. New `mcp_add_result` board-WS
   event with `{name, transport, status: "added"|"failed"|"timed_out",
   error}`. Frontend toasts the result and invalidates the
   connected-list cache. Non-zero `claude mcp add` exits forward
   the CLI's stderr (env values scrubbed) so npm errors (404
   package, missing peer dep) are surfaced.

## 0.2.15 — 2026-05-23

Patch release — `cbcl status` now reports the actual container
state instead of always saying `not_running`.

### Fixed

- **`cbcl status` always reported `Container: not_running` even
  when containers were actually running.** The status command
  runs in a SEPARATE Python process from the daemon. It
  instantiated a fresh `ContainerManager` whose `_containers`
  in-memory dict was empty (the daemon's view isn't visible
  cross-process), so the existing `get_status(office_id)`
  looked up office_id in the empty dict and returned
  `not_running` every time.

  Fix: new `get_status_by_name(container_name)` method that
  bypasses the in-memory cache and asks the Docker daemon
  directly via `client.containers.get(name)`. Distinguishes
  three states the operator cares about:
  - `not_running` — Docker returned NotFound.
  - `unknown` — other Docker error (daemon offline, permission
    denied) with the cause in the `error` field.
  - `running` / `exited` / etc. — whatever Docker reports.

  After upgrade, `cbcl status` shows the real per-office
  container status instead of "not_running" everywhere.

## 0.2.14 — 2026-05-23

Patch release — round-5 review fixes. Backend changes only on the
daemon-test side (lockstep test refactor); the version bump exists
so the public release reflects the test-suite improvement.

### Fixed

- **Lockstep test now actually runs.** The round-4 lockstep test
  (`test_constants_lockstep_with_backend`) tried to
  `from app.connectors.router import ...` which ImportErrors
  whenever the backend's dep tree isn't installed — i.e. ALWAYS
  in the daemon's own test environment. The test silently SKIPPED
  every time the daemon test suite ran. Useless safety net.

  Refactored to text-grep both source files (daemon + backend) and
  compare the constant definitions via regex. Works in every
  Python env that has the daemon source on disk. Verified locally:
  the test now ran in the daemon-only test container and PASSED
  (was SKIPPED before). Drift between the two Python copies of
  the security constants now fails CI for real.

### Note

The backend-side fixes from round-5 (sibling endpoint validators
for `/mcp/authenticate`, `/mcp/connect`, `/mcp/cli-auth`,
`/mcp/cli-auth-code`; FastAPI 422 error parsing in the
frontend toast; HTTP/HTTPS copy consistency in the dialog;
browse-catalog stdio-entry routing) ship with the platform deploy;
no cbcl-side code changed for those.

## 0.2.13 — 2026-05-23

Patch release — round-4 review fixes for the 0.2.12 stdio Custom MCP
feature. Closes argv-injection gaps and tightens defence-in-depth.

### Fixed

- **Name argv-injection guards on add AND remove.** The `name`
  field previously accepted any 1-100 char string. Now matches
  `^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$` — refuses leading `-`
  (which `claude mcp add` would argv-parse as a flag despite
  argv defeating shell injection) and refuses `/` / NUL / control
  chars that would corrupt `~/.claude.json`. Mirrored on `mcp_remove`
  so a payload that bypassed an older backend can't be removed by
  re-submitting the bad name.

- **URL scheme validation** on http/sse transport. Refuses urls
  not starting with `http://` or `https://`.

- **Env value forbidden chars**. `\x00` (crashes subprocess argv),
  `\n` / `\r` (corrupt the in-container JSON config) now refused
  at the validator with a clear message.

- **Full defence-in-depth coverage**. `_build_stdio_argv` now
  mirrors EVERY backend check: name regex, args list cap (64),
  env vars list cap (32), forbidden env value chars. HTTP/SSE
  branch also re-validates name + url scheme.

- **Env value log scrubbing**. New `_scrub_env_values` collapses
  `--env KEY=VAL` to `--env KEY=[REDACTED]` before logging
  `claude mcp add`'s stdout, so a future CLI version that echoes
  env flags doesn't leak secrets into the operator log.

- **Tighter exception scope** in `run_mcp_add` / `run_mcp_remove`:
  `subprocess.TimeoutExpired` / `SubprocessError` / `OSError`
  caught explicitly with distinct log messages per failure class
  (was a bare `except Exception` that could swallow real bugs).

### Tests

- 7 new daemon tests + 1 lockstep test that imports both backend
  and daemon constants and asserts they match (catches one-sided
  edits at CI time).

## 0.2.12 — 2026-05-23

Patch release — stdio transport support in the Custom MCP add path.
Lets the platform install MCP servers that ship as local
packages (Perplexity, Brave Search, GitHub MCP, anything `npx -y` /
`uvx`-able) entirely via the UI — no SSH-and-shell-into-the-container
ceremony.

### Added

- **`run_mcp_add` now branches on `transport`.** http / sse keep
  the legacy `claude mcp add --transport <t> --scope user <name>
  <url>` shape. stdio composes
  `claude mcp add --scope user [--env K=V ...] <name> -- <command>
  [<args>...]` via an argv ARRAY (no shell, no string-format) so
  shell metacharacters in user inputs become literal bytes that
  `subprocess.run(shell=False)` never re-parses.
- **`_build_stdio_argv` helper** — defence-in-depth re-validation:
  command must be in the allowlist (`npx`, `uvx`, `python3`,
  `node`, `deno`), args must match `^[A-Za-z0-9@:/._\-+~,=]+$`
  (no spaces, no shell metas), env-var names must match
  `^[A-Z][A-Z0-9_]{0,63}$`, no duplicate names, per-arg cap at
  512 chars. The backend's Pydantic validator is the primary
  gate; this helper is the backstop. 14-test unit suite.

Env-var values flow through the daemon argv as `--env KEY=VAL`
flags and are never logged (only the names are surfaced in the
operator log).

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
