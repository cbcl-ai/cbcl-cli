from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call
from uuid import UUID

import pytest

from src.handlers import _register_process_model_handlers
from src.office_runtime import RuntimeStorageError


@pytest.fixture
def wizard_handlers(tmp_path, monkeypatch):
    office = SimpleNamespace(
        id=UUID("8cbca3fd-6345-4afd-beb2-5c5798591d2a"),
        workspace_path=tmp_path / "office-workspace",
    )
    router = MagicMock()
    router.publish_event = AsyncMock()
    helpers = {
        command: AsyncMock()
        for command in (
            "generate_office_config",
            "improve_office_config",
            "analyze_office_description",
        )
    }
    for command, helper in helpers.items():
        monkeypatch.setattr(f"src.handlers.run_{command}", helper)
    resolver = AsyncMock(return_value="a" * 64)
    monkeypatch.setattr("src.office_runtime.resolve_office_container_id", resolver)
    _register_process_model_handlers(
        router,
        config_store=MagicMock(),
        script_syncer=MagicMock(),
        claude_md_writer=MagicMock(),
        mgr=MagicMock(),
        supervisor=MagicMock(),
        dispatcher=MagicMock(),
        script_runner=MagicMock(),
        secrets_store=MagicMock(),
        queue_manager=MagicMock(),
        office=office,
        container_name="cbcl-office-test",
    )
    callbacks = {
        registration.args[0]: registration.args[1]
        for registration in router.on.call_args_list
    }
    return SimpleNamespace(
        office=office,
        router=router,
        helpers=helpers,
        resolver=resolver,
        callbacks=callbacks,
    )


@pytest.mark.parametrize(
    "command",
    (
        "generate_office_config",
        "improve_office_config",
        "analyze_office_description",
    ),
)
async def test_registered_wizard_resolves_current_container_per_command(
    wizard_handlers, command
):
    harness = wizard_handlers
    container_ids = ("a" * 64, "b" * 64)
    harness.resolver.side_effect = container_ids
    messages = [
        {"type": command, "request_id": f"request-{index}"}
        for index in range(len(container_ids))
    ]

    for message in messages:
        await harness.callbacks[command](message)

    assert harness.resolver.await_args_list == [
        call(str(harness.office.id), "cbcl-office-test"),
        call(str(harness.office.id), "cbcl-office-test"),
    ]
    expected_calls = []
    for message, container_id in zip(messages, container_ids, strict=True):
        arguments = {"router": harness.router, "container_name": container_id}
        if command == "generate_office_config":
            arguments["workspace_path"] = harness.office.workspace_path
        expected_calls.append(call(message, **arguments))
    assert harness.helpers[command].await_args_list == expected_calls
    for other_command, helper in harness.helpers.items():
        if other_command != command:
            helper.assert_not_awaited()
    harness.router.publish_event.assert_not_awaited()


@pytest.mark.parametrize(
    ("command", "failure_type"),
    (
        ("generate_office_config", "setup_generation_failed"),
        ("improve_office_config", "setup_generation_failed"),
        ("analyze_office_description", "analyze_description_failed"),
    ),
)
async def test_registered_wizard_reports_runtime_failure_without_generation(
    wizard_handlers, command, failure_type
):
    harness = wizard_handlers
    harness.resolver.side_effect = RuntimeStorageError("synthetic ownership mismatch")
    message = {"type": command, "request_id": "request-failure"}

    await harness.callbacks[command](message)

    harness.resolver.assert_awaited_once_with(
        str(harness.office.id), "cbcl-office-test"
    )
    for helper in harness.helpers.values():
        helper.assert_not_awaited()
    harness.router.publish_event.assert_awaited_once_with(
        {
            "type": failure_type,
            "request_id": "request-failure",
            "error": (
                "This office's private runtime is unavailable. Start or upgrade "
                "the communicator and office image."
            ),
        }
    )
