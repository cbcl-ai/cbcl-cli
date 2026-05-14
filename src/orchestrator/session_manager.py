"""Session manager -- tracks all Agent SDK sessions for one office.

Manages Manager sessions keyed by context ("general_chat" or
"workstream:{id}"). Worker sessions are managed by the AgentSupervisor
(process-per-agent architecture -- each worker's session lives in its
own subprocess).

Storage backend: Redis hash at office:{oid}:sessions.
Fallback: {workspace_path}/.cubicle/sessions.json if Redis is unavailable.
Migration: existing file-based sessions are migrated to Redis on first startup.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class SessionManager:
    """Tracks Manager agent sessions with Redis-backed persistence.

    Falls back to file-based persistence if Redis is not available.
    Migrates existing file-based sessions to Redis on first init.
    """

    def __init__(
        self,
        workspace_path: str | None = None,
        redis: Redis | None = None,
        office_id: str = "",
    ) -> None:
        self._manager_sessions: dict[str, str] = {}  # In-memory cache.
        self._active_context: str | None = None
        self._sessions_file: Path | None = None
        self._redis: Redis | None = redis
        self._office_id = office_id
        self._redis_key = f"office:{office_id}:sessions" if office_id else ""
        self._redis_available = False  # Set to True after successful init.

        if workspace_path:
            self._sessions_file = (
                Path(workspace_path) / ".cubicle" / "sessions.json"
            )

    async def init(self) -> None:
        """Initialize the session manager.

        1. Try to connect to Redis and load sessions from the hash.
        2. If Redis is available and a sessions file exists, migrate
           file contents to Redis and delete the file.
        3. If Redis is not available, fall back to file-based persistence.
        """
        # Step 1: Try Redis.
        if self._redis and self._redis_key:
            try:
                await self._load_from_redis()
                self._redis_available = True
                logger.info(
                    "Session manager using Redis (key=%s, %d sessions loaded)",
                    self._redis_key,
                    len(self._manager_sessions),
                )
            except Exception as exc:
                logger.warning(
                    "Redis unavailable for sessions, falling back to file: %s",
                    exc,
                )
                self._redis_available = False

        # Step 2: Migrate from file if Redis is available.
        if self._redis_available and self._sessions_file:
            await self._migrate_file_to_redis()

        # Step 3: Fallback to file if Redis not available.
        if not self._redis_available and self._sessions_file:
            await self._load_from_file()
            logger.info(
                "Session manager using file fallback (%d sessions loaded)",
                len(self._manager_sessions),
            )

    # Backward-compat alias for callers using the old method name.
    async def init_from_disk(self) -> None:
        """Legacy alias for init(). Calls init() internally."""
        await self.init()

    @property
    def manager_sessions(self) -> dict[str, str]:
        """Read-only access to the session map (used by HealthReporter)."""
        return dict(self._manager_sessions)

    @property
    def active_context(self) -> str | None:
        return self._active_context

    def switch_context(self, context_key: str) -> str | None:
        """Switch Manager to a new context.

        Returns the session_id to resume, or None for a new session.
        """
        previous = self._active_context
        self._active_context = context_key
        session_id = self._manager_sessions.get(context_key)

        if session_id:
            logger.info(
                "Switching context %s -> %s (resume session %s)",
                previous,
                context_key,
                session_id[:12],
            )
        else:
            logger.info(
                "Switching context %s -> %s (new session)",
                previous,
                context_key,
            )

        return session_id

    async def save_session(self, context_key: str, session_id: str) -> None:
        """Save session_id after a query completes.

        Writes to both the in-memory cache and the persistent store
        (Redis hash or file).
        """
        self._manager_sessions[context_key] = session_id
        logger.debug("Session saved for %s: %s", context_key, session_id[:12])

        if self._redis_available:
            await self._save_to_redis(context_key, session_id)
        else:
            await self._persist_to_file()

    def get_session_id(self, context_key: str) -> str | None:
        """Get the session ID for a context key.

        Reads from the in-memory cache (populated at init time).
        """
        return self._manager_sessions.get(context_key)

    def remove_session(self, context_key: str) -> None:
        """Remove a session by context key (synchronous in-memory).

        For async removal with Redis cleanup, use clear_session().
        """
        removed = self._manager_sessions.pop(context_key, None)
        if removed:
            logger.debug("Session removed for %s", context_key)
            # Note: Redis cleanup happens asynchronously if needed.
            # For immediate Redis cleanup, use clear_session().

    async def clear_session(self, context_key: str) -> None:
        """Clear session for a context, forcing a fresh session next time.

        Used after a crash so the next message starts a new session
        instead of resuming a potentially corrupted one.
        """
        removed = self._manager_sessions.pop(context_key, None)
        if removed:
            logger.info(
                "Session cleared for %s (will start fresh on next message)",
                context_key,
            )

            if self._redis_available:
                await self._delete_from_redis(context_key)
            else:
                await self._persist_to_file()

    # ------------------------------------------------------------------
    # Redis persistence
    # ------------------------------------------------------------------

    async def _load_from_redis(self) -> None:
        """Load all sessions from the Redis hash."""
        if not self._redis or not self._redis_key:
            return
        data = await self._redis.hgetall(self._redis_key)
        self._manager_sessions = dict(data) if data else {}

    async def _save_to_redis(self, context_key: str, session_id: str) -> None:
        """Save a single session to the Redis hash."""
        if not self._redis or not self._redis_key:
            return
        try:
            await self._redis.hset(self._redis_key, context_key, session_id)
        except Exception as exc:
            logger.warning("Failed to save session to Redis: %s", exc)
            # Fall back to file.
            await self._persist_to_file()

    async def _delete_from_redis(self, context_key: str) -> None:
        """Delete a single session from the Redis hash."""
        if not self._redis or not self._redis_key:
            return
        try:
            await self._redis.hdel(self._redis_key, context_key)
        except Exception as exc:
            logger.warning("Failed to delete session from Redis: %s", exc)

    # ------------------------------------------------------------------
    # File persistence (fallback)
    # ------------------------------------------------------------------

    async def _load_from_file(self) -> None:
        """Load saved sessions from disk (async-safe via to_thread)."""
        if self._sessions_file and self._sessions_file.exists():
            try:
                raw = await asyncio.to_thread(self._sessions_file.read_text)
                data = json.loads(raw)
                self._manager_sessions = data.get("manager_sessions", {})
                logger.info(
                    "Loaded %d sessions from file",
                    len(self._manager_sessions),
                )
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to load sessions file: %s", exc)

    async def _persist_to_file(self) -> None:
        """Save sessions to disk (async-safe via to_thread)."""
        if not self._sessions_file:
            return

        def _write() -> None:
            try:
                self._sessions_file.parent.mkdir(parents=True, exist_ok=True)
                self._sessions_file.write_text(
                    json.dumps(
                        {"manager_sessions": self._manager_sessions},
                        indent=2,
                    )
                )
            except OSError as exc:
                logger.warning("Failed to persist sessions to file: %s", exc)

        await asyncio.to_thread(_write)

    # ------------------------------------------------------------------
    # Migration
    # ------------------------------------------------------------------

    async def _migrate_file_to_redis(self) -> None:
        """Migrate sessions from file to Redis (one-time operation).

        If a sessions file exists and Redis is available:
        1. Load sessions from file.
        2. Write all sessions to Redis hash.
        3. Delete the sessions file.
        """
        if not self._sessions_file or not self._sessions_file.exists():
            return
        if not self._redis or not self._redis_key:
            return

        try:
            raw = await asyncio.to_thread(self._sessions_file.read_text)
            data = json.loads(raw)
            file_sessions = data.get("manager_sessions", {})

            if file_sessions:
                # Merge: Redis sessions take precedence over file sessions.
                for context_key, session_id in file_sessions.items():
                    if context_key not in self._manager_sessions:
                        self._manager_sessions[context_key] = session_id
                        await self._redis.hset(
                            self._redis_key, context_key, session_id,
                        )

                logger.info(
                    "Migrated %d sessions from file to Redis",
                    len(file_sessions),
                )

            # Delete the file after successful migration.
            await asyncio.to_thread(self._sessions_file.unlink)
            logger.info("Deleted sessions file after migration to Redis")

        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to migrate sessions file: %s", exc)
