"""Office-local SQLite datastore for Flow Studio collections (FS-P1.T3/T4).

Collection SCHEMAS are platform-side (Postgres, synced to the daemon via
``sync_config.collections``); collection ROWS are business data and live
on the user's machine only — in ``~/.cubicle/data/<office-slug>.sqlite``,
owned by this module (spec ``docs/specs/flow-studio/spec.md`` §5.2).

One generic table, no per-collection DDL::

    rows(collection TEXT, id TEXT, data JSON, created_at, updated_at,
         PRIMARY KEY(collection, id))

Schema validation happens HERE, against the schema cache the daemon
holds in :class:`src.config_sync.sync_service.ConfigStore` (fed by
every ``sync_config`` push). The platform reads/writes rows only via
the request-scoped ``data_*`` RequestBridge actions (spec §5.3),
dispatched from ``src._handlers._requests`` to
:meth:`OfficeDatastore.handle_request` — rows are never persisted
backend-side.

Error convention mirrors ``fs_*``: a failed action responds with
``{"error": <human message>, "status": <http-int>}`` and the backend
re-raises ``HTTPException(status, error)``. Success payloads are the
exact per-action shapes in ws-protocol.md §3.5 ("Collections
datastore" group).

Concurrency: writes are serialized per office DB by an asyncio lock
(the ``variable_manager`` precedent) and each write is one SQLite
transaction, so a crash mid-write never leaves a half-applied
mutation. Malformed stored rows degrade to WARNING + skip on reads —
one bad row never crashes a list.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import sqlite3
import uuid
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


# Row id shape — mirrors the backend row-proxy validation
# (``backend/app/collections/router.py``). Enforced daemon-side too
# because the FS-P3 Curator/worker row tools ride these actions
# directly, without the backend regex in front.
ROW_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Per-row parameter definitions (a ``params_schema`` field's VALUE)
# use the simple types only — a parameter can't itself be a ref or a
# nested params_schema.
_PARAM_TYPES = ("text", "number", "bool", "enum", "date")

# Import caps (FS-P1.T4): reject oversized CSVs with a teaching error
# instead of grinding through them. v1 is APPEND only.
IMPORT_MAX_CSV_CHARS = 2 * 1024 * 1024  # 2 MB
IMPORT_MAX_ROWS = 5000
IMPORT_MAX_ERRORS = 20  # row-numbered error strings kept in the response

# Per-DB-path write locks — the ``variable_manager._SET_BINDING_LOCKS``
# precedent. Process-local scope is fine: the daemon is single-process
# and is the ONLY writer of the office datastore.
_WRITE_LOCKS: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


class DatastoreError(Exception):
    """A datastore failure with an HTTP-ish status for the wire.

    ``str(exc)`` is the human/teaching message that lands in the
    ``{"error": ..., "status": ...}`` response dict.
    """

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Value validation (module-level for unit-testability)
# ---------------------------------------------------------------------------


def _is_valid_date_string(value: str) -> bool:
    """Accept ISO dates (``2026-08-05``) and full ISO timestamps."""
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        pass
    try:
        datetime.fromisoformat(value)
        return True
    except ValueError:
        return False


def _validate_ref_value(field_name: str, value: Any) -> str | None:
    """A ``ref`` field holds ``{id, display}`` — a row id + display value."""
    if not isinstance(value, dict):
        return (
            f"field {field_name!r} is a ref — its value must be an object "
            "{id, display}"
        )
    extra = sorted(set(value) - {"id", "display"})
    if extra:
        return (
            f"field {field_name!r} ref value has unknown keys "
            f"{extra} (only 'id' and 'display' are allowed)"
        )
    rid = value.get("id")
    if not isinstance(rid, str) or not rid.strip():
        return f"field {field_name!r} ref value needs a non-empty string 'id'"
    display = value.get("display", "")
    if not isinstance(display, str):
        return f"field {field_name!r} ref 'display' must be a string"
    return None


def _validate_param_default(
    field_name: str,
    param_name: str,
    ptype: str,
    options: list,
    default: Any,
) -> str | None:
    """Type-check a parameter definition's ``default`` against its own type."""
    prefix = f"field {field_name!r} parameter {param_name!r}"
    if ptype == "text":
        if not isinstance(default, str):
            return f"{prefix}: default must be a string"
    elif ptype == "number":
        if isinstance(default, bool) or not isinstance(default, (int, float)):
            return f"{prefix}: default must be a number"
    elif ptype == "bool":
        if not isinstance(default, bool):
            return f"{prefix}: default must be a boolean"
    elif ptype == "enum":
        if not isinstance(default, str) or (options and default not in options):
            return f"{prefix}: default must be one of its options"
    elif ptype == "date":
        if not isinstance(default, str) or not _is_valid_date_string(default):
            return f"{prefix}: default must be an ISO date string"
    return None


