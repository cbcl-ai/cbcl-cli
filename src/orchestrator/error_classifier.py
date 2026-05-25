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
    TIMEOUT = "timeout"
    TOOL_UNAVAILABLE = "tool_unavailable"
    AUTH_FAILED = "auth_failed"
    # The CLI subprocess was killed by the OS (SIGKILL/SIGTERM).
    # Typically caused by the OOM killer when the Claude CLI's memory
    # footprint exceeds the container limit while streaming a very
    # large response. Retry with a chunked approach so the resident
    # set at any moment stays small.
    PROCESS_KILLED = "process_killed"
    UNKNOWN_FATAL = "unknown_fatal"


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
            r"prompt\s+too\s+long|context\s+window|input\s+token.{0,30}(exceed|limit|maximum)"
            r"|too\s+many\s+input\s+tokens",
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
                "Waiting a few seconds and retrying — no change in approach."
            ),
            env_overrides={},
            reset_session=False,
            backoff_seconds=15.0,
            escalation_message=(
                "Agent hit the API rate limit multiple times. Check the "
                "Anthropic plan tier or reduce parallel agent activity."
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
