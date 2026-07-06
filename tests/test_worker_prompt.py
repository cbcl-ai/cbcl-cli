"""Tests for the worker prompt builder.

Focused on the two instructional contracts users depend on:

1. **Pre-execution discovery** — STEP 0 must tell the agent to look for
   unregistered files and any `*_CHECKPOINT.md` index before acting, so
   an interrupted task resumes instead of restarting from scratch.
2. **Large deliverable protocol** — for tasks likely to exceed the
   output-token cap, the agent must chunk its writes and maintain a
   checkpoint index so work is never lost to a single oversized reply.

These are regression anchors for the 2026-04-20 OUTPUT_TOKEN_LIMIT fix:
the prompt-side contract must stay aligned with the retry-side guidance
in error_classifier.py.
"""

from __future__ import annotations

import pytest

from src.orchestrator.worker_prompt import build_worker_prompt, format_task_brief


def _minimal_task(**overrides):
    """Task data dict with all required fields set to usable defaults."""
    base = {
        "task_id": "00000000-0000-0000-0000-000000000001",
        "readable_id": "FCB-001.T92",
        "title": "Build something large",
        "status": "ready",
        "rework_count": 0,
        "recent_activities": [],
        "artifacts": [],
        "reviewer": "",
        "assigned_agent": "automation-script-developer",
        "brief": {
            "goal": "Produce X",
            "context": "Because Y",
            "inputs": "/workspace/inputs/foo.md",
            "output_format": "One Python script + README",
            "acceptance_criteria": ["Script runs without errors"],
            "allowed_tools": ["Read", "Write", "Bash"],
            "required_skills": [],
            "risks_and_edge_cases": "None",
            "verification_steps": "python script.py",
        },
    }
    base.update(overrides)
    return base


def _large_output_task(**overrides):
    """Task whose output_format trips the large-deliverable heuristic (T5.3.4)."""
    base = _minimal_task()
    base["brief"]["output_format"] = (
        "A multi-section research report document with 5+ sections"
    )
    base.update(overrides)
    return base


class TestLargeDeliverableProtocol:
    """The protocol is emitted in FULL only when the brief's output_format
    suggests a large/multi-part artifact (T5.3.4); small outputs get a
    one-line pointer instead of the ~300-token block."""

    def test_section_header_present_for_large_output(self):
        prompt = build_worker_prompt(_large_output_task())
        assert "LARGE DELIVERABLE PROTOCOL" in prompt

    def test_small_output_gets_pointer_not_full_protocol(self):
        # The minimal task's output_format ("One Python script + README") is
        # small — no full protocol, just the short "## Output size" pointer.
        prompt = build_worker_prompt(_minimal_task())
        assert "LARGE DELIVERABLE PROTOCOL" not in prompt
        assert "## Output size" in prompt

    def test_mentions_token_budget_cue(self):
        prompt = build_worker_prompt(_large_output_task())
        assert "5000 tokens" in prompt

    def test_review_mode_large_output_gets_pointer_not_full_protocol(self):
        # T5.3.4 (re-review F-5.3.4-B): a REVIEW dispatch carries the SAME
        # large output_format as the executor task, but the reviewer must NOT
        # receive the full produce-the-deliverable protocol — it assesses, it
        # doesn't produce. Mode-gated → pointer only.
        prompt = format_task_brief(_large_output_task(status="review"))
        assert "LARGE DELIVERABLE PROTOCOL" not in prompt
        assert "## Output size" in prompt

    def test_triage_mode_large_output_gets_pointer_not_full_protocol(self):
        prompt = format_task_brief(_large_output_task(status="blocked"))
        assert "LARGE DELIVERABLE PROTOCOL" not in prompt

    def test_instructs_chunked_writes(self):
        prompt = build_worker_prompt(_large_output_task())
        lower = prompt.lower()
        assert "chunk" in lower
        assert "`Write`" in prompt

    def test_mandates_checkpoint_file(self):
        prompt = build_worker_prompt(_large_output_task())
        assert "fcb-001_t92_CHECKPOINT.md" in prompt
        assert "done" in prompt.lower() and "pending" in prompt.lower()

    def test_checkpoint_file_not_registered_as_artifact(self):
        prompt = build_worker_prompt(_large_output_task())
        assert "do NOT call `save_file` on it" in prompt

    def test_short_assistant_messages_rule(self):
        prompt = build_worker_prompt(_large_output_task())
        assert "Short assistant messages" in prompt


