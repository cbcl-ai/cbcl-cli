"""Tests for Script Runner Redis event publishing (P2-T08)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.scripts.script_notifier import (
    _publish,
    format_duration,
    notify_completion,
    report_progress,
)


class TestPublishHelper:
    """Tests for the dual-publish helper."""

    @pytest.mark.asyncio
    async def test_prefers_redis_over_ws(self):
        """When both router and ws are available, uses router."""
        router = AsyncMock()
        ws = AsyncMock()

        await _publish(router, ws, "script_status", {"key": "value"})

        router.publish_event.assert_called_once_with(
            {"type": "script_status", "key": "value"}
        )
        ws.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_ws_on_redis_error(self):
        """When router raises, falls back to ws.send()."""
        router = AsyncMock()
        router.publish_event = AsyncMock(side_effect=RuntimeError("Redis down"))
        ws = AsyncMock()

        await _publish(router, ws, "script_status", {"key": "value"})

        router.publish_event.assert_called_once()
        ws.send.assert_called_once()
        sent = ws.send.call_args[0][0]
        assert sent["type"] == "script_status"
        assert sent["key"] == "value"

    @pytest.mark.asyncio
    async def test_works_with_router_only(self):
        """When ws is None, only router is used."""
        router = AsyncMock()

        await _publish(router, None, "test_event", {"data": 1})

        router.publish_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_works_with_ws_only(self):
        """When router is None, only ws is used."""
        ws = AsyncMock()

        await _publish(None, ws, "test_event", {"data": 1})

        ws.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_error_when_both_none(self):
        """When both are None, no exception is raised."""
        await _publish(None, None, "test_event", {"data": 1})


class TestNotifyCompletion:
    """Tests for notify_completion with router."""

    @pytest.mark.asyncio
    async def test_publishes_three_events_on_completion(self):
        """Completion sends script_status, task_activity, and manager_action."""
        router = AsyncMock()

        await notify_completion(
            ws=None, router=router,
            script_name="test-script", exec_id="exec-001",
            task_id="task-1", triggered_by="analyst",
            started_at_iso="2026-03-25T10:00:00Z",
            process_returncode=0, status="completed",
            duration=3600.0, error_message=None,
            progress={"done": 100, "total": 100},
        )

        assert router.publish_event.call_count == 3
        event_types = [
            call[0][0]["type"] for call in router.publish_event.call_args_list
        ]
        assert "script_status" in event_types
        assert "task_activity" in event_types
        assert "manager_action" in event_types

    @pytest.mark.asyncio
    async def test_skips_task_activity_when_no_task(self):
        """Without task_id, only script_status and manager_action are sent."""
        router = AsyncMock()

        await notify_completion(
            ws=None, router=router,
            script_name="test-script", exec_id="exec-001",
            task_id=None, triggered_by="user",
            started_at_iso="2026-03-25T10:00:00Z",
            process_returncode=0, status="completed",
            duration=60.0, error_message=None, progress={},
        )

        assert router.publish_event.call_count == 2


class TestReportProgress:
    """Tests for report_progress with router."""

    @pytest.mark.asyncio
    async def test_publishes_progress_via_router(self):
        """Progress update is published via router."""
        router = AsyncMock()

        await report_progress(
            ws=None, script_name="test-script",
            exec_id="exec-001", task_id="task-1",
            progress={"done": 50, "total": 100, "current_item": "item 50"},
            router=router,
        )

        assert router.publish_event.call_count == 2  # script_status + task_activity


class TestFormatDuration:
    """Tests for format_duration (unchanged, regression check)."""

    def test_seconds(self):
        assert format_duration(45) == "45s"

    def test_minutes(self):
        assert format_duration(300) == "5m"

    def test_hours_and_minutes(self):
        assert format_duration(5400) == "1h 30m"

    def test_exact_hours(self):
        assert format_duration(7200) == "2h"