def _validate_params_schema_value(field_name: str, value: Any) -> str | None:
    """A ``params_schema`` field's VALUE (per row) is itself a list of
    parameter definitions ``{name, type, options, default, required,
    help}`` — the quoter's parametric-row feature (spec §5.1)."""
    if not isinstance(value, list):
        return (
            f"field {field_name!r} is a params_schema — its value must be a "
            "LIST of parameter definitions "
            "[{name, type, options?, default?, required?, help?}]"
        )
    if len(value) > 50:
        return (
            f"field {field_name!r} params_schema value exceeds 50 parameter "
            "definitions"
        )
    seen: set[str] = set()
    for i, param in enumerate(value):
        if not isinstance(param, dict):
            return f"field {field_name!r} parameter #{i + 1} must be an object"
        name = param.get("name")
        if not isinstance(name, str) or not name.strip():
            return (
                f"field {field_name!r} parameter #{i + 1} needs a non-empty "
                "string 'name'"
            )
        if name in seen:
            return f"field {field_name!r} has duplicate parameter {name!r}"
        seen.add(name)
        ptype = param.get("type")
        if ptype not in _PARAM_TYPES:
            return (
                f"field {field_name!r} parameter {name!r} has unknown type "
                f"{ptype!r} (one of: {', '.join(_PARAM_TYPES)})"
            )
        options = param.get("options", [])
        if options is None:
            options = []
        if not isinstance(options, list) or any(
            not isinstance(o, str) for o in options
        ):
            return (
                f"field {field_name!r} parameter {name!r}: 'options' must be "
                "a list of strings"
            )
        if ptype == "enum" and not options:
            return (
                f"field {field_name!r} enum parameter {name!r} needs at "
                "least one option"
            )
        required = param.get("required", False)
        if not isinstance(required, bool):
            return (
                f"field {field_name!r} parameter {name!r}: 'required' must "
                "be a boolean"
            )
        help_text = param.get("help", "")
        if help_text is not None and not isinstance(help_text, str):
            return (
                f"field {field_name!r} parameter {name!r}: 'help' must be a " "string"
            )
        default = param.get("default")
        if default is not None:
            err = _validate_param_default(
                field_name,
                name,
                ptype,
                options,
                default,
            )
            if err:
                return err
    return None


def _validate_value(field: dict, value: Any) -> str | None:
    """Type-check ONE present, non-null value against its field def.

    Returns an error string, or ``None`` when the value is valid.
    """
    name = field.get("name", "?")
    ftype = field.get("type", "text")
    if ftype == "text":
        if not isinstance(value, str):
            return f"field {name!r} must be a string"
    elif ftype == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"field {name!r} must be a number"
    elif ftype == "bool":
        if not isinstance(value, bool):
            return f"field {name!r} must be a boolean"
    elif ftype == "enum":
        options = field.get("options") or []
        if not isinstance(value, str):
            return f"field {name!r} must be a string (one of its options)"
        if options and value not in options:
            return (
                f"field {name!r} must be one of: {', '.join(options)} "
                f"(got {value!r})"
            )
    elif ftype == "date":
        if not isinstance(value, str) or not _is_valid_date_string(value):
            return f"field {name!r} must be an ISO date string " "(e.g. 2026-08-05)"
    elif ftype == "ref":
        return _validate_ref_value(name, value)
    elif ftype == "params_schema":
        return _validate_params_schema_value(name, value)
    # Unknown field type in a synced schema — the backend validates
    # types at save, so this is a version-skew edge. Fail open (accept)
    # rather than wedge every row write on a type this daemon predates.
    return None


