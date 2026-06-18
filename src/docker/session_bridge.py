"""Session Bridge — invokes Claude CLI directly in Docker containers.

Instead of POSTing to an HTTP server inside the container, the communicator
runs ``docker exec`` to invoke the Claude CLI directly.  Results are streamed
back as NDJSON on stdout.

This module provides the bridge function used by agent_worker.py and
manager_controller.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# 4-hour per-attempt wall cap: agent sessions can be long-running but
# should not run forever. T3.2.3 (07/G5, 03/#4): this constant was
# historically DEAD — nothing in the read loop enforced it (the only
# ``except asyncio.TimeoutError`` reachable was the 30s post-EOF wait),
# so a CLI that kept emitting chatty output indefinitely was unbounded.
# It is now checked on every loop iteration; an attempt that exceeds it
# is terminated and classified as a TIMEOUT (the worker retry ladder
# picks it up).
_SESSION_TIMEOUT_SECONDS = 4 * 3600

# How long a single ``stdout.readline()`` waits before we poll process
# liveness + the inactivity/wall deadlines. Module-level so tests can
# shrink it.
_READ_TIMEOUT_SECONDS: float = 60.0

# T3.2.3 (07/G5): output-liveness timeout. A CLI process that stays
# ALIVE but emits NOTHING was previously unbounded — heartbeat PONGs
# are answered inline by agent_worker even while the CLI is wedged,
# and the backend's stale-in_progress sweeper deliberately suppresses
# itself for live+working agents, so a silent worker ran forever.
# After this many seconds of zero CLI output the attempt is terminated
# and classified as a TIMEOUT → the existing retry ladder (resume,
# 3 attempts) → blocked escalation. Mirrors the Manager's 300s
# inactivity timer at worker scale. Env-tunable.
_DEFAULT_INACTIVITY_SECONDS: float = 1200.0


def _inactivity_timeout_seconds() -> float:
    """Resolve the output-liveness timeout (CUBICLE_WORKER_INACTIVITY_SECONDS)."""
    raw = os.environ.get("CUBICLE_WORKER_INACTIVITY_SECONDS", "")
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
            logger.warning(
                "CUBICLE_WORKER_INACTIVITY_SECONDS=%r is not positive — "
                "using default %.0fs", raw, _DEFAULT_INACTIVITY_SECONDS,
            )
        except ValueError:
            logger.warning(
                "CUBICLE_WORKER_INACTIVITY_SECONDS=%r is not a number — "
                "using default %.0fs", raw, _DEFAULT_INACTIVITY_SECONDS,
            )
    return _DEFAULT_INACTIVITY_SECONDS

# asyncio StreamReader buffer limit (default 64KB is too small — a single
# Claude CLI NDJSON line carrying a large tool_result (e.g. Read of a 70KB
# file) can exceed 64KB and trigger LimitOverrunError, aborting the stream).
# 16MB accommodates the largest reasonable tool_result while still bounding
# memory.
_STREAM_LIMIT = 16 * 1024 * 1024


@dataclass
class SessionMessage:
    """A message from the Claude CLI stream."""

    type: str  # "assistant", "result", "error", etc.
    data: dict[str, Any]


def _mask_cmd_for_debug(
    cmd: list[str],
    *,
    system_prompt: str = "",
    prompt: str = "",
) -> list[str]:
    """Return a copy of ``cmd`` safe for DEBUG logging.

    T2.2.1/T2.2.2 (03/#1, 03/#25): the argv itself is now secret-free
    by construction (MCP config rides a container file; the prompt
    rides stdin), but this masker is defense-in-depth — if a future
    change re-inlines either value, the logs still won't carry it:

    * any element equal to the system prompt or the user prompt is
      replaced with a ``<…:Nchars>`` placeholder;
    * an inline-JSON ``--mcp-config`` value (it can embed
      TOOL_PROXY_TOKEN / OFFICE_TOOL_SECRET) is masked whole — only
      the file-path form is loggable.
    """
    masked: list[str] = []
    prev = ""
    for element in cmd:
        if system_prompt and element == system_prompt:
            masked.append(f"<system_prompt:{len(element)}chars>")
        elif prompt and element == prompt:
            masked.append(f"<prompt:{len(element)}chars>")
        elif prev == "--mcp-config" and element.lstrip().startswith("{"):
            masked.append(f"<mcp-config-inline:{len(element)}chars>")
        else:
            masked.append(element)
        prev = element
    return masked


async def _ensure_container_cubicle_dir(container_name: str) -> str | None:
    """``mkdir -p /workspace/.cubicle`` inside the container.

    Returns ``None`` on success, or a short user-facing error string on
    failure (the caller yields it as an ``error`` SessionMessage).
    """
    mkdir_proc = await asyncio.create_subprocess_exec(
        "docker", "exec", "-u", "agent", container_name,
        "mkdir", "-p", "/workspace/.cubicle",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, mkdir_err = await asyncio.wait_for(
            mkdir_proc.communicate(), timeout=10,
        )
    except asyncio.TimeoutError:
        mkdir_proc.kill()
        # P2.5-A: bound the post-kill wait. A wedged dockerd could
        # otherwise hang here forever; let the leak go to PID 1 /
        # docker reaper rather than block the worker.
        try:
            await asyncio.wait_for(mkdir_proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning(
                "mkdir reap timed out for %s; abandoning to docker reaper",
                container_name,
            )
        return "Timeout creating session-file directory in container"
    if mkdir_proc.returncode != 0:
        logger.error(
            "Failed to mkdir session-file dir in %s (rc=%s): %s",
            container_name, mkdir_proc.returncode,
            (mkdir_err or b"").decode(errors="replace")[:300],
        )
        return "Failed to create session-file directory in container"
    return None


async def _write_container_file(
    container_name: str,
    path: str,
    content: str,
    *,
    description: str,
) -> str | None:
    """Stream ``content`` into ``path`` inside the container via
    ``docker exec -i … tee``.

    The content rides the docker-exec client's STDIN — never the host
    argv — so secrets in it (MCP env tokens) are not visible in
    ``ps`` / ``/proc/<pid>/cmdline``. ``tee`` exits 0 only when the
    write succeeds; its stdout (the echoed content) is discarded.

    Returns ``None`` on success, or a short user-facing error string.
    """
    write_proc = await asyncio.create_subprocess_exec(
        "docker", "exec", "-i", "-u", "agent", container_name,
        "tee", path,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, write_err = await asyncio.wait_for(
            write_proc.communicate(input=content.encode()),
            timeout=30,  # large payloads can take a few seconds
        )
    except asyncio.TimeoutError:
        write_proc.kill()
        try:
            await asyncio.wait_for(write_proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning(
                "tee reap timed out for %s; abandoning to docker reaper",
                container_name,
            )
        return f"Timeout writing {description} to container"
    if write_proc.returncode != 0:
        logger.error(
            "Failed to write %s file in %s (rc=%s): %s",
            description, container_name, write_proc.returncode,
            (write_err or b"").decode(errors="replace")[:300],
        )
        return f"Failed to write {description} file to container"
    return None


async def stream_cli_session(
    container_name: str,
    model: str,
    system_prompt: str,
    prompt: str,
    *,
    cwd: str | None = None,
    mcp_config: dict[str, Any] | None = None,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    output_format: str = "stream-json",
    permission_mode: str = "bypassPermissions",
    resume_session: str | None = None,
    max_turns: int | None = None,
    env_overrides: dict[str, str] | None = None,
    secret_env: dict[str, str] | None = None,
    include_partial_messages: bool = False,
    effort: str | None = None,
    settings_json: str | None = None,
) -> AsyncIterator[SessionMessage]:
    """Run a Claude CLI session inside a Docker container via docker exec.

    Builds the ``docker exec ... claude --print ...`` command and streams
    NDJSON results from stdout.

    Parameters
    ----------
    container_name:
        Docker container name (e.g. ``cbcl-office-recruitment``).
    model:
        Claude model to use (e.g. ``claude-sonnet-4-6``).
    system_prompt:
        The system prompt for the session.
    prompt:
        The user/task prompt. Delivered to the CLI over STDIN (never
        argv) so it stays out of host ``ps`` / ``/proc/*/cmdline``.
    mcp_config:
        MCP server configuration dict. Written to a per-session file
        inside the container and passed as ``--mcp-config <path>`` —
        never inline JSON (the env map carries TOOL_PROXY_TOKEN /
        OFFICE_TOOL_SECRET, which must not reach the host argv).
    allowed_tools:
        List of tool names the agent may use.
    disallowed_tools:
        Explicit blocklist (passed to Claude CLI as ``--disallowed-tools``).
        Defense-in-depth complement to the MCP role filter: useful for
        the Manager session, which should never use ``Bash`` or
        ``Task`` (subagent spawn) — even if a prompt injection tries
        to convince it otherwise. Does NOT affect MCP connector tools
        (``mcp__*``) since their naming is namespaced and stable.
    output_format:
        Output format flag (default ``stream-json``).
    permission_mode:
        Permission mode (default ``bypassPermissions``).
    resume_session:
        Session ID to resume (optional).
    max_turns:
        Maximum number of agent turns (optional).
    env_overrides:
        Extra environment variables to set INSIDE the container for this
        CLI invocation (injected as ``docker exec -e KEY=VALUE``). Used by
        the error-recovery retry path to raise limits like
        ``CLAUDE_CODE_MAX_OUTPUT_TOKENS``. Keys/values must be non-empty
        strings; invalid entries are dropped with a warning.
    include_partial_messages:
        Emit ``stream_event`` frames with ``text_delta`` / tool_use starts
        as they arrive (token-level streaming). Only valid together with
        ``output_format="stream-json"`` and ``--print``. The Manager
        session uses this so the UI gets live token streaming instead of
        one message-sized block at the end of the turn.

    Yields
    ------
    SessionMessage
        Parsed messages from the Claude CLI NDJSON stream.
    """
    # ``-i`` keeps the docker-exec client's stdin open: the user/task
    # prompt is delivered over stdin (T2.2.2) instead of as an argv
    # element, so it never shows in host ``ps`` / ``/proc/*/cmdline``.
    cmd = ["docker", "exec", "-i"]
    # Inject per-session env vars BEFORE --workdir/container so they apply
    # to the CLI process. Validate to prevent shell-injection via values.
    if env_overrides:
        for key, value in env_overrides.items():
            if not isinstance(key, str) or not key or not key.isidentifier():
                logger.warning("Dropping invalid env override key: %r", key)
                continue
            if not isinstance(value, str) or not value:
                logger.warning(
                    "Dropping empty env override value for %s", key,
                )
                continue
            # docker exec -e accepts KEY=VALUE. The value is passed
            # literally to execve, so shell metacharacters in it do not
            # reach a shell — safe without quoting.
            cmd.extend(["-e", f"{key}={value}"])

    # Office secrets (and any other sensitive values) are injected via the
    # NAME-ONLY ``docker exec -e KEY`` form: docker forwards the value from
    # THIS process's environment into the container, so the value never
    # appears in the host command line (``ps`` / ``/proc/<pid>/cmdline`` /
    # ``docker inspect``) — only in the container process's environ. This is
    # the same host-only posture the Script Runner uses (NEW-4). The values
    # are merged into the subprocess env at launch (see ``subprocess_env``).
    subprocess_env: dict[str, str] | None = None
    if secret_env:
        subprocess_env = dict(os.environ)
        for key, value in secret_env.items():
            if not isinstance(key, str) or not key or not key.isidentifier():
                logger.warning("Dropping invalid secret env key: %r", key)
                continue
            if not isinstance(value, str) or not value:
                logger.warning("Dropping empty secret value for %s", key)
                continue
            subprocess_env[key] = value
            cmd.extend(["-e", key])  # name only — value rides the env
    if cwd:
        cmd.extend(["--workdir", cwd])
    cmd.extend([
        container_name,
        "stdbuf", "-oL",  # Line-buffered stdout so docker exec streams output
        "claude", "--print",
        "--model", model,
        "--output-format", output_format,
        "--permission-mode", permission_mode,
        "--verbose",
    ])
    # Orchestration flags (item-6): reasoning-effort (``--effort``, opus-tier
    # only) and/or the ``ultracode`` dynamic-workflow setting
    # (``--settings '{"ultracode": true}'``). The session policy decides which
    # land: a plain level -> ``--effort <level>`` only; ``ultracode`` ->
    # BOTH ``--effort xhigh`` AND the ultracode setting (the documented headless
    # recipe — the explicit xhigh is a fallback so an older CLI that ignores the
    # unknown ultracode key still lands xhigh, not default effort); the Manager
    # and plain workers with no effort -> neither (command line byte-for-byte
    # unchanged). A CLI build that doesn't recognise these is handled by the
    # worker retry loop, which drops them and retries (see ``_agent_worker_task``).
    if effort:
        cmd.extend(["--effort", effort])
    if settings_json:
        cmd.extend(["--settings", settings_json])
    # Token-level streaming via stream_event frames with text_delta /
    # content_block_start for tool_use. Only compatible with
    # --print + --output-format=stream-json per the CLI help.
    if include_partial_messages and output_format == "stream-json":
        cmd.append("--include-partial-messages")

    # Session files — system prompt AND MCP config are written to files
    # inside the container's workspace (owned by the agent user) instead
    # of riding the argv. Rationale:
    #
    # * System prompt (P2-A): avoids OS arg-list limits for very large
    #   prompts; historically this used a bash/base64 pipeline that
    #   blocked the event loop — the current mechanism streams the
    #   content over the docker-exec client's stdin via ``tee``.
    # * MCP config (T2.2.1, 03/#1 P0): the config's env map embeds
    #   TOOL_PROXY_TOKEN and OFFICE_TOOL_SECRET. Passing the JSON
    #   inline as one argv element made both secrets world-readable on
    #   the host (``ps`` / ``/proc/<pid>/cmdline``) for the entire CLI
    #   session and logged them whole at DEBUG. In-container
    #   readability of the file is acceptable — the same secrets
    #   already sit in the MCP server process's env; the goal is
    #   removing host-argv + log exposure.
    #
    # Both files are deleted in the finally block at the end of this
    # generator to prevent accumulation across retries and tasks.
    prompt_path: str | None = None
    mcp_config_path: str | None = None
    if system_prompt or mcp_config:
        dir_error = await _ensure_container_cubicle_dir(container_name)
        if dir_error:
            yield SessionMessage(type="error", data={"error": dir_error})
            return

    if system_prompt:
        import uuid as _uuid

        prompt_id = _uuid.uuid4().hex[:8]
        prompt_path = f"/workspace/.cubicle/.prompt-{prompt_id}"
        write_error = await _write_container_file(
            container_name, prompt_path, system_prompt,
            description="system prompt",
        )
        if write_error:
            yield SessionMessage(type="error", data={"error": write_error})
            return
        cmd.extend(["--system-prompt-file", prompt_path])

    if mcp_config:
        import uuid as _uuid

        mcp_id = _uuid.uuid4().hex[:8]
        mcp_config_path = f"/workspace/.cubicle/.mcp-{mcp_id}.json"
        write_error = await _write_container_file(
            container_name, mcp_config_path, json.dumps(mcp_config),
            description="MCP config",
        )
        if write_error:
            yield SessionMessage(type="error", data={"error": write_error})
            return
        # Path form ONLY — never inline JSON (see the rationale above;
        # test_session_bridge_argv_hygiene.py locks this).
        cmd.extend(["--mcp-config", mcp_config_path])

    if allowed_tools:
        cmd.extend(["--allowed-tools", ",".join(allowed_tools)])

    if disallowed_tools:
        # CLI accepts the same comma-separated form as --allowed-tools.
        # Used by the Manager to block Bash + Task even though the MCP
        # role filter already excludes them — belt-and-braces.
        cmd.extend(["--disallowed-tools", ",".join(disallowed_tools)])

    if resume_session:
        cmd.extend(["--resume", resume_session])

    if max_turns:
        cmd.extend(["--max-turns", str(max_turns)])

    # The prompt is delivered via STDIN (T2.2.2, 03/#25) — ``claude
    # --print`` reads the prompt from stdin when no positional argument
    # is given. The old ``cmd.extend(["--", prompt])`` positional form
    # exposed the full task brief / activity history / user-pasted text
    # in host ``ps`` / ``/proc/*/cmdline`` for the whole session and
    # re-opened the ARG_MAX ceiling for long prompts. Stdin also makes
    # the dash-leading-prompt CLI bug (2026-06-04, "- SMTP_CREDENTIALS
    # …" parsed as an unknown option) structurally impossible — stdin
    # content never reaches the option parser. The feed happens right
    # after the subprocess is spawned (see ``_feed_prompt`` below).

    # Log command for debugging. The argv is secret-free by
    # construction now; ``_mask_cmd_for_debug`` is the defense-in-depth
    # backstop if a future change re-inlines the prompt or MCP JSON.
    cmd_debug = _mask_cmd_for_debug(
        cmd, system_prompt=system_prompt, prompt=prompt,
    )
    logger.info(
        "Starting CLI session in container %s (model=%s, tools=%s, cmd_len=%d)",
        container_name, model,
        len(allowed_tools) if allowed_tools else "default",
        len(cmd),
    )
    logger.debug("CLI command: %s", cmd_debug)

    proc: asyncio.subprocess.Process | None = None
    stderr_task: asyncio.Task[None] | None = None
    stdin_task: asyncio.Task[None] | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LIMIT,
            # When secret_env is set, the docker-exec client process needs
            # those values in its OWN env so ``-e KEY`` (name-only) forwards
            # them into the container without exposing them on the argv.
            env=subprocess_env,
        )

        assert proc.stdout is not None
        assert proc.stderr is not None

        # Feed the prompt over stdin in a background task (T2.2.2). A
        # background task — not an inline drain — so a CLI that starts
        # writing stdout before fully reading stdin can't deadlock us
        # (we'd be blocked on drain while its stdout pipe fills).
        # Closing stdin signals EOF — the CLI then has the complete
        # prompt. ``getattr`` keeps test fakes without a ``stdin``
        # attribute working.
        stdin_writer = getattr(proc, "stdin", None)
        if stdin_writer is not None:

            async def _feed_prompt() -> None:
                try:
                    stdin_writer.write(prompt.encode())
                    await stdin_writer.drain()
                except (
                    BrokenPipeError, ConnectionResetError, RuntimeError,
                ) as exc:
                    logger.warning(
                        "Failed to deliver prompt via stdin to %s: %s",
                        container_name, exc,
                    )
                finally:
                    try:
                        stdin_writer.close()
                    except Exception:  # noqa: BLE001 — best-effort close
                        pass

            stdin_task = asyncio.create_task(_feed_prompt())

        # Log the full command for debugging
        logger.info("CLI PID: %s", proc.pid)

        # Start stderr reader in background. Tracked so we can cancel it
        # in the finally block — otherwise it becomes an orphan task.
        # Chunks are accumulated into a shared buffer so the non-zero
        # exit path can include stderr context in the emitted error
        # payload — without this, a second proc.stderr.read() at that
        # site returns empty (already drained) and the downstream
        # classifier ends up with the contentless
        # "Claude CLI exited with code N" string, which never matches
        # any known error pattern.
        # T8.3.6 (03/#23): bound the retained stderr — a chatty CLI could
        # accumulate unbounded memory for the whole session. Keep the HEAD (up
        # to a byte budget — the root-cause error is usually first) plus a
        # rolling TAIL (recent lines). Fatal CLI errors are far under 64KB so
        # they sit fully in the head; truncation only drops the verbose middle.
        # (The "exited with code N" string is synthesized separately into the
        # error payload, not part of this stderr stream.)
        _STDERR_HEAD_BUDGET = 64 * 1024
        _STDERR_TAIL_CHUNKS = 8
        stderr_head: list[str] = []
        stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_CHUNKS)
        _stderr_state = {"head_bytes": 0, "truncated": False}

        async def _read_stderr():
            while True:
                chunk = await proc.stderr.read(65536)
                if not chunk:
                    break
                decoded = chunk.decode(errors="replace")
                if _stderr_state["head_bytes"] < _STDERR_HEAD_BUDGET:
                    stderr_head.append(decoded)
                    _stderr_state["head_bytes"] += len(decoded)
                else:
                    stderr_tail.append(decoded)
                    _stderr_state["truncated"] = True
                logger.warning("CLI stderr: %s", decoded[:1000].rstrip())
        stderr_task = asyncio.create_task(_read_stderr())

        # T3.2.3: per-attempt deadlines. ``last_output_at`` advances on
        # every stdout line; the inactivity check fires in the read-
        # timeout branch (a silent-but-alive CLI), while the wall-cap
        # check runs every iteration (an endlessly-CHATTY CLI never
        # hits the read timeout, so the cap must live in the hot loop).
        inactivity_limit = _inactivity_timeout_seconds()
        session_started_at = time.monotonic()
        last_output_at = session_started_at

        async def _terminate_proc() -> None:
            """Terminate (then kill) the CLI subprocess; bounded reap."""
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    logger.warning(
                        "CLI reap timed out for %s; abandoning to "
                        "docker reaper", container_name,
                    )

        while True:
            elapsed = time.monotonic() - session_started_at
            if elapsed >= _SESSION_TIMEOUT_SECONDS:
                # Per-attempt wall cap (the previously-dead
                # _SESSION_TIMEOUT_SECONDS, now real). Catches the
                # infinite-chatty-loop case the inactivity timer
                # cannot see.
                logger.error(
                    "CLI session in %s exceeded the per-attempt "
                    "wall-clock cap (%.0fs >= %ds) — terminating",
                    container_name, elapsed, _SESSION_TIMEOUT_SECONDS,
                )
                await _terminate_proc()
                yield SessionMessage(
                    type="error",
                    data={
                        "error": (
                            f"Session timed out: exceeded the "
                            f"per-attempt wall-clock cap of "
                            f"{_SESSION_TIMEOUT_SECONDS}s"
                        ),
                    },
                )
                return

            try:
                raw_line = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=_READ_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                # Actively poll process status (returncode isn't updated automatically)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass  # Process is still alive
                if proc.returncode is not None:
                    logger.info("CLI process exited (rc=%s) during read timeout", proc.returncode)
                    break
                # T3.2.3 (07/G5): output-liveness deadline. The process
                # is alive but silent; once the silence window exceeds
                # the inactivity limit, terminate the attempt and emit
                # a TIMEOUT-classifiable error so the worker's retry
                # ladder (resume → 3 attempts → blocked escalation)
                # takes over instead of running unbounded.
                silence = time.monotonic() - last_output_at
                if silence >= inactivity_limit:
                    logger.error(
                        "CLI session in %s produced no output for "
                        "%.0fs (>= inactivity timeout %.0fs) — "
                        "terminating the attempt",
                        container_name, silence, inactivity_limit,
                    )
                    await _terminate_proc()
                    yield SessionMessage(
                        type="error",
                        data={
                            "error": (
                                f"Session timed out: CLI produced no "
                                f"output for {int(silence)}s "
                                f"(inactivity timeout "
                                f"{int(inactivity_limit)}s)"
                            ),
                        },
                    )
                    return
                logger.debug("CLI read timeout but process still alive, continuing...")
                continue
            except ValueError as exc:
                # T8.1.2 (03/#7): a single NDJSON line exceeded the
                # StreamReader limit. CPython's ``readline()`` does NOT raise
                # ``LimitOverrunError`` to the caller — it CLEARS the buffer
                # and raises ``ValueError`` ("Separator is not found, and chunk
                # exceed the limit"). The old ``except asyncio.LimitOverrunError``
                # branch was therefore DEAD: the ValueError fell through to the
                # generic ``except Exception`` and aborted the whole session
                # ("Session error") instead of the documented skip-and-continue.
                # The buffer is already cleared, so we simply skip this oversized
                # line and continue with the next.
                logger.warning(
                    "CLI line exceeded the %d-byte stream limit; skipping it "
                    "(%s)", _STREAM_LIMIT, exc,
                )
                continue

            if not raw_line:
                break  # EOF — process closed stdout

            # Any stdout data counts as liveness — refresh the
            # inactivity clock (T3.2.3).
            last_output_at = time.monotonic()

            # W5-P2-H1: ``errors="replace"`` so a malformed UTF-8 byte
            # in the Claude CLI's NDJSON stream (e.g. a tool result
            # that smuggled binary, a buggy emoji) substitutes
            # U+FFFD instead of crashing the streaming reader. The
            # JSON parse below catches the resulting line cleanly.
            line = raw_line.decode(errors="replace").strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                msg_type = data.get("type", "unknown")
                yield SessionMessage(type=msg_type, data=data)
            except json.JSONDecodeError:
                logger.debug("Non-JSON output from CLI: %s", line[:200])

        # Wait for process to complete
        await asyncio.wait_for(proc.wait(), timeout=30)

        # Check exit code
        if proc.returncode and proc.returncode != 0:
            # Wait briefly for the stderr reader to drain any final chunks
            # so we include them in the emitted error payload. The reader
            # exits on EOF once the subprocess closes its stderr.
            if stderr_task is not None and not stderr_task.done():
                try:
                    await asyncio.wait_for(stderr_task, timeout=2)
                except (asyncio.TimeoutError, Exception):
                    # Best-effort — whatever chunks are already buffered
                    # are better than nothing.
                    pass
            _parts = list(stderr_head)
            if _stderr_state["truncated"]:
                _parts.append("\n…[stderr truncated — middle dropped]…\n")
            _parts.extend(stderr_tail)
            stderr_output = "".join(_parts).strip()

            logger.warning(
                "CLI session exited with code %d: %s",
                proc.returncode, stderr_output[:500],
            )
            yield SessionMessage(
                type="error",
                data={
                    "error": f"Claude CLI exited with code {proc.returncode}",
                    "stderr": stderr_output[:4000],
                    "exit_code": proc.returncode,
                },
            )

    except asyncio.TimeoutError:
        # Only reachable from the 30s post-EOF ``proc.wait()`` above —
        # the CLI closed stdout but the process refused to exit. (The
        # inactivity + wall-cap deadlines are handled inline in the
        # read loop; the old log line here misleadingly blamed
        # _SESSION_TIMEOUT_SECONDS, which never governed this branch.)
        logger.error(
            "CLI process in %s did not exit within 30s after closing "
            "stdout — terminating",
            container_name,
        )
        if proc:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()
        yield SessionMessage(
            type="error",
            data={"error": "Session timed out"},
        )

    except asyncio.CancelledError:
        logger.info("CLI session in %s cancelled", container_name)
        if proc:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                proc.kill()
        raise

    except Exception as exc:
        logger.exception("CLI session error in %s: %s", container_name, exc)
        if proc and proc.returncode is None:
            proc.terminate()
        yield SessionMessage(
            type="error",
            data={"error": f"Session error: {exc}"},
        )

    finally:
        # T8.1.1 (03/#5): terminate a still-running CLI on EVERY exit path —
        # including GeneratorExit, which is a BaseException that bypasses both
        # the `except asyncio.CancelledError` and `except Exception` handlers
        # above and lands straight here. The concrete in-tree trigger is the
        # mid-attempt wall-clock AgentErrorEscalation raised inside the
        # consumer's `async for` (it abandons this generator at the yield).
        # Without this the in-container `claude` keeps executing (writing
        # files, calling tools) while the task is escalated to blocked.
        # Idempotent with the except-handler terminations (returncode is set
        # once it exits, so we skip).
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    proc.kill()
            except Exception as exc:  # noqa: BLE001 — best-effort reap
                logger.warning(
                    "Failed to terminate CLI proc in %s during cleanup: %s",
                    container_name, exc,
                )

        # Cancel and await the stderr reader so it doesn't become an
        # orphan task leaking across sessions.
        if stderr_task is not None and not stderr_task.done():
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):
                pass

        # Same for the stdin prompt feeder (normally already done by
        # the time the stream ends; cancellation covers error paths).
        if stdin_task is not None and not stdin_task.done():
            stdin_task.cancel()
            try:
                await stdin_task
            except (asyncio.CancelledError, Exception):
                pass

        # Remove the temporary session files we wrote to the container
        # (system prompt + MCP config). Without this,
        # /workspace/.cubicle/.prompt-* and .mcp-*.json files accumulate
        # across every retry and every task in an office — and the MCP
        # config carries the tool-proxy token, so stale copies extend
        # its in-container exposure window.
        session_files = [p for p in (prompt_path, mcp_config_path) if p]
        if session_files:
            import subprocess as _sp

            try:
                _sp.run(
                    ["docker", "exec", "-u", "agent", container_name,
                     "rm", "-f", *session_files],
                    timeout=5, capture_output=True,
                )
            except Exception as exc:
                logger.debug(
                    "Failed to remove session files %s: %s",
                    session_files, exc,
                )


