"""The in-container ``execute_script`` refuses reserved Claude sign-in names.

The host manifest parser (``src.scripts.manifest``) refuses a variable named
like a Claude sign-in setting (``ANTHROPIC_*``, ``CLAUDE_CODE_*``,
``CLAUDE_CONFIG_DIR``) and an office-secret reference to one. The agent
image cannot import ``src``, so ``_mcp_script_exec`` reads ``script.yaml``
itself; without its own check a legacy or agent-written manifest declaring
``ANTHROPIC_API_KEY`` launched locally with that variable set (item 37), and
a binding to a reserved office-secret name was handed to the host, whose
refusal the proxy reports only as a generic invalid-parameters error
(item 38). Both now stop in-container with the teaching message.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.agent_image_stub import stubbed_mcp_script_exec


@pytest.fixture(scope="module")
def mcp_script_exec():
    with stubbed_mcp_script_exec() as module:
        yield module


def _prepare(module, monkeypatch, tmp_path, *, proxy_url: str, task_mode: str):
    monkeypatch.setattr(module, "TOOL_PROXY_URL", proxy_url)
    monkeypatch.setattr(module, "TASK_ID", "bound-task" if proxy_url else "")
    monkeypatch.setattr(module, "TASK_MODE", task_mode)
    monkeypatch.delenv("CUBICLE_AGENT_INSTANCE_ID", raising=False)
    monkeypatch.setattr(module, "Path", lambda path: tmp_path)
    monkeypatch.setattr(module, "_task_launch_refusal", AsyncMock(return_value=None))
    monkeypatch.setattr(module, "_check_bootstrap_status", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_local_launch_refuses_a_reserved_variable_name(
    mcp_script_exec, monkeypatch, tmp_path
) -> None:
    """A manifest declaring ANTHROPIC_API_KEY never reaches the script env."""
    module = mcp_script_exec
    _prepare(module, monkeypatch, tmp_path, proxy_url="", task_mode="execute")
    (tmp_path / "script.yaml").write_text(
        "variables:\n  - name: ANTHROPIC_API_KEY\n    default: sk-ant-api03-x\n"
    )
    spawn = AsyncMock(side_effect=OSError("must not spawn"))
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)

    result = await module._execute_script({"script_name": "summarise"})

    spawn.assert_not_awaited()
    assert result["error"] is True
    assert "'ANTHROPIC_API_KEY' is reserved" in result["message"]
    assert "subscription login" in result["message"]


@pytest.mark.parametrize(
    "variables",
    [
        "  - name: CLAUDE_KEY\n    from_office_secret: ANTHROPIC_API_KEY\n",
        "  - name: claude_config_dir\n",
        "  - name: LIB_PATH\n    from_office_secret: LD_PRELOAD\n",
    ],
)
def test_manifest_reader_refuses_reserved_names(
    mcp_script_exec, tmp_path, variables
) -> None:
    (tmp_path / "script.yaml").write_text("variables:\n" + variables)
    with pytest.raises(ValueError, match="is reserved"):
        mcp_script_exec._parse_manifest(tmp_path)


def test_manifest_reader_keeps_other_service_names(mcp_script_exec, tmp_path) -> None:
    (tmp_path / "script.yaml").write_text(
        "variables:\n  - name: CLAUDE_KEY\n    from_office_secret: CLAUDE_API_TOKEN\n"
    )
    manifest = mcp_script_exec._parse_manifest(tmp_path)
    assert manifest["variables"][0]["from_office_secret"] == "CLAUDE_API_TOKEN"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_mode", "blocking_call"),
    [
        ("execute", 'ONE `update_status(new_status="blocked")` call'),
        ("triage", 'escalate_blocker(blocker_class="missing_credential")'),
    ],
)
async def test_binding_to_a_reserved_secret_is_refused_before_delegation(
    mcp_script_exec, monkeypatch, tmp_path, task_mode, blocking_call
) -> None:
    """A stored binding to ANTHROPIC_API_KEY could never resolve: refuse it
    here with the fix instead of asking the host (and then the user) for a
    secret no office can hold."""
    import json

    module = mcp_script_exec
    _prepare(
        module,
        monkeypatch,
        tmp_path,
        proxy_url="http://host.invalid",
        task_mode=task_mode,
    )
    (tmp_path / "script.yaml").write_text("variables:\n  - name: CLAUDE_KEY\n")
    (tmp_path / "variables.json").write_text(
        json.dumps(
            {"CLAUDE_KEY": {"kind": "office_secret", "ref": "ANTHROPIC_API_KEY"}}
        )
    )
    session = MagicMock()
    monkeypatch.setattr(module, "_get_session", AsyncMock(return_value=session))

    result = await module._execute_script({"script_name": "summarise"})

    session.post.assert_not_called()
    assert result["error"] is True
    message = result["message"]
    assert "office secret name(s) ANTHROPIC_API_KEY" in message
    assert "reserved" in message and "bind_script_variable" in message
    assert blocking_call in message


def _delegating_session(module, monkeypatch):
    import json
    import sys

    response = MagicMock(status=200)
    response.text = AsyncMock(return_value=json.dumps({"execution_id": "exec-1"}))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post.return_value = context
    monkeypatch.setattr(module, "_get_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        sys.modules["_mcp_backend"], "_caller_envelope", dict, raising=False
    )
    return session


@pytest.mark.asyncio
async def test_secure_input_override_skips_the_stale_reserved_binding(
    mcp_script_exec, monkeypatch, tmp_path
) -> None:
    """The host runner leaves a variable supplied by a from_human_action
    override out of its office-secret preflight, so its stale reserved
    binding is never resolved. The in-container check must agree and hand
    the run to the host instead of refusing it."""
    import json

    module = mcp_script_exec
    _prepare(
        module,
        monkeypatch,
        tmp_path,
        proxy_url="http://host.invalid",
        task_mode="execute",
    )
    (tmp_path / "script.yaml").write_text(
        "variables:\n  - name: CLAUDE_KEY\n    is_secret: true\n"
    )
    (tmp_path / "variables.json").write_text(
        json.dumps(
            {"CLAUDE_KEY": {"kind": "office_secret", "ref": "ANTHROPIC_API_KEY"}}
        )
    )
    session = _delegating_session(module, monkeypatch)
    overrides = {"CLAUDE_KEY": {"from_human_action": "request-1"}}

    result = await module._execute_script(
        {"script_name": "summarise", "variable_overrides": overrides}
    )

    assert result == {"execution_id": "exec-1", "delegated_to": "host_runner"}
    session.post.assert_called_once()
    assert session.post.call_args.kwargs["json"]["variable_overrides"] == overrides


@pytest.mark.asyncio
async def test_secure_input_override_does_not_hide_another_reserved_binding(
    mcp_script_exec, monkeypatch, tmp_path
) -> None:
    import json

    module = mcp_script_exec
    _prepare(
        module,
        monkeypatch,
        tmp_path,
        proxy_url="http://host.invalid",
        task_mode="execute",
    )
    (tmp_path / "script.yaml").write_text(
        "variables:\n"
        "  - name: CLAUDE_KEY\n    is_secret: true\n"
        "  - name: OTHER_KEY\n    is_secret: true\n"
    )
    (tmp_path / "variables.json").write_text(
        json.dumps(
            {
                "CLAUDE_KEY": {"kind": "office_secret", "ref": "ANTHROPIC_API_KEY"},
                "OTHER_KEY": {
                    "kind": "office_secret",
                    "ref": "CLAUDE_CODE_OAUTH_TOKEN",
                },
            }
        )
    )
    session = _delegating_session(module, monkeypatch)

    result = await module._execute_script(
        {
            "script_name": "summarise",
            "variable_overrides": {"CLAUDE_KEY": {"from_human_action": "request-1"}},
        }
    )

    session.post.assert_not_called()
    assert result["error"] is True
    assert "office secret name(s) CLAUDE_CODE_OAUTH_TOKEN," in result["message"]
    assert "ANTHROPIC_API_KEY" not in result["message"]


@pytest.mark.asyncio
async def test_binding_to_a_loader_named_secret_is_refused_before_delegation(
    mcp_script_exec, monkeypatch, tmp_path
) -> None:
    """The backend refuses LD_PRELOAD as an office-secret name, so a binding
    stored before that rule could never resolve either."""
    import json

    module = mcp_script_exec
    _prepare(
        module, monkeypatch, tmp_path, proxy_url="http://host.invalid", task_mode="execute"
    )
    (tmp_path / "script.yaml").write_text("variables:\n  - name: LIB_PATH\n")
    (tmp_path / "variables.json").write_text(
        json.dumps({"LIB_PATH": {"kind": "office_secret", "ref": "LD_PRELOAD"}})
    )
    session = MagicMock()
    monkeypatch.setattr(module, "_get_session", AsyncMock(return_value=session))

    result = await module._execute_script({"script_name": "summarise"})

    session.post.assert_not_called()
    assert result["error"] is True
    assert "office secret name(s) LD_PRELOAD" in result["message"]
    assert "library to load" in result["message"]


@pytest.mark.asyncio
async def test_local_launch_drops_a_loader_named_variable(
    mcp_script_exec, monkeypatch, tmp_path
) -> None:
    """The host runner never passes LD_LIBRARY_PATH to a run; a local
    in-container launch drops it too, so both paths give the same env."""
    module = mcp_script_exec
    _prepare(module, monkeypatch, tmp_path, proxy_url="", task_mode="execute")
    (tmp_path / "script.yaml").write_text(
        "variables:\n"
        "  - name: LD_LIBRARY_PATH\n    default: /opt/lib\n"
        "  - name: REGION\n    default: eu\n"
    )
    spawn = AsyncMock(side_effect=OSError("stop at spawn"))
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)

    await module._execute_script({"script_name": "summarise"})

    spawn.assert_awaited()
    env = spawn.await_args.kwargs["env"]
    assert env.get("REGION") == "eu"
    assert "LD_LIBRARY_PATH" not in env
