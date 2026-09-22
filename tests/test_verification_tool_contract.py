"""Real optional plan composition and wire schemas match the backend contract."""
import importlib.util
import json
from pathlib import Path
import sys
import uuid

import pytest
from jsonschema import Draft202012Validator

from src._agent_image._mcp.tools_manager import get_manager_tools
from src._agent_image._mcp.tools_planner import get_planner_tools
from src._agent_image._mcp.tools_worker import get_worker_subcatalog
from src._agent_image._mcp.transforms import transform_params
from src.orchestrator.worker_prompt import build_worker_prompt


PLAN = {"version": 1, "checks": [{"id": "invoice_totals", "criterion_indices": [1],
    "owner": "executor", "method": "Reconcile source records", "scope": "September invoice snapshot",
    "required": True, "freshness": "current_cycle"}]}


def _tools(catalog):
    return {tool["name"]: tool for tool in catalog}


def _backend_schema_module():
    path = Path(__file__).resolve().parents[2] / "backend/app/tasks/verification_schemas.py"
    if not path.exists():
        pytest.skip("standalone communicator checkout")
    spec = importlib.util.spec_from_file_location("backend_verification_schemas", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("catalog", [get_manager_tools, get_planner_tools])
def test_plan_survives_create_and_update_catalog_and_wire(catalog):
    tools = _tools(catalog())
    for name in ("create_task", "update_task"):
        tool = tools[name]
        fields = tool["inputSchema"]["properties"]
        schema = fields["verification_plan"] if name == "create_task" else fields["brief"]["properties"]["verification_plan"]
        Draft202012Validator(schema).validate(PLAN)
        params = {"verification_plan": PLAN} if name == "create_task" else {"brief": {"verification_plan": PLAN}}
        sent = transform_params(tool["action"], tool.get("transform"), params.copy())
        assert sent == params
    _backend_schema_module().VerificationPlan.model_validate(PLAN)


@pytest.mark.parametrize("task_class,phase", [("assignment", "execute"), ("assignment", "review"), ("ask", "execute")])
def test_task_owner_can_record_evidence_and_fingerprint_survives_done(task_class, phase):
    tools = _tools(get_worker_subcatalog(phase, "auditor", task_class=task_class))
    receipt = {"task_id": str(uuid.uuid4()), "check_id": "invoice_totals", "receipt_key": str(uuid.uuid4()),
        "input_fingerprint": "a" * 64, "source_identity": "September snapshot version 3",
        "artifact_refs": ["reports/reconciliation.csv"], "outcome": "pass", "summary": "Totals reconcile."}
    tool = tools["record_verification_evidence"]
    Draft202012Validator(tool["inputSchema"]).validate(receipt)
    assert transform_params(tool["action"], tool.get("transform"), receipt.copy()) == receipt
    _backend_schema_module().VerificationEvidenceCreate.model_validate({key: value for key, value in receipt.items() if key != "task_id"})
    if phase == "review" or task_class == "ask":
        move = tools["move_task"]
        verdict = {"overall": "pass", "verification_input_fingerprint": receipt["input_fingerprint"]}
        result = transform_params(move["action"], move["transform"], {"task_id": receipt["task_id"], "new_status": "done", "verdict": verdict})
        assert result["verdict"] == verdict
    assert "record_verification_evidence" not in _tools(get_manager_tools())


@pytest.mark.parametrize("status,task_class", [("ready", "assignment"), ("review", "assignment"), ("ready", "ask")])
def test_actual_task_render_includes_full_optional_plan_and_preserves_prose(status, task_class):
    brief = {"goal": "Reconcile invoices", "inputs": "September snapshot", "acceptance_criteria": ["Totals reconcile"],
        "verification_steps": "Reconcile every supplied invoice; preserve independent review if required.", "verification_plan": PLAN}
    task = {"task_id": str(uuid.uuid4()), "status": status, "task_class": task_class, "assigned_agent": "analyst", "reviewer": "auditor", "brief": brief}
    prompt = build_worker_prompt(task)
    assert json.dumps(PLAN, ensure_ascii=False, sort_keys=True) in prompt
    assert brief["verification_steps"] in prompt
    assert "Missing/failed/partial required evidence cannot pass" in prompt
    assert "verdict.verification_input_fingerprint" in prompt
    assert "one shared acceptance input manifest/fingerprint across all required checks" in prompt
    assert "automation PASS needs a matching succeeded" in prompt
    if task_class == "ask":
        assert "there is no review round" in prompt
    brief.pop("verification_plan")
    assert "## Structured verification plan" not in build_worker_prompt(task)


def test_receipt_rejects_fabricated_actor_and_unknown_fields():
    schema = _tools(get_worker_subcatalog("execute", "analyst"))["record_verification_evidence"]["inputSchema"]
    assert schema["additionalProperties"] is False
    assert not {"actor", "attempt_id", "execution_cycle", "plan_hash"} & schema["properties"].keys()


def test_async_automation_plan_uses_cycle_freshness_and_grouped_criterion_coverage():
    schema = _tools(get_manager_tools())["create_task"]["inputSchema"]["properties"]["verification_plan"]
    assert "Ask tasks only use executor checks or execute-phase automation" in schema["description"]
    plan = {"version": 1, "checks": [{**PLAN["checks"][0], "owner": "automation", "criterion_indices": [1, 2], "automation_phase": "review"}]}
    Draft202012Validator(schema).validate(plan)
    _backend_schema_module().VerificationPlan.model_validate(plan)
    plan["checks"][0]["freshness"] = "current_attempt"
    assert list(Draft202012Validator(schema).iter_errors(plan))
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        _backend_schema_module().VerificationPlan.model_validate(plan)
