"""Section A — Review → Done routing & reviewer orchestration fixes.

Covers the unit-testable pieces of the ADD-A* findings from
``docs/archive/audits/2026-06-01-production-readiness/12-additional-findings.md``:

- ADD-A3: scoped queue removal on task_kill (don't clobber the reviewer's
  just-routed entry).
- ADD-A4: reviewer-eligibility helper (inactive/deleted reviewer → fall back
  to the Manager Assistant rather than starving forever).
- ADD-A5: MA review-completion decision (don't auto-approve a no-op session).
"""
from __future__ import annotations

import pytest
import pytest_asyncio
import fakeredis.aioredis

from src.orchestrator.agent_queue import AgentQueueManager


OFFICE_ID = "office-sectionA"


@pytest_asyncio.fixture
async def fake_redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest_asyncio.fixture
async def qm(fake_redis) -> AgentQueueManager:
    return AgentQueueManager(fake_redis, OFFICE_ID)


def _task(task_id: str, status: str, **extra) -> dict:
    return {
        "task_id": task_id,
        "readable_id": f"WR-001.{task_id}",
        "status": status,
        "priority": "urgent",
        **extra,
    }


# --- ADD-A3: task_kill must not yank the task out of the reviewer's queue ---


@pytest.mark.asyncio
async def test_scoped_removal_preserves_reviewer_queue(qm: AgentQueueManager):
    """remove_task(executor) must leave the same task in the reviewer's queue.

    Reproduces the task_kill race: on review submission the backend routes
    the task to the reviewer's queue (route_task_moved) AND kills the
    executor (task_kill). The kill must remove the task only from the
    EXECUTOR's queue, never the reviewer's.
    """
    t = _task("T01", "review", reviewer="auditor")
    # route_task_moved put it in the reviewer's queue:
    await qm.add_task("auditor", t)
    # (a stale copy may also sit in the executor's queue)
    await qm.add_task("python-dev", t)

    # task_kill scoped to the executor (the ADD-A3 fix behaviour):
    await qm.remove_task("python-dev", "T01")

    # Reviewer still has it; executor no longer does.
    assert await qm.pop_next("auditor") is not None
    assert await qm.pop_next("python-dev") is None


@pytest.mark.asyncio
async def test_remove_task_from_all_still_wipes_everything(qm: AgentQueueManager):
    """The broad sweep (used as the no-agent fallback) still clears all queues."""
    t = _task("T02", "review")
    await qm.add_task("auditor", t)
    await qm.add_task("python-dev", t)

    await qm.remove_task_from_all("T02")

    assert await qm.pop_next("auditor") is None
    assert await qm.pop_next("python-dev") is None


# --- ADD-A4: inactive/deleted reviewer must fall back to the MA ---

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from src.config_sync.sync_service import ConfigStore  # noqa: E402
from src._handlers._tasks import (  # noqa: E402
    route_task_moved,
    route_task_updated,
)


def test_is_agent_dispatchable():
    cfg = ConfigStore()
    cfg.agents = [
        {"name": "active1", "is_active": True},
        {"name": "inactive1", "is_active": False},
    ]
    assert cfg.is_agent_dispatchable("active1") is True
    assert cfg.is_agent_dispatchable("inactive1") is False
    assert cfg.is_agent_dispatchable("missing") is False
    assert cfg.is_agent_dispatchable("") is False


def _routing_mocks():
    dispatcher = MagicMock()
    dispatcher.dispatch_agent = AsyncMock()
    supervisor = MagicMock()
    supervisor.is_agent_busy.return_value = False
    supervisor.get_all_statuses.return_value = {}
    router = MagicMock()
    router.publish_event = AsyncMock()
    return dispatcher, supervisor, router


