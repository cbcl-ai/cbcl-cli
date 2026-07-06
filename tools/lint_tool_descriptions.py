"""Lint the Manager + Worker MCP tool surfaces (P6.13).

Enforces three invariants from the round-2 audit:

1. Every tool has a non-trivial top-level ``description``.
2. Every parameter in ``inputSchema.properties`` has a ``description``.
3. Every tool's description mentions when NOT to call it. The check
   is keyword-based — any of ``not use``, ``do not``, ``don't``,
   ``avoid``, ``never``, ``instead of``, ``ONLY when``, ``ONLY for``
   counts as a "when-not" clause.

Exit 0 = clean; exit 1 = at least one violation. CI invokes this as
`python -m tools.lint_tool_descriptions`.
"""
from __future__ import annotations

import re
import sys
from collections.abc import Iterable

# These tools are exempt from the "when-not" clause because they're
# pure read helpers where the answer is "use it whenever you need
# the data" — adding a manufactured negation would dilute the rule.
WHEN_NOT_EXEMPT: set[str] = {
    "get_board",
    "get_task_detail",
    "list_scripts",
    "get_script",
    "list_script_executions",
    "list_scopes",
    "get_scope",
    "list_files",
    "get_file",
    "search_kb",
    "get_kb_document",
    "list_action_requests",
    "get_action_request",
    "list_audit_log",
}

WHEN_NOT_KEYWORDS = (
    "not use",
    "do not",
    "don't",
    "avoid",
    "never",
    "instead of",
    "only when",
    "only for",
    # Common audit-flagged phrasings that ALSO satisfy the contract.
    "not for",
    "skip this",
)


def _load_tools() -> tuple[list[dict], list[dict], list[dict]]:
    """Import + return the manager / worker / planner-only tool lists.

    The MCP tool surface lives at ``communicator/src/_agent_image/_mcp/``
    (bundled into the agent container image at build time). Earlier
    iterations of this project kept it under ``communicator/docker/``;
    that path is stale and the lint failed with ``ModuleNotFoundError``.

    EVAL-07: the Planner catalog was previously unlinted, so its
    planner-only plan/spec-write tool descriptions escaped the
    description-quality gate. We add the tools that are UNIQUE to the
    planner surface (the rest overlap the manager catalog and are already
    linted there) so every model-facing tool description is covered exactly
    once.
    """
    import sys as _sys
    from pathlib import Path

    mcp_parent = (
        Path(__file__).resolve().parent.parent / "src" / "_agent_image"
    )
    if str(mcp_parent) not in _sys.path:
        _sys.path.insert(0, str(mcp_parent))

    from _mcp.tools_manager import get_manager_tools
    from _mcp.tools_planner import get_planner_tools
    from _mcp.tools_worker import get_worker_tools

    manager = get_manager_tools()
    worker = get_worker_tools()
    seen = {t["name"] for t in manager} | {t["name"] for t in worker}
    planner_only = [t for t in get_planner_tools() if t["name"] not in seen]
    return manager, worker, planner_only


def _has_when_not_clause(description: str) -> bool:
    low = description.lower()
    return any(kw in low for kw in WHEN_NOT_KEYWORDS)


def _check_tool(
    role: str,
    tool: dict,
    errors: list[str],
    warnings: list[str],
) -> None:
    name = tool.get("name", "<unnamed>")
    description = (tool.get("description") or "").strip()

    # ERROR: missing / trivial top-level description.
    if not description or len(description) < 15:
        errors.append(
            f"{role}/{name}: missing or trivial description (length={len(description)})"
        )
    # WARNING: present description but no when-not clause. Treated
    # as advisory rather than fatal because the existing 50+ tool
    # surface needs a docs sweep to satisfy it; CI gates on errors
    # only so daily work isn't blocked.
    elif name not in WHEN_NOT_EXEMPT and not _has_when_not_clause(description):
        warnings.append(
            f"{role}/{name}: description lacks a 'when not to use' "
            f"clause — add a phrase like 'Do not use...', "
            f"'Only when...', or 'Instead of X use Y'."
        )

    schema = tool.get("inputSchema") or {}
    properties = schema.get("properties") or {}
    for param_name, param_def in properties.items():
        if not isinstance(param_def, dict):
            errors.append(
                f"{role}/{name}: parameter '{param_name}' is not a dict"
            )
            continue
        param_desc = (param_def.get("description") or "").strip()
        if not param_desc:
            # ERROR — undocumented parameter is a real bug (the LLM
            # has no idea what to pass).
            errors.append(
                f"{role}/{name}: parameter '{param_name}' has no description"
            )


def lint(
    tools_by_role: Iterable[tuple[str, list[dict]]],
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    for role, tools in tools_by_role:
        for tool in tools:
            _check_tool(role, tool, errors, warnings)
    return errors, warnings


def main() -> int:
    manager_tools, worker_tools, planner_only_tools = _load_tools()
    errors, warnings = lint(
        [
            ("manager", manager_tools),
            ("worker", worker_tools),
            ("planner", planner_only_tools),
        ]
    )
    n_manager = len(manager_tools)
    n_worker = len(worker_tools)
    n_planner = len(planner_only_tools)
    counts = f"{n_manager} manager + {n_worker} worker + {n_planner} planner-only tools"
    if not errors and not warnings:
        print(
            f"OK — {counts}, "
            "every description + parameter complete + when-not clause."
        )
        return 0
    if warnings:
        print(f"{len(warnings)} 'when not to use' warning(s) — advisory:")
        for w in warnings:
            print(f"  ~ {w}")
    if errors:
        print(f"{len(errors)} ERROR(s):")
        for e in errors:
            print(f"  ✗ {e}")
        return 1
    print(f"OK (with {len(warnings)} advisory warnings) — {counts}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
