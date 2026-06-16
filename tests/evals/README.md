# Prompt content evals

Content-level regression tests for the AI layer. These complement
`tests/test_worker_prompt.py` (which checks structural assertions like
"the section header exists") with **content** assertions: given a
specific input, the prompt asks Claude to do the right thing — or, just
as importantly, NOT to do the wrong thing.

These are NOT live API calls — they exercise the prompt builders
deterministically and assert on the resulting prompt text. They run on
every CI build with no external dependencies.

## What's covered

| File | Focus |
|------|-------|
| `test_prompt_injection_defenses.py` | XML fences + "treat as data" directives are present in Manager + Worker prompts; literal closer escaping. |
| `test_brief_to_prompt_contract.py` | Required brief fields all surface in the worker prompt; missing brief sections degrade gracefully. |
| `test_review_mode_routing.py` | Designated-reviewer prompt vs non-designated-reviewer prompt vs Manager-Assistant Board-Operator prompt are all distinct and authorise the correct tools. |
| `test_step0_branches.py` | STEP 0 branch selection (fresh / partial-with-activity / artifacts-present / rework) maps correctly to task state. |
| `test_prompt_references_reality.py` | (T5.4.1) every `mcp__cubicle-tools__X` referenced in any template is a REAL tool; the generated Manager allowlist is reverse-complete. Would have caught F2/F4/F5/F12. Lane: static CI. |
| `test_prompt_transitions_legal.py` | (T5.4.3) no template instructs a move to `backlog`; every instructed target is a legal `VALID_TRANSITIONS` target (imports `app.tasks.board`). Would have caught F9. Lane: static CI. |
| `test_numeric_invariant_pins.py` | (T5.4.7) prompts state the same numbers as the code constants (bounce cap, rework cap, scope soft-max in Manager + Planner, MA review budget ≤ ceiling, triage cooldown, board-sweeper interval). Would have caught I-10. Lane: static CI. |
| `../test_auto_decide_rows.py` | (T5.4.8) auto-decide rows ↔ backend `REQUEST_TYPES` bidirectional parity; no row contradicts itself (F13 shape). Lane: static CI. |
| `test_rework_cap_policy.py` | (T5.4.5) reviewer surfaces say escalate-at-cap / never rubber-stamp; no surface instructs silent auto-approve. Cross-ref Phase 1 F24. Lane: static CI. |
| `../test_session_lock_pin.py` | (T5.1.4) Manager session-lock trigger set ↔ code constant. Lane: static CI. |
| `../test_blocker_protocol_consistency.py` | (T5.2.5) blocker template/enum/routing single-source; no phantom `category=`/`severity=`. Lane: static CI. |
| `../test_system_agent_roster_parity.py` | (T5.2.7) five-agent roster incl. Planner across all wizard render sites. Lane: static CI. |
| `backend/tests/test_system_agent_prompts.py` | (T5.2.3) no "unassign" in any SYSTEM_AGENT_DEFAULTS prompt; Auditor has Write. Lane: backend pytest. |
| `backend/tests/test_escalate_routing_e2e.py` | (T5.4.6 ✅) escalate_blocker routing E2E through the tool-call handler over the full `BLOCKER_CLASS_TO_CATEGORY` map. Lane: backend pytest. |
| `live/test_tier_routing.py` | (T5.4.9 — TODO) Tier-0 golden: "verify this SSH connection" → one MA task, no scope/script. Lane: LIVE (API). |
| `backend/tests/test_spec_transition_drift.py` | (T5.4.4 / T9.3.1) task-spec transition table ↔ `board.py`. Built by Phase 9. Lane: backend pytest. |
| `test_spec_driven_planning.py` | (Phase 10 / T10.3.5) spec-driven planning family: Planner specify-first + `covers:`, Manager Tier-3-starts-with-spec + requirement-change routing, `[REQ-n]` brief + reviewer spec-check, STEP 0.0a spec read, spec_change impact pass, verify-mode REQ coverage, `propose_spec_update` transform completeness. Lane: static CI. |
| `../test_spec_template.py` | (Phase 10 / T10.1.1) workstream spec template: seven sections, REQ/FLOW append-only id lint, convention path helpers, token budget. Lane: static CI. |

## Drift class → guarding eval (T5.3.7)

Tool descriptions + playbooks are prompt content (see communicator CLAUDE.md
"Tool descriptions are prompts"). Each drift class that has bitten us maps to
the eval that now pins it:

| Drift class | Guarding test |
|------|------|
| Per-role tool catalog drift (a tool added/removed/renamed) | `../test_tool_catalog_drift.py` (exact per-role sets, incl. the three worker sub-catalogs + Planner set-algebra) |
| Manager allowlist ≠ live catalog | `../test_claude_md_writer.py::TestManagerAllowlistGeneration` (rendered == catalog, bidirectional) |
| Phantom `category=`/`severity=` escalate args; blocker template/enum drift | `../test_blocker_protocol_consistency.py` |
| "four vs five" system-agent roster; missing-planner reserved-name guard | `../test_system_agent_roster_parity.py` |
| Manager session-lock trigger set (prompt ↔ code) | `../test_session_lock_pin.py` |
| Transform ↔ schema ↔ backend-payload consistency | `../test_transform_schema_consistency.py` |
| System-agent system-prompt content (no "unassign", Auditor Write) | `backend/tests/test_system_agent_prompts.py` |

## How to add a new eval

1. Build a representative `task_data` or `context_data` dict.
2. Call the prompt builder.
3. Assert on substring presence / absence in the result.

If you find yourself needing to assert on a multi-paragraph block, add a
small helper that finds the section by header and checks within it —
don't make assertions on byte ranges.
