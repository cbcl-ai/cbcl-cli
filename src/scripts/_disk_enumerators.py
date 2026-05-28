"""Daemon-side enumerators for script execution + notification history.

On a split-host production deployment (backend container on one host,
``cbcl`` daemon on another), the backend's local disk-scan modules
read ``~/.cubicle/workspaces`` — which is empty because the daemon's
workspace lives on the OTHER machine. Result: the Execution History
popup and the Manager-Callback Notifications drawer rendered empty
even when ``status.json`` files and ``.outbox/.processed/*.json``
files were sitting on disk.

This module mirrors the backend's scan logic on the daemon side so
the backend can ask via ``request_bridge`` ("script_list_executions"
/ "script_list_notifications" actions) and get JSON-serialisable
dicts back. Shapes match exactly what
``backend/app/scripts/_executions_service._scan_disk_executions`` +
``backend/app/scripts/_notifications_service.list_notifications``
produce so the merge / dedup paths on the backend work unchanged.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


# Mirror backend's _EXECUTION_ID_RE — accept the same shape the runner
# emits. A divergence here would let one side surface execution dirs
# the other rejects, producing phantom rows.
_EXECUTION_ID_RE = re.compile(r"^[a-zA-Z0-9\-]{1,80}$")

# Mirror backend's _NOTIFY_FILENAME_RE for the same reason.
_NOTIFY_FILENAME_RE = re.compile(
    r"^(?P<reason>[a-z0-9-]+\.)?notify-(?P<ts>[A-Za-z0-9]+)-"
    r"[a-f0-9]{6,16}\.json$"
)

# Per-call hard cap. Keeps a runaway cron (one run per minute for
# months) from ballooning into a tens-of-thousands-file walk.
_DEFAULT_EXECUTIONS_LIMIT = 200
_DEFAULT_NOTIFICATIONS_LIMIT = 100


def _iso(value) -> str | None:
    """Coerce a datetime / ISO string to an ISO string for JSON."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value
    return None


def list_executions_on_disk(
    workspace_path: str, script_name: str, limit: int,
) -> list[dict]:
    """Enumerate the newest ``limit`` executions for one script.

    Returns a list of dicts shaped to mirror the backend's
    ``_scan_disk_executions`` output. Wraps datetimes as ISO strings
    so the response goes through JSON cleanly; the backend converts
    them back via ``_parse_iso``.
    """
    if limit <= 0 or limit > _DEFAULT_EXECUTIONS_LIMIT * 5:
        limit = _DEFAULT_EXECUTIONS_LIMIT
    base_dir = Path(workspace_path) / ".scripts" / script_name / "executions"
    if not base_dir.is_dir():
        return []
    try:
        all_names = sorted(
            (p.name for p in base_dir.iterdir() if p.is_dir()),
            reverse=True,
        )
    except OSError as exc:
        logger.warning(
            "list_executions_on_disk: failed to list %s: %s", base_dir, exc,
        )
        return []
    candidate_names = all_names[:limit]
    records: list[dict] = []
    for name in candidate_names:
        if not _EXECUTION_ID_RE.match(name):
            logger.warning(
                "list_executions_on_disk: skipping non-conforming "
                "execution dir name: %s", name,
            )
            continue
        status_file = base_dir / name / "status.json"
        if not status_file.is_file():
            continue
        try:
            raw = json.loads(status_file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "list_executions_on_disk: skipping corrupt "
                "status.json at %s: %s", status_file, exc,
            )
            continue
        if not isinstance(raw, dict):
            continue
        records.append({
            "execution_id": name,
            "status": raw.get("status") or "unknown",
            "triggered_by": raw.get("triggered_by"),
            # task_id stays a string here; backend re-parses via
            # uuid.UUID() in _scan_disk_executions's caller path.
            "task_id": raw.get("task_id"),
            "started_at": _iso(raw.get("started_at")),
            "completed_at": _iso(raw.get("completed_at")),
            "duration_seconds": raw.get("duration_seconds"),
            "error_message": raw.get("error_message"),
        })
    return records


