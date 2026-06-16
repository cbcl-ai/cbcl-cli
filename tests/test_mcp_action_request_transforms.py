"""Unit tests for the worker-side Action Request MCP transforms.

The transforms live in ``mcp_tool_server._transform_params`` and rewrite
typed worker-friendly tool arguments (``propose_subtask``,
``escalate_blocker``, etc.) into the generic ``propose_action`` payload
the backend dispatcher expects:

    {request_type, payload, justification, source_task_id, requesting_agent}

Backend contract: ``app/ws/tool_endpoint.py::_handle_propose_action``
reads ``request_type`` (must be in REQUEST_TYPES), ``payload`` (dict),
``justification``, ``requesting_agent`` (or ``actor`` fallback), and
``source_task_id`` from params. These tests verify every transform
produces a correctly-shaped payload that satisfies that contract.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

# Load the MCP tool server module off disk — it lives inside
# ``src/_agent_image/`` (the bundled agent-image asset dir) so it
# ships with the wheel AND copies into the agent Docker image as a
# standalone file at ``/opt/cubicle/mcp_tool_server.py``.
_MCP_PATH = (
    Path(__file__).resolve().parent.parent
    / "src" / "_agent_image" / "mcp_tool_server.py"
)
_spec = importlib.util.spec_from_file_location("mcp_tool_server", _MCP_PATH)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_transform_params = _mod._transform_params


def _setenv_for_worker(monkeypatch, agent="research-agent", task="task-uuid"):
    """The transforms read AGENT_NAME and TASK_ID from ``os.environ`` at
    call time (post P3-F: the transforms helper lives in ``_mcp.transforms``
    and no longer reaches into the entrypoint's module globals). The
    legacy globals are still exposed by the entrypoint for back-compat
    but the helpers prefer ``os.environ`` so a monkeypatched env wins."""
    monkeypatch.setenv("AGENT_NAME", agent)
    monkeypatch.setenv("TASK_ID", task)
    # Also patch the legacy module globals so any future re-import path
    # that still reads them sees the same values.
    monkeypatch.setattr(_mod, "AGENT_NAME", agent, raising=False)
    monkeypatch.setattr(_mod, "TASK_ID", task, raising=False)


def test_propose_subtask_shape(monkeypatch):
    _setenv_for_worker(monkeypatch)
    out = _transform_params(
        action="propose_action",
        transform="propose_subtask",
        params={
            "title": "Enrich profiles",
            "brief_hints": {"goal": "..."},
            "justification": "Sourcing produced 100 profiles needing enrichment.",
        },
    )
    assert out["request_type"] == "create_subtask"
    assert out["payload"]["title"] == "Enrich profiles"
    assert out["payload"]["brief_hints"] == {"goal": "..."}
    # parent_task_id is informational; the backend dispatcher derives
    # the parent from source_task_id and the field is OPTIONAL on the
    # validator. Keep it in the transform so audit logs show what
    # the worker meant, but don't assert on its UUID-validity here —
    # the dispatcher's pre-validation normalization drops it when
    # empty (worker without TASK_ID).
    assert out["payload"]["parent_task_id"] == "task-uuid"
    assert out["justification"].startswith("Sourcing")
    assert out["source_task_id"] == "task-uuid"
    assert out["requesting_agent"] == "research-agent"


def test_propose_subtask_with_empty_task_id_still_emits_string(monkeypatch):
    """Worker without TASK_ID set still produces a tool call. The
    backend dispatcher drops the empty parent_task_id before the
    validator runs (see ``_handle_propose_action`` normalization)."""
    _setenv_for_worker(monkeypatch, agent="research-agent", task="")
    out = _transform_params(
        action="propose_action",
        transform="propose_subtask",
        params={"title": "X", "justification": "Y"},
    )
    # parent_task_id is "" — the BACKEND drops it. The transform
    # itself stays simple and just forwards what env exposes.
    assert out["payload"]["parent_task_id"] == ""
    assert out["source_task_id"] == ""


def test_propose_split_into_scope_shape(monkeypatch):
    _setenv_for_worker(monkeypatch)
    out = _transform_params(
        action="propose_action",
        transform="propose_split_into_scope",
        params={
            "scope_short_key": "Auth",
            "scope_name": "Auth Migration",
            "tasks": [{"title": "design"}, {"title": "implement"}],
            "justification": "Scope-worthy.",
        },
    )
    assert out["request_type"] == "split_into_scope"
    assert out["payload"]["scope_short_key"] == "Auth"
    assert out["payload"]["scope_name"] == "Auth Migration"
    assert len(out["payload"]["tasks"]) == 2
    assert out["source_task_id"] == "task-uuid"


def test_propose_update_task_shape(monkeypatch):
    _setenv_for_worker(monkeypatch)
    out = _transform_params(
        action="propose_action",
        transform="propose_update_task",
        params={
            "task_id": "WR-001.T05",
            "changes": {"priority": "high"},
            "justification": "Blocking downstream.",
        },
    )
    assert out["request_type"] == "update_task"
    assert out["payload"] == {
        "task_id": "WR-001.T05",
        "changes": {"priority": "high"},
    }


def test_escalate_blocker_shape(monkeypatch):
    _setenv_for_worker(monkeypatch)
    out = _transform_params(
        action="propose_action",
        transform="escalate_blocker",
        params={
            "blocker_summary": "API key invalid.",
            "suggested_unblock": "Refresh in skill.",
            "justification": "Got 401 on every call.",
        },
    )
    assert out["request_type"] == "escalate_blocker"
    assert out["payload"]["blocker_summary"] == "API key invalid."
    assert out["payload"]["suggested_unblock"] == "Refresh in skill."


def test_request_clarification_shape(monkeypatch):
    _setenv_for_worker(monkeypatch)
    out = _transform_params(
        action="propose_action",
        transform="request_clarification",
        params={
            "question": "What region?",
            "justification": "Brief says 'LATAM' but DB constrains to one country.",
        },
    )
    assert out["request_type"] == "request_clarification"
    assert out["payload"] == {"question": "What region?"}


def test_request_review_check_omits_optional_index(monkeypatch):
    _setenv_for_worker(monkeypatch)
    out = _transform_params(
        action="propose_action",
        transform="request_review_check",
        params={"justification": "Criterion 3 is a judgement call."},
    )
    assert out["request_type"] == "request_review_check"
    assert "criterion_index" not in out["payload"]
    assert out["payload"]["task_id"] == "task-uuid"


def test_request_review_check_includes_index_when_supplied(monkeypatch):
    _setenv_for_worker(monkeypatch)
    out = _transform_params(
        action="propose_action",
        transform="request_review_check",
        params={"criterion_index": 2, "justification": "..."},
    )
    assert out["payload"]["criterion_index"] == 2


def test_propose_artifact_handoff_shape(monkeypatch):
    _setenv_for_worker(monkeypatch)
    out = _transform_params(
        action="propose_action",
        transform="propose_artifact_handoff",
        params={
            "target_task_id": "WR-001.T07",
            "file_path": "/workspace/outputs/WR/profiles.json",
            "justification": "T07 imports profiles.",
        },
    )
    assert out["request_type"] == "propose_artifact_handoff"
    assert out["payload"] == {
        "source_task_id": "task-uuid",
        "target_task_id": "WR-001.T07",
        "file_path": "/workspace/outputs/WR/profiles.json",
    }


def test_legacy_propose_task_unchanged(monkeypatch):
    """Legacy propose_task transform is preserved — the bridge in
    backend ``add_activity`` continues to populate the inbox."""
    _setenv_for_worker(monkeypatch)
    out = _transform_params(
        action="add_activity",
        transform="propose_task",
        params={"task_id": "task-uuid", "content": "New thing"},
    )
    assert out["event_type"] == "task_proposed"
    assert out["actor"] == "research-agent"
    assert out["content"] == "New thing"


def test_unknown_transform_passes_params_through(monkeypatch):
    _setenv_for_worker(monkeypatch)
    out = _transform_params(
        action="some_action",
        transform=None,
        params={"x": 1},
    )
    assert out == {"x": 1}


def test_missing_agent_name_falls_back_to_worker(monkeypatch):
    """An empty AGENT_NAME shouldn't crash — fall back to the literal
    string 'worker'. This is defensive; the real fix for missing
    AGENT_NAME is a CRITICAL log at server startup (see
    test_mcp_tool_filter.py)."""
    _setenv_for_worker(monkeypatch, agent="", task="task-uuid")
    out = _transform_params(
        action="propose_action",
        transform="propose_subtask",
        params={"title": "X", "justification": "Y"},
    )
    assert out["requesting_agent"] == "worker"


# ── T5.1.2 — add_activity details channel (04/F3) ───────────────────────


def test_add_activity_forwards_whitelisted_details(monkeypatch):
    _setenv_for_worker(monkeypatch, agent="research-agent")
    out = _transform_params(
        action="add_activity",
        transform="add_activity",
        params={
            "task_id": "t1",
            "event_type": "comment",
            "content": "ESCALATED (auth_failed): token rejected",
            "details": {"blocker_class": "auth_failed", "junk": 123},
        },
    )
    # The blocker_class survives; the unknown key is dropped by the
    # _ACTIVITY_DETAIL_KEEP whitelist.
    assert out["details"] == {"blocker_class": "auth_failed"}
    assert out["actor"] == "research-agent"
    assert out["content"].startswith("ESCALATED")


def test_add_activity_omits_details_when_absent(monkeypatch):
    _setenv_for_worker(monkeypatch)
    out = _transform_params(
        action="add_activity",
        transform="add_activity",
        params={"task_id": "t1", "event_type": "checkpoint", "content": "x"},
    )
    assert "details" not in out


def test_add_activity_omits_details_when_only_unknown_keys(monkeypatch):
    _setenv_for_worker(monkeypatch)
    out = _transform_params(
        action="add_activity",
        transform="add_activity",
        params={
            "task_id": "t1", "event_type": "comment", "content": "x",
            "details": {"junk": 1, "more_junk": 2},
        },
    )
    # Nothing survives the whitelist → no empty details blob emitted.
    assert "details" not in out
