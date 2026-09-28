"""Fix wave 4, package fw4-prompts: pins for the prompt corrections.

Each test names the finding it closes. They check the surface the model
actually reads (rendered prompts, projected tool results) rather than
source text, so a later edit cannot quietly reopen the gap.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from src.orchestrator.worker_prompt import build_worker_prompt


def _norm(text: str) -> str:
    return " ".join(text.split())


def _task(**overrides):
    base = {
        "task_id": "00000000-0000-0000-0000-000000000004",
        "readable_id": "FW-004.T01",
        "title": "Fix-wave-4 task",
        "status": "in_progress",
        "rework_count": 1,
        "brief": {
            "goal": "Deliver the report.",
            "inputs": "The user's request.",
            "acceptance_criteria": ["Report exists"],
            "verification_steps": "Execution checks: open it.",
        },
        "workstream_short_code": "FW",
        "workstream_context": {"name": "Fix Wave", "short_code": "FW"},
        "assigned_agent": "dev",
        "reviewer": "auditor",
    }
    base.update(overrides)
    return base


def _feedback_block(task: dict) -> str:
    text = build_worker_prompt(task)
    return text.split("<review_feedback>")[1].split("</review_feedback>")[0]


# ── R19: the Auditor can read the run evidence its rule asks for ───────────


async def _posted_script_activity() -> dict:
    """The activity row the host runner posts for a finished task script."""
    from src.scripts.script_notifier import notify_completion

    router = AsyncMock()
    await notify_completion(
        ws=None, router=router,
        script_name="enrich", exec_id="exec-2026-09-25T10-00-00-abc123",
        task_id="00000000-0000-0000-0000-000000000004", triggered_by="dev",
        started_at_iso="2026-09-25T10:00:00Z", process_returncode=0,
        status="completed", duration=3.0, error_message=None, progress={},
    )
    event = next(
        call.args[0] for call in router.publish_event.call_args_list
        if call.args[0]["type"] == "task_activity"
    )
    return {key: event[key] for key in ("event_type", "actor", "content", "details")}


async def test_script_run_evidence_survives_the_projected_task_read():
    from src._agent_image._mcp.transforms import project_response

    row = await _posted_script_activity()
    lean = project_response("get_task_detail", {"recent_activities": [row]})
    shown = lean["recent_activities"][0]
    assert shown["details"] == {
        "execution_id": "exec-2026-09-25T10-00-00-abc123",
        "status": "completed",
        "exit_code": 0,
    }
    assert "exec-2026-09-25T10-00-00-abc123" in shown["content"]
    assert "exit code 0" in shown["content"]
    # Only script_completed rows keep run fields; the generic list is unchanged.
    other = dict(row, event_type="checkpoint")
    lean = project_response("get_task_detail", {"recent_activities": [other]})
    assert lean["recent_activities"][0]["details"] == {}


async def test_script_run_evidence_survives_the_review_prompt_window():
    row = await _posted_script_activity()
    text = _norm(build_worker_prompt(_task(
        status="review", rework_count=0, recent_activities=[row],
    )))
    window = text.split("<activity>")[1].split("</activity>")[0]
    assert "exec-2026-09-25T10-00-00-abc123" in window
    assert "exit code 0" in window


async def test_failed_script_run_names_its_status_and_exit_code():
    from src.scripts.script_notifier import notify_completion

    router = AsyncMock()
    await notify_completion(
        ws=None, router=router, script_name="enrich", exec_id="exec-9",
        task_id="t-1", triggered_by="dev", started_at_iso="2026-09-25T10:00:00Z",
        process_returncode=None, status="timed_out", duration=5.0,
        error_message="too slow", progress={},
    )
    content = next(
        call.args[0]["content"] for call in router.publish_event.call_args_list
        if call.args[0]["type"] == "task_activity"
    )
    assert content.startswith("Script 'enrich' run exec-9 ended with status timed_out")
    assert "exit code unknown" in content


def test_auditor_rule_names_host_records_the_reviewer_receives():
    from src.config_sync.claude_md_templates._system_agents import AUDITOR_CLAUDE_MD

    step = _norm(AUDITOR_CLAUDE_MD).split("6. **Test evidence**")[1].split("7. **")[0]
    assert "host record on THIS task: its `script_completed` activity" in step
    assert "`run <id> … exit code <n>`; `get_my_brief` has them in `details`" in step
    # The host ledger list the review prompt renders under this heading.
    assert '"Recorded script executions" list' in step
    assert "No matching host record = FAIL" in step
    assert "never proof of the exit" in step
    heading = _norm(build_worker_prompt(_task(
        status="review", rework_count=0,
        script_handoff_results=[{"execution_id": "exec-1", "state": "completed"}],
    )))
    assert "## Recorded script executions — inspect receipts" in heading
    assert "Execution exec-1: completed" in heading


# ── R21: an older FAIL verdict is not shown as the current one ─────────────


def _fail_verdict(content: str, fixes: list[str]) -> dict:
    return {
        "event_type": "comment",
        "actor": "auditor",
        "content": content,
        "details": {
            "overall": "fail",
            "rationale": "Criteria unmet.",
            "criteria": [{"criterion_index": 1, "name": "Report exists",
                          "status": "fail", "evidence": "no file"}],
            "required_fixes": fixes,
        },
    }


def test_verdictless_return_does_not_replay_an_older_fail_verdict():
    override = "User wants the title changed"
    block = _feedback_block(_task(
        rework_count=2,
        rework_feedback=override,
        recent_activities=[
            _fail_verdict("FAIL — see verdict.", ["Fix A"]),
            {"event_type": "comment", "actor": "manager", "content": override,
             "details": None},
        ],
    ))
    assert override in block
    assert "Structured verdict (not repeated above)" not in block
    assert "Fix A" not in block


def test_verdict_on_the_returning_comment_is_still_shown():
    block = _feedback_block(_task(
        rework_feedback="FAIL — see verdict.",
        recent_activities=[
            _fail_verdict("FAIL — round one.", ["Old fix"]),
            _fail_verdict("FAIL — see verdict.", ["Current fix"]),
        ],
    ))
    assert "- Current fix" in block
    assert "Old fix" not in block


# ── R22: long evidence keeps its tail and says text was left out ───────────


def test_long_verdict_evidence_is_marked_and_keeps_its_tail():
    evidence = "setup " * 500 + "FINAL: AssertionError at line 42"
    activity = _fail_verdict("FAIL — see verdict.", ["Fix it"])
    activity["details"]["criteria"][0]["evidence"] = evidence
    block = _feedback_block(_task(
        rework_feedback="FAIL — see verdict.", recent_activities=[activity],
    ))
    assert "FINAL: AssertionError at line 42" in block
    assert "characters of the reviewer's evidence omitted here" in block
    short = _fail_verdict("FAIL — see verdict.", ["Fix it"])
    assert "omitted here" not in _feedback_block(_task(
        rework_feedback="FAIL — see verdict.", recent_activities=[short],
    ))


# ── R23: the MA playbook's summaries match its current paths and modes ─────


def test_ma_summaries_match_path_a_and_the_execute_mode():
    from src.config_sync.claude_md_content import MANAGER_ASSISTANT_CLAUDE_MD

    ma = _norm(MANAGER_ASSISTANT_CLAUDE_MD)
    triage = ma.split("- **`triage`**")[1].split("## Hard Rules")[0]
    assert "answer + `escalate_blocker` approval request" in triage
    assert "comment + answer" not in ma
    assert "with no agent" not in ma
    assert "When you are in `execute` mode (a normal task assigned to YOU" in ma
