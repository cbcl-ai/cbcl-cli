"""Backend and standalone admission agree on typed, current advisory intent."""
import copy
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.backend_client import _is_advisory_parent_followup, _review_request_blocks
from src._agent_image._mcp.tools_worker import get_worker_tools
from src._agent_image._mcp.transforms import transform_params
from tests.test_review_recovery_admission import mock_api
from src.backend_client import task_has_pending_review_decision


TASK = {"id": str(uuid.uuid4()), "office_id": str(uuid.uuid4()), "workstream_id": str(uuid.uuid4()),
    "execution_cycle": 2, "task_contract_digest": "a" * 64}
REQUEST = {"id": str(uuid.uuid4()), "office_id": TASK["office_id"], "workstream_id": TASK["workstream_id"],
    "source_task_id": TASK["id"], "requesting_agent": "analyst", "request_type": "create_subtask",
    "status": "pending", "category": "workstream", "requires_user": False,
    "payload": {"title": "Independent future comparison", "parent_task_id": TASK["id"],
        "parent_dependency": "advisory", "creation_contract_version": 1,
        "advisory_parent": {"version": 1, "task_id": TASK["id"], "execution_cycle": 2, "contract_digest": "a" * 64}}}


@pytest.mark.parametrize("mutation", ["none", "required", "legacy", "human", "unknown", "mixed", "cycle", "contract", "task", "office", "workstream", "version", "category", "missing_context", "malformed_resource", "mixed_hints"])
def test_backend_runtime_parity_fails_closed_on_uncertain_exemption(mutation):
    request, task = copy.deepcopy(REQUEST), dict(TASK)
    payload = request["payload"]
    if mutation == "required": payload["parent_dependency"] = "required"
    elif mutation == "legacy": payload.pop("parent_dependency")
    elif mutation == "human": request["requires_user"] = True
    elif mutation == "unknown": payload["unknown"] = "decision"
    elif mutation == "mixed": payload["rework_cap"] = True
    elif mutation == "cycle": task["execution_cycle"] += 1
    elif mutation == "contract": task["task_contract_digest"] = "b" * 64
    elif mutation == "task": payload["parent_task_id"] = str(uuid.uuid4())
    elif mutation == "office": request["office_id"] = str(uuid.uuid4())
    elif mutation == "workstream": request["workstream_id"] = str(uuid.uuid4())
    elif mutation == "version": payload["advisory_parent"]["version"] = True
    elif mutation == "category": request["category"] = "user_input"
    elif mutation == "missing_context": task.pop("task_contract_digest")
    elif mutation == "malformed_resource": payload["execution_resources"] = [False]
    elif mutation == "mixed_hints": payload["brief_hints"] = {"blocker_summary": "Still required"}
    expected = mutation == "none"
    assert _is_advisory_parent_followup(request, task) is expected
    assert _review_request_blocks(request, task) is not expected
    if not (Path(__file__).resolve().parents[2] / "backend/app/tasks/parent_followups.py").exists():
        pytest.skip("backend parity requires monorepo")
    from app.tasks.parent_followups import is_advisory_parent_followup
    backend_request = SimpleNamespace(**{key: uuid.UUID(value) if key in {"office_id", "workstream_id", "source_task_id"} else value for key, value in request.items()})
    backend_task = SimpleNamespace(**{key: uuid.UUID(value) if key in {"id", "office_id", "workstream_id"} else value for key, value in task.items()})
    assert is_advisory_parent_followup(backend_request, backend_task, task.get("task_contract_digest")) is expected


@pytest.mark.parametrize("dependency", [None, "required", "advisory"])
def test_proposal_wire_preserves_explicit_intent_and_defaults_to_required(monkeypatch, dependency):
    tool = next(tool for tool in get_worker_tools() if tool["name"] == "propose_subtask")
    params = {"title": "Later work", "justification": "Future improvement"}
    if dependency is not None:
        params["parent_dependency"] = dependency
    monkeypatch.setenv("TASK_ID", TASK["id"])
    result = transform_params(tool["action"], tool["transform"], params)
    assert result["payload"]["parent_dependency"] == (dependency or "required")
    assert "advisory_parent" not in result["payload"]


async def test_list_snapshot_advisory_does_not_mask_a_required_neighbor(monkeypatch):
    rows = [copy.deepcopy(REQUEST)]
    snapshot = {"items": rows, "total": len(rows)}
    mock_api(monkeypatch, [snapshot])
    assert await task_has_pending_review_decision("http://backend", TASK["office_id"], TASK["id"], "token", task=TASK) is False
    required = copy.deepcopy(REQUEST)
    required["payload"]["parent_dependency"] = "required"
    rows.append(required)
    # mock response data is a snapshot; keep its total consistent as well.
    snapshot["total"] = len(rows)
    assert await task_has_pending_review_decision("http://backend", TASK["office_id"], TASK["id"], "token", task=TASK) is True
