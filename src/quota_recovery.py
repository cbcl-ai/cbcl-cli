"""Durable office capacity pause; deadlines schedule probes, never imply recovery."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from src.orchestrator.error_classifier import ErrorClass, classify_error

logger = logging.getLogger(__name__)
RESET_GRACE_SECONDS = 60
UNKNOWN_RESET_RETRY_SECONDS = 3600
CONTEXT_RETRY_SECONDS = 600


class QuotaStateMixin:
    def initialize_quota_state(self) -> None:
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS office_quota (
                    office_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quota_contexts (
                    office_id TEXT NOT NULL, context_key TEXT NOT NULL,
                    retry_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (office_id, context_key)
                );
                CREATE TABLE IF NOT EXISTS quota_sessions (
                    office_id TEXT NOT NULL, task_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (office_id, task_id)
                );
            """)
            connection.execute("BEGIN IMMEDIATE")
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(quota_contexts)")
            }
            if "retry_at" not in columns:
                connection.execute(
                    "ALTER TABLE quota_contexts ADD COLUMN retry_at REAL NOT NULL DEFAULT 0"
                )

    def defer_quota_context(self, context_key: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO quota_contexts (office_id, context_key) VALUES (?, ?)",
                (self.office_id, context_key),
            )

    def quota_contexts(self, *, due_at: float | None = None) -> list[str]:
        with self._connection() as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT context_key FROM quota_contexts WHERE office_id=? AND (? IS NULL OR retry_at<=?) ORDER BY retry_at, context_key",
                    (self.office_id, due_at, due_at),
                )
            ]

    def delay_quota_context(self, context_key: str, retry_at: float) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE quota_contexts SET retry_at=? WHERE office_id=? AND context_key=?",
                (retry_at, self.office_id, context_key),
            )

    def acknowledge_quota_context(self, context_key: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM quota_contexts WHERE office_id=? AND context_key=?",
                (self.office_id, context_key),
            )

    def quota_status(self) -> dict:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT revision, payload FROM office_quota WHERE office_id=?",
                (self.office_id,),
            ).fetchone()
        return (
            {**json.loads(row["payload"]), "revision": row["revision"]}
            if row
            else {"state": "running", "revision": 0}
        )

    def pause_for_quota(
        self, error: str, model: str, *, now: float | None = None
    ) -> dict:
        now = time.time() if now is None else now
        remedy = classify_error(error)
        reset = remedy.reset_at.timestamp() if remedy.reset_at else None
        due = (
            max(now + RESET_GRACE_SECONDS, reset + RESET_GRACE_SECONDS)
            if reset
            else now + UNKNOWN_RESET_RETRY_SECONDS
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision, payload FROM office_quota WHERE office_id=?",
                (self.office_id,),
            ).fetchone()
            old = json.loads(row["payload"]) if row else {}
            models = (
                dict(old.get("models") or {}) if old.get("state") != "running" else {}
            )
            # A second worker failing in the same incident cannot postpone a known
            # deadline with an unparseable duplicate error.
            previous = models.get(model)
            if previous and reset is None:
                due, reset = previous["check_at"], previous.get("reset_at")
            models[model] = {"reset_at": reset, "check_at": due}
            payload = {
                "state": "quota_paused",
                "models": models,
                "next_check_at": max(item["check_at"] for item in models.values()),
                "since": old.get("since", now)
                if old.get("state") != "running"
                else now,
            }
            revision = row["revision"] + 1 if row else 1
            connection.execute(
                "INSERT INTO office_quota VALUES (?, ?, ?) ON CONFLICT(office_id) DO UPDATE SET revision=excluded.revision, payload=excluded.payload",
                (self.office_id, revision, json.dumps(payload)),
            )
        return {**payload, "revision": revision}

    def update_quota(self, revision: int, payload: dict) -> bool:
        with self._connection() as connection:
            return (
                connection.execute(
                    "UPDATE office_quota SET revision=revision+1, payload=? WHERE office_id=? AND revision=?",
                    (json.dumps(payload), self.office_id, revision),
                ).rowcount
                == 1
            )

    def save_quota_session(self, task: dict, event: dict) -> None:
        session_id = event.get("session_id")
        if not session_id:
            return
        payload = {
            key: task.get(key)
            for key in (
                "status",
                "execution_cycle",
                "execution_generation",
                "review_retry_epoch",
                "assigned_agent",
                "reviewer",
            )
        }
        if task.get("status") == "review" and not payload["reviewer"]:
            from src.review_routing import default_reviewer

            payload["reviewer"] = default_reviewer(task)
        payload["session_id"] = session_id
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO quota_sessions VALUES (?, ?, ?) ON CONFLICT(office_id, task_id) DO UPDATE SET payload=excluded.payload",
                (self.office_id, str(task["id"]), json.dumps(payload)),
            )

    def quota_session(self, task: dict) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM quota_sessions WHERE office_id=? AND task_id=?",
                (self.office_id, str(task.get("task_id") or task.get("id"))),
            ).fetchone()
        if not row:
            return None
        saved = json.loads(row["payload"])
        if task.get("status") == "review" and not task.get("reviewer"):
            from src.review_routing import default_reviewer

            task = {**task, "reviewer": default_reviewer(task)}
        if all(
            saved.get(key) == task.get(key)
            for key in (
                "status",
                "execution_cycle",
                "execution_generation",
                "review_retry_epoch",
                "assigned_agent",
                "reviewer",
            )
        ):
            return saved["session_id"]
        return None


