"""Unit tests for the error classifier.

Covers: exact pattern matches for each known class, tolerance to wrapping
prefixes ("API Error:", etc.), casing variance, empty inputs, and the
catch-all UNKNOWN_FATAL path.
"""

from __future__ import annotations

import pytest

from src.orchestrator.error_classifier import (
    ErrorClass,
    Remedy,
    classify_error,
)


class TestOutputTokenLimit:
    """OUTPUT_TOKEN_LIMIT — must catch the exact FCB-001.T42 wording and
    its common variants, and MUST attach the CLAUDE_CODE_MAX_OUTPUT_TOKENS
    override + guidance pointing the agent at the LARGE DELIVERABLE
    PROTOCOL (CHECKPOINT.md resume convention)."""

    def test_exact_fcb_t42_wording(self):
        msg = (
            "API Error: Claude's response exceeded the 32000 output token "
            "maximum. To configure this behavior, set the "
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS environment variable."
        )
        r = classify_error(msg)
        assert r.error_class is ErrorClass.OUTPUT_TOKEN_LIMIT
        assert r.retryable is True
        assert r.env_overrides.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == "64000"
        assert r.reset_session is False
        # Guidance must route the agent back to the system-prompt protocol
        # so retry + prompt stay aligned. CHECKPOINT.md is the shared
        # vocabulary — a retry referring to "incremental" or any other
        # term the prompt doesn't use would send the agent hunting.
        assert "CHECKPOINT" in r.guidance

    def test_variant_output_token_limit(self):
        msg = "Error: response reached the output token limit"
        assert classify_error(msg).error_class is ErrorClass.OUTPUT_TOKEN_LIMIT

    def test_variant_env_var_mention(self):
        msg = "Please increase CLAUDE_CODE_MAX_OUTPUT_TOKENS"
        assert classify_error(msg).error_class is ErrorClass.OUTPUT_TOKEN_LIMIT

    def test_case_insensitive(self):
        msg = "EXCEEDED THE 32000 OUTPUT TOKEN MAXIMUM"
        assert classify_error(msg).error_class is ErrorClass.OUTPUT_TOKEN_LIMIT


class TestContextTooLarge:
    """CONTEXT_TOO_LARGE — input-side errors; distinct from output-token.
    Must trigger reset_session=True."""

    def test_prompt_too_long(self):
        r = classify_error("prompt too long: 250000 tokens vs 200000 limit")
        assert r.error_class is ErrorClass.CONTEXT_TOO_LARGE
        assert r.retryable is True
        assert r.reset_session is True

    def test_prompt_is_too_long_real_cli_phrasing(self):
        # The Claude CLI's ACTUAL message includes "is" — the old regex
        # missed it, so an oversized resumed session wedged as
        # UNKNOWN_FATAL. Lock the real phrasing (incl. the numeric tail).
        r = classify_error(
            "Claude CLI exited with code 1\n"
            "API Error: prompt is too long: 217676 tokens > 200000 maximum"
        )
        assert r.error_class is ErrorClass.CONTEXT_TOO_LARGE
        assert r.reset_session is True

    def test_tokens_over_maximum_numeric_tail(self):
        r = classify_error("400 {'message': '217676 tokens > 200000 maximum'}")
        assert r.error_class is ErrorClass.CONTEXT_TOO_LARGE

    def test_context_window(self):
        r = classify_error("Exceeded context window")
        assert r.error_class is ErrorClass.CONTEXT_TOO_LARGE

    def test_input_token_exceeded(self):
        r = classify_error("input token limit exceeded: too many messages")
        assert r.error_class is ErrorClass.CONTEXT_TOO_LARGE

    def test_does_not_match_output_token_messages(self):
        # Output-token messages should NOT fall into this bucket.
        r = classify_error("exceeded the 32000 output token maximum")
        assert r.error_class is ErrorClass.OUTPUT_TOKEN_LIMIT


class TestRateLimited:

    def test_429(self):
        r = classify_error("HTTP 429: Too many requests")
        assert r.error_class is ErrorClass.RATE_LIMITED
        assert r.retryable is True
        assert r.backoff_seconds >= 10.0

    def test_rate_limit_word(self):
        assert classify_error("rate limit hit").error_class is ErrorClass.RATE_LIMITED

    def test_quota(self):
        assert (
            classify_error("quota exceeded for this API key").error_class
            is ErrorClass.RATE_LIMITED
        )