@pytest.mark.asyncio
async def test_route_task_moved_inactive_reviewer_falls_back_to_ma(qm):
    cfg = ConfigStore()
    cfg.agents = [
        {"name": "editor", "is_active": False},  # deactivated reviewer
        {"name": "manager-assistant", "is_active": True},
    ]
    dispatcher, supervisor, router = _routing_mocks()
    await route_task_moved(
        {
            "task_id": "T10", "new_status": "review", "reviewer": "editor",
            "readable_id": "WR-001.T10", "assigned_agent": "python-dev",
        },
        queue_manager=qm, dispatcher=dispatcher, supervisor=supervisor,
        router=router, config_store=cfg,
    )
    assert await qm.pop_next("editor") is None  # dead reviewer gets nothing
    ma = await qm.pop_next("manager-assistant")
    assert ma is not None and ma["task_id"] == "T10"
    dispatcher.dispatch_agent.assert_awaited_with("manager-assistant")


@pytest.mark.asyncio
async def test_route_task_moved_active_reviewer_still_routes_to_reviewer(qm):
    cfg = ConfigStore()
    cfg.agents = [
        {"name": "editor", "is_active": True},
        {"name": "manager-assistant", "is_active": True},
    ]
    dispatcher, supervisor, router = _routing_mocks()
    await route_task_moved(
        {
            "task_id": "T11", "new_status": "review", "reviewer": "editor",
            "readable_id": "WR-001.T11", "assigned_agent": "python-dev",
        },
        queue_manager=qm, dispatcher=dispatcher, supervisor=supervisor,
        router=router, config_store=cfg,
    )
    assert await qm.pop_next("editor") is not None
    assert await qm.pop_next("manager-assistant") is None
    dispatcher.dispatch_agent.assert_awaited_with("editor")


@pytest.mark.asyncio
async def test_route_task_updated_inactive_reviewer_falls_back_to_ma(qm):
    cfg = ConfigStore()
    cfg.agents = [
        {"name": "editor", "is_active": False},
        {"name": "manager-assistant", "is_active": True},
    ]
    dispatcher, supervisor, router = _routing_mocks()
    await route_task_updated(
        {"task_data": {
            "task_id": "T12", "status": "review", "reviewer": "editor",
            "readable_id": "WR-001.T12", "assigned_agent": "python-dev",
        }},
        queue_manager=qm, dispatcher=dispatcher, supervisor=supervisor,
        router=router, config_store=cfg,
    )
    assert await qm.pop_next("editor") is None
    ma = await qm.pop_next("manager-assistant")
    assert ma is not None and ma["task_id"] == "T12"


# --- ADD-A5: MA must not auto-approve a SKIPPED (no-work) review session ---

from src._handlers._tasks import decide_ma_review_completion  # noqa: E402


def test_decide_ma_review_completion():
    # Real review, task still in review → approve (benefit of the doubt).
    assert decide_ma_review_completion("review", review_skipped=False) == "approve"
    # Task already moved on → no-op regardless of skip.
    assert decide_ma_review_completion("done", review_skipped=False) == "noop"
    assert decide_ma_review_completion("done", review_skipped=True) == "noop"
    assert decide_ma_review_completion("ready", review_skipped=True) == "noop"


def test_decide_ma_review_completion_skip_is_loop_bounded():
    """C1 regression guard: a skipped MA review must NOT loop forever.

    - skipped while the MA is NOT the reviewer (unauthorized) → authorize the
      MA as reviewer + retry ONCE;
    - skipped while the MA IS already the reviewer → noop (a retry would just
      re-skip — break the loop; the reconciler/sweeper recovers).
    """
    assert (
        decide_ma_review_completion("review", review_skipped=True, ma_is_reviewer=False)
        == "authorize_requeue"
    )
    assert (
        decide_ma_review_completion("review", review_skipped=True, ma_is_reviewer=True)
        == "noop"
    )


# --- C2 + M2: designate_ma_reviewer result-checking + route-helper persist ---

from unittest.mock import patch  # noqa: E402


class _FakeResp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {"ok": True}

    def json(self):
        return self._body


