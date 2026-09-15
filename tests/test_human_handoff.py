import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from src._agent_image import _mcp_backend, mcp_tool_server
from src._agent_image._mcp import transforms
from src._agent_image._mcp.tools_worker import get_worker_tools
from src.office_secrets import store, transient


@pytest.fixture
def secret_store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        store,
        "get_office_secrets_path",
        lambda slug: tmp_path / "secrets" / f"{slug}.json",
    )
    return tmp_path


def test_transient_input_is_office_scoped_expiring_and_cannot_be_replaced(
    secret_store, monkeypatch
):
    name = f"CBCL_INPUT_{uuid.uuid4().hex.upper()}_1"
    deadline = (datetime.now(UTC) + timedelta(seconds=90)).isoformat()
    fingerprint = transient.set_transient_secret(
        "first", name, "opaque-value", deadline
    )
    assert len(fingerprint) == 16
    assert name not in store.read_office_secrets("first")
    assert name not in store.read_office_secrets("second")
    assert (
        transient.set_transient_secret("first", name, "opaque-value", deadline)
        == fingerprint
    )
    with pytest.raises(store.OfficeSecretStoreError):
        transient.set_transient_secret("first", name, "replacement", deadline)
    with pytest.raises(store.OfficeSecretStoreError):
        store.set_office_secret("first", name, "permanent-bypass")
    record_path = next(
        (secret_store / "secrets" / "transient" / "first").glob("*.json")
    )
    assert record_path.stat().st_mode & 0o777 == 0o600
    old_record = json.loads(record_path.read_text())
    old_record["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    record_path.write_text(json.dumps(old_record))
    transient.purge_expired_inputs("first")
    assert name not in store.read_office_secrets("first")
    assert not record_path.exists()


def test_handoff_transform_uses_bound_task_not_model_supplied_id(monkeypatch):
    monkeypatch.setenv("TASK_ID", "bound-task")
    result = transforms.transform_params(
        "request_user_action",
        "request_user_action",
        {
            "task_id": "different-task",
            "question": "Ready?",
            "response_mode": "ready",
        },
    )
    assert result["task_id"] == "bound-task"


@pytest.mark.asyncio
async def test_human_handoff_locks_on_success_and_unlocks_on_failure(monkeypatch):
    monkeypatch.setattr(mcp_tool_server, "TASK_MODE", "execute")
    tool = next(
        tool for tool in get_worker_tools() if tool["name"] == "request_user_action"
    )
    server = mcp_tool_server.MCPServer([tool])
    backend = AsyncMock(return_value={"error": "not accepted"})
    monkeypatch.setattr(mcp_tool_server, "_call_backend", backend)
    result = await server._execute_tool(
        "request_user_action", {"question": "Ready?", "response_mode": "ready"}
    )
    assert result["isError"] is True
    assert server._session_locked is False
    backend.return_value = {"request_id": "stable-request", "status": "pending"}
    result = await server._execute_tool(
        "request_user_action", {"question": "Ready?", "response_mode": "ready"}
    )
    assert server._session_locked is True
    assert "stable-request" in result["content"][0]["text"]


def test_execution_envelope_uses_process_bound_identity(monkeypatch):
    monkeypatch.setenv("CUBICLE_EXECUTION_ATTEMPT_ID", "attempt")
    monkeypatch.setenv("CUBICLE_EXECUTION_CYCLE", "2")
    monkeypatch.setenv("CUBICLE_EXECUTION_GENERATION", "7")
    monkeypatch.setenv("TASK_ID", "bound-task")
    envelope = _mcp_backend._caller_envelope()
    assert envelope["attempt_id"] == "attempt"
    assert envelope["execution_cycle"] == 2
    assert envelope["execution_generation"] == 7
    assert envelope["task_id"] == "bound-task"


def test_secure_input_reference_is_bound_to_task_script_variable_and_expiry(
    secret_store,
):
    request_id = uuid.uuid4()
    task_id = str(uuid.uuid4())
    name = f"CBCL_INPUT_{request_id.hex.upper()}_1"
    binding = {
        "task_id": task_id,
        "script_name": "connect",
        "variable_name": "CALLBACK",
    }
    transient.set_transient_secret(
        "office",
        name,
        "not-for-models",
        (datetime.now(UTC) + timedelta(seconds=300)).isoformat(),
        binding=binding,
    )
    assert (
        transient.resolve_human_action_input(
            "office", request_id, task_id, "connect", "CALLBACK"
        )
        == "not-for-models"
    )
    for wrong_task, wrong_script, wrong_variable in (
        (str(uuid.uuid4()), "connect", "CALLBACK"),
        (task_id, "exfiltrate", "CALLBACK"),
        (task_id, "connect", "PUBLIC_OUTPUT"),
    ):
        with pytest.raises(store.OfficeSecretStoreError):
            transient.resolve_human_action_input(
                "office", request_id, wrong_task, wrong_script, wrong_variable
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("unrelated_missing", [False, True])
async def test_bound_human_override_masks_only_its_own_old_binding(
    secret_store,
    tmp_path,
    monkeypatch,
    unrelated_missing,
):
    from tests.test_script_runner import TestOfficeSecretsResolution as ProjectFixture
    from src.scripts.script_runner import MissingOfficeSecretError

    fixture = ProjectFixture()
    manifest = "variables:\n  - name: CALLBACK\n    type: string\n    is_secret: true\n    from_office_secret: MISSING_OLD_CALLBACK\n"
    if unrelated_missing:
        manifest += "  - name: API_KEY\n    type: string\n    from_office_secret: STILL_MISSING\n"
    fixture._make_project(tmp_path, "connect", manifest_yaml=manifest)
    task_id = str(uuid.uuid4())
    request_id = uuid.uuid4()
    transient.set_transient_secret(
        "office",
        f"CBCL_INPUT_{request_id.hex.upper()}_1",
        "scoped-secret",
        (datetime.now(UTC) + timedelta(seconds=300)).isoformat(),
        binding={
            "task_id": task_id,
            "script_name": "connect",
            "variable_name": "CALLBACK",
        },
    )
    runner = fixture._runner(tmp_path, office_name="office")
    captured = {}

    class Process:
        returncode = 0

        async def wait(self):
            return 0

    async def spawn(*args, **kwargs):
        captured.update(kwargs["env"])
        return Process()

    monkeypatch.setattr(
        "src.scripts.script_runner.asyncio.create_subprocess_exec", spawn
    )
    overrides = {"CALLBACK": {"from_human_action": str(request_id)}}
    if unrelated_missing:
        with pytest.raises(MissingOfficeSecretError) as failure:
            await runner.execute(
                "connect", variable_overrides=overrides, task_id=task_id
            )
        assert failure.value.missing == ["STILL_MISSING"]
        assert not captured
    else:
        execution_id = await runner.execute(
            "connect", variable_overrides=overrides, task_id=task_id
        )
        assert captured["CALLBACK"] == "scoped-secret"
        await runner.get_status(execution_id)
        assert not runner._active
