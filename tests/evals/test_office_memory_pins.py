"""Office-memory v1 — content pins (roadmap T3.1/T3.3/T3.5).

Three families:

1. **No KB-first mandate anywhere.** The pre-memory posture told every
   role to search the KB before working ("KB-first", "search_kb before
   any research task", …). Spec §6.5 demotes the KB to EXPLICIT triggers
   (assigned references / the user asked / a nameable gap); this pin
   fails closed on the historical mandate phrases across every rendered
   playbook surface AND both KB tool descriptions, so the mandate cannot
   quietly return.
2. **The ladder + memory tools are pinned** where they must exist
   (Manager playbook section, shared worker rules, analyst step 2, the
   recall/remember descriptions' load-bearing sentences).
3. **learnings.md writing is retired** from every playbook surface
   (T3.5 — reads survive only on the Planner surfaces, deliberately:
   Planner recall is a spec §10 non-goal).
"""
from __future__ import annotations

from src._agent_image._mcp.tools_manager import get_manager_tools
from src._agent_image._mcp.tools_worker import get_worker_tools
from src.config_sync._tool_allowlist import render_manager_allowlist
from src.config_sync.claude_md_content import (
    ANALYST_CLAUDE_MD,
    AUDITOR_CLAUDE_MD,
    AUTOMATION_SCRIPT_DEV_CLAUDE_MD,
    MANAGER_ASSISTANT_CLAUDE_MD,
    MANAGER_CLAUDE_MD,
    SHARED_AGENT_WORK_RULES,
    SHARED_OFFICE_CLAUDE_MD,
)
from src.config_sync.claude_md_templates._shared_agent import (
    PLANNER_WORK_RULES,
)
from src.config_sync.claude_md_templates._system_agents import (
    BUILDER_CLAUDE_MD,
    DATA_CURATOR_CLAUDE_MD,
    FLOW_ARCHITECT_CLAUDE_MD,
    PLANNER_CLAUDE_MD,
)


def _render(template: str) -> str:
    out = template
    for slot, value in (
        ("{office_name}", "Test Office"),
        ("{manager_tool_allowlist}", render_manager_allowlist()),
        ("{office_specs_index}", ""),
        ("{office_output_style}", ""),
    ):
        if slot in out:
            out = out.replace(slot, value)
    return out


_SURFACES: dict[str, str] = {
    "manager": MANAGER_CLAUDE_MD,
    "office": SHARED_OFFICE_CLAUDE_MD,
    "shared_agent": SHARED_AGENT_WORK_RULES,
    "planner_work_rules": PLANNER_WORK_RULES,
    "analyst": ANALYST_CLAUDE_MD,
    "auditor": AUDITOR_CLAUDE_MD,
    "asd": AUTOMATION_SCRIPT_DEV_CLAUDE_MD,
    "manager_assistant": MANAGER_ASSISTANT_CLAUDE_MD,
    "planner": PLANNER_CLAUDE_MD,
    "builder": BUILDER_CLAUDE_MD,
    "flow_architect": FLOW_ARCHITECT_CLAUDE_MD,
    "data_curator": DATA_CURATOR_CLAUDE_MD,
}

# The historical KB-first mandate phrases, verbatim (whitespace-collapsed
# match). A playbook or tool description carrying ANY of these has
# re-mandated default KB reads — the exact posture spec §6.5 retired.
_KB_FIRST_MANDATE_PHRASES = (
    "KB-first",
    "`search_kb` before any research task",
    "Before researching, look for prior work",
    "check for existing research on the topic",
    "search them before re-researching",
    "Search those BEFORE re-researching",
    "BEFORE WebSearch when the task is about",
    "Before any research or analysis task, check for relevant existing",
    # 2026-09-02 prompt-surface sweep: three unconditional default-KB
    # process steps survived the original verbatim list (MA, ASD,
    # Planner playbooks). Ban their exact shapes so they cannot return.
    "Check existing knowledge: `mcp__cubicle-tools__search_kb`",
    "Call `mcp__cubicle-tools__search_kb` for existing documentation",
    "**Check existing knowledge** — `search_kb`",
    "search existing research / decisions",
)


def _norm(text: str) -> str:
    return " ".join(text.split())


def test_no_playbook_carries_a_kb_first_mandate() -> None:
    offenders: dict[str, list[str]] = {}
    for name, template in _SURFACES.items():
        rendered = _norm(_render(template))
        hits = [p for p in _KB_FIRST_MANDATE_PHRASES if _norm(p) in rendered]
        if hits:
            offenders[name] = hits
    assert not offenders, (
        f"KB-first mandate phrases re-appeared: {offenders} — the KB is an "
        "explicit-trigger reference library (spec §6.5); route defaults "
        "through memory instead."
    )


def _tool(tools: list[dict], name: str) -> dict:
    for t in tools:
        if t["name"] == name:
            return t
    raise AssertionError(f"{name} not found")


def test_kb_tool_descriptions_are_demoted_in_both_catalogs() -> None:
    for tools in (get_manager_tools(), get_worker_tools()):
        search = _norm(_tool(tools, "search_kb")["description"])
        assert "HUMAN-CURATED reference LIBRARY" in search
        assert "ONLY when" in search
        assert "limit default 5" in search
        # The mandate phrases must be gone from the descriptions too.
        for phrase in _KB_FIRST_MANDATE_PHRASES:
            assert _norm(phrase) not in search, phrase
        get_doc = _norm(_tool(tools, "get_kb_document")["description"])
        assert "ONLY" in get_doc
        assert "reference material" in get_doc


