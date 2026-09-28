"""Fix wave 2, package fx-prompts: pins for the prompt corrections.

Each test names the finding it closes. They pin the corrected wording on the
rendered surfaces (or the prompt constants) so a later edit cannot quietly
reintroduce the contradiction, dangling reference or false lifecycle fact.
"""

from __future__ import annotations

from src.config_sync.claude_md_content import MANAGER_ASSISTANT_CLAUDE_MD


def _norm(text: str) -> str:
    return " ".join(text.split())


MA = _norm(MANAGER_ASSISTANT_CLAUDE_MD)


# ── C3b-G1 / C3e-G1 (shared decision 1): Path A has a real exit ────────────


def test_ma_path_a_files_an_approval_request_after_the_answer():
    path_a = MA.split("**A. The worker asked a clarification question")[1]
    path_a = path_a.split("**B. Worker is blocked")[0]
    assert '`event_type: "answer"`' in path_a
    assert '`escalate_blocker` with `blocker_class: "ambiguous_spec"`' in path_a
    assert "Answered in-thread; approve to resume" in path_a
    assert "your answer as `suggested_unblock`" in path_a
    assert "the task resumes when that request is approved" in path_a
    # The false claim that someone moves it once they read the answer.
    assert "will move it to ready" not in MA


def test_ma_path_b_and_parked_tasks_do_not_claim_a_human_only_exit():
    path_b = MA.split("**B. Worker is blocked")[1].split("**C. Decision needs")[0]
    assert "the one unblock that needs no approval" in path_b
    assert "ONLY legitimate non-human unblock path" not in MA
    # A pending request may be decided by the Manager, not only the user.
    assert "leave the task alone until that request is decided" in MA
    assert "leave the task alone and let the user decide" not in MA


# ── C3b-G2: at the bounce cap the MA never repeats Path A ──────────────────


def test_ma_after_refused_auto_unblock_leaves_it_to_the_person():
    after = MA.split("### After an approved escalation")[1].split("###")[0]
    refused = after.split('**"Auto-unblock refused"**')[1]
    assert "(never Path A again)" in refused
    # fz-prompts U01: the bounce cap already gave the decision to the user's
    # Inbox, so the MA no longer routes the task to a Manager retry.
    assert "post your synthesis comment only" in refused
    assert "calls `retry_blocked_task` once" not in after
    from src._agent_image._mcp.tools_manager import get_manager_tools

    retry = next(t for t in get_manager_tools() if t["name"] == "retry_blocked_task")
    assert "until a person approves" in retry["description"]


# ── C4b-G3: Path C names every required escalate_blocker field ─────────────


def test_ma_path_c_lists_blocker_class_as_required():
    from src._agent_image._mcp.tools_worker import get_worker_tools

    schema = next(t for t in get_worker_tools() if t["name"] == "escalate_blocker")
    required = schema["inputSchema"]["required"]
    path_c = MA.split("**C. Decision needs the USER's authority**")[1]
    path_c = path_c.split("`request_clarification` — ONLY")[0]
    for field in required:
        assert f"`{field}`" in path_c, field


# ── C4b-G4: phase-correct blocker rules; no "only a human" claim ───────────


def test_ma_communication_blocker_rule_is_phase_aware():
    comm = MA.split("## Communication")[1].split("## Scope")[0]
    assert 'Execute: follow "Escalating a Blocker" below' in comm
    # Not the old unconditional "call update_status" (unserved in review/triage).
    assert "If blocked by a REAL issue, call `update_status`" not in comm
    assert 'Review: a genuine blocker is `move_task(new_status="blocked")`' in comm
    assert "Triage: Path C." in comm
    assert "`update_status` is not served in review or triage" in comm
    assert "## Escalating a Blocker (execute mode — when YOUR OWN task is blocked)" in (
        MANAGER_ASSISTANT_CLAUDE_MD
    )


def test_ma_hard_rule_names_the_helper_task_auto_promotion():
    assert "There is exactly one path back to ready" not in MA
    assert "the only path back to ready is a deliberate human decision" not in MA
    assert "auto-promotion when a Path B helper task you created reaches done" in MA


