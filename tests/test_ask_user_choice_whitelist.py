"""ask_user_choice: tool schema, transform whitelist and backend handler
must agree on every parameter (X57).

The transform rebuilds the params from ``ASK_USER_CHOICE_PARAMS`` (the
catalog omits ``additionalProperties: false``, so the whitelist is the
schema guard's stand-in). A key the backend handler reads but the
whitelist omits is silently stripped — ``questions``, ``topic`` and
``flow_name`` each shipped broken that way once, and the run_flow
``materials`` carrier (X57) was unreachable from the Manager until this
pin. The backend side is read statically (AST), so the pin runs without
a database and fails closed in the monorepo.
"""
from __future__ import annotations

import ast

import pytest

from src._agent_image._mcp.tools_manager import get_manager_tools
from src._agent_image._mcp.transforms import (
    ASK_USER_CHOICE_PARAMS,
    transform_params,
)

from tests.backend_boundary import BACKEND_ROOT

_HANDLER_FILE = BACKEND_ROOT / "app" / "ws" / "tool_endpoint" / "_handlers_chat.py"
_ENTRY_POINT = "_handle_ask_user_choice"

# Keys the backend reads that deliberately never come from the model.
_NOT_FROM_THE_MODEL = {
    # Injected from the session env by the transform (L-6), never the model.
    "context_key": "session-bound context, injected by the transform",
    # Reserved bookkeeping (flow-intake verify F6): a lenient intake-only
    # param the backend tolerates; the record↔flow link ships when the tool
    # schema adds it (docs/03-contracts/rest-api.md §15.4).
    "flow": "reserved intake bookkeeping (F6), not in the tool schema",
}


def _params_keys_read(function: ast.AST) -> set[str]:
    keys: set[str] = set()
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "params"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            keys.add(node.args[0].value)
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "params"
            and isinstance(node.slice, ast.Constant)
        ):
            keys.add(node.slice.value)
    return keys


def _callees_given_params(function: ast.AST) -> set[str]:
    """Module functions the handler hands its whole ``params`` dict to."""
    callees: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            arguments = [*node.args, *(kw.value for kw in node.keywords)]
            if any(isinstance(a, ast.Name) and a.id == "params" for a in arguments):
                callees.add(node.func.id)
    return callees


def _backend_handler_keys() -> set[str]:
    if not _HANDLER_FILE.is_file():
        if BACKEND_ROOT.is_dir():
            raise AssertionError(f"ask_user_choice handler moved: {_HANDLER_FILE}")
        pytest.skip("Private backend is absent from this standalone CLI checkout")
    tree = ast.parse(_HANDLER_FILE.read_text())
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert _ENTRY_POINT in functions
    keys: set[str] = set()
    pending, visited = [_ENTRY_POINT], set()
    while pending:
        name = pending.pop()
        if name in visited or name not in functions:
            continue
        visited.add(name)
        keys |= _params_keys_read(functions[name])
        pending.extend(_callees_given_params(functions[name]))
    # The run_flow helper must be part of the walk (it reads materials).
    assert "_validate_run_flow_meta" in visited
    return keys


def _schema_properties() -> set[str]:
    tool = next(t for t in get_manager_tools() if t["name"] == "ask_user_choice")
    return set(tool["inputSchema"]["properties"])


def test_every_backend_read_key_reaches_the_backend() -> None:
    read = _backend_handler_keys()
    assert {"materials", "flow_name", "questions", "topic"} <= read
    missing = read - set(ASK_USER_CHOICE_PARAMS) - set(_NOT_FROM_THE_MODEL)
    assert not missing, (
        "ask_user_choice keys the backend reads are stripped by the "
        f"transform whitelist: {sorted(missing)} — add them to "
        "ASK_USER_CHOICE_PARAMS and the tool schema"
    )


def test_schema_and_whitelist_agree() -> None:
    assert _schema_properties() == set(ASK_USER_CHOICE_PARAMS)


def test_run_flow_materials_survive_the_transform(monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_KEY", "workstream:00000000-0000-0000-0000-000000000001")
    out = transform_params(
        "ask_user_choice",
        None,
        {
            "question": "Run the presale flow on this RFP?",
            "kind": "run_flow",
            "flow_name": "presale",
            "options": [],
            "materials": ["inbox/rfp.pdf"],
            "context_key": "workstream:forged",
            "unknown": "dropped",
        },
    )
    assert out["materials"] == ["inbox/rfp.pdf"]
    assert out["context_key"] == "workstream:00000000-0000-0000-0000-000000000001"
    assert "unknown" not in out


def _backend_constant(name: str) -> int:
    _backend_handler_keys()  # skips cleanly outside the monorepo
    for node in ast.parse(_HANDLER_FILE.read_text()).body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
            and isinstance(node.value, ast.Constant)
        ):
            return node.value.value
    raise AssertionError(f"{name} not found in {_HANDLER_FILE.name}")


def test_materials_schema_matches_backend_caps() -> None:
    tool = next(t for t in get_manager_tools() if t["name"] == "ask_user_choice")
    materials = tool["inputSchema"]["properties"]["materials"]
    assert materials["maxItems"] == _backend_constant("_MAX_RUN_FLOW_MATERIALS")
    assert materials["items"]["maxLength"] == _backend_constant(
        "_MAX_RUN_FLOW_MATERIAL_LEN"
    )
    assert "run_flow" in materials["description"]
    assert "materials" in tool["description"]
