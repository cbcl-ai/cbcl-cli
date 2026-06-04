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
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# 4-hour timeout: agent sessions can be long-running but should not hang forever.
_SESSION_TIMEOUT_SECONDS = 4 * 3600

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
        The user/task prompt.
    mcp_config:
        MCP server configuration dict (passed via ``--mcp-config``).
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
    cmd = ["docker", "exec"]
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
    # Token-level streaming via stream_event frames with text_delta /
    # content_block_start for tool_use. Only compatible with
    # --print + --output-format=stream-json per the CLI help.
    if include_partial_messages and output_format == "stream-json":
        cmd.append("--include-partial-messages")

    # System prompt — write to a file inside the container's workspace
    # (owned by agent user) to avoid command-line length limits.
    # The file is deleted in the finally block at the end of this
    # generator to prevent accumulation across retries and tasks.
    #
    # P2-A (review): historically this used `subprocess.run("bash -c",
    # "echo {b64} | base64 -d > {path}")`. That:
    # 1. Blocked the event loop for 100-500ms per call (bash spawn +
    #    decode for prompts >100KB).
    # 2. Embedded the encoded prompt in the bash arg list, hitting
    #    OS arg-list limits for very large prompts.
    # 3. Required shell escaping that was hard to keep correct.
    #
    # The replacement uses async ``asyncio.create_subprocess_exec``
    # with ``docker exec -i ... tee {path}`` and pipes the prompt
    # to stdin. No bash, no base64, no event-loop block, no arg-list
    # ceiling.
    prompt_path: str | None = None
    if system_prompt:
        import uuid as _uuid

        prompt_id = _uuid.uuid4().hex[:8]
        prompt_path = f"/workspace/.cubicle/.prompt-{prompt_id}"

        # First: ensure the directory exists. Cheap; one-shot mkdir.
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
            # P2.5-A: bound the post-kill wait. A wedged dockerd
            # could otherwise hang here forever; let the leak go to
            # PID 1 / docker reaper rather than block the worker.
            try:
                await asyncio.wait_for(mkdir_proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                logger.warning(
                    "mkdir reap timed out for %s; abandoning to docker reaper",
                    container_name,
                )
            yield SessionMessage(
                type="error",
                data={"error": "Timeout creating prompt directory in container"},
            )
            return
        if mkdir_proc.returncode != 0:
            logger.error(
                "Failed to mkdir prompt dir in %s (rc=%s): %s",
                container_name, mkdir_proc.returncode,
                (mkdir_err or b"").decode(errors="replace")[:300],
            )
            yield SessionMessage(
                type="error",
                data={"error": "Failed to create prompt directory in container"},
            )
            return

        # Then: stream the prompt to ``tee {path}`` over stdin. ``tee``
        # exits 0 only when the write succeeds; we discard its stdout
        # (it echoes the prompt). Using docker exec -i so the host
        # side keeps stdin open until we close it.
        write_proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", "-u", "agent", container_name,
            "tee", prompt_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, write_err = await asyncio.wait_for(
                write_proc.communicate(input=system_prompt.encode()),
                timeout=30,  # large prompts can take a few seconds
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
            yield SessionMessage(
                type="error",
                data={"error": "Timeout writing system prompt to container"},
            )
            return
        if write_proc.returncode != 0:
            logger.error(
                "Failed to write system prompt file in %s (rc=%s): %s",
                container_name, write_proc.returncode,
                (write_err or b"").decode(errors="replace")[:300],
            )
            yield SessionMessage(
                type="error",
                data={"error": "Failed to write system prompt file to container"},
            )
            return
        cmd.extend(["--system-prompt-file", prompt_path])

    if mcp_config:
        cmd.extend(["--mcp-config", json.dumps(mcp_config)])

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

    # The prompt is the last argument, passed as a POSITIONAL. ``--print``
    # (above) already enables print mode; ``-p`` is just its short alias, so
    # we don't repeat it here. We MUST precede the positional with ``--`` to
    # terminate option parsing — otherwise a prompt whose first line starts
    # with "-" (e.g. a markdown bullet "- SMTP_CREDENTIALS …" pasted by the
    # user) is parsed by the CLI's commander as an unknown option and the
    # whole turn dies with `error: unknown option '- …'`. The CLI reads the
    # positional as the prompt (claude-src getInputPrompt), so this is safe.
    cmd.extend(["--", prompt])

    # Log command for debugging (truncate system prompt)
    cmd_debug = [c if c != system_prompt else f"<system_prompt:{len(system_prompt)}chars>" for c in cmd]
    logger.info(
        "Starting CLI session in container %s (model=%s, tools=%s, cmd_len=%d)",
        container_name, model,
        len(allowed_tools) if allowed_tools else "default",
        len(cmd),
    )
    logger.debug("CLI command: %s", cmd_debug)

    proc: asyncio.subprocess.Process | None = None
    stderr_task: asyncio.Task[None] | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
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
        stderr_chunks: list[str] = []

        async def _read_stderr():
            while True:
                chunk = await proc.stderr.read(65536)
                if not chunk:
                    break
                decoded = chunk.decode(errors="replace")
                stderr_chunks.append(decoded)
                logger.warning("CLI stderr: %s", decoded[:1000].rstrip())
        stderr_task = asyncio.create_task(_read_stderr())

        while True:
            try:
                raw_line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=60,  # 1min read timeout
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
                logger.debug("CLI read timeout but process still alive, continuing...")
                continue
            except asyncio.LimitOverrunError as exc:
                # A single NDJSON line exceeded _STREAM_LIMIT. Drain the
                # offending bytes so the stream can recover and skip this
                # line rather than aborting the whole session.
                logger.warning(
                    "CLI line exceeded %d byte limit (%d bytes consumed); skipping",
                    _STREAM_LIMIT, exc.consumed,
                )
                try:
                    await proc.stdout.readexactly(exc.consumed)
                except (asyncio.IncompleteReadError, Exception) as drain_exc:
                    logger.warning("Failed to drain over-limit line: %s", drain_exc)
                    # Read and discard until newline to resync
                    try:
                        await proc.stdout.readuntil(b"\n")
                    except Exception:
                        break
                continue

            if not raw_line:
                break  # EOF — process closed stdout

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
            stderr_output = "".join(stderr_chunks).strip()

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
        logger.error(
            "CLI session in %s timed out after %ds",
            container_name, _SESSION_TIMEOUT_SECONDS,
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
        # Cancel and await the stderr reader so it doesn't become an
        # orphan task leaking across sessions.
        if stderr_task is not None and not stderr_task.done():
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):
                pass

        # Remove the temporary system-prompt file we wrote to the
        # container. Without this, /workspace/.cubicle/.prompt-* files
        # accumulate across every retry and every task in an office.
        if prompt_path:
            import subprocess as _sp

            try:
                _sp.run(
                    ["docker", "exec", "-u", "agent", container_name,
                     "rm", "-f", prompt_path],
                    timeout=5, capture_output=True,
                )
            except Exception as exc:
                logger.debug(
                    "Failed to remove prompt file %s: %s", prompt_path, exc,
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