class TestApiOverloaded:
    """Anthropic API 529 / 503 / 'overloaded_error' — provider-wide
    temporary overload, distinct from per-account 429 rate-limit.
    Backoff is intentionally long (~3 min) so the existing 3-retry
    budget covers ~9 minutes total — long enough to ride out most
    blips before escalating."""

    def test_529(self):
        r = classify_error("API Error 529: Overloaded")
        assert r.error_class is ErrorClass.API_OVERLOADED
        assert r.retryable is True
        assert r.backoff_seconds >= 120.0, (
            f"Expected ≥2 min backoff; got {r.backoff_seconds}s"
        )

    def test_overloaded_error_word(self):
        r = classify_error('{"type":"overloaded_error","message":"Overloaded"}')
        assert r.error_class is ErrorClass.API_OVERLOADED

    def test_temporarily_overloaded(self):
        assert (
            classify_error("The API is temporarily overloaded.").error_class
            is ErrorClass.API_OVERLOADED
        )

    def test_503(self):
        assert (
            classify_error("HTTP 503 Service Unavailable").error_class
            is ErrorClass.API_OVERLOADED
        )

    def test_service_temporarily_unavailable(self):
        assert (
            classify_error("Service is temporarily unavailable").error_class
            is ErrorClass.API_OVERLOADED
        )

    def test_529_does_not_match_rate_limited(self):
        """529 must classify as API_OVERLOADED, not RATE_LIMITED — the
        remediation (long backoff, provider-wide) differs from 429
        (short backoff, per-account)."""
        r = classify_error("HTTP 529")
        assert r.error_class is ErrorClass.API_OVERLOADED
        assert r.error_class is not ErrorClass.RATE_LIMITED

    def test_504_still_classifies_as_timeout(self):
        """Sanity: 504 must NOT pull into API_OVERLOADED (504 is a
        gateway-timeout, semantically a TIMEOUT)."""
        r = classify_error("HTTP 504 Gateway Timeout")
        assert r.error_class is ErrorClass.TIMEOUT


class TestTimeout:

    def test_timeout_keyword(self):
        r = classify_error("Request timed out after 120s")
        assert r.error_class is ErrorClass.TIMEOUT
        assert r.retryable is True

    def test_deadline(self):
        assert (
            classify_error("Deadline exceeded").error_class is ErrorClass.TIMEOUT
        )

    def test_504(self):
        assert (
            classify_error("504 Gateway Timeout").error_class is ErrorClass.TIMEOUT
        )


class TestToolUnavailable:

    def test_tool_not_found(self):
        r = classify_error("Tool not found: my_custom_tool")
        assert r.error_class is ErrorClass.TOOL_UNAVAILABLE
        assert r.retryable is False  # config bug, no automatic fix

    def test_mcp_server_failed(self):
        assert (
            classify_error("MCP server failed to start").error_class
            is ErrorClass.TOOL_UNAVAILABLE
        )

    def test_our_own_cubicle_tool_not_misclassified(self):
        # Our MCP tool server's "Unknown tool: mcp__cubicle-tools__foo"
        # comes from a different code path and shouldn't land here as
        # a TOOL_UNAVAILABLE (which is a CLI-level error class).
        msg = "Unknown tool: mcp__cubicle-tools__foo"
        r = classify_error(msg)
        # Not the tool_unavailable pattern because of the cubicle-tools
        # negative lookahead; falls through to UNKNOWN_FATAL.
        assert r.error_class is not ErrorClass.TOOL_UNAVAILABLE


class TestSessionNotFound:
    """ADD-E1: a stale --resume target must be a recoverable
    fresh-session retry, NOT an UNKNOWN_FATAL wedge."""

    def test_exact_cli_wording(self):
        r = classify_error(
            "No conversation found with session ID abc-123-def"
        )
        assert r.error_class is ErrorClass.SESSION_NOT_FOUND
        assert r.retryable is True
        assert r.reset_session is True  # must start fresh, not re-resume

    def test_session_id_not_found_variant(self):
        r = classify_error("session id xyz not found")
        assert r.error_class is ErrorClass.SESSION_NOT_FOUND

    def test_could_not_resume_variant(self):
        r = classify_error("Could not resume the conversation")
        assert r.error_class is ErrorClass.SESSION_NOT_FOUND

    def test_session_expired_variant(self):
        r = classify_error("session has expired")
        assert r.error_class is ErrorClass.SESSION_NOT_FOUND

    def test_not_confused_with_tool_not_found(self):
        # "Tool not found" must stay TOOL_UNAVAILABLE, not session.
        r = classify_error("Tool not found: my_custom_tool")
        assert r.error_class is ErrorClass.TOOL_UNAVAILABLE


