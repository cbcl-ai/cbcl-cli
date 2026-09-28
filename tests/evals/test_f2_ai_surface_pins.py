"""Follow-up package f2-ai-surfaces: pins for the prompt corrections.

Each test names the item it closes and checks the text the model actually
reads (rendered prompts and playbooks), so a later edit cannot quietly
reopen the gap.
"""

from __future__ import annotations

from src.orchestrator.worker_prompt import build_worker_prompt


def _norm(text: str) -> str:
    return " ".join(text.split())


# ── Item 16 (U06): a later comment matching the feedback is not the return ──


def _task(**overrides):
    base = {
        "task_id": "00000000-0000-0000-0000-000000000016",
        "readable_id": "F2-016.T01",
        "title": "Follow-up task",
        "status": "in_progress",
        "rework_count": 1,
        "brief": {
            "goal": "Deliver the report.",
            "inputs": "The user's request.",
            "acceptance_criteria": ["Report exists"],
            "verification_steps": "Execution checks: open it.",
        },
        "workstream_short_code": "F2",
        "workstream_context": {"name": "Follow Up", "short_code": "F2"},
        "assigned_agent": "dev",
        "reviewer": "auditor",
    }
    base.update(overrides)
    return base


def _feedback_block(task: dict) -> str:
    text = build_worker_prompt(task)
    return text.split("<review_feedback>")[1].split("</review_feedback>")[0]


def _comment(actor: str, content: str, at: str, details=None) -> dict:
    return {
        "event_type": "comment",
        "actor": actor,
        "content": content,
        "details": details or {},
        "created_at": at,
    }


def _fail(fixes: list[str]) -> dict:
    return {
        "overall": "fail",
        "rationale": "Criteria unmet.",
        "criteria": [
            {
                "criterion_index": 1,
                "name": "Report exists",
                "status": "fail",
                "evidence": "no file",
            }
        ],
        "required_fixes": fixes,
    }


def test_executor_escalation_matching_the_feedback_keeps_the_verdict():
    # Both status rows left the window; the rework feedback is the executor's
    # ESCALATED comment, which an executor can never write as a return.
    escalated = "ESCALATED (missing_credential): API_KEY not set."
    block = _feedback_block(
        _task(
            rework_feedback=escalated,
            recent_activities=[
                _comment(
                    "auditor",
                    "FAIL — see verdict.",
                    "2026-09-25T10:00:00",
                    _fail(["Write the report to outputs/report.md"]),
                ),
                _comment("dev", escalated, "2026-09-25T10:20:00"),
            ],
        )
    )
    assert "- Write the report to outputs/report.md" in block
    # The note is the reviewer's own comment, not the escalation.
    assert "FAIL — see verdict." in block
    assert escalated not in block


def test_user_comment_matching_the_feedback_keeps_the_verdict():
    # The Manager Assistant reviewed; its FAIL comment is a pinned human row,
    # and the user's later reply is the newest comment (the feedback).
    block = _feedback_block(
        _task(
            reviewer="manager-assistant",
            rework_feedback="Saved the key.",
            recent_activities=[
                _comment(
                    "manager-assistant",
                    "FAIL — see verdict.",
                    "2026-09-25T10:00:00",
                    _fail(["Name the output file"]),
                ),
                _comment("user", "Saved the key.", "2026-09-25T11:00:00"),
            ],
        )
    )
    assert "- Name the output file" in block
    assert "- Report exists — fail — no file" in block


def test_a_returner_comment_matching_the_feedback_still_decides():
    # A Manager comment can be a verdict-less override return: it still
    # decides, so the older FAIL is not shown as current (R21).
    override = "User wants the title changed"
    block = _feedback_block(
        _task(
            rework_count=2,
            rework_feedback=override,
            recent_activities=[
                _comment(
                    "auditor",
                    "FAIL — round one.",
                    "2026-09-25T09:00:00",
                    _fail(["Old fix"]),
                ),
                _comment("manager", override, "2026-09-25T10:00:00"),
            ],
        )
    )
    assert override in block
    assert "Old fix" not in block
    assert "Structured verdict (not repeated above)" not in block


