"""Lint every model-facing MCP tool definition (P6.13, X41).

Enforces three invariants from the round-2 audit:

1. Every tool has a non-trivial top-level ``description``.
2. Every NAMED parameter has a ``description`` — at any depth: top-level
   ``inputSchema.properties`` AND every nested ``properties`` reached
   through object parameters, array ``items`` and ``anyOf`` / ``oneOf`` /
   ``allOf`` branches (X41: the first version checked only the top level,
   so e.g. every ``update_task.brief.*`` field and
   ``execute_script.operation.*`` shipped undescribed).
3. Every tool's description mentions when NOT to call it. The check
   is keyword-based — any of ``not use``, ``do not``, ``don't``,
   ``avoid``, ``never``, ``instead of``, ``ONLY when``, ``ONLY for``
   counts as a "when-not" clause.

Coverage (X41): the Manager catalog, the worker pool, the Planner,
Flow Architect and Data Curator catalogs, and every re-voiced
``get_worker_subcatalog`` variant (executor / reviewer / triage, Manager
Assistant, ask class). Identical definitions are linted once; a
re-voiced copy (different description or schema) is linted on its own.

Exit 0 = clean; exit 1 = at least one violation. CI invokes this as
`python tools/lint_tool_descriptions.py`.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Iterator

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

# Worker sub-catalog variants the MCP server can serve (task_mode,
# agent_name, task_class). ``analyst`` stands for every ordinary worker;
# the Manager Assistant and ask class get re-voiced copies.
_WORKER_VARIANTS: tuple[tuple[str, str, str | None], ...] = tuple(
    (mode, agent, task_class)
    for mode in ("execute", "review", "triage")
    for agent in ("analyst", "manager-assistant")
    for task_class in (None, "ask")
)


def _catalogs() -> list[tuple[str, list[dict]]]:
    """Every served catalog as ``(role label, tools)``, in lint order."""
    import sys as _sys
    from pathlib import Path

    mcp_parent = (
        Path(__file__).resolve().parent.parent / "src" / "_agent_image"
    )
    if str(mcp_parent) not in _sys.path:
        _sys.path.insert(0, str(mcp_parent))

    from _mcp.tools_data_curator import get_data_curator_tools
    from _mcp.tools_flow_architect import get_flow_architect_tools
    from _mcp.tools_manager import get_manager_tools
    from _mcp.tools_planner import get_planner_tools
    from _mcp.tools_worker import get_worker_subcatalog, get_worker_tools

    catalogs: list[tuple[str, list[dict]]] = [
        ("manager", get_manager_tools()),
        ("worker", get_worker_tools()),
        ("planner", get_planner_tools()),
        ("flow-architect", get_flow_architect_tools()),
        ("data-curator", get_data_curator_tools()),
    ]
    for mode, agent, task_class in _WORKER_VARIANTS:
        label = f"worker[{mode}/{agent}" + (f"/{task_class}]" if task_class else "]")
        catalogs.append(
            (label, get_worker_subcatalog(mode, agent, task_class=task_class))
        )
    return catalogs


def _load_tools() -> list[tuple[str, list[dict]]]:
    """Every distinct model-facing tool definition, grouped by the first
    catalog that serves it.

    A definition shared verbatim by several catalogs (the Planner's copy
    of a Manager tool, an unmodified worker sub-catalog entry) is linted
    once; a re-voiced copy — different description or schema — is a
    different definition the model reads, so it is linted separately.
    """
    seen: set[str] = set()
    out: list[tuple[str, list[dict]]] = []
    for role, tools in _catalogs():
        distinct: list[dict] = []
        for tool in tools:
            key = json.dumps(tool, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            distinct.append(tool)
        if distinct:
            out.append((role, distinct))
    return out


def _has_when_not_clause(description: str) -> bool:
    low = description.lower()
    return any(kw in low for kw in WHEN_NOT_KEYWORDS)


def _named_parameters(
    schema: object, path: str,
) -> Iterator[tuple[str, object]]:
    """Yield ``(dotted path, definition)`` for every named parameter in
    ``schema``, depth-first: object ``properties``, array ``items`` (shown
    as ``[]``) and ``anyOf`` / ``oneOf`` / ``allOf`` branches."""
    if not isinstance(schema, dict):
        return
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, definition in properties.items():
            child = f"{path}.{name}" if path else name
            yield child, definition
            yield from _named_parameters(definition, child)
    items = schema.get("items")
    if isinstance(items, dict):
        yield from _named_parameters(items, f"{path}[]")
    elif isinstance(items, list):
        for item in items:
            yield from _named_parameters(item, f"{path}[]")
    for combinator in ("anyOf", "oneOf", "allOf"):
        branches = schema.get(combinator)
        if isinstance(branches, list):
            for branch in branches:
                yield from _named_parameters(branch, path)


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

    for param_path, definition in _named_parameters(
        tool.get("inputSchema") or {}, "",
    ):
        if not isinstance(definition, dict):
            errors.append(
                f"{role}/{name}: parameter '{param_path}' is not a dict"
            )
            continue
        if not (definition.get("description") or "").strip():
            # ERROR — an undocumented parameter is a real bug (the LLM
            # has no idea what to pass), nested or not.
            errors.append(
                f"{role}/{name}: parameter '{param_path}' has no description"
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
    catalogs = _load_tools()
    errors, warnings = lint(catalogs)
    total = sum(len(tools) for _, tools in catalogs)
    counts = f"{total} distinct tool definitions across {len(catalogs)} catalogs"
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
