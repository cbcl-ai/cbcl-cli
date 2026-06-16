"""Manager-session runner for ``agent_worker`` (Wave 10 decomposition).

Extracted from ``agent_worker.py`` — the Manager-side IPC handler
+ Claude CLI streaming runner. The chat handler is the entry point
the Orchestrator drops a ``chat_message`` IPC frame at; it builds
the system prompt from ``ConfigStore``, calls the streaming runner
below, and emits ``response_chunk`` / ``response_final`` IPC
frames back.

The runner uses Anthropic-style ``--include-partial-messages``
incremental streaming so each text token reaches the user fast,
while still re-using the per-turn fallback for older CLI builds
that drop the partial-messages flag.

Adapters in ``agent_worker.py`` (``_handle_chat_message``,
``_run_manager_session``) call the module-level functions below
with ``self`` as the first arg.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from src.agent_protocol import MessageType
from ._agent_worker_mcp import _CLAUDE_CLI_BUILTIN_DISALLOW

if TYPE_CHECKING:
    from src.agent_worker import AgentWorker


logger = logging.getLogger(__name__)


async def handle_chat_message(worker: "AgentWorker", msg: dict) -> None:
    """Handle a Manager chat query (streaming response).

    Receives a user's chat message, builds the Manager system prompt,
    runs an SDK query, and streams response chunks back to the
    Orchestrator. Sends response_final when the query completes.
    """
    context_key = msg.get("context_key", "general_chat")
    content = msg.get("content", "")
    conversation_id = msg.get("conversation_id", "")
    session_id = msg.get("session_id")
    context_data = msg.get("context_data", {})

    logger.info("Chat message [%s]: %s", context_key, content[:80])

    try:
        from src.orchestrator.manager_controller import build_dynamic_context
        from src.config_sync.sync_service import ConfigStore

        # Build system prompt from context data
        # ConfigStore is populated from agent_config passed in the message
        config_store = ConfigStore()
        worker._agent_config = msg.get("agent_config", {})
        config_store.update_from_agent_config(worker._agent_config)
        # T5.3.3: a resumed session (session_id present) already has the chat
        # history in its transcript — don't re-inject it into the system prompt.
        system_prompt = build_dynamic_context(
            context_key, context_data, config_store,
            is_fresh_session=not session_id,
        )

        # Route through the worker's adapter (instance method) rather
        # than calling ``run_manager_session`` directly so test code
        # that monkeypatches ``worker._run_manager_session`` still
        # takes effect.
        _result = await worker._run_manager_session(
            user_message=content,
            system_prompt=system_prompt,
            session_id=session_id,
            context_key=context_key,
            conversation_id=conversation_id,
            agent_config=msg.get("agent_config", {}),
        )
        # Tolerant unpack: the live run_manager_session returns a 3-tuple
        # (sid, cost, rotate_session); test monkeypatches may still return a
        # 2-tuple (T4.3.4 back-compat).
        new_session_id, total_cost = _result[0], _result[1]
        rotate_session = _result[2] if len(_result) > 2 else False

        # Report final
        worker._send({
            "type": MessageType.RESPONSE_FINAL,
            "conversation_id": conversation_id,
            "context_key": context_key,
            "token_cost": total_cost or 0.0,
            "session_id": new_session_id or "",
            # T4.3.4: when True, the receiver clears the saved session so the
            # NEXT turn for this context starts fresh (no --resume).
            "rotate_session": rotate_session,
        })

    except asyncio.CancelledError:
        # Chat-v2 (CHAT-005): user-initiated cancel. Emit a clean
        # response_final so the ManagerController's chat handler
        # unblocks even if the CLI subprocess didn't get a chance
        # to flush a final NDJSON frame.
        #
        # W5-P2-C1: ``session_id=""`` here means "do NOT update the
        # session map from this cancel event". The receiver's guard
        # ``if session_id and context_key: save_session(...)`` in
        # ``_manager_events.on_response_final`` correctly skips
        # empty saves, so the PRIOR turn's session_id stays in the
        # map and the NEXT user turn resumes from there with full
        # history intact. (Earlier comment said "next turn will
        # start fresh" — that was wrong; the prior session is
        # preserved, which is exactly the user-friendly outcome:
        # the cancelled turn drops out but conversation history
        # before it remains.)
        logger.info(
            "Chat cancelled mid-turn (conv=%s, ctx=%s)",
            (conversation_id or "")[:8], context_key,
        )
        worker._send({
            "type": MessageType.RESPONSE_FINAL,
            "conversation_id": conversation_id,
            "context_key": context_key,
            "token_cost": 0.0,
            "session_id": "",
        })
        raise
    except Exception as exc:
        logger.exception("Chat message failed: %s", exc)
        # Event-hygiene: stamp the ORIGINATING turn's conversation_id so
        # the controller's stale-frame gate (T1.1.5,
        # ``_manager_events.handle_manager_event``) can drop a zombie
        # turn's late error instead of letting it poison the active
        # turn (set ``_response_error``/``_response_done`` and charge
        # the consecutive-error streak).
        worker._send({
            "type": MessageType.ERROR,
            "message": str(exc)[:1000],
            "conversation_id": conversation_id,
            "context_key": context_key,
            "fatal": False,
        })


async def run_manager_session(
    worker: "AgentWorker",
    user_message: str,
    system_prompt: str,
    session_id: str | None,
    context_key: str,
    conversation_id: str,
    agent_config: dict,
) -> tuple[str | None, float | None, bool]:
    """Run a Manager CLI query via docker exec with streaming response.

    The Manager session is long-lived. Each call to this function runs
    one query (one user message -> one response). The session_id is
    used to resume the conversation from the previous query.

    Returns:
        Tuple of (new_session_id, total_cost, rotate_session). When
        ``rotate_session`` is True the resumed context this turn exceeded
        ``CUBICLE_MANAGER_ROTATE_TOKENS`` (T4.3.4) and the NEXT turn should
        start fresh (no --resume) — a proactive rotation at the turn boundary
        that converts a guaranteed future "session too large" wedge into a
        planned, loss-free reset.
    """
    from src.docker.session_bridge import stream_cli_session

    container_name = agent_config.get("_container_name", "")
    # F5/R2-F9 (audit): central fallback constant. Manager runs Opus
    # in normal operation; fallback only fires when agent_config is
    # malformed. Log so the gap surfaces.
    from src.orchestrator._model_defaults import FALLBACK_MANAGER_MODEL
    model = agent_config.get("model") or FALLBACK_MANAGER_MODEL
    if not agent_config.get("model"):
        logger.warning(
            "Manager agent_config missing 'model' — falling back to "
            "%s. Investigate the chat dispatch path.",
            FALLBACK_MANAGER_MODEL,
        )

    logger.info(
        "Manager session: container=%s, model=%s, prompt_len=%d, session=%s",
        container_name, model, len(system_prompt or ""), session_id,
    )

    if not container_name:
        raise RuntimeError(
            "No _container_name in agent_config — cannot run docker exec"
        )

    # Build MCP config for manager tools (task_mode="manager" bypasses executor guard).
    # context_key determines whether board-write tools are available —
    # General Chat is READ-ONLY; writes require switching to a workstream.
    mcp_config = worker._build_mcp_config(
        "manager", task_mode="manager", context_key=context_key,
    )

    total_cost: float | None = None
    new_session_id: str | None = None
    # Effective input context for this turn, summed from the CLI's own
    # result-frame usage. Drives the proactive native /compact below.
    effective_input_tokens = 0

    agent_cwd = "/workspace/agents/manager"

    # Token-level streaming state:
    # - ``text_blocks_seen`` counts the text content blocks we've
    #   already streamed within this turn. On the 2nd+ block we
    #   prepend "\n\n" so accumulated markdown keeps its structure
    #   (lists, headings, code fences) instead of running into the
    #   previous paragraph.
    # - ``current_block_kind`` tracks whether the in-flight block
    #   is text or tool_use, so we ignore input_json_delta frames
    #   that belong to a tool_use argument stream.
    text_blocks_seen = 0
    current_block_kind: str | None = None

    msg_count = 0
    # Defense-in-depth at the CLI level. ``Bash`` and ``Task``
    # (subagent spawn) are Claude CLI **built-ins**, NOT MCP
    # tools — the role filter in mcp_tool_server.py only filters
    # ``mcp__cubicle-tools__*`` names, so it cannot exclude
    # these. ``--disallowed-tools`` is the only mechanism that
    # actually keeps the Manager from calling them, so this
    # block is the primary guard (not "belt-and-braces"). The
    # system prompt and ``manager-spec.md`` reinforce it but
    # neither is enforced by Claude CLI on its own. We also
    # exclude Claude CLI's built-in TaskCreate family — see
    # ``_CLAUDE_CLI_BUILTIN_DISALLOW`` for the rationale.
    MANAGER_DISALLOWED_TOOLS = ["Bash", "Task", *_CLAUDE_CLI_BUILTIN_DISALLOW]

    async for msg in stream_cli_session(
        container_name=container_name,
        model=model,
        system_prompt=system_prompt,
        prompt=user_message,
        cwd=agent_cwd,
        mcp_config=mcp_config,
        disallowed_tools=MANAGER_DISALLOWED_TOOLS,
        resume_session=session_id,
        include_partial_messages=True,
    ):
        msg_count += 1
        logger.info("Manager stream msg #%d: type=%s", msg_count, msg.type)
        if msg.type == "result":
            new_session_id = msg.data.get("session_id")
            total_cost = msg.data.get("cost_usd") or msg.data.get("total_cost_usd")
            usage = msg.data.get("usage") or {}
            # The bytes the model actually saw this turn ≈ fresh input +
            # cache-creation + cache-read. cache_read is usually the bulk
            # (the resumed transcript + system prompt), which is exactly
            # what we want to bound.
            effective_input_tokens = (
                (usage.get("input_tokens") or 0)
                + (usage.get("cache_creation_input_tokens") or 0)
                + (usage.get("cache_read_input_tokens") or 0)
            )
        elif msg.type == "stream_event":
            # --include-partial-messages emits Anthropic-style
            # incremental frames. We only need three of them:
            #   content_block_start  → note kind + paragraph break
            #   content_block_delta  → text_delta → one chunk
            #   content_block_stop   → clear kind
            event = msg.data.get("event", {})
            event_type = event.get("type", "")

            if event_type == "content_block_start":
                block = event.get("content_block", {}) or {}
                current_block_kind = block.get("type")
                if current_block_kind == "text":
                    # Separate the current text block from the
                    # previous one so markdown lists / headings
                    # don't collapse into a single paragraph
                    # (Manager often emits "Here's what I found:"
                    # then a list after a tool call).
                    if text_blocks_seen > 0:
                        worker._send({
                            "type": MessageType.RESPONSE_CHUNK,
                            "conversation_id": conversation_id,
                            "context_key": context_key,
                            "content": "\n\n",
                        })
                    text_blocks_seen += 1
                elif current_block_kind == "tool_use":
                    # User-visible "Manager is using X" signal.
                    # tool_proxy handles the actual tool execution
                    # separately — this is purely a typing-indicator
                    # hint and keeps the 5-min watchdog alive.
                    tool_name = block.get("name") or "tool"
                    # Strip the MCP server prefix for readability
                    # (mcp__cubicle-tools__get_board → get_board).
                    bare = tool_name.split("__")[-1] if "__" in tool_name else tool_name
                    worker._send({
                        "type": MessageType.ACTIVITY,
                        "conversation_id": conversation_id,
                        "context_key": context_key,
                        "activity": "tool_use",
                        "tool": bare,
                    })

            elif event_type == "content_block_delta":
                delta = event.get("delta", {}) or {}
                if (
                    current_block_kind == "text"
                    and delta.get("type") == "text_delta"
                ):
                    text = delta.get("text", "")
                    if text:
                        worker._send({
                            "type": MessageType.RESPONSE_CHUNK,
                            "conversation_id": conversation_id,
                            "context_key": context_key,
                            "content": text,
                        })

            elif event_type == "content_block_stop":
                current_block_kind = None

        elif msg.type == "assistant":
            # With --include-partial-messages the full `assistant`
            # message arrives AFTER we've already streamed every
            # text_delta — re-emitting here would duplicate the
            # content in the UI. The full assistant frame is still
            # useful as a fallback when partial frames aren't
            # available (e.g. a future CLI version that drops the
            # flag): only emit if no text deltas have been seen
            # for this turn.
            if text_blocks_seen == 0:
                for block in msg.data.get("message", {}).get("content", []):
                    if block.get("type") == "text" and block.get("text"):
                        worker._send({
                            "type": MessageType.RESPONSE_CHUNK,
                            "conversation_id": conversation_id,
                            "context_key": context_key,
                            "content": block["text"],
                        })
        elif msg.type == "error":
            logger.error("Manager stream error: %s", msg.data)
            err = msg.data.get("error", "Unknown error")
            stderr = (msg.data.get("stderr") or "").strip()
            # Fold the CLI stderr into the raised message so the
            # controller's classify_error() can see the REAL cause.
            # The session bridge only puts a synthetic "Claude CLI
            # exited with code N" in ``error`` and stashes the actual
            # diagnostic (e.g. "prompt is too long" on an oversized
            # --resume) in ``stderr``. Without this the controller
            # classified every exit-N as UNKNOWN_FATAL and never reset
            # the session, wedging a long-lived chat forever.
            raise RuntimeError(f"{err}\n{stderr}" if stderr else err)

    # ``input_tokens`` is logged for observability — it lets us watch the
    # resumed-context size per turn and confirm the lean board/task
    # projections (and native auto-compact) keep a long session bounded.
    logger.info(
        "Manager stream ended: %d messages, session=%s, cost=%s, "
        "input_tokens=%d",
        msg_count, new_session_id, total_cost, effective_input_tokens,
    )

    # T4.3.4: proactive session rotation. If the resumed context this turn
    # exceeded the threshold, signal the controller to start the NEXT turn
    # fresh (the reactive CONTEXT_TOO_LARGE recovery only fires AFTER a wedge).
    rotate_session = False
    try:
        rotate_threshold = int(os.environ.get("CUBICLE_MANAGER_ROTATE_TOKENS", "120000"))
    except (TypeError, ValueError):
        rotate_threshold = 120000
    if rotate_threshold > 0 and effective_input_tokens > rotate_threshold and new_session_id:
        rotate_session = True
        logger.warning(
            "Manager session %s exceeded rotation threshold (%d > %d effective "
            "input tokens) — the next turn for %s will start fresh.",
            (new_session_id or "")[:12], effective_input_tokens,
            rotate_threshold, context_key,
        )
        # One-line user-visible note so any continuity gap is explained.
        worker._send({
            "type": MessageType.RESPONSE_CHUNK,
            "conversation_id": conversation_id,
            "context_key": context_key,
            "content": (
                "\n\n_(This conversation has grown large; I'll start a fresh "
                "session for the next message to stay responsive. Prior "
                "decisions are recorded on the board and in office files.)_"
            ),
        })

    return new_session_id, total_cost, rotate_session
