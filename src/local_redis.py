"""In-process Redis client for the cbcl daemon.

CRITICAL DESIGN CONSTRAINT: cbcl runs ONLY office containers on the
daemon host. No separate Redis container, no system Redis package,
no subprocess. The user's host stays untouched outside the office
container set.

Implementation: ``fakeredis.aioredis.FakeRedis`` — a pure-Python
implementation of the Redis protocol that runs inside the daemon
process. Same async API surface as ``redis.asyncio.Redis``, so the
rest of the daemon code is unchanged.

State lifetime: daemon process. On daemon restart, state is empty
and rebuilt from:

- The backend full-resync (re-issues task-queue entries; see
  ``cbcl.dispatcher``'s ``Startup sync`` log line).
- The JSON session-file fallback (``session_manager`` already
  persists Manager session IDs to ``~/.cubicle/`` — that's the
  load-bearing piece).
- Health is re-published every 30s by ``health.reporter`` so the
  brief gap during restart is invisible to the backend.

Operators with a pre-existing real Redis can set
``redis_url=redis://...`` in ``~/.cubicle/config.yaml`` to opt out
of the in-process backend (escape hatch for multi-host
deployments). When that URL is set, ``get_redis_client`` returns a
real ``redis.asyncio.Redis`` instance pointed at it.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Module-level singleton so every consumer in the daemon process
# shares the same FakeRedis instance (and therefore the same data
# store). Without this, ``health.reporter`` would write to one
# in-memory store and ``task_dispatcher`` would read from a
# different one — silent data fragmentation.
_singleton: Any | None = None


async def get_redis_client(redis_url: str | None = None) -> Any:
    """Return a Redis-compatible async client.

    Default: in-process ``FakeRedis``. When ``redis_url`` is a
    non-empty real Redis URL (e.g. ``redis://10.0.0.5:6379/0``),
    returns a real ``redis.asyncio.Redis`` instead — opt-in escape
    hatch for multi-host deployments where the operator runs their
    own Redis intentionally.
    """
    global _singleton
    if redis_url:
        # Operator opted into a real Redis. Connect, ping once to
        # surface unreachable-server errors early, return.
        import redis.asyncio as aioredis

        client = aioredis.from_url(redis_url, decode_responses=True)
        await client.ping()
        logger.info("Using external Redis at %s", redis_url)
        return client

    if _singleton is None:
        # Lazy import so the daemon doesn't pay the cost when an
        # operator IS using external Redis.
        try:
            import fakeredis.aioredis
        except ImportError as exc:
            raise RuntimeError(
                "fakeredis is not installed but is required for the "
                "in-process Redis backend. Re-install cbcl: "
                "``pipx install --force git+"
                "https://github.com/cbcl-ai/cbcl-cli.git``"
            ) from exc

        _singleton = fakeredis.aioredis.FakeRedis(decode_responses=True)
        logger.info(
            "Using in-process FakeRedis (no host services spawned). "
            "State lifetime = daemon process; full resync on restart."
        )
    return _singleton


def reset_for_tests() -> None:
    """Drop the cached singleton — test-only hook."""
    global _singleton
    _singleton = None
