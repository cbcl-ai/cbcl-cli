"""Live agent-channel emission for Flow Studio consults (V2-P2).

The streaming OVERLAY on top of the durable design_log / REST-poll pair
(spec ``docs/specs/flow-studio/spec-v2-surfaces.md`` §4): while a Flow
Architect / Data Curator consult session streams CLI NDJSON, the daemon
forwards the exchange over the connector WS as ``agent_channel`` events
so the Studio rails feel like Manager chat. The backend relays the frame
verbatim to the office's chat-WS clients and NEVER persists it — replay
comes from the design_log / poll status, so a dropped frame loses
nothing durable.

Event shape (one family, ``kind``-discriminated)::

    {
      "type": "agent_channel",
      "channel": "flow-design:<flow_id>" | "collections-curate",
      "request_id": "<consult request uuid>",
      "kind": "chunk" | "final" | "tool_start" | "tool_end" | "state",
      # kind=chunk|final: "text"
      # kind=tool_start|tool_end: "tool_use_id", "details"
      #   (the EXISTING build_tool_activity details shape — Manager-feed
      #    parity), plus "duration_ms"/"ok" on tool_end
      # kind=state: "state" ("working"|"done"|"failed"), "message"
    }

Hard rules pinned by ``tests/test_agent_channel_stream.py``:

* **Best-effort, always.** Every public method swallows every
  exception — a relay failure must never affect the consult itself or
  the durable ``flow_consult_*`` path.
* **Chunks coalesce to ≤10 publishes/s per channel**
  (``CHUNK_COALESCE_INTERVAL_SECONDS``): text deltas arriving inside
  the window are buffered and flushed as ONE joined chunk.
* **Transcript order holds**: buffered chunk text is flushed BEFORE any
  tool/state/final frame on the same channel.
* **Every death path ends in ``state: failed``** (wired in
  ``handlers.py``) so the FE typing indicator can never hang while the
  daemon is alive. A daemon crash emits nothing — the poll TTL is the
  durable backstop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

logger = logging.getLogger("cbcl.agent_channel")

# ≤10 chunk publishes per second per channel.
CHUNK_COALESCE_INTERVAL_SECONDS = 0.1

# Defensive caps — chunk text arrives ≤500 chars per checkpoint frame and
# the final text is already capped at 4000 by the worker's summary cap
# (``_agent_worker_task.py`` — parity with flow_consult_complete.summary);
# these bound pathological buffers, not normal traffic.
_MAX_CHUNK_TEXT = 8_000
_MAX_FINAL_TEXT = 4_000
_MAX_STATE_MESSAGE = 500

_CHANNEL_COLLECTIONS_CURATE = "collections-curate"
_CHANNEL_FLOW_DESIGN_PREFIX = "flow-design:"


def channel_key(kind: str, flow_id: str = "") -> str:
    """Map a consult marker's ``kind`` (+ ``flow_id``) to its channel key.

    ``flow_design`` without a flow_id has no addressable surface — the
    empty string tells the emitter to skip (best-effort overlay).
    """
    if kind == "collections_curate":
        return _CHANNEL_COLLECTIONS_CURATE
    if kind == "flow_design" and flow_id:
        return f"{_CHANNEL_FLOW_DESIGN_PREFIX}{flow_id}"
    return ""


class AgentChannelEmitter:
    """Per-office ``agent_channel`` publisher with chunk coalescing.

    ``publish`` is the connector-WS publish coroutine
    (``WsTransport.publish_event``-shaped). One instance per office —
    the ``collections-curate`` key is only office-unique, so a shared
    emitter across offices would cross-wire coalescing buffers.
    """

    def __init__(
        self, publish: Callable[[dict], Awaitable[None]]
    ) -> None:
        self._publish = publish
        # channel -> {"buf": [str], "rid": str, "last": float,
        #             "timer": asyncio.Task | None}
        self._pending: dict[str, dict] = {}

    # -- marker plumbing ------------------------------------------------

    @staticmethod
    def _channel_for(marker: dict) -> tuple[str, str]:
        """(channel, request_id) from a consult marker; ("", "") = skip."""
        if not isinstance(marker, dict):
            return "", ""
        rid = str(marker.get("request_id") or "")
        channel = channel_key(
            str(marker.get("kind") or ""),
            str(marker.get("flow_id") or ""),
        )
        return (channel, rid) if channel and rid else ("", "")

    # -- public relay surface (all best-effort) -------------------------

    async def relay_started(self, marker: dict, message: str) -> None:
        """``state: working`` — the typing indicator turns on."""
        try:
            channel, rid = self._channel_for(marker)
            if not channel:
                return
            await self._emit_state(channel, rid, "working", message)
        except Exception:
            logger.debug("agent_channel relay_started failed", exc_info=True)

    async def relay_progress(self, marker: dict, event: dict) -> None:
        """Map ONE worker PROGRESS frame onto the channel.

        * ``checkpoint`` (main session) → coalesced ``chunk``. Sidechain
          narration is skipped — subagent text is not the transcript.
        * ``tool_run`` → ``tool_start`` / ``tool_end`` carrying the
          existing ``build_tool_activity`` details verbatim (start =
          ``running`` rows; end = rows carrying output/duration/error;
          lean unpaired rows ride as a start). Buffered chunk text is
          flushed first so transcript order holds.
        * anything else (sidechain error rows, …) → skipped; terminal
          failure surfaces via ``state: failed`` on the death paths.
        """
        try:
            channel, rid = self._channel_for(marker)
            if not channel or not isinstance(event, dict):
                return
            etype = str(event.get("event_type") or "")
            details = event.get("details")
            if not isinstance(details, dict):
                details = {}
            if etype == "checkpoint":
                if details.get("sidechain"):
                    return
                text = str(event.get("content") or "").strip()
                if text:
                    await self._chunk(channel, rid, text)
            elif etype == "tool_run":
                if not details:
                    return
                await self._flush(channel)
                frame: dict = {
                    "type": "agent_channel",
                    "channel": channel,
                    "request_id": rid,
                    "tool_use_id": str(details.get("tool_use_id") or ""),
                    "details": details,
                }
                is_end = not details.get("running") and (
                    "output_preview" in details
                    or "duration_ms" in details
                    or details.get("is_error")
                )
                if is_end:
                    frame["kind"] = "tool_end"
                    if details.get("duration_ms") is not None:
                        frame["duration_ms"] = int(details["duration_ms"])
                    frame["ok"] = not bool(details.get("is_error"))
                else:
                    # start rows AND lean unpaired rows (cubicle-internal
                    # tools emit name-only, no pair).
                    frame["kind"] = "tool_start"
                await self._send(frame)
        except Exception:
            logger.debug("agent_channel relay_progress failed", exc_info=True)

    async def relay_final(self, marker: dict, text: str) -> None:
        """Terminal success: ``final`` (the finished message) then
        ``state: done``."""
        try:
            channel, rid = self._channel_for(marker)
            if not channel:
                return
            await self._flush(channel)
            await self._send({
                "type": "agent_channel",
                "channel": channel,
                "request_id": rid,
                "kind": "final",
                "text": str(text or "")[:_MAX_FINAL_TEXT],
            })
            await self._emit_state(channel, rid, "done", "")
        except Exception:
            logger.debug("agent_channel relay_final failed", exc_info=True)

    async def relay_failed(self, marker: dict, message: str) -> None:
        """Terminal failure: ``state: failed``. Wired on EVERY death
        path (spawn refusals, error-classed completions, worker errors,
        supervisor-synthesized fatals) so the typing indicator can never
        hang while the daemon lives."""
        try:
            channel, rid = self._channel_for(marker)
            if not channel:
                return
            await self._emit_state(
                channel, rid, "failed",
                str(message or "").strip() or "the consult session failed",
            )
        except Exception:
            logger.debug("agent_channel relay_failed failed", exc_info=True)

    async def drain(self) -> None:
        """Flush every buffered chunk now (test/shutdown helper)."""
        try:
            for channel in list(self._pending):
                await self._flush(channel)
        except Exception:
            logger.debug("agent_channel drain failed", exc_info=True)

    # -- internals -------------------------------------------------------

    async def _emit_state(
        self, channel: str, rid: str, state: str, message: str
    ) -> None:
        await self._flush(channel)
        await self._send({
            "type": "agent_channel",
            "channel": channel,
            "request_id": rid,
            "kind": "state",
            "state": state,
            "message": str(message or "")[:_MAX_STATE_MESSAGE],
        })

    async def _chunk(self, channel: str, rid: str, text: str) -> None:
        """Coalescing chunk write: publish immediately when the channel
        is outside its rate window, otherwise buffer + schedule ONE
        delayed flush at the window edge."""
        state = self._pending.setdefault(
            channel, {"buf": [], "rid": rid, "last": 0.0, "timer": None},
        )
        state["rid"] = rid
        if sum(len(part) for part in state["buf"]) < _MAX_CHUNK_TEXT:
            state["buf"].append(text)
        now = time.monotonic()
        elapsed = now - float(state["last"] or 0.0)
        if elapsed >= CHUNK_COALESCE_INTERVAL_SECONDS:
            await self._flush(channel)
            return
        if state["timer"] is None or state["timer"].done():
            delay = CHUNK_COALESCE_INTERVAL_SECONDS - elapsed
            state["timer"] = asyncio.create_task(
                self._delayed_flush(channel, delay),
            )

    async def _delayed_flush(self, channel: str, delay: float) -> None:
        try:
            await asyncio.sleep(max(delay, 0.0))
            await self._flush(channel)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug(
                "agent_channel delayed flush failed", exc_info=True,
            )

    async def _flush(self, channel: str) -> None:
        state = self._pending.get(channel)
        if state is None:
            return
        timer = state.get("timer")
        if timer is not None and not timer.done():
            timer.cancel()
        state["timer"] = None
        if not state["buf"]:
            return
        text = "\n\n".join(state["buf"])[:_MAX_CHUNK_TEXT]
        state["buf"] = []
        state["last"] = time.monotonic()
        await self._send({
            "type": "agent_channel",
            "channel": channel,
            "request_id": str(state.get("rid") or ""),
            "kind": "chunk",
            "text": text,
        })

    async def _send(self, frame: dict) -> None:
        """One publish, best-effort."""
        try:
            await self._publish(frame)
        except Exception:
            logger.debug(
                "agent_channel publish failed (non-fatal): %s",
                frame.get("kind"), exc_info=True,
            )
