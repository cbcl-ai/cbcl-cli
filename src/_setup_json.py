"""Tolerant JSON parsing for Claude CLI responses.

Extracted from ``setup_generator.py`` (Wave 4 decomposition). Pure
stdlib, no async, no docker. Re-exported from ``setup_generator``
for back-compat.

Real-world Claude output frequently arrives with:
* Wrapped in a ```json fence (or a bare ``` fence).
* A leading apology paragraph before the JSON object.
* Trailing commas before ``}`` / ``]`` (allowed in JS, fatal in JSON).
* Missing commas between adjacent objects in an array.
"""

from __future__ import annotations

import json
import re
from typing import Any


class GenerationError(RuntimeError):
    """A generation failure whose message is SAFE to surface verbatim to
    the end user.

    The single-shot AI generators raise plain ``Exception`` /
    ``RuntimeError`` for a wide range of internal faults whose text may
    embed workspace paths, token prefixes, or raw ``docker exec`` stderr —
    those must be collapsed to a generic "check the daemon logs" message
    at the request boundary. ``GenerationError`` is the opt-in marker for
    the SUBSET of failures whose message is curated + actionable (empty
    Claude output with auth/model disambiguation, a non-object JSON
    payload): the request dispatch (``_handlers/_requests.py``) forwards
    ``str(exc)`` for these and only these, so the user gets the useful
    guidance instead of a dead-end "check the logs".
    """


class EmptyGenerationOutputError(GenerationError):
    """The CLI returned NO output — a DETERMINISTIC failure (auth broken or
    the configured model is unavailable), NOT a transient formatting glitch.

    Distinguished from a plain ``GenerationError`` (which also covers a
    retryable non-object-JSON parse failure) so GEN-14's budget-permitted
    retry does NOT waste an attempt re-running a call that will fail
    identically. Still a ``GenerationError`` subclass, so it keeps the
    user-safe-message forwarding behaviour at the request boundary.
    """


def _require_dict(parsed: Any) -> dict[str, Any]:
    """Guard: the generation contract is ALWAYS a JSON object.

    ``json.loads`` happily returns a list / string / number / bool for a
    top-level non-object payload, so despite the ``dict[str, Any]``
    annotation a stray non-object escapes ``_parse_json_response`` and the
    caller's ``result.get("instructions"/"content"/…)`` then blows up with
    an opaque ``AttributeError`` deep inside the daemon handler (surfacing
    as a generic 502). Fail fast here with a clear, user-safe message that
    matches the empty-output error's posture instead.
    """
    if not isinstance(parsed, dict):
        raise GenerationError(
            "Claude returned a non-object JSON value "
            f"({type(parsed).__name__}); the generator expected a JSON "
            "object. This usually means the model didn't follow the "
            "output contract — retry, or refine the request."
        )
    return parsed


def _empty_cli_output_error() -> GenerationError:
    """Fallback error for the empty-output case when callers don't go
    through ``_setup_cli._empty_cli_output_error``. The richer version
    (with model + probe diagnostics) lives in ``_setup_cli``; this
    one keeps the JSON parser self-contained for unit-testability.
    """
    return EmptyGenerationOutputError(
        "Claude CLI returned empty output — parse target is empty. "
        "Caller should have raised a rich CLI-specific error first; "
        "this branch is the defence-in-depth backstop."
    )


def _parse_json_response(raw_text: str) -> dict[str, Any]:
    """Parse a Claude JSON response, tolerating common malformations.

    Real-world Claude output frequently arrives with one of:

    * Wrapped in a ```json fence (or a bare ``` fence with no lang).
    * A leading apology paragraph before the JSON object.
    * Trailing commas before ``}`` / ``]`` (allowed in JS, fatal in JSON).
    * Missing commas between adjacent objects in an array — the
      ``Expecting ',' delimiter`` error the setup wizard kept hitting.

    We try the parser in stages, cheapest-first, so a clean response
    still hits the fast path and only malformed payloads pay the
    repair cost. Stages:

    1. Bare ``json.loads``.
    2. Strip code fences and retry.
    3. Slice out the first balanced ``{...}`` block (drops prose
       around it) and retry.
    4. Apply two repair regexes (trailing commas, ``}<ws>{`` →
       ``},{``) and retry.

    If everything fails we raise the LAST exception so the caller sees
    the actual parse error in logs, not a generic "couldn't repair".
    A stdlib-only implementation is deliberate — adding ``json-repair``
    to the deps balloons the agent image without buying much: the four
    rules above cover every Claude failure we've actually seen, and an
    unknown new failure mode should land in a bug ticket, not get
    silently papered over by a magic repair lib.
    """
    text = raw_text.strip()
    # Defence-in-depth for the silent-auth-failure case; ``_run_claude_cli``
    # already raises this for the production path, but a future caller
    # bypassing it would otherwise hit ``json.loads("")`` and get an
    # opaque ``Expecting value: line 1 column 1 (char 0)`` error.
    if not text:
        raise _empty_cli_output_error()
    # Stage 1 — fast path. ``_require_dict`` enforces the object contract;
    # a non-object payload raises ``GenerationError`` (NOT a
    # ``JSONDecodeError``) so it propagates immediately instead of falling
    # through to the repair stages, which would only re-parse to the same
    # non-object value.
    try:
        return _require_dict(json.loads(text))
    except json.JSONDecodeError as exc:
        first_error = exc

    # Stage 2 — strip code fences.
    text = _strip_code_fences(text)
    try:
        return _require_dict(json.loads(text))
    except json.JSONDecodeError:
        pass

    # Stage 3 — extract first balanced {...} block.
    extracted = _extract_first_json_object(text)
    if extracted is not None:
        try:
            return _require_dict(json.loads(extracted))
        except json.JSONDecodeError:
            text = extracted

    # Stage 4 — apply repair regexes.
    repaired = _repair_common_json_errors(text)
    if repaired != text:
        try:
            return _require_dict(json.loads(repaired))
        except json.JSONDecodeError as exc:
            # Re-raise the repaired-stage error rather than the original
            # so the log line points at the failure mode the repair pass
            # couldn't handle. The caller will retry the whole CLI call;
            # this just helps the next debugger.
            raise exc

    raise first_error


def _strip_code_fences(text: str) -> str:
    """Strip ``` fences (with or without a language tag) from a payload.

    Keeps the inside verbatim — earlier versions used ``strip()`` per
    line which silently ate user-content leading whitespace.
    """
    s = text.strip()
    if not s.startswith("```"):
        return s
    try:
        first_nl = s.index("\n")
    except ValueError:
        return s
    last_fence = s.rfind("```")
    if last_fence > first_nl:
        return s[first_nl + 1:last_fence].strip()
    return s[first_nl + 1:].strip()


def _extract_first_json_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` substring, or None.

    Walks the text and counts unescaped braces so a JSON value
    containing literal ``{`` / ``}`` inside a string doesn't unbalance
    the count. Stops at the first depth-0 close. Returns None when no
    balanced block is found.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


_MISSING_COMMA_RE = re.compile(r"([}\]\"])(\s*\n\s*)([{\[\"])")


def _repair_common_json_errors(text: str) -> str:
    """Two cheap regex passes that fix the failure modes we see most.

    Order matters: trailing-comma removal first (a ``,}`` looks like
    a malformed gap to the missing-comma pass otherwise).
    """
    fixed = _TRAILING_COMMA_RE.sub(r"\1", text)
    fixed = _MISSING_COMMA_RE.sub(r"\1,\2\3", fixed)
    return fixed