class TestBlockerTemplateNotDuplicatedInTaskPrompt:
    """T5.3.4 — the full ESCALATED template lives in the shared work rules
    (one emission); the task prompt only CROSS-REFERENCES it."""

    def test_task_prompt_cross_references_not_duplicates(self):
        prompt = build_worker_prompt(_minimal_task())
        # The blocker section exists...
        assert "If You Need Clarification or Hit a Real Blocker" in prompt
        # ...but the full template body is NOT restated here.
        assert "What I already tried:" not in prompt
        assert "blocker protocol in your work rules" in prompt

    def test_full_template_lives_in_shared_rules(self):
        from src.config_sync.claude_md_content import SHARED_AGENT_WORK_RULES
        assert "What I already tried:" in SHARED_AGENT_WORK_RULES


class TestStep0ChecksForCheckpoint:
    """STEP 0.3 (glob) and BRANCH B (partial work) must know about the
    CHECKPOINT.md convention — otherwise the protocol is write-only and
    a retry never resumes properly."""

    def test_glob_patterns_include_checkpoint(self):
        prompt = build_worker_prompt(_minimal_task())
        # STEP 0 always emits the slug glob (covers CHECKPOINT.md via the
        # trailing wildcard) + the generic "read CHECKPOINT.md first" prose,
        # regardless of output size (T5.3.4 made only the full PROTOCOL
        # conditional, not STEP 0's resume awareness).
        assert "fcb-001_t92*" in prompt
        assert "CHECKPOINT.md" in prompt

    def test_large_output_names_slug_checkpoint_file(self):
        # The slug-specific CHECKPOINT path is named when the full protocol
        # renders (large output).
        prompt = build_worker_prompt(_large_output_task())
        assert "fcb-001_t92_CHECKPOINT.md" in prompt

    def test_branch_b_reads_checkpoint_first(self):
        # BRANCH B only fires when activity is present but no artifacts
        # are registered — the classic "partial work from a crash" case.
        task = _minimal_task(
            recent_activities=[
                {"event_type": "checkpoint", "content": "Drafted chapter 1"},
            ],
            artifacts=[],
        )
        prompt = build_worker_prompt(task)
        assert "BRANCH B" in prompt
        # The crucial instruction: read CHECKPOINT.md FIRST, resume from
        # the first pending chunk, do NOT redo done chunks.
        assert "Read` it FIRST" in prompt
        assert "pending" in prompt.lower()
        assert "do NOT redo" in prompt

    def test_step_0_mentions_checkpoint_priority(self):
        prompt = build_worker_prompt(_minimal_task())
        # STEP 0.3 should flag checkpoint files as the highest-priority
        # recovery signal — an agent that skips this re-does work.
        assert "READ IT FIRST" in prompt


class TestProtocolConsistencyWithErrorClassifier:
    """If these diverge, the retry message tells the agent one thing and
    the system prompt tells it another — that's what failed users see as
    'the AI ignored the guidance'."""

    def test_retry_guidance_references_checkpoint_md(self):
        from src.orchestrator.error_classifier import classify_error

        r = classify_error("API Error: exceeded the 32000 output token maximum")
        # Both the prompt section and the retry guidance point at the
        # same *_CHECKPOINT.md convention — a mismatch here would send
        # the agent hunting for a different filename after a failure.
        assert "CHECKPOINT" in r.guidance


class TestFreshTaskStillGetsProtocol:
    """A fresh task (Branch A) has no activity and no artifacts. Even so,
    it must receive the Large Deliverable Protocol — that's the point of
    making it preventive, not reactive."""

    def test_branch_a_large_output_includes_protocol(self):
        # A fresh task with a large output_format gets the full protocol
        # preventively (T5.3.4). A small fresh task gets the pointer instead
        # (covered by TestLargeDeliverableProtocol).
        prompt = build_worker_prompt(_large_output_task())
        assert "BRANCH A" in prompt
        assert "LARGE DELIVERABLE PROTOCOL" in prompt