# ── C4b-G5: Action B approves with the structured verdict ──────────────────


def test_ma_action_b_approval_carries_the_structured_verdict():
    action_b = MA.split("### Action B — Approve or Return")[1].split("### HARD RULES")[0]
    approve = action_b.split("2. **If FAIL")[0]
    assert '`verdict` = {overall: "pass" or "conditional", rationale, criteria}' in approve
    assert "criterion_index, name, status \"pass\", evidence" in approve
    assert "`get_verification_status`" in approve
    assert "`verification_input_fingerprint`" in approve
    assert "three with a verification plan (`get_verification_status`)" in MA


def test_ma_review_catalog_serves_the_approval_tools():
    from src._agent_image._mcp.tools_worker import get_worker_subcatalog

    names = {t["name"] for t in get_worker_subcatalog("review", "manager-assistant")}
    assert {"move_task", "get_verification_status", "get_task_detail"} <= names


# ── C4b-G6: no unreachable orphan-routing sub-mode ─────────────────────────


def test_ma_playbook_has_no_orphan_triage_mode():
    assert "orphan" not in MA.lower()
    assert "THREE sub-modes — two task-triggered (Review / Blocked)" in MA


# ── C4a-G4: research is program-only everywhere the gate is described ──────


def test_research_is_named_in_every_program_only_gate():
    from pathlib import Path

    from src.config_sync.claude_md_templates._manager_modules import (
        MANAGER_PROGRAM_PROCEDURES,
    )

    handler = (
        Path(__file__).resolve().parents[3]
        / "backend" / "app" / "ws" / "tool_endpoint" / "_handlers_planner.py"
    )
    if handler.exists():  # ground truth: the backend gate covers research
        assert "(scope_plan / materialize / research)" in handler.read_text()
    procs = _norm(MANAGER_PROGRAM_PROCEDURES)
    assert "- `research` (consented programs only) — investigate" in procs
    from src._agent_image._mcp.tools_manager import get_manager_tools

    consult = next(t for t in get_manager_tools() if t["name"] == "consult_planner")
    assert "research = investigate a question (program only)" in consult["description"]


# ── C4a-G6: create_scope's order matches the Planner-authored milestone ────


def test_create_scope_description_routes_milestone_tasks_through_the_planner():
    from src._agent_image._mcp.tools_manager import get_manager_tools

    desc = next(t for t in get_manager_tools() if t["name"] == "create_scope")[
        "description"
    ]
    assert "create_task(scope_id=…) × N" not in desc
    assert "consult_planner(mode='materialize', scope_id=…)" in desc
    assert "Never hand-write a milestone's tasks" in desc


# ── C1-G6 (tool-description half): the Manager is told the length limits ──


def test_propose_configuration_states_the_backend_length_limits():
    import re
    import uuid

    from src._agent_image._mcp.tools_configuration import (
        CONFIGURATION_FIELD_LIMITS,
        CONFIGURATION_TOOLS,
    )

    from tests.backend_boundary import import_backend

    tool = next(t for t in CONFIGURATION_TOOLS if t["name"] == "propose_configuration")
    change = tool["inputSchema"]["properties"]["changes"]["items"]
    after = change["properties"]["after"]["description"]
    # The description is rendered from the image's copy of the limits, and
    # that copy must equal the backend's source of truth field for field.
    schemas = import_backend("app.configuration_proposals.schemas")
    assert CONFIGURATION_FIELD_LIMITS == schemas.CONFIGURATION_FIELD_LIMITS
    assert (
        "Character limits: Office claude_md_content 16,000, work_policy 4,000; "
        "Workstream context_notes 26,000; agent system_prompt/claude_md_content "
        "50,000, role_description 5,000." in after
    )
    stated = {
        (target, field): limit
        for target, limits in CONFIGURATION_FIELD_LIMITS.items()
        for field, limit in limits.items()
    }
    for (target, field), limit in stated.items():
        label = {"office": "Office", "workstream": "Workstream"}.get(target, target)
        section = after.split(f"{label} ", 1)[1].split(";")[0]
        assert re.search(rf"\b{field}\b[^,;]* {limit:,}\b", section), (target, field)
    # Ground truth: the backend validator accepts exactly the stated limit
    # and refuses one character more, for every supported target field.
    for (target, field), limit in stated.items():
        base = {
            "target": target,
            "target_id": uuid.uuid4(),
            "field": field,
            "before": "old",
            "reason": "pin",
        }
        schemas.ConfigurationChange(**base, after="x" * limit)
        try:
            schemas.ConfigurationChange(**base, after="x" * (limit + 1))
        except ValueError:
            pass
        else:  # pragma: no cover - the pin's failure branch
            raise AssertionError(f"{target}.{field} accepted {limit + 1} chars")