def test_recall_description_pins() -> None:
    # Both catalogs: scope is server-derived (no scope parameter), the
    # index-expansion slug path is taught, and the KB boundary is named.
    for tools in (get_manager_tools(), get_worker_tools()):
        desc = _norm(_tool(tools, "recall")["description"])
        assert "there is no scope parameter" in desc
        assert "`slug`" in desc
        assert "WHEN NOT" in desc
        assert "human-curated" in desc.lower()
    # The worker voice anchors scope to the TASK; the Manager voice to the
    # CONTEXT (General Chat = office level only).
    worker_desc = _norm(_tool(get_worker_tools(), "recall")["description"])
    assert "YOUR task's workstream" in worker_desc
    manager_desc = _norm(_tool(get_manager_tools(), "recall")["description"])
    assert "General Chat: office level only" in manager_desc


def test_remember_description_pins() -> None:
    desc = _norm(_tool(get_manager_tools(), "remember")["description"])
    # The closed trigger list is the tool's whole authority story.
    assert "Closed trigger list" in desc
    assert "remember this" in desc
    # Machine-written kinds are refused.
    assert "task summaries and lessons are captured automatically" in desc
    # Consent shape: office_wide lands PROPOSED for a human.
    assert "PROPOSED" in desc
    assert "Memory UI" in desc
    # The collections / flows / KB boundary (the when-NOT clause).
    assert "collections" in desc
    assert "`define_flow`" in desc
    assert "humans curate it" in desc
    # Schema: writable kinds exclude the machine-written two.
    schema = _tool(get_manager_tools(), "remember")["inputSchema"]
    assert schema["properties"]["kind"]["enum"] == [
        "decision", "preference", "fact", "how_to",
    ]
    assert set(schema["required"]) == {"kind", "title", "body"}


def test_manager_playbook_carries_the_ladder_and_remember_triggers() -> None:
    manager = _norm(_render(MANAGER_CLAUDE_MD))
    assert "## Memory, Knowledge Base and Office Files" in manager
    assert (
        "Context ladder, in order: the request/brief itself → workstream "
        "memory → office memory → the KB" in manager
    )
    assert "never as a default research step" in manager
    assert "closed trigger list" in manager
    assert '"remember this"' in manager
    assert "lands as PROPOSED for human approval" in manager


def test_manager_playbook_carries_the_memory_precedence_rule() -> None:
    # Final audit: a memory record and older office-instructions text can
    # contradict (the record is written AFTER the instructions and rides
    # user consent); the playbook must state which wins.
    manager = _norm(_render(MANAGER_CLAUDE_MD))
    assert (
        "On conflict, a memory record wins over older office-instructions "
        "text (the record is newer and user-approved)." in manager
    )


def test_no_playbook_instructs_writing_learnings_md() -> None:
    # T3.5: lessons are composed backend-side from the reviewer verdict.
    # Reads survive ONLY on the Planner surfaces (recall is a Planner
    # non-goal, spec §10); no surface may instruct WRITING the file.
    for name, template in _SURFACES.items():
        rendered = _norm(_render(template))
        if "learnings.md" not in rendered:
            continue
        assert name in ("planner", "planner_work_rules"), (
            f"{name} still references learnings.md — the file is retired "
            "(lessons ride workstream memory)"
        )
        for verb in ("Append", "Write a", "append a"):
            assert f"{verb} " not in rendered.split("learnings.md", 1)[1][:200], (
                f"{name} appears to instruct writing learnings.md"
            )


def test_office_wide_remember_is_general_chat_stripped() -> None:
    # T3.1: remember stays in the General-Chat strip (the office-wide
    # carve-out is NOT built v1); recall survives as a read.
    from src._agent_image.mcp_tool_server import (
        _BOARD_WRITE_ACTIONS,
        filter_general_chat_tools,
    )

    assert "memory_remember" in _BOARD_WRITE_ACTIONS
    surviving = {
        t["name"] for t in filter_general_chat_tools(get_manager_tools())
    }
    assert "remember" not in surviving
    assert "recall" in surviving


def test_default_kb_steps_are_rewritten_to_explicit_triggers() -> None:
    """The 2026-09-02 sweep findings, pinned POSITIVELY: the MA / ASD /
    Planner process steps and the office Common Tool Reference now route
    defaults through memory and gate the KB on explicit triggers."""
    from src.config_sync.claude_md_templates._system_agents import (
        _automation_script_developer as _asd,
        _manager_assistant as _ma,
        _planner as _pl,
    )
    from src.config_sync.claude_md_templates import _office as _off

    ma = _norm(_render(_ma.MANAGER_ASSISTANT_CLAUDE_MD))
    assert "never as a default step" in ma
    assert "Assigned references cite documents" in ma

    asd = _norm(_render(_asd.AUTOMATION_SCRIPT_DEV_CLAUDE_MD))
    assert "ONLY when the Brief's Assigned" in asd

    pl = _norm(_render(_pl.PLANNER_CLAUDE_MD))
    assert "**Check prior work**" in _render(_pl.PLANNER_CLAUDE_MD)
    assert "never as a default step" in pl

    off = _norm(_render(_off.SHARED_OFFICE_CLAUDE_MD))
    assert "human-curated reference library" in off
    assert "Decisions, lessons, and task summaries live in MEMORY" in off
