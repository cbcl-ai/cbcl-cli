"""Shared decision 3: every tool result reaches the model complete, or with an
explicit, actionable marker (C1-G2, C4c-G1, C4c-G3).

The pinned Claude CLI (2.1.259) replaces an MCP result with a file path and a
2 KB preview once it is over the tool's persistence threshold (50,000
characters unless the tool declares ``_meta["anthropic/maxResultSizeChars"]``)
and token-counts unannotated results against MAX_MCP_OUTPUT_TOKENS. The server
renders compact JSON with real characters, declares each tool's limit in
``tools/list`` and never returns more than that many UTF-16 units, so under the
CLI's default remote configuration it neither cuts nor replaces a single
result. Every limit also stays under the CLI's remote-gated per-message budget
(R12, below).
"""
from __future__ import annotations

import asyncio
import json
import random
import sys
from unittest.mock import AsyncMock

import pytest

from src._agent_image import mcp_tool_server as server_module
from src._agent_image._mcp import result_text
from src._agent_image._mcp.result_text import (
    CLI_RESULT_SIZE_CEILING,
    CLI_RESULT_SIZE_META_KEY,
    DEFAULT_RESULT_LIMIT,
    js_length,
    render_result,
    render_section,
    result_limit,
)
from src._agent_image._mcp.tools_data_curator import get_data_curator_tools
from src._agent_image._mcp.tools_flow_architect import get_flow_architect_tools
from src._agent_image._mcp.tools_manager import get_manager_tools
from src._agent_image._mcp.tools_planner import get_planner_tools
from src._agent_image._mcp.tools_worker import get_worker_subcatalog, get_worker_tools
from tests.backend_boundary import import_backend

# The server imports ``_mcp`` through its own sys.path entry (as it does in
# the image), so its result_text module is a separate object from the one
# imported above: limits are patched on the module the server really uses.
SERVER_RESULT_TEXT = sys.modules[server_module._render_result.__module__]

_UKRAINIAN = (
    "Агент відповідає за підготовку комерційних пропозицій для клієнтів "
    "компанії та перевіряє відповідність вимогам"
).split()


def _ukrainian(length: int, seed: int = 1) -> str:
    rng = random.Random(seed)
    words: list[str] = []
    size = 0
    while size < length:
        word = rng.choice(_UKRAINIAN)
        if rng.random() < 0.08:
            word += "\n"
        words.append(word)
        size += len(word) + 1
    return " ".join(words)[:length]


def _run(server, name: str, arguments: dict) -> dict:
    return asyncio.run(server._execute_tool(name, arguments))


def _text(outcome: dict) -> str:
    assert not outcome.get("isError"), outcome
    return outcome["content"][0]["text"]


def _server(monkeypatch, tools, backend_result, *, task_mode="manager"):
    monkeypatch.setattr(server_module, "TASK_MODE", task_mode)
    monkeypatch.setattr(server_module, "CONTEXT_KEY", "workstream:ws-1")
    monkeypatch.setenv("CONTEXT_KEY", "workstream:ws-1")
    backend = AsyncMock(return_value=backend_result)
    monkeypatch.setattr(server_module, "_call_backend", backend)
    return server_module.MCPServer(tools), backend


# ── rendering ───────────────────────────────────────────────────────


def test_results_are_compact_json_with_real_characters():
    rendered = render_result({"name": "Звіт", "items": [1, 2]}, "get_board")
    assert rendered.text == '{"name":"Звіт","items":[1,2]}'
    assert rendered.truncated is False


def test_every_tool_declares_its_result_limit_to_the_cli(monkeypatch):
    server, _ = _server(monkeypatch, get_manager_tools(), {})
    listed = asyncio.run(
        server._handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    )["result"]["tools"]
    assert listed
    by_name = {tool["name"]: tool for tool in listed}
    for tool in get_manager_tools():
        declared = by_name[tool["name"]]["_meta"][CLI_RESULT_SIZE_META_KEY]
        assert declared == result_limit(tool["action"])
        assert 0 < declared <= CLI_RESULT_SIZE_CEILING
    assert by_name["inspect_configuration"]["_meta"][CLI_RESULT_SIZE_META_KEY] == 190_000
    assert by_name["get_task_detail"]["_meta"][CLI_RESULT_SIZE_META_KEY] == 190_000
    assert by_name["get_board"]["_meta"][CLI_RESULT_SIZE_META_KEY] == DEFAULT_RESULT_LIMIT