class TestFormatTaskBriefDirect:
    """Cover format_task_brief directly — build_worker_prompt delegates
    to it, so changes to the rendering logic are caught here."""

    def test_returns_non_empty_string(self):
        out = format_task_brief(_minimal_task())
        assert isinstance(out, str)
        assert len(out) > 500

    def test_contains_task_id_and_title(self):
        out = format_task_brief(_minimal_task())
        assert "FCB-001.T92" in out
        assert "Build something large" in out


class TestPerWorkstreamOutputDir:
    """The worker prompt must thread workstream_short_code (and optional
    scope_readable_id) into the output-directory references it gives the
    agent. Without this, all tasks' deliverables collide in
    /workspace/outputs/ and the user can't find anything."""

    def test_workstream_only_yields_workstream_subdir(self):
        out = format_task_brief(
            _minimal_task(workstream_short_code="WR")
        )
        assert "/workspace/outputs/WR" in out
        # Default Glob patterns should be scoped, not flat.
        assert "/workspace/outputs/WR/" in out

    def test_workstream_plus_scope_yields_nested_subdir(self):
        out = format_task_brief(
            _minimal_task(
                workstream_short_code="WR",
                scope_readable_id="WR-003.S01",
            )
        )
        assert "/workspace/outputs/WR/WR-003.S01" in out

    def test_missing_short_code_falls_back_to_legacy_flat_path(self):
        """When the orchestrator (legacy version) doesn't provide
        workstream_short_code, the prompt must still produce a valid
        path — falling back to the flat /workspace/outputs/ root."""
        out = format_task_brief(_minimal_task())  # no workstream_short_code
        # Must mention the legacy flat path so the agent has somewhere
        # to write.
        assert "/workspace/outputs" in out

    def test_checkpoint_path_uses_per_workstream_dir(self):
        """Regression for QA finding E.1 — the Large Deliverable
        Protocol's checkpoint index must live under the same per-
        workstream output_dir the worker writes deliverables to.
        Otherwise STEP 0 globs the wrong directory on retry."""
        out = format_task_brief(
            _minimal_task(workstream_short_code="WR")
        )
        # The CHECKPOINT.md path must be inside the workstream dir,
        # NOT the flat /workspace/outputs/ root.
        assert "/workspace/outputs/WR/" in out
        # Ensure no remaining literal flat checkpoint reference.
        assert "/workspace/outputs/fcb_001_t92_CHECKPOINT.md" not in out


# ─── Triage-mode prompt rendering (C3 fix) ────────────────────────────
# When the dispatcher hands a blocked task to the MA, the worker prompt
# MUST render the triage-specific instructions instead of the normal
# "submit via update_status('review')" closing block. Otherwise the
# MA would arrive with execution instructions on a task the playbook
# tells it never to execute.


class TestTriageModePrompt:
    def test_blocked_task_renders_triage_block_instead_of_submit_block(self):
        prompt = build_worker_prompt(
            _minimal_task(status="blocked", assigned_agent="manager-assistant"),
        )
        assert "DOCUMENT-AND-ESCALATE" in prompt, (
            "Blocked-task prompt must announce the triage role"
        )
        # The normal execution closing instruction must NOT appear —
        # otherwise the MA sees conflicting guidance.
        assert "Calling `update_status('review')` is the LAST" not in prompt

    def test_blocked_task_lists_three_resolution_paths(self):
        prompt = build_worker_prompt(
            _minimal_task(status="blocked", assigned_agent="manager-assistant"),
        )
        assert "answer-and-stop" in prompt
        assert "helper task" in prompt.lower()
        # Path D is the user-facing escalation. The previous prompt
        # called the tool ``propose_action`` (which is a backend
        # action verb, not an MCP tool name) — see audit; renamed to
        # the actual worker MCP tool ``escalate_blocker``.
        assert "escalate_blocker" in prompt

    def test_blocked_task_forbids_status_flip(self):
        """The prompt must explicitly forbid update_status and
        move_task(blocked → ready) on the current task. The MCP
        server enforces this too, but the prompt is the first line
        of defence (the MA reads it before any tool call)."""
        prompt = build_worker_prompt(
            _minimal_task(status="blocked", assigned_agent="manager-assistant"),
        )
        lower = prompt.lower()
        assert "do not call `update_status`" in lower
        assert "blocked → ready" in prompt

    def test_ready_task_still_renders_submit_block(self):
        """Regression guard: the triage rendering must NOT leak into
        regular execution tasks."""
        prompt = build_worker_prompt(
            _minimal_task(status="ready", assigned_agent="analyst"),
        )
        assert "DOCUMENT-AND-ESCALATE" not in prompt
        assert "How to Submit Your Work" in prompt


