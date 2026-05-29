"""Claude CLI runners + tool-list normalizer used by setup_generator.

Extracted from ``setup_generator.py`` (Wave 4 decomposition).
Re-exported from ``setup_generator`` for back-compat.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import uuid
from typing import Any

from .config_sync.claude_md_content import SYSTEM_AGENT_CLAUDE_MD  # noqa: F401
from .orchestrator._model_defaults import FALLBACK_MANAGER_MODEL
from ._setup_json import _parse_json_response

logger = logging.getLogger(__name__)


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


_MAX_RETRIES = 2


_STANDARD_TOOL_NAMES = frozenset(
    {"Read", "Write", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"}
)


def _empty_cli_output_error(
    *,
    model: str = "",
    stderr: str = "",
    container_name: str = "",
    probe_succeeded: bool | None = None,
) -> RuntimeError:
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
    return RuntimeError(msg)


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
) -> str:
    """Run a Claude CLI query inside the Docker container."""
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

        result = await asyncio.to_thread(
            subprocess.run,
            [
                "docker", "exec", container_name,
                "bash", "-c",
                f'cat "{user_file}" | claude --print'
                f" --output-format text"
                f" --max-turns 1"
                f" --model {_DEFAULT_GENERATION_MODEL}"
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
) -> dict[str, Any]:
    """Run a single chunk with bounded retries. Returns parsed JSON.

    ``max_retries=0`` is the right call for single-shot UI flows
    (Agents "Create with AI", workstream context-note generator) where
    the user is staring at a spinner and would rather retry by hand
    than wait through hidden retries. The multi-phase setup wizard
    keeps the default of 2 since each chunk is small and the streamed
    progress hides the wait.
    """
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            raw = await _run_claude_cli(
                container_name, system_prompt, user_prompt, timeout=timeout,
            )
            return _parse_json_response(raw)
        except Exception as exc:
            last_error = exc
            if attempt < max_retries:
                logger.warning(
                    "Chunk failed (attempt %d/%d): %s — retrying...",
                    attempt + 1, max_retries + 1, exc,
                )
                await asyncio.sleep(2)
    raise last_error  # type: ignore[misc]


