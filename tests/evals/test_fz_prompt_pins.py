"""Fix package fz-prompts: pins for the prompt corrections.

Each test names the finding it closes and checks the text the model
actually reads (rendered prompts and playbooks), so a later edit cannot
quietly reopen the gap.
"""

from __future__ import annotations

from src.orchestrator.worker_prompt import build_worker_prompt


def _norm(text: str) -> str:
    return " ".join(text.split())


# ── U06: the rework prompt shows the newest review return's FAIL verdict ──


def _task(**overrides):
    base = {
        "task_id": "00000000-0000-0000-0000-000000000006",
        "readable_id": "FZ-006.T01",
        "title": "Fix-zone task",
        "status": "in_progress",
        "rework_count": 1,
        "brief": {
            "goal": "Deliver the report.",
            "inputs": "The user's request.",
            "acceptance_criteria": ["Report exists"],
            "verification_steps": "Execution checks: open it.",
        },
        "workstream_short_code": "FZ",
        "workstream_context": {"name": "Fix Zone", "short_code": "FZ"},
        "assigned_agent": "dev",
        "reviewer": "auditor",
    }
    base.update(overrides)
    return base


def _feedback_block(task: dict) -> str:
    text = build_worker_prompt(task)
    return text.split("<review_feedback>")[1].split("</review_feedback>")[0]


def _moved(actor: str, old: str, new: str, at: str) -> dict:
    return {
        "event_type": "status_changed", "actor": actor,
        "content": f"Moved from {old} to {new}",
        "details": {"old_status": old, "new_status": new}, "created_at": at,
    }


def _comment(actor: str, content: str, at: str, details=None) -> dict:
    return {
        "event_type": "comment", "actor": actor, "content": content,
        "details": details or {}, "created_at": at,
    }


def _fail(fixes: list[str]) -> dict:
    return {
        "overall": "fail",
        "rationale": "Criteria unmet.",
        "criteria": [{"criterion_index": 1, "name": "Report exists",
                      "status": "fail", "evidence": "no file"}],
        "required_fixes": fixes,
    }


def _review_return(actor: str, content: str, at: str, verdict=None) -> list[dict]:
    """The two rows one review return writes; they share ``created_at``."""
    return [
        _moved(actor, "review", "ready", at),
        _comment(actor, content, at, verdict),
    ]


def test_return_block_triage_resume_keeps_the_current_verdict():
    triage = "Triage: needs API_KEY from the user."
    block = _feedback_block(_task(
        rework_feedback=triage,
        recent_activities=[
            *_review_return("auditor", "FAIL — see verdict.", "2026-09-25T10:00:00",
                            _fail(["Write the report to outputs/report.md"])),
            _moved("dev", "ready", "in_progress", "2026-09-25T10:05:00"),
            _moved("dev", "in_progress", "blocked", "2026-09-25T10:20:00"),
            _comment("dev", "ESCALATED (missing_credential): API_KEY not set.",
                     "2026-09-25T10:20:00"),
            _comment("manager-assistant", triage, "2026-09-25T10:30:00"),
            _comment("user", "Saved the key.", "2026-09-25T11:00:00"),
            _moved("manager", "blocked", "ready", "2026-09-25T11:01:00"),
        ],
    ))
    assert "- Write the report to outputs/report.md" in block
    # The rework note is the reviewer's return comment, not the triage note.
    assert "FAIL — see verdict." in block
    assert triage not in block


def test_fail_return_then_user_comment_keeps_the_current_verdict():
    later = "Also keep the old title please."
    block = _feedback_block(_task(
        rework_feedback=later,
        recent_activities=[
            *_review_return("auditor", "FAIL — see verdict.", "2026-09-25T10:00:00",
                            _fail(["Add the missing chart"])),
            _comment("user", later, "2026-09-25T10:10:00"),
        ],
    ))
    assert "- Add the missing chart" in block
    assert "- Report exists — fail — no file" in block


def test_return_comment_listed_before_its_status_row_still_counts():
    # Both rows of one move share created_at, so the window can list them
    # in either order.
    return_rows = _review_return(
        "auditor", "FAIL — see verdict.", "2026-09-25T10:00:00",
        _fail(["Add the missing chart"]),
    )
    block = _feedback_block(_task(
        rework_feedback="FAIL — see verdict.",
        recent_activities=list(reversed(return_rows)),
    ))
    assert "- Add the missing chart" in block