class TestWorkstreamClaudeMdAwareness:
    """Worker prompts must explicitly point at the workstream CLAUDE.md
    file. Claude CLI's cwd auto-discovery only walks ``/workspace/agents/
    <name>/``, so the workstream conventions live in a parallel path the
    CLI never visits on its own — the prompt is the only way the worker
    learns about them."""

    def test_workstream_claude_md_path_referenced_in_header(self):
        prompt = build_worker_prompt(
            _minimal_task(
                workstream_context={
                    "name": "Recruitment",
                    "description": "Hire engineers",
                    "goals": "10 hires by Q2",
                },
            ),
        )
        assert "/workspace/workstreams/recruitment/CLAUDE.md" in prompt
        # Header must explain WHY to read it, not just where it is.
        assert "READ THIS BEFORE STARTING" in prompt

    def test_step_0_0_runs_read_on_workstream_claude_md(self):
        prompt = build_worker_prompt(
            _minimal_task(
                workstream_context={
                    "name": "Recruitment",
                    "description": "",
                    "goals": "",
                },
            ),
        )
        assert "### 0.0 — Read workstream conventions FIRST" in prompt
        # Step 0.0 must literally name the Read tool — the agent ignores
        # generic "look at the file" instructions more often than tool-
        # named ones.
        assert "Run `Read` on" in prompt

    def test_no_workstream_context_skips_step_0_0(self):
        """Tasks without workstream context (manual/orphan tasks) must
        not get a dangling 0.0 step pointing at a path that doesn't
        exist."""
        prompt = build_worker_prompt(_minimal_task())
        assert "### 0.0 — Read workstream conventions" not in prompt


class TestPriorityAndScopeStateInHeader:
    """The worker's first read of the prompt should answer 'how hot is
    this task?' and 'is this part of an executing scope or running
    alone?' — both inform pacing and cross-task awareness."""

    def test_priority_line_present_with_hint(self):
        prompt = build_worker_prompt(_minimal_task(priority="high"))
        assert "Priority: **high**" in prompt

    def test_urgent_priority_renders_urgent_hint(self):
        prompt = build_worker_prompt(_minimal_task(priority="urgent"))
        # The hint must be loud enough that the worker notices.
        assert "Priority: **urgent**" in prompt

    def test_priority_defaults_to_medium_when_missing(self):
        prompt = build_worker_prompt(_minimal_task())
        assert "Priority: **medium**" in prompt

    def test_priority_hint_carries_no_emoji(self):
        """W5-P3-H4: no-emoji project directive. The priority hint
        text must convey urgency via the LITERAL word ('URGENT',
        'High', 'Medium', 'Low') plus its explanation — no fire /
        circle / cross glyphs in the worker prompt."""
        for priority in ("urgent", "high", "medium", "low"):
            prompt = build_worker_prompt(_minimal_task(priority=priority))
            for emoji in ("🔥", "🟠", "🟢", "⚪", "🔴", "🟡"):
                assert emoji not in prompt, (
                    f"priority={priority} still leaks {emoji!r} into "
                    "the worker prompt — see W5-P3-H4"
                )
        # Spot-check that the literal-word fallback still ships the
        # urgency cue.
        urgent = build_worker_prompt(_minimal_task(priority="urgent"))
        assert "URGENT" in urgent
        assert "drop all interruptable work" in urgent

    def test_scope_state_line_appears_when_provided(self):
        prompt = build_worker_prompt(
            _minimal_task(
                scope_readable_id="WR-003.S02",
                scope_name="Auth epic",
                scope_state="executing",
            ),
        )
        assert "Scope state:" in prompt
        assert "`executing`" in prompt

    def test_scope_state_absent_when_task_has_no_scope(self):
        prompt = build_worker_prompt(_minimal_task())
        assert "Scope state:" not in prompt