# ── C1-G2: large non-English instructions ───────────────────────────


def test_inspect_configuration_returns_capped_cyrillic_fields_verbatim(monkeypatch):
    fields = {
        "role_description": _ukrainian(5_000, seed=1),
        "system_prompt": _ukrainian(50_000, seed=2),
        "claude_md_content": _ukrainian(50_000, seed=3),
    }
    result = {"target": "agent", "target_id": "0" * 36, "name": "writer",
              "fields": fields, "recent_proposals": []}
    server, _ = _server(monkeypatch, get_manager_tools(), result)
    text = _text(_run(server, "inspect_configuration",
                      {"target": "agent", "target_id": "0" * 36}))
    assert js_length(text) <= result_limit("inspect_configuration")
    assert json.loads(text)["fields"] == fields


def test_default_limit_counts_real_characters_not_escapes(monkeypatch):
    # 45k Cyrillic characters used to escape to ~270k and be cut at 50k.
    notes = _ukrainian(45_000, seed=4)
    server, _ = _server(monkeypatch, get_manager_tools(), {"notes": notes})
    text = _text(_run(server, "get_board", {}))
    assert json.loads(text) == {"notes": notes}


# ── C4c-G3: legally sized briefs, specs and plans ───────────────────


def _task_detail(*, inputs: str, context: str, comments: list[str]) -> dict:
    return {
        "id": "task-1",
        "readable_id": "WR-001.T01",
        "title": "Write the proposal",
        "brief": {
            "goal": "Deliver the proposal.",
            "context": context,
            "inputs": inputs,
            "output_format": "Markdown",
            "acceptance_criteria": ["Names every service", "Pricing traces to the rate card"],
            "verification_steps": "Execution checks: build. Independent review: read.",
        },
        "recent_activities": [
            {"event_type": "comment", "actor": "user", "content": body,
             "details": {}, "created_at": f"2026-09-2{index}"}
            for index, body in enumerate(comments)
        ],
    }


def test_legal_brief_is_delivered_complete(monkeypatch):
    detail = _task_detail(inputs="x" * 49_165, context="y" * 5_100,
                          comments=["z" * 12_000] * 3)
    server, _ = _server(monkeypatch, get_worker_subcatalog("execute", "builder"),
                        detail, task_mode="execute")
    text = _text(_run(server, "get_my_brief", {}))
    delivered = json.loads(text)
    assert next(iter(delivered)) == "_delivery"  # large result: preview note first
    delivered.pop("_delivery")
    assert delivered == detail


def test_oversized_brief_keeps_criteria_and_newest_comment_and_says_what_was_cut(monkeypatch):
    newest = "NEWEST " + "n" * 40_000
    detail = _task_detail(inputs="i" * 150_000, context="c" * 90_000,
                          comments=["old " + "o" * 45_000, newest])
    server, _ = _server(monkeypatch, get_manager_tools(), detail)
    text = _text(_run(server, "get_task_detail", {"task_id": "task-1"}))
    assert js_length(text) <= result_limit("get_task_detail")
    parsed = json.loads(text)
    brief = parsed["brief"]
    assert brief["acceptance_criteria"] == detail["brief"]["acceptance_criteria"]
    assert brief["verification_steps"] == detail["brief"]["verification_steps"]
    assert parsed["recent_activities"][-1]["content"] == newest
    notice = parsed["_truncated"]
    assert notice["complete"] is False
    shortened = {entry["path"]: entry for entry in notice["fields"]}
    assert "brief.inputs" in shortened
    assert shortened["brief.inputs"]["chars"] == 150_000
    assert "brief.inputs omitted here" in brief["inputs"]
    assert "`section`" in notice["guidance"]
    assert brief["inputs"].startswith("i" * 100)
    # Where a section read continues the omitted middle.
    kept_head = brief["inputs"].index("\n…[")
    assert shortened["brief.inputs"]["next_offset"] == kept_head
    assert len(shortened["brief.inputs"]["fingerprint"]) == 12