# ── C1-G8: skills, model/effort, tools, connectors are not proposal targets


def test_stewardship_names_what_is_outside_the_proposal_path():
    from src._agent_image._mcp.tools_configuration import CONFIGURATION_TOOLS
    from tests.evals._prompt_composition import composed_manager_norm

    tool = next(t for t in CONFIGURATION_TOOLS if t["name"] == "propose_configuration")
    assert "Skills, model/effort, tools and connectors are not targets." in (
        tool["description"]
    )
    text = composed_manager_norm("general_chat")
    assert "custom-agent instructions for role methods no skill carries" in text
    assert "are not proposal targets" in text


# ── C4b-G1 / C4b-G2: review and triage prompts match their phase and role ──


def _phase_task(**overrides):
    base = {
        "task_id": "00000000-0000-0000-0000-000000000001",
        "readable_id": "FX-001.T01",
        "title": "Fix-wave task",
        "status": "ready",
        "rework_count": 0,
        "brief": {
            "goal": "Deliver the report.",
            "inputs": "The user's request.",
            "acceptance_criteria": ["Report exists"],
            "verification_steps": "Execution checks: open it.",
        },
        "workstream_short_code": "FX",
        "workstream_context": {"name": "Fix Wave", "short_code": "FX"},
        "assigned_agent": "dev",
        "reviewer": "auditor",
    }
    base.update(overrides)
    return base


def _prompt(**overrides):
    from src.orchestrator.worker_prompt import build_worker_prompt

    return _norm(build_worker_prompt(_phase_task(**overrides)))


def test_step_00_reference_only_in_the_execution_phase():
    assert "STEP 0.0 below tells you exactly when to read it" in _prompt(
        status="in_progress"
    )
    for overrides in (
        {"status": "review"},
        {"status": "review", "reviewer": "manager-assistant"},
        {"status": "blocked", "assigned_agent": "manager-assistant"},
    ):
        text = _prompt(**overrides)
        assert "STEP 0.0" not in text, overrides
        assert "The Phase orientation section below says how to use it" in text


def test_phase_inspection_line_matches_the_ma_playbook():
    registered = "registered deliverables first"
    assert registered in _prompt(status="review")  # other reviewers
    ma_review = _prompt(status="review", reviewer="manager-assistant")
    assert registered not in ma_review
    assert "open deliverables only for a smoke review you run yourself" in ma_review
    triage = _prompt(status="blocked", assigned_agent="manager-assistant")
    assert registered not in triage
    assert "blocker evidence (the escalation comment) first" in triage
    assert "do not read this task's deliverables" in triage


def test_office_file_points_review_and_triage_at_phase_orientation():
    from src.config_sync.claude_md_templates._office import SHARED_OFFICE_CLAUDE_MD

    office = _norm(SHARED_OFFICE_CLAUDE_MD)
    assert "Your task's STEP 0.0 tells you to read it" not in office
    assert "Your task prompt (STEP 0.0a when executing, Phase orientation in " in office


# ── C4b-G7: every quoted section a playbook cites is a heading it renders ──