def test_newest_return_without_a_verdict_shows_no_older_verdict():
    override = "User wants the title changed"
    block = _feedback_block(_task(
        rework_count=2,
        rework_feedback="Triage note after the override.",
        recent_activities=[
            *_review_return("auditor", "FAIL — round one.", "2026-09-25T10:00:00",
                            _fail(["Old fix"])),
            _moved("dev", "in_progress", "review", "2026-09-25T10:40:00"),
            *_review_return("manager", override, "2026-09-25T11:00:00"),
            _comment("manager-assistant", "Triage note after the override.",
                     "2026-09-25T11:10:00"),
        ],
    ))
    assert override in block
    assert "Old fix" not in block
    assert "Structured verdict (not repeated above)" not in block


def test_resubmission_then_reviewer_block_shows_no_older_fail():
    # FAIL return, resubmission, then the reviewer blocks the task instead of
    # returning it. The block is the current review outcome: round one's
    # verdict must not come back as current (R21).
    escalated = "ESCALATED (missing_credential): need API_KEY to verify."
    rows = [
        *_review_return("auditor", "FAIL — round one.", "2026-09-25T10:00:00",
                        _fail(["Old fix already done"])),
        _moved("dev", "ready", "in_progress", "2026-09-25T10:05:00"),
        _moved("dev", "in_progress", "review", "2026-09-25T10:40:00"),
        _comment("dev", "Resubmitted with the fix.", "2026-09-25T10:40:00"),
        _moved("auditor", "review", "blocked", "2026-09-25T11:00:00"),
        _comment("auditor", escalated, "2026-09-25T11:00:00"),
        _moved("manager", "blocked", "ready", "2026-09-25T12:00:00"),
    ]
    block = _feedback_block(_task(rework_feedback=escalated, recent_activities=rows))
    assert escalated in block
    assert "FAIL — round one." not in block
    assert "Old fix already done" not in block
    # A FAIL verdict on the reviewer's block is the current one.
    rows[-2] = _comment("auditor", escalated, "2026-09-25T11:00:00",
                        _fail(["Ask the user for API_KEY"]))
    block = _feedback_block(_task(rework_feedback=escalated, recent_activities=rows))
    assert "- Ask the user for API_KEY" in block
    assert "Old fix already done" not in block


def test_unreviewed_resubmission_shows_no_older_fail():
    # The newest review move is a submission whose outcome is not in the
    # window: an executor does not see round one's verdict as current.
    later = "Please also update the summary."
    rows = [
        *_review_return("auditor", "FAIL — round one.", "2026-09-25T10:00:00",
                        _fail(["Old fix"])),
        _moved("dev", "in_progress", "review", "2026-09-25T10:40:00"),
        _comment("user", later, "2026-09-25T10:50:00"),
    ]
    block = _feedback_block(_task(rework_feedback=later, recent_activities=rows))
    assert later in block
    assert "Old fix" not in block
    # The reviewer of that submission still sees the prior round's findings.
    review = build_worker_prompt(_task(
        status="review", rework_feedback=later, recent_activities=rows,
    ))
    findings = review.split("<review_feedback>")[1].split("</review_feedback>")[0]
    assert "FAIL — round one." in findings
    assert "- Old fix" in findings


def test_feedback_comment_decides_when_no_review_move_is_in_the_window():
    # Both status rows left the window; the reviewer's FAIL comment is pinned
    # as a human conversation row. The comment whose text is the rework
    # feedback decides (R21): here it is the FAIL comment itself.
    block = _feedback_block(_task(
        reviewer="manager-assistant",
        rework_feedback="FAIL — see verdict.",
        recent_activities=[
            _comment("manager-assistant", "FAIL — round one.",
                     "2026-09-25T09:00:00", _fail(["Old fix"])),
            _comment("manager-assistant", "FAIL — see verdict.",
                     "2026-09-25T10:00:00", _fail(["Name the output file"])),
        ],
    ))
    assert "- Name the output file" in block
    assert "Old fix" not in block
    # With no comment matching the feedback, the newest FAIL is shown.
    block = _feedback_block(_task(
        reviewer="manager-assistant",
        rework_feedback="A comment that left the window.",
        recent_activities=[
            _comment("manager-assistant", "FAIL — see verdict.",
                     "2026-09-25T10:00:00", _fail(["Name the output file"])),
        ],
    ))
    assert "- Name the output file" in block


# ── U18: the vision prompts describe only the fields the user supplied ────


