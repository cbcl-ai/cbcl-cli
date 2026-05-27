"""Daemon management — foreground/background execution and process helpers."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import NamedTuple

import click

from src.config import Config, OfficeConfig, fetch_offices, set_api_key
from src.docker.container_manager import ContainerManager
from src.handlers import ProcessModelOfficeComponents
from src.paths import get_logs_path, get_pid_path

logger = logging.getLogger("cbcl")

# How often to poll for new offices (seconds)
OFFICE_POLL_INTERVAL = int(os.environ.get("CBCL_OFFICE_POLL_INTERVAL", "15"))



class ProcessModelComponents(NamedTuple):
    """Components created for one office in process-per-agent mode.

    Only contains the components the daemon needs for lifecycle management
    (startup, shutdown, polling).  The full component set lives in
    ``ProcessModelOfficeComponents`` in handlers.py.

    ``office_name`` is captured at connect time so the disconnect path
    can derive the workspace slug WITHOUT depending on the manager's
    sync_config (which may never have arrived if the office failed
    early). Used for the office-secrets host-file cleanup in
    ``_disconnect_office_process_model``.
    """

    supervisor: object  # AgentSupervisor
    dispatcher: object  # TaskDispatcher
    router: object  # TransportClient (WsTransport)
    reporter: object  # HealthReporter
    script_runner: object  # ScriptRunner
    watchdog_task: asyncio.Task | None
    queue_manager: object | None  # AgentQueueManager
    tool_proxy: object | None  # ToolProxyServer (WS mode only)
    office_name: str  # captured at connect time for slug derivation


def _start_foreground(config: Config) -> None:
    """Run the Communicator in the foreground (logs to stdout).

    Writes the same PID file as the daemon path so ``cbcl status``,
    ``cbcl stop``, and ``cbcl logs`` work whether the daemon was
    started in the foreground (the default) or via ``cbcl start
    --daemon``. Before this, foreground starts left no PID file
    behind and every status / stop call would report "Not running"
    even with the daemon happily handling traffic — operators ran
    into this in tmux / screen setups where they couldn't reach the
    foreground terminal to Ctrl+C.

    Refuses to start if a PID file already names a live process,
    mirroring the daemon path's collision check.
    """
    pid_path = get_pid_path()

    # Collision check — same posture as ``_start_daemon``.
    if pid_path.exists():
        existing_pid = _read_pid(pid_path)
        if existing_pid and _is_process_running(existing_pid):
            click.echo(f"Communicator already running (PID {existing_pid})")
            sys.exit(1)
        # Stale PID file — clean it up before we write our own.
        pid_path.unlink(missing_ok=True)

    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))
    os.chmod(str(pid_path), 0o600)

    _setup_logging_foreground()

    click.echo("Starting Communicator...")
    click.echo(f"  Platform:       {config.platform_url}")
    click.echo(f"  Execution mode: process-per-agent")
    click.echo("  Offices:        (auto-discovered from platform)")

    try:
        asyncio.run(_run_process_model(config))
    except KeyboardInterrupt:
        pass
    finally:
        # Symmetric with the daemon path. Clean up on every exit
        # path — Ctrl+C, KeyboardInterrupt, normal shutdown — so
        # the next ``cbcl start`` doesn't trip the collision check.
        pid_path.unlink(missing_ok=True)


def _start_daemon(config: Config) -> None:
    """Fork to background and run as a daemon."""
    pid_path = get_pid_path()

    # Check if already running
    if pid_path.exists():
        existing_pid = _read_pid(pid_path)
        if existing_pid and _is_process_running(existing_pid):
            click.echo(f"Communicator already running (PID {existing_pid})")
            sys.exit(1)
        # Stale PID file
        pid_path.unlink(missing_ok=True)

    # Fork to background
    pid = os.fork()
    if pid > 0:
        # Parent process — report and exit
        click.echo(f"Communicator started in background (PID {pid})")
        click.echo(f"Logs: {get_logs_path() / 'communicator.log'}")
        click.echo("Stop: cbcl stop")
        sys.exit(0)

    # Child process (daemon)
    os.setsid()

    # Write PID file with restricted permissions
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))
    os.chmod(str(pid_path), 0o600)

    # Set up logging to file with rotation
    _setup_logging_daemon()

    # Redirect daemon stdout/stderr to /dev/null
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, sys.stdout.fileno())
    os.dup2(devnull, sys.stderr.fileno())
    os.close(devnull)

    logger.info(
        "Communicator daemon started (PID %d, platform=%s)",
        os.getpid(),
        config.platform_url,
    )

    try:
        asyncio.run(_run_process_model(config))
    finally:
        pid_path.unlink(missing_ok=True)


_TOKEN_REVOKED_FLAG_PATH = None  # set lazily on first use, see below


async def _discover_offices(
    platform_url: str, security_token: str | None = None,
) -> list[OfficeConfig]:
    """Fetch offices from the platform, with error handling.

    Attaches the Communicator's Company Token so the Bearer-authed
    discovery endpoint accepts the call. Without the token the platform
    returns 401 (see CLI-010).

    A 401 specifically is treated as "token revoked or wrong Company"
    and surfaces a loud, single-line warning + drops a marker file
    under ~/.cubicle/ so ``cbcl status`` can flag the state. The
    daemon DOES NOT crash on revocation — running office WSes keep
    serving their offices on their existing connections (handshake
    auth was already passed) until the user runs ``cbcl setup``
    with a fresh token. Other transport errors are still rate-
    limited via a counter so we don't fill the log on a flapping
    network.
    """
    import httpx

    from src.utils import describe_exception

    try:
        offices = await fetch_offices(platform_url, security_token)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            _mark_token_revoked()
            logger.error(
                "Discovery returned 401 — the Company Token has been "
                "revoked or belongs to a different Company. Run "
                "'cbcl setup' with a fresh token from Company "
                "Settings > Tokens. Existing offices stay connected "
                "until you restart the daemon.",
            )
        else:
            logger.error(
                "Failed to discover offices: %s", describe_exception(exc),
            )
        return []
    except Exception as exc:
        logger.error(
            "Failed to discover offices: %s", describe_exception(exc),
        )
        return []
    # Successful discovery clears the revoked-flag if it was set.
    # (Operator could have re-paired with a fresh token without
    # bouncing the daemon — fetch_offices working again proves the
    # token is good now.)
    _clear_token_revoked()
    return offices


def _token_revoked_flag() -> "object":
    """Resolve the marker-file path lazily.

    Resolved on first use to avoid import-time filesystem touches
    that would surprise unit tests of unrelated modules in this
    package.
    """
    global _TOKEN_REVOKED_FLAG_PATH
    if _TOKEN_REVOKED_FLAG_PATH is None:
        from pathlib import Path
        from src.paths import CUBICLE_HOME

        _TOKEN_REVOKED_FLAG_PATH = Path(CUBICLE_HOME) / ".token_revoked"
    return _TOKEN_REVOKED_FLAG_PATH


# One-shot WARN log for marker write failures. The marker is
# observability only — we don't want the daemon to crash on a
# read-only home dir, but we DO want the operator to see one
# breadcrumb that the token-revoked surface is silently broken.
# After the first warn, subsequent attempts log at debug to avoid
# spamming the log on every poll (default 15s) when the underlying
# issue isn't going away.
_marker_failure_warned: bool = False


def _mark_token_revoked() -> None:
    global _marker_failure_warned
    try:
        path = _token_revoked_flag()
        path.touch(exist_ok=True)
    except Exception:
        if not _marker_failure_warned:
            _marker_failure_warned = True
            logger.warning(
                "Failed to write token-revoked marker — "
                "'cbcl status' will not surface the revoked-token "
                "warning. Subsequent failures are logged at debug.",
                exc_info=True,
            )
        else:
            logger.debug(
                "Failed to write token-revoked marker (suppressed)",
                exc_info=True,
            )


def _clear_token_revoked() -> None:
    try:
        path = _token_revoked_flag()
        path.unlink(missing_ok=True)
    except Exception:
        logger.debug("Failed to clear token-revoked marker", exc_info=True)


# ---------------------------------------------------------------------------
# Process-per-agent model (the only supported mode)
# ---------------------------------------------------------------------------


async def _connect_redis(config: Config):
    """Return a Redis-compatible client for the daemon.

    Default is in-process ``FakeRedis`` (see ``src.local_redis``) —
    NO external service required, NO host ports opened, NO
    additional containers/processes spawned. The user's daemon host
    stays untouched outside the office containers.

    When ``config.redis_url`` is a non-empty real Redis URL, that
    URL is honoured instead (multi-host escape hatch). The connect
    attempt retries 5 times before giving up.
    """
    from src.local_redis import get_redis_client

    redis_url = (getattr(config, "redis_url", None) or "").strip()

    if not redis_url:
        # In-process path. No retries needed — FakeRedis can't fail
        # to start; it's a Python object.
        client = await get_redis_client(None)
        return client

    # External Redis path. Retry on connection failure.
    for attempt in range(5):
        try:
            return await get_redis_client(redis_url)
        except Exception as exc:
            if attempt < 4:
                logger.warning(
                    "External Redis at %s not ready (attempt %d/5): %s",
                    redis_url, attempt + 1, exc,
                )
                await asyncio.sleep(2)
            else:
                raise


async def _run_process_model(config: Config) -> None:
    """Main async loop using process-per-agent model."""
    set_api_key(config.anthropic_api_key)

    # Create the ContainerManager up-front for office-container
    # lifecycle. Tests patch ``src.daemon.ContainerManager`` directly
    # — keep this as the single instantiation site so the patch is
    # observed.
    containers = ContainerManager(use_docker=True)

    # Connect to Redis. Default is in-process FakeRedis — no host
    # services spawned. See ``_connect_redis`` for the escape hatch
    # to an external Redis via ``config.redis_url``.
    redis_client = await _connect_redis(config)

    connected: dict[str, ProcessModelComponents] = {}
    # In-flight set: office_ids whose connect coroutine has STARTED
    # but hasn't yet populated ``connected``. Used as a mutex between
    # the create-consumer (`office_created` push) and the poll-loop
    # discovery — both could otherwise observe ``office_id not in
    # connected`` simultaneously and double-connect, leaving an
    # orphan ProcessModelComponents whose container, WS, and agent
    # subprocesses leak. Whoever adds first wins; the other skips.
    # Membership is added BEFORE the first await in
    # ``_connect_office_process_model`` and removed in its
    # finally clause regardless of success/failure.
    connecting: set[str] = set()
    background_tasks: list[asyncio.Task] = []
    poll_task: asyncio.Task | None = None
    # Daemon-level fan-in for ``office_deleted`` push notifications.
    # Per-office routers enqueue here when the backend pushes the
    # delete; the consumer below runs the teardown out of the
    # router's own callback context (the router's ``stop`` would
    # otherwise deadlock against the callback that's calling it).
    #
    # ``maxsize=1000`` is essentially unbounded for a sane single-
    # user setup (events arrive at the rate the user clicks Delete
    # in the UI — at most a handful per minute). The cap exists so
    # the QueueFull guards in the producer handlers
    # (``handlers.py::_handle_office_deleted/_created``) aren't
    # dead code: a runaway producer eventually trips the cap, the
    # producer logs ERROR and falls back to the poll-loop, and the
    # daemon process keeps running rather than OOMing on the queue.
    delete_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
    delete_consumer_task: asyncio.Task | None = None
    # Symmetric queue for ``office_created`` broadcasts — backend
    # blasts the event on every connected WS so the FIRST router
    # to receive it enqueues here. Consumer connects the new
    # office immediately, replacing the 15s poll-loop latency
    # with ~1s of WS round-trip + connect work. Same cap as above.
    create_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1000)
    create_consumer_task: asyncio.Task | None = None

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    _shutdown_count = 0

    def _signal_handler() -> None:
        nonlocal _shutdown_count
        _shutdown_count += 1
        if _shutdown_count > 1:
            logger.warning("Forced exit (second signal)")
            sys.exit(1)
        logger.info("Shutdown signal received")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    try:
        await containers.ensure_image()

        offices = await _discover_offices(config.platform_url, config.security_token)
        logger.info("Discovered %d office(s)", len(offices))

        for office in offices:
            # Sequential startup: nothing else racing yet (poll
            # task and consumers haven't started). Pass
            # ``connecting`` for symmetry; it's a no-op when no
            # parallel callers exist.
            connecting.add(office.id)
            try:
                await _connect_office_process_model(
                    office, config, containers, redis_client,
                    connected, background_tasks,
                    delete_queue=delete_queue,
                    create_queue=create_queue,
                    connecting=connecting,
                )
            finally:
                # `_connect_office_process_model` removes itself in
                # its finally clause, but discard is idempotent.
                connecting.discard(office.id)

        background_tasks.append(
            asyncio.create_task(containers.health_check_all())
        )

        # Consumer for proactive ``office_deleted`` pushes — see
        # ``_handle_office_deleted`` in handlers.py for the producer
        # side and ``_disconnect_office_process_model`` for what
        # gets done.
        delete_consumer_task = asyncio.create_task(
            _consume_office_deletes(
                delete_queue, connected, containers, redis_client,
                shutdown_event,
            ),
        )

        # Symmetric consumer for ``office_created`` broadcasts —
        # see ``_handle_office_created`` in handlers.py.
        create_consumer_task = asyncio.create_task(
            _consume_office_creates(
                create_queue, config, containers, redis_client,
                connected, connecting, background_tasks,
                shutdown_event,
                delete_queue=delete_queue,
            ),
        )

        poll_task = asyncio.create_task(
            _poll_for_new_offices_process_model(
                config, containers, redis_client,
                connected, connecting, background_tasks,
                shutdown_event,
                delete_queue=delete_queue,
                create_queue=create_queue,
            )
        )

        await shutdown_event.wait()

    finally:
        logger.info("Shutting down (process model)...")

        # Phase 1: Stop accepting work
        for oc in connected.values():
            try:
                await oc.router.stop()
            except Exception as exc:
                logger.debug("Router stop error: %s", exc)
            try:
                await oc.dispatcher.stop()
            except Exception as exc:
                logger.debug("Dispatcher stop error: %s", exc)
            if oc.watchdog_task:
                oc.watchdog_task.cancel()
            oc.reporter.stop()

        # Phase 2: Graceful agent shutdown (30s per office)
        for oc in connected.values():
            try:
                await oc.supervisor.shutdown(timeout=30)
            except Exception as exc:
                logger.warning("Supervisor shutdown error: %s", exc)

        # Phase 3: Flush and cleanup
        for oc in connected.values():
            try:
                await oc.script_runner.shutdown()
            except Exception as exc:
                logger.debug("Script runner shutdown error: %s", exc)
            if oc.tool_proxy is not None:
                try:
                    await oc.tool_proxy.stop()
                except Exception as exc:
                    logger.debug("Tool proxy shutdown error: %s", exc)

        # Phase 3b: Clear presence keys so UI shows disconnected immediately
        for office_id in connected:
            try:
                await redis_client.delete(f"connections:{office_id}:orchestrator")
                await redis_client.delete(f"office:{office_id}:health")
            except Exception:
                pass

        try:
            await redis_client.aclose()
        except Exception as exc:
            logger.debug("Redis close error: %s", exc)

        # Phase 4: Infrastructure
        if poll_task is not None:
            poll_task.cancel()
            try:
                await poll_task
            except (asyncio.CancelledError, Exception):
                pass
        if delete_consumer_task is not None:
            delete_consumer_task.cancel()
            try:
                await delete_consumer_task
            except (asyncio.CancelledError, Exception):
                pass
        if create_consumer_task is not None:
            create_consumer_task.cancel()
            try:
                await create_consumer_task
            except (asyncio.CancelledError, Exception):
                pass

        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)

        await containers.stop_all()
        get_pid_path().unlink(missing_ok=True)
        logger.info("Shutdown complete")


async def _connect_office_process_model(
    office: OfficeConfig,
    config: Config,
    containers: ContainerManager,
    redis_client: object,
    connected: dict[str, ProcessModelComponents],
    background_tasks: list[asyncio.Task],
    delete_queue: "asyncio.Queue[str] | None" = None,
    create_queue: "asyncio.Queue[dict] | None" = None,
    connecting: set[str] | None = None,
) -> None:
    """Initialize and connect a single office using the process model.

    ``connecting`` (when provided) is the daemon-level in-flight
    set guarding against concurrent double-connects from the
    poll-loop and the create-consumer. Caller MUST check membership
    before invoking; this function only stamps and clears its own
    entry.
    """
    # Mark in-flight FIRST, before any await — caller already
    # checked membership, so we just stamp. If the caller didn't
    # pass a set we run unprotected (e.g. the very first sync
    # connect during startup, before the poll loop is up).
    if connecting is not None:
        connecting.add(office.id)
    try:
        await containers.ensure_container(office)
        cname = containers.get_container_name(office.id) or ""

        from src.handlers import init_office_process_model

        oc = await init_office_process_model(
            office, config.platform_url,
            container_name=cname,
            redis_client=redis_client,
            security_token=config.security_token,
            delete_queue=delete_queue,
            create_queue=create_queue,
        )
        # Start components
        background_tasks.append(asyncio.create_task(oc.router.start()))
        background_tasks.append(asyncio.create_task(oc.dispatcher.run()))
        background_tasks.append(asyncio.create_task(oc.script_runner.monitor_all()))
        # Global periodic outbox + execution-status sweep — fallback
        # for in-container MCP runs whose host-tool-proxy POST silently
        # fails. Without this, agent-triggered ``notify_manager`` drops
        # sit forever in ``.outbox/`` and AI test runs never appear in
        # the Execution History tab. Bounded cost (~30s tick, sub-100ms
        # disk I/O per tick on typical offices).
        background_tasks.append(asyncio.create_task(oc.script_runner.global_sweep()))

        # Cron scheduler: polls /cron/due every minute and dispatches
        # due schedules to the ScriptRunner.
        try:
            from src.scripts.cron_scheduler import CronScheduler
            cron_scheduler = CronScheduler(
                office_id=str(office.id),
                backend_url=config.platform_url,
                script_runner=oc.script_runner,
                security_token=config.security_token,
            )
            cron_scheduler.start()
            # Keep a reference on the script_runner so shutdown can stop it
            oc.script_runner._cron_scheduler = cron_scheduler
        except Exception:
            logger.exception("Failed to start cron scheduler for office %s", office.name)

        oc.reporter.start()

        # Start the Manager subprocess (handles chat messages)
        await oc.manager.start()

        watchdog_task = asyncio.create_task(oc.watchdog.run())

        connected[office.id] = ProcessModelComponents(
            supervisor=oc.supervisor,
            dispatcher=oc.dispatcher,
            router=oc.router,
            reporter=oc.reporter,
            script_runner=oc.script_runner,
            watchdog_task=watchdog_task,
            queue_manager=oc.queue_manager,
            tool_proxy=oc.tool_proxy,
            office_name=office.name,
        )

        logger.info(
            "Office '%s' (%s) connected (process model)", office.name, office.id,
        )
    except Exception as exc:
        logger.error("Failed to connect office '%s': %s", office.name, exc)
    finally:
        # Always clear the in-flight marker — on success we now
        # have a ``connected`` entry, on failure the next dedup
        # check (poll loop, retry, etc.) needs a clean slate.
        if connecting is not None:
            connecting.discard(office.id)


async def _disconnect_office_process_model(
    office_id: str,
    connected: dict[str, ProcessModelComponents],
    containers: ContainerManager,
    redis_client: object,
) -> None:
    """Tear down a single office's components and remove its container.

    Mirrors the Phase 1-3 shutdown sequence in ``_run_process_model``'s
    ``finally`` block, but scoped to one office. Called when the
    Communicator detects an office has been deleted backend-side
    (either via the ``office_deleted`` push from the platform, or
    via the office-poll reconciliation pass when the office is
    missing from ``GET /api/offices``).

    Phases mirror the daemon-wide shutdown to keep the teardown
    logic in lockstep:

      1. Stop accepting work (router, dispatcher, watchdog,
         reporter, cron scheduler).
      2. Graceful agent shutdown (supervisor.shutdown sends
         shutdown over NDJSON, then SIGKILLs after the timeout —
         caps at 30s so a deletion can't hang indefinitely on a
         stuck agent).
      3. Flush + close (script_runner, manager, tool_proxy).
      4. Clear Redis presence keys (so the UI shows
         "disconnected" immediately rather than waiting for the
         60s TTL).
      5. ``docker stop`` + ``docker rm`` the office container —
         this is the visible part of the bug fix; without this
         the container kept running after the office row was
         deleted.
      6. Drop from the in-memory ``connected`` dict.

    Idempotent: calling twice on the same office_id is a no-op
    (second call finds it missing from ``connected`` and returns
    early). All step failures are logged but never raise — a
    teardown must not abort partway, otherwise we'd leak whatever
    came after the failing step.
    """
    oc = connected.pop(office_id, None)
    if oc is None:
        # Already disconnected (or never connected). Still try the
        # container cleanup in case the in-memory state and the
        # actual container set drifted apart.
        try:
            await containers.stop_office(office_id)
        except Exception as exc:
            logger.debug(
                "stop_office for unknown %s: %s", office_id, exc,
            )
        return

    logger.info("Disconnecting office %s — beginning teardown", office_id)

    # Phase 1: stop accepting work.
    try:
        await oc.router.stop()
    except Exception as exc:
        logger.debug("Router stop error for %s: %s", office_id, exc)
    try:
        await oc.dispatcher.stop()
    except Exception as exc:
        logger.debug("Dispatcher stop error for %s: %s", office_id, exc)
    if oc.watchdog_task:
        oc.watchdog_task.cancel()
        try:
            await oc.watchdog_task
        except (asyncio.CancelledError, Exception):
            pass
    try:
        oc.reporter.stop()
    except Exception as exc:
        logger.debug("Reporter stop error for %s: %s", office_id, exc)
    # Cron scheduler is stashed on the script_runner at connect time
    # (see ``_connect_office_process_model``). Stop it explicitly so
    # its 60s polling task doesn't outlive the office.
    #
    # BUG-FIX (user report 2026-05-27): ``stop()`` is an async coroutine
    # but was previously called WITHOUT await — the coroutine object
    # was created, the warning logged, but the background task never
    # got cancelled. ``cbcl status`` continued to show cron schedules
    # for the deleted office because the polling task kept hitting
    # ``/cron/due`` every 60s indefinitely.
    cron_scheduler = getattr(oc.script_runner, "_cron_scheduler", None)
    if cron_scheduler is not None:
        try:
            await cron_scheduler.stop()
        except Exception as exc:
            logger.debug(
                "Cron scheduler stop error for %s: %s", office_id, exc,
            )
        # Clear the reference so ``script_runner.shutdown()`` (called
        # in Phase 3 below) doesn't re-stop the same scheduler.
        oc.script_runner._cron_scheduler = None

    # Phase 2: graceful agent shutdown.
    try:
        await oc.supervisor.shutdown(timeout=30)
    except Exception as exc:
        logger.warning(
            "Supervisor shutdown error for %s: %s", office_id, exc,
        )

    # Phase 3: flush + close. The Manager subprocess is already
    # killed by ``supervisor.shutdown(timeout=30)`` above — sending
    # an extra ``manager.stop()`` IPC at this point would race a
    # dead process. The supervisor's graceful loop sends ``shutdown``
    # via IPC to every tracked agent (Manager included) before the
    # SIGKILL fallback, so the flush opportunity is already there.
    try:
        await oc.script_runner.shutdown()
    except Exception as exc:
        logger.debug("Script runner shutdown error for %s: %s", office_id, exc)
    if oc.tool_proxy is not None:
        try:
            await oc.tool_proxy.stop()
        except Exception as exc:
            logger.debug("Tool proxy stop error for %s: %s", office_id, exc)

    # Phase 4: clear presence keys + per-office Redis state so the UI
    # flips to disconnected immediately AND no orphan queues / streams
    # accumulate over many office-create-delete cycles.
    try:
        await redis_client.delete(f"connections:{office_id}:orchestrator")
        await redis_client.delete(f"office:{office_id}:health")
        await redis_client.delete(f"office:{office_id}:sessions")
        await redis_client.delete(f"office:{office_id}:commands")
        await redis_client.delete(f"office:{office_id}:events")
        # Per-agent queues + active hashes — wildcard scan because
        # agent names are dynamic. Bounded by the office's roster
        # size (max ~20 agents typical) so a SCAN is cheap.
        async for key in redis_client.scan_iter(
            match=f"office:{office_id}:aq:*", count=100,
        ):
            await redis_client.delete(key)
        # Agent activity feed lists.
        async for key in redis_client.scan_iter(
            match=f"office:{office_id}:agent_feed:*", count=100,
        ):
            await redis_client.delete(key)
        # Dispatcher locks (task_lock:<task_id>) — bounded by the
        # number of in-flight tasks at delete time; SCAN is cheap.
        async for key in redis_client.scan_iter(
            match=f"office:{office_id}:task_lock:*", count=100,
        ):
            await redis_client.delete(key)
    except Exception as exc:
        logger.debug("Redis cleanup error for %s: %s", office_id, exc)

    # Phase 4b: drop the office-secrets host file. The container is
    # about to be removed; leaving these credentials on disk after the
    # office is gone is a stale-secret hazard (a future office with
    # the same slug would auto-inherit them). Best-effort delete —
    # missing file is fine. Slug comes from the ``office_name``
    # captured at connect time so this still works even when the
    # orchestrator's sync_config never arrived (early failure path).
    try:
        from src.paths import get_office_secrets_path
        from src.utils import slugify
        slug = slugify(oc.office_name) if oc.office_name else ""
        if slug:
            secrets_path = get_office_secrets_path(slug)
            if secrets_path.is_file():
                secrets_path.unlink(missing_ok=True)
                logger.info(
                    "Removed office-secrets file %s for deleted office %s",
                    secrets_path, office_id,
                )
    except Exception as exc:
        logger.debug(
            "Office-secrets cleanup error for %s: %s", office_id, exc,
        )

    # Phase 5: stop + remove the Docker container. THIS is the
    # missing step the bug report flagged — without it the office
    # container kept running indefinitely after the office row was
    # deleted backend-side.
    try:
        await containers.stop_office(office_id)
    except Exception as exc:
        logger.warning(
            "Container stop error for %s: %s — orphan container may remain",
            office_id, exc,
        )

    logger.info("Office %s disconnected and container removed", office_id)


async def _consume_office_creates(
    create_queue: asyncio.Queue,
    config: Config,
    containers: ContainerManager,
    redis_client: object,
    connected: dict[str, ProcessModelComponents],
    connecting: set[str],
    background_tasks: list[asyncio.Task],
    shutdown_event: asyncio.Event,
    delete_queue: "asyncio.Queue[str] | None" = None,
) -> None:
    """Drain ``office_created`` notifications and connect each one.

    The proactive path: when the backend creates an office it
    broadcasts ``office_created`` on every connected WS. The first
    router to handle it enqueues here; the consumer connects the
    new office immediately. Without this, the user would wait up
    to 15s for the office-poll loop to discover the row.

    Dedupe contract: the producer (``_handle_office_created``) does
    NOT check ``connected``, so the same office_id can land in the
    queue multiple times if the backend retries the broadcast or
    if multiple routers race. We dedupe HERE — if it's already in
    ``connected``, skip. This keeps the producer trivial and puts
    the rule in one place.

    Failure isolation: a connect that throws is logged and the
    consumer keeps running, otherwise a single bad row would
    permanently block subsequent creates until daemon restart.
    """
    while not shutdown_event.is_set():
        try:
            payload = await asyncio.wait_for(
                create_queue.get(), timeout=5.0,
            )
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            return

        office_id = payload.get("office_id", "")
        name = payload.get("name") or office_id
        if not office_id:
            continue
        # Two-tier dedup: check ``connected`` (fully online) AND
        # ``connecting`` (in-flight). The in-flight check closes
        # the race where the poll loop just started a connect for
        # this office but hasn't reached ``connected[oid] = ...``
        # yet — without it, both the poll and the consumer would
        # spawn parallel ``_connect_office_process_model`` coroutines,
        # leaving an orphan ProcessModelComponents whose container
        # and agent processes leak.
        if office_id in connected or office_id in connecting:
            logger.debug(
                "office_created %s ignored — already %s",
                office_id,
                "connected" if office_id in connected else "in-flight",
            )
            continue
        # Stake the in-flight claim before any await so the poll
        # loop's next reconcile pass sees us and skips.
        connecting.add(office_id)

        logger.info(
            "office_created push: connecting '%s' (%s) immediately",
            name, office_id,
        )
        try:
            await _connect_office_process_model(
                OfficeConfig(id=office_id, name=name),
                config, containers, redis_client,
                connected, background_tasks,
                delete_queue=delete_queue,
                create_queue=create_queue,
                connecting=connecting,
            )
        except Exception:
            logger.exception(
                "office_created consumer: connect failed for %s — "
                "poll loop will retry",
                office_id,
            )
        finally:
            # ``_connect_office_process_model`` clears the marker
            # in its own finally; double-discard is harmless.
            connecting.discard(office_id)


async def _consume_office_deletes(
    delete_queue: asyncio.Queue,
    connected: dict[str, ProcessModelComponents],
    containers: ContainerManager,
    redis_client: object,
    shutdown_event: asyncio.Event,
) -> None:
    """Drain ``office_deleted`` notifications and tear down each office.

    The proactive path: when the backend pushes ``office_deleted``
    over the office's WebSocket, the per-office router enqueues
    here. We process out-of-band so the router callback can return
    cleanly (calling ``_disconnect_office_process_model`` directly
    from the callback would deadlock — the teardown calls
    ``router.stop()`` which would await the very task that's
    awaiting us).

    Failures are logged but never raised: if one teardown fails the
    consumer must keep running, otherwise a single bad delete
    permanently disables proactive teardowns until restart.
    """
    while not shutdown_event.is_set():
        try:
            office_id = await asyncio.wait_for(
                delete_queue.get(), timeout=5.0,
            )
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            return

        try:
            await _disconnect_office_process_model(
                office_id, connected, containers, redis_client,
            )
        except Exception:
            logger.exception(
                "office_deleted consumer: teardown failed for %s",
                office_id,
            )


async def _poll_for_new_offices_process_model(
    config: Config,
    containers: ContainerManager,
    redis_client: object,
    connected: dict[str, ProcessModelComponents],
    connecting: set[str],
    background_tasks: list[asyncio.Task],
    shutdown_event: asyncio.Event,
    delete_queue: "asyncio.Queue[str] | None" = None,
    create_queue: "asyncio.Queue[dict] | None" = None,
) -> None:
    """Poll the platform for new (and deleted) offices.

    Reconciles in BOTH directions:

    * Add: any office returned by ``/api/offices`` that we don't
      already have in ``connected`` gets connected.
    * Remove: any office in ``connected`` that's NOT in the
      discovery list gets torn down via
      ``_disconnect_office_process_model``. This is the safety net
      for the case where the Communicator was offline when the
      backend pushed ``office_deleted`` (the OfflineBuffer attempts
      delivery on reconnect, but the Communicator might also have
      missed the buffer entirely if the office was deleted before
      it ever connected).

    A failed discovery call (network blip, backend restart) is
    deliberately treated as "no info" — we DON'T tear down anything
    on a discovery failure, because the empty list returned by
    ``_discover_offices`` on error would otherwise nuke every office.
    """
    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(
                shutdown_event.wait(), timeout=OFFICE_POLL_INTERVAL,
            )
            return
        except asyncio.TimeoutError:
            pass

        try:
            offices = await _discover_offices(config.platform_url, config.security_token)
        except Exception as exc:
            from src.utils import describe_exception
            logger.warning("Office poll failed: %s", describe_exception(exc))
            continue

        # ``_discover_offices`` swallows errors and returns ``[]``,
        # which would falsely look like "all offices were deleted".
        # Guard: skip the remove pass if discovery returned empty
        # AND we believe at least one office exists. The trade-off
        # is that a true "all offices deleted" state takes one
        # extra cycle to reconcile — acceptable for a rare edge.
        discovered_ids = {o.id for o in offices}
        if not offices and connected:
            logger.debug(
                "Discovery returned empty list while %d office(s) "
                "are connected — treating as transient failure, "
                "skipping reconciliation pass",
                len(connected),
            )
            continue

        # Pass 1: add new offices. Honour the in-flight ``connecting``
        # set: if the create-consumer is mid-connect for this office
        # we skip; otherwise we'd race the consumer and produce two
        # parallel ProcessModelComponents for the same office_id.
        for office in offices:
            if office.id in connected or office.id in connecting:
                continue
            logger.info(
                "New office discovered: '%s' (%s) — connecting",
                office.name, office.id,
            )
            connecting.add(office.id)
            try:
                await _connect_office_process_model(
                    office, config, containers, redis_client,
                    connected, background_tasks,
                    delete_queue=delete_queue,
                    create_queue=create_queue,
                    connecting=connecting,
                )
            except Exception:
                logger.exception(
                    "Failed to connect office %s", office.id,
                )
            finally:
                connecting.discard(office.id)

        # Pass 2: tear down deleted offices. Snapshot the keys —
        # ``_disconnect_office_process_model`` mutates ``connected``.
        for office_id in list(connected):
            if office_id not in discovered_ids:
                logger.info(
                    "Office %s missing from platform — tearing down "
                    "(was deleted backend-side)",
                    office_id,
                )
                try:
                    await _disconnect_office_process_model(
                        office_id, connected, containers, redis_client,
                    )
                except Exception:
                    logger.exception(
                        "Failed to disconnect office %s", office_id,
                    )


def _setup_logging_foreground() -> None:
    """Configure logging to stdout (foreground mode)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


