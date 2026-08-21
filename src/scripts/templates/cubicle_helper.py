"""Stdlib-only SDK shipped to every mini-project's ``lib/cubicle/``.

This file is the CONTENTS of what will land at
``{script_dir}/lib/cubicle/__init__.py`` when the bootstrap creates
a new script. The outbox watcher reads the JSON files this module
writes.

Scripts import it like:

    import cubicle
    cubicle.notify_manager(
        workstream="Recruitment",
        message="Sourced 87 profiles, 13 flagged — please review.",
        attachments=["outputs/sourced_profiles.json"],
    )

Collections access rides the same module: ``cubicle.collections``
reads and writes the office's shared data tables through the local
cbcl tool proxy (see the ``_Collections`` docstring below).

Design constraints:
  - Zero third-party imports. A script with ``requirements.txt: []``
    must be able to use ``cubicle.notify_manager`` as soon as it
    starts, no pip install needed.
  - Atomic drop via ``os.replace``. A crash mid-write can't produce
    a half-read payload the watcher would either parse partially or
    skip forever.
  - Stateless — no globals, no background threads. Each call is
    self-contained so unit tests can exercise it without setup.
    (The ``collections`` singleton keeps no state either — env is
    read per call.)

This file is also imported by ``tests/test_outbox_watcher.py`` as a
sanity check: the helper's output must round-trip cleanly through
the Pydantic ``OutboxNotifyPayload`` schema the watcher enforces.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid

# SDK version sentinel (spec ui-ux-aug19 D4.5). The daemon's
# script_sync backfill compares the on-disk copy against this line:
# a lib/cubicle/__init__.py that is missing or carries an older (or
# no) sentinel is rewritten from the platform template on the next
# config sync. Bump it whenever the SDK surface changes.
# v3: collections transport hardening — proxy-env-immune opener,
# OSError → CollectionsError mapping, 401 stale-token teaching copy.
# v4: collections CSV import — ``import_csv`` → the proxy lane's
# ``data_import`` action (script-lane completion #1, 2026-08-21).
__CUBICLE_SDK_VERSION__ = 4


def output_dir() -> str:
    """Return the per-task output directory injected by the Runner.

    The Runner sets ``CUBICLE_OUTPUT_DIR`` based on the task's
    workstream + (optional) scope so output from different
    workstreams stays separated and discoverable. The directory is
    pre-created — scripts can write to it directly without a mkdir.

    Path shape:
        * ``/workspace/outputs/{workstream_short_code}/{scope_readable_id}/``
          when the script was triggered from a scoped task.
        * ``/workspace/outputs/{workstream_short_code}/`` when the
          task has no scope (legacy one-off path).
        * ``/workspace/outputs/`` when the script was triggered
          manually with no task context (UI's manual Run button on a
          script not bound to a workstream).

    Use this instead of hardcoding ``/workspace/outputs/`` so the
    same script works correctly across workstreams.

    Example::

        import cubicle
        with open(f"{cubicle.output_dir()}/profiles.json", "w") as f:
            json.dump(results, f)

    Returns:
        Absolute path string. Always set when the script is launched
        via the Runner; falls back to ``/workspace/outputs`` if the
        env var is somehow missing (legacy or test environments).
    """
    return os.environ.get("CUBICLE_OUTPUT_DIR", "/workspace/outputs")


def report_progress(
    done: int,
    total: int | None = None,
    current_item: str = "",
) -> None:
    """Report progress for the running script (ADD-C7).

    Writes ``.progress.json`` in the script directory ATOMICALLY (temp
    file + ``os.replace``) so the Runner's 2-10s poll never observes a
    torn, half-written file — a plain ``write_text`` could be read
    mid-write, JSON-decode-fail, and silently drop the update. The
    Runner forwards each read as a ``script_progress`` Activity event
    for task-linked runs.

    Use this instead of hand-rolling a ``Path('.progress.json')
    .write_text(...)`` — the manual write is the racy pattern this
    helper exists to replace.

    Args:
        done: Items completed so far.
        total: Total items (optional — omit for indeterminate progress).
        current_item: Optional label for the item being processed now.

    Raises:
        RuntimeError: if ``CUBICLE_SCRIPT_DIR`` isn't set (the script is
            running outside the Runner).
    """
    script_dir = os.environ.get("CUBICLE_SCRIPT_DIR")
    if not script_dir:
        raise RuntimeError(
            "cubicle.report_progress: CUBICLE_SCRIPT_DIR env var is not "
            "set. This helper must run inside a mini-project launched "
            "by the Cubicle Runner."
        )

    payload: dict = {"done": done}
    if total is not None:
        payload["total"] = total
    if current_item:
        payload["current_item"] = current_item

    final = os.path.join(script_dir, ".progress.json")
    # pid-suffixed tmp so two concurrent executions of the same script
    # (cron fires while a manual Run is mid-flight) never clobber each
    # other's tmp file. os.replace is atomic on the same filesystem.
    tmp = f"{final}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, final)


def notify_manager(
    message: str,
    workstream: str | None = None,
    attachments: list[str] | None = None,
) -> str:
    """Send a notification to the office Manager.

    Args:
        message: Free-text message. Caps at 8 K characters — longer
            content should be dropped in a file and referenced via
            ``attachments``.
        workstream: Target chat context. Optional — when omitted,
            the helper auto-resolves to the workstream of the task
            that triggered the script. Resolution order:
              1. Caller-supplied value (UUID, display name, or
                 ``"general_chat"``).
              2. ``CUBICLE_WORKSTREAM_SHORT_CODE`` env var the
                 Runner injects for every task-linked execution —
                 covers the common case ("the script ran as part
                 of task X; route the response to X's workstream
                 chat") without forcing scriptmakers to thread the
                 value through their own code.
              3. ``"general_chat"`` fallback for manual UI Runs
                 with no task context.
            If you explicitly want general chat, pass
            ``workstream="general_chat"``; the env-derived value
            won't override an explicit caller argument.
        attachments: Optional list of workspace-relative paths the
            Manager can read. Absolute paths and ``..`` traversal
            attempts are dropped before the payload reaches the
            Manager; the message still goes through with the
            sanitised list so a scriptmaker can see which
            attachments were skipped.

    Returns:
        The filename of the dropped notification (useful for logs
        + debugging — the user can ``ls .outbox/.processed/`` to
        find it after the watcher picks it up).

    Raises:
        RuntimeError: if ``CUBICLE_SCRIPT_DIR`` isn't set. The
            Runner always injects this; running the script outside
            the Runner is a scriptmaker mistake and the exception
            makes the fix obvious.
    """
    script_dir = os.environ.get("CUBICLE_SCRIPT_DIR")
    if not script_dir:
        raise RuntimeError(
            "cubicle.notify_manager: CUBICLE_SCRIPT_DIR env var is not "
            "set. This helper must run inside a mini-project launched "
            "by the Cubicle Runner — it won't work when invoked manually."
        )

    # Auto-derive the target workstream from the task context the
    # Runner injects. Caller-supplied value wins (lets scriptmakers
    # route to general_chat or a different workstream explicitly);
    # the env value covers the common "route to my own workstream"
    # case so a task-launched script doesn't have to know which
    # workstream it belongs to.
    if workstream is None or (
        isinstance(workstream, str) and not workstream.strip()
    ):
        env_ws = (
            os.environ.get("CUBICLE_WORKSTREAM_SHORT_CODE")
            or ""
        ).strip()
        workstream = env_ws or "general_chat"

    outbox = os.path.join(script_dir, ".outbox")
    os.makedirs(outbox, exist_ok=True)

    payload = {
        "v": 1,
        "action": "notify_manager",
        "workstream": workstream,
        "message": message,
        "attachments": list(attachments or []),
        "execution_id": os.environ.get("CUBICLE_EXECUTION_ID"),
        "task_id": os.environ.get("CUBICLE_TASK_ID"),
        "script_name": os.environ.get("CUBICLE_SCRIPT_NAME"),
        "emitted_at": time.time(),
    }

    fname = f"notify-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.json"
    tmp = os.path.join(outbox, fname + ".tmp")
    final = os.path.join(outbox, fname)

    # Write to tempfile + rename so the watcher never sees a
    # half-written file. json.dumps in one shot keeps this small.
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, final)
    return fname


# ── Collections access (office data tables) ─────────────────────────


class CollectionsError(Exception):
    """Raised by every ``cubicle.collections`` call on any failure.

    Attributes:
        status: HTTP-ish status of the failure. Business errors
            carry the datastore's code (400 schema violation, 404
            unknown collection/row, ...); transport problems
            (endpoint env missing, proxy unreachable, malformed
            response) carry ``0``.
        message: Human-readable explanation, safe to log.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _Collections:
    """Client for the office's shared data collections.

    Rows live in the HOST-side office datastore
    (``~/.cubicle/data/<office-slug>.sqlite``), reached through the
    per-office cbcl tool proxy at ``POST /collections/rpc``. The
    Runner injects the endpoint as ``CUBICLE_TOOL_PROXY_URL`` plus a
    NARROW, collections-only bearer token as
    ``CUBICLE_COLLECTIONS_TOKEN`` — the token opens nothing else on
    the proxy. Param shapes match the platform's ``data_*`` RPC
    family exactly (no third wire shape).

    Use the module-level singleton::

        import cubicle

        page = cubicle.collections.query("leads", search="acme")
        for row in page["rows"]:
            print(row["id"], row["data"])
        cubicle.collections.upsert("leads", {"name": "Acme Corp"})

    Every method raises :class:`CollectionsError` on failure —
    schema violations, unknown collections/rows, and transport
    problems alike. No retries by design: the proxy is host-local,
    so a failure is a real answer, not a network blip to paper over.
    """

    _TIMEOUT_SECONDS = 30

    def _rpc(self, action: str, params: dict) -> dict:
        base_url = (os.environ.get("CUBICLE_TOOL_PROXY_URL") or "").strip()
        token = (
            os.environ.get("CUBICLE_COLLECTIONS_TOKEN") or ""
        ).strip()
        if not base_url or not token:
            raise CollectionsError(
                0,
                "cubicle.collections: CUBICLE_TOOL_PROXY_URL / "
                "CUBICLE_COLLECTIONS_TOKEN are not set — collections "
                "access needs a newer cbcl. Restart cbcl (cbcl stop && "
                "cbcl start) on an upgraded daemon, then re-run this "
                "script.",
            )
        body = json.dumps({"action": action, "params": params})
        request = urllib.request.Request(
            base_url.rstrip("/") + "/collections/rpc",
            data=body.encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + token,
            },
            method="POST",
        )
        # Proxy-free opener: the default urllib opener honors
        # HTTP_PROXY/http_proxy from the env, and a script may
        # legitimately declare HTTP_PROXY as a manifest variable for
        # its OWN outbound calls — that must not reroute this
        # host-local call (or the bearer token) through a third-party
        # proxy.
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )
        try:
            with opener.open(
                request, timeout=self._TIMEOUT_SECONDS,
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                error_body = json.loads(
                    exc.read().decode("utf-8", "replace")
                )
                if isinstance(error_body, dict):
                    detail = str(error_body.get("error") or "")
            except Exception:
                detail = ""
            if exc.code == 401:
                raise CollectionsError(
                    401,
                    (detail or "unauthorized")
                    + " — the collections token is re-minted on every "
                    "cbcl restart, so a script that outlived a daemon "
                    "restart holds a stale one. Re-run this script to "
                    "pick up the fresh token.",
                ) from None
            raise CollectionsError(
                exc.code,
                detail or f"HTTP {exc.code} from the cbcl tool proxy",
            ) from None
        except urllib.error.URLError as exc:
            raise CollectionsError(
                0,
                "cubicle.collections: could not reach the cbcl tool "
                f"proxy ({exc.reason}) — is cbcl running on the host?",
            ) from None
        except OSError as exc:
            # Response-phase transport failures (socket.timeout while
            # awaiting/reading the response, a connection reset when
            # the proxy dies mid-exchange) are NOT wrapped in URLError
            # by urllib — catch them here (URLError is an OSError
            # subclass, so this handler must come after it) to keep
            # the "every failure raises CollectionsError" contract.
            raise CollectionsError(
                0,
                "cubicle.collections: the cbcl tool proxy dropped the "
                f"connection mid-call ({exc}) — the daemon may have "
                "restarted. Re-run this script.",
            ) from None
        try:
            result = json.loads(raw.decode("utf-8"))
        except Exception:
            raise CollectionsError(
                0, "cubicle.collections: malformed proxy response",
            ) from None
        if not isinstance(result, dict):
            raise CollectionsError(
                0, "cubicle.collections: malformed proxy response",
            )
        return result

    def query(
        self,
        collection: str,
        *,
        filter: dict | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """List rows of a collection (paged).

        Args:
            collection: Collection slug (e.g. ``"leads"``).
            filter: Exact-match field filters, AND semantics
                (``{"country": "DE"}``).
            search: Case-insensitive substring over text fields.
            limit: Page size, 1-200 (server-clamped).
            offset: Page start.

        Returns:
            ``{"rows": [{"id", "data", "created_at", "updated_at"}],
            "total": N, "limit": M, "offset": O}``.
        """
        params: dict = {
            "collection": collection,
            "limit": limit,
            "offset": offset,
        }
        if filter:
            params["filter"] = filter
        if search:
            params["search"] = search
        return self._rpc("data_rows_list", params)

    def get(self, collection: str, row_id: str) -> dict:
        """Fetch ONE row by id.

        Returns the row dict (``{"id", "data", "created_at",
        "updated_at"}``). Raises :class:`CollectionsError` with
        ``status=404`` when the row does not exist.
        """
        result = self._rpc(
            "data_row_get",
            {"collection": collection, "row_id": row_id},
        )
        row = result.get("row")
        return row if isinstance(row, dict) else result

    def upsert(
        self,
        collection: str,
        data: dict,
        row_id: str | None = None,
    ) -> dict:
        """Create or update one row.

        ``data`` is validated against the collection's schema by the
        daemon (a violation raises ``status=400`` with the teaching
        message). Omit ``row_id`` to create with a generated id;
        pass one to upsert-by-id (a missing row is created).

        Returns ``{"row": {...}, "created": bool, "row_count": N}``.
        """
        params: dict = {"collection": collection, "data": data}
        if row_id is not None:
            params["row_id"] = row_id
        return self._rpc("data_row_upsert", params)

    def delete(self, collection: str, row_id: str) -> dict:
        """Delete one row by id (idempotent — a missing row returns
        ``deleted: false`` rather than raising).

        Returns ``{"deleted": bool, "warnings": [str],
        "row_count": N}`` — ``warnings`` carries the inbound-``ref``
        notices (archive-don't-delete is the better default for
        referenced rows).
        """
        return self._rpc(
            "data_row_delete",
            {"collection": collection, "row_id": row_id},
        )

    def count(self, collection: str) -> int:
        """Return the collection's current row count."""
        result = self._rpc(
            "data_rows_count", {"collection": collection},
        )
        try:
            return int(result.get("count", 0))
        except (TypeError, ValueError):
            return 0

    def import_csv(self, collection: str, csv_text: str) -> dict:
        """Bulk-APPEND rows from CSV text (the platform's
        ``data_import`` action).

        The first CSV record is the header row and must name fields
        of the collection's schema; each data row is schema-validated
        by the daemon — bad rows are skipped with row-numbered
        entries in ``errors`` while good rows commit in one
        transaction with generated ids. Daemon-side caps apply: 2 MB
        of CSV text and 5000 data rows per call (a violation raises
        ``status=400`` with the teaching message — split the file
        and import in parts).

        Returns ``{"imported": N, "skipped": N, "errors": [str],
        "row_count": N}``.
        """
        return self._rpc(
            "data_import",
            {"collection": collection, "csv": csv_text},
        )


# Module-level singleton — the public entry point
# (``cubicle.collections``). Stateless: the endpoint env is read per
# call, so the instance holds nothing the design constraints forbid.
collections = _Collections()