def test_vision_prompt_omits_empty_requirement_fields():
    from src._setup_prompts import _build_vision_user_prompt

    # The wizard sends the three requirement fields empty and the brief in
    # additional_context.
    out = _build_vision_user_prompt("Acme", "", {
        "responsibility_areas": "", "desired_agents": "", "workflows": "",
        "additional_context": "A bakery that takes custom cake orders.",
    })
    assert "Analyzed" not in out
    assert "## Stated" not in out
    assert "A bakery that takes custom cake orders." in out
    stated = _build_vision_user_prompt("Acme", "A bakery.", {
        "responsibility_areas": "Orders and deliveries.", "workflows": "",
    })
    assert "## Stated responsibility areas\nOrders and deliveries." in stated
    assert "Stated workflows" not in stated


def test_vision_and_roster_prompts_do_not_claim_analyzed_fields():
    from src._setup_prompts import ROSTER_PROMPT, SYNTHESIZE_VISION_PROMPT

    for prompt in (SYNTHESIZE_VISION_PROMPT, ROSTER_PROMPT):
        text = _norm(prompt).lower()
        assert "analyzed" not in text
        assert (
            "any requirement fields the user supplied (often only the "
            "free-text description)"
        ) in text


async def test_empty_vision_falls_back_to_the_supplied_requirements(monkeypatch):
    import src.setup_generator as sg
    from src._setup_prompts import (
        INSTRUCTIONS_PROMPT,
        ROSTER_PROMPT,
        SYNTHESIZE_VISION_PROMPT,
    )

    roster_prompts: list[str] = []

    async def fake_run_chunk(container, system_prompt, user_prompt, **kwargs):
        if system_prompt is SYNTHESIZE_VISION_PROMPT:
            return {"vision": ""}
        if system_prompt is INSTRUCTIONS_PROMPT:
            return {"instructions": "# Bakery\n\nTake orders."}
        if system_prompt is ROSTER_PROMPT:
            roster_prompts.append(user_prompt)
            return {"agents": []}
        raise AssertionError("unexpected system prompt in test")

    async def no_sources(*args, **kwargs):
        return False

    class Router:
        async def publish_event(self, event: dict) -> None:
            pass

    monkeypatch.setattr(sg, "_run_chunk", fake_run_chunk)
    monkeypatch.setattr(sg, "_container_has_source_files", no_sources)
    await sg.generate_office_config(
        router=Router(), request_id="req-fz", office_name="Bakery",
        office_description="", requirements={
            "responsibility_areas": "", "desired_agents": "", "workflows": "",
            "additional_context": "A bakery that takes custom cake orders.",
        },
        skill_catalog=[], container_name="cbcl-office-test",
    )
    roster = _norm(roster_prompts[0])
    assert (
        "fall back to the requirement fields the user supplied below, often "
        "only the free-text description"
    ) in roster
    assert "analyzed" not in roster.lower()


# ── U01: a person's decision on a blocked task is final for agents ────────


def _ma() -> str:
    from src.config_sync.claude_md_templates._system_agents import (
        MANAGER_ASSISTANT_CLAUDE_MD,
    )

    return _norm(MANAGER_ASSISTANT_CLAUDE_MD)


def test_manager_invariant_treats_a_person_decision_as_final():
    from tests.evals._prompt_composition import composed_manager_norm

    text = composed_manager_norm("default_workstream")
    invariant = text.split("4. **Blocked tasks never spontaneously auto-unblock.**")[1]
    invariant = invariant.split("5. **Action requests are deduped")[0]
    assert "retry is refused until a person approves" in invariant
    # f4: after a rejection the Manager's one retry naming the change the
    # user's notes asked for is how that change reaches them (its refusal
    # places their card); nothing else unblocks the task.
    assert (
        "A person's decision is final: after they reject a bounce-cap card, "
        "only make a change their notes ask for and `retry_blocked_task` once "
        "naming it; never unblock it another way (helper task, approval)."
        in invariant
    )
    # f2 Item 19: the refusal also fires while the card is still pending.
    assert (
        "A `bounce_cap_user_decision` refusal means a person decides it (its "
        "Inbox card, or Resume task) or has decided — tell the user which, as "
        "it states." in invariant
    )
    assert "stays blocked by their decision" not in invariant
    # f5: only the Manager's own refused retry places the person's card, so
    # the Manager makes that retry itself instead of delegating it.
    assert (
        "Make that retry yourself, never through the Manager Assistant: only "
        "your own retry places their card." in invariant
    )


