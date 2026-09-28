"""Execution-only recovery and handoff instructions; never emitted for review/triage."""
from __future__ import annotations

import re
from typing import Any


# States RuntimeState records for a task's managed script run. The runner
# writes ``timed_out`` (``timeout`` is the legacy spelling); both are
# terminal in ``RuntimeState.script_wait``.
RECORDED_SCRIPT_STATES = frozenset({
    "completed",
    "failed",
    "killed",
    "cancelled",
    "timeout",
    "timed_out",
    "running",
    "unknown",
})


# The prompt lists at most this many recorded runs: the newest ones.
MAX_LISTED_SCRIPT_RUNS = 20


def _valid_script_runs(task_data: dict[str, Any]) -> list[tuple[str, str]]:
    """Every well-formed ``(execution_id, state)`` row, oldest launch first.

    Execution ids start with the launch time (``exec-<UTC timestamp>-<hex>``),
    so id order is launch order whatever order the runtime ledger returned.
    """
    results = task_data.get("script_handoff_results")
    if not isinstance(results, list):
        return []
    runs: list[tuple[str, str]] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        execution_id = str(result.get("execution_id") or "")
        state = str(result.get("state") or "unknown")
        if (
            re.fullmatch(r"[A-Za-z0-9_-]{1,100}", execution_id)
            and state in RECORDED_SCRIPT_STATES
        ):
            runs.append((execution_id, state))
    return sorted(runs, key=lambda run: run[0])


def recorded_script_runs(task_data: dict[str, Any]) -> list[tuple[str, str]]:
    """The newest ``MAX_LISTED_SCRIPT_RUNS`` recorded runs, oldest first.

    The ONE gate shared by the STEP 0 branch selector and the
    verification-resume list in ``worker_prompt``: BRANCH S is selected
    exactly when this list is non-empty, so it can never point at a list
    that did not render. The newest runs are kept because a resume follows
    the latest completions; ``omitted_script_run_count`` says how many
    earlier runs the list leaves out.
    """
    return _valid_script_runs(task_data)[-MAX_LISTED_SCRIPT_RUNS:]


def omitted_script_run_count(task_data: dict[str, Any]) -> int:
    """How many earlier recorded runs ``recorded_script_runs`` leaves out."""
    return max(0, len(_valid_script_runs(task_data)) - MAX_LISTED_SCRIPT_RUNS)


def has_capacity_wait_resume(task_data: dict[str, Any]) -> bool:
    """Same gate as ``render_capacity_wait_resume`` (any dict renders)."""
    return isinstance(task_data.get("capacity_wait_resume"), dict)


def task_owns_output_dir(task_data: dict[str, Any], output_dir: str) -> bool:
    """True when ``output_dir`` is this task's own directory (dynamic mode).

    ``worker_prompt.task_output_dir`` ends a task-owned path with
    ``/tasks/<task_id>``; the legacy per-workstream directory is shared by
    every task in the workstream.
    """
    task_id = str(task_data.get("task_id") or task_data.get("id") or "")
    return bool(task_id) and output_dir.rstrip("/").endswith(f"/tasks/{task_id}")


def _orphan_glob_lines(
    task_data: dict[str, Any], output_dir: str, readable_slug: str,
) -> list[str]:
    """STEP 0.3 crash-leftover patterns (C4b-G9).

    A task-owned directory holds only this task's files, so every path in it
    is a candidate. The shared legacy directory is searched by the task's
    readable-id prefix, which the execution prompt tells workers to use; an
    unprefixed glob there would pick up other tasks' files.
    """
    if task_owns_output_dir(task_data, output_dir):
        return [
            f"  - `{output_dir}/**` (this directory is this task's alone: any",
            "    file there not listed in EXISTING DELIVERABLES is a candidate)",
        ]
    return [
        # The trailing wildcard also covers the CHECKPOINT.md convention.
        f"  - `{output_dir}/{readable_slug}*`",
        f"  - `{output_dir}/**/{readable_slug}*`",
        # Legacy flat path — scan in case prior runs (before per-
        # workstream separation) wrote there.
        f"  - `/workspace/outputs/{readable_slug}*`",
    ]