def test_long_list_keeps_first_and_last_items_valid_json():
    result = {"items": [{"n": index, "title": "t" * 200} for index in range(2_000)]}
    rendered = render_result(result, "get_board")
    assert js_length(rendered.text) <= DEFAULT_RESULT_LIMIT
    parsed = json.loads(rendered.text)
    assert parsed["items"][0] == result["items"][0]
    assert parsed["items"][-1] == result["items"][-1]
    assert any(isinstance(item, str) and "items of items omitted" in item
               for item in parsed["items"])
    assert parsed["_truncated"]["lists"][0]["items"] == 2_000


def test_last_resort_cut_is_character_safe_and_marked():
    # A top-level list cannot carry a _truncated object: it is cut as text.
    emoji = ["😀" * 50] * 2_000
    rendered = render_result(emoji, "get_board")
    assert rendered.truncated is True
    assert js_length(rendered.text) <= DEFAULT_RESULT_LIMIT
    assert "[TRUNCATED:" in rendered.text
    assert "NOT valid JSON" in rendered.text
    # No lone surrogate: the text encodes as strict UTF-8.
    rendered.text.encode("utf-8")


def test_cut_never_splits_a_surrogate_pair_or_an_escape():
    assert result_text._cut_units("a😀b", 2) == "a"
    assert result_text._cut_units("a😀b", 3) == "a😀"
    assert result_text._drop_partial_escape('"line\\') == '"line'
    assert result_text._drop_partial_escape('"x\\u00') == '"x'
    assert result_text._drop_partial_escape('"x\\\\') == '"x\\\\'


# ── C4c-G1: flow graph reads and the wholesale graph write ──────────


def _near_cap_graph() -> dict:
    graph_schemas = import_backend("app.flows.graph_schemas")

    def build(padding: int) -> dict:
        blocks = [{
            "id": f"step-{index:02d}", "type": "work",
            "name": f"Step {index} deliverable",
            "goal": "Produce this section of the proposal.",
            "config": {"tasks": [{
                "title": f"Draft section {index}", "agent": "builder",
                "reviewer": "auditor", "outputs": [f"section_{index}"],
                "brief_template": {
                    "goal": f"Draft section {index}. " + "g" * padding,
                    "inputs": "Use the intake answers in the manifest.",
                    "acceptance_criteria": [f"Section {index} names every service"],
                    "verification_steps": "Execution checks: re-read.",
                },
            }]},
        } for index in range(1, graph_schemas.GRAPH_MAX_BLOCKS + 1)]
        edges = [{"from": f"step-{index:02d}", "to": f"step-{index + 1:02d}",
                  "when": "always"}
                 for index in range(1, graph_schemas.GRAPH_MAX_BLOCKS)]
        return graph_schemas.Graph.model_validate(
            {"blocks": blocks, "edges": edges}
        ).as_storage_dict()

    cap = graph_schemas.GRAPH_MAX_SERIALIZED_BYTES
    graph = build(padding := 800)
    while True:  # grow until the validator's own size cap refuses the next step
        try:
            graph = build(padding + 20)
        except ValueError:
            break
        padding += 20
    assert len(json.dumps(graph, separators=(",", ":")).encode()) > cap - 2_000
    return graph


def _architect_server(monkeypatch, backend_result):
    tools = server_module.select_session_tools("worker", "flow-architect", "execute")
    return _server(monkeypatch, tools, backend_result, task_mode="execute")


def test_graph_read_at_the_cap_arrives_complete(monkeypatch):
    graph = _near_cap_graph()
    envelope = {"flow_id": "f" * 8, "name": "proposal", "display_name": "Proposal",
                "revision": 4, "is_active": False, "graph": graph,
                "manifest_schema": {"groups": []}, "trigger_config": None}
    server, _ = _architect_server(monkeypatch, envelope)
    text = _text(_run(server, "get_flow_graph", {"flow_name": "proposal"}))
    parsed = json.loads(text)
    assert parsed["graph"] == graph
    assert len(parsed["graph"]["blocks"]) == len(graph["blocks"])
    assert len(parsed["graph"]["edges"]) == len(graph["edges"])
    assert "_truncated" not in parsed
    # Its read receipt ends it, still within the limit.
    assert list(parsed)[-1] == "read_receipt"
    assert js_length(text) <= result_limit("get_flow_graph")


