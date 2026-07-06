"""T5.2.5 — pin the single-source blocker protocol against the live surfaces.

The ESCALATED template + blocker_class enum + routing sentence used to be
hand-duplicated and drifted into phantom `category=`/`severity=` args. These
pins fail loudly if the canonical constant drifts from the tool schema enum,
the shared playbook copy, or if a phantom arg creeps back into any swept site.
"""
from __future__ import annotations

import re
from pathlib import Path

from src.config_sync._blocker_protocol import (
    BLOCKER_CLASS_TABLE,
    ESCALATED_COMMENT_TEMPLATE,
)
from src.config_sync.claude_md_content import SHARED_AGENT_WORK_RULES
from src._agent_image._mcp.tools_worker import get_worker_tools


def _escalate_blocker_enum() -> set[str]:
    for t in get_worker_tools():
        if t["name"] == "escalate_blocker":
            return set(t["inputSchema"]["properties"]["blocker_class"]["enum"])
    raise AssertionError("escalate_blocker not in worker catalog")


def test_blocker_class_table_matches_tool_enum() -> None:
    table_classes = set(re.findall(r"`([a-z_]+)`", BLOCKER_CLASS_TABLE))
    assert table_classes == _escalate_blocker_enum()


def _deindent(text: str) -> str:
    # Collapse leading whitespace per line so an indented emission (the shared
    # block nests the fence/table inside a bullet) still matches the constant.
    return "\n".join(line.strip() for line in text.splitlines())


def test_shared_playbook_emits_the_canonical_template_and_table() -> None:
    # The canonical emitted copy lives in the shared agent rules; pin that it
    # matches the constant so the two can't drift (T5.3.4 collapses the rest).
    shared = _deindent(SHARED_AGENT_WORK_RULES)
    assert _deindent(ESCALATED_COMMENT_TEMPLATE) in shared
    assert _deindent(BLOCKER_CLASS_TABLE) in shared


def test_manager_assistant_playbook_emits_the_canonical_template_and_table() -> None:
    # CTX-06: the MA does NOT load the full SHARED_AGENT_WORK_RULES (it appends
    # only the two sections it needs), so its "escalate a blocker yourself"
    # instruction must carry its OWN copy of the template + class table rather
    # than a dangling "see your shared work rules" cross-reference. Pin that it
    # emits the SAME canonical constants so it can't drift from workers.
    from src.config_sync.claude_md_content import MANAGER_ASSISTANT_CLAUDE_MD

    ma = _deindent(MANAGER_ASSISTANT_CLAUDE_MD)
    assert _deindent(ESCALATED_COMMENT_TEMPLATE) in ma
    assert _deindent(BLOCKER_CLASS_TABLE) in ma
    # And the dangling cross-reference the fix removed must not creep back.
    assert "see your shared" not in MANAGER_ASSISTANT_CLAUDE_MD


def test_manager_assistant_carries_the_no_blocking_bash_rule() -> None:
    # CTX-06: the MA is the direct-Bash verification agent (Tier-0 one-shot
    # checks), so the no-blocking-Bash safety rule (Tier-2 session-churn fix)
    # MUST reach it even though it doesn't load the full shared rules.
    from src.config_sync.claude_md_content import MANAGER_ASSISTANT_CLAUDE_MD
    from src.config_sync.claude_md_templates._shared_agent import (
        LONG_RUNNING_BASH_RULE,
    )

    assert _deindent(LONG_RUNNING_BASH_RULE) in _deindent(MANAGER_ASSISTANT_CLAUDE_MD)


def _update_status_description() -> str:
    for t in get_worker_tools():
        if t["name"] == "update_status":
            return t["inputSchema"]["properties"]["comment"]["description"]
    raise AssertionError("update_status not in worker catalog")