def validate_row_data(fields: list[dict], data: dict) -> list[str]:
    """Validate a row's ``data`` dict against the synced field list.

    Returns a list of teaching-error strings — empty means valid.
    Checks: unknown fields, missing required fields, and per-type
    value shapes (incl. ``ref`` ``{id, display}`` and per-row
    ``params_schema`` definition lists).
    """
    errors: list[str] = []
    by_name: dict[str, dict] = {}
    for f in fields:
        if isinstance(f, dict) and isinstance(f.get("name"), str):
            by_name[f["name"]] = f
    unknown = sorted(k for k in data if k not in by_name)
    if unknown:
        errors.append(
            f"unknown field(s): {', '.join(unknown)} "
            f"(collection fields: {', '.join(by_name) or 'none'})"
        )
    for name, field in by_name.items():
        value = data.get(name)
        if value is None:
            if field.get("required"):
                errors.append(f"missing required field {name!r}")
            continue
        err = _validate_value(field, value)
        if err:
            errors.append(err)
    return errors


def coerce_csv_cell(field: dict, cell: str) -> Any:
    """Coerce a CSV cell string into the field's value type.

    Raises ``ValueError`` with a human message on an uncoercible cell.
    ``text`` / ``enum`` / ``date`` cells pass through as strings
    (membership/format is the validator's job). ``ref`` cells accept a
    JSON ``{id, display}`` object or a bare row id (wrapped as
    ``{id: cell, display: cell}``). ``params_schema`` cells must be a
    JSON list.
    """
    name = field.get("name", "?")
    ftype = field.get("type", "text")
    if ftype == "number":
        try:
            return int(cell)
        except ValueError:
            pass
        try:
            return float(cell)
        except ValueError:
            raise ValueError(f"field {name!r}: {cell!r} is not a number")
    if ftype == "bool":
        lowered = cell.strip().lower()
        if lowered in ("true", "1", "yes", "y"):
            return True
        if lowered in ("false", "0", "no", "n"):
            return False
        raise ValueError(f"field {name!r}: {cell!r} is not a boolean (use true/false)")
    if ftype == "ref":
        try:
            parsed = json.loads(cell)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        return {"id": cell, "display": cell}
    if ftype == "params_schema":
        try:
            parsed = json.loads(cell)
        except json.JSONDecodeError:
            raise ValueError(f"field {name!r}: params_schema cells must be a JSON list")
        return parsed
    # text | enum | date (and unknown types, fail-open like the
    # validator): the string is the value.
    return cell


