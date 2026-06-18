"""T2.2.1 — argv secret-hygiene lock (03/#1, 03/#25).

The MCP config embeds TOOL_PROXY_TOKEN and OFFICE_TOOL_SECRET in its env
map. Passing the config inline as one argv element made both secrets
world-readable on the host (``ps`` / ``/proc/<pid>/cmdline``) for the whole
CLI session and logged them whole at DEBUG. This test pins that NO secret
value reaches ANY ``docker exec`` argv (mkdir, the tee that writes the
container file, or the claude invocation) and that ``--mcp-config`` carries
a PATH, never inline JSON. Without it, a future re-inline regresses silently.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from src.docker import session_bridge


_TOOL_PROXY_TOKEN = "tpx_SECRET_proxy_token_value_zzz"
_OFFICE_TOOL_SECRET = "ots_SECRET_office_tool_value_yyy"

_MCP_CONFIG = {
    "mcpServers": {
        "cubicle-tools": {
            "type": "stdio",
            "command": "python3",
            "args": ["/opt/cubicle/mcp_tool_server.py", "--role", "worker"],
            "env": {
                "TOOL_PROXY_TOKEN": _TOOL_PROXY_TOKEN,
                "OFFICE_TOOL_SECRET": _OFFICE_TOOL_SECRET,
            },
        }
    }
}


class _FakeStdin:
    def __init__(self) -> None:
        self.written = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def write_eof(self) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True


class _FakeStream:
    async def readline(self) -> bytes:
        return b""

    async def read(self, _n: int) -> bytes:
        return b""


class _FakeProc:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()
        self.returncode = 0

    async def wait(self) -> int:
        return 0

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:  # noqa: A002
        # mkdir + tee helpers use communicate(); the tee passes the file
        # content via ``input=`` (STDIN) — confirming the secret JSON rides
        # stdin, never the argv. Succeed silently.
        return b"", b""

    def kill(self) -> None:
        return None


async def _run_capture_all(**kwargs) -> list[list[str]]:
    """Run a session; return EVERY captured ``create_subprocess_exec`` argv
    (mkdir + tee + claude), so secret-freedom can be asserted across all of
    them, not just the final claude command."""
    captured: list[list[str]] = []

    async def _fake_exec(*args: str, **_kw: Any) -> _FakeProc:
        captured.append(list(args))
        return _FakeProc()

    with patch.object(asyncio, "create_subprocess_exec", _fake_exec), \
            patch("subprocess.run"):
        async for _ in session_bridge.stream_cli_session(
            container_name="cbcl-office-test",
            model="claude-opus-4-7",
            system_prompt="you are a worker",
            prompt="do the task",
            mcp_config=_MCP_CONFIG,
            **kwargs,
        ):
            pass

    assert captured, "create_subprocess_exec was never reached"
    return captured


@pytest.mark.asyncio
async def test_no_secret_value_reaches_any_argv():
    all_argvs = await _run_capture_all()
    flat = [part for argv in all_argvs for part in argv]
    for secret in (_TOOL_PROXY_TOKEN, _OFFICE_TOOL_SECRET):
        assert not any(secret in part for part in flat), (
            f"secret {secret!r} leaked onto a docker exec argv"
        )


@pytest.mark.asyncio
async def test_mcp_config_passed_as_path_not_inline_json():
    all_argvs = await _run_capture_all()
    # Find the claude invocation (the argv carrying --mcp-config).
    claude = next(a for a in all_argvs if "--mcp-config" in a)
    idx = claude.index("--mcp-config")
    value = claude[idx + 1]
    # Path form, NOT inline JSON.
    assert not value.lstrip().startswith("{"), "mcp-config rode inline JSON"
    assert value.startswith("/workspace/.cubicle/.mcp-")
    assert value.endswith(".json")


@pytest.mark.asyncio
async def test_no_inline_json_blob_anywhere_in_argv():
    all_argvs = await _run_capture_all()
    for argv in all_argvs:
        for part in argv:
            # No argv element should be a JSON object literal (the secret
            # JSON rides the tee's STDIN, the config rides a file path).
            assert not part.lstrip().startswith('{"mcpServers"'), (
                f"inline mcp JSON found on argv: {part[:60]}"
            )


# ─── item-6 orchestration flag wiring (effort / ultracode --settings) ───────
#
# Locks the policy->argv translation in session_bridge (the layer that turns
# the (effort, settings_json) tuple from build_session_policy into the
# docker-exec command line), the same way the --mcp-config path is locked
# above. A future refactor that renames the flags, or stops emitting BOTH
# --effort xhigh AND --settings for ultracode (the documented recipe), fails
# here.


def _claude_argv(all_argvs: list[list[str]]) -> list[str]:
    """The claude invocation is the argv carrying --mcp-config."""
    return next(a for a in all_argvs if "--mcp-config" in a)


@pytest.mark.asyncio
async def test_ultracode_emits_settings_and_effort_xhigh():
    # ultracode policy => effort="xhigh" + settings '{"ultracode": true}'.
    argv = _claude_argv(
        await _run_capture_all(effort="xhigh", settings_json='{"ultracode": true}')
    )
    assert "--settings" in argv
    assert argv[argv.index("--settings") + 1] == '{"ultracode": true}'
    assert "--effort" in argv
    assert argv[argv.index("--effort") + 1] == "xhigh"


@pytest.mark.asyncio
async def test_plain_effort_emits_effort_not_settings():
    argv = _claude_argv(await _run_capture_all(effort="high"))
    assert "--effort" in argv
    assert argv[argv.index("--effort") + 1] == "high"
    assert "--settings" not in argv


@pytest.mark.asyncio
async def test_no_orchestration_emits_neither_flag():
    argv = _claude_argv(await _run_capture_all())
    assert "--effort" not in argv
    assert "--settings" not in argv


@pytest.mark.asyncio
async def test_env_override_reaches_docker_exec():
    # The Manager's CLAUDE_CODE_DISABLE_WORKFLOWS=1 (and any env override) must
    # land as a `docker exec -e KEY=VALUE` flag on the claude argv.
    argv = _claude_argv(
        await _run_capture_all(
            env_overrides={"CLAUDE_CODE_DISABLE_WORKFLOWS": "1"}
        )
    )
    assert "-e" in argv
    assert "CLAUDE_CODE_DISABLE_WORKFLOWS=1" in argv