def test_cited_shell_sections_exist_in_the_rendered_agent_file():
    import re

    from src.config_sync.claude_md_writer import ClaudeMdWriter

    rendered = {
        "asd": ClaudeMdWriter._get_agent_claude_md({
            "name": "automation-script-developer",
            "agent_type": "system",
            "allowed_tools": ["Read", "Write", "Bash", "Glob", "Grep"],
        }),
        "custom_bash": ClaudeMdWriter._get_agent_claude_md({
            "name": "custom-dev",
            "agent_type": "custom",
            "display_name": "Custom Dev",
            "role_description": "Engineering — builds things.",
            "allowed_tools": ["Read", "Write", "Bash"],
        }),
    }
    for name, text in rendered.items():
        norm = _norm(text)
        assert "Git is Direct" not in norm, name
        assert 'the office CLAUDE.md "Office Secrets in Your Shell"' not in norm, name
        assert '"Office Secrets in Your Shell"' in norm, name  # non-vacuous
        headings = set(re.findall(r"^#+ (.+?)\s*$", text, flags=re.MULTILINE))
        for cited in ("Office Secrets in Your Shell", "One-off Shell Operations"):
            if f'"{cited}"' in norm:
                assert cited in headings, (name, cited)


# ── C4b-G8: flow consults report to the user, not the Manager ──────────────


def test_flow_consult_handoff_names_the_reader_that_gets_the_report():
    from src.config_sync.claude_md_templates._system_agents import (
        DATA_CURATOR_CLAUDE_MD,
        FLOW_ARCHITECT_CLAUDE_MD,
        PLANNER_CLAUDE_MD,
    )

    for text in (FLOW_ARCHITECT_CLAUDE_MD, DATA_CURATOR_CLAUDE_MD):
        norm = _norm(text)
        assert "external step to the Manager instead of waiting" not in norm
        assert "in your final report (the user reads it)" in norm
    # The Planner's result DOES poke the Manager, so its clause is unchanged.
    assert "external step to the Manager instead of waiting" in _norm(PLANNER_CLAUDE_MD)


# ── C4b-G11: credentials are described as configured, never as a given ────


def test_playbooks_do_not_present_credentials_as_always_present():
    from src.config_sync.claude_md_templates._system_agents import (
        ANALYST_CLAUDE_MD,
        AUTOMATION_SCRIPT_DEV_CLAUDE_MD,
    )
    from tests.evals._prompt_composition import composed_manager_norm

    analyst = _norm(ANALYST_CLAUDE_MD)
    assert "GitHub token is available as an env var" not in analyst
    assert "an SSH key is in `~/.ssh/`" not in analyst
    assert "`list_office_secrets`" in analyst
    assert "report that as the gap; do not attribute the failure to the service" in (
        analyst
    )
    asd = _norm(AUTOMATION_SCRIPT_DEV_CLAUDE_MD)
    assert "an SSH key is in `~/.ssh/`" not in asd
    assert "SSH keys the user added" in asd
    manager = composed_manager_norm("default_workstream")
    assert "The office SSH key lives in" not in manager
    assert "SSH keys the user added live in `~/.ssh/`" in manager


# ── C4b-G12: the Auditor proves exit 0 from the host, not agent-writable disk


def test_auditor_script_evidence_uses_the_host_posted_activity():
    from pathlib import Path

    from src._agent_image._mcp.tools_worker import get_worker_tools
    from src.config_sync.claude_md_templates._system_agents import AUDITOR_CLAUDE_MD

    auditor = _norm(AUDITOR_CLAUDE_MD)
    step = auditor.split("6. **Test evidence**")[1].split("7. **")[0]
    assert "host record on THIS task: its `script_completed` activity" in step
    assert "must be completed, exit code 0" in step
    assert "No matching host record = FAIL" in step
    assert "never proof of the exit" in step
    assert "`cat /workspace/.scripts/<name>/executions/<id>/status.json`" not in step
    # Ground truth: the host runner posts that activity with these details.
    notifier = (
        Path(__file__).resolve().parents[2] / "src" / "scripts" / "script_notifier.py"
    ).read_text()
    for key in ('"event_type": "script_completed", "actor": "system"',
                '"execution_id": exec_id', '"exit_code": process_returncode'):
        assert key in notifier, key
    listing = next(
        t for t in get_worker_tools() if t["name"] == "list_script_executions"
    )["description"]
    assert "0 exit_code" not in listing
    assert "Rows carry no exit code" in listing


# ── B5-hygiene-04: BRANCH S agrees with the contract's resume fact ────────