def test_ma_names_resume_task_as_a_way_back_after_a_person_decides():
    # f5: after a person leaves a bounce-capped task blocked, their approval
    # is not the only way back; Resume task on the task is the other.
    ma = _ma()
    assert "It resumes only when a person approves it or uses its Resume task." in ma
    assert "It resumes only when a person approves it." not in ma


def test_asd_notify_limit_names_the_silent_reject():
    # f5: an over-limit notify message gets a platform notice, but the outbox
    # watcher rejects a notify FILE over 1 MiB unread, with no notice.
    from src.config_sync.claude_md_templates._system_agents import (
        AUTOMATION_SCRIPT_DEV_CLAUDE_MD,
    )
    from src.scripts.outbox_watcher import _MAX_OVERSIZED_PARSE_BYTES

    asd = _norm(AUTOMATION_SCRIPT_DEV_CLAUDE_MD)
    assert _MAX_OVERSIZED_PARSE_BYTES == 1024 * 1024
    assert (
        "a longer one is not delivered, the Manager gets only a platform "
        "notice (a notify file over 1 MiB is rejected with no notice)." in asd
    )


def test_auto_decide_policy_never_overrides_a_person_decision():
    from src.config_sync._auto_decide_rows import render_auto_decide_guidance

    for request_type in ("escalate_blocker", "request_clarification"):
        policy = _norm(render_auto_decide_guidance(request_type))
        assert "`bounce_cap_user_decision`" in policy
        assert (
            "only make a change their notes ask for and `retry_blocked_task` "
            "once naming it (its refusal can place their card); never unblock "
            "that task another way." in policy
        )


def test_ma_leaves_a_bounce_cap_decision_to_the_person():
    after = _ma().split("### After an approved escalation")[1].split("###")[0]
    skipped, refused = after.split('**"Auto-unblock refused"**')
    # A gate that is not a person's decision still takes Path C.
    assert '**"Auto-unblock skipped"**' in skipped
    assert "take Path C" in skipped
    # The bounce cap already put the decision in the user's Inbox: the MA
    # files nothing more and no longer routes the task to a Manager retry.
    assert "post your synthesis comment only" in refused
    assert "no request, helper task or retry" in refused
    assert "calls `retry_blocked_task` once" not in after
    assert "`bounce_cap_user_decision`" in after


def test_ma_triage_paths_name_the_bounce_cap_exception():
    # Every surface of the triage session that demands one of paths A–C also
    # names the bounce-cap exception, so no instruction sends the MA to a
    # helper task (dependency auto-promotion) or a new card after a person
    # decided.
    from tests.evals._prompt_composition import compose_worker_session

    session = _norm(compose_worker_session("ma_triage").text)
    pick = session.split("3. Pick exactly ONE resolution path")[1][:300]
    assert (
        "— except when the newest system comment since the task last entered "
        'blocked is "Auto-unblock refused" (the bounce cap): a person has '
        "decided and left the task blocked, so post only the synthesis comment "
        "and stop." in pick
    )
    assert (
        "**STOP IMMEDIATELY** after one of A/B/C (or after that synthesis "
        "comment alone at the bounce cap)." in session
    )
    exception = (
        "your synthesis comment alone after the current block's bounce-cap "
        '"Auto-unblock refused"'
    )
    ma = _ma()
    triage_mode = ma.split("- **`triage`**")[1].split("## Hard Rules")[0]
    assert exception in triage_mode
    hard_rule = ma.split("## Hard Rules")[1].split("## Role 1")[0]
    assert f"(paths A–C, or {exception})" in hard_rule
    steps = ma.split("### Triage steps")[1].split("**A. The worker asked")[0]
    assert (
        "3. Decide which resolution path applies: A, B or C below — none after "
        'the current block\'s bounce-cap "Auto-unblock refused" (see "After an '
        'approved escalation").' in steps
    )
    path_c = ma.split("**C. Decision needs the USER's authority**")[1]
    assert (
        "**MANDATORY** (except after the current block's bounce-cap "
        '"Auto-unblock refused", where a person has decided)' in path_c
    )


def test_ma_bounce_cap_rule_names_where_the_decision_went():
    # The refusal hands the decision to the user's Inbox, or to a pending
    # escalation whose decision hands it there (a Manager rejection re-routes
    # it); triage runs only when nothing is pending.
    after = _ma().split("### After an approved escalation")[1].split("###")[0]
    refused = after.split('**"Auto-unblock refused"**')[1]
    assert "as the newest system comment" in refused
    assert (
        "its decision went to the user's Inbox, or to a pending escalation "
        "whose decision hands the task to the user (a Manager rejection "
        "re-routes it there)" in refused
    )
    assert "You triage only when nothing is pending, so a person has decided" in refused
    from src._agent_image.mcp_tool_server import TRIAGE_PATHS_TEXT

    assert (
        'except after an "Auto-unblock refused" posted since the task last '
        "entered blocked (the bounce cap): with nothing pending, a person has "
        "decided, so comment only." in _norm(TRIAGE_PATHS_TEXT)
    )