def build_execution_preflight(
    task_data: dict[str, Any], *, output_dir: str, artifacts_info: str,
    workstream_claude_md_path: str | None, workstream_spec_md_path: str | None,
) -> list[str]:
    """Recover completed/partial/rework state without restarting valid work."""
    readable_id = task_data.get("readable_id", "?")
    readable_slug = readable_id.lower().replace(".", "_")
    task_id = task_data.get("task_id", "")
    task_status = str(task_data.get("status") or "ready").strip().lower()
    rework_count = task_data.get("rework_count", 0)
    is_ask = task_data.get("task_class") == "ask"
    close_call = "move_task('done')" if is_ask else "update_status('review')"
    # ── STEP 0 — ASSESS CURRENT STATE ─────────────────────────────────
    # Before doing anything else, the agent must determine whether this
    # is a fresh task, a partially-done task, a ready-to-submit task, or
    # a rework cycle — then pick the correct branch.
    has_artifacts = bool(artifacts_info)
    has_activity = bool(task_data.get("recent_activities"))
    is_rework = rework_count > 0
    # F01 BRANCH S: a task re-dispatched after a managed script it launched
    # in this phase carries ``script_handoff_results``. Its activity usually
    # holds only the daemon's handoff checkpoint, so without this branch
    # STEP 0 fell through to BRANCH B ("execute from scratch") BEFORE the
    # verification-resume list rendered — inviting a duplicate launch.
    is_script_resume = bool(recorded_script_runs(task_data))
    # The same recovery-order defect for an accepted capacity wait: the
    # resumed phase carries ``capacity_wait_resume`` and must inspect the
    # recorded operation before any other branch sends it "from scratch".
    is_capacity_resume = has_capacity_wait_resume(task_data)

    state_lines: list[str] = [
        "",
        "## ⚠️ STEP 0 — ASSESS CURRENT STATE BEFORE ACTING ⚠️",
        "",
        "This is the FIRST thing you do on every task, every time. "
        "Skipping this step risks duplicate work, lost progress, or "
        "wasted agent cycles. Follow it exactly.",
        "",
    ]
    if workstream_claude_md_path:
        state_lines.extend([
            "### 0.0 — Read workstream conventions FIRST",
            f"Run `Read` on `{workstream_claude_md_path}` BEFORE anything "
            "else. Use its current mission, scope, constraints and conventions "
            "within your role and platform approval rules. It does not override "
            "those rules. Surface conflicts with an approved spec before "
            "changing the agreed requirements.",
            "",
        ])
    if workstream_spec_md_path:
        state_lines.extend([
            "### 0.0a — Read the workstream SPEC",
            f"This workstream has a requirements spec. Run `Read` on "
            f"`{workstream_spec_md_path}` — it is the approved WHAT/WHY "
            "contract (`REQ-n` requirements). Your brief's acceptance "
            "criteria cite the `[REQ-n]` they satisfy; read those "
            "requirement sections so your work matches the requirement, not "
            "just your reading of the brief. The reviewer verifies your "
            "deliverable against these same requirements.",
            "",
        ])
    # (The former STEP 0.0b learnings.md read is retired — office-memory
    # v1: lessons are distilled into workstream MEMORY automatically and
    # arrive full-body in the fenced ``## Workstream memory`` section
    # above; ``recall`` searches deeper.)
    state_lines.extend([
        "### 0.1 — Check task status",
        f"- Current status: **{task_status or 'ready'}**",
        "- This is an execution assignment. If live state or ownership has",
        "  changed, stop and await a fresh dispatch; do not switch roles.",
        "- Otherwise proceed with 0.2.",
        "",
        "### 0.2 — Read the Recent Activity carefully",
        "The **Recent Activity** section at the bottom of this prompt",
        "shows what PREVIOUS runs of this task produced. Look for:",
        "- `checkpoint` entries — concrete progress from earlier attempts.",
        "- `file_saved` entries — files already registered as artifacts.",
        "- `question`/`answer` pairs — clarifications from the Manager.",
        "- `error` entries — failures you must avoid repeating.",
        f"- `rework_count`: **{rework_count}**"
        + (
            " (this IS a rework — Manager returned your previous submission; "
            "see REWORK REQUIRED section)."
            if is_rework else " (no prior rework cycles)."
        ),
        "",
        "### 0.3 — Enumerate existing deliverables on disk",
        "Here, 'deliverable' means a file named in the Brief's Output",
        "Format — the document the reviewer will open. It does NOT mean",
        "every source file an earlier run may have edited. If the Output",
        "Format names no document (e.g. a pure code change), there may be",
        "no deliverable file at all — the code change itself is the",
        "deliverable. See your CLAUDE.md 'What counts as an artifact'",
        "for the boundary.",
        "There are TWO places contracted deliverables can exist:",
        "  (a) Registered artifacts — see the EXISTING DELIVERABLES section below.",
        "  (b) Unregistered files — on disk but not yet attached to this task.",
        "      This happens if a prior session wrote a file but crashed",
        "      before calling `save_file`.",
        "",
        "**Run `Glob` with these patterns to catch unregistered files:**",
        *_orphan_glob_lines(task_data, output_dir, readable_slug),
        "Also check any exact file path the Brief's Output Format names.",
        "If the glob returns paths NOT listed in EXISTING DELIVERABLES,",
        "treat them as orphan files: register one (per 0.5) only if it is a",
        "contracted deliverable matching the Brief's Output Format (complete it",
        "first if partial); leave other crash leftovers unregistered.",
        "**If a CHECKPOINT.md file exists, READ IT FIRST** — it is the",
        "progress index written by a prior attempt and tells you exactly",
        "which chunks are done vs pending.",
        "",
        "### 0.4 — Pick the correct branch and act",
        "",
    ])

    # Completion-fence short-circuit (T4.3.5): a prior session may have
    # finished the work and written the marker but had its final
    # update_status(review) fail transiently. Don't redo hours of work.
    # The marker records the rework_count of the attempt that wrote it, so
    # the short-circuit fires ONLY when it matches THIS dispatch's attempt:
    # a stale marker from before a rework (a different rework_count) is
    # ignored, so a rework genuinely redoes the work instead of falsely
    # short-circuiting — AND a reworked-then-failed-to-submit task is still
    # protected from a full re-execution (its post-rework marker matches).
    # AIQ-5: ask-class tasks never write the marker (no STEP 0.7), so the
    # branch is not rendered for them.
    if not is_ask:
        state_lines.extend([
            "**→ BRANCH 0 (ALREADY COMPLETE?) — check this FIRST, even on "
            "rework.**",
            f"`Read` `/workspace/.cubicle/tasks/{readable_slug}/COMPLETED.json`.",
            "Short-circuit ONLY if ALL of these hold: the file exists; its "
            f"`rework_count` equals **{rework_count}** (THIS attempt — a marker "
            "with any other value is stale, from a prior attempt or a pre-rework "
            "run: IGNORE it and do the work below); and every artifact path it "
            "lists is on disk. The marker is a recovery hint, not proof of correctness. "
            "When all hold, inspect the current artifacts and required checks (a prior "
            "session finished but its submit failed): verify those artifacts "
            "satisfy the acceptance criteria, post a brief `add_activity` note "
            "('resuming — prior run completed; submitting'), then call "
            "`update_status('review')` IMMEDIATELY — do NOT redo the work. "
            "Otherwise ignore this and continue to the branch below.",
            "",
        ])

    if is_script_resume or is_capacity_resume:
        next_step = (
            "the next required test run of your two-run protocol"
            if (task_data.get("assigned_agent") or "").strip()
            == "automation-script-developer"
            else "the brief's next step"
        )
        branch_lines: list[str] = [
            "**→ BRANCH S (MANAGED-SCRIPT RESUME)** — this task was parked on",
            "a managed script handoff made in this phase. This branch replaces",
            "branches A-D for this session.",
        ]
        if is_capacity_resume:
            branch_lines.extend([
                "Capacity wait: follow 'Resume after capacity waiting' below —",
                "call `get_operation` for the recorded operation FIRST, then",
                "retry only with its recorded operation key (or repeat only its",
                "recorded reconcile/cancel action); never start a replacement run.",
            ])
        if is_script_resume:
            branch_lines.extend([
                "Recorded run(s) are listed under 'Managed script",
                "verification-resume' below.",
                "1. Inspect each listed run (`get_script_status`, its log and",
                "   outputs) BEFORE any new side effect; never relaunch a completed run.",
                "2. Succeeded → verify its outputs against the criteria, register",
                f"   contracted deliverables, then continue with {next_step} or",
                f"   submit via `{close_call}`.",
                "3. Failed/unknown → diagnose first; relaunch only as a deliberate,",
                "   corrected run (a tracked operation needs a new key). If you",
                "   genuinely cannot proceed, use the ESCALATED blocker protocol.",
            ])
        else:
            branch_lines.append(
                "Once the recorded operation is resolved and its result verified, "
                f"continue with {next_step} or submit via `{close_call}`."
            )
        if is_rework:
            branch_lines.append(
                "Rework still applies: address every point in REWORK REQUIRED."
            )
        state_lines.extend(branch_lines)
    elif is_rework:
        state_lines.extend([
            "**→ BRANCH D (REWORK)** — rework_count = "
            f"{rework_count}. The Manager/reviewer returned your previous",
            "submission with specific feedback (see REWORK REQUIRED).",
            "1. Read the reviewer's feedback carefully.",
            "2. Read every existing artifact listed in EXISTING DELIVERABLES.",
            "3. Address EACH feedback point. Edit the existing files;",
            "   do NOT rewrite from scratch unless the reviewer explicitly asks.",
            "4. Re-assess all criteria; verify fixes and affected regressions, reusing valid evidence. Submit via",
            f"   `{close_call}`.",
            "5. Do NOT re-register files you only edited — the artifact",
            "   record still points to them.",
        ])
    elif has_artifacts:
        state_lines.extend([
            "**→ BRANCH C (ARTIFACTS PRESENT)** — a prior run registered",
            "deliverables. DO NOT recreate them.",
            "1. Read each artifact file via the `Read` tool.",
            "2. Verify every acceptance criterion is satisfied.",
            "3. Satisfy Execution checks, reusing valid evidence under the verification contract.",
            f"4. If all pass → call `{close_call}` immediately.",
            "5. If anything is missing or wrong → fix it minimally in place",
            "   (edit the existing file; do NOT create new variants).",
            "6. Creating duplicate files when the work is already done is a",
            "   CRITICAL ERROR.",
        ])
    elif has_activity:
        state_lines.extend([
            "**→ BRANCH B (PARTIAL WORK LIKELY)** — activity exists but no",
            "artifacts are registered. A previous run may have been",
            "interrupted. Before creating anything:",
            "1. Run the `Glob` patterns from 0.3 to find unregistered files.",
            f"2. If `{output_dir}/{readable_slug}_CHECKPOINT.md`",
            "   exists, `Read` it FIRST. It lists which chunks the prior",
            "   attempt already wrote (done) and which remain (pending).",
            "   Resume from the next `pending` entry — do NOT redo `done`",
            "   chunks.",
            "3. For every other unregistered file from step 1 — `Read` it",
            "   and decide:",
            "   (a) content satisfies the brief → register via `save_file`",
            "       (automatically attached to this task), verify criteria, submit.",
            "   (b) content is partial/wrong → complete/fix it, register,",
            "       then submit.",
            "4. If no matching files exist, review the Recent Activity for",
            "   context and execute from scratch. Pick up where the prior",
            "   run left off if the checkpoints describe progress.",
        ])
    else:
        state_lines.extend([
            "**→ BRANCH A (FRESH TASK)** — no prior activity, no artifacts.",
            "1. Still run the `Glob` patterns from 0.3 as a safety check",
            "   (a prior crash can leave orphan files with no activity log).",
            "2. If nothing found → execute the brief from scratch.",
            "3. If anything found → for each hit, decide whether it is",
            "   a CONTRACTED deliverable (i.e. matches the Brief's Output",
            "   Format) before calling `save_file`. Crash-leftovers that",
            "   aren't part of the contracted output (working notes, half-",
            "   written drafts of the wrong artifact, stray source edits)",
            "   should NOT be registered — leave them or clean them up.",
            "   Register only the legitimate matches, then verify and",
            "   submit if they already satisfy the brief.",
        ])

    if is_ask:
        # AIQ-5: ask-class close is ONE move_task('done') with the answer in
        # the comment — no completion marker, no submit-for-review machinery,
        # and normally no artifacts at all.
        state_lines.extend([
            "",
            "### 0.5 — Artifacts (ask-class)",
            "An ask normally produces NO artifacts — the answer travels in",
            "the `move_task` comment. Call `save_file` ONLY if the task",
            "genuinely produced a file the brief asked for.",
            "",
            "### 0.6 — Close criteria (ask-class — how you know you're done)",
            "All of these MUST be true before closing:",
            "  ✓ The ANSWER satisfies every acceptance criterion.",
            "  ✓ Required Execution checks are satisfied under the verification contract.",
            "Then close in two calls: post the answer via `add_activity`",
            "(event_type `comment`), then ONE `move_task` of this task to `done`",
            "with the answer summarized in the move comment. No completion marker",
            "is written for asks.",
            "",
        ])
    else:
        # DELIBERATE restatement (recorded 2026-08-26, artifact-boundary
        # dedup pass): STEP 0.5 repeats the canonical "What counts as an
        # artifact" boundary from SHARED_AGENT_WORK_RULES on purpose — the
        # register-every-source-file failure is the #1 observed artifact
        # mistake, and per-task salience at the point of action is the fix
        # the two-copies doctrine allows (canonical rules + ONE
        # point-of-use restatement; 06_ai_best_practices.md I-7 record).
        # STEP 0.6 below deliberately REFERENCES this step instead of
        # restating the boundary a third time. The office CLAUDE.md's
        # Common Rules bullet is a one-line pointer, not a copy.
        state_lines.extend([
            "",
            "### 0.5 — Registering a file as an artifact",
            "Register ONLY the files named in the Brief's Output Format —",
            "the documents the reviewer will open to decide PASS/FAIL. If",
            "your task is a code change touching many source files, register",
            "a markdown change-summary ONLY when the Output Format names one",
            "— and then it is ONE document (rationale, files touched, test",
            "evidence, follow-ups), NOT every edited `.py`/`.ts`/`.tsx`.",
            "Otherwise the code change itself is the deliverable: register",
            "nothing and carry a 3-line summary of the change in your",
            "`update_status` comment instead. See your CLAUDE.md 'What",
            "counts as an artifact' if in doubt.",
            "",
            "A contracted deliverable is only COMPLETE when it is BOTH on",
            "disk AND registered via `save_file`. Registration is idempotent",
            "— calling `save_file` with the same `file_path` twice reuses",
            "the same DB row (no duplicate artifact rows), so retrying on",
            "transient errors is safe. The system auto-attaches any",
            "save_file call to your current task, so you just pass `title`",
            "+ `file_path` (and optional `tags` / `file_type`).",
            "",
            "### 0.6 — Submission criteria (how you know you're done)",
            "All of these MUST be true before calling `update_status('review')`:",
            "  ✓ Every acceptance criterion from the brief is satisfied.",
            "  ✓ Required Execution checks are satisfied under the verification contract.",
            "  ✓ Every file named in the Brief's Output Format is on disk",
            "    AND registered as an artifact (one `save_file` call per",
            "    contracted output — the 0.5 boundary applies: side-effect",
            "    source edits register nothing).",
            "  ✓ No CONTRACTED deliverable from 0.3 remains unregistered.",
            "If any item above is NOT true, do NOT submit. Finish it first.",
            "",
            "### 0.7 — Completion fence (write the marker, THEN submit)",
            "IMMEDIATELY before calling `update_status('review')`, `Write` a "
            "completion marker so a transient submit failure can't trigger a "
            "full re-execution:",
            f"  `/workspace/.cubicle/tasks/{readable_slug}/COMPLETED.json`",
            "  containing: `{\"task_id\": \"" + task_id + "\", "
            f"\"rework_count\": {rework_count}, "
            "\"timestamp\": \"<current UTC time, ISO-8601, e.g. "
            "2026-06-15T10:30:00Z>\", "
            "\"artifacts\": [<the file paths you registered>], "
            "\"completed\": true}`. The `rework_count` MUST be the value above "
            f"({rework_count}) so a later session can tell this marker is "
            "current.",
            "Write the marker, then call `update_status('review')`. If the "
            "move fails transiently, the marker lets your next session submit "
            "without redoing the work (see STEP 0).",
            "",
        ])

    if has_artifacts:
        state_lines.extend([
            "## EXISTING DELIVERABLES (registered artifacts)",
            "",
            artifacts_info,
            "",
        ])

    return state_lines