def test_branch_s_resume_steps_agree_with_the_contract():
    from src._lifecycle_contract import SCRIPT_HANDOFF_RESUME_FACT
    from src.orchestrator.worker_prompt import build_worker_prompt

    fact = _norm(SCRIPT_HANDOFF_RESUME_FACT)
    # The two load-bearing clauses the branch restates as steps.
    for clause in ("before any new side effect", "never relaunch a completed run"):
        assert clause in fact, clause
    prompt = _norm(build_worker_prompt(_phase_task(
        status="in_progress",
        rework_count=0,
        script_handoff_results=[{"execution_id": "exec-1", "state": "completed"}],
    )))
    assert "BEFORE any new side effect; never relaunch a completed run" in prompt


# ── CRIT-01: the resume list keeps the newest runs and counts the rest ────


def test_script_resume_lists_the_newest_runs_and_counts_the_earlier_ones():
    import random

    from src.orchestrator.worker_prompt import build_worker_prompt

    # 25 minute-spaced launches in one cycle; the newest completed and made
    # the task resumable, every earlier one failed.
    ids = [f"exec-2026-09-25T10-{minute:02d}-00-abc{minute:03d}" for minute in range(25)]
    results = [{"execution_id": run_id, "state": "failed"} for run_id in ids[:-1]]
    results.append({"execution_id": ids[-1], "state": "completed"})
    for rows in (results, random.Random(7).sample(results, len(results))):
        prompt = _norm(build_worker_prompt(_phase_task(
            status="in_progress", rework_count=0, script_handoff_results=rows,
        )))
        assert "**→ BRANCH S (MANAGED-SCRIPT RESUME)**" in prompt
        assert f"Execution {ids[-1]}: completed." in prompt
        assert f"Execution {ids[5]}: failed." in prompt
        for dropped in ids[:5]:
            assert dropped not in prompt
        assert "(5 earlier recorded runs not shown;" in prompt
        # Oldest first, newest last.
        assert prompt.index(f"Execution {ids[5]}") < prompt.index(f"Execution {ids[-1]}")
    short = _norm(build_worker_prompt(_phase_task(
        status="in_progress", rework_count=0, script_handoff_results=results[-3:],
    )))
    assert "earlier recorded runs not shown" not in short


# ── C4b-G9: crash recovery finds the files agents are told to write ───────


def test_orphan_search_matches_the_output_naming_per_mode():
    legacy = _prompt(status="in_progress")
    assert "`/workspace/outputs/FX/fx-001_t01*`" in legacy
    assert "Name each new file or directory `fx-001_t01_<descriptive-name>`" in legacy
    assert "Also check any exact file path the Brief's Output Format names." in legacy
    task_id = "00000000-0000-0000-0000-000000000001"
    owned = _prompt(
        status="in_progress",
        agent_execution_policy={"enabled": True, "max_workers": 4,
                                "max_workers_per_profile": 2},
        workstream_slug="fix-wave",
    )
    owned_dir = f"/workspace/workstreams/fix-wave/tasks/{task_id}"
    assert f"`{owned_dir}/**` (this directory is this task's alone" in owned
    assert f"`{owned_dir}/fx-001_t01*`" not in owned
    assert "Name each new file or directory" not in owned


def test_delivery_rules_name_the_task_slug_prefix():
    from src.config_sync.claude_md_content import SHARED_AGENT_WORK_RULES
    from src.config_sync.claude_md_templates._system_agents import BUILDER_CLAUDE_MD

    shared = _norm(SHARED_AGENT_WORK_RULES)
    assert "{task-slug}_{descriptive-name}.md" in shared
    assert "e.g. `wr-003_t14`" in shared
    assert "(the task's readable id lowercased with `.` → `_`" in _norm(BUILDER_CLAUDE_MD)


# ── C4b-G10: the ask close describes its two calls as two calls ───────────


def test_ask_close_does_not_call_two_steps_one_call():
    ask = _prompt(status="in_progress", task_class="ask")
    assert "ONE call: post" not in ask
    assert "close in two calls: post the answer via `add_activity`" in ask


# ── C4c-G5: the thread window says when older entries are omitted ─────────