def test_update_is_refused_after_a_shortened_graph_read(monkeypatch):
    graph = {"blocks": [{"id": f"b{index}", "goal": "g" * 2_000} for index in range(40)],
             "edges": []}
    envelope = {"flow_id": "FLOW-UUID", "name": "proposal", "graph": graph}
    server, backend = _architect_server(monkeypatch, envelope)
    monkeypatch.setitem(SERVER_RESULT_TEXT.RESULT_LIMITS, "get_flow_graph", 20_000)
    read = _text(_run(server, "get_flow_graph", {"flow_name": "proposal"}))
    assert json.loads(read)["_truncated"]["complete"] is False

    backend.reset_mock()
    for target in ({"flow_name": "Proposal"}, {"flow_id": "flow-uuid"}):
        refused = _run(server, "update_flow_graph", {**target, "graph": graph})
        assert refused.get("isError") is True
        assert "update_flow_graph refused" in refused["content"][0]["text"]
    backend.assert_not_called()

    # A complete re-read clears the refusal; the write passes its receipt.
    monkeypatch.setitem(
        SERVER_RESULT_TEXT.RESULT_LIMITS, "get_flow_graph", result_text.LARGE_RESULT_LIMIT
    )
    reread = json.loads(
        _text(_run(server, "get_flow_graph", {"flow_name": "proposal"}))
    )
    backend.return_value = {"flow_id": "FLOW-UUID", "revision": 5}
    written = _run(server, "update_flow_graph", {
        "flow_name": "proposal", "graph": graph,
        "read_receipt": reread["read_receipt"],
    })
    assert not written.get("isError")
    assert backend.call_args.args[0] == "update_flow_graph"


def test_update_of_a_different_flow_is_not_refused(monkeypatch):
    envelope = {"flow_id": "A", "name": "alpha",
                "graph": {"blocks": [{"goal": "g" * 3_000}] * 20, "edges": []}}
    server, backend = _architect_server(monkeypatch, envelope)
    monkeypatch.setitem(SERVER_RESULT_TEXT.RESULT_LIMITS, "get_flow_graph", 20_000)
    _text(_run(server, "get_flow_graph", {"flow_name": "alpha"}))
    backend.return_value = {"flow_id": "B", "revision": 2}
    written = _run(server, "update_flow_graph", {"flow_name": "beta", "graph": {}})
    assert not written.get("isError")


@pytest.mark.parametrize("action", sorted(result_text.RESULT_LIMITS))
def test_large_read_limits_stay_within_the_cli_ceiling(action):
    assert DEFAULT_RESULT_LIMIT < result_limit(action) <= CLI_RESULT_SIZE_CEILING


# ── R12: the CLI's per-assistant-message tool-result budget ──────────
#
# Claude CLI 2.1.259 also totals the tool results of ONE assistant message
# (``Pnr = 200000``) and swaps the largest for a ``<persisted-output>`` file
# preview when the total is over it. Only a tool with a non-finite
# ``maxResultSizeChars`` or ``skipAggregateToolResultBudget`` is exempt, and
# every Cubicle tool declares a finite limit. The budget is gated by the
# remote flag ``tengu_hawthorn_steeple`` (default off). Keep every per-tool
# limit under it so a single read is never swapped; re-check on a CLI bump.
_CLI_PER_MESSAGE_RESULT_BUDGET = 200_000


def test_every_result_limit_stays_under_the_cli_per_message_budget():
    assert result_text.CLI_PER_MESSAGE_RESULT_BUDGET == _CLI_PER_MESSAGE_RESULT_BUDGET
    assert max(result_limit(action) for action in result_text.RESULT_LIMITS) < (
        _CLI_PER_MESSAGE_RESULT_BUDGET
    )
    assert result_limit("get_board") < _CLI_PER_MESSAGE_RESULT_BUDGET


def _catalogs() -> dict[str, list[dict]]:
    catalogs = {
        "manager": get_manager_tools(),
        "worker_pool": get_worker_tools(),
        "planner": get_planner_tools(),
        "flow_architect": get_flow_architect_tools(),
        "data_curator": get_data_curator_tools(),
    }
    for mode in ("execute", "review", "triage"):
        for agent in ("builder", "manager-assistant"):
            catalogs[f"worker_{mode}_{agent}"] = get_worker_subcatalog(mode, agent)
    return catalogs