def _collect_notify_dir(
    directory: Path,
    items: list[dict],
    *,
    rejected: bool,
    limit: int,
) -> None:
    """Append notify-*.json summaries from ONE directory to ``items``.

    Mirrors backend's ``_collect_notifications`` field shape EXACTLY
    so the frontend's ``ScriptNotification`` interface keeps working
    when the daemon RPC path returns data. The required fields:

        filename, emitted_at_ms, rejected, reason, workstream,
        message, attachments, script_name, execution_id

    Drift between this output shape and the backend's same-shape
    output is what made the Notifications popup render empty in
    v0.2.55 even when files were on disk — frontend's
    ``fetchScriptNotifications`` got dicts missing ``rejected`` and
    ``script_name`` and discarded them.
    """
    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        logger.warning(
            "list_notifications_on_disk: failed to list %s: %s",
            directory, exc,
        )
        return
    for entry in entries:
        if len(items) >= limit:
            return
        if not entry.is_file():
            continue
        match = _NOTIFY_FILENAME_RE.match(entry.name)
        if not match:
            continue
        ts_raw = match.group("ts")
        ts_ms: int | None = None
        if ts_raw.isdigit():
            # SDK writes ``{int(time.time() * 1000)}`` — already ms.
            # Accept second-epoch as fallback (≤10 digits ⇒ seconds).
            try:
                ts_ms = int(ts_raw)
                if len(ts_raw) <= 10:
                    ts_ms *= 1000
            except ValueError:
                ts_ms = None
        raw_reason = match.group("reason")
        reason_clean = raw_reason.rstrip(".") if raw_reason else None
        try:
            payload = json.loads(entry.read_text(errors="replace"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug(
                "list_notifications_on_disk: skipping malformed "
                "notification %s: %s", entry, exc,
            )
            continue
        if not isinstance(payload, dict):
            continue
        # Fallback timestamp: payload's ``emitted_at`` (Unix
        # seconds float) — agent-authored files may have non-ms
        # filename stamps.
        if ts_ms is None:
            emitted_at = payload.get("emitted_at")
            if isinstance(emitted_at, (int, float)):
                try:
                    ts_ms = int(emitted_at * 1000)
                except (ValueError, OverflowError):
                    ts_ms = None
        raw_attachments = payload.get("attachments")
        attachments = (
            raw_attachments if isinstance(raw_attachments, list) else []
        )
        items.append({
            "filename": entry.name,
            "emitted_at_ms": ts_ms,
            "rejected": rejected,
            "reason": reason_clean,
            # Caps mirror the backend's caps so a single bloated
            # notify can't blow the response size.
            "workstream": str(payload.get("workstream", ""))[:200],
            "message": str(payload.get("message", ""))[:8192],
            "attachments": [
                str(a)[:512] for a in attachments[:10]
            ],
            "script_name": str(payload.get("script_name") or "")[:100],
            "execution_id": str(payload.get("execution_id") or "")[:120],
        })


def list_notifications_on_disk(
    workspace_path: str, script_name: str, limit: int,
) -> list[dict]:
    """Enumerate the newest ``limit`` notify_manager drops for one script.

    Walks ``.scripts/<name>/.outbox/.processed/{YYYY-MM-DD}/`` and
    its ``rejected/`` sub-tree. Returns dicts shaped to mirror the
    backend's ``_collect_notifications`` output exactly so the
    frontend's ScriptNotification interface keeps working.

    Newest-first sort by ``emitted_at_ms`` from the filename ms
    epoch — matches the backend's sort key so results are stable
    regardless of which side served the request.
    """
    if limit <= 0 or limit > _DEFAULT_NOTIFICATIONS_LIMIT * 5:
        limit = _DEFAULT_NOTIFICATIONS_LIMIT
    processed_root = (
        Path(workspace_path)
        / ".scripts"
        / script_name
        / ".outbox"
        / ".processed"
    )
    if not processed_root.is_dir():
        return []
    items: list[dict] = []
    # Same "newest-day first, 3x budget" walk the backend does so a
    # busy day's rejected files don't crowd out the prior day's
    # successful drops in the limited output.
    budget = max(1, limit) * 3
    try:
        day_dirs = sorted(
            (p for p in processed_root.iterdir() if p.is_dir()),
            key=lambda p: p.name,
            reverse=True,
        )
    except OSError as exc:
        logger.warning(
            "list_notifications_on_disk: failed to list %s: %s",
            processed_root, exc,
        )
        return []
    for day_dir in day_dirs:
        _collect_notify_dir(
            day_dir, items, rejected=False, limit=budget,
        )
        rejected_root = day_dir / "rejected"
        if rejected_root.is_dir():
            _collect_notify_dir(
                rejected_root, items, rejected=True, limit=budget,
            )
        if len(items) >= budget:
            break
    items.sort(
        key=lambda it: it.get("emitted_at_ms") or 0,
        reverse=True,
    )
    return items[:limit]