def test_retry_tool_is_refused_until_a_person_approves():
    from src._agent_image._mcp.tools_manager import get_manager_tools

    retry = next(t for t in get_manager_tools() if t["name"] == "retry_blocked_task")
    text = _norm(retry["description"])
    assert "refused (`bounce_cap_user_decision`) until a person approves" in text
    assert "after a second cap hit or a person's rejection of its bounce-cap card" in text
    assert "a refused Manager call places ONE user card if none is pending" in text
    assert "until a person decides" not in text


async def _decided_poke(
    monkeypatch, decision: str, request_type: str, **extra,
) -> str:
    from unittest.mock import AsyncMock, MagicMock

    import src.orchestrator._manager_action_requests as mar

    monkeypatch.setattr(mar, "build_script_context_data", lambda c, k: {})
    controller = MagicMock()
    controller.handle_chat_message = AsyncMock()
    await mar.ingest_action_request_decided(controller, {
        "context_key": "workstream:WS-1", "request_id": "req-1",
        "request_type": request_type, "decision": decision,
        "source_task_id": "FZ-001.T01", **extra,
    })
    return _norm(controller.handle_chat_message.await_args.args[0]["user_message"])


async def test_rejected_bounce_cap_card_poke_leaves_the_task_blocked(monkeypatch):
    rejected = await _decided_poke(
        monkeypatch, "rejected", "escalate_blocker", bounce_cap_card=True,
    )
    assert "Check whether that task is now unblocked" not in rejected
    assert (
        "The request was task FZ-001.T01's bounce-cap card: the user left that "
        "task blocked, and their decision is final. Do not re-file it or "
        "unblock it another way." in rejected
    )
    # f4: a change their notes ask for reaches them through ONE refused
    # retry, which places their card; Resume task is their other way back.
    assert (
        "If their notes ask for a change, make it, then `retry_blocked_task` "
        "once naming it: it stays refused (`bounce_cap_user_decision`), and "
        "its text says whether it placed ONE card with your change in their "
        "Inbox." in rejected
    )
    assert "use the task's Resume task" in rejected
    assert "only their approval resumes" not in rejected
    approved = await _decided_poke(
        monkeypatch, "approved", "escalate_blocker", bounce_cap_card=True,
    )
    assert "Check whether that task is now unblocked" in approved
    # A rejection of a non-blocker request keeps the generic follow-up.
    other = await _decided_poke(monkeypatch, "rejected", "create_subtask")
    assert "Check whether that task is now unblocked" in other


async def test_other_rejected_blocker_request_allows_the_asked_revision(monkeypatch):
    # Only a bounce-cap card's rejection is final for agents. Any other
    # rejected blocker request (or a clarification, which never blocks its
    # task) leaves room for the change the user's notes ask for.
    for request_type in ("escalate_blocker", "request_clarification"):
        for extra in ({}, {"bounce_cap_card": False}):
            rejected = await _decided_poke(
                monkeypatch, "rejected", request_type,
                decision_notes="Use the public API instead.", **extra,
            )
            assert "decision is final" not in rejected
            assert "User notes: Use the public API instead." in rejected
            assert (
                "The request originated from task FZ-001.T01. Do not re-file it. "
                "If the user's notes ask for a change (for example a brief "
                "revision) and the task is blocked, make that change, then "
                "`retry_blocked_task` once naming it." in rejected
            )


# ── U03: the Manager Assistant names a missing Office Secret too ──────────


def test_ma_execute_blocker_names_missing_office_secrets():
    from src.config_sync.claude_md_content import SHARED_AGENT_WORK_RULES
    from src.config_sync.claude_md_templates._shared_agent import (
        MISSING_CREDENTIAL_NAMES_RULE,
    )

    rule = _norm(MISSING_CREDENTIAL_NAMES_RULE)
    assert rule == (
        "For `missing_credential`, also pass the exact Office Secret names in "
        "`office_secret_names`: saving them resumes the task."
    )
    execute = _ma().split("## Escalating a Blocker (execute mode")[1]
    assert rule in execute.split("`<blocker_class>` MUST be one of")[0]
    assert rule in _norm(SHARED_AGENT_WORK_RULES)