# ---------------------------------------------------------------------------
# The datastore
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class OfficeDatastore:
    """The office-local collections rows store (SQLite).

    Parameters
    ----------
    db_path:
        ``~/.cubicle/data/<office-slug>.sqlite`` (from
        :func:`src.paths.get_datastore_path`). Any path works — tests
        pass a tmp dir.
    config_store:
        The office's :class:`~src.config_sync.sync_service.ConfigStore`
        (or any object with a ``collections`` list attribute) — the
        schema cache fed by ``sync_config.collections``. Read LIVE on
        every operation so a schema push applies without re-wiring.
    """

    def __init__(self, db_path: Path | str, config_store: Any) -> None:
        self._db_path = Path(db_path)
        self._config = config_store

    # -- schema cache ------------------------------------------------------

    def _synced_collections(self) -> list[dict]:
        collections = getattr(self._config, "collections", None) or []
        return [c for c in collections if isinstance(c, dict)]

    def get_collection_schema(self, collection: str) -> list[dict] | None:
        """The synced field list for ``collection`` — ``None`` if unknown."""
        for item in self._synced_collections():
            if item.get("name") == collection:
                schema = item.get("schema")
                return schema if isinstance(schema, list) else []
        return None

    def _require_schema(self, collection: str) -> list[dict]:
        if not collection:
            raise DatastoreError("collection is required", 400)
        schema = self.get_collection_schema(collection)
        if schema is None:
            known = sorted(c.get("name", "") for c in self._synced_collections())
            raise DatastoreError(
                f"unknown collection {collection!r} "
                f"(synced collections: {', '.join(known) or 'none'})",
                404,
            )
        return schema

    # -- sqlite core (sync — run via asyncio.to_thread) --------------------

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS rows ("
            " collection TEXT NOT NULL,"
            " id TEXT NOT NULL,"
            " data JSON NOT NULL,"
            " created_at TEXT NOT NULL,"
            " updated_at TEXT NOT NULL,"
            " PRIMARY KEY (collection, id)"
            ")"
        )
        return conn

    def _row_from_db(
        self,
        collection: str,
        row_id: str,
        data_json: Any,
        created_at: str,
        updated_at: str,
    ) -> dict | None:
        """Decode one stored row — malformed rows degrade to WARNING +
        ``None`` (skipped by the caller), never a crash."""
        try:
            data = json.loads(data_json)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "Skipping malformed stored row %s/%s (data is not valid " "JSON)",
                collection,
                row_id,
            )
            return None
        if not isinstance(data, dict):
            logger.warning(
                "Skipping malformed stored row %s/%s (data is %s, not an " "object)",
                collection,
                row_id,
                type(data).__name__,
            )
            return None
        return {
            "id": row_id,
            "data": data,
            "created_at": created_at,
            "updated_at": updated_at,
        }

    def _load_collection_rows(
        self,
        conn: sqlite3.Connection,
        collection: str,
    ) -> list[dict]:
        cur = conn.execute(
            "SELECT id, data, created_at, updated_at FROM rows "
            "WHERE collection = ? ORDER BY created_at ASC, id ASC",
            (collection,),
        )
        rows: list[dict] = []
        for row_id, data_json, created_at, updated_at in cur.fetchall():
            row = self._row_from_db(
                collection,
                row_id,
                data_json,
                created_at,
                updated_at,
            )
            if row is not None:
                rows.append(row)
        return rows

    def _count(self, conn: sqlite3.Connection, collection: str) -> int:
        cur = conn.execute(
            "SELECT COUNT(*) FROM rows WHERE collection = ?",
            (collection,),
        )
        return int(cur.fetchone()[0])

    # -- filtering ---------------------------------------------------------

    @staticmethod
    def _matches_filter(row_data: dict, filters: dict) -> bool:
        """Exact-match AND over data fields. A scalar filter against a
        stored ``ref`` object matches on the ref's ``id`` (useful for
        "who references X" reads)."""
        for field, expected in filters.items():
            stored = row_data.get(field)
            if isinstance(stored, dict) and not isinstance(expected, dict):
                if stored.get("id") != expected:
                    return False
                continue
            if stored != expected:
                return False
        return True

    @staticmethod
    def _matches_search(
        row_data: dict,
        text_fields: list[str],
        needle: str,
    ) -> bool:
        """Case-insensitive substring over text-typed field values."""
        for field in text_fields:
            value = row_data.get(field)
            if isinstance(value, str) and needle in value.lower():
                return True
        return False

    # -- async API ---------------------------------------------------------

    async def list_rows(
        self,
        collection: str,
        *,
        filters: dict | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        schema = self._require_schema(collection)
        if filters is not None and not isinstance(filters, dict):
            raise DatastoreError("filter must be a JSON object", 400)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 50
        try:
            offset = int(offset)
        except (TypeError, ValueError):
            offset = 0
        limit = max(1, min(limit, 200))
        offset = max(0, offset)

        rows = await asyncio.to_thread(self._list_rows_sync, collection)

        if filters:
            rows = [r for r in rows if self._matches_filter(r["data"], filters)]
        if search:
            needle = str(search).lower()
            text_fields = [
                f.get("name", "")
                for f in schema
                if isinstance(f, dict) and f.get("type") == "text"
            ]
            rows = [
                r for r in rows if self._matches_search(r["data"], text_fields, needle)
            ]
        total = len(rows)
        page = rows[offset : offset + limit]
        return {"rows": page, "total": total, "limit": limit, "offset": offset}

    def _list_rows_sync(self, collection: str) -> list[dict]:
        if not self._db_path.exists():
            return []
        with closing(self._connect()) as conn:
            return self._load_collection_rows(conn, collection)

    async def get_row(self, collection: str, row_id: str) -> dict:
        self._require_schema(collection)
        if not row_id:
            raise DatastoreError("row_id is required", 400)
        row = await asyncio.to_thread(self._get_row_sync, collection, row_id)
        if row is None:
            raise DatastoreError(
                f"row {row_id!r} not found in collection {collection!r}",
                404,
            )
        return {"row": row}

    def _get_row_sync(self, collection: str, row_id: str) -> dict | None:
        if not self._db_path.exists():
            return None
        with closing(self._connect()) as conn:
            cur = conn.execute(
                "SELECT id, data, created_at, updated_at FROM rows "
                "WHERE collection = ? AND id = ?",
                (collection, row_id),
            )
            hit = cur.fetchone()
        if hit is None:
            return None
        return self._row_from_db(collection, hit[0], hit[1], hit[2], hit[3])

    async def upsert_row(
        self,
        collection: str,
        data: Any,
        row_id: str | None = None,
    ) -> dict:
        schema = self._require_schema(collection)
        if not isinstance(data, dict):
            raise DatastoreError(
                "data must be a JSON object mapping field names to values",
                400,
            )
        if row_id is not None and not ROW_ID_RE.match(str(row_id)):
            raise DatastoreError(
                "row_id must match ^[A-Za-z0-9_-]{1,64}$",
                400,
            )
        errors = validate_row_data(schema, data)
        if errors:
            raise DatastoreError(
                f"row does not match the {collection!r} schema: "
                + "; ".join(errors[:5]),
                400,
            )
        async with _WRITE_LOCKS[str(self._db_path)]:
            return await asyncio.to_thread(
                self._upsert_row_sync,
                collection,
                data,
                row_id,
            )

    def _upsert_row_sync(
        self,
        collection: str,
        data: dict,
        row_id: str | None,
    ) -> dict:
        now = _utc_now_iso()
        minted = row_id or uuid.uuid4().hex
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with closing(self._connect()) as conn:
            with conn:  # one transaction — atomic
                cur = conn.execute(
                    "SELECT created_at FROM rows " "WHERE collection = ? AND id = ?",
                    (collection, minted),
                )
                existing = cur.fetchone()
                created = existing is None
                created_at = existing[0] if existing else now
                conn.execute(
                    "INSERT INTO rows "
                    "(collection, id, data, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(collection, id) DO UPDATE SET "
                    "data = excluded.data, updated_at = excluded.updated_at",
                    (collection, minted, payload, created_at, now),
                )
            row_count = self._count(conn, collection)
        return {
            "row": {
                "id": minted,
                "data": data,
                "created_at": created_at,
                "updated_at": now,
            },
            "created": created,
            "row_count": row_count,
        }

    async def delete_row(self, collection: str, row_id: str) -> dict:
        self._require_schema(collection)
        if not row_id:
            raise DatastoreError("row_id is required", 400)
        async with _WRITE_LOCKS[str(self._db_path)]:
            return await asyncio.to_thread(
                self._delete_row_sync,
                collection,
                row_id,
            )

    def _delete_row_sync(self, collection: str, row_id: str) -> dict:
        with closing(self._connect()) as conn:
            with conn:
                cur = conn.execute(
                    "DELETE FROM rows WHERE collection = ? AND id = ?",
                    (collection, row_id),
                )
                deleted = cur.rowcount > 0
            warnings: list[str] = []
            if deleted:
                warnings = self._inbound_ref_warnings(conn, collection, row_id)
            row_count = self._count(conn, collection)
        return {
            "deleted": deleted,
            "warnings": warnings,
            "row_count": row_count,
        }

    def _inbound_ref_warnings(
        self,
        conn: sqlite3.Connection,
        collection: str,
        row_id: str,
    ) -> list[str]:
        """v1 warn-only inbound-``ref`` scan (spec §5.4): find rows in
        OTHER synced collections whose ``ref`` fields point at the
        deleted row. Hard row-level integrity is v1.1."""
        warnings: list[str] = []
        for item in self._synced_collections():
            other_name = item.get("name", "")
            schema = item.get("schema")
            if not other_name or not isinstance(schema, list):
                continue
            ref_fields = [
                f.get("name", "")
                for f in schema
                if isinstance(f, dict)
                and f.get("type") == "ref"
                and f.get("ref_to") == collection
            ]
            if not ref_fields:
                continue
            counts: dict[str, int] = {f: 0 for f in ref_fields}
            for row in self._load_collection_rows(conn, other_name):
                for field in ref_fields:
                    value = row["data"].get(field)
                    if isinstance(value, dict) and value.get("id") == row_id:
                        counts[field] += 1
            for field, n in counts.items():
                if n:
                    warnings.append(
                        f"{n} row(s) in collection {other_name!r} still "
                        f"reference the deleted row via field {field!r} — "
                        "those references are now dangling"
                    )
        return warnings

    async def count_rows(self, collection: str) -> dict:
        self._require_schema(collection)
        count = await asyncio.to_thread(self._count_rows_sync, collection)
        return {"count": count}

    def _count_rows_sync(self, collection: str) -> int:
        if not self._db_path.exists():
            return 0
        with closing(self._connect()) as conn:
            return self._count(conn, collection)

    async def import_csv(self, collection: str, csv_text: Any) -> dict:
        """v1 CSV APPEND: header row = field names; each data row is
        schema-validated; bad rows are skipped with row-numbered
        errors (capped at ``IMPORT_MAX_ERRORS``); good rows commit in
        ONE transaction with daemon-minted ids."""
        schema = self._require_schema(collection)
        if not isinstance(csv_text, str):
            raise DatastoreError("csv must be a string", 400)
        if len(csv_text) > IMPORT_MAX_CSV_CHARS:
            raise DatastoreError(
                f"CSV is too large ({len(csv_text)} chars; limit "
                f"{IMPORT_MAX_CSV_CHARS}). Split the file and import in "
                "parts.",
                400,
            )

        try:
            parsed = list(csv.reader(io.StringIO(csv_text)))
        except csv.Error as exc:
            raise DatastoreError(f"CSV could not be parsed: {exc}", 400)
        # Drop fully-blank records (trailing newlines etc.).
        parsed = [record for record in parsed if any(cell.strip() for cell in record)]
        if not parsed:
            count = await asyncio.to_thread(self._count_rows_sync, collection)
            return {
                "imported": 0,
                "skipped": 0,
                "errors": [],
                "row_count": count,
            }

        header = [cell.strip() for cell in parsed[0]]
        data_rows = parsed[1:]
        if len(data_rows) > IMPORT_MAX_ROWS:
            raise DatastoreError(
                f"CSV has {len(data_rows)} data rows (limit "
                f"{IMPORT_MAX_ROWS}). Split the file and import in parts.",
                400,
            )

        by_name = {
            f.get("name"): f for f in schema if isinstance(f, dict) and f.get("name")
        }
        unknown = sorted(h for h in header if h and h not in by_name)
        if unknown:
            raise DatastoreError(
                f"CSV header names unknown field(s): {', '.join(unknown)}. "
                f"The {collection!r} collection has: "
                f"{', '.join(by_name) or 'none'}.",
                400,
            )
        if not any(header):
            raise DatastoreError("CSV header row is empty", 400)

        good: list[dict] = []
        errors: list[str] = []
        skipped = 0
        for index, record in enumerate(data_rows):
            row_number = index + 2  # 1-based, counting the header row
            if len(record) > len(header):
                skipped += 1
                if len(errors) < IMPORT_MAX_ERRORS:
                    errors.append(
                        f"row {row_number}: has {len(record)} cells but the "
                        f"header has {len(header)}"
                    )
                continue
            data: dict[str, Any] = {}
            cell_error: str | None = None
            for column, cell in zip(header, record):
                if not column or not cell.strip():
                    continue  # empty cell → field absent
                try:
                    data[column] = coerce_csv_cell(by_name[column], cell)
                except ValueError as exc:
                    cell_error = str(exc)
                    break
            if cell_error is None:
                row_errors = validate_row_data(schema, data)
                if row_errors:
                    cell_error = row_errors[0]
            if cell_error is not None:
                skipped += 1
                if len(errors) < IMPORT_MAX_ERRORS:
                    errors.append(f"row {row_number}: {cell_error}")
                continue
            good.append(data)

        async with _WRITE_LOCKS[str(self._db_path)]:
            row_count = await asyncio.to_thread(
                self._append_rows_sync,
                collection,
                good,
            )
        return {
            "imported": len(good),
            "skipped": skipped,
            "errors": errors,
            "row_count": row_count,
        }

    def _append_rows_sync(self, collection: str, rows: list[dict]) -> int:
        now = _utc_now_iso()
        with closing(self._connect()) as conn:
            with conn:  # one transaction for the whole import
                for data in rows:
                    conn.execute(
                        "INSERT INTO rows "
                        "(collection, id, data, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            collection,
                            uuid.uuid4().hex,
                            json.dumps(data, ensure_ascii=False, sort_keys=True),
                            now,
                            now,
                        ),
                    )
            return self._count(conn, collection)

    # -- health heartbeat --------------------------------------------------

    async def collection_counts(self) -> dict[str, int]:
        """Per-collection row counts for the health report — the
        daemon→backend leg of the backend's cached ``row_count``.
        Keys are the SYNCED collection names (0 when no rows yet);
        best-effort: any failure returns what could be read."""
        names = [c.get("name", "") for c in self._synced_collections() if c.get("name")]
        if not names:
            return {}
        if not self._db_path.exists():
            return {name: 0 for name in names}
        try:
            table_counts = await asyncio.to_thread(self._group_counts_sync)
        except Exception:
            logger.warning(
                "Failed to read collection counts from %s",
                self._db_path,
                exc_info=True,
            )
            return {name: 0 for name in names}
        return {name: int(table_counts.get(name, 0)) for name in names}

    def _group_counts_sync(self) -> dict[str, int]:
        with closing(self._connect()) as conn:
            cur = conn.execute(
                "SELECT collection, COUNT(*) FROM rows GROUP BY collection",
            )
            return {name: count for name, count in cur.fetchall()}

    # -- RequestBridge entry ----------------------------------------------

    async def handle_request(
        self,
        message: dict,
        send_fn: Callable[[dict], Any],
    ) -> None:
        """Handle one ``data_*`` RequestBridge action and ALWAYS send a
        ``response`` frame (the ``fs_handler.handle_request`` posture).

        Args:
            message: The full request dict
                (``type``, ``request_id``, ``action``, ``params``).
            send_fn: Async function sending the response over the WS.
        """
        request_id = message.get("request_id", "")
        action = message.get("action", "")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        try:
            data = await self._dispatch(action, params)
        except DatastoreError as exc:
            data = {"error": str(exc), "status": exc.status}
        except sqlite3.Error as exc:
            logger.exception("Datastore SQLite failure on %s", action)
            data = {"error": f"datastore storage error: {exc}", "status": 500}
        except Exception as exc:
            logger.exception("Datastore action failed: %s", action)
            data = {"error": f"internal datastore error: {exc}", "status": 500}
        await send_fn(
            {
                "type": "response",
                "request_id": request_id,
                "data": data,
            }
        )

    async def dispatch(self, action: str, params: dict) -> dict:
        """Public dispatch of one ``data_*`` action (D4.1).

        The tool proxy's ``POST /collections/rpc`` route (the script
        SDK's transport) calls this instead of reaching into
        ``_dispatch``. Raises :class:`DatastoreError` on business
        failures — the caller owns the wire mapping.
        """
        return await self._dispatch(action, params)

    async def _dispatch(self, action: str, params: dict) -> dict:
        collection = str(params.get("collection") or "").strip()
        if action == "data_rows_list":
            return await self.list_rows(
                collection,
                filters=params.get("filter"),
                search=params.get("search"),
                limit=params.get("limit", 50),
                offset=params.get("offset", 0),
            )
        if action == "data_row_get":
            return await self.get_row(
                collection,
                str(params.get("row_id") or ""),
            )
        if action == "data_row_upsert":
            row_id = params.get("row_id")
            return await self.upsert_row(
                collection,
                params.get("data"),
                row_id=str(row_id) if row_id is not None else None,
            )
        if action == "data_row_delete":
            return await self.delete_row(
                collection,
                str(params.get("row_id") or ""),
            )
        if action == "data_rows_count":
            return await self.count_rows(collection)
        if action == "data_import":
            return await self.import_csv(collection, params.get("csv"))
        raise DatastoreError(f"unknown datastore action {action!r}", 400)
