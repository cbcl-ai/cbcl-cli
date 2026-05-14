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


class TestLargeDeliverableProtocol:
    """Section must be present on every task prompt — its whole point is
    that the agent reads it BEFORE deciding how to structure output."""

    def test_section_header_present(self):
        prompt = build_worker_prompt(_minimal_task())
        assert "LARGE DELIVERABLE PROTOCOL" in prompt

    def test_mentions_token_budget_cue(self):
        # The ~5000-token threshold is the practical trigger. Verifying
        # the literal keeps the prompt consistent with its explanation
        # and with the retry guidance in error_classifier.
        prompt = build_worker_prompt(_minimal_task())
        assert "5000 tokens" in prompt

    def test_instructs_chunked_writes(self):
        prompt = build_worker_prompt(_minimal_task())
        lower = prompt.lower()
        assert "chunk" in lower
        # Must name the Write tool explicitly — agents ignore generic
        # "save to disk" instructions more often than tool-named ones.
        assert "`Write`" in prompt

    def test_mandates_checkpoint_file(self):
        prompt = build_worker_prompt(_minimal_task())
        # The checkpoint filename convention is {readable_slug}_CHECKPOINT.md.
        # Slug is lowercase with dots replaced by underscores.
        assert "fcb-001_t92_CHECKPOINT.md" in prompt
        assert "done" in prompt.lower() and "pending" in prompt.lower()

    def test_checkpoint_file_not_registered_as_artifact(self):
        # Explicit guardrail — we don't want every task polluting the
        # artifact feed with a working index file.
        prompt = build_worker_prompt(_minimal_task())
        assert "do NOT call `save_file` on it" in prompt

    def test_short_assistant_messages_rule(self):
        prompt = build_worker_prompt(_minimal_task())
        # The behavioural root cause of the bug: long assistant replies.
        # Prompt must explicitly push work OFF the assistant turn.
        assert "Short assistant messages" in prompt


class TestStep0ChecksForCheckpoint:
    """STEP 0.3 (glob) and BRANCH B (partial work) must know about the
    CHECKPOINT.md convention — otherwise the protocol is write-only and
    a retry never resumes properly."""

    def test_glob_patterns_include_checkpoint(self):
        prompt = build_worker_prompt(_minimal_task())
        # Pattern must be emitted as part of the glob instructions.
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

    def test_branch_a_prompt_includes_protocol(self):
        prompt = build_worker_prompt(_minimal_task())
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
        assert "propose_action" in prompt

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
