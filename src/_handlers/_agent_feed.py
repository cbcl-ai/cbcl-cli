"""Per-agent Redis activity feed used by the sidebar "Recent Activity" UI.

Extracted from ``handlers.py`` (wave 13). The feed is a lightweight
Redis LIST per (office, agent) capped at ``_AGENT_FEED_MAX`` entries
with a ``_AGENT_FEED_TTL`` second sliding TTL — the UI polls the
list every few seconds for a quick "what is this agent up to right
now" surface that doesn't require a backend round-trip.

Key shape: ``office:{oid}:agent_feed:{agent_name}``

The push helper is intentionally noisy-tolerant (a Redis hiccup
must NEVER break the agent's actual workflow) — every Redis call
is wrapped in a broad ``except`` that drops the entry silently.

A standalone module keeps the helper unit-testable without spinning
up the entire ``init_office_process_model`` orchestration. Tests
that need to assert "Manager Assistant entry landed in the feed
after a triage" can ``await push_agent_feed(...)`` directly.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.orchestrator.agent_supervisor import AgentSupervisor

logger = logging.getLogger("cbcl.handlers")

# Sliding TTL on the feed list. 5 minutes matches the typical
# "user opened the sidebar after a few minutes idle" window — long
# enough to show recent activity, short enough that a long-idle
# office's keys are reaped automatically without a sweeper.
_AGENT_FEED_TTL = 300

# Cap per agent. The UI shows ~10 entries at most; cap at 30 so a
# user who scrolls down sees a few more historical events without
# pulling the full task log.
_AGENT_FEED_MAX = 30


async def push_agent_feed(
    agent_name: str,
    event: dict,
    *,
    office_id: str,
    redis_client,
    supervisor: "AgentSupervisor | None" = None,
) -> None:
    """Push one activity entry to the agent's Redis feed list.

    The supervisor (optional) is consulted for the agent's current
    ``readable_id`` when the event payload itself doesn't carry one
    — common for IPC frames that only know the agent identity.
    Passing ``None`` skips that lookup; the entry then has an empty
    ``readable_id`` and the UI falls back to the raw task_id.

    Failures are swallowed. The activity feed is a UI nicety, not
    a critical path — a Redis blip must not interfere with the
    agent's actual work.
    """
    readable_id = event.get("readable_id", "")
    if not readable_id and supervisor is not None:
        # ``_agents`` is the supervisor's internal process map.
        # Reading it is a tight in-memory dict lookup; safe to do
        # from the event hot path.
        proc = supervisor._agents.get(agent_name)
        if proc and proc.current_readable_id:
            readable_id = proc.current_readable_id

    entry = {
        "event_type": event.get("event_type") or event.get("type", ""),
        "content": (
            event.get("content")
            or event.get("comment")
            or event.get("message", "")
        ),
        "task_id": event.get("task_id", ""),
        "readable_id": readable_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    key = f"office:{office_id}:agent_feed:{agent_name}"
    try:
        async with redis_client.pipeline(transaction=False) as pipe:
            pipe.lpush(key, json.dumps(entry))
            pipe.ltrim(key, 0, _AGENT_FEED_MAX - 1)
            pipe.expire(key, _AGENT_FEED_TTL)
            await pipe.execute()
    except Exception:
        # Best-effort — the feed is a UI surface, not a workflow
        # invariant. Log nothing here either; the Redis client's
        # own logger will surface infrastructure-level errors.
        pass
