"""Smoke test for the tool-description lint (P6.13).

Runs the live lint against the actual Manager + Worker tool surfaces
and asserts there are ZERO errors. Warnings (missing 'when not to
use' clauses) are advisory and counted but don't fail the test.
"""
from __future__ import annotations

import sys
from pathlib import Path


def test_tool_descriptions_have_no_errors() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from tools.lint_tool_descriptions import _load_tools, lint

    manager_tools, worker_tools = _load_tools()
    errors, _warnings = lint(
        [("manager", manager_tools), ("worker", worker_tools)]
    )

    assert errors == [], (
        "Tool-description lint surfaced errors that would have shipped:\n"
        + "\n".join(f"  - {e}" for e in errors)
    )

    # Sanity: we should actually be loading real tools.
    assert len(manager_tools) > 5
    assert len(worker_tools) > 5


def test_lint_distinguishes_error_from_warning() -> None:
    """P6.13 review fix: the severity split itself must work.

    Construct synthetic tool definitions that violate each rule and
    verify the lint puts the missing-parameter-description in
    errors and the missing-when-not-clause in warnings.
    """
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from tools.lint_tool_descriptions import lint

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
    errors, warnings = lint([("test", fake)])
    assert any("widget_id" in e for e in errors)
    assert any("when not to use" in w for w in warnings)