class TestAuthFailed:

    def test_401(self):
        r = classify_error("HTTP 401 Unauthorized")
        assert r.error_class is ErrorClass.AUTH_FAILED
        assert r.retryable is False

    def test_invalid_api_key(self):
        assert (
            classify_error("invalid api key").error_class
            is ErrorClass.AUTH_FAILED
        )

    def test_credentials_expired(self):
        assert (
            classify_error("Credentials expired").error_class
            is ErrorClass.AUTH_FAILED
        )


class TestProcessKilled:
    """PROCESS_KILLED — covers the signals the OS/Docker sends to a
    runaway Claude CLI. Exit code 137 (SIGKILL) and 143 (SIGTERM) are
    the two we see in practice, plus the kernel/Docker textual markers
    OOMKilled / out of memory / killed.

    Regression: FCB-001.T92 escalated as unknown_fatal with the
    synthetic string "Claude CLI exited with code 137" before this
    class existed."""

    def test_exit_code_137_is_process_killed(self):
        r = classify_error("Claude CLI exited with code 137")
        assert r.error_class is ErrorClass.PROCESS_KILLED
        assert r.retryable is True
        assert r.reset_session is False  # preserve session for resume
        assert r.backoff_seconds >= 1.0

    def test_exit_code_143_is_process_killed(self):
        r = classify_error("Claude CLI exited with code 143")
        assert r.error_class is ErrorClass.PROCESS_KILLED
        assert r.retryable is True

    def test_exit_code_139_sigsegv_is_process_killed(self):
        # Native crash inside the CLI (or a C extension) surfaces as
        # 128+11=139. Rare in practice but real — the retry path
        # (preserve session + disk work) is the right remedy.
        r = classify_error("Claude CLI exited with code 139")
        assert r.error_class is ErrorClass.PROCESS_KILLED
        assert r.retryable is True

    def test_exit_code_134_sigabrt_is_process_killed(self):
        # abort() / assertion failure inside the CLI → 128+6=134.
        r = classify_error("Claude CLI exited with code 134")
        assert r.error_class is ErrorClass.PROCESS_KILLED
        assert r.retryable is True

    def test_segmentation_fault_text(self):
        r = classify_error("Segmentation fault (core dumped)")
        assert r.error_class is ErrorClass.PROCESS_KILLED

    def test_sigsegv_text(self):
        r = classify_error("received SIGSEGV signal")
        assert r.error_class is ErrorClass.PROCESS_KILLED

    def test_aborted_text(self):
        r = classify_error("Aborted (core dumped)")
        assert r.error_class is ErrorClass.PROCESS_KILLED

    def test_sigabrt_text(self):
        r = classify_error("got SIGABRT from assertion")
        assert r.error_class is ErrorClass.PROCESS_KILLED

    def test_aborted_by_user_not_classified(self):
        # User-initiated abort shouldn't auto-retry, same as
        # "killed by user".
        r = classify_error("Aborted by user — cancellation requested")
        assert r.error_class is not ErrorClass.PROCESS_KILLED

    def test_oomkilled_marker(self):
        # Docker inspect / kernel cgroup emits this string verbatim.
        r = classify_error("container status: OOMKilled")
        assert r.error_class is ErrorClass.PROCESS_KILLED

    def test_out_of_memory_text(self):
        r = classify_error("fatal: out of memory allocating buffer")
        assert r.error_class is ErrorClass.PROCESS_KILLED

    def test_sigkill_text(self):
        r = classify_error("received SIGKILL")
        assert r.error_class is ErrorClass.PROCESS_KILLED

    def test_killed_text_without_user_suffix(self):
        r = classify_error("Process killed")
        assert r.error_class is ErrorClass.PROCESS_KILLED

    def test_killed_by_user_not_process_killed(self):
        # A user-initiated cancel should NOT look like OOM — don't
        # auto-retry what the user meant to stop.
        r = classify_error("Task killed by user")
        assert r.error_class is not ErrorClass.PROCESS_KILLED

    def test_guidance_references_checkpoint_protocol(self):
        # The retry must steer the agent at the CHECKPOINT.md resume
        # flow so it doesn't re-do completed chunks and re-hit OOM.
        r = classify_error("Claude CLI exited with code 137")
        assert "CHECKPOINT" in r.guidance
        lowered = r.guidance.lower()
        assert "pending" in lowered
        assert "memory" in lowered

    def test_exit_code_1_is_not_process_killed(self):
        # Generic non-zero exit with no signal context falls through to
        # UNKNOWN_FATAL. We don't want to silently retry every failure.
        r = classify_error("Claude CLI exited with code 1")
        assert r.error_class is ErrorClass.UNKNOWN_FATAL


