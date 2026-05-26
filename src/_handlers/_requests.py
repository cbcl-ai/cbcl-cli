"""Request-bridge dispatch helper (split from handlers.py).

The backend's ``RequestBridge`` calls the communicator over the
connector WebSocket with a ``{"type": "request", "action": ..., ...}``
envelope. This module owns the dispatch table for those actions:

- ``fs_*`` → ``fs_handler.handle_request``
- ``mcp_list_query`` → return cached server list from Redis
- ``mcp_get_oauth_creds`` → read credentials.json inside the container
- ``auth_status`` / ``auth_start`` / ``auth_complete`` → Phase 2/3
  pre-flight + setup-wizard auth flow

Each branch eventually calls ``router.ws_client.send({"type":
"response", "request_id", "data"})`` so the backend's awaitable
gets resolved.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess

logger = logging.getLogger(__name__)


async def dispatch_backend_request(
    message: dict,
    *,
    router,
    fs_handler,
    office,
    redis_client,
    container_name: str,
) -> None:
    """Route a request from the backend's RequestBridge to its handler."""
    action = message.get("action", "")
    request_id = message.get("request_id", "")

    if action.startswith("fs_"):
        await fs_handler.handle_request(message, router.ws_client.send)
        return

    if action == "script_set_binding":
        # AI-driven variable → office-secret binding (Phase 1.5 +
        # 0.2.22). The Automation Script Developer agent calls a new
        # MCP tool ``bind_script_variable`` so it can wire up its own
        # credentials instead of escalating to the user. Backend
        # validates (script exists, variable declared, secret exists),
        # then forwards here.
        #
        # The handler is intentionally thin — all policy lives on the
        # backend side; we just write to ``variables.json`` via the
        # existing ``VariableManager.set_binding`` primitive (same
        # path the chat WS uses for user-driven UI binding writes).
        from src.scripts.variable_manager import (
            VariableManager,
            normalise_binding,
        )

        params = message.get("params") or {}
        script_name = (params.get("script_name") or "").strip()
        var_name = (params.get("variable_name") or "").strip()
        binding_raw = params.get("binding")

        if not script_name or not var_name:
            await router.ws_client.send({
                "type": "response",
                "request_id": request_id,
                "data": {
                    "error": "script_name and variable_name required",
                    "status": 400,
                },
            })
            return

        manager = VariableManager(office.workspace_path)
        try:
            if binding_raw is None:
                manager.set_binding(script_name, var_name, None)
                payload = {"cleared": True}
            else:
                binding = normalise_binding(binding_raw, variable_name=var_name)
                if binding is None:
                    await router.ws_client.send({
                        "type": "response",
                        "request_id": request_id,
                        "data": {
                            "error": (
                                "binding shape invalid; expected "
                                "{kind: 'literal'|'office_secret', ...}"
                            ),
                            "status": 400,
                        },
                    })
                    return
                manager.set_binding(script_name, var_name, binding)
                payload = {"binding": binding}
        except OSError as exc:
            await router.ws_client.send({
                "type": "response",
                "request_id": request_id,
                "data": {
                    "error": f"failed to persist binding: {exc}",
                    "status": 500,
                },
            })
            return
        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": {
                "script_name": script_name,
                "variable_name": var_name,
                **payload,
            },
        })
        return

    if action == "mcp_list_query":
        cache_key = f"office:{office.id}:mcp_list"
        cached = (
            await redis_client.get(cache_key) if redis_client else None
        )
        servers = json.loads(cached) if cached else []
        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": {"servers": servers},
        })
        return

    if action == "mcp_get_oauth_creds":
        server_name = message.get("params", {}).get("name", "")
        creds_data: dict = {}
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["docker", "exec", container_name, "cat",
                 "/home/agent/.claude/.credentials.json"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                all_creds = json.loads(result.stdout)
                mcp_oauth = all_creds.get("mcpOAuth", {})
                for key, entry in mcp_oauth.items():
                    if (
                        entry.get("serverName") == server_name
                        or server_name in key
                    ):
                        creds_data = {
                            "client_id": entry.get("clientId", ""),
                            "client_secret": entry.get("clientSecret", ""),
                            "auth_server_url": (
                                entry.get("discoveryState", {})
                                .get("authorizationServerUrl", "")
                            ),
                            "step_up_scope": entry.get("stepUpScope", ""),
                        }
                        break
        except Exception as exc:
            logger.warning("Failed to read MCP OAuth creds: %s", exc)
        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": creds_data,
        })
        return

    if action == "auth_status":
        # Phase 2 pre-flight: container ``claude --print`` round-trip,
        # ~1-3 s but blocking on subprocess. Wrap in to_thread so the
        # WS reader keeps draining other messages.
        from src.auth_helpers import (
            get_auth_account_info,
            verify_claude_in_container,
        )

        authenticated = False
        account: str | None = None
        if container_name:
            authenticated = await asyncio.to_thread(
                verify_claude_in_container, container_name,
            )
            if authenticated:
                account = await asyncio.to_thread(
                    get_auth_account_info, container_name,
                )
        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": {
                "authenticated": authenticated,
                "account": account,
                "container_name": container_name or None,
            },
        })
        return

    if action == "auth_start":
        # Phase 3: PKCE generation + URL build. Sync (no subprocess).
        from src.auth_service import start_auth_flow

        try:
            payload = await asyncio.to_thread(
                start_auth_flow, container_name or "",
            )
            response_data: dict = {**payload, "error": None}
        except ValueError as exc:
            response_data = {"error": str(exc)}
        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": response_data,
        })
        return

    if action == "auth_complete":
        # Phase 3: exchange the user-pasted code for tokens and write
        # credentials.json into the container. ~30 s budget for the
        # round-trip + verify.
        from src.auth_service import complete_auth_flow

        params = message.get("params", {})
        session_id = params.get("session_id", "")
        raw_code = params.get("code", "")
        response_data = await asyncio.to_thread(
            complete_auth_flow, session_id, raw_code,
        )
        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": response_data,
        })
        return

    if action == "agent_feed":
        # Backend's ``GET /agents/by-name/{agent}/recent-activity``
        # endpoint asks for the per-agent activity feed the
        # dispatcher writes to OUR Redis (see ``handlers.py:_push_agent_feed``).
        # Same architectural pattern as ``agent_queues`` below —
        # without this bridge the backend never sees the
        # near-real-time feed and silently falls back to the DB
        # path. Returns ``{"items": [{...}, ...]}``; ``[]`` is a
        # valid response (5-min TTL on the LIST means quiet
        # agents legitimately have empty feeds).
        params = message.get("params") or {}
        agent_name = params.get("agent_name", "")
        try:
            limit = int(params.get("limit", 30))
        except (TypeError, ValueError):
            limit = 30
        items = await _read_agent_feed(
            redis_client, office.id, agent_name, limit,
        )
        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": {"items": items},
        })
        return

    if action == "generate_agent_config":
        # AI-assisted agent creation. Backend POSTs the user's
        # free-text description + the office's existing skills /
        # connectors (slug + display_name + description, so the model
        # can pick relevant ones without inventing slugs). We run
        # one ``claude --print`` inside the office container and
        # return the parsed JSON. The backend maps slug references
        # to UUIDs before returning to the UI.
        from src.setup_generator import generate_agent_from_description

        params = message.get("params") or {}
        description = (params.get("description") or "").strip()
        agent_office_name = (params.get("office_name") or "").strip()
        agent_office_description = params.get("office_description") or None
        available_skills = params.get("available_skills") or []
        available_connectors = params.get("available_connectors") or []
        # Backend passes the slim catalog so the AI can pick catalog
        # templates to install via skill_template_ids.
        skill_catalog = params.get("skill_catalog") or []

        agent_data: dict = {}
        if not description:
            agent_data = {"error": "description is required"}
        elif not container_name:
            agent_data = {"error": "office container is not running"}
        else:
            try:
                agent_data = await generate_agent_from_description(
                    container_name,
                    description,
                    agent_office_name,
                    agent_office_description,
                    available_skills,
                    available_connectors,
                    skill_catalog,
                )
            except Exception as exc:
                # Log the full exception (with traceback) for the
                # operator; surface only a generic, user-safe summary
                # to the browser so workspace paths / token prefixes
                # / Claude CLI stderr don't leak through the UI.
                logger.exception("generate_agent_config failed: %s", exc)
                agent_data = {
                    "error": (
                        "Agent generation failed. Check the cbcl daemon "
                        "logs and retry."
                    ),
                }

        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": agent_data,
        })
        return

    if action == "generate_workstream_context":
        # AI-assisted workstream context-note generation. Same
        # one-shot Claude CLI pattern as ``generate_agent_config``.
        # The user supplies a free-text brief covering goals,
        # processes, responsibilities, tools — the model synthesises
        # a polished markdown context note that goes into the
        # workstream's ``context_notes`` field (and eventually into
        # its CLAUDE.md). The backend returns the markdown verbatim
        # for the UI to render in the existing editable textarea.
        from src.setup_generator import generate_workstream_context_note

        params = message.get("params") or {}
        workstream_name = (params.get("workstream_name") or "").strip()
        brief = (params.get("brief") or "").strip()
        ws_office_name = (params.get("office_name") or "").strip() or None

        ws_data: dict = {}
        if not brief:
            ws_data = {"error": "brief is required"}
        elif not workstream_name:
            ws_data = {"error": "workstream_name is required"}
        elif not container_name:
            ws_data = {"error": "office container is not running"}
        else:
            try:
                context_notes = await generate_workstream_context_note(
                    container_name,
                    workstream_name,
                    brief,
                    ws_office_name,
                )
                ws_data = {"context_notes": context_notes}
            except Exception as exc:
                logger.exception(
                    "generate_workstream_context failed: %s", exc,
                )
                ws_data = {
                    "error": (
                        "Context-note generation failed. Check the cbcl "
                        "daemon logs and retry."
                    ),
                }

        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": ws_data,
        })
        return

    if action == "generate_skill":
        # AI-assisted standalone skill creation. The user opens the
        # Create Skill dialog, types a one-paragraph overview, and
        # clicks Generate. Same one-shot Claude CLI pattern as
        # ``generate_agent_config`` / ``generate_workstream_context``.
        #
        # Returns the skill payload PLUS a ``written_path``: the daemon
        # lands SKILL.md on disk inline (single WS round-trip) instead
        # of forcing the backend to follow up with a separate ``fs_write``
        # call. Backend reads ``written_path`` on success and skips
        # the redundant write; if it's absent (pre-0.2.10 daemon), or
        # if the daemon's slug disagrees with the backend's resolved
        # slug, the backend falls back to its own ``fs_write``.
        #
        # The slug-of-record policy + the actual file write live in
        # ``setup_generator.write_skill_to_workspace`` so that policy
        # is co-located with the generation logic and is unit-testable
        # without the WS scaffold. This handler stays pure dispatch +
        # serialize.
        from src.setup_generator import (
            generate_skill_from_overview, write_skill_to_workspace,
        )

        params = message.get("params") or {}
        overview = (params.get("overview") or "").strip()
        requested_name = (params.get("name") or "").strip() or None
        requested_display_name = (
            (params.get("display_name") or "").strip() or None
        )
        skill_office_name = (params.get("office_name") or "").strip() or None
        skill_office_description = params.get("office_description") or None

        skill_data: dict = {}
        if not overview:
            skill_data = {"error": "overview is required"}
        elif not container_name:
            skill_data = {"error": "office container is not running"}
        else:
            try:
                skill_data = await generate_skill_from_overview(
                    container_name,
                    overview,
                    requested_name,
                    requested_display_name,
                    skill_office_name,
                    skill_office_description,
                )
                try:
                    rel_path = write_skill_to_workspace(
                        fs_handler._workspace,
                        skill_data,
                        requested_name,
                    )
                    skill_data["written_path"] = rel_path
                except ValueError as exc:
                    # Slug rejected by validate_name (escape attempt
                    # or empty after slugify). Surface a specific
                    # message instead of the catchall below.
                    skill_data = {"error": f"invalid skill name: {exc}"}
                except OSError as exc:
                    # Disk-level failures (full disk, permission
                    # denied, bind-mount race) get a distinct message
                    # so operators see the actual cause in the toast.
                    logger.exception("generate_skill write failed: %s", exc)
                    skill_data = {
                        "error": (
                            f"Failed to write SKILL.md: {type(exc).__name__}. "
                            "Check the cbcl daemon logs."
                        ),
                    }
            except Exception as exc:
                # Generic surface; full exception (traceback + paths)
                # only goes to the operator log, mirroring the
                # ``generate_agent_config`` security posture.
                logger.exception("generate_skill failed: %s", exc)
                skill_data = {
                    "error": (
                        "Skill generation failed. Check the cbcl daemon "
                        "logs and retry."
                    ),
                }

        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": skill_data,
        })
        return

    if action == "agent_queues":
        # Backend's ``GET /agent-queues`` endpoint asks us for a
        # snapshot of each agent's queue + active task. Critically:
        # the dispatcher writes this state to OUR Redis (the
        # daemon's in-process ``fakeredis`` by default — see
        # ``communicator/src/local_redis.py``), NOT the backend's
        # docker-compose Redis. Without this bridge action the
        # backend was reading a perpetually-empty Redis and the
        # agent-activity sidebar's Queue tab showed "empty" no
        # matter what the dispatcher had picked up.
        #
        # Returns ``{"agents": {agent_name: {pending: [...],
        # active: {...} or null}}}``. The backend enriches each
        # entry with readable_id + title from Postgres — we just
        # provide the raw queue snapshot here.
        agents_snapshot = await _snapshot_agent_queues(
            redis_client, office.id,
        )
        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": {"agents": agents_snapshot},
        })
        return


