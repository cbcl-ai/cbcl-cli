"""Real private-file permission and symlink refusal, entirely in test scratch."""

import os
import stat
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.docker.session_files import ENSURE_DIRECTORY_PROGRAM, SESSION_FILE_DIRECTORY, WRITE_FILE_PROGRAM, session_file_path


def run_program(program, path, content=b""):
    return subprocess.run([sys.executable, "-I", "-S", "-c", program, str(path)], input=content, capture_output=True, timeout=5)


def test_sessions_never_stage_proxy_credentials_in_shared_workspace():
    assert not SESSION_FILE_DIRECTORY.startswith("/workspace")
    assert session_file_path("mcp") != session_file_path("mcp")
    with pytest.raises(ValueError):
        session_file_path("../workspace")


def test_private_directory_and_exclusive_file(tmp_path):
    directory = tmp_path / "session"
    assert run_program(ENSURE_DIRECTORY_PROGRAM, directory).returncode == 0
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    path = directory / "mcp.json"
    assert run_program(WRITE_FILE_PROGRAM, path, b"synthetic-input").returncode == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert run_program(WRITE_FILE_PROGRAM, path, b"replacement").returncode != 0
    assert path.read_bytes() == b"synthetic-input"


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink ownership boundary")
def test_symlinks_cannot_redirect_staging(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    directory = tmp_path / "linked-dir"
    directory.symlink_to(target, target_is_directory=True)
    assert run_program(ENSURE_DIRECTORY_PROGRAM, directory).returncode != 0
    victim = target / "victim"
    victim.write_text("unchanged")
    linked_file = target / "linked-file"
    linked_file.symlink_to(victim)
    assert run_program(WRITE_FILE_PROGRAM, linked_file, b"overwrite").returncode != 0
    assert victim.read_text() == "unchanged"


@pytest.mark.parametrize("failure", ["write failed", RuntimeError("staging unavailable")])
async def test_partial_staging_cleans_both_files_on_failure(monkeypatch, failure):
    from src.docker import session_bridge

    monkeypatch.setattr(session_bridge, "_ensure_container_cubicle_dir", AsyncMock(return_value=None))
    monkeypatch.setattr(session_bridge, "_write_container_file", AsyncMock(side_effect=[None, failure]))
    cleanup = AsyncMock()
    monkeypatch.setattr(session_bridge, "_remove_session_files", cleanup)
    stream = session_bridge.stream_cli_session("owned-container", "model", "synthetic-prompt", "input", mcp_config={"synthetic": True})
    if isinstance(failure, Exception):
        with pytest.raises(RuntimeError, match="staging unavailable"):
            _messages = [message async for message in stream]
    else:
        messages = [message async for message in stream]
        assert messages[0].type == "error"
    paths = cleanup.await_args.args[1]
    assert len(paths) == 2
    assert all(path.startswith(SESSION_FILE_DIRECTORY + "/") for path in paths)


async def test_cancelled_writer_reaps_host_helper(monkeypatch):
    import asyncio
    from src.docker import session_bridge

    process = MagicMock()
    process.communicate = AsyncMock(side_effect=asyncio.CancelledError)
    process.wait = AsyncMock(return_value=0)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(asyncio.CancelledError):
        await session_bridge._write_container_file("owned-container", session_file_path("mcp"), "synthetic", description="test")
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()


@pytest.mark.parametrize("target", ["/tmp", SESSION_FILE_DIRECTORY, SESSION_FILE_DIRECTORY + "/token"])
def test_extra_mounts_cannot_replace_private_runtime(target):
    from src.docker.container_manager import _is_reserved_container_path

    assert _is_reserved_container_path(target)
