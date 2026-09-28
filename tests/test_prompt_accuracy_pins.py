"""Prompt-accuracy pins for the AI-quality extras (wp-prompts).

Each pin names the verified defect it guards. They pin the CORRECTED fact,
never merely the absence of old words, so a reintroduced wrong instruction
fails here even if it is reworded.
"""
from __future__ import annotations

from src.config_sync.claude_md_content import (
    MANAGER_ASSISTANT_CLAUDE_MD,
    MANAGER_CLAUDE_MD,
    SHARED_OFFICE_CLAUDE_MD,
)
from src.config_sync.claude_md_templates._system_agents import PLANNER_CLAUDE_MD
from src.config_sync.claude_md_writer import render_office_specs_index
from src.orchestrator.planner_prompt import build_planner_prompt


def _norm(text: str) -> str:
    return " ".join(text.split())


def _office() -> str:
    return _norm(
        SHARED_OFFICE_CLAUDE_MD.format(
            office_name="Test Office",
            office_specs_index=render_office_specs_index([]),
        )
    )


def test_x00_materialize_recovery_repairs_briefs_with_update_task():
    # backend create_task_with_brief returns a deduplicated scoped row
    # UNCHANGED; only update_task(brief=...) completes an incomplete brief.
    materialize = _norm(build_planner_prompt(
        {"planner_consult": {"mode": "materialize", "scope_id": "scope-1"}}
    ))
    planner = _norm(PLANNER_CLAUDE_MD)
    # F07: the materialize-recovery rule loads with the program procedures.
    from tests.evals._prompt_composition import composed_manager_norm, manager_corpus

    manager = composed_manager_norm("program_workstream")
    assert "complete it with `update_task(task_id, brief={...})`" in materialize
    assert "returns the existing row UNCHANGED" in materialize
    assert "complete it with `update_task(task_id, brief={...})`" in planner
    assert "completes existing incomplete briefs with `update_task(brief=…)`" in manager
    for text in (materialize, planner, _norm(manager_corpus())):
        assert "FILLS the existing row" not in text
        assert "fills empty-brief tasks" not in text


def test_x02_ma_triage_helper_task_is_unscoped():
    # _enforce_sequential_addition refuses an in-scope helper with empty
    # depends_on; depending on the blocked task instead creates a cycle.
    playbook = _norm(MANAGER_ASSISTANT_CLAUDE_MD)
    assert "UNSCOPED" in playbook
    assert "in the same workstream + scope" not in playbook


def test_x06_manager_archive_guidance_uses_the_stop_protocol():
    # board.py allows in_progress/review → archived for the Manager; archive
    # stages the durable stop. Only archive_SCOPE refuses live tasks.
    from tests.evals._prompt_composition import manager_corpus_norm

    manager = _norm(MANAGER_CLAUDE_MD)
    corpus = manager_corpus_norm()  # T24: core + modules + dynamic context
    assert "cancel gracefully first" not in corpus
    assert "move to blocked, then archive" not in corpus
    assert "On live work it stages the same durable stop as `stop_task`" in manager
    assert "then `stop_task` it (never a detour through `blocked`)" in manager


def test_x07_no_count_based_rework_escalation_in_the_ma_playbook():
    playbook = _norm(MANAGER_ASSISTANT_CLAUDE_MD)
    assert "After 2 rework cycles" not in playbook
    assert "Rework has no count limit" in playbook


def test_x10_script_routing_is_scoped_to_office_automation():
    from tests.evals._prompt_composition import manager_corpus_norm

    manager = _norm(MANAGER_CLAUDE_MD)
    assert "No exceptions" not in manager_corpus_norm()  # T24
    assert "NOT script tasks: an application/prototype/site whose source includes `.py`" in manager
    assert "Clues, not proof" in manager


def test_x11_forced_done_respects_verification_plans():
    # The Manager move_task schema carries no verdict, so a plan-bearing
    # brief always refuses a forced Done.
    manager = _norm(MANAGER_CLAUDE_MD)
    assert (
        "a brief with a `verification_plan` refuses a forced Done" in manager
    )


def test_x50_skills_are_read_from_the_index_not_auto_discovered():
    # D1: the native Skill tool stays disallowed; playbooks are listed in the
    # agent CLAUDE.md and read on demand with Read.
    from tests.evals._prompt_composition import skill_autoload_claims

    office = _office()
    assert skill_autoload_claims(office) == []  # T28: any phrasing, not one
    assert "`Read` the relevant one" in office
    assert "native skill auto-discovery is off" in office


def test_f07_manager_allowlist_heading_scopes_the_exact_set():
    # The generated allowlist is the workstream catalog; in General Chat the
    # stripped writes are named by the generated General Chat procedures
    # (F07 moved them out of the core file, so the header points there).
    manager = _norm(MANAGER_CLAUDE_MD)
    assert "In a workstream your tool set is EXACTLY these." in manager
    assert (
        "In General Chat the board/planning writes are not registered — the "
        "General Chat procedures in your context name them." in manager
    )


def test_x05_consult_bash_rules_name_only_held_tools():
    from src.config_sync.claude_md_writer import ClaudeMdWriter

    for name in ("flow-architect", "data-curator"):
        rendered = ClaudeMdWriter._get_agent_claude_md({
            "name": name,
            "agent_type": "system",
            "allowed_tools": ["Read", "Bash"],
        })
        assert "escalate_blocker" not in rendered
        assert "list_office_secrets" not in rendered
    planner = ClaudeMdWriter._get_agent_claude_md({
        "name": "planner", "agent_type": "system", "allowed_tools": ["Bash"],
    })
    assert "escalate_blocker" not in planner
