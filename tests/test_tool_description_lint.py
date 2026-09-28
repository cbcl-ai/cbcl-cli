"""Smoke test for the tool-description lint (P6.13, X41).

Runs the live lint against every served tool surface and asserts there
are ZERO errors. Warnings (missing 'when not to use' clauses) are
advisory and counted but don't fail the test.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _lint_module():
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from tools import lint_tool_descriptions

    return lint_tool_descriptions


def test_tool_descriptions_have_no_errors() -> None:
    module = _lint_module()
    catalogs = module._load_tools()
    errors, _warnings = module.lint(catalogs)

    assert errors == [], (
        "Tool-description lint surfaced errors that would have shipped:\n"
        + "\n".join(f"  - {e}" for e in errors)
    )

    # Sanity: we should actually be loading real tools from every role.
    roles = {role for role, _ in catalogs}
    assert {"manager", "worker", "planner", "flow-architect", "data-curator"} <= roles
    names_by_role = {role: {t["name"] for t in tools} for role, tools in catalogs}
    assert len(names_by_role["manager"]) > 5
    assert len(names_by_role["worker"]) > 5


def test_lint_covers_every_served_catalog_and_revoiced_copy() -> None:
    """X41: the Flow Architect / Data Curator catalogs and the re-voiced
    worker sub-catalog copies (ask-class and Manager Assistant move_task)
    are linted, each distinct definition exactly once."""
    module = _lint_module()
    catalogs = module._load_tools()
    move_task_copies = [
        tool
        for _, tools in catalogs
        for tool in tools
        if tool["name"] == "move_task"
    ]
    descriptions = {tool["description"] for tool in move_task_copies}
    # Manager, worker pool (reviewer voice), ask executor, MA ask executor.
    assert len(descriptions) == len(move_task_copies) >= 4
    labels = {role for role, _ in catalogs}
    assert any(label.startswith("worker[execute/analyst/ask") for label in labels)
    assert any(label.startswith("worker[execute/manager-assistant/ask") for label in labels)


def test_lint_distinguishes_error_from_warning() -> None:
    """P6.13 review fix: the severity split itself must work.

    Construct synthetic tool definitions that violate each rule and
    verify the lint puts the missing-parameter-description in
    errors and the missing-when-not-clause in warnings.
    """
    module = _lint_module()

    fake = [
        {
            "name": "frob_widget",
            "description": "Frobs the widget. A solid, useful tool.",
            # No "when-not" clause anywhere → WARNING.
            "inputSchema": {
                "type": "object",
                "properties": {
                    # Missing description → ERROR.
                    "widget_id": {"type": "string"},
                },
            },
        },
    ]
    errors, warnings = module.lint([("test", fake)])
    assert any("widget_id" in e for e in errors)
    assert any("when not to use" in w for w in warnings)


def test_lint_recurses_into_nested_parameters() -> None:
    """X41: nested object properties, array items and anyOf/oneOf/allOf
    branches are parameters the model fills too."""
    module = _lint_module()
    fake = [
        {
            "name": "frob_widget",
            "description": "Frobs the widget. Never use it for gadgets.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "brief": {
                        "type": "object",
                        "description": "Nested object.",
                        "properties": {"goal": {"type": "string"}},
                    },
                    "checks": {
                        "type": "array",
                        "description": "Array of objects.",
                        "items": {
                            "type": "object",
                            "properties": {"id": {"type": "string"}},
                        },
                    },
                    "mode": {
                        "description": "A union.",
                        "anyOf": [
                            {
                                "type": "object",
                                "properties": {"level": {"type": "integer"}},
                            },
                            {"type": "null"},
                        ],
                    },
                    "described": {
                        "type": "object",
                        "description": "Fully described.",
                        "properties": {
                            "ok": {"type": "string", "description": "Fine."},
                        },
                    },
                },
            },
        },
    ]
    errors, warnings = module.lint([("test", fake)])
    assert warnings == []
    flagged = sorted(e.split("'")[1] for e in errors)
    assert flagged == ["brief.goal", "checks[].id", "mode.level"]