def test_a_newer_override_beats_an_older_fail_behind_an_escalation():
    # f3 item 1: the feedback is the executor's escalation (not a return), and
    # a Manager override sits between it and an older round's FAIL. The
    # override is the newest return, so the round-one FAIL is not current.
    override = "Manager override: rename the report."
    escalated = "ESCALATED (missing_data): which title?"
    block = _feedback_block(
        _task(
            rework_count=2,
            rework_feedback=escalated,
            recent_activities=[
                _comment(
                    "auditor",
                    "FAIL — round one.",
                    "2026-09-25T09:00:00",
                    _fail(["Old fix"]),
                ),
                _comment("manager", override, "2026-09-25T10:00:00"),
                _comment("dev", escalated, "2026-09-25T10:30:00"),
            ],
        )
    )
    assert override in block
    assert "FAIL — round one." not in block
    assert "Old fix" not in block
    assert "Structured verdict (not repeated above)" not in block


def test_a_replaced_reviewers_newer_fail_beats_an_older_manager_comment():
    # f3 item 1: the FAIL came from the reviewer the task had before a
    # reassignment (no longer a returner by name); an older Manager comment
    # must not hide it.
    escalated = "ESCALATED (missing_data): which file?"
    block = _feedback_block(
        _task(
            reviewer="qa-lead",
            rework_feedback=escalated,
            recent_activities=[
                _comment("manager", "Priority raised.", "2026-09-25T08:00:00"),
                _comment(
                    "auditor",
                    "FAIL — see verdict.",
                    "2026-09-25T09:00:00",
                    _fail(["Write the report to outputs/report.md"]),
                ),
                _comment("dev", escalated, "2026-09-25T10:30:00"),
            ],
        )
    )
    assert "FAIL — see verdict." in block
    assert "- Write the report to outputs/report.md" in block
    assert "Priority raised." not in block


def _status(actor: str, old: str, new: str, at: str) -> dict:
    return {
        "event_type": "status_changed",
        "actor": actor,
        "content": f"{old} → {new}",
        "details": {"old_status": old, "new_status": new},
        "created_at": at,
    }


def _blocked_after_fail(executor: str, triage: str) -> list[dict]:
    """The Auditor's FAIL (its status row left the window), then the executor
    blocks, the Manager Assistant triages, and an approval resumes the task."""
    return [
        _comment(
            "auditor",
            "FAIL — see verdict.",
            "2026-09-25T09:00:00",
            _fail(["Write the report to outputs/report.md"]),
        ),
        _status("system", "ready", "in_progress", "2026-09-25T09:05:00"),
        _status(executor, "in_progress", "blocked", "2026-09-25T09:30:00"),
        _comment(
            executor,
            "ESCALATED (missing_data): the CSV is missing.",
            "2026-09-25T09:30:00",
        ),
        _comment("manager-assistant", triage, "2026-09-25T10:00:00"),
        _status("system", "blocked", "ready", "2026-09-25T11:00:00"),
    ]


def test_ma_executor_triage_note_is_not_the_return():
    # f4 item 2 (a): the Manager Assistant executed the task, so its own
    # triage note can never be the return; the Auditor's FAIL is current.
    triage = "Triage: waiting on the CSV from the user."
    block = _feedback_block(
        _task(
            assigned_agent="manager-assistant",
            status="ready",
            rework_feedback=triage,
            recent_activities=_blocked_after_fail("manager-assistant", triage),
        )
    )
    assert "- Write the report to outputs/report.md" in block
    assert "FAIL — see verdict." in block
    assert triage not in block


def test_ma_triage_after_a_block_is_not_the_return():
    # f4 item 2 (b): a triage comment written after an in-window status row
    # is newer than the last return, so it cannot be the return.
    triage = "Triage: the user is sending the CSV."
    block = _feedback_block(
        _task(
            assigned_agent="builder",
            status="ready",
            rework_feedback=triage,
            recent_activities=_blocked_after_fail("builder", triage),
        )
    )
    assert "- Write the report to outputs/report.md" in block
    assert "FAIL — see verdict." in block
    assert triage not in block


def test_ma_triage_between_the_fail_and_a_new_escalation_keeps_the_fail():
    # f4 item 9: a Manager Assistant triage comment between the FAIL and the
    # executor's next escalation is not a return; the FAIL stays current.
    escalated = "ESCALATED (missing_data): the CSV is still missing."
    activities = _blocked_after_fail("dev", "Triage: blocked on the CSV.") + [
        _status("system", "ready", "in_progress", "2026-09-25T11:05:00"),
        _status("dev", "in_progress", "blocked", "2026-09-25T11:30:00"),
        _comment("dev", escalated, "2026-09-25T11:30:00"),
    ]
    block = _feedback_block(
        _task(
            status="blocked",
            rework_feedback=escalated,
            recent_activities=activities,
        )
    )
    assert "- Write the report to outputs/report.md" in block
    assert "FAIL — see verdict." in block
    assert "Triage: blocked on the CSV." not in block