class _SecureRotatingFileHandler(RotatingFileHandler):
    """``RotatingFileHandler`` that chmods every roll target to 0o600.

    The default handler creates the new log file under the process
    umask after rotation, so a log that started 0o600 would become
    0o644 on first roll. Logs contain token fingerprints, request ids,
    and other diagnostic strings worth keeping owner-readable.
    """

    def doRollover(self) -> None:  # type: ignore[override]
        super().doRollover()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:
            # Don't fail the logging pipeline on a chmod race; the
            # initial setup chmod will be re-applied on the next
            # daemon restart anyway.
            pass


def _setup_logging_daemon() -> None:
    """Configure logging to a rotating file (daemon mode)."""
    log_file = get_logs_path() / "communicator.log"

    handler = _SecureRotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10 MB per file
        backupCount=5,  # Keep 5 rotated files
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    # Restrict log file permissions on the active file. Rolled files
    # carry their own 0o600 via _SecureRotatingFileHandler.doRollover.
    try:
        os.chmod(str(log_file), 0o600)
    except OSError:
        # Initial setup race — file may not exist yet on first start;
        # the handler will create it lazily.
        pass

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)



def _read_pid(pid_path: Path) -> int | None:
    """Read PID from file, returning None if invalid."""
    try:
        return int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return None


