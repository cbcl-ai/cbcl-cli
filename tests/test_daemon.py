"""Tests for daemon mode PID helpers and log rotation setup."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.daemon import _format_uptime, _is_process_running, _read_pid


class TestReadPid:
    """Tests for PID file reading."""

    def test_reads_valid_pid(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345")
        assert _read_pid(pid_file) == 12345

    def test_returns_none_for_invalid_content(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("not-a-number")
        assert _read_pid(pid_file) is None

    def test_returns_none_for_missing_file(self, tmp_path):
        pid_file = tmp_path / "nonexistent.pid"
        assert _read_pid(pid_file) is None

    def test_handles_whitespace(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("  67890  \n")
        assert _read_pid(pid_file) == 67890


class TestIsProcessRunning:
    """Tests for process running check."""

    def test_current_process_is_running(self):
        assert _is_process_running(os.getpid()) is True

    def test_nonexistent_pid_not_running(self):
        # PID 99999999 is almost certainly not running
        assert _is_process_running(99999999) is False


class TestFormatUptime:
    """Tests for uptime formatting."""

    def test_returns_string(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345")
        result = _format_uptime(pid_file)
        assert isinstance(result, str)
        # Freshly created file should show 0m
        assert "m" in result

    def test_returns_unknown_for_missing_file(self, tmp_path):
        pid_file = tmp_path / "nonexistent.pid"
        assert _format_uptime(pid_file) == "unknown"
