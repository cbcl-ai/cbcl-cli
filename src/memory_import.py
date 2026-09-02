"""One-time ``learnings.md`` → office-memory import (office-memory v1, T3.6).

The reviewer used to append durable lessons to a per-workstream
``learnings.md`` file (BEST-01). Office-memory v1 moves lessons into the
backend ``memories`` store; this module migrates the existing files —
lazily, per office, on connect (the ``_run_history_backfill`` pattern in
``handlers.py``):

* per workstream dir, if ``learnings.md`` exists → parse ALL its
  ``## <task> — <cause>`` sections best-effort into ``{title, body}``
  lessons,
* ``POST /api/offices/{oid}/memories/import-lessons`` in chunks of ≤100
  (the backend's per-batch cap; Company-Token bearer; the backend
  dedupes on content-hash slugs, so re-posting any chunk is idempotent),
* once EVERY chunk returned 2xx, rename the file →
  ``learnings.migrated.md`` — the rename IS the migration marker. Any
  failure (offline backend, a pre-memory backend answering 404, a
  network error, a mid-chunk error) leaves the file untouched so the
  next office connect retries; already-imported chunks re-post
  harmlessly.

Nothing here blocks office bring-up: the caller fire-and-forgets the
import via ``_spawn_background`` and every failure is swallow-and-WARN.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import httpx

from src.backend_client import auth_headers
from src.paths import slugify

logger = logging.getLogger(__name__)

# The migrated-file name — presence of learnings.md (not this) is what
# triggers an import, so the rename is the idempotency marker.
MIGRATED_FILENAME = "learnings.migrated.md"
LEARNINGS_FILENAME = "learnings.md"

# The backend contract caps ONE import batch at 100 lessons — the
# parser is uncapped; the POST loop chunks at this size.
MAX_LESSONS_PER_IMPORT = 100
# Mirror the backend memory caps (spec §4.1) so an oversized section is
# clipped client-side instead of bouncing the whole batch with a 422.
_TITLE_MAX_CHARS = 120
_BODY_MAX_CHARS = 2000

_REQUEST_TIMEOUT_SECONDS = 30.0
# How long to wait for the ConfigStore's workstream list (populated by
# the startup bootstrap fetch or the first connector-WS sync_config)
# before giving up for this connect. The file survives a give-up.
_CONFIG_WAIT_SECONDS = 60.0
_CONFIG_POLL_SECONDS = 0.5


def parse_learnings_markdown(text: str) -> list[dict[str, str]]:
    """Parse a ``learnings.md`` into ``[{title, body}]`` lessons.

    Best-effort against the reviewer's historical append shape::

        ## <task readable_id> — <one-line cause class>
        - What went wrong: <one line>
        - What would have prevented it: <one line, actionable>

    Every ``## `` heading starts a lesson (title = heading text, body =
    the section text until the next heading). Prose before the first
    heading is dropped; heading-less non-empty files become ONE lesson so
    hand-written notes are not silently lost. Empty titles/bodies are
    skipped. The output is UNCAPPED — the importer chunks POSTs at
    ``MAX_LESSONS_PER_IMPORT``, so no section is silently dropped.
    """
    raw = text or ""
    lessons: list[dict[str, str]] = []
    lines = raw.splitlines()
    current_title: str | None = None
    current_body: list[str] = []

    def _flush() -> None:
        if current_title is None:
            return
        title = current_title.strip()[:_TITLE_MAX_CHARS]
        body = "\n".join(current_body).strip()[:_BODY_MAX_CHARS]
        if title and body:
            lessons.append({"title": title, "body": body})

    for line in lines:
        if line.startswith("## "):
            _flush()
            current_title = line[3:]
            current_body = []
        elif current_title is not None:
            current_body.append(line)
    _flush()

    if not lessons and raw.strip() and current_title is None:
        # No ``## `` headings at all — keep the whole text as one lesson.
        lessons.append({
            "title": "Imported learnings",
            "body": raw.strip()[:_BODY_MAX_CHARS],
        })
    return lessons


async def import_workstream_learnings(
    *,
    learnings_path: Path,
    workstream_id: str,
    platform_url: str,
    office_id: str,
    security_token: str,
    client: httpx.AsyncClient,
) -> bool:
    """Import ONE workstream's learnings file; True = migrated (renamed).

    Lessons POST in chunks of ≤``MAX_LESSONS_PER_IMPORT`` (the backend's
    per-batch cap) and the rename happens ONLY after EVERY chunk returned
    2xx — any other outcome leaves the file for the next connect (the
    content-hash slugs make re-posting already-imported chunks
    idempotent). An empty/parse-empty file is renamed WITHOUT a POST
    (nothing to import, nothing lost).
    """
    try:
        text = learnings_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning(
            "learnings import: cannot read %s: %s", learnings_path, exc,
        )
        return False

    migrated_path = learnings_path.with_name(MIGRATED_FILENAME)
    lessons = parse_learnings_markdown(text)
    if not lessons:
        try:
            learnings_path.replace(migrated_path)
        except OSError as exc:
            logger.warning(
                "learnings import: cannot rename empty %s: %s",
                learnings_path, exc,
            )
            return False
        logger.info(
            "learnings import: %s had no parseable lessons — marked "
            "migrated without a POST", learnings_path,
        )
        return True

    url = f"{platform_url}/api/offices/{office_id}/memories/import-lessons"
    # One POST per ≤100-lesson chunk (the backend caps a batch at
    # MAX_LESSONS_PER_IMPORT). A mid-chunk failure keeps the file — the
    # content-hash slugs make the next connect's re-post of the already-
    # imported chunks idempotent (they count as ``skipped``).
    chunks = [
        lessons[i : i + MAX_LESSONS_PER_IMPORT]
        for i in range(0, len(lessons), MAX_LESSONS_PER_IMPORT)
    ]
    imported_total = 0
    skipped_total = 0
    for chunk_no, chunk in enumerate(chunks, start=1):
        try:
            resp = await client.post(
                url,
                json={"workstream_id": workstream_id, "lessons": chunk},
                headers=auth_headers(security_token),
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning(
                "learnings import: POST failed for %s (chunk %d/%d: %s) "
                "— file kept for the next connect",
                learnings_path, chunk_no, len(chunks), exc,
            )
            return False
        if not (200 <= resp.status_code < 300):
            # A pre-memory backend answers 404 here — expected until the
            # endpoint ships; the file waits.
            logger.warning(
                "learnings import: backend answered %s for %s "
                "(chunk %d/%d) — file kept for the next connect",
                resp.status_code, learnings_path, chunk_no, len(chunks),
            )
            return False
        try:
            summary = resp.json()
        except ValueError:
            summary = {}
        imported_total += int(summary.get("imported") or 0)
        skipped_total += int(summary.get("skipped") or 0)

    try:
        learnings_path.replace(migrated_path)
    except OSError as exc:
        # The lessons ARE imported (idempotent content-hash slugs), so a
        # re-run after this rare failure re-posts harmlessly.
        logger.warning(
            "learnings import: imported but could not rename %s: %s",
            learnings_path, exc,
        )
        return False
    logger.info(
        "learnings import: migrated %s (%d chunk(s), imported=%d "
        "skipped=%d)",
        learnings_path, len(chunks), imported_total, skipped_total,
    )
    return True


def _workstream_ids_by_slug(config_store: object) -> dict[str, str]:
    """Map ``slugify(workstream name)`` → workstream id from the store."""
    out: dict[str, str] = {}
    for ws in getattr(config_store, "workstreams", None) or []:
        name = str(ws.get("name") or "")
        ws_id = str(ws.get("id") or "")
        if name and ws_id:
            out[slugify(name)] = ws_id
    return out


async def run_learnings_import(
    *,
    workspace_path: Path,
    platform_url: str,
    office_id: str,
    security_token: str,
    config_store: object,
    config_wait_seconds: float = _CONFIG_WAIT_SECONDS,
) -> dict[str, int]:
    """Import every un-migrated ``learnings.md`` under this workspace.

    Fire-and-forget from office init (the reconnect-heal seam). Waits
    briefly for the ConfigStore's workstream list (needed to resolve dir
    slug → workstream_id); a still-empty store, an unmatched dir, or any
    per-file failure just leaves that file for the next connect.
    """
    workstreams_root = Path(workspace_path) / "workstreams"
    if not workstreams_root.is_dir():
        return {"migrated": 0, "left": 0}

    # ``workstreams/.archived/`` (the orphan sweep's archive-don't-delete
    # destination) is DELIBERATELY out of the migration's scope: archived
    # dirs belong to workstreams that no longer exist backend-side, so
    # there is no workstream_id to import their lessons against — the
    # files stay in the archive as the human-readable record.
    archived_root = workstreams_root / ".archived"
    if any(archived_root.glob(f"*/{LEARNINGS_FILENAME}")):
        logger.info(
            "learnings import: skipping %s — archived lessons are "
            "deliberately out of the migration's scope", archived_root,
        )

    pending = sorted(
        d / LEARNINGS_FILENAME
        for d in workstreams_root.iterdir()
        if d.is_dir()
        and d.name != ".archived"
        and (d / LEARNINGS_FILENAME).is_file()
    )
    if not pending:
        return {"migrated": 0, "left": 0}

    deadline = time.monotonic() + config_wait_seconds
    while not _workstream_ids_by_slug(config_store):
        if time.monotonic() >= deadline:
            logger.warning(
                "learnings import: no workstream config after %.0fs — "
                "leaving %d file(s) for the next connect",
                config_wait_seconds, len(pending),
            )
            return {"migrated": 0, "left": len(pending)}
        await asyncio.sleep(_CONFIG_POLL_SECONDS)

    ids_by_slug = _workstream_ids_by_slug(config_store)
    migrated = 0
    left = 0
    async with httpx.AsyncClient() as client:
        for learnings_path in pending:
            ws_slug = learnings_path.parent.name
            workstream_id = ids_by_slug.get(ws_slug)
            if not workstream_id:
                logger.warning(
                    "learnings import: no workstream matches dir %r — "
                    "file kept for the next connect", ws_slug,
                )
                left += 1
                continue
            try:
                ok = await import_workstream_learnings(
                    learnings_path=learnings_path,
                    workstream_id=workstream_id,
                    platform_url=platform_url,
                    office_id=office_id,
                    security_token=security_token,
                    client=client,
                )
            except Exception:
                logger.exception(
                    "learnings import: unexpected failure for %s",
                    learnings_path,
                )
                ok = False
            migrated += 1 if ok else 0
            left += 0 if ok else 1
    return {"migrated": migrated, "left": left}
