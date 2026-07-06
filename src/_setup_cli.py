"""Claude CLI runners + tool-list normalizer used by setup_generator.

Extracted from ``setup_generator.py`` (Wave 4 decomposition).
Re-exported from ``setup_generator`` for back-compat.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
import uuid
from typing import Any

from .config_sync.claude_md_content import SYSTEM_AGENT_CLAUDE_MD  # noqa: F401
from .orchestrator._model_defaults import FALLBACK_MANAGER_MODEL, is_opus_tier
from ._setup_json import (
    EmptyGenerationOutputError,
    GenerationError,
    _parse_json_response,
)

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    """Read a positive-integer env override, falling back to ``default``.

    A blank, non-numeric, or non-positive value logs a warning and keeps
    the default — an operator typo can never silently zero out a timeout.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Ignoring non-integer %s=%r; using default %d.", name, raw, default,
        )
        return default
    if value <= 0:
        logger.warning(
            "Ignoring non-positive %s=%d; using default %d.", name, value, default,
        )
        return default
    return value


_CHUNK_TIMEOUT = 360


# Setup-wizard generation runs on the platform-standard Opus-thinking
# tier — the office design pass is the single highest-leverage moment
# in an office's life (it decides the team, conventions, workflows, and
# skills the office runs on forever), so it gets the strongest model
# even though it costs latency. A typical office fires ~6-15 LLM calls;
# on Opus a full run takes ~15-20 min. That tradeoff is intentional:
# design quality > setup speed. Operators who need a faster (lower
# quality) setup can set ``CBCL_GENERATION_MODEL=claude-sonnet-4-6``
# (or any alias) to override per-install.
_DEFAULT_GENERATION_MODEL = (
    os.environ.get("CBCL_GENERATION_MODEL", "").strip()
    or FALLBACK_MANAGER_MODEL
)


# Item-6: run the high-leverage office / workstream / agent / skill AI
# generators at high reasoning-effort. xhigh = Claude Code's agentic
# default. Applied ONLY when the generation model is the Opus tier (the
# CLI rejects the higher levels on some non-opus models); an operator who
# overrides CBCL_GENERATION_MODEL to a non-opus tier transparently gets
# the CLI default. A flag-support gap on an older container CLI is handled
# by the graceful-degrade in ``_run_chunk``.
#
# NOTE: native sub-agent "workflows" are intentionally NOT enabled for
# these one-shot ``--max-turns 1`` JSON generators — sub-agent
# orchestration is the agentic Planner/worker path; wiring it into a
# single-shot JSON producer would need an agentic redesign and risks the
# JSON contract. Tracked as a follow-up; effort is the safe uplift here.
_DEFAULT_GENERATION_EFFORT: str | None = (
    "xhigh" if is_opus_tier(_DEFAULT_GENERATION_MODEL) else None
)


# Synchronous single-shot UI generators — the "Generate / Improve with AI"
# buttons (office instructions, agent system-prompt / instructions / config,
# workstream context, skill). These run while the user stares at a spinner AND
# the backend's ``call_generator`` waits a BOUNDED RPC budget
# (``ai_generation.DEFAULT_TIMEOUT_SECONDS``). The full xhigh + 360s setup-
# wizard budget does NOT fit that synchronous path: a slow xhigh generation
# outlived the backend's patience, which abandoned the request (504) while the
# daemon was STILL producing — so nothing ever came back (the reported
# "generation timed out … the Claude CLI may be stuck", with no result). These
# flows therefore run at a FASTER effort and a timeout that fits UNDER the
# backend budget, so the daemon always returns (a result, or a clean error)
# before the backend gives up. The async multi-phase setup wizard keeps the
# xhigh/360s budget (it streams progress, so a long wait is visible).
#
# ``CBCL_SYNC_GENERATION_EFFORT`` lets an operator trade speed for depth
# (e.g. set it to ``xhigh``) without a code change; leave it unset for the
# platform default ("high" on Opus). ``CBCL_SYNC_GENERATION_TIMEOUT`` is the
# matching knob to give a deeper generation more wall-clock.
#
# INVARIANT: this daemon ceiling MUST stay strictly below the backend's
# ``ai_generation.DEFAULT_TIMEOUT_SECONDS`` RPC budget (default 240s, itself
# tunable via ``CBCL_SYNC_GENERATION_BACKEND_TIMEOUT``) — otherwise the
# backend abandons the request (504) while the daemon is still producing and
# nothing comes back. An operator raising this env must raise the backend
# budget FIRST, keeping ~30-60s of headroom for tee/parse + the graceful
# ``--effort`` degrade re-run.
_SYNC_GENERATION_TIMEOUT = _int_env("CBCL_SYNC_GENERATION_TIMEOUT", 150)
_SYNC_GENERATION_EFFORT: str | None = (
    (os.environ.get("CBCL_SYNC_GENERATION_EFFORT", "").strip() or "high")
    if is_opus_tier(_DEFAULT_GENERATION_MODEL)
    else None
)


