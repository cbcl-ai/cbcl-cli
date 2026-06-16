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