def test_update_status_description_carries_the_four_section_template() -> None:
    # The SECOND emitted copy (T5.3.4) is the in-container update_status tool
    # description — it can't import the constant, so pin its 4 section labels +
    # the full blocker_class enum against the oracle here, or it can silently
    # drift from the shared-playbook copy (F-5.3.4-A / F-5.2.5-B).
    desc = _update_status_description()
    for label in (
        "ESCALATED (<blocker_class>):",
        "Original error:",
        "What I was trying to do:",
        "What I already tried:",
        "What's needed to resume:",
    ):
        assert label in desc, f"update_status desc missing section: {label!r}"
    # Every blocker_class from the canonical table must be named in the desc.
    for cls in re.findall(r"`([a-z_]+)`", BLOCKER_CLASS_TABLE):
        assert cls in desc, f"update_status desc missing blocker_class: {cls}"


def test_no_phantom_category_or_severity_args_in_swept_files() -> None:
    # The escalate_blocker schema has no category/severity; no prompt may
    # instruct passing them.
    root = Path(__file__).resolve().parent.parent / "src"
    swept = [
        root / "config_sync" / "claude_md_templates" / "_office.py",
        root / "config_sync" / "claude_md_templates" / "_system_agents"
        / "_manager_assistant.py",
        root / "config_sync" / "claude_md_templates" / "_system_agents"
        / "_automation_script_developer.py",
        root / "orchestrator" / "worker_prompt.py",
        root / "_agent_image" / "_mcp" / "tools_worker.py",
    ]
    bad: list[str] = []
    for path in swept:
        text = path.read_text()
        for m in re.finditer(r"(category|severity)=`?(credentials|user_input|"
                             r"infrastructure|cost|high|low|medium)", text):
            bad.append(f"{path.name}: {m.group(0)}")
    assert not bad, f"phantom escalate_blocker args remain: {bad}"


def test_no_surface_prescribes_the_two_call_block_flow() -> None:
    """WRK-01/TOOL-04: the canonical block flow is ONE call —
    update_status(blocked, comment='ESCALATED (<class>): …'). No auto-loaded
    playbook or tool description may still tell the worker to post the comment
    'via add_activity FIRST' (the contradictory two-call flow), and the backend
    routes from the comment prefix (not a details.blocker_class channel)."""
    from src.config_sync.claude_md_content import (
        MANAGER_ASSISTANT_CLAUDE_MD,
        SHARED_OFFICE_CLAUDE_MD,
    )
    from src.orchestrator import worker_prompt as _wp  # noqa: F401 (import smoke)

    surfaces = {
        "shared_agent": SHARED_AGENT_WORK_RULES,
        "office": SHARED_OFFICE_CLAUDE_MD,
        "manager_assistant": MANAGER_ASSISTANT_CLAUDE_MD,
    }
    for name, text in surfaces.items():
        low = text.lower()
        assert "add_activity first" not in low, (
            f"{name} still prescribes the two-call add_activity-first block flow"
        )

    # The update_status tool description must NOT tell the worker to post via
    # add_activity first, and must reference the ESCALATED prefix routing.
    us = next(t for t in get_worker_tools() if t["name"] == "update_status")
    comment_desc = us["inputSchema"]["properties"]["comment"]["description"].lower()
    assert "add_activity first" not in comment_desc
    assert "escalated (<class>)" in comment_desc or "escalated (" in comment_desc


def test_shared_playbook_routes_on_prefix_not_details_field() -> None:
    # AI-04: the shared rules must no longer claim the MA reads
    # `details.blocker_class` to route — routing is on the ESCALATED prefix.
    low = SHARED_AGENT_WORK_RULES.lower()
    assert "reads `details.blocker_class` to route" not in low
    assert "escalated (<class>) prefix" in low or "escalated (<class>)" in low


def test_executor_create_task_refusal_steers_to_propose_family() -> None:
    """TOOL-07 pin (review RP1-6): the executor create_task refusal must steer
    to the LIVE propose family, never the legacy add_activity/task_proposed
    path. Refusal strings are prompts — the model reads them at the exact
    moment it picks its next tool."""
    src = (
        Path(__file__).resolve().parent.parent
        / "src" / "_agent_image" / "mcp_tool_server.py"
    ).read_text()
    # Locate the executor create_task refusal text.
    idx = src.index("create_task is not available to executors")
    refusal = src[idx:idx + 300]
    assert "propose_task" in refusal
    assert "propose_subtask" in refusal
    assert "Action Request inbox" in refusal
    # The legacy steering must not creep back into this refusal.
    assert "task_proposed" not in refusal
