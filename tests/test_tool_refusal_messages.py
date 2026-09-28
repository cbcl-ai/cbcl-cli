"""Model-facing refusal/error strings must name REAL tools (X08/X65).

Tool results are prompts too: when a guard refuses a call, the text it
returns is the model's only instruction for what to do next. Several
refusals used to redirect agents to ``propose_action`` (the backend
umbrella ACTION behind the typed ``propose_*`` tools — no catalog
registers a tool by that name), to a nonexistent ``retry_bootstrap``
tool, or to ``request_type=archive_task`` (not a request type). The
prompt-reference evals scan playbooks only, so these strings slipped
through.

This module scans every non-docstring string literal in the in-container
MCP server and the backend tool-call handlers for tool-shaped tokens
(snake_case identifiers whose first word is a verb some real tool uses)
and fails on any token that no catalog registers and that is not a
documented non-tool identifier (request type, retired action named in a
teaching error, internal transport action).
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest

from src._agent_image._mcp.tools_data_curator import get_data_curator_tools
from src._agent_image._mcp.tools_flow_architect import get_flow_architect_tools
from src._agent_image._mcp.tools_manager import get_manager_tools
from src._agent_image._mcp.tools_planner import get_planner_tools
from src._agent_image._mcp.tools_worker import (
    get_worker_subcatalog,
    get_worker_tools,
)

from tests.backend_boundary import BACKEND_ROOT

_COMM_ROOT = Path(__file__).resolve().parents[1]
_AGENT_IMAGE = _COMM_ROOT / "src" / "_agent_image"

# Identifiers that look like tools but are deliberately NOT tools. Every
# entry carries its reason; adding one is a reviewed decision.
_NON_TOOL_IDENTIFIERS: dict[str, str] = {
    # Internal transport actions the MCP server calls itself (never
    # named to the model as something to call).
    "record_script_execution": "internal backend action (script row)",
    "request_outbox_scan": "internal backend action (outbox nudge)",
    # Retired actions: the backend's teaching error names the retired
    # verb it received and points at the replacement.
    "update_workstream_plan": "retired action named in its teaching error",
    "get_workstream_plan": "retired action (read-compat only)",
    # A field name, not a tool: the ActionRequest ``request_type`` the typed
    # propose_* tools set through their transforms (never a tool parameter).
    "request_type": "action-request field named in propose_* descriptions",
}

_TOKEN = re.compile(r"\b([a-z]+(?:_[a-z0-9]+)+)\b")


def _property_names(schema: object) -> set[str]:
    """Every property name declared anywhere in an inputSchema."""
    names: set[str] = set()
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            names |= set(properties)
        for value in schema.values():
            names |= _property_names(value)
    elif isinstance(schema, list):
        for value in schema:
            names |= _property_names(value)
    return names


def _all_catalog_tools() -> list[dict]:
    tools: list[dict] = []
    for loader in (
        get_manager_tools,
        get_worker_tools,
        get_planner_tools,
        get_flow_architect_tools,
        get_data_curator_tools,
    ):
        tools += loader()
    for mode in ("execute", "review", "triage"):
        tools += get_worker_subcatalog(mode, "manager-assistant")
    return tools


def _catalog_names() -> set[str]:
    return {tool["name"] for tool in _all_catalog_tools()}


def _parameter_names() -> set[str]:
    """T26: argument names (``request_id``, ``new_status``) are exempt only
    when a real inputSchema declares them — a suffix rule let phantom tools
    such as ``get_task_status`` through."""
    names: set[str] = set()
    for tool in _all_catalog_tools():
        names |= _property_names(tool.get("inputSchema"))
    return names


def _request_types() -> set[str]:
    """Backend REQUEST_TYPES, read statically (no backend import needed).

    Fail CLOSED in the monorepo: a moved/renamed ``request_types.py`` (or a
    parse that finds nothing) must be a loud failure, never a silently empty
    set that makes the known-name and ``archive_task`` checks vacuous. Only a
    standalone CLI checkout (no backend tree at all) returns an empty set.
    """
    if not BACKEND_ROOT.is_dir():
        return set()
    path = BACKEND_ROOT / "app" / "action_requests" / "request_types.py"
    assert path.is_file(), (
        f"{path} is missing — the backend REQUEST_TYPES source moved; update "
        "this scanner rather than letting it read an empty set"
    )
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if re.fullmatch(r"[a-z]+(?:_[a-z]+)+", node.value):
                found.add(node.value)
    assert (
        "escalate_blocker" in found
    ), f"parsed no recognisable request types from {path}"
    return found


def _communicator_request_types() -> set[str]:
    """The ``request_type`` values the propose_* transforms send, read
    statically from ``transforms.py``. A standalone CLI checkout has no
    backend REQUEST_TYPES, so this keeps the scan meaningful there."""
    path = _AGENT_IMAGE / "_mcp" / "transforms.py"
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "request_type"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    found.add(value.value)
    assert "escalate_blocker" in found, f"parsed no request types from {path}"
    return found


_LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}


def _model_facing_strings(path: Path) -> list[tuple[int, str]]:
    """Every string literal except docstrings and logging-call arguments
    (operator logs never reach the model)."""
    tree = ast.parse(path.read_text())
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _LOG_METHODS
        ):
            for arg in node.args:
                docstrings.update(id(sub) for sub in ast.walk(arg))
        if isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            # A bare identifier (dict key, action name) is not prose.
            and " " in node.value
        ):
            out.append((node.lineno, node.value))
    return out


def _phantom_tokens(text: str, known: set[str], prefixes: set[str]) -> set[str]:
    phantoms: set[str] = set()
    for match in _TOKEN.finditer(text):
        token = match.group(1)
        if token.split("_")[0] not in prefixes:
            continue
        if token in known:
            continue
        phantoms.add(token)
    return phantoms


def _scanned_files() -> list[Path]:
    files = sorted(_AGENT_IMAGE.glob("*.py")) + sorted(
        (_AGENT_IMAGE / "_mcp").glob("*.py")
    )
    backend_ws = BACKEND_ROOT / "app" / "ws"
    if backend_ws.is_dir():
        files += sorted((backend_ws / "tool_endpoint").glob("*.py"))
        files.append(backend_ws / "office_file_handler.py")
    return files


def _known_and_prefixes() -> tuple[set[str], set[str]]:
    catalog = _catalog_names()
    known = (
        catalog
        | _parameter_names()
        | _request_types()
        | _communicator_request_types()
        | set(_NON_TOOL_IDENTIFIERS)
    )
    prefixes = {name.split("_")[0] for name in catalog}
    return known, prefixes


def test_scanner_is_not_vacuous() -> None:
    known, prefixes = _known_and_prefixes()
    assert "propose_action" not in _catalog_names()
    assert _phantom_tokens(
        "Use propose_action or call the retry_bootstrap tool.",
        known,
        prefixes,
    ) == {"propose_action", "retry_bootstrap"}
    assert not _phantom_tokens(
        "Use `escalate_blocker` with source_task_id and `propose_task`.",
        known,
        prefixes,
    )
    # T26: parameter-SHAPED phantom tools are no longer exempt by suffix.
    assert "get_task_status" not in known and "update_task_status" not in known
    assert _phantom_tokens(
        "Call get_task_status, then update_task_status.",
        known,
        prefixes,
    ) == {"get_task_status", "update_task_status"}


def test_refusal_strings_name_only_real_tools() -> None:
    known, prefixes = _known_and_prefixes()
    offenders: list[str] = []
    files = _scanned_files()
    assert any(f.name == "mcp_tool_server.py" for f in files)
    for path in files:
        for lineno, text in _model_facing_strings(path):
            for token in sorted(_phantom_tokens(text, known, prefixes)):
                offenders.append(f"{path.name}:{lineno}: {token}")
    assert not offenders, (
        "Model-facing strings name tools no catalog registers — name a "
        "real tool instead (or document a deliberate non-tool identifier "
        "in _NON_TOOL_IDENTIFIERS):\n" + "\n".join(offenders)
    )


def test_communicator_request_types_exist_in_backend() -> None:
    if not BACKEND_ROOT.is_dir():
        pytest.skip("Private backend is absent from this standalone CLI checkout")
    missing = _communicator_request_types() - _request_types()
    assert (
        not missing
    ), f"transforms.py sends request types the backend does not know: {sorted(missing)}"


def test_backend_refusals_are_scanned_in_monorepo() -> None:
    if not BACKEND_ROOT.is_dir():
        pytest.skip("Private backend is absent from this standalone CLI checkout")
    names = {path.name for path in _scanned_files()}
    assert {"_dispatch.py", "_handlers_tasks.py", "office_file_handler.py"} <= names
    request_types = _request_types()
    assert {
        "escalate_blocker",
        "create_subtask",
        "request_review_check",
    } <= request_types
    assert "archive_task" not in request_types


def _load_mcp_server():
    path = _AGENT_IMAGE / "mcp_tool_server.py"
    spec = importlib.util.spec_from_file_location("mcp_tool_server_refusals", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_triage_paths_text_uses_the_canonical_letter_map() -> None:
    text = _load_mcp_server().TRIAGE_PATHS_TEXT
    triage = {t["name"] for t in get_worker_subcatalog("triage", "manager-assistant")}
    # The one tool it names only to rule out is not served in triage at all.
    assert "retry_blocked_task" not in triage
    for tool in re.findall(r"`([a-z_]+)`", text):
        if tool == "retry_blocked_task":
            continue
        assert (
            tool in triage
        ), f"triage refusal names {tool!r}, not in the MA triage catalog"
    # A=answer, B=helper task, C=escalate/clarify — the MA playbook's map.
    assert text.index("(A)") < text.index("`add_activity`") < text.index("(B)")
    assert text.index("(B)") < text.index("`create_task`") < text.index("(C)")
    assert "`escalate_blocker`" in text[text.index("(C)") :]
    # Shared decision 1: Path A also files the approval request that gives
    # the answered task an exit (the answer alone resumes nothing).
    path_a = text[text.index("(A)") : text.index("(B)")]
    assert "`escalate_blocker` (blocker_class='ambiguous_spec'" in path_a
    assert "then stop" not in path_a
    # retry_blocked_task is never a triage resolution (final review P8).
    assert "`retry_blocked_task` is NOT a triage path" in text
    assert "Path D" not in text
    assert "(D)" not in text
