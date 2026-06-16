"""T3.2.2 — queue hardening (07/G13, 03/#13 §4.4, 03/§4.8, 03/#28, 07/G20).

Pins the four fixes:

(a) a FAILED board fetch skips ``reconcile()`` entirely — queues are
    never wiped by a transient backend error;
(b) the board fetch paginates past 200 — a >200-active-task office
    reconciles (and therefore dispatches) everything;
(c) the FIFO score uses the raw creation epoch (no ~2.8h modulo wrap),
    so same-priority ordering holds for arbitrary timestamps;
(d) a TRANSIENT status-lookup failure during ``dispatch_agent``
    re-queues the in-hand entry instead of dropping it;
plus 07/G20: a 401/403 board fetch logs at ERROR (revoked Company
Token) and skips reconcile like other failures.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

import fakeredis.aioredis

from src.orchestrator.agent_queue import AgentQueueManager, compute_score
from src.orchestrator.task_dispatcher import (
    _STATUS_FETCH_FAILED,
    TaskDispatcher,
)


# ---------------------------------------------------------------------------
# Fixtures (mirrors tests/test_task_dispatcher.py)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
def office_id() -> str:
    return "test-office-hardening"


@pytest.fixture
def mock_supervisor() -> MagicMock:
    supervisor = MagicMock()
    supervisor.can_spawn.return_value = True
    supervisor.is_agent_busy.return_value = False
    supervisor.spawn_worker = AsyncMock(return_value=True)
    supervisor._kill_process = AsyncMock()
    supervisor.get_all_statuses.return_value = {
        "analyst": {"pid": 1234, "status": "working"},
    }
    return supervisor


@pytest.fixture
def mock_config() -> MagicMock:
    config = MagicMock()
    config.get_agent.return_value = {
        "name": "analyst",
        "model": "claude-sonnet-4-6",
        "system_prompt": "x",
        "allowed_tools": ["Read"],
    }
    config.agents = [{"name": "analyst", "is_active": True}]
    return config


@pytest_asyncio.fixture
async def queue_manager(fake_redis, office_id) -> AgentQueueManager:
    return AgentQueueManager(fake_redis, office_id)


@pytest.fixture
def dispatcher(
    fake_redis, office_id, mock_supervisor, mock_config, queue_manager,
) -> TaskDispatcher:
    return TaskDispatcher(
        redis=fake_redis,
        office_id=office_id,
        supervisor=mock_supervisor,
        config_store=mock_config,
        queue_manager=queue_manager,
    )


class _FakeResponse:
    def __init__(self, status_code: int, items: list[dict] | None = None):
        self.status_code = status_code
        self._items = items or []
        self.text = ""

    def json(self) -> dict:
        return {"items": self._items}


def _fake_async_client(get_side_effect):
    """Build a patchable ``httpx.AsyncClient`` context-manager class."""
    client = MagicMock()
    client.get = AsyncMock(side_effect=get_side_effect)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)
    return factory, client


# ---------------------------------------------------------------------------
# (a) Failed fetch never wipes queues
# ---------------------------------------------------------------------------


class TestReconcileSkipOnFailedFetch:

    async def test_fetch_error_skips_reconcile_and_keeps_queue(
        self, dispatcher, queue_manager,
    ):
        await queue_manager.add_task("analyst", {
            "task_id": "t1", "readable_id": "WR-001.T01",
            "status": "ready", "priority": "medium",
        })

        async def _fail() -> list[dict] | None:
            return None  # fetch failed

        dispatcher._fetch_board_tasks = _fail  # type: ignore[method-assign]
        reconcile_spy = AsyncMock()
        queue_manager.reconcile = reconcile_spy  # type: ignore[method-assign]

        await dispatcher._reconcile_once()

        reconcile_spy.assert_not_awaited()
        assert await queue_manager.get_queue_size("analyst") == 1

    async def test_empty_board_still_reconciles(self, dispatcher, queue_manager):
        # [] means "board really is empty" — reconcile MUST run so
        # stale queue entries are cleaned up.
        async def _empty() -> list[dict] | None:
            return []

        dispatcher._fetch_board_tasks = _empty  # type: ignore[method-assign]
        reconcile_spy = AsyncMock(return_value={"added": 0, "removed": 0})
        queue_manager.reconcile = reconcile_spy  # type: ignore[method-assign]

        await dispatcher._reconcile_once()

        reconcile_spy.assert_awaited_once_with([])

    async def test_transport_error_returns_none(self, dispatcher):
        factory, _ = _fake_async_client(OSError("connection refused"))
        with patch("httpx.AsyncClient", factory):
            result = await dispatcher._fetch_board_tasks()
        assert result is None

    async def test_http_500_returns_none(self, dispatcher):
        factory, _ = _fake_async_client([_FakeResponse(500)])
        with patch("httpx.AsyncClient", factory):
            result = await dispatcher._fetch_board_tasks()
        assert result is None


# ---------------------------------------------------------------------------
# (b) Pagination past 200
# ---------------------------------------------------------------------------


class TestBoardFetchPagination:

    async def test_250_tasks_fetched_across_two_pages(self, dispatcher):
        page1 = [{"id": f"t{i}", "status": "ready"} for i in range(200)]
        page2 = [{"id": f"t{i}", "status": "ready"} for i in range(200, 250)]
        factory, client = _fake_async_client(
            [_FakeResponse(200, page1), _FakeResponse(200, page2)],
        )
        with patch("httpx.AsyncClient", factory):
            result = await dispatcher._fetch_board_tasks()

        assert result is not None
        assert len(result) == 250
        assert {t["id"] for t in result} == {f"t{i}" for i in range(250)}
        # Two requests with advancing offsets.
        offsets = [
            call.kwargs["params"]["offset"]
            for call in client.get.await_args_list
        ]
        assert offsets == [0, 200]

    async def test_250_tasks_all_reconciled_none_evicted(
        self, dispatcher, queue_manager,
    ):
        tasks = [
            {
                "id": f"t{i}", "task_id": f"t{i}",
                "readable_id": f"WR-001.T{i:02d}",
                "status": "ready", "priority": "medium",
                "assigned_agent": "analyst",
                "created_at": "2026-06-01T00:00:00+00:00",
            }
            for i in range(250)
        ]
        page1, page2 = tasks[:200], tasks[200:]
        factory, _ = _fake_async_client(
            [_FakeResponse(200, page1), _FakeResponse(200, page2)],
        )
        with patch("httpx.AsyncClient", factory):
            await dispatcher._reconcile_once()

        # ALL 250 are queued — nothing past the old 200-cap evicted.
        assert await queue_manager.get_queue_size("analyst") == 250

    async def test_failure_on_second_page_fails_whole_fetch(self, dispatcher):
        page1 = [{"id": f"t{i}", "status": "ready"} for i in range(200)]
        factory, _ = _fake_async_client(
            [_FakeResponse(200, page1), _FakeResponse(503)],
        )
        with patch("httpx.AsyncClient", factory):
            result = await dispatcher._fetch_board_tasks()
        # A partial board would wipe everything past the failed page —
        # the whole fetch must fail instead.
        assert result is None

    async def test_single_short_page_one_request(self, dispatcher):
        factory, client = _fake_async_client(
            [_FakeResponse(200, [{"id": "t1", "status": "ready"}])],
        )
        with patch("httpx.AsyncClient", factory):
            result = await dispatcher._fetch_board_tasks()
        assert result is not None and len(result) == 1
        assert client.get.await_count == 1


# ---------------------------------------------------------------------------
# 07/G20 — auth failures are loud and skip reconcile
# ---------------------------------------------------------------------------


class TestAuthFailureLogging:

    @pytest.mark.parametrize("code", [401, 403])
    async def test_auth_failure_logs_error_and_returns_none(
        self, dispatcher, caplog, code,
    ):
        factory, _ = _fake_async_client([_FakeResponse(code)])
        with patch("httpx.AsyncClient", factory):
            with caplog.at_level(logging.DEBUG, logger="cbcl.dispatcher"):
                result = await dispatcher._fetch_board_tasks()

        assert result is None
        auth_records = [
            r for r in caplog.records
            if "Company Token" in r.getMessage()
        ]
        assert auth_records, "expected a Company-Token log line"
        assert all(
            r.levelno >= logging.WARNING for r in auth_records
        ), "401/403 must log at WARNING/ERROR, not DEBUG"
        assert any(r.levelno == logging.ERROR for r in auth_records)


# ---------------------------------------------------------------------------
# (c) FIFO score — no modulo wrap
# ---------------------------------------------------------------------------


class TestFifoScore:

    def test_fifo_preserved_across_old_wrap_boundary(self):
        # The old score used ``int(ts) % 10000`` (~2.8h wrap). Pick two
        # same-priority timestamps straddling a wrap boundary: the
        # older one used to get position 9999 vs the newer one's 0 —
        # ordering NEWEST-first. The raw-epoch score must order
        # oldest-first.
        base = 1_750_000_000
        # Align so old modulo would wrap between the two.
        older_ts = base - (base % 10000) + 9999   # old position: 9999
        newer_ts = older_ts + 1                   # old position: 0
        import datetime as dt
        older = dt.datetime.fromtimestamp(
            older_ts, tz=dt.timezone.utc,
        ).isoformat()
        newer = dt.datetime.fromtimestamp(
            newer_ts, tz=dt.timezone.utc,
        ).isoformat()

        score_older = compute_score(
            {"status": "ready", "priority": "medium", "created_at": older},
        )
        score_newer = compute_score(
            {"status": "ready", "priority": "medium", "created_at": newer},
        )
        assert score_older < score_newer  # FIFO: older picked first

    def test_priority_band_still_dominates_age(self):
        import datetime as dt
        old_low = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc).isoformat()
        new_urgent = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc).isoformat()
        assert compute_score(
            {"status": "ready", "priority": "urgent", "created_at": new_urgent},
        ) < compute_score(
            {"status": "ready", "priority": "low", "created_at": old_low},
        )

    def test_column_band_still_dominates_priority(self):
        import datetime as dt
        ts = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc).isoformat()
        assert compute_score(
            {"status": "review", "priority": "low", "created_at": ts},
        ) < compute_score(
            {"status": "ready", "priority": "urgent", "created_at": ts},
        )

    def test_score_is_float64_exact_for_current_epochs(self):
        # col 3 / pri 3 / epoch ~2026 — the sum must stay integer-exact
        # in float64 so two adjacent-second tasks never tie/swap.
        import datetime as dt
        ts_a = dt.datetime(2026, 6, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
        ts_b = dt.datetime(2026, 6, 1, 0, 0, 1, tzinfo=dt.timezone.utc)
        s_a = compute_score(
            {"status": "ready", "priority": "low",
             "created_at": ts_a.isoformat()},
        )
        s_b = compute_score(
            {"status": "ready", "priority": "low",
             "created_at": ts_b.isoformat()},
        )
        assert s_b - s_a == pytest.approx(1.0)

    def test_unparseable_created_at_does_not_crash(self):
        score = compute_score(
            {"status": "ready", "priority": "medium", "created_at": "junk"},
        )
        assert score > 0


# ---------------------------------------------------------------------------
# (d) Transient status-lookup failure re-queues the in-hand entry
# ---------------------------------------------------------------------------


class TestTransientLookupRequeue:

    async def test_transient_failure_requeues_no_spawn(
        self, dispatcher, queue_manager, mock_supervisor,
    ):
        await queue_manager.add_task("analyst", {
            "task_id": "t-requeue", "readable_id": "WR-001.T01",
            "status": "ready", "priority": "medium",
            "assigned_agent": "analyst",
        })

        async def _transient(task_id: str) -> str | None:
            return _STATUS_FETCH_FAILED

        dispatcher._fetch_task_status = _transient  # type: ignore[method-assign]

        result = await dispatcher.dispatch_agent("analyst")

        assert result is False
        mock_supervisor.spawn_worker.assert_not_awaited()
        # The popped entry is BACK in the queue — no 60s-reconciler
        # dependency.
        assert await queue_manager.get_queue_size("analyst") == 1
        ids = await queue_manager.get_queue_task_ids("analyst")
        assert ids == {"t-requeue"}

    async def test_task_missing_still_drops(
        self, dispatcher, queue_manager, mock_supervisor,
    ):
        # ``None`` (task gone / 404) keeps the deliberate-drop
        # behaviour — reconciler-recovered as designed.
        await queue_manager.add_task("analyst", {
            "task_id": "t-gone", "readable_id": "WR-001.T02",
            "status": "ready", "priority": "medium",
            "assigned_agent": "analyst",
        })

        async def _gone(task_id: str) -> str | None:
            return None

        dispatcher._fetch_task_status = _gone  # type: ignore[method-assign]

        result = await dispatcher.dispatch_agent("analyst")

        assert result is False
        mock_supervisor.spawn_worker.assert_not_awaited()
        assert await queue_manager.get_queue_size("analyst") == 0

    async def test_fetch_task_status_maps_exceptions_to_sentinel(
        self, dispatcher,
    ):
        factory, _ = _fake_async_client(OSError("backend down"))
        with patch("httpx.AsyncClient", factory):
            status = await dispatcher._fetch_task_status("task-1")
        assert status == _STATUS_FETCH_FAILED

    async def test_fetch_task_status_maps_5xx_to_sentinel_404_to_none(
        self, dispatcher,
    ):
        factory, _ = _fake_async_client(
            [_FakeResponse(503), _FakeResponse(404)],
        )
        with patch("httpx.AsyncClient", factory):
            assert await dispatcher._fetch_task_status("t") == (
                _STATUS_FETCH_FAILED
            )
            assert await dispatcher._fetch_task_status("t") is None