class TestCompletionFence:
    """T4.3.5 — COMPLETED.json marker + STEP-0 short-circuit so a failed final
    move_task doesn't trigger a full re-execution."""

    def test_submission_writes_completion_marker(self):
        prompt = build_worker_prompt(_minimal_task())
        assert "Completion fence" in prompt
        assert "COMPLETED.json" in prompt
        assert "fcb-001_t92" in prompt  # marker keyed to the task slug

    def test_fresh_task_has_branch0_short_circuit(self):
        prompt = build_worker_prompt(_minimal_task())
        assert "BRANCH 0 (ALREADY COMPLETE?)" in prompt
        assert "redo the work" in prompt
        # Fresh attempt → marker carries rework_count 0.
        assert '"rework_count": 0' in prompt

    def test_rework_renders_branch0_but_gated_on_attempt(self):
        # T4.3.5 (hardened): BRANCH 0 now renders on rework TOO so a
        # reworked-then-failed-to-submit task is also protected from a full
        # re-execution — BUT the short-circuit is gated on the marker's
        # rework_count matching THIS attempt, so a stale pre-rework marker
        # can't falsely short-circuit a genuine rework.
        prompt = build_worker_prompt(_minimal_task(rework_count=1))
        assert "BRANCH 0 (ALREADY COMPLETE?)" in prompt
        assert "BRANCH D (REWORK)" in prompt
        # The gate + the written marker both reference attempt #1.
        assert "rework_count` equals **1**" in prompt
        assert '"rework_count": 1' in prompt


class TestLearningsLoop:
    """BEST-01: the durable per-workstream learnings loop across the three
    prompt surfaces (worker STEP 0.0b, reviewer FAIL append, Planner scope_plan)."""

    def _ws_task(self, **overrides):
        return _minimal_task(
            workstream_context={"name": "Website Redesign"}, **overrides
        )

    def test_worker_step_0_0b_reads_learnings(self):
        prompt = format_task_brief(self._ws_task())
        assert "0.0b" in prompt
        assert "/workspace/workstreams/website-redesign/learnings.md" in prompt

    def test_no_learnings_step_without_workstream_context(self):
        # A task with no workstream context can't resolve a learnings path.
        prompt = format_task_brief(_minimal_task())
        assert "learnings.md" not in prompt

    def test_reviewer_appends_learning_on_fail(self):
        review = self._ws_task(status="review", assigned_agent="auditor")
        prompt = build_worker_prompt(review)
        assert "record a LEARNING" in prompt
        assert "/workspace/workstreams/website-redesign/learnings.md" in prompt
        # Best-effort + append-not-overwrite are load-bearing instructions.
        assert "do NOT overwrite" in prompt or "do NOT overwrite)" in prompt \
            or "Append (do NOT overwrite" in prompt
        assert "best-effort" in prompt.lower()

    def test_manager_assistant_reviewer_has_no_designated_block(self):
        # The MA reviews via its Board-Operator playbook, not this path; the
        # learnings step rides on the designated-reviewer block, so the MA
        # dispatch must not carry it (avoids a double surface).
        review = self._ws_task(status="review", assigned_agent="manager-assistant")
        prompt = build_worker_prompt(review)
        assert "record a LEARNING" not in prompt


class TestReviewerRunsChecks:
    """BEST-03: the reviewer must actually RUN command verification steps and
    record the exit code as evidence — not accept 'looks correct'."""

    def test_reviewer_must_run_commands_and_record_exit_code(self):
        review = _minimal_task(
            status="review", assigned_agent="auditor",
            workstream_context={"name": "WS"},
        )
        prompt = build_worker_prompt(review)
        low = prompt.lower()
        assert "exit code" in low
        assert "must actually run" in low or "you must actually run" in low
        # The weak "if applicable" phrasing must be gone from step 6.
        assert "Run any verification steps if applicable" not in prompt
