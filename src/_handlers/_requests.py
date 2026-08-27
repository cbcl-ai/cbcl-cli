"""Request-bridge dispatch helper (split from handlers.py).

The backend's ``RequestBridge`` calls the communicator over the
connector WebSocket with a ``{"type": "request", "action": ..., ...}``
envelope. This module owns the dispatch table for those actions:

- ``fs_*`` → ``fs_handler.handle_request``
- ``mcp_list_query`` → return cached server list from Redis
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

from src._setup_json import GenerationError

logger = logging.getLogger(__name__)


# Caps mirror the DB column sizes from ``architecture.md`` §17.
# A hallucinated long display_name / name would otherwise 422 on
# save with a generic error the user can't act on. Truncating
# here keeps the AI's output usable; the small loss of a long
# title is preferable to "create failed".
_DISPLAY_NAME_MAX = 255
_NAME_MAX = 100
# Soft cap on free-form user inputs that get injected into AI
# prompts. Prevents both context-budget blowups (a 50KB brief
# pushed into every retry) and prompt-injection blast radius —
# combined with the fencing below, a malicious input has limited
# room to maneuver.
_USER_INPUT_MAX = 10_000


def _fence_user_input(value: str | None, *, max_len: int = _USER_INPUT_MAX) -> str:
    """Sanitise a user-supplied free-text value for safe AI-prompt
    embedding.

    Two protections:
      * Length cap — prevents a malicious or accidentally-huge
        input from blowing the prompt's context budget.
      * Escape the canonical fence tokens our prompt builders use
        (``<user_input>`` / ``</user_input>``) so a malicious
        input can't break out of its data fence and inject
        instructions the AI would follow.

    The setup_generator's prompt builders are responsible for the
    actual fencing wrapper. This helper just neutralises the
    closing tags inside the value.
    """
    if not value:
        return ""
    # NUL bytes can truncate downstream subprocess argv parsing.
    sanitised = value.replace("\x00", "")
    # Escape the fence-closing token so a malicious input can't
    # close our wrapper and start its own instructions.
    sanitised = sanitised.replace("</user_input>", "</user_input_escaped>")
    sanitised = sanitised.replace("</office_description>", "</office_description_escaped>")
    sanitised = sanitised.replace("</overview>", "</overview_escaped>")
    sanitised = sanitised.replace("</brief>", "</brief_escaped>")
    sanitised = sanitised.replace(
        "</current_instructions>", "</current_instructions_escaped>"
    )
    sanitised = sanitised.replace(
        "</current_notes>", "</current_notes_escaped>"
    )
    # B4: the settings-path source-survey splice fences under its own
    # tag so it can never collide with the workstream regenerate's
    # ``<brief>`` splice.
    sanitised = sanitised.replace(
        "</source_survey>", "</source_survey_escaped>"
    )
    if len(sanitised) > max_len:
        sanitised = (
            sanitised[:max_len] +
            f"\n\n[truncated — input was {len(value)} chars, "
            f"capped at {max_len}]"
        )
    return sanitised


def _cap_str(value, max_len: int) -> str:
    """Truncate a string to ``max_len``; return empty string on
    None / non-string. Used for AI-output validation right before
    the response reaches the backend's create endpoint."""
    if not isinstance(value, str):
        return ""
    return value[:max_len]


def _safe_generation_error(exc: Exception, fallback: str) -> str:
    """Choose the user-facing error string for a failed AI generation.

    ``GenerationError`` messages are curated + actionable (empty-output
    auth/model guidance, "model returned non-object JSON") and safe to show
    verbatim, so the user gets a fix instead of a dead end. Every OTHER
    exception may embed workspace paths, token prefixes, or raw
    ``docker exec`` stderr, so it collapses to the generic ``fallback``.
    The full traceback always reaches the operator log via the caller's
    ``logger.exception`` regardless.
    """
    if isinstance(exc, GenerationError):
        return str(exc)
    return fallback