def test_recent_activity_marks_omitted_entries():
    acts = [{"event_type": "comment", "actor": "user", "content": "EU only"}]
    shown = _prompt(status="in_progress", recent_activities=acts,
                    recent_activities_total=27)
    assert "(26 earlier entries not shown.)" in shown
    whole = _prompt(status="in_progress", recent_activities=acts,
                    recent_activities_total=1)
    assert "earlier entries not shown" not in whole
    legacy = _prompt(status="in_progress", recent_activities=acts)
    assert "earlier entries not shown" not in legacy


def test_worker_session_copies_the_omission_markers_from_the_refetch():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "src" / "_agent_worker_task.py"
    ).read_text()
    assert '("recent_activities_total", "artifacts_truncated")' in source


# ── C4c-G11: capped AI-facing lists say that they are capped ─────────────


def test_capped_lists_mark_their_omissions():
    from src.orchestrator.flow_consult_prompt import (
        _MAX_COLLECTIONS,
        build_data_curator_prompt,
        build_flow_architect_prompt,
    )
    from src.orchestrator.manager_context import build_dynamic_context
    from tests.backend_boundary import import_backend

    many = [
        {"name": f"c{i}", "display_name": f"C {i}", "field_names": ["a"],
         "schema": [{"name": "a", "type": "text"}], "schema_revision": 1,
         "row_count": 0}
        for i in range(_MAX_COLLECTIONS + 1)
    ]
    for build in (build_flow_architect_prompt, build_data_curator_prompt):
        text = _norm(build({"directive": "tidy up", "collections": many,
                            "flow_name": "f", "flow_display_name": "F"}))
        assert "1 more collection(s) not listed — call `list_collections`" in text
    curator = _norm(build_data_curator_prompt({"directive": "tidy", "collections": many}))
    assert "full schemas below" not in curator
    assert "each listed schema in full" in curator

    context_builder = import_backend("app.ws.context_builder")
    assert context_builder.RECENT_DONE_LIMIT == 8  # the heading states 8
    from src.config_sync.sync_service import ConfigStore

    ctx = _norm(build_dynamic_context(
        "general_chat",
        {"recently_completed": [{"readable_id": "WR-001.T01", "title": "Done"}]},
        ConfigStore(),
        True,
    ))
    assert "at most 8 shown — `get_board(status=done)` for more" in ctx


def test_truncated_artifact_list_is_marked():
    arts = [{"file_path": f"/workspace/outputs/FX/f{i}.md"} for i in range(50)]
    marked = _prompt(status="in_progress", artifacts=arts, artifacts_truncated=True)
    assert "(50 newest shown — older attachments exist but are not listed here)" in marked
    assert "older attachments exist" not in _prompt(status="in_progress", artifacts=arts)


# ── C4c-G12: the executor sees the structured FAIL verdict ────────────────


def _fail_verdict_activity(fixes, criteria=None, content="FAIL — see verdict."):
    # ``content`` must equal the rework feedback: only the verdict on the
    # returning comment is rendered (R21).
    return {
        "event_type": "comment",
        "actor": "auditor",
        "content": content,
        "details": {
            "overall": "fail",
            "rationale": "Criteria unmet.",
            "criteria": criteria or [
                {"criterion_index": 1, "name": "Report exists",
                 "status": "fail", "evidence": "no file at the output path"},
            ],
            "required_fixes": fixes,
        },
    }


def test_rework_prompt_renders_fixes_kept_only_in_the_verdict():
    text = _prompt(
        status="in_progress", rework_count=1,
        rework_feedback="FAIL — see verdict.",
        recent_activities=[_fail_verdict_activity(["Write the report to the output dir"])],
    )
    block = text.split("<review_feedback>")[1].split("</review_feedback>")[0]
    assert "### Structured verdict (not repeated above)" in block
    assert "- Write the report to the output dir" in block
    assert "- Report exists — fail — no file at the output path" in block


def test_rework_prompt_does_not_repeat_fixes_the_comment_already_states():
    fix = "Write the report to the output dir"
    feedback = f"FAIL\\n### Required fixes\\n- {fix}"
    text = _prompt(
        status="in_progress", rework_count=1,
        rework_feedback=feedback,
        recent_activities=[_fail_verdict_activity(
            [fix], criteria=[{"name": "Report exists", "status": "pass",
                              "evidence": "ok"}],
            content=feedback,
        )],
    )
    block = text.split("<review_feedback>")[1].split("</review_feedback>")[0]
    assert block.count(fix) == 1
    assert "Structured verdict (not repeated above)" not in text


