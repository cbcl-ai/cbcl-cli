"""Flow-Architect-role MCP tool list (Flow Studio FS-P3.T3).

The Flow Architect is a consult-only agent spawned as a worker process
whose ``AGENT_NAME`` is ``flow-architect``; ``mcp_tool_server`` selects
THIS catalog for that agent name (the Planner selection pattern — no
new ``--role`` threading). Its surface is deliberately MINIMAL and
justified per tool:

* the three flow-authoring tools (its mandate: graphs + templates);
* the collection tools MINUS ``delete_row`` (extraction CREATES
  collections and populates their rows — spec §8.4; destructive row
  curation belongs to the Data Curator's consult, so the Architect
  doesn't carry the delete verb);
* the KB reads (context research).

No board tools, no run tools, no ``save_file`` — templates are written
via ``write_template`` (the daemon materialises the files) and any
scratch reading uses the CLI built-ins. Backend gates: the flow tools
are ``flow-architect | manager``; the collection writes are the pinned
triple ``data-curator | flow-architect | manager``
(``_handlers_flow_design.py`` / ``_handlers_collections.py``).
"""
from __future__ import annotations

from .tools_data_curator import COLLECTION_TOOLS
from .tools_worker import get_worker_tools

# KB reads pulled by name from the worker pool (single-sourced defs —
# the _MA_BOARD_OPERATOR_EXTRAS precedent).
_KB_READS = ("search_kb", "get_kb_document")

# Destructive row curation is the Data Curator's surface — the
# Architect creates/extends collections and writes extraction rows,
# never deletes rows (archive-don't-delete is the office default and
# the Curator owns the exceptions).
_ARCHITECT_EXCLUDED_COLLECTION_TOOLS = frozenset({"delete_row"})


FLOW_ARCHITECT_TOOLS: list[dict] = [
    {
        "name": "get_flow_graph",
        "description": (
            "Read ONE flow's machine-executable definition: the typed "
            "block graph, manifest_schema, trigger_config, revision, "
            "and is_active. Call this BEFORE any design edit — "
            "update_flow_graph writes the WHOLE graph, so you must "
            "start from the current one — and at the start of an "
            "extraction re-run so you converge instead of duplicating. "
            "`graph` is null for a legacy prose-only flow (v1 "
            "definitions keep working untouched). Accepts the flow "
            "UUID or the slug name."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "flow_id": {"type": "string", "description": "Flow UUID."},
                "flow_name": {
                    "type": "string",
                    "description": "Flow slug name (alternative to flow_id).",
                },
            },
        },
        "action": "get_flow_graph",
    },
    {
        "name": "update_flow_graph",
        "description": (
            "Write a flow's block graph (and optionally its "
            "manifest_schema): validates the 13-type block contract "
            "(DAG rules, switch defaults, the ≤60-block cap), bumps "
            "the revision, and snapshots the prior one — graph edits "
            "land as NEW revisions, never destructive rewrites, and "
            "in-flight runs keep their pinned revision. The `graph` "
            "param REPLACES the stored graph wholesale: start from "
            "get_flow_graph and change only what the directive asks, "
            "then name what changed in `note` and in your report. A "
            "validation failure returns a teaching error naming the "
            "exact block/edge to fix — fix and retry once. This tool "
            "never enables the flow: drafts stay invisible to the "
            "Manager's matcher until the USER enables them. Collection "
            "references (`config.collection`, `ref_to`) must name "
            "existing collections EXACTLY (create them first)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "flow_id": {"type": "string", "description": "Flow UUID."},
                "flow_name": {
                    "type": "string",
                    "description": "Flow slug name (alternative to flow_id).",
                },
                "graph": {
                    "type": "object",
                    "description": (
                        "REQUIRED. The FULL graph: {blocks: [{id, type, "
                        "name, goal, config, on_fail}], edges: [{from, "
                        "to, when}]}."
                    ),
                },
                "manifest_schema": {
                    "type": "object",
                    "description": (
                        "Run-record field groups: {groups: [{name, "
                        "fields: [{name, type, ref_to?, required, "
                        "derivable, help}]}]}. Absent = keep the stored "
                        "one; {\"groups\": []} = clear."
                    ),
                },
                "note": {
                    "type": "string",
                    "description": (
                        "One-line revision note naming what changed "
                        "(≤500 chars) — shown in the revision history."
                    ),
                },
            },
            "required": ["graph"],
        },
        "action": "update_flow_graph",
    },
    {
        "name": "write_template",
        "description": (
            "Write ONE document template for a flow into the office "
            "workspace: `doc_yaml` becomes "
            "templates/<flow>/<doc>/doc.yaml (the ordered section list "
            "— each entry {file, include_when?, ai?}; include_when "
            "holds the manifest expression gating a conditional "
            "section, but V1 NEVER RESOLVES IT: a section carrying "
            "include_when is ALWAYS skipped at generate time, so "
            "author sections unconditionally in v1) and each "
            "`sections` entry becomes "
            "templates/<flow>/<doc>/sections/<file> (a markdown "
            "fragment with {{manifest.*}} bindings). The flow row need "
            "not exist yet (extraction may write templates first). "
            "Re-writing the same doc overwrites its files — the "
            "template IS the latest version. Not for run outputs or "
            "reports (templates are recipes the `generate` block "
            "assembles at run time); a daemon-offline error names any "
            "files already written — re-issue the call when the "
            "daemon is back."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "flow_name": {
                    "type": "string",
                    "description": "Flow slug the template belongs to.",
                },
                "doc_name": {
                    "type": "string",
                    "description": "Document slug (e.g. 'commercial-offer').",
                },
                "doc_yaml": {
                    "type": "string",
                    "description": (
                        "The doc.yaml content: ordered sections "
                        "[{file: 'sections/x.md', include_when?, "
                        "ai?: {prompt, max_words}}]. include_when is "
                        "NOT resolved in v1 — such sections are "
                        "always skipped at generate time."
                    ),
                },
                "sections": {
                    "type": "object",
                    "description": (
                        "Section fragments: {filename.md: markdown "
                        "content with {{bindings}}} (≤40 files)."
                    ),
                },
            },
            "required": ["flow_name", "doc_name", "doc_yaml"],
        },
        "action": "write_template",
    },
]


def get_flow_architect_tools() -> list[dict]:
    """The Flow Architect catalog: flow authoring + collections (no
    delete_row) + KB reads.

    Collection definitions are shared with the Data Curator
    (``tools_data_curator.COLLECTION_TOOLS`` — the ``tools_plan.py``
    single-source pattern); extraction needs them because the
    Architect CREATES the collections a flow reads and populates the
    extracted rows (spec §8.4).
    """
    collections = [
        t
        for t in COLLECTION_TOOLS
        if t["name"] not in _ARCHITECT_EXCLUDED_COLLECTION_TOOLS
    ]
    kb = [t for t in get_worker_tools() if t["name"] in _KB_READS]
    return [*FLOW_ARCHITECT_TOOLS, *collections, *kb]
