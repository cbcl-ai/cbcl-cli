"""Tests for daemon mode PID helpers and log rotation setup."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock

import pytest

from src.daemon import (
    _format_uptime,
    _is_process_running,
    _read_pid,
    _supervise_connector,
)


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


# --- Connector supervisor (reconnect-hardening) ---


class _ScriptedRouter:
    """Fake router whose ``start()`` follows a scripted behaviour per call:
    'crash' raises, 'graceful' sets should_run=False + returns, 'cancel'
    raises CancelledError, 'dirty_return' returns while still should_run."""

    def __init__(self, script: list[str]) -> None:
        self._script = script
        self.calls = 0
        self.should_run = True

    async def start(self) -> None:
        action = self._script[self.calls]
        self.calls += 1
        if action == "crash":
            raise RuntimeError("boom")
        if action == "cancel":
            raise asyncio.CancelledError()
        if action == "graceful":
            self.should_run = False
            return
        if action == "dirty_return":
            return  # returns but should_run stays True (defensive case)
        raise AssertionError(f"unknown action {action}")


class TestSuperviseConnector:
    @pytest.mark.asyncio
    async def test_restarts_on_crash_then_stops_on_graceful(self, monkeypatch):
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        router = _ScriptedRouter(["crash", "graceful"])
        await _supervise_connector(router, "Dev")
        # Crashed once → restarted → graceful stop → supervisor returns.
        assert router.calls == 2

    @pytest.mark.asyncio
    async def test_no_restart_when_stop_in_flight_during_crash(self, monkeypatch):
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        class R:
            def __init__(self):
                self.calls = 0
                self.should_run = True

            async def start(self):
                self.calls += 1
                self.should_run = False  # stop() landed concurrently
                raise RuntimeError("x")

        r = R()
        await _supervise_connector(r, "Dev")
        assert r.calls == 1  # graceful stop → NOT restarted

    @pytest.mark.asyncio
    async def test_reraises_cancellation_without_restart(self, monkeypatch):
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        router = _ScriptedRouter(["cancel"])
        with pytest.raises(asyncio.CancelledError):
            await _supervise_connector(router, "Dev")
        assert router.calls == 1  # shutdown — not restarted

    @pytest.mark.asyncio
    async def test_restarts_on_unexpected_clean_return(self, monkeypatch):
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        router = _ScriptedRouter(["dirty_return", "graceful"])
        await _supervise_connector(router, "Dev")
        # Returned while still should_run → restarted → then graceful.
        assert router.calls == 2