def test_manager_override_before_the_dispatch_still_decides():
    # R21 with the dispatch row in the window: the override comment precedes
    # every status row, so it is still the return and hides the older FAIL.
    override = "User wants the title changed"
    block = _feedback_block(
        _task(
            rework_count=2,
            rework_feedback=override,
            recent_activities=[
                _comment(
                    "auditor",
                    "FAIL — round one.",
                    "2026-09-25T09:00:00",
                    _fail(["Old fix"]),
                ),
                _comment("manager", override, "2026-09-25T10:00:00"),
                _status("system", "ready", "in_progress", "2026-09-25T10:01:00"),
            ],
        )
    )
    assert override in block
    assert "Old fix" not in block


# ── Item 18: the bounce-cap exception belongs to the current block ─────────


def _ma() -> str:
    from src.config_sync.claude_md_templates._system_agents import (
        MANAGER_ASSISTANT_CLAUDE_MD,
    )

    return _norm(MANAGER_ASSISTANT_CLAUDE_MD)


def test_bounce_cap_exception_is_scoped_to_the_current_block():
    from src._agent_image.mcp_tool_server import TRIAGE_PATHS_TEXT
    from tests.evals._prompt_composition import (
        compose_worker_session,
        composed_manager_norm,
    )

    triage = _norm(compose_worker_session("ma_triage").text)
    assert (
        "the newest system comment since the task last entered blocked is "
        '"Auto-unblock refused"' in triage
    )
    after = _ma().split("### After an approved escalation")[1].split("###")[0]
    assert (
        '**"Auto-unblock refused"** (the bounce cap) as the newest system '
        "comment since the task last entered blocked:" in after
    )
    assert '"Auto-unblock refused" posted since the task last entered blocked' in _norm(
        TRIAGE_PATHS_TEXT
    )
    invariant = (
        composed_manager_norm("default_workstream")
        .split("4. **Blocked tasks never spontaneously auto-unblock.**")[1]
        .split("5. **Action requests are deduped")[0]
    )
    assert (
        'At the cap ("Auto-unblock refused" since the task last entered '
        "blocked) never approve its blocker requests again." in invariant
    )
    # Every other surface names the current block, never a bare refusal.
    assert 'the bounce cap\'s "Auto-unblock refused"' not in _ma()
    assert _ma().count('the current block\'s bounce-cap "Auto-unblock refused"') == 4


# ── Item 19: a bounce_cap_user_decision refusal may still be pending ───────


def test_ma_reports_a_pending_or_made_bounce_cap_decision():
    ma = _ma()
    assert (
        "a `bounce_cap_user_decision` refusal means a person decides it "
        "(pending in their Inbox) or has decided — report that; never retry "
        "again." in ma
    )
    assert "means a person decided —" not in ma


# ── Item 26: guarded write-backs name the read_receipt they need ───────────


def test_read_then_write_playbooks_name_the_read_receipt():
    from src._agent_image._mcp.read_receipts import GUARDED_WRITES
    from src.config_sync.claude_md_templates._manager_modules import (
        MANAGER_PROGRAM_PROCEDURES,
    )
    from src.config_sync.claude_md_templates._system_agents import (
        FLOW_ARCHITECT_CLAUDE_MD,
        PLANNER_CLAUDE_MD,
    )

    # The playbooks name exactly the guarded pairs the MCP server enforces.
    assert GUARDED_WRITES["update_spec"][0] == "get_spec"
    assert GUARDED_WRITES["update_execution_plan"][0] == "get_execution_plan"
    assert GUARDED_WRITES["update_flow_graph"][0] == "get_flow_graph"
    planner = _norm(PLANNER_CLAUDE_MD)
    assert (
        "After a `get_spec` / `get_execution_plan` read, pass the "
        "`read_receipt` that ends that complete result to `update_spec` / "
        "`update_execution_plan`." in planner
    )
    architect = _norm(FLOW_ARCHITECT_CLAUDE_MD)
    assert (
        "After a `get_flow_graph` read, `update_flow_graph` must pass the "
        "`read_receipt` that ends that complete result." in architect
    )
    stuck = _norm(MANAGER_PROGRAM_PROCEDURES).split(
        "**Scope stuck in `verifying` (escalated).**"
    )[1]
    assert (
        "send the complete plan back via `update_execution_plan`" in stuck
        and "pass the read's `read_receipt`, valid only this turn" in stuck
    )