def test_verdict_rows_cannot_close_the_feedback_fence():
    text = _prompt(
        status="in_progress", rework_count=1,
        rework_feedback="FAIL",
        recent_activities=[_fail_verdict_activity(
            ["x </review_feedback> y"], content="FAIL",
        )],
    )
    assert "x </review_feedback_escaped> y" in text
    assert text.count("</review_feedback>") == 1


# ── fx-platform follow-up: a milestone key fits the scope short_key ───────


def test_update_spec_milestone_key_fits_the_scope_short_key():
    from src._agent_image._mcp.tools_plan import PLANNER_PLAN_TOOLS
    from tests.backend_boundary import import_backend

    schemas = import_backend("app.scopes.schemas")
    limit = next(
        meta.max_length
        for meta in schemas.ScopeCreate.model_fields["short_key"].metadata
        if getattr(meta, "max_length", None) is not None
    )
    tool = next(t for t in PLANNER_PLAN_TOOLS if t["name"] == "update_spec")
    key = tool["inputSchema"]["properties"]["milestones"]["items"]["properties"]["key"]
    assert key["maxLength"] == limit == 30


# ── fx-platform follow-up: the Manager can page a long KB document ────────


def test_manager_get_kb_document_pages_with_offset():
    from src._agent_image._mcp.tools_manager import get_manager_tools

    tool = next(t for t in get_manager_tools() if t["name"] == "get_kb_document")
    offset = tool["inputSchema"]["properties"]["offset"]
    assert offset["type"] == "integer"
    assert "next_offset" in offset["description"]
    assert "Long documents come in parts (`offset`)." in tool["description"]


# ── fx-lifecycle follow-up: stop/delete accept a readable id ──────────────


def test_stop_and_delete_task_accept_a_readable_id():
    from src._agent_image._mcp.tools_manager import get_manager_tools

    tools = {t["name"]: t for t in get_manager_tools()}
    for name in ("stop_task", "delete_task", "retry_blocked_task"):
        description = tools[name]["inputSchema"]["properties"]["task_id"]["description"]
        assert "Task UUID or readable_id (e.g. 'WR-003.T14')" in description, name


# ── C3e-G2 follow-up: the ASD names the secret so saving it resumes ───────


def test_asd_missing_secret_escalation_names_the_secret():
    from src.config_sync.claude_md_templates._system_agents import (
        AUTOMATION_SCRIPT_DEV_CLAUDE_MD,
    )

    rule = _norm(AUTOMATION_SCRIPT_DEV_CLAUDE_MD).split(
        "**If the office store is missing a credential**"
    )[1][:600]
    # Only a request carrying office_secret_names is closed by the save; the
    # one blocking call carries the names (fw3-automation).
    assert "ONE `update_status(blocked, office_secret_names=[<Office Secret name>])`" in rule
    assert "escalate_blocker" not in rule
    assert "adding the named secret in Settings → Security resumes the task" in rule


# ── fw3-automation: a bare missing-credential block names its secrets ─────


def test_shared_rules_name_missing_secrets_on_the_blocking_call():
    from src.config_sync.claude_md_content import SHARED_AGENT_WORK_RULES

    text = _norm(SHARED_AGENT_WORK_RULES)
    assert (
        "For `missing_credential`, also pass the exact Office Secret names in "
        "`office_secret_names`: saving them resumes the task." in text
    )
    # The pinned no-separate-question rule survives the trim.
    assert "Do NOT post a separate `question` checkpoint first." in text


def test_builder_hosting_block_names_a_missing_office_secret():
    from src.config_sync.claude_md_templates._system_agents import (
        BUILDER_CLAUDE_MD,
    )

    text = _norm(BUILDER_CLAUDE_MD)
    assert (
        "`update_status(blocked)` whose comment starts "
        "`ESCALATED (missing_credential):` (name a missing Office Secret in "
        "`office_secret_names`)." in text
    )
