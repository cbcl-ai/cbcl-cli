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
import time
from typing import TYPE_CHECKING

from src.agent_protocol import MessageType
from src.orchestrator.error_classifier import ErrorClass, classify_error
from ._agent_worker_mcp import _CLAUDE_CLI_BUILTIN_DISALLOW
from ._session_policy import _SUBAGENT_TOOLS, is_unknown_flag_error
from ._tool_summary import build_tool_activity

# Error classes a Manager turn retries IN-PLACE when they occur BEFORE any
# user-visible output: the work is fine, the API was just busy. A rate-limit
# waits ~60s (per the resilience requirement); overload ~180s; a transient
# transport drop resumes immediately. USAGE_LIMIT is deliberately EXCLUDED —
# an interactive chat turn must not sleep for a multi-hour usage window; it
# surfaces the "usage limit reached" error to the user instead (autonomous
# worker tasks get the deferred-resume path, not interactive Manager turns).
_MANAGER_RETRY_CLASSES = {
    ErrorClass.RATE_LIMITED,
    ErrorClass.API_OVERLOADED,
    ErrorClass.CONNECTION_LOST,
}
_MANAGER_MAX_ATTEMPTS = 3

# FIX M1(b): fixed continuation prompt for the ONE mid-stream recovery
# attempt. Resuming the CURRENT attempt's session id CONTINUES the
# interrupted transcript rather than replaying the pre-turn one, so the
# SES-02 duplicate-side-effect objection (re-streamed text / re-issued
# board mutations) doesn't apply — the model sees its own partial turn
# and is told to finish it, not redo it.
_MANAGER_CONTINUATION_PROMPT = (
    "You were interrupted mid-turn by a transient API error. Continue "
    "EXACTLY where you stopped: do not repeat text you already sent to "
    "the user, and do not re-issue tool calls that already completed — "
    "their results are in this conversation. Finish the turn."
)

# FIX M1(c): while a retry/continuation wait runs, ping liveness every
# this many seconds so the controller's inactivity watchdog (300s) never
# fires mid-wait and the status pill shows an honest "API busy" message
# instead of drifting into the misleading "Manager silent" warning.
_RETRY_WAIT_PING_INTERVAL = 15.0


class _ManagerRetry(Exception):
    """Raised inside a Manager attempt for a retryable upfront API error
    (rate limit / overload / transient drop, before any streamed output).
    The retry loop catches it, waits the remedy backoff, and re-runs the
    same turn (resuming the session)."""

    def __init__(self, remedy, err_text: str) -> None:
        self.remedy = remedy
        self.err_text = err_text
        super().__init__(err_text)


class _ManagerContinuation(Exception):
    """FIX M1(b): raised for a retryable API error that struck MID-STREAM
    (after visible text or an executed tool), when the CURRENT attempt's
    session id was captured from the system/init frame. The retry loop
    grants ONE continuation attempt: resume THAT session with the fixed
    continuation prompt so the turn finishes instead of dying as a red
    bubble the user must manually resend."""

    def __init__(self, remedy, err_text: str, resume_id: str) -> None:
        self.remedy = remedy
        self.err_text = err_text
        self.resume_id = resume_id
        super().__init__(err_text)


class _ManagerEffortDegrade(Exception):
    """SES-05 graceful-degrade: raised when an older container Claude CLI
    rejects ``--effort`` (unknown flag) BEFORE any visible output. The retry
    loop drops the effort flag and re-runs the SAME turn IMMEDIATELY (no
    backoff). Mirrors the worker (``_agent_worker_task``) + generation
    (``_run_chunk``) degrade paths so the Manager — the highest-value session —
    isn't the one surface that hard-fails on a CLI that predates ``--effort``."""

    def __init__(self, err_text: str) -> None:
        self.err_text = err_text
        super().__init__(err_text)

if TYPE_CHECKING:
    from src.agent_worker import AgentWorker