def test_ma_triage_path_c_names_missing_office_secrets():
    """Credential reconciliation closes only an escalation whose
    `office_secret_names` were all saved. The triage Path C (task prompt and
    MA playbook) files escalate_blocker itself, so it passes the names too —
    otherwise saving the secret leaves the card pending and the task Blocked."""
    from src.config_sync.claude_md_templates._shared_agent import (
        MISSING_CREDENTIAL_NAMES_RULE,
    )
    from src.orchestrator.worker_prompt import format_task_brief

    rule = _norm(MISSING_CREDENTIAL_NAMES_RULE)
    playbook_path_c = _ma().split("* `escalate_blocker` — DEFAULT for")[1]
    assert rule in playbook_path_c.split("* `request_clarification`")[0]
    triage = _norm(
        format_task_brief(
            {
                "task_id": "00000000-0000-0000-0000-000000000001",
                "readable_id": "FCB-001.T92",
                "title": "Blocked on a key",
                "status": "blocked",
                "rework_count": 0,
                "recent_activities": [],
                "artifacts": [],
                "reviewer": "auditor",
                "assigned_agent": "analyst",
                "brief": {"goal": "Produce X", "acceptance_criteria": ["X"]},
            }
        )
    )
    path_c = triage.split("**C (escalate to user):**")[1]
    assert rule in path_c.split("4. **STOP IMMEDIATELY**")[0]


# ── C3c-G9: a research consult that did not finish names what to check ────


async def _planner_poke(monkeypatch, consult: dict, **extra) -> str:
    from unittest.mock import AsyncMock, MagicMock

    import src.orchestrator._manager_action_requests as mar

    monkeypatch.setattr(mar, "build_script_context_data", lambda c, k: {})
    controller = MagicMock()
    controller._config.get_workstream = MagicMock(return_value={"name": "Website"})
    controller.handle_chat_message = AsyncMock()
    await mar.ingest_planner_result(controller, {"planner_consult": consult, **extra})
    return _norm(controller.handle_chat_message.await_args.args[0]["user_message"])


async def test_unscoped_research_failure_poke_names_its_research_file(monkeypatch):
    body = await _planner_poke(
        monkeypatch, {"mode": "research", "workstream_id": "WS-1"},
        task_id="planner-0123456789ab",
        planner_error="the Planner session ended WITHOUT persisting the research output",
    )
    assert "Your **research** consult did not finish" in body
    assert (
        "Its output may be missing or partial: check "
        "`/workspace/workstreams/website/research/planner-0123456789ab.md` first"
        in body
    )
    assert "Nothing was changed" not in body
    assert "get_execution_plan" not in body


_SPAWN_TIME_RESEARCH_PATH = (
    "/workspace/workstreams/old-site/research/planner-0123456789ab.md"
)


async def test_research_pokes_name_the_spawn_time_path_after_a_rename(monkeypatch):
    # f3 item 2: the workstream was renamed while the consult ran (the
    # ConfigStore row now says "Website"); the Planner was told, and wrote,
    # the path stored on the consult marker at spawn. Both pokes name that
    # path, not a recomputed one in the new folder.
    consult = {
        "mode": "research",
        "workstream_id": "WS-1",
        "_research_path": _SPAWN_TIME_RESEARCH_PATH,
    }
    failure = await _planner_poke(
        monkeypatch,
        consult,
        task_id="planner-0123456789ab",
        planner_error="the Planner session ended without completing",
    )
    assert f"check `{_SPAWN_TIME_RESEARCH_PATH}` first" in failure
    success = await _planner_poke(monkeypatch, consult, task_id="planner-0123456789ab")
    assert f"Its findings are in `{_SPAWN_TIME_RESEARCH_PATH}` — `Read` it" in success
    for body in (failure, success):
        assert "/workstreams/website/" not in body


async def test_scoped_research_failure_poke_points_at_the_scope_plan(monkeypatch):
    for consult in (
        {"mode": "research", "workstream_id": "WS-1", "scope_id": "SC-1"},
        {"mode": "scope_plan", "workstream_id": "WS-1", "scope_id": "SC-1"},
    ):
        body = await _planner_poke(
            monkeypatch, consult, task_id="planner-0123456789ab",
            planner_error="the Planner session ended without completing",
        )
        assert (
            "Its output may be missing or partial: check the scope's plan "
            "(`get_execution_plan`) first" in body
        )
        assert "/research/" not in body