class TestUnknownFatal:

    def test_none(self):
        assert classify_error(None).error_class is ErrorClass.UNKNOWN_FATAL

    def test_empty(self):
        assert classify_error("").error_class is ErrorClass.UNKNOWN_FATAL

    def test_whitespace_only(self):
        assert classify_error("   \n\t").error_class is ErrorClass.UNKNOWN_FATAL

    def test_non_string(self):
        assert classify_error(12345).error_class is ErrorClass.UNKNOWN_FATAL  # type: ignore[arg-type]

    def test_unclassified_text(self):
        r = classify_error("Something completely random and novel happened")
        assert r.error_class is ErrorClass.UNKNOWN_FATAL
        assert r.retryable is False

    def test_unknown_fatal_includes_original_in_escalation(self):
        r = classify_error("Some weird thing 12345")
        assert "Some weird thing 12345" in r.escalation_message


class TestRemedyShape:
    """Every Remedy should be well-formed and the dataclass should be
    safe to serialize (frozen, comparable)."""

    @pytest.mark.parametrize(
        "text",
        [
            "exceeded the 32000 output token maximum",
            "prompt too long",
            "HTTP 429",
            "timed out",
            "tool not found",
            "HTTP 401",
            # Cover the process-kill path too so the shape invariants
            # (guidance non-empty, env_overrides dict, etc.) are
            # enforced for the newest class alongside the others.
            "Claude CLI exited with code 137",
            "weird unknown error",
        ],
    )
    def test_remedy_has_required_fields(self, text: str):
        r = classify_error(text)
        assert isinstance(r, Remedy)
        assert isinstance(r.error_class, ErrorClass)
        assert isinstance(r.retryable, bool)
        assert isinstance(r.guidance, str) and r.guidance
        assert isinstance(r.env_overrides, dict)
        assert isinstance(r.reset_session, bool)
        assert r.backoff_seconds >= 0
        assert isinstance(r.escalation_message, str) and r.escalation_message

    def test_remedy_is_frozen(self):
        r = classify_error("exceeded the 32000 output token maximum")
        with pytest.raises(Exception):
            r.retryable = False  # type: ignore[misc]

    def test_remedies_are_equal_for_same_input(self):
        r1 = classify_error("rate limited")
        r2 = classify_error("rate limited")
        assert r1 == r2


class TestOrderingAndSpecificity:
    """The first matching pattern wins. Verify the order is correct —
    specific classes should beat general ones."""

    def test_output_token_in_timeout_like_sentence(self):
        # "timed out" shows up in many stacktraces — make sure we don't
        # misclassify an output-token error that mentions timing.
        msg = (
            "After 60s the API returned: exceeded the 32000 output token "
            "maximum"
        )
        assert (
            classify_error(msg).error_class is ErrorClass.OUTPUT_TOKEN_LIMIT
        )

    def test_rate_limit_beats_timeout(self):
        # 429 responses commonly mention Retry-After which can read like
        # a timeout hint. Rate-limit is more specific and should win.
        msg = "HTTP 429 Too Many Requests (retry after timeout)"
        assert classify_error(msg).error_class is ErrorClass.RATE_LIMITED