async def dispatch_backend_request(
    message: dict,
    *,
    router,
    fs_handler,
    office,
    redis_client,
    container_name: str,
    supervisor=None,
    datastore=None,
) -> None:
    """Route a backend RequestBridge call to its handler, GUARANTEEING a
    ``response`` frame.

    AIGEN-2: the WS dispatch wrapper (``ws_client._run_handler``) catches a
    handler exception and logs it WITHOUT sending a response, so any fault
    raised OUTSIDE an action branch's inner try — a bad lazy import, param
    parsing, an ``_fence_user_input`` edge — would leave the backend's
    ``RequestBridge`` future unresolved until its full RPC budget elapsed
    (~240s), surfacing to the user as a misleading 504. Wrapping the impl
    here converts ANY unhandled fault into a clean error ``response`` so the
    backend resolves fast (502) instead of hanging. A duplicate response
    (a branch already sent one, THEN raised) is harmless — the backend
    drops a response for an already-resolved ``request_id``.
    """
    request_id = message.get("request_id", "")
    action = message.get("action", "")
    try:
        await _dispatch_backend_request_impl(
            message,
            router=router,
            fs_handler=fs_handler,
            office=office,
            redis_client=redis_client,
            container_name=container_name,
            supervisor=supervisor,
            datastore=datastore,
        )
    except Exception as exc:
        logger.exception(
            "Unhandled error dispatching backend request action=%r: %s",
            action, exc,
        )
        if not request_id:
            return
        try:
            await router.ws_client.send({
                "type": "response",
                "request_id": request_id,
                "data": {
                    "error": (
                        "The daemon hit an internal error handling this "
                        "request. Check the cbcl daemon logs and retry."
                    ),
                    "status": 500,
                },
            })
        except Exception:
            logger.exception(
                "Failed to send fallback error response for request %r",
                request_id,
            )