def public_quota_status(state: dict) -> dict:
    def stamp(value):
        return (
            datetime.fromtimestamp(value, timezone.utc).isoformat()
            if isinstance(value, (float, int))
            else None
        )

    return {
        "state": state["state"],
        "next_check_at": stamp(state.get("next_check_at")),
        "reset_at": stamp(
            max(
                (
                    item.get("reset_at") or 0
                    for item in state.get("models", {}).values()
                ),
                default=0,
            )
            or None
        ),
    }


class QuotaRecovery:
    def __init__(
        self,
        runtime_state,
        *,
        container_id: str,
        dispatcher,
        probe=None,
        clock=time.time,
    ):
        self.runtime = runtime_state
        self.container_id = container_id
        self.dispatcher = dispatcher
        self.clock = clock
        self.probe = probe or self._probe
        self._lock = asyncio.Lock()
        self._check_task = None
        self.on_recovered = None

    def request_check(self, *, force: bool = False) -> None:
        if self._check_task is not None and not self._check_task.done():
            return
        state = self.runtime.quota_status()
        if state["state"] == "running":
            if not self.runtime.quota_contexts(due_at=self.clock()):
                return
        elif not force and self.clock() < state["next_check_at"]:
            return
        self._check_task = asyncio.create_task(
            self.tick(force=force), name="office-quota-check"
        )

        def finished(task):
            if not task.cancelled() and task.exception() is not None:
                logger.error(
                    "Office quota recovery needs another check",
                    exc_info=task.exception(),
                )

        self._check_task.add_done_callback(finished)

    def stop(self) -> None:
        if self._check_task is not None:
            self._check_task.cancel()

    async def _probe(self, model: str) -> tuple[bool, str]:
        from src._setup_cli import _probe_claude_works
        from src.office_runtime import async_runtime_lock, validated_container_id

        errors: list[str] = []

        def probe():
            identity = validated_container_id(self.runtime.office_id, self.container_id)
            return _probe_claude_works(identity, model=model, error_sink=errors)

        # Serialize OAuth refresh with sign-in/migration/keepalive. Keep the
        # lock until the bounded subprocess finishes even during shutdown.
        async with async_runtime_lock(self.runtime.office_id):
            operation = asyncio.create_task(asyncio.to_thread(probe))
            try:
                ok = await asyncio.shield(operation)
            except asyncio.CancelledError:
                await operation
                raise
        return bool(ok), errors[-1] if errors else "Capacity check unavailable"

    async def tick(self, *, force: bool = False) -> bool:
        # User input and heartbeat can arrive together. Only one probe owns a
        # revision; an old success must never erase a newer quota observation.
        if self._lock.locked():
            return False
        async with self._lock:
            state = self.runtime.quota_status()
            if state["state"] == "running":
                if self.on_recovered is not None:
                    await self.on_recovered()
                return False
            if not force and self.clock() < state["next_check_at"]:
                return False
            revision = state.pop("revision")
            if not self.runtime.update_quota(
                revision,
                {
                    **state,
                    "state": "checking_capacity",
                    "next_check_at": self.clock() + 180,
                },
            ):
                return False
            revision += 1
            failures = {}
            for model in state.get("models", {}):
                try:
                    ok, error = await self.probe(model)
                except Exception:
                    logger.warning("Office capacity check unavailable", exc_info=True)
                    ok, error = False, "Capacity check unavailable"
                if not ok:
                    remedy = classify_error(error)
                    reset = (
                        remedy.reset_at.timestamp()
                        if remedy.error_class == ErrorClass.USAGE_LIMIT_EXCEEDED
                        and remedy.reset_at
                        else None
                    )
                    due = (
                        max(
                            self.clock() + RESET_GRACE_SECONDS,
                            reset + RESET_GRACE_SECONDS,
                        )
                        if reset
                        else self.clock() + UNKNOWN_RESET_RETRY_SECONDS
                    )
                    failures[model] = {"reset_at": reset, "check_at": due}
            next_state = (
                {"state": "running"}
                if not failures
                else {
                    "state": "quota_paused",
                    "since": state.get("since"),
                    "models": failures,
                    "next_check_at": max(
                        item["check_at"] for item in failures.values()
                    ),
                }
            )
            if not self.runtime.update_quota(revision, next_state):
                return False
            if failures:
                return False
            await self.dispatcher._reconcile_once()
            self.dispatcher.wake()
            if self.on_recovered is not None:
                await self.on_recovered()
            return True


def defer_quota_task(runtime, task: dict, event: dict) -> bool:
    """Preserve the board stage only for the still-current interrupted attempt."""
    caller = event.get("_caller") or {}
    from src.review_routing import default_reviewer

    reviewing = bool(event.get("is_review_completion"))
    owner = (
        (task.get("reviewer") or default_reviewer(task))
        if reviewing
        else task.get("assigned_agent")
    )
    if (
        str(caller.get("task_id") or "") != str(task.get("id") or "")
        or not owner
        or caller.get("agent_name") != owner
    ):
        return False
    if not caller or any(
        caller.get(key, 0) != task.get(key, 0)
        for key in (
            "execution_cycle",
            "execution_generation",
            "review_retry_epoch",
        )
    ):
        return False
    expected = "review" if reviewing else "in_progress"
    if task.get("status") != expected:
        return False
    runtime.save_quota_session(task, event)
    return True
