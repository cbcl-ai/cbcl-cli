"""Eval: the tool-error posture is single-sourced and cannot drift.

The posture — "an error means the server IS working and rejected your
input; READ it; retry at most ONCE (two failures = the input is wrong);
never conclude 'MCP unavailable' from an error response" — was hand-copied
into five playbook templates with no shared source, the exact drift shape
the repo already solved twice (the ``_blocker_protocol`` oracle module;
``LONG_RUNNING_BASH_RULE``'s one-template-two-renderings). It is now
rendered from ONE template in ``_shared_agent.py``:

- ``TOOL_ERROR_RULE``         → spliced into ``SHARED_AGENT_WORK_RULES``
                                (every executor playbook).
- ``TOOL_ERROR_RULE_CONSULT`` → spliced into ``PLANNER_WORK_RULES``.
- ``TOOL_ERROR_RULE_MA``      → the Manager Assistant's compact rendering
                                (its playbook runs just under a hard
                                budget ceiling, so it keeps the paragraph
                                shape; the core-sentence pins below keep
                                it semantically locked to the template).

STILL HAND-WRITTEN (their owners' consolidation, not covered here yet):
the Flow Architect and Data Curator playbook copies — the Curator's is
missing the never-conclude sentence today. Extend ``_SURFACES`` below
when those are consolidated.
"""
from __future__ import annotations

from src.config_sync.claude_md_content import MANAGER_ASSISTANT_CLAUDE_MD
from src.config_sync.claude_md_templates._shared_agent import (
    PLANNER_WORK_RULES,
    SHARED_AGENT_WORK_RULES,
    TOOL_ERROR_RULE,
    TOOL_ERROR_RULE_CONSULT,
    TOOL_ERROR_RULE_MA,
)


def _norm(text: str) -> str:
    return " ".join(text.split())


# Every surface that carries the posture, with which rendering it embeds.
_SURFACES = {
    "shared_agent": (SHARED_AGENT_WORK_RULES, TOOL_ERROR_RULE),
    "planner_rules": (PLANNER_WORK_RULES, TOOL_ERROR_RULE_CONSULT),
    "manager_assistant": (MANAGER_ASSISTANT_CLAUDE_MD, TOOL_ERROR_RULE_MA),
}


def test_each_surface_embeds_its_rendering_verbatim():
    """The surfaces splice the shared constants — a hand-edited fork inside
    a playbook (the old five-copies failure) fails here."""
    for name, (surface, rendering) in _SURFACES.items():
        assert rendering in surface, (
            f"{name} no longer embeds its shared tool-error rendering — "
            "edit the template/rendering in _shared_agent.py, not the copy"
        )


def test_core_posture_sentences_present_in_every_rendering():
    """The three invariant sentences of the posture, pinned per rendering
    (the MA's compact form is prose-shaped, so pin semantics not layout)."""
    for name, rendering in (
        ("executor", TOOL_ERROR_RULE),
        ("consult", TOOL_ERROR_RULE_CONSULT),
        ("ma", TOOL_ERROR_RULE_MA),
    ):
        norm = _norm(rendering)
        # (1) An error means the server IS working/up and rejected the input.
        assert "server IS" in norm and "reject" in norm, name
        # (2) Retry once; two failures = wrong input.
        assert "ONCE" in norm or "Retry at most once" in norm, name
        assert "Two failures = the input is wrong" in norm, name
        # (3) Never conclude the bridge is down from an error response.
        assert 'Never conclude "MCP unavailable"' in norm, name


def test_task_holding_renderings_forbid_blocking_on_tool_errors():
    """The two renderings for roles that can move tasks to ``blocked``
    (executors, the MA) must carry the never-block-over-a-tool-error rule.
    The consult rendering deliberately does not — the Planner holds no
    ``update_status`` and cannot block a task."""
    assert "Never move a task to `blocked` over a tool error" in _norm(
        TOOL_ERROR_RULE
    )
    assert "never move a task to `blocked` over a tool error" in _norm(
        TOOL_ERROR_RULE_MA
    )