class TestConnectionLost:
    """Transient transport drops must be retryable (resume), not escalated
    to blocked as UNKNOWN_FATAL. Regression for the T4b crash where a
    'socket connection closed / CLI exit 1' on the next step blocked an
    otherwise-finished task."""

    def test_socket_connection_closed(self):
        r = classify_error("Error: socket connection closed")
        assert r.error_class is ErrorClass.CONNECTION_LOST
        assert r.retryable is True
        assert r.reset_session is False  # resume — work isn't lost

    def test_connection_reset_by_peer(self):
        r = classify_error("aiohttp.ClientError: Connection reset by peer")
        assert r.error_class is ErrorClass.CONNECTION_LOST
        assert r.retryable is True

    def test_broken_pipe(self):
        assert (
            classify_error("BrokenPipeError: [Errno 32] Broken pipe").error_class
            is ErrorClass.CONNECTION_LOST
        )

    def test_econnreset_marker(self):
        assert (
            classify_error("write ECONNRESET").error_class
            is ErrorClass.CONNECTION_LOST
        )

    def test_server_disconnected(self):
        assert (
            classify_error("Server disconnected").error_class
            is ErrorClass.CONNECTION_LOST
        )

    def test_bare_exit_1_is_not_misclassified(self):
        # A bare exit-1 with NO connection marker stays fatal (ambiguous).
        r = classify_error("Claude CLI exited with code 1")
        assert r.error_class is not ErrorClass.CONNECTION_LOST


class TestUsageLimit:
    """USAGE_LIMIT_EXCEEDED — the rolling 5-hour / weekly subscription cap,
    distinct from the per-minute RATE_LIMITED 429. Carries a parsed reset
    time so the work can be deferred + auto-resumed."""

    def test_usage_limit_reached_classifies(self):
        r = classify_error("Claude usage limit reached. Your limit will reset at 11pm")
        assert r.error_class is ErrorClass.USAGE_LIMIT_EXCEEDED
        assert r.retryable is True
        assert r.reset_at is not None

    def test_five_hour_limit_classifies(self):
        r = classify_error("5-hour limit reached ∙ resets 3pm")
        assert r.error_class is ErrorClass.USAGE_LIMIT_EXCEEDED

    def test_weekly_limit_classifies(self):
        assert (
            classify_error("You have reached your weekly limit").error_class
            is ErrorClass.USAGE_LIMIT_EXCEEDED
        )

    def test_epoch_reset_time_parsed(self):
        import time as _t
        from datetime import timezone

        future = int(_t.time()) + 5 * 3600
        r = classify_error(f"Claude AI usage limit reached|{future}")
        assert r.error_class is ErrorClass.USAGE_LIMIT_EXCEEDED
        assert r.reset_at is not None
        assert abs(r.reset_at.timestamp() - future) < 2

    def test_usage_limit_wins_over_rate_limit_ordering(self):
        # A message with both "usage limit" and "limit" must NOT fall to
        # RATE_LIMITED (which would apply a 60s backoff to a multi-hour cap).
        r = classify_error("Claude usage limit reached. resets at 2026-12-31T23:59:00")
        assert r.error_class is ErrorClass.USAGE_LIMIT_EXCEEDED

    def test_bare_resets_in_does_not_hijack_429(self):
        # A 429 that merely mentions "resets in 30 seconds" must stay
        # RATE_LIMITED (60s), not be deferred ~600s as a usage cap.
        r = classify_error("API Error: 429 Too Many Requests. Quota resets in 30 seconds")
        assert r.error_class is ErrorClass.RATE_LIMITED

    def test_bare_resets_phrase_is_not_a_usage_limit(self):
        r = classify_error("Token bucket resets in 30 seconds")
        assert r.error_class is not ErrorClass.USAGE_LIMIT_EXCEEDED

    def test_limit_resets_at_without_will_classifies(self):
        # "Your limit resets at 11pm" (no "will") still classifies as usage.
        assert (
            classify_error("Your limit resets at 11pm").error_class
            is ErrorClass.USAGE_LIMIT_EXCEEDED
        )

    def test_connection_reset_still_wins_over_usage(self):
        # "reset by peer" must stay CONNECTION_LOST, not match the usage
        # "reset" pattern.
        assert (
            classify_error("socket connection reset by peer").error_class
            is ErrorClass.CONNECTION_LOST
        )


class TestRateLimitBackoff:
    def test_rate_limit_backoff_is_one_minute(self):
        # Per the resilience requirement: a 429 waits ~1 minute then resumes.
        r = classify_error("API Error 429 rate limit exceeded")
        assert r.error_class is ErrorClass.RATE_LIMITED
        assert r.backoff_seconds == 60.0