async def check_container_health(container_name: str) -> dict:
    """Check if the container is running and Claude CLI is available."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", container_name,
            "claude", "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LIMIT,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)

        if proc.returncode == 0:
            version = stdout.decode().strip()
            return {"status": "healthy", "claude_version": version}
        return {
            "status": "unhealthy",
            "error": stderr.decode().strip()[:200],
        }
    except asyncio.TimeoutError:
        return {"status": "unhealthy", "error": "Health check timed out"}
    except Exception as exc:
        return {"status": "unreachable", "error": str(exc)}


async def probe_cli_versions(container_name: str) -> dict:
    """Return the Claude CLI + bundled-SDK versions inside a container.

    Two distinct version surfaces (see ``opus-48-audit.md`` C1):

    * ``cli_version`` — the raw ``claude --version`` output. The CLI
      binary's OWN version string; human-facing.
    * ``sdk_version`` — the installed ``claude-agent-sdk`` package
      version (via ``importlib.metadata``). This is the value that's
      comparable to PyPI, so the backend uses it to decide whether an
      upgrade is available. The ``claude`` binary is a symlink into this
      package's ``_bundled/`` dir, so the SDK version is the real
      upgrade lever.

    Either field is ``None`` if its probe fails — the backend treats
    unknown versions conservatively (never claims "out of date").
    """
    cli_version: str | None = None
    sdk_version: str | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", container_name,
            "claude", "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LIMIT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode == 0:
            cli_version = stdout.decode().strip() or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("cli --version probe failed for %s: %s", container_name, exc)

    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", container_name,
            "python3", "-c",
            "import importlib.metadata as m; "
            "print(m.version('claude-agent-sdk'))",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LIMIT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode == 0:
            sdk_version = stdout.decode().strip() or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("sdk version probe failed for %s: %s", container_name, exc)

    return {
        "cli_version": cli_version,
        "sdk_version": sdk_version,
        "container_name": container_name,
    }


async def upgrade_cli(container_name: str) -> dict:
    """Upgrade the bundled Claude CLI inside a container, in place.

    The ``claude`` binary is a symlink into the ``claude-agent-sdk``
    package's ``_bundled/`` dir (see ``Dockerfile.agent``), so upgrading
    = ``pip install -U claude-agent-sdk`` + re-point the symlink.

    Audit-driven specifics:

    * Runs as ``-u root`` (H1) — the container's runtime user is the
      non-root ``agent``, which can't write site-packages or
      ``/usr/local/bin``.
    * Re-resolves the bundled binary path the SAME way the Dockerfile
      does (blocker 3) instead of hardcoding ``_bundled/claude``, so a
      future SDK that relocates the binary still works.
    * Verifies with ``claude --version`` after; on failure the previous
      symlink target still exists (we change nothing destructive), so we
      just report ``ok=False``.

    Returns ``{ok, cli_version, sdk_version, message}``.
    """
    # Same resolver the Dockerfile uses. Passed as a single argv element
    # to ``python3 -c`` — no shell is involved (create_subprocess_exec),
    # so the inner single quotes are literal Python and safe.
    _resolver = (
        "import claude_agent_sdk, pathlib; "
        "print(pathlib.Path(claude_agent_sdk.__file__).parent "
        "/ '_bundled' / 'claude')"
    )

    async def _run(args: list[str], timeout: float) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=_STREAM_LIMIT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            # Reap the killed child so it doesn't linger as a zombie /
            # trip an "Exception ignored / subprocess still running"
            # warning — matches the reap pattern used elsewhere in this
            # file's timeout paths.
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:  # noqa: BLE001 — best-effort reap
                pass
            return 124, "timed out"
        return proc.returncode or 0, out.decode(errors="replace").strip()

    # 1. Upgrade the package. NO ``--no-cache-dir``: this is a runtime
    # ``docker exec`` into a long-lived container (not an image build),
    # and the auto-upgrade-on-connect path runs this on every cbcl
    # start. Keeping pip's cache makes the already-latest / repeat case
    # fast (a metadata check, no re-download) instead of re-fetching the
    # wheel every connect.
    rc, out = await _run(
        ["docker", "exec", "-u", "root", container_name,
         "pip", "install", "-U", "claude-agent-sdk"],
        timeout=150,
    )
    if rc != 0:
        return {
            "ok": False,
            "message": f"pip upgrade failed: {out[-500:]}",
        }

    # 2. Resolve the (possibly relocated) bundled binary path.
    rc, resolved = await _run(
        ["docker", "exec", "-u", "root", container_name,
         "python3", "-c", _resolver],
        timeout=15,
    )
    if rc != 0 or not resolved:
        return {
            "ok": False,
            "message": f"could not resolve bundled CLI path: {resolved[-300:]}",
        }

    # 3. Re-point the symlink.
    rc, out = await _run(
        ["docker", "exec", "-u", "root", container_name,
         "ln", "-sf", resolved, "/usr/local/bin/claude"],
        timeout=15,
    )
    if rc != 0:
        return {"ok": False, "message": f"re-symlink failed: {out[-300:]}"}

    # 4. Verify the upgraded CLI runs + report new versions.
    versions = await probe_cli_versions(container_name)
    if not versions.get("cli_version"):
        return {
            "ok": False,
            "message": "upgrade ran but `claude --version` failed afterwards",
            **versions,
        }
    return {"ok": True, "message": "upgraded", **versions}


async def wait_for_container_healthy(
    container_name: str,
    timeout: float = 30.0,
    poll_interval: float = 2.0,
) -> bool:
    """Wait for the container to be healthy (Claude CLI available).

    Returns True if healthy within timeout, False otherwise.
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = await check_container_health(container_name)
        if result.get("status") == "healthy":
            return True
        await asyncio.sleep(poll_interval)
    logger.warning(
        "Container %s not healthy after %.0fs", container_name, timeout
    )
    return False