class _FakeClient:
    def __init__(self, status_code, body=None):
        self._sc = status_code
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return _FakeResp(self._sc, self._body)


@pytest.mark.asyncio
async def test_designate_ma_reviewer_true_on_200_no_error():
    from src.backend_client import designate_ma_reviewer

    with patch("httpx.AsyncClient", return_value=_FakeClient(200, {"id": "tid"})):
        assert await designate_ma_reviewer("http://x", "oid", "tid", None) is True


@pytest.mark.asyncio
async def test_designate_ma_reviewer_false_on_non_200():
    """C2: a transport non-200 must report failure."""
    from src.backend_client import designate_ma_reviewer

    with patch("httpx.AsyncClient", return_value=_FakeClient(503)):
        assert await designate_ma_reviewer("http://x", "oid", "tid", None) is False


@pytest.mark.asyncio
async def test_designate_ma_reviewer_false_on_200_with_error_body():
    """F1: /tool-call returns HTTP 200 even on a logical write failure
    ({"error": ...}). designate_ma_reviewer must report that as NOT persisted,
    else the caller re-dispatches the MA on an un-persisted reviewer → loop."""
    from src.backend_client import designate_ma_reviewer

    with patch(
        "httpx.AsyncClient",
        return_value=_FakeClient(200, {"error": "An agent cannot review its own work"}),
    ):
        assert await designate_ma_reviewer("http://x", "oid", "tid", None) is False


@pytest.mark.asyncio
async def test_route_task_moved_inactive_reviewer_persists_ma_reviewer(qm, monkeypatch):
    """M2: the route-helper fallback persists reviewer=MA (so the MA's first
    dispatch is authorized), then routes to the MA."""
    cfg = ConfigStore()
    cfg.agents = [
        {"name": "editor", "is_active": False},
        {"name": "manager-assistant", "is_active": True},
    ]
    dispatcher, supervisor, router = _routing_mocks()
    calls = []

    async def _fake_designate(platform_url, office_id, task_id, security_token):
        calls.append(task_id)
        return True

    monkeypatch.setattr("src.backend_client.designate_ma_reviewer", _fake_designate)

    await route_task_moved(
        {
            "task_id": "T20", "new_status": "review", "reviewer": "editor",
            "readable_id": "WR-001.T20", "assigned_agent": "python-dev",
        },
        queue_manager=qm, dispatcher=dispatcher, supervisor=supervisor,
        router=router, config_store=cfg,
        platform_url="http://x", office_id="oid", security_token=None,
    )

    assert calls == ["T20"], "route helper must persist reviewer=MA"
    ma = await qm.pop_next("manager-assistant")
    assert ma is not None and ma["task_id"] == "T20"


@pytest.mark.asyncio
async def test_route_task_updated_inactive_reviewer_persists_ma_reviewer(qm, monkeypatch):
    """M2 symmetry: route_task_updated also persists reviewer=MA on fallback."""
    cfg = ConfigStore()
    cfg.agents = [
        {"name": "editor", "is_active": False},
        {"name": "manager-assistant", "is_active": True},
    ]
    dispatcher, supervisor, router = _routing_mocks()
    calls = []

    async def _fake_designate(platform_url, office_id, task_id, security_token):
        calls.append(task_id)
        return True

    monkeypatch.setattr("src.backend_client.designate_ma_reviewer", _fake_designate)

    await route_task_updated(
        {"task_data": {
            "task_id": "T21", "status": "review", "reviewer": "editor",
            "readable_id": "WR-001.T21", "assigned_agent": "python-dev",
        }},
        queue_manager=qm, dispatcher=dispatcher, supervisor=supervisor,
        router=router, config_store=cfg,
        platform_url="http://x", office_id="oid", security_token=None,
    )

    assert calls == ["T21"], "route_task_updated must persist reviewer=MA"
    ma = await qm.pop_next("manager-assistant")
    assert ma is not None and ma["task_id"] == "T21"