_MAX_RETRIES = 2

# GEN-14: single-shot flows run with max_retries=0 because the user is waiting.
# But a CHEAP parse failure (the CLI returned quickly with malformed / non-object
# JSON — a transient formatting glitch) is worth ONE retry IF it fits under the
# backend's synchronous generation ceiling. A TIMEOUT is never retried (it
# already consumed the whole per-call budget). This is the total wall-clock
# budget a single-shot generation may consume across attempts — it MUST match
# the backend's RPC ceiling, so it reads the SAME env
# (``CBCL_SYNC_GENERATION_BACKEND_TIMEOUT``) an operator uses to tune that
# ceiling (default 240), rather than a bare literal that silently diverges if
# the operator lowers the backend budget.
_GENERATION_WALL_BUDGET_S = _int_env("CBCL_SYNC_GENERATION_BACKEND_TIMEOUT", 240)
# Headroom so a budget-permitted retry finishes before the backend gives up.
_BUDGET_RETRY_HEADROOM_S = 30


_STANDARD_TOOL_NAMES = frozenset(
    {"Read", "Write", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"}
)


def _empty_cli_output_error(
    *,
    model: str = "",
    stderr: str = "",
    container_name: str = "",
    probe_succeeded: bool | None = None,
) -> GenerationError:
    """Shared error for the "Claude CLI produced no output" failure.

    Two distinct root causes the message disambiguates between:
      * ``probe_succeeded=True`` — a haiku probe DID get a response,
        so auth + CLI are fine; the configured ``model`` is the
        problem (not in this account's plan, or CLI too old to
        recognise the alias). Suggests trying the exact model.
      * ``probe_succeeded=False`` — even haiku came back empty, so
        auth itself is the issue. Suggests ``cbcl auth``.
      * ``probe_succeeded=None`` — no probe was run. Falls back to
        the generic both-causes message.
    """
    if probe_succeeded is True:
        # The configured model fails but a haiku probe works → auth
        # is fine but the container's Claude CLI can't resolve the
        # alias (CLI version too old to know it, or transient API
        # rejection). Suggest rebuilding the agent image to refresh
        # the CLI.
        msg = (
            f"Claude CLI returned empty output for model "
            f"``{model or '<unknown>'}``. The container's auth is "
            "fine (a haiku probe succeeded) — most likely the "
            "container's bundled Claude CLI is too old to recognise "
            "the model alias. Rebuild the agent image with "
            "`cbcl setup --force-rebuild-image` (or pull the latest "
            "image manually). Verify with: "
        )
        if container_name:
            msg += (
                f"`docker exec {container_name} claude --print "
                f"-p hello --model {model or '<alias>'}`"
            )
        else:
            msg += (
                f"`claude --print -p hello --model "
                f"{model or '<alias>'}` inside the office container"
            )
    elif probe_succeeded is False:
        # Auth-itself failure (even haiku empty).
        msg = (
            "Claude CLI returned empty output AND a fallback haiku "
            "probe also came back empty. The office container's "
            "Claude auth is most likely missing or expired — run "
            "`cbcl auth` to re-authenticate."
        )
    else:
        # Probe timed out / docker error — can't disambiguate.
        msg = (
            "Claude CLI returned empty output"
            + (f" for model ``{model}``" if model else "")
            + ". A haiku probe didn't complete cleanly so we can't "
            "tell yet whether this is auth or a model-alias issue. "
            "Try `cbcl auth` first; if that doesn't help, rebuild "
            "the agent image with `cbcl setup --force-rebuild-image`."
        )
    if stderr:
        msg += f" stderr: {stderr}"
    # Marked user-safe: every branch above is curated, actionable guidance
    # (run ``cbcl auth`` / rebuild the agent image / retry) — the request
    # dispatcher forwards it to the browser instead of the generic
    # "check the daemon logs" catch-all. ``EmptyGenerationOutputError`` (a
    # ``GenerationError`` subclass) marks this as DETERMINISTIC so GEN-14's
    # parse-retry doesn't waste an attempt on it.
    return EmptyGenerationOutputError(msg)


_PROBE_MODEL = "claude-haiku-4-5-20251001"


def _probe_claude_works(container_name: str) -> bool | None:
    """Run a 5s haiku probe to test if the container's Claude works
    at all. Returns True if the probe got a non-empty response,
    False if it also came back empty (auth is broken), None on
    timeout / docker error (can't tell either way).

    Cheap (haiku, single token) so safe to call from the
    empty-output diagnostic path.
    """
    try:
        result = subprocess.run(
            [
                "docker", "exec", container_name,
                "claude", "--print",
                "-p", "ok",
                "--output-format", "text",
                "--model", _PROBE_MODEL,
                "--max-turns", "1",
                "--permission-mode", "bypassPermissions",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return False
        return bool(result.stdout.strip())
    except Exception:
        return None


def _normalize_allowed_tools(raw: Any) -> list[str]:
    """Filter a raw `allowed_tools` value against the standard tool set.

    Defends against the AI returning hallucinated tool names (``Edit``,
    ``PerformSearch``, …) that would break the downstream AgentCreate
    validator. Always returns a non-empty list — falls back to
    ``["Read", "Write"]`` when the raw value is missing or filters
    down to empty.
    """
    if not isinstance(raw, list):
        return ["Read", "Write"]
    filtered = [
        t for t in raw
        if isinstance(t, str) and t in _STANDARD_TOOL_NAMES
    ]
    return filtered or ["Read", "Write"]


async def _run_claude_cli(
    container_name: str,
    system_prompt: str,
    user_prompt: str,
    timeout: int = _CHUNK_TIMEOUT,
    effort: str | None = None,
) -> str:
    """Run a Claude CLI query inside the Docker container.

    ``effort`` (item-6) adds ``--effort <level>`` when set — the value
    comes from a fixed internal set (never user input), so it's safe to
    interpolate into the bash command.
    """
    sys_file = f"/tmp/cubicle_sys_{uuid.uuid4().hex[:8]}.txt"
    user_file = f"/tmp/cubicle_user_{uuid.uuid4().hex[:8]}.txt"

    try:
        await asyncio.to_thread(
            subprocess.run,
            ["docker", "exec", "-i", container_name, "tee", sys_file],
            input=system_prompt, capture_output=True, text=True, timeout=10,
        )
        await asyncio.to_thread(
            subprocess.run,
            ["docker", "exec", "-i", container_name, "tee", user_file],
            input=user_prompt, capture_output=True, text=True, timeout=10,
        )

        effort_flag = f" --effort {effort}" if effort else ""
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "docker", "exec", container_name,
                "bash", "-c",
                f'cat "{user_file}" | claude --print'
                f" --output-format text"
                f" --max-turns 1"
                f" --model {_DEFAULT_GENERATION_MODEL}"
                f"{effort_flag}"
                f" --permission-mode bypassPermissions"
                f' --system-prompt-file "{sys_file}"',
            ],
            capture_output=True, text=True, timeout=timeout,
        )

        if result.returncode != 0:
            stderr = result.stderr.strip()[:500]
            stdout = result.stdout.strip()[:500]
            raise RuntimeError(
                f"Claude CLI failed (rc={result.returncode}): {stderr or stdout}"
            )

        stdout = result.stdout.strip()
        if not stdout:
            # rc=0 + empty stdout. Disambiguate auth vs
            # model-unavailable by running a haiku probe — same model
            # cbcl-setup uses for its auth check. If the probe ALSO
            # comes back empty, auth is broken; if it succeeds, the
            # configured model is the problem (most likely not in
            # this account's plan, or CLI too old).
            probe_result = await asyncio.to_thread(
                _probe_claude_works, container_name,
            )
            raise _empty_cli_output_error(
                model=_DEFAULT_GENERATION_MODEL,
                stderr=result.stderr.strip()[:500],
                container_name=container_name,
                probe_succeeded=probe_result,
            )
        return stdout

    finally:
        asyncio.create_task(asyncio.to_thread(
            subprocess.run,
            ["docker", "exec", container_name, "rm", "-f", sys_file, user_file],
            capture_output=True, timeout=5,
        ))


async def _run_chunk(
    container_name: str,
    system_prompt: str,
    user_prompt: str,
    timeout: int = _CHUNK_TIMEOUT,
    max_retries: int = _MAX_RETRIES,
    effort: str | None = _DEFAULT_GENERATION_EFFORT,
) -> dict[str, Any]:
    """Run a single chunk with bounded retries. Returns parsed JSON.

    ``max_retries=0`` is the right call for single-shot UI flows
    (Agents "Create with AI", workstream context-note generator) where
    the user is staring at a spinner and would rather retry by hand
    than wait through hidden retries. The multi-phase setup wizard
    keeps the default of 2 since each chunk is small and the streamed
    progress hides the wait.

    ``effort`` (item-6) is the CLI reasoning-effort for the generation
    call — defaults to xhigh on the Opus generation tier. If an older
    container CLI rejects ``--effort``, the call is retried ONCE without
    it (graceful degrade), independent of ``max_retries`` — so a
    flag-support gap never breaks generation, even on single-shot flows.
    """
    from ._session_policy import is_unknown_flag_error

    last_error = None
    current_effort = effort
    attempt = 0
    budget_retry_used = False
    started = time.monotonic()
    while True:
        try:
            raw = await _run_claude_cli(
                container_name, system_prompt, user_prompt,
                timeout=timeout, effort=current_effort,
            )
            return _parse_json_response(raw)
        except Exception as exc:
            last_error = exc
            # Graceful-degrade: drop --effort on an older CLI that
            # doesn't recognise it, then retry immediately (does NOT
            # consume a normal attempt — protects max_retries=0 flows).
            if current_effort and is_unknown_flag_error(str(exc)):
                logger.warning(
                    "Generation CLI rejected --effort; retrying without it.",
                )
                current_effort = None
                continue
            if attempt < max_retries:
                attempt += 1
                logger.warning(
                    "Chunk failed (attempt %d/%d): %s — retrying...",
                    attempt, max_retries + 1, exc,
                )
                await asyncio.sleep(2)
                continue
            # GEN-14: budget-permitted single retry for a CHEAP parse failure
            # on an otherwise no-retry (max_retries=0) flow. NEVER for a timeout
            # (already consumed the per-call budget) — only when the CLI came
            # back fast with malformed JSON AND another attempt still fits under
            # the backend ceiling with headroom.
            # Retry a transient parse glitch (malformed / non-object JSON), but
            # NOT a timeout (already consumed the per-call budget) and NOT the
            # DETERMINISTIC empty-output/auth failure (it would fail identically).
            is_parse_failure = isinstance(
                exc, (GenerationError, json.JSONDecodeError)
            ) and not isinstance(
                exc, (subprocess.TimeoutExpired, EmptyGenerationOutputError)
            )
            elapsed = time.monotonic() - started
            fits = (
                elapsed + timeout + _BUDGET_RETRY_HEADROOM_S
                <= _GENERATION_WALL_BUDGET_S
            )
            if is_parse_failure and not budget_retry_used and fits:
                budget_retry_used = True
                logger.warning(
                    "Chunk parse failure after %.0fs (%s); one budget-permitted "
                    "retry fits under the %ds ceiling — retrying.",
                    elapsed, exc, _GENERATION_WALL_BUDGET_S,
                )
                await asyncio.sleep(1)
                continue
            break
    raise last_error  # type: ignore[misc]


