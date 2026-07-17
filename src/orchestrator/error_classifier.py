"""Error classifier — translates raw Claude CLI errors into actionable classes.

The Claude CLI streams errors as ``{type: "error", error: "..."}`` or the
process exits with a stderr message. Until now agent_worker just raised
RuntimeError on any error, which killed the session without any recovery
guidance. This module parses the error text, assigns it to a known class,
and produces a structured Remedy the caller can use to retry intelligently
or escalate to the Manager Assistant.

Design goals:
- Pattern matching is case-insensitive and tolerant of wrapping/prefixes
  ("API Error: ...", "Error: ...", leading whitespace, etc.).
- Every remedy is a pure data object — no side effects inside the
  classifier. The caller decides whether to act on it.
- Unknown errors are always safely classified as UNKNOWN_FATAL with a
  ``retryable=False`` flag so we never infinite-loop on an unfamiliar class.
- Each class carries a human-readable ``guidance`` string that we can
  embed directly in the retry prompt or escalation activity.

Public API:
- ``ErrorClass`` enum
- ``Remedy`` dataclass
- ``classify_error(text: str) -> Remedy``
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum


class ErrorClass(str, Enum):
    """Known error categories from the Claude CLI / SDK.

    Values are stable strings so they can be serialised into activity
    details and reports without coupling to Python identifiers.
    """

    OUTPUT_TOKEN_LIMIT = "output_token_limit"
    CONTEXT_TOO_LARGE = "context_too_large"
    RATE_LIMITED = "rate_limited"
    # Anthropic API HTTP 529 / "overloaded_error" / 503 — the model
    # endpoint is temporarily overloaded. Distinct from RATE_LIMITED
    # (429, which is per-account throttling) and TIMEOUT (504, our own
    # wait). The remediation is the same as the user-reported one:
    # back off ~3 minutes and retry. Multiple retries usually clear
    # within ~9 minutes.
    API_OVERLOADED = "api_overloaded"
    # Claude subscription/account USAGE limit — the rolling 5-hour window
    # or the weekly cap (distinct from RATE_LIMITED, which is the
    # per-minute 429 throttle that clears in seconds). A usage limit does
    # NOT clear with a short backoff — it stays exhausted until the window
    # RESETS at a specific time that the CLI/API reports ("Claude usage
    # limit reached. Your limit will reset at 11pm", a 5-hour-limit
    # message, or an ISO/epoch reset timestamp). The remedy carries
    # ``reset_at`` so the daemon can DEFER the work and auto-resume it
    # exactly when the window reopens, rather than burning retries on a
    # guaranteed-failing call. Must be matched ABOVE RATE_LIMITED.
    USAGE_LIMIT_EXCEEDED = "usage_limit_exceeded"
    TIMEOUT = "timeout"
    TOOL_UNAVAILABLE = "tool_unavailable"
    AUTH_FAILED = "auth_failed"
    # The CLI subprocess was killed by the OS (SIGKILL/SIGTERM).
    # Typically caused by the OOM killer when the Claude CLI's memory
    # footprint exceeds the container limit while streaming a very
    # large response. Retry with a chunked approach so the resident
    # set at any moment stays small.
    PROCESS_KILLED = "process_killed"
    # The CLI was asked to ``--resume`` a session_id that no longer
    # exists in the container ("No conversation found with session ID
    # ..."). Happens after a container recreate / CLI upgrade wipes the
    # in-container conversation store while our persisted session_id
    # file survives. The fix is to drop the stale id and start a FRESH
    # session — retryable with reset_session=True. Without this class the
    # error fell through to UNKNOWN_FATAL (non-retryable), wedging a
    # Manager workstream chat into a permanent "An error occurred" loop
    # until ``cbcl stop/start`` (ADD-E1).
    SESSION_NOT_FOUND = "session_not_found"
    # A transient transport drop between the daemon and the CLI / MCP /
    # backend: the socket closed mid-stream, the connection reset, or the
    # CLI exited 1 immediately after a connection-drop marker. NOT a logic
    # blocker and NOT an OOM kill — the work itself is fine, the pipe just
    # broke. Before this class such drops ("socket connection closed",
    # "Connection reset by peer", a bare "exited with code 1" next to a
    # connection marker) fell through to UNKNOWN_FATAL (non-retryable) and
    # escalated a perfectly recoverable task to ``blocked``. Retryable by
    # RESUMING the same session so the agent continues from where the drop
    # interrupted it (no redone work).
    CONNECTION_LOST = "connection_lost"
    UNKNOWN_FATAL = "unknown_fatal"


# Transient PROVIDER/TRANSPORT outages — the work itself is fine, the
# infrastructure was busy or the pipe broke. Shared by the callers that
# grant these classes extra patience beyond the plain retry budget: the
# worker session's deferred-resume ladder (``_agent_worker_task``) and
# the planner-consult one-shot infra re-fire (``handlers``). Deliberately
# EXCLUDES ``USAGE_LIMIT_EXCEEDED`` (it has its own reset_at-timed defer),
# ``PROCESS_KILLED`` (an OOM recurs deterministically — waiting doesn't
# fix a memory footprint), and the fresh-session classes (context/session
# problems, not provider outages).
INFRA_OUTAGE_CLASSES: frozenset[ErrorClass] = frozenset({
    ErrorClass.API_OVERLOADED,
    ErrorClass.RATE_LIMITED,
    ErrorClass.TIMEOUT,
    ErrorClass.CONNECTION_LOST,
})


@dataclass(frozen=True)
class Remedy:
    """What the caller should do about an error.

    Attributes:
        error_class: Category of the error (enum value).
        retryable: Whether an automatic retry is likely to succeed.
        guidance: A sentence appended to the retry prompt so the agent
            knows what to avoid next time.
        env_overrides: Extra environment variables to set on the retry
            subprocess (e.g. {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000"}).
        reset_session: True if the retry should start a fresh session
            (new ``session_id``) instead of resuming. Useful when the
            context itself is the problem.
        backoff_seconds: Seconds to wait before the retry.
        escalation_message: Human-readable summary for the MA/Manager
            activity when retries are exhausted.
    """

    error_class: ErrorClass
    retryable: bool
    guidance: str
    env_overrides: dict[str, str] = field(default_factory=dict)
    reset_session: bool = False
    backoff_seconds: float = 0.0
    escalation_message: str = ""
    # For USAGE_LIMIT_EXCEEDED: the wall-clock time the usage window
    # reopens (parsed from the error text when available). When set, the
    # caller should DEFER the work until this time instead of inline-
    # sleeping ``backoff_seconds`` — a 5-hour reset can't be a blocking
    # sleep. ``None`` for every other class (and for usage-limit errors
    # whose text carried no parseable reset time — those fall back to a
    # conservative fixed defer).
    reset_at: datetime | None = None


# ── Pattern table ──────────────────────────────────────────────────────
#
# Each entry is (ErrorClass, compiled regex). The first matching entry
# wins. Order matters — put more specific patterns above broader ones.
# Patterns are matched against the full error text lowercased.

_PATTERNS: list[tuple[ErrorClass, re.Pattern[str]]] = [
    # Process-kill signals. Exit codes we classify:
    #   137 = 128 + SIGKILL(9) — almost always the OOM killer.
    #   143 = 128 + SIGTERM(15) — container shutdown / Docker cgroup.
    #   139 = 128 + SIGSEGV(11) — segfault (native crash in CLI/ffi).
    #   134 = 128 + SIGABRT(6)  — assertion / abort() trap.
    # We match the synthetic "Claude CLI exited with code N" string
    # the session bridge emits AND the bare stderr markers the
    # kernel/Docker can leave behind ("OOMKilled", "out of memory",
    # "SIGKILL", "Segmentation fault", "Aborted", bare "killed").
    # Must come BEFORE the output-token pattern because an OOM during
    # a large response looks lexically similar.
    (
        ErrorClass.PROCESS_KILLED,
        re.compile(
            r"exited\s+with\s+code\s+(?:137|143|139|134)\b"
            r"|\bOOMKilled\b"
            r"|\bout\s+of\s+memory\b"
            r"|\bsigkill\b"
            r"|\bsigsegv\b|\bsegmentation\s+fault\b"
            r"|\bsigabrt\b|\baborted\b(?!\s+by\s+user)"
            r"|\bkilled\b(?!\s+by\s+user)",
            re.IGNORECASE,
        ),
    ),
    # Transient transport drops (daemon ↔ CLI ↔ MCP ↔ backend). The socket
    # closed mid-stream, the peer reset, the pipe broke, or the CLI exited
    # 1 right after a connection-drop marker. MUST come before the broad
    # patterns AND before AUTH (a reset isn't an auth failure). We do NOT
    # match a bare "exited with code 1" on its own — that's ambiguous and
    # could be a real fatal — only when a connection marker is present.
    (
        ErrorClass.CONNECTION_LOST,
        re.compile(
            r"socket\s+connection\s+closed"
            r"|connection\s+(?:reset|closed|aborted|refused)"
            r"|reset\s+by\s+peer"
            r"|broken\s+pipe"
            r"|\bECONN(?:RESET|REFUSED|ABORTED)\b"
            r"|\bEPIPE\b"
            r"|socket\s+hang\s*up"
            r"|server\s+disconnected"
            r"|peer\s+closed\s+(?:the\s+)?connection"
            r"|remote\s+end\s+closed\s+connection"
            r"|connection\s+to\s+the\s+\w+\s+was\s+(?:lost|closed)"
            r"|transport\s+(?:closed|error)"
            r"|client(?:_| )?disconnected",
            re.IGNORECASE,
        ),
    ),
    # "API Error: Claude's response exceeded the 32000 output token maximum."
    # Also covers "output token limit", "max_tokens", "exceeded N tokens" (output side).
    (
        ErrorClass.OUTPUT_TOKEN_LIMIT,
        re.compile(
            r"exceeded.{0,40}output\s+token|output\s+token.{0,20}(maximum|limit|exceeded)"
            r"|max_tokens.{0,30}exceed|claude_code_max_output_tokens",
            re.IGNORECASE,
        ),
    ),
    # Input/context window size errors. Distinct from output-token.
    (
        ErrorClass.CONTEXT_TOO_LARGE,
        re.compile(
            # "prompt is too long" / "prompt too long" — the Claude CLI's
            # actual phrasing is "prompt is too long: N tokens > M maximum"
            # (note the "is"), which the old "prompt\s+too\s+long" missed,
            # so an oversized resumed session classified as UNKNOWN_FATAL.
            r"prompt\s+(?:is\s+)?too\s+long"
            r"|context\s+window"
            r"|input\s+token.{0,30}(exceed|limit|maximum)"
            r"|too\s+many\s+input\s+tokens"
            # "N tokens > M maximum" — the numeric tail the CLI prints.
            r"|\d[\d,]*\s+tokens?\s*>\s*\d",
            re.IGNORECASE,
        ),
    ),
    # Claude subscription USAGE / SESSION limit (the rolling 5-hour window
    # or weekly cap). MUST come ABOVE RATE_LIMITED — these messages also
    # contain the word "limit", but they are NOT a transient per-minute
    # 429; they stay exhausted until a specific reset time. Matches the
    # Claude Code phrasings ("Claude usage limit reached", "5-hour limit
    # reached", "weekly limit", "your limit will reset at …") without
    # matching the generic "rate limit" (left to RATE_LIMITED below).
    (
        ErrorClass.USAGE_LIMIT_EXCEEDED,
        re.compile(
            # Anchored to usage/limit context only — every Claude usage-limit
            # phrasing carries one of these. SES-10: the "limit resets"
            # alternative excludes a nearby "second(s)" token via negative
            # lookahead so a benign "…limit resets in 30 seconds" 429 is NOT
            # hijacked into a multi-hour usage-cap DEFER — it falls through to
            # RATE_LIMITED (a ~60s backoff). A usage cap resets in hours/at a
            # clock time, never seconds. The reset TIME is still extracted by
            # _parse_reset_time once the class is matched.
            r"usage\s+limit"
            r"|\b\d+\s*-?\s*hour\s+limit"
            r"|weekly\s+limit"
            r"|limit\s+(?:will\s+)?resets?\b(?!.{0,20}second)",
            re.IGNORECASE,
        ),
    ),
    # HTTP 429 / rate-limit messages.
    (
        ErrorClass.RATE_LIMITED,
        re.compile(
            r"\b429\b|rate[\s_-]?limit|too\s+many\s+requests|quota\s+exceeded",
            re.IGNORECASE,
        ),
    ),
    # HTTP 529 / Anthropic "overloaded_error" / generic 503. The API
    # is temporarily oversubscribed; back off and retry. Distinct
    # from RATE_LIMITED — that's per-account; this is provider-wide
    # and usually resolves within a few minutes.
    #
    # The 503 token guards against matching "503-bad-other-thing"
    # by requiring word boundaries, AND we explicitly exclude the
    # "504" case (already covered by TIMEOUT) by placing this above
    # the TIMEOUT pattern.
    (
        ErrorClass.API_OVERLOADED,
        re.compile(
            r"\b529\b"
            r"|overloaded(?:_error)?"
            r"|api[\s_-]+(?:is\s+)?(?:temporarily\s+)?overload"
            r"|\b503\b(?!\d)"
            r"|service\s+(?:is\s+)?temporarily\s+unavailable",
            re.IGNORECASE,
        ),
    ),
    # Timeouts from the API or our own wait_for.
    (
        ErrorClass.TIMEOUT,
        re.compile(
            r"\btimed?\s*out\b|timeout|\bdeadline\b|504\s+gateway",
            re.IGNORECASE,
        ),
    ),
    # Stale --resume target. The CLI emits "No conversation found with
    # session ID <id>" when the persisted session_id no longer maps to a
    # live conversation. Place above AUTH/TOOL so a "not found" here is
    # classified as a recoverable fresh-session retry, not an auth/tool
    # fatal.
    (
        ErrorClass.SESSION_NOT_FOUND,
        re.compile(
            r"no\s+conversation\s+found"
            r"|no\s+such\s+session"
            r"|session\s+(?:id\s+)?\S*\s*(?:not\s+found|does\s+not\s+exist"
            r"|is\s+invalid|has\s+expired|expired)"
            r"|could\s+not\s+(?:find|resume)\s+(?:the\s+)?(?:conversation|session)"
            r"|invalid\s+session\s+id",
            re.IGNORECASE,
        ),
    ),
    # Tool / MCP invocation errors that mean the prompt references a
    # tool the CLI doesn't know about. NOT the generic "Unknown tool"
    # we return from the MCP server — that's caught earlier in our code.
    (
        ErrorClass.TOOL_UNAVAILABLE,
        re.compile(
            r"tool\s+not\s+found|unknown\s+tool(?!\s*:?\s*mcp__cubicle)"
            r"|no\s+such\s+tool|mcp\s+server.*(?:not\s+found|failed\s+to\s+start)",
            re.IGNORECASE,
        ),
    ),
    # 401/403 auth problems — agent credential rotation needed.
    (
        ErrorClass.AUTH_FAILED,
        re.compile(
            r"\b(401|403)\b|unauthori[sz]ed|authentication.{0,30}fail"
            r"|invalid\s+api\s+key|credential(?:s)?\s+(?:expired|invalid)",
            re.IGNORECASE,
        ),
    ),
]


_MAX_OUTPUT_TOKENS_BUMP = "64000"
"""Bumped value for CLAUDE_CODE_MAX_OUTPUT_TOKENS on retry.

Default CLI limit is 32000. Claude Sonnet/Opus support larger outputs
(64000 is a conservative safe ceiling for the current Sonnet 4.6 and
Opus 4.7 family). If a task's output truly exceeds 64K the right move
is splitting the task, not bumping further.
"""


_EPOCH_RE = re.compile(r"\b(1[0-9]{9})\b")  # 10-digit unix ts (2001-2033)
_ISO_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)\b")
_CLOCK_RE = re.compile(
    r"reset[s]?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?",
    re.IGNORECASE,
)
_RELATIVE_RE = re.compile(
    r"reset[s]?\s+in\s+(\d+)\s*(hour|hr|minute|min)",
    re.IGNORECASE,
)


def _parse_reset_time(text: str) -> datetime | None:
    """Best-effort parse of a usage-limit reset time from CLI error text.

    Handles the shapes Claude / Claude Code surface: a Unix epoch (the
    ``…limit reached|<epoch>`` form), an ISO-8601 timestamp, a relative
    ``resets in N hours``, and a bare clock time ``reset at 11pm``. Returns
    a timezone-aware UTC datetime in the FUTURE, or ``None`` when nothing
    parseable is present (the caller then uses a conservative fixed defer)."""
    now = datetime.now(timezone.utc)

    m = _EPOCH_RE.search(text)
    if m:
        try:
            ts = datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
            if ts > now:
                return ts
        except (ValueError, OSError, OverflowError):
            pass

    m = _ISO_RE.search(text)
    if m:
        raw = m.group(1).replace(" ", "T")
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                ts = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
                if ts > now:
                    return ts
            except ValueError:
                continue

    m = _RELATIVE_RE.search(text)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        delta = (
            timedelta(hours=n)
            if unit.startswith(("hour", "hr"))
            else timedelta(minutes=n)
        )
        return now + delta

    m = _CLOCK_RE.search(text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = (m.group(3) or "").lower().replace(".", "")
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            # Best-effort: assume the next occurrence of that clock time in
            # UTC (the message rarely carries a reliable tz). The scheduler
            # re-checks, so an over-estimate just means one extra probe.
            candidate = now.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if candidate <= now:
                candidate += timedelta(days=1)
            return candidate

    return None


def classify_error(text: str | None) -> Remedy:
    """Classify a CLI/SDK error string into an actionable Remedy.

    Args:
        text: The error message as received from the CLI stream or an
            exception ``str(exc)``. May be ``None`` or empty; those are
            treated as UNKNOWN_FATAL so the caller doesn't silently
            retry an error it can't identify.

    Returns:
        A ``Remedy`` instance. Never raises.
    """
    if not text or not isinstance(text, str):
        return _unknown_fatal("empty or non-string error")

    stripped = text.strip()
    if not stripped:
        return _unknown_fatal("empty error string")

    for cls, pattern in _PATTERNS:
        if pattern.search(stripped):
            return _remedy_for(cls, stripped)

    return _unknown_fatal(stripped)


# ── Per-class remedy builders ──────────────────────────────────────────


def _remedy_for(cls: ErrorClass, text: str) -> Remedy:
    if cls is ErrorClass.OUTPUT_TOKEN_LIMIT:
        return Remedy(
            error_class=cls,
            retryable=True,
            guidance=(
                "Your previous attempt exceeded the output-token limit "
                "(the model tried to emit more text in one assistant turn "
                "than is allowed). The session has been resumed, so you "
                "retain conversation context. Follow the LARGE DELIVERABLE "
                "PROTOCOL in your system prompt — specifically:\n"
                "0. FIRST — `Glob` your task's output directory "
                "   (your prompt names it; under per-workstream layout it "
                "   is `/workspace/outputs/{workstream}/[{scope}/]`) for "
                "   files matching your task's readable_id (also look for "
                "   a `*_CHECKPOINT.md` index file). Also glob the legacy "
                "   flat `/workspace/outputs/` root in case prior runs "
                "   pre-dated per-workstream separation. `Read` the "
                "   checkpoint file if present; it tells you which "
                "   chunks are `done` and which are `pending`. Resume "
                "   from the first `pending` chunk. Do NOT redo "
                "   completed chunks.\n"
                "1. Write each remaining chunk to disk via `Write` the "
                "   moment it's drafted — never accumulate multiple "
                "   chunks in conversation.\n"
                "2. Update the CHECKPOINT.md file after every chunk so "
                "   that if this attempt also gets interrupted, the next "
                "   one can resume.\n"
                "3. Keep each assistant message short — a brief plan or a "
                "   one-line status. Tool calls do the heavy lifting.\n"
                "4. When all chunks are on disk and acceptance criteria "
                "   pass, submit via `update_status('review')`. Do not "
                "   restate large outputs inline."
            ),
            env_overrides={
                "CLAUDE_CODE_MAX_OUTPUT_TOKENS": _MAX_OUTPUT_TOKENS_BUMP,
            },
            # Resume the same session — the agent already has context; we
            # just need it to finish without another giant turn.
            reset_session=False,
            backoff_seconds=2.0,
            escalation_message=(
                "Agent repeatedly hit the output-token limit. Consider "
                "splitting this task into smaller deliverables or lowering "
                "the scope so each output fits in one model response."
            ),
        )

    if cls is ErrorClass.CONTEXT_TOO_LARGE:
        return Remedy(
            error_class=cls,
            retryable=True,
            guidance=(
                "Your previous attempt sent too much input context to the "
                "model. On this retry, keep responses focused on the Brief "
                "only; do NOT read additional files beyond those in Inputs; "
                "avoid re-reading already-processed files."
            ),
            env_overrides={},
            # Fresh session — the accumulated context IS the problem.
            reset_session=True,
            backoff_seconds=2.0,
            escalation_message=(
                "Agent's context window filled up before the task could "
                "finish. Consider splitting the task or trimming Inputs."
            ),
        )

    if cls is ErrorClass.RATE_LIMITED:
        return Remedy(
            error_class=cls,
            retryable=True,
            guidance=(
                "The API briefly rate-limited the previous attempt. "
                "Waiting ~1 minute and retrying — no change in approach."
            ),
            env_overrides={},
            reset_session=False,
            # ~60s: a per-minute 429 bucket clears within a minute, and the
            # CLI/stream-json doesn't expose the `retry-after` header, so a
            # fixed minute is the safe resume interval (the work itself is
            # fine — we just wait out the throttle, then resume the SAME
            # session). Applies to every agent INCLUDING the Manager.
            backoff_seconds=60.0,
            escalation_message=(
                "Agent hit the API rate limit multiple times. Check the "
                "Anthropic plan tier or reduce parallel agent activity."
            ),
        )

    if cls is ErrorClass.USAGE_LIMIT_EXCEEDED:
        reset_at = _parse_reset_time(text)
        when = (
            reset_at.astimezone(timezone.utc).strftime("%H:%M UTC")
            if reset_at is not None
            else "the next reset"
        )
        return Remedy(
            error_class=cls,
            retryable=True,
            guidance=(
                "Your Claude subscription's usage window is exhausted (the "
                "rolling 5-hour or weekly cap). This is NOT a transient "
                f"throttle — the work is PAUSED and will resume automatically "
                f"at {when} when the window reopens. Do not try to work "
                "around it."
            ),
            env_overrides={},
            reset_session=False,
            # Do NOT inline-sleep a multi-hour reset — the caller defers the
            # work to ``reset_at`` and a scheduler resumes it. ``backoff_
            # seconds`` is only a conservative fallback when no reset time
            # could be parsed (re-check in ~10 min in case it was a short
            # window).
            backoff_seconds=600.0,
            reset_at=reset_at,
            escalation_message=(
                "Claude usage limit reached. The work is paused and will "
                "auto-resume when the usage window resets."
            ),
        )

    if cls is ErrorClass.API_OVERLOADED:
        return Remedy(
            error_class=cls,
            retryable=True,
            guidance=(
                "The Anthropic API was temporarily overloaded (HTTP "
                "529 / 503 / 'overloaded_error') on the previous "
                "attempt. This is a provider-wide condition that "
                "usually clears within a few minutes — no change in "
                "approach is needed on the retry. The session has "
                "been resumed, so your prior conversation context is "
                "intact."
            ),
            env_overrides={},
            reset_session=False,
            # 3 minutes — the user-confirmed cadence ("usually
            # temporary, retry in 3-5 minutes"). Combined with the
            # 3-attempt session retry budget, this gives us up to
            # ~9 minutes of patience before escalating to blocked,
            # which is long enough to ride out most provider blips
            # without flipping to a costly user-visible escalation.
            backoff_seconds=180.0,
            escalation_message=(
                "Agent hit a sustained API-overload condition across "
                "multiple retries (~9 min). The Anthropic platform "
                "is under unusual load; let the user know and retry "
                "manually in 15–30 minutes."
            ),
        )

    if cls is ErrorClass.TIMEOUT:
        return Remedy(
            error_class=cls,
            retryable=True,
            guidance=(
                "Your previous attempt timed out. On this retry, be "
                "decisive: pick the most direct path to satisfying the "
                "Acceptance Criteria. Do NOT re-explore alternative "
                "approaches — execute the one you already started."
            ),
            env_overrides={},
            reset_session=False,
            backoff_seconds=5.0,
            escalation_message=(
                "Agent session timed out repeatedly. The task may be too "
                "broad or blocked on a slow upstream service."
            ),
        )

    if cls is ErrorClass.CONNECTION_LOST:
        return Remedy(
            error_class=cls,
            retryable=True,
            guidance=(
                "The previous attempt was interrupted by a transient "
                "connection drop (the socket closed / reset mid-stream), "
                "NOT by anything wrong with the task. RESUMING the same "
                "session — your prior work and context are intact. Pick up "
                "exactly where you left off; do not redo completed steps."
            ),
            env_overrides={},
            # Resume — the conversation still exists; only the pipe broke.
            # Re-running from scratch would discard finished work (the exact
            # T4b symptom: MRs merged, only the change-summary write left).
            reset_session=False,
            backoff_seconds=3.0,
            escalation_message=(
                "Agent session was repeatedly cut off by connection drops "
                "(socket closed / reset). This is transport instability, not "
                "a task blocker — check the daemon↔backend WebSocket / MCP "
                "proxy health (cbcl status); the work itself may already be "
                "complete."
            ),
        )

    if cls is ErrorClass.SESSION_NOT_FOUND:
        return Remedy(
            error_class=cls,
            retryable=True,
            guidance=(
                "The previous attempt tried to resume a conversation that "
                "no longer exists in the container (the session was wiped "
                "by a container recreate or CLI upgrade). Starting a FRESH "
                "session — prior in-conversation context is gone, so rely "
                "on your system prompt, CLAUDE.md, and any on-disk state "
                "(task brief, checkpoint files, office files)."
            ),
            env_overrides={},
            # Drop the stale session_id and start clean — resuming it
            # would just fail again.
            reset_session=True,
            backoff_seconds=1.0,
            escalation_message=(
                "Agent's stored session id no longer resolves to a live "
                "conversation and a fresh session also failed. The "
                "in-container conversation store may be unhealthy — try "
                "restarting the office container (cbcl stop/start)."
            ),
        )

    if cls is ErrorClass.TOOL_UNAVAILABLE:
        return Remedy(
            error_class=cls,
            retryable=False,
            guidance=(
                "A tool referenced in the prompt is not available. This "
                "indicates a configuration bug — cannot self-recover."
            ),
            env_overrides={},
            reset_session=False,
            backoff_seconds=0.0,
            escalation_message=(
                "Agent reported a missing tool. Check the agent config "
                "(allowed_tools) and MCP server registration."
            ),
        )

    if cls is ErrorClass.AUTH_FAILED:
        return Remedy(
            error_class=cls,
            retryable=False,
            guidance=(
                "Authentication against the Anthropic API failed. This "
                "requires user action (token refresh) — cannot self-recover."
            ),
            env_overrides={},
            reset_session=False,
            backoff_seconds=0.0,
            escalation_message=(
                "Agent hit an auth failure (401/403). The Claude credentials "
                "need to be refreshed via `claude auth login`."
            ),
        )

    if cls is ErrorClass.PROCESS_KILLED:
        return Remedy(
            error_class=cls,
            retryable=True,
            guidance=(
                "Your previous attempt was killed by the operating "
                "system. Exit codes we observe: 137 = OOM killer, "
                "143 = container SIGTERM, 139 = SIGSEGV (native "
                "crash), 134 = SIGABRT (assertion trap). All four "
                "most often trigger when the Claude CLI's resident "
                "memory or syscall activity exceeds container limits "
                "while assembling a very large response. The retry "
                "resumes the same session AND your disk work survives, "
                "so follow the LARGE DELIVERABLE PROTOCOL:\n"
                "0. FIRST — `Glob` your task's output directory (your "
                "   prompt names it; under per-workstream layout it is "
                "   `/workspace/outputs/{workstream}/[{scope}/]`) for "
                "   files matching your readable_id (also look for "
                "   `*_CHECKPOINT.md`), plus the legacy flat "
                "   `/workspace/outputs/` root for older runs. `Read` "
                "   the checkpoint file if present; it lists `done` "
                "   vs `pending` chunks.\n"
                "1. Resume from the first `pending` chunk. Do NOT redo "
                "   completed chunks.\n"
                "2. Never hold large content fully in memory. Use "
                "   `Write` for each chunk as soon as it is drafted, "
                "   use `Bash` (`cat`, `sed`, `awk`) for file transforms "
                "   rather than reading entire files through the "
                "   assistant, and prefer small tool-call outputs.\n"
                "3. If a single chunk is still too large, split it "
                "   further and update the CHECKPOINT.md index so the "
                "   next retry (if any) can resume precisely.\n"
                "4. Keep assistant messages short — one-line status "
                "   updates between tool calls."
            ),
            env_overrides={},
            # Keep session — the context contains everything the agent
            # knows about the task so far; losing it forces rediscovery.
            # Memory pressure is ephemeral between attempts.
            reset_session=False,
            # Brief pause lets kernel reclaim pages and Docker settle.
            backoff_seconds=5.0,
            escalation_message=(
                "Agent was killed by the OS on multiple attempts (likely "
                "OOM). The task's memory footprint is too large for one "
                "session — split into smaller tasks, or reduce the input "
                "size, or increase the container memory limit. Each "
                "retry already tried the chunked/resume protocol."
            ),
        )

    # Fallthrough — shouldn't happen given the classify_error control flow,
    # but keeps the type-checker happy and is a safe default.
    return _unknown_fatal(text)


def _unknown_fatal(text: str) -> Remedy:
    return Remedy(
        error_class=ErrorClass.UNKNOWN_FATAL,
        retryable=False,
        guidance=(
            "An unclassified error occurred. The system cannot "
            "automatically recover — escalating to the Manager Assistant."
        ),
        env_overrides={},
        reset_session=False,
        backoff_seconds=0.0,
        escalation_message=(
            f"Agent produced an unclassified error: {text[:400]}"
        ),
    )
