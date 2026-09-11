from unittest.mock import AsyncMock

import pytest

from src._agent_image.generation_runner import (
    SUPPORTED_CLI_VERSION,
    SUPPORTED_SDK_VERSION,
)
from src.docker import session_bridge
from src.docker.container_manager import _DOCKER_DIR


def test_security_helpers_are_installed_in_root_owned_runtime():
    dockerfile = (_DOCKER_DIR / "Dockerfile.agent").read_text()
    assert f'"claude-agent-sdk=={SUPPORTED_SDK_VERSION}"' in dockerfile
    assert "poppler-utils" in dockerfile
    assert "RUN mkdir -p /usr/local/libexec/cubicle" in dockerfile
    for helper in ("secure_files", "generation_runner", "generation_sources"):
        assert f"COPY {helper}.py /usr/local/libexec/cubicle/{helper}.py" in dockerfile
    assert dockerfile.index("COPY secure_files.py") < dockerfile.index("\nUSER agent")
    assert "chown agent:agent /usr/local" not in dockerfile


@pytest.mark.asyncio
@pytest.mark.parametrize("compatible", [True, False])
async def test_cli_upgrade_remains_pinned_and_checks_installed_version(
    monkeypatch, compatible
):
    commands = []

    async def spawn(*arguments, **kwargs):
        commands.append(arguments)
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (
            b"/usr/local/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude\n",
            b"",
        )
        return process

    monkeypatch.setattr(session_bridge.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(
        session_bridge,
        "probe_cli_versions",
        AsyncMock(
            return_value={
                "sdk_version": SUPPORTED_SDK_VERSION,
                "cli_version": (
                    f"{SUPPORTED_CLI_VERSION} (Claude Code)"
                    if compatible
                    else "9.9.9 (Claude Code)"
                ),
            }
        ),
    )
    result = await session_bridge.upgrade_cli("synthetic-container")
    assert result["ok"] is compatible
    assert commands[0][-1] == f"claude-agent-sdk=={SUPPORTED_SDK_VERSION}"
    assert "--isolated" in commands[0]
    assert "-U" not in commands[0]
    assert commands[0][2:4] == ("--workdir", "/")
    assert "-I" in commands[0]
    assert "-I" in commands[1]
