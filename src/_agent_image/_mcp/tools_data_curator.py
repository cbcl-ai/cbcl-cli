"""Data-Curator-role MCP tool list (Flow Studio FS-P3.T3).

The Data Curator is a consult-only agent spawned as a worker process
whose ``AGENT_NAME`` is ``data-curator``; ``mcp_tool_server`` selects
THIS catalog for that agent name (the Planner selection pattern — no
new ``--role`` threading). Its surface is deliberately MINIMAL: the
collection tools (its entire mandate) plus the KB reads for context.
No board tools, no run tools, no file/script tools — the Curator's
deliverables are schema/row changes persisted through the collection
tools, and its report rides the consult completion, not ``save_file``.

The collection tool DEFINITIONS live here (the Curator is the
collections steward) and are shared with the Flow Architect's catalog
(``tools_flow_architect`` imports ``COLLECTION_TOOLS``) — the
``tools_plan.py`` shared-definition pattern. Backend gate for every
write below: the pinned triple ``data-curator | flow-architect |
manager`` (``_handlers_collections.py``); ``get_collection`` and
``query_rows`` are ungated worker reads. Row tools proxy over the
RequestBridge ``data_*`` family to the office-local datastore — rows
live on the user's machine, never in Postgres.
"""
from __future__ import annotations

from .tools_worker import get_worker_tools

# KB reads pulled by name from the worker pool so the definitions stay
# single-sourced (the _MA_BOARD_OPERATOR_EXTRAS precedent).
_KB_READS = ("search_kb", "get_kb_document")