_LARGE_READ_TOOLS = sorted(
    (catalog, tool["name"])
    for catalog, tools in _catalogs().items()
    for tool in tools
    if result_limit(tool.get("action", "")) > DEFAULT_RESULT_LIMIT
)


def test_every_large_read_in_every_catalog_is_covered():
    # The six large-read tools (get_my_brief dispatches as get_task_detail)
    # appear across the Manager, worker, Planner and Flow Architect catalogs.
    names = {name for _, name in _LARGE_READ_TOOLS}
    assert names == {
        "get_task_detail", "get_my_brief", "get_spec", "get_execution_plan",
        "inspect_configuration", "get_flow_graph",
    }
    assert ("planner", "get_spec") in _LARGE_READ_TOOLS
    assert ("worker_pool", "get_my_brief") in _LARGE_READ_TOOLS


# A real part of each tool's result, named as its ``section`` example.
_SECTION_EXAMPLES = {
    "get_task_detail": "brief.inputs",
    "get_my_brief": "brief.inputs",
    "get_spec": "spec.content",
    "get_execution_plan": "execution_plan.task_breakdown",
    "get_flow_graph": "graph.blocks",
    "inspect_configuration": "fields.claude_md_content",
}


@pytest.mark.parametrize(("catalog", "name"), _LARGE_READ_TOOLS)
def test_large_read_descriptions_warn_about_a_persisted_output_preview(catalog, name):
    tool = next(t for t in _catalogs()[catalog] if t["name"] == name)
    assert "<persisted-output>" in tool["description"]
    assert "not the result" in tool["description"]
    assert result_text.LARGE_READ_GUIDANCE in tool["description"]
    properties = tool["inputSchema"]["properties"]
    assert properties["section"]["type"] == "string"
    assert f"`{_SECTION_EXAMPLES[name]}`" in properties["section"]["description"]
    offset = properties["offset"]
    assert (offset["type"], offset["minimum"]) == ("integer", 0)


# ── L03: a large result tells a preview it is not the result ─────────


def test_large_results_lead_with_a_preview_delivery_note(monkeypatch):
    spec = {"spec": {"id": "s1", "content": "req " * 10_000, "milestones": []}}
    server, _ = _server(monkeypatch, get_manager_tools(), spec)
    text = _text(_run(server, "get_spec", {"workstream_id": "ws-1"}))
    # Inside the CLI's 2 KB <persisted-output> preview, before any content.
    assert text.startswith(
        '{"_delivery":"Only if this text is inside a <persisted-output> preview'
    )
    assert "Call this tool again alone" in text[:400]
    delivered = json.loads(text)
    assert delivered["spec"] == spec["spec"]


def test_every_large_read_the_preview_could_cut_leads_with_the_note():
    # A per-tool threshold override (tengu_velvet_ibis) can swap a result of
    # any size, so the note does not wait for a large share of the budget.
    mid = render_result({"spec": {"content": "req " * 1_250}}, "get_spec")
    assert js_length(mid.text) < 20_000
    assert next(iter(json.loads(mid.text))) == "_delivery"
    # The CLI's 2,000-character preview holds this one whole.
    whole = render_result({"spec": {"content": "r" * 1_500}}, "get_spec")
    assert "_delivery" not in json.loads(whole.text)


def test_delivery_note_keeps_a_near_limit_result_within_its_limit():
    content = "Вимога " * 40_000  # Cyrillic: counted in UTF-16 units
    result = {"spec": {"content": content}}
    rendered = render_result(result, "get_spec")
    assert js_length(rendered.text) <= result_limit("get_spec")
    parsed = json.loads(rendered.text)
    assert list(parsed)[:2] == ["_delivery", "_truncated"]


def test_small_and_ordinary_results_carry_no_delivery_note():
    small = render_result({"spec": {"content": "short"}}, "get_spec")
    assert "_delivery" not in json.loads(small.text)
    board = render_result({"items": ["t" * 40_000]}, "get_board")
    assert "_delivery" not in json.loads(board.text)


