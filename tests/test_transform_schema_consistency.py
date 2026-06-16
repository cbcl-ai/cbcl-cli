"""Transform ↔ schema consistency guard (T3.3.1, finding 04/F1).

The original F1 bug: the ``escalate_blocker`` tool REQUIRES
``blocker_class`` in its inputSchema, every playbook says it drives
routing — and the param transform silently threw it away, so every
agent escalation defaulted to ``category=workstream`` → Manager
auto-decide instead of the user Inbox.

Two generic guards so the NEXT dropped field fails CI:

1. **Input survival** — for every tool that declares a ``transform``,
   every REQUIRED inputSchema property's value must appear somewhere in
   the transform output (top level or nested in ``payload``).
2. **Backend payload validity** — for every ``propose_action`` tool,
   the transform output's ``payload`` must validate against the REAL
   backend per-type Pydantic validator
   (``backend/app/action_requests/schemas.py``), and every
   backend-required payload field must be present in the output.

Plus a regression pin for the F1 field itself.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest

from src._agent_image._mcp.tools_manager import get_manager_tools
from src._agent_image._mcp.tools_worker import get_worker_tools
from src._agent_image._mcp.transforms import transform_params

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_BACKEND_SCHEMAS = (
    _REPO_ROOT / "backend" / "app" / "action_requests" / "schemas.py"
)

# Valid UUID so payload fields typed ``uuid.UUID`` on the backend
# (CreateSubtaskPayload.parent_task_id, RequestReviewCheckPayload.task_id)
# validate when the transform injects TASK_ID.
_TASK_UUID = str(uuid.uuid4())


def _load_backend_schemas():
    """Import the REAL backend payload validators off disk.

    ``schemas.py`` is pure pydantic + stdlib, so loading it standalone
    (without the ``app`` package) is safe. Skip when the backend tree
    isn't present (standalone communicator checkout).
    """
    if not _BACKEND_SCHEMAS.exists():
        pytest.skip("backend tree not present — monorepo-only check")
    spec = importlib.util.spec_from_file_location(
        "backend_action_request_schemas", _BACKEND_SCHEMAS,
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec so pydantic can resolve the
    # module's deferred annotations (``from __future__ import
    # annotations`` + ``uuid.UUID`` fields) via the module globals.
    import sys

    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# Per-property sentinel overrides where a bare sentinel string would
# fail backend validation constraints.
_VALUE_OVERRIDES: dict[str, object] = {
    # UpdateTaskPayload whitelists change keys.
    "changes": {"priority": "high"},
    # SplitTaskInScopePayload.tasks: list[dict], min 1 item.
    "tasks": [{"title": "sentinel-split-task"}],
    "brief_hints": {"goal": "sentinel-goal"},
    "criterion_index": 3,
}


def _sentinel_for(name: str, prop_schema: dict) -> object:
    if name in _VALUE_OVERRIDES:
        return _VALUE_OVERRIDES[name]
    enum = prop_schema.get("enum")
    if enum:
        return enum[0]
    ptype = prop_schema.get("type", "string")
    if ptype in ("number", "integer"):
        return 7
    if ptype == "boolean":
        return True
    if ptype == "array":
        return [f"sentinel-{name}"]
    if ptype == "object":
        return {"k": f"sentinel-{name}"}
    return f"sentinel-{name}"


def _flatten_values(obj: object, acc: list) -> list:
    """Collect every leaf value (and every container) reachable in obj."""
    acc.append(obj)
    if isinstance(obj, dict):
        for v in obj.values():
            _flatten_values(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _flatten_values(v, acc)
    return acc


def _transformed_tools() -> list[dict]:
    tools: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for tool in get_worker_tools() + get_manager_tools():
        transform = tool.get("transform")
        if not transform:
            continue
        key = (tool["name"], transform)
        if key in seen:
            continue
        seen.add(key)
        tools.append(tool)
    return tools


def _build_params(tool: dict, required_only: bool = False) -> dict:
    schema = tool.get("inputSchema", {})
    props: dict = schema.get("properties", {})
    required = set(schema.get("required", ()))
    params = {}
    for name, prop_schema in props.items():
        if required_only and name not in required:
            continue
        params[name] = _sentinel_for(name, prop_schema)
    return params


@pytest.fixture(autouse=True)
def _agent_env(monkeypatch):
    monkeypatch.setenv("AGENT_NAME", "research-agent")
    monkeypatch.setenv("TASK_ID", _TASK_UUID)


# ── Guard 1: every required input field survives the transform ─────────


@pytest.mark.parametrize(
    "tool", _transformed_tools(), ids=lambda t: t["name"],
)
def test_required_input_fields_survive_transform(tool):
    """The F1 class of bug: a field the tool schema REQUIRES of the
    model must not be silently dropped by the param transform. We fill
    every required property with a distinctive sentinel and assert each
    sentinel is reachable somewhere in the transform output."""
    params = _build_params(tool)
    out = transform_params(tool["action"], tool["transform"], dict(params))
    flattened = _flatten_values(out, [])
    required = tool.get("inputSchema", {}).get("required", ())
    for name in required:
        assert params[name] in flattened, (
            f"tool '{tool['name']}': required input field '{name}' "
            f"(value {params[name]!r}) was dropped by transform "
            f"'{tool['transform']}' — output: {out!r}"
        )


# ── Guard 2: propose_action payloads satisfy the backend validators ────


def test_propose_action_payloads_validate_against_backend_schemas():
    """Round-trip every propose_action transform output through the
    REAL backend per-type payload validator. Catches both directions of
    drift: a transform emitting a shape the backend rejects, and a new
    backend-required field the transform doesn't supply."""
    schemas = _load_backend_schemas()
    validators: dict = schemas._PAYLOAD_VALIDATORS

    checked = 0
    for tool in _transformed_tools():
        if tool["action"] != "propose_action":
            continue
        params = _build_params(tool)
        out = transform_params(tool["action"], tool["transform"], dict(params))
        request_type = out["request_type"]
        validator = validators.get(request_type)
        assert validator is not None, (
            f"tool '{tool['name']}' emits request_type='{request_type}' "
            "with no backend payload validator"
        )
        payload = out["payload"]
        # Every backend-REQUIRED payload field must be in the output.
        for fname, finfo in validator.model_fields.items():
            if finfo.is_required():
                assert fname in payload, (
                    f"tool '{tool['name']}': backend payload field "
                    f"'{fname}' is required by {validator.__name__} but "
                    f"missing from the transform output: {payload!r}"
                )
        # And the whole payload must validate.
        try:
            validator.model_validate(payload)
        except Exception as exc:  # pragma: no cover - assertion path
            pytest.fail(
                f"tool '{tool['name']}': transform output payload does "
                f"not validate against {validator.__name__}: {exc}\n"
                f"payload: {payload!r}"
            )
        checked += 1
    assert checked >= 6, "expected to check the full propose_* family"


# ── F1 regression pins ─────────────────────────────────────────────────


def test_escalate_blocker_carries_blocker_class():
    out = transform_params(
        "propose_action",
        "escalate_blocker",
        {
            "blocker_summary": "Unipile 401 on every call.",
            "blocker_class": "missing_credential",
            "suggested_unblock": "Add UNIPILE_API_KEY office secret.",
            "justification": "Retried thrice; credential rejected.",
        },
    )
    assert out["payload"]["blocker_class"] == "missing_credential"
    assert out["payload"]["blocker_summary"] == "Unipile 401 on every call."


def test_escalate_blocker_omits_missing_blocker_class():
    """Legacy callers without blocker_class must not emit an empty
    string — the backend falls back to keyword routing on absence."""
    out = transform_params(
        "propose_action",
        "escalate_blocker",
        {
            "blocker_summary": "Stuck.",
            "justification": "Reasons.",
        },
    )
    assert "blocker_class" not in out["payload"]
