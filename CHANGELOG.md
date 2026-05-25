# Changelog

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