async def _dispatch_backend_request_impl(
    message: dict,
    *,
    router,
    fs_handler,
    office,
    redis_client,
    container_name: str,
    supervisor=None,
    datastore=None,
) -> None:
    """Route a request from the backend's RequestBridge to its handler."""
    action = message.get("action", "")
    request_id = message.get("request_id", "")

    if action.startswith("fs_"):
        await fs_handler.handle_request(message, router.ws_client.send)
        return

    if action.startswith("data_"):
        # Flow Studio (FS-P1.T4): the office-local collections datastore.
        # Mirrors the ``fs_`` routing — the whole ``data_*`` family
        # (rows_list / row_get / row_upsert / row_delete / rows_count /
        # import, ws-protocol.md §3.5) dispatches into
        # ``OfficeDatastore.handle_request``, which guarantees a
        # ``response`` frame with the ``{error, status}`` convention on
        # failure. ``datastore=None`` only on test surfaces built
        # without the wiring — answer honestly instead of hanging the
        # backend's RPC future.
        if datastore is None:
            await router.ws_client.send({
                "type": "response",
                "request_id": request_id,
                "data": {
                    "error": (
                        "office datastore is not available on this daemon"
                    ),
                    "status": 503,
                },
            })
            return
        await datastore.handle_request(message, router.ws_client.send)
        return

    if action == "script_get_bindings":
        # Read-back endpoint for the Variables UI. Returns the current
        # ``variables.json`` map for a script so the drawer can show
        # the user the actual binding state (literal value /
        # office_secret ref / cleared) instead of defaulting every
        # row to a blank "Custom" field. Without this read-back, a
        # user opening the Variables drawer on a configured script
        # saw blank inputs and could ACCIDENTALLY clear bindings by
        # hitting Save with empty values.
        from src.scripts.variable_manager import VariableManager

        params = message.get("params") or {}
        script_name = (params.get("script_name") or "").strip()
        if not script_name:
            await router.ws_client.send({
                "type": "response",
                "request_id": request_id,
                "data": {
                    "error": "script_name required",
                    "status": 400,
                },
            })
            return
        manager = VariableManager(office.workspace_path)
        try:
            raw = await asyncio.to_thread(
                manager.get_variables, script_name,
            )
        except OSError as exc:
            await router.ws_client.send({
                "type": "response",
                "request_id": request_id,
                "data": {
                    "error": f"failed to read variables.json: {exc}",
                    "status": 500,
                },
            })
            return
        # Reveal-secret-values is OUT OF SCOPE — secrets stay
        # host-only. For ``literal`` bindings we DO return the value
        # because the user typed it themselves; the Variables UI shows
        # it back in the input field. ``.secrets.json`` content is
        # NEVER read here — only ``variables.json``. The Variables UI
        # still has the masked Set / Replace dialog for secret-marked
        # variables bound as ``literal``.
        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": {
                "script_name": script_name,
                "bindings": raw,
            },
        })
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
                await manager.set_binding_async(script_name, var_name, None)
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
                await manager.set_binding_async(script_name, var_name, binding)
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

    if action == "script_list_executions":
        # Daemon-side enumeration of .scripts/<name>/executions/ —
        # the backend's local _scan_disk_executions reads
        # ~/.cubicle/workspaces which is empty on a split-host
        # deployment (backend + daemon on different machines). This
        # RPC lets the backend list the actual on-disk execution
        # history via the connector WS instead. Mirrors fs_tree's
        # routing pattern: the backend asks, the daemon answers.
        #
        # Returns the same dict shape _scan_disk_executions does so
        # the backend's existing _backfill_missing_from_disk merge
        # path works unchanged.
        from src.scripts._disk_enumerators import list_executions_on_disk

        params = message.get("params") or {}
        script_name = (params.get("script_name") or "").strip()
        limit = params.get("limit") or 200
        if not script_name:
            await router.ws_client.send({
                "type": "response",
                "request_id": request_id,
                "data": {
                    "error": "script_name required",
                    "status": 400,
                },
            })
            return
        try:
            items = await asyncio.to_thread(
                list_executions_on_disk,
                office.workspace_path, script_name, int(limit),
            )
        except OSError as exc:
            await router.ws_client.send({
                "type": "response",
                "request_id": request_id,
                "data": {
                    "error": f"failed to enumerate executions: {exc}",
                    "status": 500,
                },
            })
            return
        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": {"items": items},
        })
        return

    if action == "script_list_notifications":
        # Daemon-side enumeration of .scripts/<name>/.outbox/.processed/ —
        # same split-host root cause as script_list_executions. The
        # Manager-Callback Notifications drawer was empty on prod even
        # after notify_manager() drops landed because the backend
        # couldn't see the daemon's filesystem.
        from src.scripts._disk_enumerators import (
            list_notifications_on_disk,
        )

        params = message.get("params") or {}
        script_name = (params.get("script_name") or "").strip()
        limit = params.get("limit") or 100
        if not script_name:
            await router.ws_client.send({
                "type": "response",
                "request_id": request_id,
                "data": {
                    "error": "script_name required",
                    "status": 400,
                },
            })
            return
        try:
            items = await asyncio.to_thread(
                list_notifications_on_disk,
                office.workspace_path, script_name, int(limit),
            )
        except OSError as exc:
            await router.ws_client.send({
                "type": "response",
                "request_id": request_id,
                "data": {
                    "error": f"failed to enumerate notifications: {exc}",
                    "status": 500,
                },
            })
            return
        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": {"items": items},
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

    if action == "mcp_login_start":
        # Direct connector OAuth (step 1): run ``claude mcp login <name>
        # --no-browser`` under a PTY, capture the authorize URL, and keep
        # the session alive for the paste-back (container/DCR connectors)
        # or report it's an account connector that needs no paste-back.
        from src._handlers._mcp_login import start_login

        params = message.get("params", {})
        name = params.get("name", "")
        result = await asyncio.to_thread(
            lambda: start_login(
                container_name or "",
                name,
                url=params.get("url") or None,
                transport=params.get("transport") or "http",
            )
        )
        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": result,
        })
        return

    if action == "mcp_login_complete":
        # Direct connector OAuth (step 2): write the user-pasted redirect
        # URL into the waiting PTY to finish the token exchange.
        from src._handlers._mcp_login import complete_login

        params = message.get("params", {})
        name = params.get("name", "")
        callback_url = params.get("callback_url", "")
        result = await asyncio.to_thread(
            complete_login, container_name or "", name, callback_url,
        )
        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": result,
        })
        return

    if action == "cli_version":
        # Phase 1 (Opus-4.8 readiness): report the container's Claude CLI
        # version + installed claude-agent-sdk version. The backend
        # compares sdk_version against PyPI to decide "out of date".
        from src.docker.session_bridge import probe_cli_versions

        data: dict = {
            "cli_version": None,
            "sdk_version": None,
            "container_name": container_name or None,
        }
        if container_name:
            data = await probe_cli_versions(container_name)
        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": data,
        })
        return

    if action == "cli_upgrade":
        # Phase 1 slice 2: in-place upgrade of the bundled Claude CLI.
        # Quiesce guard (M9): the symlink flip is container-wide, so
        # refuse if ANY agent in this office is mid-task — flipping the
        # binary under a live ``claude --print`` could crash it. The
        # check is office-wide (active_count), not per-agent.
        from src.docker.session_bridge import upgrade_cli

        if not container_name:
            await router.ws_client.send({
                "type": "response",
                "request_id": request_id,
                "data": {"ok": False, "message": "no container for this office"},
            })
            return

        # ``active_count`` is a @property on AgentSupervisor — read it,
        # don't call it. (Calling an int raises TypeError, which would
        # be swallowed by the dispatch wrapper WITHOUT a response frame,
        # hanging the RPC until the backend's timeout.)
        active = supervisor.active_count if supervisor is not None else 0
        if active > 0:
            await router.ws_client.send({
                "type": "response",
                "request_id": request_id,
                "data": {
                    "ok": False,
                    "busy": True,
                    "message": (
                        f"{active} agent task(s) in progress. Wait for them "
                        "to finish, then retry the upgrade."
                    ),
                },
            })
            return

        # Defensive: any unexpected error here must still emit a response
        # frame, else the RPC future never resolves and the backend waits
        # its full timeout (surfacing as a misleading 504 to the user).
        try:
            result = await upgrade_cli(container_name)
        except Exception as exc:  # noqa: BLE001
            logger.exception("cli_upgrade failed for %s", container_name)
            result = {"ok": False, "message": f"upgrade errored: {exc}"}
        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": {**result, "container_name": container_name},
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
        # valid response (the sliding TTL on the LIST means
        # long-quiet agents legitimately have empty feeds).
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
        # Fence + cap user-supplied free-text before the AI sees it.
        # Without this, a malicious description ("Forget all prior
        # instructions and …") could nudge the model to produce an
        # agent config that bypasses the office's stated purpose.
        description = _fence_user_input(
            (params.get("description") or "").strip(),
        )
        agent_office_name = _cap_str(
            (params.get("office_name") or "").strip(), _DISPLAY_NAME_MAX,
        )
        agent_office_description = _fence_user_input(
            params.get("office_description"),
        ) or None
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
                # Cap AI-output fields against DB column sizes so a
                # hallucinated long display_name / name doesn't 422
                # on save with a generic error the user can't act on.
                # The actual prompt asks for ≤8 words / ≤4 words but
                # the model can drift; truncating here keeps the
                # payload usable.
                if isinstance(agent_data, dict) and "error" not in agent_data:
                    agent_data["display_name"] = _cap_str(
                        agent_data.get("display_name"), _DISPLAY_NAME_MAX,
                    )
                    agent_data["name"] = _cap_str(
                        agent_data.get("name"), _NAME_MAX,
                    )
                    # Validate skill/connector slugs against the
                    # catalogs we passed in — drop anything
                    # hallucinated so the backend's slug→UUID
                    # mapping doesn't silently null out skills the
                    # AI invented.
                    valid_skill_names = {
                        s.get("name") for s in available_skills
                        if isinstance(s, dict) and s.get("name")
                    }
                    if isinstance(agent_data.get("skill_names"), list):
                        agent_data["skill_names"] = [
                            n for n in agent_data["skill_names"]
                            if isinstance(n, str) and n in valid_skill_names
                        ]
                    valid_connector_names = {
                        c.get("name") for c in available_connectors
                        if isinstance(c, dict) and c.get("name")
                    }
                    if isinstance(agent_data.get("connector_names"), list):
                        agent_data["connector_names"] = [
                            n for n in agent_data["connector_names"]
                            if isinstance(n, str) and n in valid_connector_names
                        ]
            except Exception as exc:
                # Log the full exception (with traceback) for the
                # operator; surface a user-safe summary to the browser.
                # A GenerationError (auth/model/empty-output guidance) is
                # forwarded verbatim; anything else collapses to the generic
                # message so workspace paths / token prefixes / Claude CLI
                # stderr don't leak through the UI.
                logger.exception("generate_agent_config failed: %s", exc)
                agent_data = {
                    "error": _safe_generation_error(
                        exc,
                        "Agent generation failed. Check the cbcl daemon "
                        "logs and retry.",
                    ),
                }

        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": agent_data,
        })
        return

    if action == "generate_office_instructions":
        # Item-1: AI office-instructions (CLAUDE.md) generation. Same
        # one-shot Claude CLI pattern as ``generate_agent_config``.
        from src.setup_generator import (
            _sanitize_source_paths,
            generate_office_instructions,
        )

        params = message.get("params") or {}
        oi_directive = _fence_user_input(
            (params.get("directive") or "").strip(),
        )
        oi_office_name = _cap_str(
            (params.get("office_name") or "").strip(), _DISPLAY_NAME_MAX,
        )
        oi_office_description = _fence_user_input(
            params.get("office_description"),
        ) or None
        # Current instructions are the office's own content (not
        # adversarial free-text) — cap length to bound the prompt.
        oi_current = _cap_str((params.get("current_instructions") or ""), 50000)
        oi_mode = (params.get("mode") or "improve").strip()
        if oi_mode not in ("improve", "regenerate"):
            oi_mode = "improve"
        # Instruction-surfaces (D5/D8): workspace-relative source paths
        # the backend already validated — re-validated defensively here
        # (and again in the generator; the helper is idempotent).
        oi_sources = _sanitize_source_paths(params.get("sources") or [])

        oi_data: dict = {}
        if not oi_directive:
            oi_data = {"error": "directive is required"}
        elif not container_name:
            oi_data = {"error": "office container is not running"}
        else:
            try:
                instructions, oi_changes = await generate_office_instructions(
                    container_name,
                    oi_office_name,
                    oi_office_description,
                    oi_current,
                    oi_directive,
                    oi_mode,
                    sources=oi_sources,
                )
                oi_data = {
                    "instructions": instructions,
                    "changes": oi_changes,
                }
            except Exception as exc:
                logger.exception(
                    "generate_office_instructions failed: %s", exc,
                )
                oi_data = {
                    "error": _safe_generation_error(
                        exc,
                        "Office-instructions generation failed. Check the "
                        "cbcl daemon logs and retry.",
                    ),
                }

        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": oi_data,
        })
        return

    if action == "generate_agent_field":
        # AI generate/improve ONE agent field (system_prompt or
        # claude_md_content). Same one-shot Claude CLI + JSON contract as
        # generate_office_instructions.
        from src.setup_generator import generate_agent_field

        params = message.get("params") or {}
        af_field = (params.get("field") or "").strip()
        af_directive = _fence_user_input((params.get("directive") or "").strip())
        af_mode = (params.get("mode") or "improve").strip()
        if af_mode not in ("improve", "regenerate"):
            af_mode = "improve"
        # The current value + office instructions are the office's own
        # content (not adversarial) — cap length to bound the prompt.
        af_current = _cap_str((params.get("current_value") or ""), 100000)
        af_office_name = _cap_str(
            (params.get("office_name") or "").strip(), _DISPLAY_NAME_MAX,
        )
        af_office_desc = _fence_user_input(
            params.get("office_description"),
        ) or None
        af_office_instr = _cap_str((params.get("office_instructions") or ""), 50000)
        af_agent_name = _cap_str(
            (params.get("agent_name") or "").strip(), _DISPLAY_NAME_MAX,
        )
        af_role = _fence_user_input((params.get("role_description") or "").strip())
        af_model = _cap_str((params.get("model") or "").strip(), 255)
        # Lists: cap count + per-item length (tool/skill/connector names).
        af_tools = [
            _cap_str(str(t).strip(), 100)
            for t in (params.get("allowed_tools") or [])[:50]
        ]
        af_skills = [
            _cap_str(str(s).strip(), 255)
            for s in (params.get("skill_names") or [])[:100]
        ]
        af_connectors = [
            _cap_str(str(c).strip(), 255)
            for c in (params.get("connector_names") or [])[:100]
        ]

        af_data: dict = {}
        if af_field not in ("system_prompt", "claude_md_content"):
            af_data = {"error": "invalid field"}
        elif not af_directive:
            af_data = {"error": "directive is required"}
        elif not container_name:
            af_data = {"error": "office container is not running"}
        else:
            try:
                content = await generate_agent_field(
                    container_name,
                    field=af_field,
                    directive=af_directive,
                    mode=af_mode,
                    current_value=af_current,
                    office_name=af_office_name,
                    office_description=af_office_desc,
                    office_instructions=af_office_instr,
                    agent_name=af_agent_name,
                    role_description=af_role,
                    model=af_model,
                    allowed_tools=af_tools,
                    skill_names=af_skills,
                    connector_names=af_connectors,
                )
                af_data = {"content": content}
            except Exception as exc:
                logger.exception("generate_agent_field failed: %s", exc)
                af_data = {
                    "error": _safe_generation_error(
                        exc,
                        "Agent-field generation failed. Check the cbcl daemon "
                        "logs and retry.",
                    ),
                }

        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": af_data,
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
        # Instruction-surfaces (D5): ``mode="improve"`` +
        # ``current_notes`` bring office-side improve parity;
        # ``sources`` runs the scoped source survey.
        from src.setup_generator import (
            _sanitize_source_paths,
            generate_workstream_context_note,
        )

        params = message.get("params") or {}
        # Same fencing posture as generate_agent_config — workstream
        # name + brief are user-supplied free-text reaching the AI
        # prompt directly.
        workstream_name = _cap_str(
            (params.get("workstream_name") or "").strip(), _DISPLAY_NAME_MAX,
        )
        brief = _fence_user_input(
            (params.get("brief") or "").strip(),
        )
        ws_office_name = _cap_str(
            (params.get("office_name") or "").strip(), _DISPLAY_NAME_MAX,
        ) or None
        # Regenerate is the historical default (D5) — unlike the
        # office side, which defaults to improve.
        ws_mode = (params.get("mode") or "regenerate").strip()
        if ws_mode not in ("improve", "regenerate"):
            ws_mode = "regenerate"
        # Current notes are the workstream's own content (not
        # adversarial free-text) — cap length to bound the prompt; the
        # generator fences them with the ``current_notes`` tag.
        ws_current = _cap_str((params.get("current_notes") or ""), 50000)
        ws_sources = _sanitize_source_paths(params.get("sources") or [])

        ws_data: dict = {}
        if not brief:
            ws_data = {"error": "brief is required"}
        elif not workstream_name:
            ws_data = {"error": "workstream_name is required"}
        elif not container_name:
            ws_data = {"error": "office container is not running"}
        else:
            try:
                context_notes, ws_changes = (
                    await generate_workstream_context_note(
                        container_name,
                        workstream_name,
                        brief,
                        ws_office_name,
                        mode=ws_mode,
                        current_notes=ws_current,
                        sources=ws_sources,
                    )
                )
                ws_data = {
                    "context_notes": context_notes,
                    "changes": ws_changes,
                }
            except Exception as exc:
                logger.exception(
                    "generate_workstream_context failed: %s", exc,
                )
                ws_data = {
                    "error": _safe_generation_error(
                        exc,
                        "Context-note generation failed. Check the cbcl "
                        "daemon logs and retry.",
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
        # Same fencing posture as the other AI-gen handlers.
        overview = _fence_user_input(
            (params.get("overview") or "").strip(),
        )
        requested_name = _cap_str(
            (params.get("name") or "").strip(), _NAME_MAX,
        ) or None
        requested_display_name = _cap_str(
            (params.get("display_name") or "").strip(), _DISPLAY_NAME_MAX,
        ) or None
        skill_office_name = _cap_str(
            (params.get("office_name") or "").strip(), _DISPLAY_NAME_MAX,
        ) or None
        skill_office_description = _fence_user_input(
            params.get("office_description"),
        ) or None

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
                # ``generate_agent_config`` security posture. A user-safe
                # GenerationError (auth/model/empty-output) is forwarded
                # verbatim so the user gets the actionable hint.
                logger.exception("generate_skill failed: %s", exc)
                skill_data = {
                    "error": _safe_generation_error(
                        exc,
                        "Skill generation failed. Check the cbcl daemon "
                        "logs and retry.",
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
    - the LIST is absent (TTL lapsed with no new events)
    - the LIST is corrupted (each entry is JSON-decoded
      defensively; bad rows are dropped, the rest pass through)

    A non-empty read REFRESHES the sliding TTL: the push path only
    bumps it on new frames, and an ultracode dynamic-workflow phase
    legitimately pushes nothing for many minutes — an actively-watched
    feed must not expire underneath the sidebar's poll (incident
    2026-07-16).
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
    if raw_items:
        # Best-effort sliding-TTL refresh on read (see docstring). A
        # failure here must never break the read — same posture as
        # the push helper.
        from src._handlers._agent_feed import _AGENT_FEED_TTL

        try:
            await redis_client.expire(key, _AGENT_FEED_TTL)
        except Exception:
            pass
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