# Defense-in-depth at the CLI level. ``Bash`` and the subagent-spawn
# tools (``Task`` on legacy CLI builds, ``Agent`` on v2.1.63+ where the
# tool was renamed) are Claude CLI **built-ins**, NOT MCP tools — the
# role filter in mcp_tool_server.py only filters ``mcp__cubicle-tools__*``
# names, so it cannot exclude these. ``--disallowed-tools`` is the only
# mechanism that actually keeps the Manager from calling them, so this
# list is the PRIMARY guard for the sole-orchestrator invariant (the
# Manager must NEVER spawn subagents or run shell work directly). The
# system prompt and ``manager-spec.md`` reinforce it but neither is
# enforced by Claude CLI on its own. ``_SUBAGENT_TOOLS`` is shared with
# the worker session policy so both the Manager and workflow-disabled
# workers block the same (old + new) tool names. We also exclude Claude
# CLI's built-in TaskCreate family — see ``_CLAUDE_CLI_BUILTIN_DISALLOW``.
MANAGER_DISALLOWED_TOOLS = ["Bash", *_SUBAGENT_TOOLS, *_CLAUDE_CLI_BUILTIN_DISALLOW]

# Second layer of the sole-orchestrator guarantee: hard-disable Claude Code's
# dynamic-workflow orchestration for the Manager session via env, even if a
# future change were to slip ``ultracode`` / a workflow trigger into the
# Manager path. Combined with MANAGER_DISALLOWED_TOOLS (Task/Agent/Bash), the
# Manager can never spawn sub-agents or run a workflow.
MANAGER_ENV_OVERRIDES = {"CLAUDE_CODE_DISABLE_WORKFLOWS": "1"}


logger = logging.getLogger(__name__)


