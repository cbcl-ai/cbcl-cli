"""Outbox watcher — script → Manager callback via filesystem drop.

Mini-project scripts can notify the Manager by writing a JSON
file to ``{script_dir}/.outbox/notify-*.json``. This module picks
those files up, validates them, resolves the target workstream,
and feeds the payload into the Manager the same way a user chat
message is fed in — with a ``[Script: <name>]`` prefix so the user
and the Manager can distinguish it from direct user input.

Why filesystem drop rather than a new listener:
  * The workspace is already bind-mounted between host and the
    office container — scripts write, the communicator (on the
    host) reads. Zero new network surface.
  * No per-script auth token to rotate. Scripts and the
    communicator are the same host user; filesystem perms are the
    authoritative gate.
  * Survives crashes — an atomic rename claim means partial drops
    never produce half-routed messages.

Integration:
  * The existing per-execution poll loop (``script_execution.py``)
    calls :func:`scan_and_dispatch` each tick.
  * :func:`scan_and_dispatch` is idempotent: called repeatedly on
    the same directory it processes new files only, because each
    file is renamed to ``.processing`` at claim time and moved to
    ``.processed/{YYYY-MM-DD}/`` on success.

The stdlib-only ``cubicle.notify_manager`` helper that scripts
import lives at :file:`templates/cubicle_helper.py` and is copied
into ``lib/cubicle/__init__.py`` by the backend bootstrap on
script create.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, ValidationError, field_validator

if TYPE_CHECKING:
    from src.config_sync.sync_service import ConfigStore
    from src.orchestrator.manager_controller import ManagerController

logger = logging.getLogger(__name__)

# Caps on payload shape — bigger values are almost certainly a bug
# and accepting them would either DoS the Manager (messages beyond
# a few KB are too big for a chat bubble) or bloat the chat stream
# with megabyte notifications. Note ``_MAX_MESSAGE_CHARS`` bounds
# Unicode code points (Pydantic ``max_length`` semantics); the
# extra ``_enforce_byte_budget`` validator then enforces the UTF-8
# byte budget so a character-sized fixture of multi-byte chars
# can't silently overflow the whole-payload budget.
_MAX_MESSAGE_CHARS = 8 * 1024
_MAX_MESSAGE_BYTES = 32 * 1024
_MAX_ATTACHMENTS = 10
_MAX_ATTACHMENT_PATH_CHARS = 512
_MAX_PAYLOAD_BYTES = 32 * 1024

# RP-4 runaway-loop guard: max Manager DELIVERIES per script execution. Every
# delivered notify is a full Manager (Opus) chat turn, so size caps alone
# don't bound cost — a notify-looping script could drive unbounded serial
# turns. The cap-crossing drop becomes one final platform-notice message;
# later drops from that execution archive as ``notify-rate-cap`` rejects.
# Env-tunable for offices whose scripts legitimately chatter more.
_MAX_NOTIFIES_PER_EXECUTION = int(
    os.environ.get("CUBICLE_MAX_NOTIFIES_PER_EXECUTION", "20")
)
# execution_id → deliveries so far (module-scope, like _INGEST_ATTEMPTS;
# clears on daemon restart, which is an acceptable reset for a rate guard).
_NOTIFY_COUNT_BY_EXECUTION: dict[str, int] = {}

# Stale-claim reaper runs only at office startup now (not mid-loop).
# Threshold below is therefore a "we've been down at least this long
# since the claim; safe to assume it's orphaned". If the office was
# up <10m ago and then crashed, the re-started watcher calls the
# reaper and immediately sees the file is younger than 10m — we
# leave it alone rather than racing a possibly-still-running sibling
# (separate cbcl process, or the NT ``docker exec`` python that's
# just slow to die). Any remaining claim older than the threshold
# is definitely orphaned.
_STALE_PROCESSING_SECONDS = 10 * 60

# Retention on the audit trail — enough to debug recent runs, not
# so long that it bloats the workspace. The UI's notifications
# panel surfaces whatever is still here.
_PROCESSED_RETENTION_DAYS = 7

# Per-script-dir scan lock. A single script can have multiple
# concurrent executions (cron fires while manual Run is mid-flight —
# explicitly supported). Each execution's poll loop calls
# ``scan_and_dispatch`` on the same ``.outbox/`` directory. Without
# this lock, two concurrent scans both try to atomically rename
# the same notify file (one loses that race cleanly — fine) AND both
# race on the archive-move path (no atomicity — one can move a file
# that the other is stat-ing).  Serialising scans per-dir removes
# the entire class of races at trivial cost (per-file scan is fast).
_SCAN_LOCKS: dict[str, asyncio.Lock] = {}

# ── Transient-ingest retry (ADD-C2) ─────────────────────────────────
# A notify drop that PASSED validation can still fail to deliver when
# the Manager is transiently unavailable (respawning, OOM-killed,
# inactivity/hard timeout mid-turn). The old code archived such a drop
# as ``rejected`` on ANY exception — conflating "bad payload" (already
# rejected upstream) with "Manager busy right now", and permanently
# losing the ``[Script: …]`` callback exactly when the office was
# offline/restarting. Instead we un-claim the file (back to pending),
# schedule a backed-off re-scan, and only give up after a bounded
# number of attempts. A pending file also gets retried by the daemon's
# startup orphan-notify reaper, so a restart mid-backoff never loses it.
_INGEST_BASE_BACKOFF_SECONDS = 5.0
_INGEST_BACKOFF_FACTOR = 3.0
_INGEST_MAX_BACKOFF_SECONDS = 300.0
_MAX_INGEST_ATTEMPTS = 5
# base notify filename → transient-failure attempts so far.
_INGEST_ATTEMPTS: dict[str, int] = {}
# base notify filename → monotonic time before which we won't retry.
_INGEST_RETRY_AT: dict[str, float] = {}
# Strong refs for scheduled re-scan tasks (GC would otherwise collect
# a pending task mid-backoff).
_RETRY_TASKS: set[asyncio.Task] = set()


def _ingest_backoff_seconds(attempt: int) -> float:
    """Exponential backoff for the ``attempt``-th transient failure
    (1-based), capped at ``_INGEST_MAX_BACKOFF_SECONDS``."""
    delay = _INGEST_BASE_BACKOFF_SECONDS * (
        _INGEST_BACKOFF_FACTOR ** max(0, attempt - 1)
    )
    return min(delay, _INGEST_MAX_BACKOFF_SECONDS)


def _unclaim(claimed: Path, original: Path) -> None:
    """Rename a claimed ``.processing`` file back to its pending name so
    a later scan re-picks it up. Best-effort."""
    try:
        claimed.rename(original)
    except OSError:
        logger.warning(
            "outbox: failed to un-claim %s for retry", claimed,
            exc_info=True,
        )


def _schedule_outbox_rescan(
    *,
    script_dir: Path,
    script_name: str,
    office_id: str,
    config_store: "ConfigStore",
    manager: "ManagerController",
    workspace_root: Path,
    delay: float,
) -> None:
    """Fire-and-forget a delayed re-scan of one script's outbox so a
    transiently-failed drop is retried after the backoff window without
    relying on another script run to trigger a scan."""

    async def _run() -> None:
        try:
            await asyncio.sleep(delay)
            await scan_and_dispatch(
                script_dir=script_dir,
                script_name=script_name,
                office_id=office_id,
                config_store=config_store,
                manager=manager,
                workspace_root=workspace_root,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "outbox: scheduled re-scan failed for %s", script_dir.name,
            )

    task = asyncio.create_task(_run())
    _RETRY_TASKS.add(task)
    task.add_done_callback(_RETRY_TASKS.discard)


def _get_scan_lock(script_dir: Path) -> asyncio.Lock:
    """Return the per-script asyncio.Lock used to serialise scans.

    Keyed on the resolved string path so two loops on the same
    script directory share one lock even if they pass subtly
    different Path objects (symlink vs real path, trailing slash
    differences from the caller).

    Opportunistic pruning: every ``_SCAN_LOCK_PRUNE_INTERVAL`` calls,
    walk the keys and drop any whose directory no longer exists. A
    daemon that runs for weeks with frequent script create/delete
    cycles used to accumulate dead keys indefinitely (the prune-on-
    startup path only fired once per daemon lifetime). The cost is
    a single ``Path(...).exists()`` per key, amortized over the
    interval — negligible compared to a single outbox scan.
    """
    key = str(script_dir.resolve())
    lock = _SCAN_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _SCAN_LOCKS[key] = lock
        _maybe_prune_scan_locks()
    return lock


# Prune cadence + counter. The counter is module-level so it survives
# across multiple watcher instances within one daemon process. The
# cadence is conservative — pruning every 200 NEW-script-dir scans
# keeps the dict bounded by the count of CURRENTLY-EXISTING scripts
# plus at most 200 dead entries, which is fine even for a heavy-use
# office.
_SCAN_LOCK_PRUNE_INTERVAL = 200
_SCAN_LOCK_NEW_COUNT = 0


def _maybe_prune_scan_locks() -> None:
    """Drop _SCAN_LOCKS entries whose script directory is gone.

    Cheap: one ``Path.exists()`` per key. Only walks every
    ``_SCAN_LOCK_PRUNE_INTERVAL`` new-key insertions so the loop
    body's amortised cost stays low.
    """
    global _SCAN_LOCK_NEW_COUNT
    _SCAN_LOCK_NEW_COUNT += 1
    if _SCAN_LOCK_NEW_COUNT < _SCAN_LOCK_PRUNE_INTERVAL:
        return
    _SCAN_LOCK_NEW_COUNT = 0
    try:
        dead = [k for k in list(_SCAN_LOCKS) if not Path(k).exists()]
        for k in dead:
            _SCAN_LOCKS.pop(k, None)
        if dead:
            logger.debug(
                "outbox_watcher: pruned %d dead scan-lock entries",
                len(dead),
            )
    except Exception:
        # Defence: pruning is housekeeping, never load-bearing.
        logger.debug(
            "outbox_watcher: scan-lock prune failed (non-fatal)",
            exc_info=True,
        )


class OutboxNotifyPayload(BaseModel):
    """Strict schema for ``notify-*.json``. Rejected payloads are
    moved to ``.processed/<date>/rejected/`` with the rejection
    reason logged, so a scriptmaker can debug without the
    communicator crashing on a malformed input.

    Unknown fields are IGNORED (``extra="ignore"``) rather than
    rejected outright — a script that ships its own wrapper over
    ``cubicle.notify_manager`` and adds its own metadata (e.g. a
    ``schema`` envelope tag) stays compatible. The authoritative
    version gate is ``v: Literal[1]``; a genuinely incompatible
    future payload (``v=2``) is still rejected because the
    Literal itself rejects the mismatch.
    """

    model_config = {"extra": "ignore"}

    # Pinned to literal 1 so a future-shaped payload (e.g. v=2
    # with a required new field) is REJECTED by this watcher
    # instead of silently running against the old code path.
    v: Literal[1] = 1
    action: Literal["notify_manager"] = "notify_manager"
    workstream: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=_MAX_MESSAGE_CHARS)
    # Per-item length cap keeps log lines bounded even when a
    # script dumps a pathologically long attachment path.
    attachments: list[
        Annotated[str, StringConstraints(max_length=_MAX_ATTACHMENT_PATH_CHARS)]
    ] = Field(default_factory=list)
    execution_id: str | None = None
    script_name: str | None = None
    # Script's wall-clock at drop time. Used only for log context
    # (latency diagnostics); a NaN/Inf would break a UI surfacing
    # it later, so reject now.
    emitted_at: float | None = None

    @field_validator("message")
    @classmethod
    def _message_byte_budget(cls, v: str) -> str:
        encoded = len(v.encode("utf-8"))
        if encoded > _MAX_MESSAGE_BYTES:
            raise ValueError(
                f"message exceeds {_MAX_MESSAGE_BYTES}-byte UTF-8 budget "
                f"(got {encoded} bytes)"
            )
        return v

    @field_validator("emitted_at")
    @classmethod
    def _emitted_at_sane(cls, v: float | None) -> float | None:
        if v is None:
            return v
        if not math.isfinite(v) or v < 0:
            raise ValueError(
                "emitted_at must be a finite non-negative timestamp"
            )
        # Reject wall-clock drops more than a day in the future —
        # almost certainly a misconfigured host clock, not a legit
        # payload, and leaving it in would skew any latency
        # diagnostics that read it.
        if v > time.time() + 86400:
            raise ValueError(
                "emitted_at is more than 24h in the future "
                "(check the script container's clock)",
            )
        return v


async def scan_and_dispatch(
    *,
    script_dir: Path,
    script_name: str,
    office_id: str,
    config_store: ConfigStore,
    manager: ManagerController,
    workspace_root: Path,
) -> int:
    """Scan ``{script_dir}/.outbox`` for pending notify files and
    route each through the Manager. Returns the number of files
    successfully dispatched (for logging / caller metrics).

    Non-fatal — every exception during per-file handling is logged
    and the scan continues on the next file. We don't want a single
    bad payload to stall the whole outbox.
    """
    outbox = script_dir / ".outbox"
    if not outbox.is_dir():
        return 0

    # Serialise scans of the same script directory. Prevents two
    # concurrent executions of the same script racing on the
    # archive-move path. If one scan is already running we simply
    # skip — the other call will process the drop.
    scan_lock = _get_scan_lock(script_dir)
    if scan_lock.locked():
        logger.debug(
            "outbox: scan already in progress for %s, skipping tick",
            script_dir.name,
        )
        return 0

    async with scan_lock:
        return await _scan_and_dispatch_locked(
            outbox=outbox,
            script_dir=script_dir,
            script_name=script_name,
            office_id=office_id,
            config_store=config_store,
            manager=manager,
            workspace_root=workspace_root,
        )


async def _scan_and_dispatch_locked(
    *,
    outbox: Path,
    script_dir: Path,
    script_name: str,
    office_id: str,
    config_store: ConfigStore,
    manager: ManagerController,
    workspace_root: Path,
) -> int:
    """Scan body — caller holds the per-dir scan lock.

    Does NOT reap stale ``.processing`` claims. That's the boot-time
    reaper's job (see :func:`reap_stale_claims_on_startup`); doing
    it per-tick is wrong because a long Manager ingest (slow LLM,
    compaction, large context) can legitimately hold a claim open
    past the stale threshold — if we reaped on every tick, the
    next scan on the same dir would archive a claim that's still
    being ingested, producing a phantom ``stale-claim`` audit row
    for a payload the Manager actually received.
    """
    dispatched = 0
    for entry in sorted(outbox.iterdir()):
        # Only pick up fresh notify drops — skip our own claim
        # markers, the ``.tmp`` files the helper uses for atomic
        # writes, and the processed archive subdir.
        if entry.is_dir():
            continue
        if not entry.name.startswith("notify-"):
            continue
        if not entry.name.endswith(".json"):
            continue

        try:
            ok = await _handle_one(
                path=entry,
                script_dir=script_dir,
                script_name=script_name,
                office_id=office_id,
                config_store=config_store,
                manager=manager,
                workspace_root=workspace_root,
            )
            if ok:
                dispatched += 1
        except Exception:
            logger.exception(
                "outbox_watcher: unexpected error handling %s — "
                "leaving file in place for retry",
                entry,
            )
    return dispatched


async def _handle_one(
    *,
    path: Path,
    script_dir: Path,
    script_name: str,
    office_id: str,
    config_store: ConfigStore,
    manager: ManagerController,
    workspace_root: Path,
) -> bool:
    """Claim, validate, route, archive a single notify file.

    Returns True if the file was handed off to the Manager. False
    covers the "malformed payload" case — the file is moved to the
    ``rejected/`` archive so it doesn't block the queue but isn't
    lost either.
    """
    # 0. Backoff gate (ADD-C2): if a prior transient ingest failure put
    # this drop into backoff, leave it pending until the window passes.
    # Keeps a per-execution scan tick from hammering a down Manager
    # ahead of the scheduled re-scan.
    base_name = path.name
    retry_at = _INGEST_RETRY_AT.get(base_name)
    if retry_at is not None and time.monotonic() < retry_at:
        return False

    # 1. Atomic claim. Rename to ``.processing`` so a concurrent
    # call (or crash-restart of the watcher loop) skips over files
    # already being handled.
    claimed = path.with_name(path.name + ".processing")
    try:
        path.rename(claimed)
    except FileNotFoundError:
        # Another runner grabbed it first, or it was removed out-of-band.
        # Drop any transient-retry bookkeeping so the module-level dicts
        # can't accumulate dead entries for a file that no longer exists
        # (F5). If another runner is handling it, it owns the lifecycle.
        _INGEST_ATTEMPTS.pop(base_name, None)
        _INGEST_RETRY_AT.pop(base_name, None)
        return False
    except OSError as exc:
        logger.warning(
            "outbox_watcher: could not claim %s: %s", path, exc,
        )
        return False

    # 2. Size + parse guard before Pydantic — a multi-MB file would
    # otherwise consume an unbounded amount of memory in .read_text.
    try:
        size = claimed.stat().st_size
        if size > _MAX_PAYLOAD_BYTES:
            logger.warning(
                "outbox_watcher: payload %s is %d bytes, rejecting",
                claimed, size,
            )
            _archive_rejected(claimed, script_dir, reason="oversized")
            return False
        raw = claimed.read_text(errors="replace")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "outbox_watcher: failed to parse %s: %s", claimed, exc,
        )
        _archive_rejected(claimed, script_dir, reason="parse-error")
        return False

    # 3. Schema validation.
    try:
        payload = OutboxNotifyPayload.model_validate(data)
    except ValidationError as exc:
        logger.warning(
            "outbox_watcher: schema rejected %s: %s",
            claimed, exc.errors()[0] if exc.errors() else exc,
        )
        _archive_rejected(claimed, script_dir, reason="schema")
        return False

    # 3b. Per-execution notify CAP (review RP-4): each delivered notify drives
    # a full Manager (Opus) chat turn. Sizes were bounded but COUNT was not —
    # a script stuck in a notify loop could drive unbounded serial Manager
    # turns (cost + a Manager that can't serve the user). Cap deliveries per
    # execution_id; the cap-crossing drop is REPLACED by one final warning
    # message so the Manager (and user) learn the script is misbehaving,
    # then further drops from that execution are rejected silently.
    exec_key = payload.execution_id or ""
    if exec_key:
        n = _NOTIFY_COUNT_BY_EXECUTION.get(exec_key, 0) + 1
        _NOTIFY_COUNT_BY_EXECUTION[exec_key] = n
        if n > _MAX_NOTIFIES_PER_EXECUTION:
            if n == _MAX_NOTIFIES_PER_EXECUTION + 1:
                logger.warning(
                    "outbox_watcher: execution %s exceeded %d notifies — "
                    "suppressing further Manager deliveries from it "
                    "(runaway notify loop?)",
                    exec_key, _MAX_NOTIFIES_PER_EXECUTION,
                )
                payload = payload.model_copy(update={"message": (
                    f"[Platform notice] Script '{script_name}' (execution "
                    f"{exec_key}) has sent {_MAX_NOTIFIES_PER_EXECUTION} "
                    "Manager notifications in one run — further ones from "
                    "this execution are being SUPPRESSED as a runaway-loop "
                    "guard. If this volume is intentional, the script should "
                    "batch its updates into fewer notifies."
                )})
            else:
                _archive_rejected(claimed, script_dir, reason="notify-rate-cap")
                return False

    # 4. Attachments must live inside the workspace. We reject
    # absolute paths and any path that escapes via ``..``.
    valid_attachments: list[str] = []
    for att in payload.attachments[:_MAX_ATTACHMENTS]:
        if not _attachment_is_safe(att, workspace_root):
            logger.warning(
                "outbox_watcher: dropping unsafe attachment %r in %s",
                att, claimed,
            )
            continue
        valid_attachments.append(att)

    # 5. Resolve the workstream name/UUID/"general_chat" into the
    # backend's context_key shape. Unknown workstreams are a
    # scriptmaker bug; archive the payload so it's visible.
    context_key = _resolve_context_key(
        payload.workstream, config_store,
    )
    if context_key is None:
        logger.warning(
            "outbox_watcher: unknown workstream %r in %s "
            "(is it archived, or from a different office?)",
            payload.workstream, claimed,
        )
        _archive_rejected(claimed, script_dir, reason="unknown-workstream")
        return False

    # 6. Hand off to the Manager. We deliberately AWAIT here (not
    # fire-and-forget) so a burst of script drops can't race the
    # Manager's own in-flight conversation tracking — the Manager
    # serialises chat turns through ``_active_conversation_id`` +
    # ``_response_done``, and two concurrent ``handle_chat_message``
    # calls would clobber each other's state. Back-pressuring the
    # scan loop is the right call. A slow Manager therefore slows
    # notify delivery; that's a better failure mode than garbled
    # chat responses.
    logger.info(
        "outbox: dispatching notify (script=%s, ctx=%s, emitted_at=%s)",
        script_name,
        context_key,
        payload.emitted_at,
    )
    ingest_failed = False
    ingest_exc_info = False
    try:
        delivered = await manager.ingest_script_message(
            context_key=context_key,
            script_name=script_name,
            content=payload.message,
            execution_id=payload.execution_id or "",
            attachments=valid_attachments,
        )
        # T3.2.5 (07/G17): ``handle_chat_message`` swallows Manager
        # turn errors internally (it publishes the error in-chat), so
        # a FAILED turn used to return cleanly here and the drop was
        # archived as processed — the notify callback silently lost
        # exactly when the Manager was wedged/erroring. The ingest now
        # returns an explicit turn-outcome flag; ``False`` means the
        # turn failed and the drop must be retried like any other
        # transient delivery failure. ``None`` / mock returns (older
        # controllers, test doubles) keep the legacy success meaning.
        ingest_failed = delivered is False
    except Exception:
        ingest_failed = True
        ingest_exc_info = True

    if ingest_failed:
        # ADD-C2: the payload already passed validation, so this is a
        # TRANSIENT delivery failure (Manager respawning / OOM-killed /
        # turn timeout / failed Manager turn), NOT a bad payload. Retry
        # with backoff instead of permanently rejecting — otherwise the
        # [Script: …] callback is lost exactly when the office is
        # offline/restarting.
        #
        # This deliberately shifts notify delivery from at-most-once to
        # at-LEAST-once: if the Manager actually ingested the poke and
        # THEN the call raised (a post-success timeout), the retry
        # re-delivers a duplicate. For an advisory "[Script: …] finished"
        # poke a rare duplicate is strictly better than a silent loss,
        # and the ingest carries a deterministic conversation_id
        # (``script-{execution_id}``) the backend can dedup on later.
        attempts = _INGEST_ATTEMPTS.get(base_name, 0) + 1
        if attempts >= _MAX_INGEST_ATTEMPTS:
            logger.error(
                "outbox_watcher: giving up on %s after %d transient "
                "Manager-ingest failures — archiving as rejected",
                claimed, attempts,
            )
            _INGEST_ATTEMPTS.pop(base_name, None)
            _INGEST_RETRY_AT.pop(base_name, None)
            _archive_rejected(claimed, script_dir, reason="ingest-error-giveup")
            return False
        backoff = _ingest_backoff_seconds(attempts)
        _INGEST_ATTEMPTS[base_name] = attempts
        _INGEST_RETRY_AT[base_name] = time.monotonic() + backoff
        logger.warning(
            "outbox_watcher: transient Manager ingest failure for %s "
            "(attempt %d/%d, %s) — retrying in %.0fs",
            claimed, attempts, _MAX_INGEST_ATTEMPTS,
            "exception" if ingest_exc_info else "Manager turn failed",
            backoff,
            exc_info=ingest_exc_info,
        )
        # Put the file back to pending so the scheduled re-scan (and the
        # daemon's startup orphan reaper, after a restart) re-pick it.
        _unclaim(claimed, path)
        _schedule_outbox_rescan(
            script_dir=script_dir,
            script_name=script_name,
            office_id=office_id,
            config_store=config_store,
            manager=manager,
            workspace_root=workspace_root,
            delay=backoff,
        )
        return False

    # 7. Archive on success. Keep the audit trail for a week. Clear any
    # transient-retry bookkeeping for this drop.
    _INGEST_ATTEMPTS.pop(base_name, None)
    _INGEST_RETRY_AT.pop(base_name, None)
    _archive_processed(claimed, script_dir)
    return True


def _pick_unique_dest(dest_dir: Path, name: str) -> Path:
    """Return a path in ``dest_dir`` that doesn't collide with an
    existing file. If ``dest_dir / name`` is free, return it;
    otherwise suffix with ``-1``, ``-2``, …

    Needed because a reaped stale-claim + a fresh drop can land in
    the same day-dir with the same filename. Without collision
    avoidance, ``shutil.move`` silently overwrites and we lose the
    audit trail. The loop is bounded because each lookup creates at
    most one more collision; in practice the first or second attempt
    succeeds.
    """
    candidate = dest_dir / name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for i in range(1, 1000):
        candidate = dest_dir / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
    # 1000 collisions is not a scenario under the
    # ``notify-{ms}-{hex6}`` naming — reaching here means something
    # is badly wrong (filesystem corruption, a rogue writer). Warn
    # before falling back so an operator sees the signal.
    nonce = uuid.uuid4().hex[:8]
    logger.warning(
        "outbox: 1000+ archive-name collisions in %s — falling back "
        "to nonce suffix %s (check for a stuck writer or corrupt "
        "filesystem)",
        dest_dir, nonce,
    )
    return dest_dir / f"{stem}-{nonce}{suffix}"


def _archive_processed(claimed: Path, script_dir: Path) -> None:
    """Move a successful notify file to ``.outbox/.processed/<date>/``."""
    archive_root = script_dir / ".outbox" / ".processed"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest_dir = archive_root / today
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Strip the .processing suffix so the archive name is the
    # original filename — makes grepping old notifications easy.
    final_name = claimed.name
    if final_name.endswith(".processing"):
        final_name = final_name[: -len(".processing")]
    final = _pick_unique_dest(dest_dir, final_name)
    try:
        shutil.move(str(claimed), str(final))
    except OSError as exc:
        logger.warning(
            "outbox_watcher: archive of %s failed: %s", claimed, exc,
        )


def _archive_rejected(claimed: Path, script_dir: Path, *, reason: str) -> None:
    """Move a malformed payload to ``.outbox/.processed/<date>/rejected/``.
    Keeps the file around for scriptmaker debugging; the reason goes
    into the filename so the rejection class is visible in the
    Files tree without reading every file.
    """
    archive_root = script_dir / ".outbox" / ".processed"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest_dir = archive_root / today / "rejected"
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Prepend the reason so an operator listing the dir can see at
    # a glance why a payload was dropped.
    name = claimed.name
    if name.endswith(".processing"):
        name = name[: -len(".processing")]
    final = _pick_unique_dest(dest_dir, f"{reason}.{name}")
    try:
        shutil.move(str(claimed), str(final))
    except OSError as exc:
        logger.warning(
            "outbox_watcher: rejected-archive of %s failed: %s",
            claimed, exc,
        )


def reap_stale_claims_on_startup(script_dir: Path) -> int:
    """Recover ``.processing`` files orphaned by a previous crash.

    Called ONCE per office init. A normal scan claims a notify file
    by renaming it to ``name.json.processing``; if the process dies
    between claim and archive-move, the file is stuck — per-tick
    scans skip it because of the ``.json`` suffix filter.

    Doing this only at startup (not per-tick) is the correct fix
    for the race where a legitimately long Manager ingest holds a
    claim past the stale threshold: at startup, everything is by
    definition orphaned because no active scan is running.

    Returns the number of files reaped (for logging / ops).

    Also opportunistically drops any ``_SCAN_LOCKS`` entry whose
    script dir no longer exists — without this the dict grows
    unbounded as scripts are renamed / deleted over the life of
    the cbcl daemon (each rename creates a new resolved-path key).
    """
    outbox = script_dir / ".outbox"
    if not outbox.is_dir():
        return 0

    now = time.time()
    reaped = 0
    for entry in outbox.iterdir():
        if entry.is_dir():
            continue
        if not entry.name.endswith(".json.processing"):
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        # Even at startup, we honour the threshold — a claim created
        # within the last N minutes could belong to a sibling cbcl
        # process (dev environment running two daemons) or a race
        # with the last boot's graceful-shutdown handler that did
        # complete the ingest.
        if age <= _STALE_PROCESSING_SECONDS:
            continue
        logger.warning(
            "outbox: reaping stale .processing file %s (%ds old) — "
            "archiving as stale-claim",
            entry.name, int(age),
        )
        _archive_rejected(entry, script_dir, reason="stale-claim")
        reaped += 1
    # Evict scan locks whose script dir is gone (renamed, deleted,
    # or never existed). Cheap — a handful of entries in practice.
    dead_keys = [k for k in _SCAN_LOCKS if not Path(k).exists()]
    for k in dead_keys:
        _SCAN_LOCKS.pop(k, None)
    if dead_keys:
        logger.debug(
            "outbox: pruned %d stale scan-lock entries", len(dead_keys),
        )

    return reaped


def _attachment_is_safe(attachment: str, workspace_root: Path) -> bool:
    """Attachments must resolve to a path INSIDE the workspace.
    Blocks absolute paths, ``..`` traversal, and symlinks that escape.

    Trust boundary: anything inside ``/workspace`` is trusted the
    same way an agent's output is. This is deliberate — scripts run
    with workspace write access by design, so defending against a
    "malicious script" model is out of scope here. The check below
    only prevents an innocent bug (``os.path.join`` leaking an
    absolute path into attachments) from tricking the Manager into
    reading a file outside the office workspace.

    TOCTOU note: resolving at validate time leaves a small window
    where a symlink could be swapped before the Manager reads the
    path. The attacker would need write access to the workspace,
    which scripts already have; they could just embed the file
    content in ``message`` instead. We accept the TOCTOU.
    """
    if not attachment or not isinstance(attachment, str):
        return False
    candidate = (workspace_root / attachment).resolve()
    try:
        candidate.relative_to(workspace_root.resolve())
    except ValueError:
        return False
    return True


def _is_live_workstream(ws: dict) -> bool:
    """A workstream is live if it's not archived. Archived
    workstreams are planning contexts the user is done with and
    shouldn't receive new script callbacks (they're hidden from
    the chat sidebar and create-task pickers).

    Tolerant of two shapes the sync_config payload uses: some
    callers include a ``status`` field (``"active"`` / ``"archived"``),
    others a bare ``archived: bool`` flag. A workstream with
    neither set is treated as live (backward-compatible).
    """
    status = ws.get("status")
    if isinstance(status, str) and status.lower() == "archived":
        return False
    if ws.get("archived") is True:
        return False
    return True


def _resolve_context_key(
    workstream: str,
    config_store: ConfigStore,
) -> str | None:
    """Translate a script-supplied workstream identifier into the
    backend's ``context_key`` shape (``"general_chat"`` or
    ``"workstream:{uuid}"``).

    Accepts FOUR forms, in order:
      1. Literal ``"general_chat"`` (or the legacy ``"general"``).
      2. A UUID that matches a LIVE (non-archived) workstream.
      3. A short_code that matches a LIVE workstream
         (case-sensitive — short codes are uppercase ASCII).
         Powers the SDK auto-route path: the Runner injects
         ``CUBICLE_WORKSTREAM_SHORT_CODE`` and the script's
         ``cubicle.notify_manager()`` reads it when the caller
         doesn't pass ``workstream`` explicitly.
      4. A name (case-insensitive) that matches a LIVE workstream.

    Returns ``None`` if the identifier doesn't match anything —
    the caller archives the payload as rejected. Archived
    workstreams are filtered out: a script firing into a
    workstream the user has since archived would show up in the
    UI's chat sidebar under a context the user has hidden, which
    is confusing. The rejected-notification audit trail still
    surfaces the drop so the user can unarchive the workstream
    if they actually wanted it delivered.
    """
    key = (workstream or "").strip()
    if not key:
        return None
    if key.lower() in {"general_chat", "general"}:
        return "general_chat"

    workstreams = [
        ws for ws in config_store.get_workstream_list()
        if _is_live_workstream(ws)
    ]

    # UUID match first (exact, case-sensitive).
    for ws in workstreams:
        if ws.get("id") == key:
            return f"workstream:{ws['id']}"

    # Short-code match — exact, case-sensitive. Short codes are
    # always uppercase ASCII (workstream-spec); we don't lowercase
    # the key here because a name happening to share casing with a
    # short code is unlikely and the lowered-name fallback below
    # catches it if it does.
    for ws in workstreams:
        if str(ws.get("short_code", "")).strip() == key:
            return f"workstream:{ws.get('id')}"

    # Fall back to name match — case-insensitive, trimmed. This
    # is the ergonomic path for Auto Script Dev who knows the
    # workstream by display name, not UUID.
    lowered = key.lower()
    for ws in workstreams:
        if str(ws.get("name", "")).strip().lower() == lowered:
            return f"workstream:{ws.get('id')}"
    return None


async def prune_processed(
    script_dir: Path,
    retention_days: int = _PROCESSED_RETENTION_DAYS,
) -> int:
    """Delete processed-archive directories older than the retention
    cap. Called at office startup. Returns the number of day-dirs
    removed (for logging / ops visibility)."""
    archive_root = script_dir / ".outbox" / ".processed"
    if not archive_root.is_dir():
        return 0

    cutoff = datetime.now(timezone.utc).timestamp() - (
        retention_days * 86400
    )
    removed = 0
    for day_dir in archive_root.iterdir():
        if not day_dir.is_dir():
            continue
        try:
            if day_dir.stat().st_mtime < cutoff:
                # Run in a thread to avoid blocking the event loop
                # on a big archive.
                await asyncio.to_thread(shutil.rmtree, day_dir)
                removed += 1
        except OSError as exc:
            logger.warning(
                "outbox_watcher: prune of %s failed: %s",
                day_dir, exc,
            )
    return removed