def test_notices_and_cut_markers_name_the_declared_limit():
    # The note (and a read receipt) take room from the text, not from the
    # limit the model is told about.
    content = "Вимога " * 40_000
    notice = json.loads(render_result({"spec": {"content": content}}, "get_spec").text)
    assert notice["_truncated"]["limit_chars"] == 190_000
    assert "190,000-character" in notice["_truncated"]["guidance"]
    # Protected fields cannot be shortened: the last-resort cut applies.
    cut = render_result(
        {"spec": {"acceptance_criteria": ["x" * 250_000]}}, "get_spec"
    ).text
    assert "exceeded the 190,000-character tool-result limit" in cut
    assert js_length(cut) <= result_limit("get_spec")


# ── R12/L03: a wholesale write-back needs its read's receipt ─────────


def _big_graph_envelope() -> dict:
    return {"flow_id": "FLOW-UUID", "name": "proposal",
            "graph": {"blocks": [{"id": "b1", "goal": "g" * 30_000}], "edges": []}}


# (catalog, read tool, read arguments, read result, write tool, write
# arguments without the receipt)
_GUARDED_WRITES = [
    pytest.param(
        lambda: server_module.select_session_tools(
            "worker", "flow-architect", "execute"
        ),
        "get_flow_graph", {"flow_name": "proposal"}, _big_graph_envelope(),
        "update_flow_graph", {"flow_id": "flow-uuid", "graph": {"blocks": []}},
        id="flow-graph",
    ),
    pytest.param(
        get_planner_tools,
        "get_spec", {"spec_id": "SPEC-1"},
        {"spec": {"id": "SPEC-1", "workstream_id": "WS-1", "name": "Spec",
                  "content": "req " * 8_000, "milestones": []}},
        "update_spec", {"workstream_id": "ws-1", "name": "Spec", "content": "x"},
        id="spec",
    ),
    pytest.param(
        get_manager_tools,
        "get_execution_plan", {"scope_id": "SCOPE-1"},
        {"scope_id": "SCOPE-1", "execution_plan": {"summary": "s " * 15_000}},
        "update_execution_plan", {"scope_id": "scope-1", "plan": {"summary": "s"}},
        id="execution-plan",
    ),
]


@pytest.mark.parametrize(
    ("catalog", "read", "read_args", "result", "write", "write_args"), _GUARDED_WRITES
)
def test_a_write_back_needs_the_receipt_of_its_latest_complete_read(
    monkeypatch, catalog, read, read_args, result, write, write_args
):
    server, backend = _server(monkeypatch, catalog(), result, task_mode="execute")
    text = _text(_run(server, read, read_args))
    first = json.loads(text)
    receipt = first["read_receipt"]
    # The receipt is the LAST key: no CLI preview (2,000 characters) holds it.
    assert list(first)[-1] == "read_receipt"
    assert receipt not in text[:2_000]

    backend.reset_mock()
    for supplied in ({}, {"read_receipt": "not-the-receipt"}):
        refused = _run(server, write, {**write_args, **supplied})
        assert refused.get("isError") is True
        assert "pass `read_receipt`" in refused["content"][0]["text"]
    backend.assert_not_called()

    # A later read of the same target may have reached the model only as a
    # preview: the earlier receipt no longer admits a write-back.
    second = json.loads(_text(_run(server, read, read_args)))
    assert second["read_receipt"] != receipt
    backend.reset_mock()
    stale = _run(server, write, {**write_args, "read_receipt": receipt})
    assert stale.get("isError") is True
    backend.assert_not_called()

    backend.return_value = {"revision": 2}
    written = _run(
        server, write, {**write_args, "read_receipt": second["read_receipt"]}
    )
    assert not written.get("isError"), written
    assert backend.call_args.args[0] == write
    assert "read_receipt" not in backend.call_args.args[1]


def test_a_target_never_read_in_the_session_is_written_without_a_receipt(monkeypatch):
    server, backend = _server(monkeypatch, get_planner_tools(), {"revision": 1})
    written = _run(server, "update_spec",
                   {"workstream_id": "ws-2", "name": "New", "content": "draft"})
    assert not written.get("isError"), written
    assert backend.call_args.args[0] == "update_spec"


