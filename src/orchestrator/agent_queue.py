"""Per-agent task queues backed by Redis sorted sets.

Each agent has its own queue (ZSET) and active task tracker (HASH).
The queue is a derived projection of the backend DB state — the DB is
always the source of truth and the queue is updated via events.

Redis key schema:
  office:{oid}:aq:{agent}:queue   — ZSET (pending tasks, scored by priority)
  office:{oid}:aq:{agent}:active  — HASH (current task being worked on)
  office:{oid}:aq:version         — STRING (last full sync timestamp)

Score = column_weight * 1000000 + priority_weight * 10000 + position
  Lower score = higher priority (picked first).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger("cbcl.agent_queue")

# Column weights — lower = picked first.
# Priority order: review tasks first (reviewer duties), then interrupted
# in_progress tasks (crash recovery), then blocked (triage), then ready
# (fresh execution).
COLUMN_WEIGHTS: dict[str, int] = {
    "review": 0,
    "in_progress": 1,
    "blocked": 2,
    "ready": 3,
}

# Task priority weights — lower = picked first.
PRIORITY_WEIGHTS: dict[str, int] = {
    "urgent": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}


def compute_score(task: dict) -> float:
    """Compute ZSET score for a task: lower = higher priority."""
    status = task.get("status", "ready")
    priority = task.get("priority", "medium")
    created_at = task.get("created_at", "")

    col_w = COLUMN_WEIGHTS.get(status, 3)
    pri_w = PRIORITY_WEIGHTS.get(priority, 2)

    # Position: older tasks get lower position = higher priority.
    # Range 0-9999 so it never overwhelms the priority band (10000 per level).
    try:
        ts = datetime.fromisoformat(created_at).timestamp()
        position = int(ts) % 10000
    except (ValueError, TypeError):
        position = 5000

    return col_w * 1000000 + pri_w * 10000 + position


class AgentQueueManager:
    """Per-agent task queues backed by Redis sorted sets.

    Each agent has its own queue (ZSET) and active task tracker (HASH).
    The queue is a derived projection of the backend DB state.
    DB is always the source of truth — queue is updated via events.
    """

    def __init__(self, redis: Redis, office_id: str) -> None:
        self._redis = redis
        self._office_id = office_id
        self._prefix = f"office:{office_id}:aq"
        # Last reconcile outcome — used to suppress duplicate
        # "added 1, removed 0" log lines that repeat every 60s when
        # a single task keeps cycling between the dispatcher's
        # ``pop → fail dep check → add_task`` and the reconciler's
        # "missing from queue → add" path (benign race: the queue is
        # briefly empty during the dispatcher's pop). When the
        # outcome matches the previous cycle the message logs at
        # DEBUG instead of INFO.
        self._last_reconcile_counts: tuple[int, int] = (-1, -1)

    # -- Full sync (startup) -----------------------------------------------

    async def full_sync(self, tasks: list[dict]) -> dict[str, int]:
        """Clear ALL queues and rebuild from board tasks.

        Called on startup BEFORE any agents are spawned.
        Returns dict of agent_name -> queue_size.
        """
        # 1. Delete all existing queue/active keys for this office.
        pattern = f"{self._prefix}:*"
        cursor = "0"
        while True:
            cursor, keys = await self._redis.scan(
                cursor=cursor, match=pattern, count=100,
            )
            if keys:
                await self._redis.delete(*keys)
            if cursor == 0 or cursor == "0":
                break

        # 2. Group tasks by target agent.
        # Review tasks with a reviewer always go to the REVIEWER's queue
        # (not the executor's), regardless of assigned_agent.
        agent_tasks: dict[str, list[dict]] = {}
        for task in tasks:
            agent = task.get("assigned_agent") or ""
            status = task.get("status", "")
            reviewer = task.get("reviewer") or ""
            scope_state = task.get("scope_state")
            scope_id = task.get("scope_id")

            # Scope gate: if task has a scope and it is NOT executing,
            # don't enqueue. The scope activation event will re-queue.
            # Exception: review tasks stay with the reviewer regardless
            # of scope state — the reviewer needs to finish even if the
            # scope was archived/paused mid-work.
            if scope_id and scope_state and scope_state != "executing":
                if status != "review":
                    continue

            # Review tasks: route to reviewer (or MA fallback)
            if status == "review":
                if reviewer:
                    agent = reviewer
                elif not agent:
                    agent = "manager-assistant"
                # else: assigned agent handles review (old flow via MA)
            elif status == "blocked":
                # Blocked tasks ALWAYS route to the Manager Assistant,
                # regardless of ``assigned_agent``. The executor's
                # assignment is preserved on the task row for when it
                # transitions back to ``ready`` — but while blocked,
                # only the MA triages. Without this rule, a blocked
                # task with ``assigned_agent=python-developer`` would
                # be queued to python-developer's queue every
                # reconcile cycle (the original specification has
                # "No other agent picks up a task from the Blocked
                # column"; the old code only enforced this for
                # unassigned tasks). See docs/specs/task-spec.md.
                agent = "manager-assistant"
            elif not agent:
                # Non-review unassigned: MA handles triage of ready /
                # in_progress orphans.
                if status in ("ready", "in_progress"):
                    agent = "manager-assistant"
                else:
                    continue  # Skip backlog tasks

            if agent == "manager":
                continue  # Manager is not a worker agent

            agent_tasks.setdefault(agent, []).append(task)

        # 3. Populate per-agent queues.
        result: dict[str, int] = {}
        for agent, tasks_list in agent_tasks.items():
            queue_key = f"{self._prefix}:{agent}:queue"
            mapping: dict[str, float] = {}
            for task in tasks_list:
                score = compute_score(task)
                member = self._serialize_task(task)
                mapping[member] = score
            if mapping:
                await self._redis.zadd(queue_key, mapping)
            result[agent] = len(tasks_list)

        # 4. Set sync version.
        await self._redis.set(f"{self._prefix}:version", str(time.time()))

        logger.info("Full sync complete: %s", result)
        return result

    # -- Individual operations ---------------------------------------------

    # Lua script: atomically remove all entries matching a task_id, then add
    # the new entry. Runs as a single Redis operation — no race window.
    _LUA_ATOMIC_ADD = """
    local queue_key = KEYS[1]
    local task_id = ARGV[1]
    local new_member = ARGV[2]
    local new_score = tonumber(ARGV[3])

    local members = redis.call('ZRANGE', queue_key, 0, -1)
    for _, member in ipairs(members) do
        local ok, data = pcall(cjson.decode, member)
        if ok and (data.task_id == task_id or data.id == task_id) then
            redis.call('ZREM', queue_key, member)
        end
    end

    redis.call('ZADD', queue_key, new_score, new_member)
    return 1
    """

    async def add_task(self, agent: str, task: dict) -> None:
        """Add or update a task in an agent's queue (atomic).

        Uses a Lua script to atomically remove all existing entries for
        the same task_id and add the new entry in a single Redis call.
        This eliminates the race condition where two concurrent add_task
        calls could both pass the remove phase before either adds,
        creating duplicates.
        """
        task_id = task.get("task_id") or task.get("id", "")
        if not task_id:
            return
        score = compute_score(task)
        queue_key = f"{self._prefix}:{agent}:queue"
        member = self._serialize_task(task)
        await self._redis.eval(
            self._LUA_ATOMIC_ADD, 1, queue_key, task_id, member, str(score),
        )
        logger.debug(
            "Added task %s to %s queue (score=%.0f)",
            task.get("readable_id", task_id), agent, score,
        )

    # Lua script: atomically remove all entries matching a task_id.
    _LUA_ATOMIC_REMOVE = """
    local queue_key = KEYS[1]
    local task_id = ARGV[1]
    local removed = 0

    local members = redis.call('ZRANGE', queue_key, 0, -1)
    for _, member in ipairs(members) do
        local ok, data = pcall(cjson.decode, member)
        if ok and (data.task_id == task_id or data.id == task_id) then
            redis.call('ZREM', queue_key, member)
            removed = removed + 1
        end
    end

    return removed
    """

    async def remove_task(self, agent: str, task_id: str) -> None:
        """Remove a task from an agent's queue (atomic)."""
        if not task_id:
            return
        queue_key = f"{self._prefix}:{agent}:queue"
        await self._redis.eval(
            self._LUA_ATOMIC_REMOVE, 1, queue_key, task_id,
        )

    async def remove_task_from_all(self, task_id: str) -> None:
        """Remove a task from ALL agent queues (used on done/archived)."""
        if not task_id:
            return
        pattern = f"{self._prefix}:*:queue"
        cursor = "0"
        while True:
            cursor, keys = await self._redis.scan(
                cursor=cursor, match=pattern, count=100,
            )
            for key in keys:
                await self._redis.eval(
                    self._LUA_ATOMIC_REMOVE, 1, key, task_id,
                )
            if cursor == 0 or cursor == "0":
                break

    async def reassign(
        self, task_id: str, old_agent: str, new_agent: str, task: dict,
    ) -> None:
        """Move a task from one agent's queue to another."""
        await self.remove_task(old_agent, task_id)
        await self.add_task(new_agent, task)

    async def pop_next(self, agent: str) -> dict | None:
        """Get and remove the highest-priority task (lowest score).

        Returns None if the queue is empty.
        """
        queue_key = f"{self._prefix}:{agent}:queue"
        result = await self._redis.zpopmin(queue_key, count=1)
        if not result:
            return None
        member, _score = result[0]
        try:
            return json.loads(member)
        except json.JSONDecodeError:
            return None

    async def peek_next(self, agent: str) -> dict | None:
        """Look at the next task WITHOUT removing it."""
        queue_key = f"{self._prefix}:{agent}:queue"
        result = await self._redis.zrange(queue_key, 0, 0)
        if not result:
            return None
        try:
            return json.loads(result[0])
        except json.JSONDecodeError:
            return None

    # -- Active task tracking ----------------------------------------------

    async def set_active(
        self,
        agent: str,
        task_id: str,
        readable_id: str,
        status: str,
        mode: str,
        pid: int,
    ) -> None:
        """Mark an agent as working on a task."""
        key = f"{self._prefix}:{agent}:active"
        await self._redis.hset(key, mapping={
            "task_id": task_id,
            "readable_id": readable_id,
            "status": status,
            "mode": mode,  # "execute" | "review" | "triage"
            "pid": str(pid),
            "started_at": datetime.now(timezone.utc).isoformat(),
        })

    async def clear_active(self, agent: str) -> None:
        """Mark an agent as free (no active task)."""
        await self._redis.delete(f"{self._prefix}:{agent}:active")

    async def get_active(self, agent: str) -> dict | None:
        """Get the current active task, or None if agent is free."""
        data = await self._redis.hgetall(f"{self._prefix}:{agent}:active")
        return data if data else None

    async def is_busy(self, agent: str) -> bool:
        """Check if an agent has an active task in the queue system."""
        return await self._redis.exists(f"{self._prefix}:{agent}:active") > 0

    # -- Queue info --------------------------------------------------------

    async def get_queue_size(self, agent: str) -> int:
        """Get number of pending tasks for an agent."""
        return await self._redis.zcard(f"{self._prefix}:{agent}:queue")

    async def get_queue_task_ids(self, agent: str) -> set[str]:
        """Get all task_ids currently in an agent's queue."""
        queue_key = f"{self._prefix}:{agent}:queue"
        members = await self._redis.zrange(queue_key, 0, -1)
        ids: set[str] = set()
        for m in members:
            try:
                data = json.loads(m)
                tid = data.get("task_id") or data.get("id", "")
                if tid:
                    ids.add(tid)
            except json.JSONDecodeError:
                pass
        return ids

    async def get_all_queue_sizes(self) -> dict[str, int]:
        """Get queue sizes for all agents (for health reporting)."""
        result: dict[str, int] = {}
        pattern = f"{self._prefix}:*:queue"
        cursor = "0"
        while True:
            cursor, keys = await self._redis.scan(
                cursor=cursor, match=pattern, count=100,
            )
            for key in keys:
                agent = self._extract_agent_from_key(key)
                if agent:
                    result[agent] = await self._redis.zcard(key)
            if cursor == 0 or cursor == "0":
                break
        return result

    async def get_all_active(self) -> dict[str, dict]:
        """Get active task info for all agents."""
        result: dict[str, dict] = {}
        pattern = f"{self._prefix}:*:active"
        cursor = "0"
        while True:
            cursor, keys = await self._redis.scan(
                cursor=cursor, match=pattern, count=100,
            )
            for key in keys:
                agent = self._extract_agent_from_key(key)
                if agent:
                    data = await self._redis.hgetall(key)
                    if data:
                        result[agent] = data
            if cursor == 0 or cursor == "0":
                break
        return result

    # -- Reconciliation ----------------------------------------------------

    async def reconcile(self, board_tasks: list[dict]) -> dict[str, int]:
        """Safety net: compare queues against board and fix discrepancies.

        Returns dict with counts: {"added": N, "removed": M}.
        """
        # Build expected state from board.
        expected: dict[str, set[str]] = {}  # agent -> set of task_ids
        task_by_id: dict[str, dict] = {}

        for task in board_tasks:
            task_id = task.get("id") or task.get("task_id", "")
            agent = task.get("assigned_agent") or ""
            status = task.get("status", "")
            scope_id = task.get("scope_id")
            scope_state = task.get("scope_state")

            if not task_id:
                continue
            if status in ("done", "archived", "backlog"):
                continue

            # Scope gate during reconciliation: non-executing-scope tasks
            # (except review tasks) are skipped.
            if scope_id and scope_state and scope_state != "executing":
                if status != "review":
                    continue

            reviewer = task.get("reviewer") or ""
            # Review tasks: route to reviewer (or MA fallback)
            if status == "review":
                if reviewer:
                    agent = reviewer
                elif not agent:
                    agent = "manager-assistant"
            elif status == "blocked":
                # Blocked tasks ALWAYS route to the MA, even when the
                # task still has ``assigned_agent`` set. Original spec:
                # "No other agent picks up a task from the Blocked
                # column". See full_sync above + docs/specs/task-spec.md.
                agent = "manager-assistant"
            elif not agent:
                if status in ("ready", "in_progress"):
                    agent = "manager-assistant"
            if not agent or agent == "manager":
                continue

            expected.setdefault(agent, set()).add(task_id)
            task_by_id[task_id] = task

        added = 0
        removed = 0

        for agent, expected_ids in expected.items():
            queue_ids = await self.get_queue_task_ids(agent)
            active = await self.get_active(agent)
            active_id = active.get("task_id") if active else None
            actual_ids = queue_ids | ({active_id} if active_id else set())

            # Missing from queue -> add.
            for task_id in expected_ids - actual_ids:
                task = task_by_id.get(task_id)
                if task:
                    await self.add_task(agent, task)
                    added += 1

            # In queue but not on board -> remove.
            for task_id in actual_ids - expected_ids:
                if task_id and task_id != active_id:
                    await self.remove_task(agent, task_id)
                    removed += 1

        # Clean up queues for agents NOT in the expected set
        # (e.g., all their tasks moved to done/archived).
        all_queue_agents = set()
        pattern = f"{self._prefix}:*:queue"
        cursor = "0"
        while True:
            cursor, keys = await self._redis.scan(
                cursor=cursor, match=pattern, count=100,
            )
            for key in keys:
                agent = self._extract_agent_from_key(key)
                if agent:
                    all_queue_agents.add(agent)
            if cursor == 0 or cursor == "0":
                break

        for agent in all_queue_agents - set(expected.keys()):
            stale_ids = await self.get_queue_task_ids(agent)
            for task_id in stale_ids:
                await self.remove_task(agent, task_id)
                removed += 1
            if stale_ids:
                logger.info(
                    "Reconciliation: cleaned %d stale entries from %s queue",
                    len(stale_ids), agent,
                )

        if added or removed:
            # Demote duplicate-outcome reconcile reports to DEBUG.
            # The dispatcher's "pop → fail dep check → re-add" cycle
            # briefly empties the queue between pop and add_task; if
            # the reconciler ticks in that window it sees the task
            # missing and re-adds (the Lua dedup in ``add_task``
            # keeps the queue correct, so this is benign). Without
            # this guard the log gets a duplicate "added 1, removed
            # 0" line every minute for the lifetime of any task
            # waiting on a dependency.
            outcome = (added, removed)
            if outcome != self._last_reconcile_counts:
                self._last_reconcile_counts = outcome
                logger.info(
                    "Reconciliation: added %d, removed %d tasks",
                    added, removed,
                )
            else:
                logger.debug(
                    "Reconciliation: added %d, removed %d tasks "
                    "(same as previous cycle)",
                    added, removed,
                )
        else:
            # No churn at all this cycle — reset the deduper so the
            # next NON-zero outcome logs at INFO.
            self._last_reconcile_counts = (0, 0)

        return {"added": added, "removed": removed}

    # -- Internal helpers --------------------------------------------------

    @staticmethod
    def _serialize_task(task: dict) -> str:
        """Serialize task to JSON for ZSET member.

        Normalizes task_id key: backend uses "id", dispatcher uses "task_id".
        """
        normalized = dict(task)
        if "id" in normalized and "task_id" not in normalized:
            normalized["task_id"] = normalized["id"]
        return json.dumps(normalized, default=str, sort_keys=True)

    def _extract_agent_from_key(self, key: str) -> str | None:
        """Extract agent name from a Redis key like office:...:aq:analyst:queue.

        Key format: ``office:{oid}:aq:{agent}:{suffix}`` (suffix is
        ``queue``/``active``). The agent name is everything between ``aq``
        and the trailing suffix, rejoined with ``:`` — so a name that itself
        contains a colon is reconstructed in full rather than truncated to
        its first segment (the previous ``parts[aq_idx+1]`` returned only the
        first piece).
        """
        parts = key.split(":")
        try:
            aq_idx = parts.index("aq")
        except ValueError:
            return None
        # Need at least one segment between "aq" and the trailing suffix.
        if aq_idx + 1 <= len(parts) - 2:
            return ":".join(parts[aq_idx + 1 : -1])
        return None