def _is_process_running(pid: int) -> bool:
    """Check if a process with the given PID exists."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def find_running_daemon_pid() -> int | None:
    """Find a running ``cbcl start`` PID by scanning /proc.

    Fallback for the case where the PID file is missing — usually
    because the daemon was started by an older cbcl that didn't
    write one in the foreground path. Scans ``/proc/<pid>/cmdline``
    for the cbcl daemon's argv signature: a ``python``-flavoured
    executable + a path that ends with ``bin/cbcl`` + the
    ``start`` subcommand.

    Returns the PID of the FIRST match (the OS list-children order
    is monotonic), or ``None`` if nothing matches. Skips the current
    process so a ``cbcl status`` call doesn't false-positive on
    itself.

    Linux-only by design — /proc-based. macOS / Windows users
    invariably run cbcl inside the Docker compose dev stack and
    don't hit this path. A bare-metal macOS run would just fall
    back to the "Not running" message it always showed.
    """
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    self_pid = os.getpid()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == self_pid:
            continue
        cmdline_path = entry / "cmdline"
        try:
            raw = cmdline_path.read_bytes()
        except OSError:
            # Process exited mid-scan, or we lack permission. Either
            # way, skip and keep going.
            continue
        # /proc/<pid>/cmdline uses NUL as the argv separator.
        argv = [a for a in raw.split(b"\x00") if a]
        if len(argv) < 2:
            continue
        # argv[0] = python interpreter (pipx venv path), argv[1] =
        # full path to the cbcl entry point, argv[2:] = subcommand.
        # We look for "/bin/cbcl" in argv[1] AND "start" in argv.
        # Matching argv[1] specifically (not "in raw") avoids
        # false positives from ``grep cbcl`` or an editor with
        # "cbcl" in its window title.
        if not argv[1].endswith(b"/cbcl"):
            continue
        if b"start" not in argv[2:]:
            continue
        return pid
    return None


def _format_uptime(pid_path: Path) -> str:
    """Format uptime from PID file modification time."""
    try:
        mtime = pid_path.stat().st_mtime
        elapsed = time.time() - mtime
        hours = int(elapsed // 3600)
        minutes = int((elapsed % 3600) // 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except OSError:
        return "unknown"