async def _retry_wait(
    worker: "AgentWorker",
    conversation_id: str,
    context_key: str,
    wait: float,
    note: str,
) -> None:
    """FIX M1(c): sleep out a Manager retry backoff in short slices,
    pinging liveness between slices.

    A plain ``asyncio.sleep(180)`` starves the controller's inactivity
    watchdog (``MANAGER_INACTIVITY_TIMEOUT`` = 300s): at half the
    threshold the status pill flips to a misleading "Manager has been
    silent…" warning, and two back-to-back waits could kill a healthy
    retrying turn outright. Each slice sends an ``api_retry_wait``
    ACTIVITY frame — it refreshes ``_last_activity_ts`` in the
    controller's event dispatcher AND the controller publishes it as a
    ``manager_state('working', <note>)`` pill so the user sees "API busy
    — retrying" instead of a wedge.
    """
    remaining = wait
    while remaining > 0:
        step = min(_RETRY_WAIT_PING_INTERVAL, remaining)
        await asyncio.sleep(step)
        remaining -= step
        if remaining > 0:
            worker._send({
                "type": MessageType.ACTIVITY,
                "conversation_id": conversation_id,
                "context_key": context_key,
                "activity": "api_retry_wait",
                "kind": "pulse",
                "message": note,
            })


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
        planned reset.

        NOT loss-free (SES-07): the fresh turn RE-GROUNDS from the recent chat
        history (last ~20 messages, injected on fresh sessions) + the live board
        summary, but in-session reasoning/decisions that never reached the board
        or a message are not carried forward. A durable distillation handoff
        (write open commitments/constraints to an office file before rotating)
        is tracked as a P8 continuity improvement (BEST-02).
    """
    from src.docker.session_bridge import stream_cli_session

    container_name = agent_config.get("_container_name", "")
    # F5/R2-F9 (audit): central fallback constant. Manager runs Opus
    # in normal operation; fallback only fires when agent_config is
    # malformed. Log so the gap surfaces.
    from src.orchestrator._model_defaults import (
        FALLBACK_MANAGER_MODEL,
        is_opus_tier,
    )
    model = agent_config.get("model") or FALLBACK_MANAGER_MODEL
    if not agent_config.get("model"):
        logger.warning(
            "Manager agent_config missing 'model' — falling back to "
            "%s. Investigate the chat dispatch path.",
            FALLBACK_MANAGER_MODEL,
        )

    # SES-05: pin the Manager's reasoning effort to xhigh explicitly (opus-tier
    # only — defense-in-depth; `--effort` is rejected on non-opus models). Never
    # ultracode — the Manager is hard-blocked from dynamic workflows.
    from src._session_policy import DEFAULT_OPUS_EFFORT
    manager_effort = DEFAULT_OPUS_EFFORT if is_opus_tier(model) else None

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
    # Effective input context for this turn — drives the proactive rotation.
    # SES-01: this is the size of the FINAL API call's context (the resumed
    # transcript + system prompt the model saw last), NOT the result frame's
    # cumulative `usage`. The result frame sums usage across EVERY API call in
    # the run, so a tool-heavy turn (N tool round-trips) multiplies the real
    # transcript size by ~N and rotates the session far too aggressively —
    # degrading a long workstream chat toward stateless. We track the last
    # `assistant` frame's own usage (each frame carries that single call's
    # usage) and fall back to result_usage / num_turns only if none was seen.
    effective_input_tokens = 0
    last_call_input_tokens = 0
    result_cumulative_input_tokens = 0
    result_num_turns = 0

    agent_cwd = "/workspace/agents/manager"

    # ``MANAGER_DISALLOWED_TOOLS`` is hoisted to module scope (see the
    # rationale comment there) so it covers both legacy ``Task`` and the
    # renamed ``Agent`` subagent-spawn tool via the shared
    # ``_SUBAGENT_TOOLS`` tuple. ``CLAUDE_CODE_DISABLE_WORKFLOWS=1`` is the
    # second layer of the sole-orchestrator guarantee: it hard-disables Claude
    # Code's dynamic-workflow orchestration for the Manager session even if a
    # future change were to slip ``ultracode`` / a workflow trigger in. The
    # Manager NEVER orchestrates sub-agents — all work goes through the Board.

    async def _stream_once() -> None:
        """Run ONE Manager CLI attempt. Streams chunks to the user and updates
        the enclosing session/cost/token state. Returns on clean completion;
        raises ``_ManagerRetry`` for a retryable upfront API error (rate
        limit / overload / transient drop, before any user-visible output) or
        ``RuntimeError`` for a fatal one."""
        nonlocal new_session_id, total_cost, effective_input_tokens
        nonlocal last_call_input_tokens, result_cumulative_input_tokens
        nonlocal result_num_turns, manager_effort

        # Token-level streaming state (per attempt):
        # - ``text_blocks_seen`` counts the text content blocks we've already
        #   streamed within this turn. On the 2nd+ block we prepend "\n\n" so
        #   accumulated markdown keeps its structure.
        # - ``current_block_kind`` tracks whether the in-flight block is text
        #   or tool_use, so we ignore input_json_delta frames belonging to a
        #   tool_use argument stream.
        # - ``streamed_visible`` records whether ANY user-visible text reached
        #   the UI this attempt — a mid-stream error can't be silently retried
        #   (it would duplicate output), so retry is gated on this being False.
        # - ``tools_executed`` (SES-02) records whether ANY tool_use block was
        #   emitted this attempt. The CLI executes a tool_use immediately (the
        #   MCP call to the backend lands), so a turn that reached a tool_use
        #   may have ALREADY applied a board mutation. Silently retrying it
        #   (resuming the pre-turn session) would re-issue create_task /
        #   move_task / decide_action_request → duplicate side effects. Retry
        #   is therefore gated on BOTH streamed_visible AND tools_executed
        #   being False.
        text_blocks_seen = 0
        current_block_kind: str | None = None
        streamed_visible = False
        tools_executed = False
        msg_count = 0
        # SES-06: keep the inactivity watchdog alive during a long PURE-THINKING
        # stretch. The controller resets liveness on any IPC frame from the
        # agent, but extended reasoning emits only thinking/input_json deltas
        # (no text_delta, no tool call yet) — so a >300s think would be killed
        # as "stalled" mid-thought. We throttle-send a lightweight ACTIVITY
        # "thinking" frame at most every 15s while such frames flow.
        last_liveness_ping = time.monotonic()
        _LIVENESS_PING_INTERVAL = 15.0
        # Tool-call activity buffer (per attempt) — activity-feed parity
        # with the worker feed (mirrors ``_agent_worker_task``'s
        # ``pending_tools``). Each complete ``tool_use`` block is buffered
        # by its block id and paired with the later ``tool_result`` so the
        # Manager's activity feed carries the same CLI-style command +
        # output rows the workers get, plus a duration. The name-only
        # pulse at ``content_block_start`` below stays — it drives the
        # INSTANT typing indicator (the full input hasn't streamed yet at
        # that point); these enriched frames drive the durable feed
        # (``manager_events`` rows of type tool_start / tool_end).
        pending_tools: dict[str, dict] = {}

        async for msg in stream_cli_session(
            container_name=container_name,
            model=model,
            system_prompt=system_prompt,
            prompt=user_message,
            cwd=agent_cwd,
            mcp_config=mcp_config,
            effort=manager_effort,
            disallowed_tools=MANAGER_DISALLOWED_TOOLS,
            env_overrides=MANAGER_ENV_OVERRIDES,
            resume_session=session_id,
            include_partial_messages=True,
        ):
            msg_count += 1
            logger.info("Manager stream msg #%d: type=%s", msg_count, msg.type)
            if msg.type == "system":
                # SES-03 parity with the worker loop: the CLI's system/init
                # frame carries the session_id at the START of the run.
                # Capturing it here is what makes the M1(b) mid-stream
                # CONTINUATION possible — a mid-stream error arrives before
                # any `result` frame, so without this there is no session
                # id to resume the interrupted transcript from.
                _sid = msg.data.get("session_id")
                if _sid:
                    new_session_id = _sid
            elif msg.type == "result":
                new_session_id = msg.data.get("session_id")
                total_cost = msg.data.get("cost_usd") or msg.data.get("total_cost_usd")
                usage = msg.data.get("usage") or {}
                # SES-01: the result-frame usage is CUMULATIVE across every API
                # call in the run — kept only as a divide-by-num_turns fallback.
                result_cumulative_input_tokens = (
                    (usage.get("input_tokens") or 0)
                    + (usage.get("cache_creation_input_tokens") or 0)
                    + (usage.get("cache_read_input_tokens") or 0)
                )
                result_num_turns = int(msg.data.get("num_turns") or 0)
            elif msg.type == "stream_event":
                # --include-partial-messages emits Anthropic-style
                # incremental frames. We only need three of them:
                #   content_block_start  → note kind + paragraph break
                #   content_block_delta  → text_delta → one chunk
                #   content_block_stop   → clear kind
                event = msg.data.get("event", {})
                event_type = event.get("type", "")

                # SES-06: throttled liveness ping. Any stream_event proves the
                # CLI is alive (thinking or building a tool call). If no visible
                # text is flowing to reset the watchdog naturally, send a light
                # "thinking" ACTIVITY frame at most every 15s.
                _now = time.monotonic()
                if _now - last_liveness_ping >= _LIVENESS_PING_INTERVAL:
                    last_liveness_ping = _now
                    worker._send({
                        "type": MessageType.ACTIVITY,
                        "conversation_id": conversation_id,
                        "context_key": context_key,
                        "activity": "thinking",
                    })

                if event_type == "content_block_start":
                    block = event.get("content_block", {}) or {}
                    current_block_kind = block.get("type")
                    if current_block_kind == "text":
                        # Separate the current text block from the previous
                        # one so markdown lists / headings don't collapse into
                        # a single paragraph (Manager often emits "Here's what
                        # I found:" then a list after a tool call).
                        if text_blocks_seen > 0:
                            worker._send({
                                "type": MessageType.RESPONSE_CHUNK,
                                "conversation_id": conversation_id,
                                "context_key": context_key,
                                "content": "\n\n",
                            })
                        text_blocks_seen += 1
                    elif current_block_kind == "tool_use":
                        # SES-02: a tool_use block was emitted → the CLI is
                        # about to (or did) execute it, so a board mutation may
                        # already have landed. Mark the turn non-silently-
                        # retryable.
                        tools_executed = True
                        # User-visible "Manager is using X" signal.
                        # ``kind: pulse`` marks it typing-indicator-only —
                        # the backend must NOT persist it as a feed row
                        # (the enriched tool_start below carries the feed;
                        # persisting both would double every tool call).
                        # Older backends without the kind branch persist
                        # it as a legacy ``activity`` row — today's
                        # behavior, acceptable degrade.
                        tool_name = block.get("name") or "tool"
                        bare = tool_name.split("__")[-1] if "__" in tool_name else tool_name
                        worker._send({
                            "type": MessageType.ACTIVITY,
                            "conversation_id": conversation_id,
                            "context_key": context_key,
                            "activity": "tool_use",
                            "kind": "pulse",
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
                            streamed_visible = True
                            worker._send({
                                "type": MessageType.RESPONSE_CHUNK,
                                "conversation_id": conversation_id,
                                "context_key": context_key,
                                "content": text,
                            })

                elif event_type == "content_block_stop":
                    current_block_kind = None

            elif msg.type == "assistant":
                # SES-01: each `assistant` frame carries the usage of THAT
                # single API call. The last one before the result is the size
                # of the final call's context — the transcript size we want to
                # bound for rotation (result-frame usage is cumulative).
                _u = (msg.data.get("message", {}) or {}).get("usage") or {}
                _call_input = (
                    (_u.get("input_tokens") or 0)
                    + (_u.get("cache_creation_input_tokens") or 0)
                    + (_u.get("cache_read_input_tokens") or 0)
                )
                if _call_input:
                    last_call_input_tokens = _call_input
                # SES-02 (review P4R-04): the retry gate must see tool
                # executions on the NO-partial-frames fallback path too. There,
                # content_block_start never fires, so a tool_use arriving only
                # inside the complete `assistant` frame left tools_executed
                # False — a retry after that point would replay a turn whose
                # board mutations already landed (the exact double-execute
                # SES-02 closed on the streaming path). Runs unconditionally:
                # on the streaming path the flag is already True (idempotent).
                #
                # Activity-feed parity: the complete frame is also where the
                # FULL tool input is available (content_block_start carries
                # only the name — the input streams as input_json_delta), so
                # this is the enrichment point: buffer {name, input, started}
                # by block id and emit a ``tool_start`` ACTIVITY frame with
                # the same ``build_tool_activity`` details the worker feed
                # uses (redacted command summary). The matching ``user``
                # frame's tool_result closes the pair below.
                for _block in msg.data.get("message", {}).get("content", []):
                    if _block.get("type") != "tool_use":
                        continue
                    tools_executed = True
                    _tool_use_id = _block.get("id") or ""
                    if not _tool_use_id or _tool_use_id in pending_tools:
                        continue
                    _tool_name = _block.get("name") or "tool"
                    _tool_input = _block.get("input") or {}
                    pending_tools[_tool_use_id] = {
                        "name": _tool_name,
                        "input": _tool_input,
                        "started": time.monotonic(),
                    }
                    _activity = build_tool_activity(
                        _tool_name, _tool_input,
                        tool_use_id=_tool_use_id,
                        running=True,
                    )
                    worker._send({
                        "type": MessageType.ACTIVITY,
                        "conversation_id": conversation_id,
                        "context_key": context_key,
                        "activity": "tool_use",
                        "kind": "tool_start",
                        "tool": _activity["details"]["tool"],
                        "tool_use_id": _tool_use_id,
                        "details": _activity["details"],
                    })
                # With --include-partial-messages the full `assistant` message
                # arrives AFTER we've streamed every text_delta — re-emitting
                # would duplicate. Only emit if no text deltas were seen (older
                # CLI builds that drop the partial-messages flag).
                if text_blocks_seen == 0:
                    for block in msg.data.get("message", {}).get("content", []):
                        if block.get("type") == "text" and block.get("text"):
                            streamed_visible = True
                            worker._send({
                                "type": MessageType.RESPONSE_CHUNK,
                                "conversation_id": conversation_id,
                                "context_key": context_key,
                                "content": block["text"],
                            })
            elif msg.type == "user":
                # Tool OUTPUTS arrive as ``user`` frames carrying
                # ``tool_result`` blocks (same CLI stream shape the worker
                # loop consumes). Match each to the buffered tool_use by id
                # and emit the enriched ``tool_end`` ACTIVITY frame —
                # command + redacted output preview + duration — so the
                # Manager feed reaches parity with the worker feed. An
                # unmatched start stays as the record of what was invoked.
                _blocks = msg.data.get("message", {}).get("content", [])
                if isinstance(_blocks, list):
                    for _block in _blocks:
                        if not isinstance(_block, dict):
                            continue
                        if _block.get("type") != "tool_result":
                            continue
                        _tool_use_id = _block.get("tool_use_id") or ""
                        _pending = pending_tools.pop(_tool_use_id, None)
                        if _pending is None:
                            continue
                        _duration_ms = int(
                            (time.monotonic() - _pending["started"]) * 1000
                        )
                        _is_error = bool(_block.get("is_error"))
                        _activity = build_tool_activity(
                            _pending["name"],
                            _pending["input"],
                            result_content=_block.get("content"),
                            is_error=_is_error,
                            tool_use_id=_tool_use_id,
                        )
                        worker._send({
                            "type": MessageType.ACTIVITY,
                            "conversation_id": conversation_id,
                            "context_key": context_key,
                            "activity": "tool_use",
                            "kind": "tool_end",
                            "tool": _activity["details"]["tool"],
                            "tool_use_id": _tool_use_id,
                            "details": _activity["details"],
                            "duration_ms": _duration_ms,
                            "ok": not _is_error,
                        })
            elif msg.type == "error":
                logger.error("Manager stream error: %s", msg.data)
                err = msg.data.get("error", "Unknown error")
                stderr = (msg.data.get("stderr") or "").strip()
                # Fold the CLI stderr into the message so classify_error() sees
                # the REAL cause (the bridge only puts a synthetic "exited with
                # code N" in ``error`` and stashes the diagnostic in stderr).
                err_text = f"{err}\n{stderr}" if stderr else err
                # SES-05 graceful-degrade: an older container CLI that rejects
                # ``--effort`` must NOT hard-fail the Manager. Drop the flag and
                # re-run the turn (no backoff) — but ONLY before any visible
                # output / executed tool, so the replay can't duplicate text or
                # re-apply a board mutation. After dropping, the flag never
                # re-triggers, so this fires at most once.
                if (
                    manager_effort is not None
                    and is_unknown_flag_error(err_text)
                    and not streamed_visible
                    and not tools_executed
                ):
                    logger.warning(
                        "Manager CLI rejected --effort=%s; dropping it and "
                        "retrying the turn without it.", manager_effort,
                    )
                    manager_effort = None
                    raise _ManagerEffortDegrade(err_text)
                remedy = classify_error(err_text)
                # Retry an upfront, retryable API error (rate limit / overload
                # / transient drop) — but ONLY before any visible output, so a
                # retry can't duplicate streamed text.
                if (
                    remedy.retryable
                    and remedy.error_class in _MANAGER_RETRY_CLASSES
                    and not streamed_visible
                    and not tools_executed  # SES-02: don't replay executed tools
                ):
                    raise _ManagerRetry(remedy, err_text)
                # FIX M1(b): MID-STREAM retryable error (visible text or an
                # executed tool). A silent REPLAY is still forbidden
                # (SES-02 — it would duplicate streamed text / re-issue
                # board mutations), but a CONTINUATION of the current
                # attempt's OWN session is safe: the resumed transcript
                # contains the partial turn, and the continuation prompt
                # says "finish, don't redo". Requires the init-frame
                # session id; without one the classified red bubble stays
                # today's behavior.
                if (
                    remedy.retryable
                    and remedy.error_class in _MANAGER_RETRY_CLASSES
                    and new_session_id
                ):
                    raise _ManagerContinuation(remedy, err_text, new_session_id)
                raise RuntimeError(err_text)

        # SES-01: prefer the FINAL call's context size; fall back to the
        # cumulative-usage / num_turns approximation only when no per-call
        # usage was seen (older CLI builds without partial-message usage).
        if last_call_input_tokens:
            effective_input_tokens = last_call_input_tokens
        elif result_num_turns > 1:
            effective_input_tokens = result_cumulative_input_tokens // result_num_turns
        else:
            effective_input_tokens = result_cumulative_input_tokens

        # ``input_tokens`` is logged for observability — confirms the lean
        # board/task projections + native auto-compact keep a long session
        # bounded.
        logger.info(
            "Manager stream ended: %d messages, session=%s, cost=%s, "
            "final_call_input_tokens=%d (cumulative=%d over %d turns)",
            msg_count, new_session_id, total_cost, effective_input_tokens,
            result_cumulative_input_tokens, result_num_turns,
        )

    # Retry loop (#2 — rate-limit resilience for the Manager, which previously
    # had NONE). A retryable upfront API error waits the classifier backoff
    # (~60s for a 429) and re-runs the SAME turn (resuming the session). The
    # work is fine; the API was just busy. Capped at _MANAGER_MAX_ATTEMPTS,
    # after which the error surfaces to the user as before.
    attempt = 0
    # FIX M1(b): the mid-stream continuation is a ONE-SHOT per turn —
    # a second mid-stream failure surfaces as the classified red bubble.
    continuation_used = False
    while True:
        try:
            await _stream_once()
            break
        except _ManagerEffortDegrade:
            # SES-05: immediate, backoff-free re-run that does NOT consume an
            # API-retry attempt. ``manager_effort`` is now None, so the
            # unknown-flag error can't recur — this fires at most once.
            continue
        except _ManagerRetry as retry:
            attempt += 1
            if attempt >= _MANAGER_MAX_ATTEMPTS:
                raise RuntimeError(retry.err_text) from None
            wait = min(retry.remedy.backoff_seconds or 60.0, 180.0)
            logger.warning(
                "Manager turn hit %s (attempt %d/%d) — waiting %.0fs then "
                "retrying the same turn.",
                retry.remedy.error_class.value, attempt,
                _MANAGER_MAX_ATTEMPTS, wait,
            )
            worker._send({
                "type": MessageType.RESPONSE_CHUNK,
                "conversation_id": conversation_id,
                "context_key": context_key,
                "content": (
                    f"\n\n_(The API is busy — waiting ~{int(wait)}s and "
                    "retrying…)_\n\n"
                ),
            })
            # FIX M1(c): sliced wait + liveness pings so the inactivity
            # watchdog/status pill can't misread the backoff as a wedge.
            await _retry_wait(
                worker, conversation_id, context_key, wait,
                "API busy — retrying shortly…",
            )
        except _ManagerContinuation as cont:
            if continuation_used:
                raise RuntimeError(cont.err_text) from None
            continuation_used = True
            wait = min(cont.remedy.backoff_seconds or 60.0, 180.0)
            logger.warning(
                "Manager turn interrupted MID-STREAM by %s — one "
                "continuation retry: resuming session %s after %.0fs.",
                cont.remedy.error_class.value, cont.resume_id[:12], wait,
            )
            worker._send({
                "type": MessageType.RESPONSE_CHUNK,
                "conversation_id": conversation_id,
                "context_key": context_key,
                "content": (
                    f"\n\n_(A transient API error interrupted this reply "
                    f"— resuming in ~{int(wait)}s…)_\n\n"
                ),
            })
            # Redirect the NEXT attempt at the interrupted transcript:
            # resume THIS attempt's session with the fixed continuation
            # prompt. ``_stream_once`` reads both via closure.
            session_id = cont.resume_id
            user_message = _MANAGER_CONTINUATION_PROMPT
            await _retry_wait(
                worker, conversation_id, context_key, wait,
                "API busy — resuming the interrupted reply…",
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
        # FX-24.T03: the user-facing rotation notice is NO LONGER appended as
        # inline prose to the Manager's message (it read like the Manager
        # talking and emitted no distinct event). The ``rotate_session`` flag
        # rides RESPONSE_FINAL to the controller, which both clears the session
        # AND publishes a typed, transient ``manager_session_rotated`` frame the
        # UI renders as a clean "fresh session" chip
        # (see ``_manager_events.on_response_final``).

    return new_session_id, total_cost, rotate_session
