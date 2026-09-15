"""A background daemon must exec fresh instead of inheriting macOS runtime state."""

import io
import json
import os
from unittest.mock import AsyncMock, Mock

import pytest

from src import daemon
from src.config import Config


def test_detached_start_uses_private_config_pipe_and_fresh_interpreter(monkeypatch, tmp_path):
    pid_file = tmp_path / "cbcl.pid"
    monkeypatch.setattr(daemon, "get_pid_path", lambda: pid_file)
    monkeypatch.setattr(daemon, "get_logs_path", lambda: tmp_path)
    monkeypatch.setattr(os, "fork", Mock(side_effect=AssertionError("unsafe fork")))
    sent = []
    process = Mock(pid=4321)
    process.poll.return_value = None
    process.stdin.write.side_effect = lambda data: sent.append(data)
    process.stdin.close.side_effect = lambda: pid_file.write_text("4321")
    spawn = Mock(return_value=process)
    monkeypatch.setattr(daemon.subprocess, "Popen", spawn)
    config = Config(platform_url="http://localhost:8000", security_token="test-private-token")
    daemon._start_daemon(config)
    argv = spawn.call_args.args[0]
    assert argv[1:] == ["-m", "src._daemon_entry"]
    assert "test-private-token" not in str(spawn.call_args)
    assert json.loads(sent[0])["security_token"] == "test-private-token"
    assert spawn.call_args.kwargs["start_new_session"] is True
    assert spawn.call_args.kwargs["close_fds"] is True
    assert spawn.call_args.kwargs["stdout"] == daemon.subprocess.DEVNULL
    process.terminate.assert_not_called()


def test_child_failure_does_not_report_success_or_remove_another_pid(monkeypatch, tmp_path):
    pid_file = tmp_path / "cbcl.pid"
    pid_file.write_text("123")
    monkeypatch.setattr(daemon, "get_pid_path", lambda: pid_file)
    monkeypatch.setattr(daemon, "_is_process_running", lambda pid: False)
    process = Mock(pid=4321)
    process.poll.return_value = 1
    monkeypatch.setattr(daemon.subprocess, "Popen", Mock(return_value=process))
    with pytest.raises(daemon.click.ClickException, match="startup failed"):
        daemon._start_daemon(Config())
    assert pid_file.read_text() == "123"


def test_daemon_child_logs_unhandled_error_and_cleans_own_pid(monkeypatch, tmp_path, caplog):
    pid_file = tmp_path / "cbcl.pid"
    monkeypatch.setattr(daemon, "get_pid_path", lambda: pid_file)
    monkeypatch.setattr(daemon, "_setup_logging_daemon", lambda: None)
    run = AsyncMock(side_effect=RuntimeError("test runtime failure"))
    monkeypatch.setattr(daemon, "_run_process_model", run)
    with pytest.raises(RuntimeError, match="test runtime failure"):
        daemon._run_daemon_process(Config())
    assert not pid_file.exists()
    assert "daemon exited unexpectedly" in caplog.text


def test_private_entry_reconstructs_exact_config_without_reloading_disk(monkeypatch):
    from src import _daemon_entry

    stream = io.StringIO('{"platform_url":"http://localhost:8000","security_token":"test-token","redis_url":"redis://localhost:6399"}')
    monkeypatch.setattr(_daemon_entry.sys, "stdin", stream)
    run = Mock()
    monkeypatch.setattr(_daemon_entry, "_run_daemon_process", run)
    _daemon_entry.main()
    config = run.call_args.args[0]
    assert config.platform_url == "http://localhost:8000"
    assert config.security_token == "test-token"
    assert config.redis_url == "redis://localhost:6399"
    assert stream.closed