COLLECTION_TOOLS: list[dict] = [
    {
        "name": "list_collections",
        "description": (
            "List the office's collections (shared data tables): name, "
            "display name, schema revision, row count, and field names. "
            "Call this FIRST in any data consult — before creating a "
            "collection (avoid a near-duplicate under a new name) and "
            "before judging a directive's impact. Not a row read (use "
            "query_rows) and not a schema read (use get_collection for "
            "the full field definitions)."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "action": "list_collections",
    },
    {
        "name": "get_collection",
        "description": (
            "Read ONE collection's full schema: ordered field "
            "definitions (type, options, ref_to, required, help), "
            "schema_revision, and row count. Field types include `ref` "
            "(a link into another collection — the inbound references "
            "your delete-impact statements must name) and "
            "`params_schema` (the per-row parameter-panel pattern). Use "
            "before ANY schema change — update_collection_schema "
            "replaces the whole list, so you must start from the "
            "current one. Not for row data (use query_rows)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection": {
                    "type": "string",
                    "description": "Collection slug name or UUID.",
                },
            },
            "required": ["collection"],
        },
        "action": "get_collection",
    },
    {
        "name": "create_collection",
        "description": (
            "Create a NEW collection (shared office data table) with an "
            "ordered field schema. Check list_collections first — do "
            "NOT create a near-duplicate of an existing collection; "
            "extend it via update_collection_schema instead. Schema "
            "fields: {name, type: text|number|bool|enum|date|ref|"
            "params_schema, options?, ref_to? (the referenced "
            "collection's slug — must exist), required?, help?}. Use a "
            "`params_schema` field when each ROW needs its own "
            "parameter panel (per-item knobs), never dozens of "
            "mostly-null columns. Rows are added separately via "
            "upsert_row."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "display_name": {
                    "type": "string",
                    "description": "Human display name (REQUIRED).",
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Slug (lowercase, hyphens). Defaults to the "
                        "slugified display_name."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "What this collection holds and who reads it.",
                },
                "schema": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Ordered field definitions: [{name, type, "
                        "options?, ref_to?, required?, help?}]."
                    ),
                },
            },
            "required": ["display_name"],
        },
        "action": "create_collection",
    },
    {
        "name": "update_collection_schema",
        "description": (
            "Replace a collection's schema (and/or display_name/"
            "description). The `schema` param is the FULL ordered field "
            "list — it REPLACES the stored schema wholesale and bumps "
            "schema_revision, so read the current schema with "
            "get_collection first and re-send every field you keep; "
            "omitting a field REMOVES it. Migrations are ADDITIVE "
            "first: add the new field, backfill rows, repoint "
            "consumers — remove old fields only on an explicit "
            "directive. Not for row edits (use upsert_row)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection": {
                    "type": "string",
                    "description": "Collection slug name or UUID.",
                },
                "schema": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "FULL replacement field list: [{name, type, "
                        "options?, ref_to?, required?, help?}]. Omit to "
                        "keep the stored schema (metadata-only update)."
                    ),
                },
                "display_name": {"type": "string", "description": "New display name."},
                "description": {"type": "string", "description": "New description."},
            },
            "required": ["collection"],
        },
        "action": "update_collection_schema",
    },
    {
        "name": "query_rows",
        "description": (
            "Read rows from a collection (the office-local datastore on "
            "the user's machine — a live proxy read, 'Communicator not "
            "connected' when the office daemon is offline). Supports "
            "free-text `search`, exact-match AND `filter`, and "
            "limit/offset paging. Sample real rows BEFORE judging data "
            "quality or shape — never reason about rows you haven't "
            "read. Not a schema read (use get_collection) and not a KB "
            "search (use search_kb)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection": {
                    "type": "string",
                    "description": "Collection slug name or UUID.",
                },
                "search": {
                    "type": "string",
                    "description": "Free-text search across row values.",
                },
                "filter": {
                    "type": "object",
                    "description": (
                        "Exact-match field filter, AND-combined: "
                        "{field: value}."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows (1-200, default 50).",
                },
                "offset": {"type": "integer", "description": "Paging offset."},
            },
            "required": ["collection"],
        },
        "action": "query_rows",
    },
    {
        "name": "upsert_row",
        "description": (
            "Insert or update ONE row in a collection (the office-local "
            "datastore). Pass `row_id` to update an existing row; omit "
            "it to insert. `data` is validated against the collection's "
            "schema — a validation error names the offending field; fix "
            "and retry once. Write rows one at a time with honest "
            "bookkeeping — never bulk-rewrite without an explicit "
            "directive naming the transformation. Not for schema "
            "changes (use update_collection_schema)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection": {
                    "type": "string",
                    "description": "Collection slug name or UUID.",
                },
                "data": {
                    "type": "object",
                    "description": "Row data keyed by schema field name (REQUIRED).",
                },
                "row_id": {
                    "type": "string",
                    "description": "Existing row id to update; omit to insert.",
                },
            },
            "required": ["collection", "data"],
        },
        "action": "upsert_row",
    },
    {
        "name": "delete_row",
        "description": (
            "Permanently delete ONE row from a collection. LAST resort: "
            "the default recommendation is archive-don't-delete (a "
            "status/archived field keeps history and inbound `ref` "
            "links intact). A row referenced by `ref` fields elsewhere "
            "should be flagged in your report — with the referencing "
            "collection(s) named — not silently dropped. Delete only on "
            "an explicit directive that names what may be destroyed. "
            "Not for retiring a whole collection (that is a "
            "platform-side decision; deletion of a collection "
            "referenced by an enabled flow is refused)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection": {
                    "type": "string",
                    "description": "Collection slug name or UUID.",
                },
                "row_id": {
                    "type": "string",
                    "description": "Row id to delete (REQUIRED).",
                },
            },
            "required": ["collection", "row_id"],
        },
        "action": "delete_row",
    },
]


def get_data_curator_tools() -> list[dict]:
    """The Data Curator catalog: collection tools + KB reads.

    Deliberately minimal — every tool maps to the Curator's mandate
    (schemas, references, data quality, safe migrations) or the
    context reads that inform it. The impact statements its playbook
    mandates ride the backend's teaching errors (delete protection
    names the depending flows) + the schema's ``ref`` fields — the
    Curator holds NO flow-design tools (``get_flow_graph`` is gated
    ``flow-architect | manager`` backend-side).
    """
    kb = [t for t in get_worker_tools() if t["name"] in _KB_READS]
    return [*COLLECTION_TOOLS, *kb]