def test_a_shortened_spec_read_refuses_its_write_back(monkeypatch):
    spec = {"spec": {"id": "s1", "workstream_id": "ws-1", "name": "Spec",
                     "content": _ukrainian(250_000, seed=3), "milestones": []}}
    server, backend = _server(monkeypatch, get_planner_tools(), spec)
    read = json.loads(_text(_run(server, "get_spec", {"workstream_id": "ws-1"})))
    assert "read_receipt" not in read
    assert "update_spec for this spec is refused" in read["_truncated"]["guidance"]
    backend.reset_mock()
    refused = _run(server, "update_spec",
                   {"workstream_id": "ws-1", "name": "Spec", "content": "x"})
    assert refused.get("isError") is True
    assert "was shortened" in refused["content"][0]["text"]
    backend.assert_not_called()


# ── L01: a shortened large read can be continued with section reads ──


def _read_all_pages(server, name: str, arguments: dict, section: str) -> str:
    pieces, offset, fingerprints = [], 0, set()
    while offset is not None:
        page = json.loads(_text(_run(
            server, name, {**arguments, "section": section, "offset": offset}
        )))
        assert page["section"] == section and page["offset"] == offset
        pieces.append(page["text"])
        fingerprints.add(page["fingerprint"])
        offset = page["next_offset"]
    assert len(fingerprints) == 1
    return "".join(pieces)


def test_a_shortened_spec_reads_back_complete_through_section_pages(monkeypatch):
    content = _ukrainian(250_000, seed=7)
    spec = {"spec": {"id": "s1", "name": "Spec", "content": content,
                     "milestones": [{"key": "m1"}], "revision": 3, "status": "draft"}}
    server, backend = _server(monkeypatch, get_manager_tools(), spec)
    first = json.loads(_text(_run(server, "get_spec", {"workstream_id": "ws-1"})))
    entry = first["_truncated"]["fields"][0]
    assert entry["path"] == "spec.content"
    assert content.startswith(first["spec"]["content"][: entry["next_offset"]])

    assert _read_all_pages(server, "get_spec", {"workstream_id": "ws-1"},
                           "spec.content") == content
    # The omitted middle is read from where the notice says it starts.
    rest = json.loads(_text(_run(server, "get_spec", {
        "workstream_id": "ws-1", "section": "spec.content",
        "offset": entry["next_offset"],
    })))
    assert rest["fingerprint"] == entry["fingerprint"]
    assert content[entry["next_offset"]:].startswith(rest["text"])
    # The backend never sees the section arguments.
    for call in backend.call_args_list:
        assert "section" not in call.args[1] and "offset" not in call.args[1]


def test_section_reads_leave_other_tools_offset_alone(monkeypatch):
    server, backend = _server(monkeypatch, get_manager_tools(), {"items": []})
    _text(_run(server, "get_board", {"offset": 100}))
    assert backend.call_args.args[1]["offset"] == 100


def _graph_envelope(blocks: int) -> dict:
    return {
        "flow_id": "FLOW-UUID", "name": "proposal",
        "graph": {"blocks": [{"id": f"b{index:03d}", "goal": "g" * 300}
                             for index in range(blocks)],
                  "edges": []},
    }


def test_section_read_pages_a_shortened_list_by_original_index(monkeypatch):
    envelope = _graph_envelope(200)
    server, _ = _architect_server(monkeypatch, envelope)
    monkeypatch.setitem(SERVER_RESULT_TEXT.RESULT_LIMITS, "get_flow_graph", 20_000)
    first = json.loads(_text(_run(server, "get_flow_graph", {"flow_name": "proposal"})))
    note = next(entry for entry in first["_truncated"]["lists"]
                if entry["path"] == "graph.blocks")
    page = json.loads(_text(_run(server, "get_flow_graph", {
        "flow_name": "proposal", "section": "graph.blocks",
        "offset": note["next_offset"],
    })))
    assert page["total_items"] == 200
    assert page["items"][0]["id"] == f"b{note['next_offset']:03d}"
    assert page["next_offset"] == note["next_offset"] + len(page["items"])