async def _read_agent_feed(
    redis_client, office_id: str, agent_name: str, limit: int,
) -> list[dict]:
    """Read the agent's activity feed LIST from Redis.

    Mirrors the read path the backend tried (and failed) to use
    directly. Returns at most ``limit`` entries, newest first.
    Empty list when:
    - the agent_name is missing or non-string
    - the LIST is absent (no events in the last 5 min)
    - the LIST is corrupted (each entry is JSON-decoded
      defensively; bad rows are dropped, the rest pass through)
    """
    if redis_client is None or not agent_name:
        return []
    # Cap limit to defend against caller-controlled pagination
    # accidents. The communicator's _push_agent_feed trims to 30
    # max — fetching more than that returns no new data.
    capped = max(1, min(int(limit), 30))
    key = f"office:{office_id}:agent_feed:{agent_name}"
    try:
        raw_items = await redis_client.lrange(key, 0, capped - 1)
    except Exception:
        return []
    out: list[dict] = []
    for raw in raw_items or []:
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


async def _snapshot_agent_queues(
    redis_client, office_id: str,
) -> dict[str, dict]:
    """Read every agent's queue ZSET + active HASH from Redis.

    Returns a dict keyed by agent name with this shape::

        {
            "manager-assistant": {
                "pending": [{"task_id", "readable_id", "title",
                             "priority", "status", "assigned_agent"}, ...],
                "active":  {"task_id", "readable_id", "status",
                            "mode", "started_at"} or None,
            },
            ...
        }

    Cheap-ish: one SCAN to discover keys, then one MULTI pipeline
    to fetch all of them at once. Safe to call on every poll
    (frontend refetches every 10 s).
    """
    if redis_client is None:
        return {}
    prefix = f"office:{office_id}:aq"

    # Discover queues + active hashes via SCAN. SCAN is safe in
    # production (won't block Redis) and Fake Redis supports it.
    queue_keys: list[str] = []
    active_keys: list[str] = []
    async for key in redis_client.scan_iter(
        match=f"{prefix}:*:queue", count=100,
    ):
        queue_keys.append(key)
    async for key in redis_client.scan_iter(
        match=f"{prefix}:*:active", count=100,
    ):
        active_keys.append(key)

    agent_names: set[str] = set()
    for key in (*queue_keys, *active_keys):
        parts = key.split(":")
        # office:{uuid}:aq:{agent}:queue|active
        if len(parts) >= 5:
            agent_names.add(parts[3])
    if not agent_names:
        return {}

    sorted_names = sorted(agent_names)
    async with redis_client.pipeline(transaction=False) as pipe:
        for name in sorted_names:
            pipe.zrange(f"{prefix}:{name}:queue", 0, -1)
            pipe.hgetall(f"{prefix}:{name}:active")
        results = await pipe.execute()

    agents: dict[str, dict] = {}
    for i, name in enumerate(sorted_names):
        pending_raw = results[i * 2] or []
        active_raw = results[i * 2 + 1] or {}

        pending: list[dict] = []
        for member in pending_raw:
            try:
                data = json.loads(member)
            except (json.JSONDecodeError, TypeError):
                continue
            pending.append({
                "task_id": data.get("task_id") or data.get("id", ""),
                "readable_id": data.get("readable_id", ""),
                "title": data.get("title", ""),
                "priority": data.get("priority", "medium"),
                "status": data.get("status", ""),
                "assigned_agent": data.get("assigned_agent", ""),
            })

        active: dict | None = None
        if active_raw and active_raw.get("task_id"):
            active = {
                "task_id": active_raw.get("task_id", ""),
                "readable_id": active_raw.get("readable_id", ""),
                "status": active_raw.get("status", ""),
                "mode": active_raw.get("mode", ""),
                "started_at": active_raw.get("started_at", ""),
            }

        agents[name] = {"pending": pending, "active": active}

    return agents