def test_section_read_does_not_clear_the_flow_write_back_refusal(monkeypatch):
    envelope = _graph_envelope(200)
    server, backend = _architect_server(monkeypatch, envelope)
    monkeypatch.setitem(SERVER_RESULT_TEXT.RESULT_LIMITS, "get_flow_graph", 20_000)
    _text(_run(server, "get_flow_graph", {"flow_name": "proposal"}))
    # A complete read of one part is not a complete graph.
    part = json.loads(_text(_run(
        server, "get_flow_graph", {"flow_name": "proposal", "section": "name"}
    )))
    assert (part["text"], part["next_offset"]) == ("proposal", None)
    backend.reset_mock()
    refused = _run(server, "update_flow_graph",
                   {"flow_name": "proposal", "graph": envelope["graph"]})
    assert refused.get("isError") is True
    assert "update_flow_graph refused" in refused["content"][0]["text"]
    backend.assert_not_called()


def test_a_trimmed_list_fingerprint_matches_a_section_read_of_it(monkeypatch):
    # Strings inside the list are shortened before the list itself is
    # trimmed; the fingerprint still describes the unshortened list.
    envelope = {"flow_id": "FLOW-UUID", "name": "proposal",
                "graph": {"blocks": [{"id": f"b{index:03d}", "goal": "g" * 1_000}
                                     for index in range(600)],
                          "edges": []}}
    server, _ = _architect_server(monkeypatch, envelope)
    first = json.loads(_text(_run(server, "get_flow_graph", {"flow_name": "proposal"})))
    note = next(entry for entry in first["_truncated"]["lists"]
                if entry["path"] == "graph.blocks")
    assert any(entry["path"].startswith("graph.blocks[")
               for entry in first["_truncated"]["fields"])
    page = json.loads(_text(_run(server, "get_flow_graph", {
        "flow_name": "proposal", "section": "graph.blocks",
    })))
    assert page["fingerprint"] == note["fingerprint"]


def test_an_oversized_list_item_is_named_for_its_own_section_read():
    result = {"graph": {"blocks": [{"goal": "x" * 5_000}, {"goal": "small"}]}}
    request = result_text.SectionRequest("graph.blocks", 0)
    page = json.loads(render_section(result, "get_flow_graph", request, 2_000).text)
    assert page["items"] == [] and page["next_offset"] == 1
    assert "`graph.blocks[0]`" in page["note"]
    item = json.loads(render_section(
        result, "get_flow_graph", result_text.SectionRequest("graph.blocks[0]"), 2_000
    ).text)
    assert item["section"] == "graph.blocks[0]"
    assert item["value"]["_truncated"]["fields"][0]["path"] == "graph.blocks[0].goal"


def test_every_path_a_notice_names_resolves_to_the_reported_part():
    # The outer list is trimmed first; later passes then trim the nested list
    # of its LAST kept item, which must keep its original index (9, not 2).
    result = {"outer": [{"inner": ["a", "b"]} for _ in range(9)]
              + [{"inner": ["y" * 300] * 200}]}
    rendered = render_result(result, "get_flow_graph", limit=20_000)
    notice = json.loads(rendered.text)["_truncated"]
    assert {entry["path"] for entry in notice["lists"]} >= {"outer", "outer[9].inner"}
    for entry in notice["lists"]:
        part = result_text._resolve_section(result, entry["path"])
        assert len(part) == entry["items"], entry
    for entry in notice["fields"]:
        part = result_text._resolve_section(result, entry["path"])
        assert len(part) == entry["chars"], entry


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"section": "spec.nope"}, "keys `id`, `content`"),
        ({"section": "spec.content", "offset": 999_999}, "past the end"),
        ({"section": "spec", "offset": 3}, "pages text or a list"),
        ({"offset": 10}, "`offset` continues a `section`"),
        ({"section": "spec.content", "offset": -1}, "non-negative integer"),
    ],
)
def test_bad_section_reads_are_teaching_errors(monkeypatch, arguments, expected):
    spec = {"spec": {"id": "s1", "content": "c" * 1_000}}
    server, backend = _server(monkeypatch, get_manager_tools(), spec)
    outcome = _run(server, "get_spec", {"workstream_id": "ws-1", **arguments})
    assert outcome.get("isError") is True
    assert expected in outcome["content"][0]["text"]
    if "section" not in arguments or arguments.get("offset") == -1:
        backend.assert_not_called()  # refused before any backend call
