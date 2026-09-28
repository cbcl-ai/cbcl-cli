"""Offline guards: the behavioral API lane cannot leak answers (F06 D).

Default-lane (no model calls). A fake sender captures the exact Messages API
bodies ``decide_as_manager`` would send, and these tests pin that:

* the system text is exactly the production render — nothing appended;
* the first user message is exactly what the user typed;
* the tools are exactly ``select_session_tools`` for the context, under the
  production-visible ``mcp__cubicle-tools__`` prefix, with no internal keys;
* the reasoning configuration is production's (adaptive thinking + the
  Manager's effort) and no sampling parameter is sent;
* removing a rule from the production sources removes it from the request
  (mutation test): the eval cannot re-supply a rule production dropped;
* eval sources contain none of the historical answer-leading literals;
* the stub office answers only real read-only Manager tools and renders the
  roster in the backend's format.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from src._agent_image import mcp_tool_server
from src._session_policy import DEFAULT_OPUS_EFFORT
from src.config_sync import claude_md_writer
from src.config_sync.claude_md_writer import ClaudeMdWriter
from src.config_sync.sync_service import ConfigStore
from src.orchestrator.manager_context import build_dynamic_context
from tests.backend_boundary import import_backend
from tests.evals.live import _harness
from tests.evals.live import _stub_office
from tests.evals.live._stub_office import (
    CONSULT_ONLY,
    READ_ONLY_MANAGER_TOOLS,
    SYSTEM_ROSTER,
    RosterAgent,
    StubOffice,
)

EVALS_ROOT = Path(__file__).parent
USER_TEXT = "Add a /healthz endpoint to our FastAPI app."
_API_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
# Every phrasing production uses for "the reviewer is not the executor".
_REVIEWER_RULE = re.compile(
    r"reviewer[^\n]{0,80}?(?:≠|!=|must differ|different from|differ from|"
    r"never the executor|not the executor)"
    r"|≠\s*`?assigned_agent"
    r"|review(?:s|ing)? (?:its|their|your) own work"
    r"|never reviews? (?:its|their|your) own",
    re.IGNORECASE,
)
# Answer-leading constructs removed from the lane; none may return.
_FORBIDDEN_EVAL_LITERALS = (
    "Eval mode",
    "Evaluation:",
    "eval_json_suffix",
    "Return ONLY the JSON",
    "Do not perform the work yourself",
    "Create one new assignment",
)


def _office() -> StubOffice:
    return StubOffice(
        office_name="Acme Web",
        workstream_name="Backend",
        roster=SYSTEM_ROSTER + (
            RosterAgent(
                "python-developer", "Python Developer",
                "Backend engineering — owns the FastAPI service code and its tests.",
            ),
        ),
    )


class _CapturingSender:
    """Answers every request with one create_task decision; records bodies."""

    def __init__(self) -> None:
        self.bodies: list[dict] = []

    def __call__(self, body: dict) -> _harness.ApiResponse:
        self.bodies.append(json.loads(json.dumps(body)))
        return _harness.ApiResponse(
            content=[{
                "type": "tool_use", "id": "toolu_1",
                "name": _harness.TOOL_NAME_PREFIX + "create_task",
                "input": {"assigned_agent": "python-developer", "reviewer": "auditor"},
            }],
            stop_reason="tool_use", model="synthetic", usage={},
            message_id="msg_1", request_id=None, elapsed_seconds=0.0,
        )


async def _capture(office: StubOffice) -> dict:
    sender = _CapturingSender()
    decision = await _harness.decide_as_manager(office, USER_TEXT, send=sender)
    assert decision.tool_name == "create_task"
    assert len(sender.bodies) == 1
    return sender.bodies[0]


def _independent_production_system(tmp_path: Path, office: StubOffice) -> str:
    writer = ClaudeMdWriter(str(tmp_path))
    writer.ensure_directory_structure()
    writer.write_office_claude_md({"office_name": office.office_name})
    writer.write_manager_claude_md({"office_name": office.office_name})
    dynamic = build_dynamic_context(
        office.context_key, office.context_data(), ConfigStore(), False,
    )
    return "\n\n".join((
        (tmp_path / "CLAUDE.md").read_text(),
        (tmp_path / "agents/manager/CLAUDE.md").read_text(),
        dynamic,
    ))


async def test_request_system_is_exactly_the_production_render(tmp_path):
    office = _office()
    body = await _capture(office)
    assert body["system"] == _independent_production_system(tmp_path, office)


async def test_first_user_message_is_exactly_the_case_text():
    body = await _capture(_office())
    assert body["messages"] == [{"role": "user", "content": USER_TEXT}]


async def test_tools_are_the_production_selected_catalog_with_the_real_prefix():
    office = _office()
    body = await _capture(office)
    expected = mcp_tool_server.select_session_tools(
        "manager", "", "manager", None, office.context_key,
    )
    assert [tool["name"] for tool in body["tools"]] == [
        _harness.TOOL_NAME_PREFIX + tool["name"] for tool in expected
    ]
    for sent, source in zip(body["tools"], expected):
        assert _API_NAME.match(sent["name"])
        assert set(sent) == {"name", "description", "input_schema"}
        assert sent["description"] == source["description"]
        assert sent["input_schema"] == source["inputSchema"]
    assert body["tool_choice"] == {"type": "auto"}


async def test_request_uses_production_reasoning_and_no_sampling_params():
    body = await _capture(_office())
    assert not {"temperature", "top_p", "top_k"} & set(body)
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": DEFAULT_OPUS_EFFORT}
    assert DEFAULT_OPUS_EFFORT == "xhigh"
    assert body["max_tokens"] >= 16000


async def test_read_tools_are_answered_by_the_stub_and_the_decision_is_not_executed():
    office = _office()
    responses = iter([
        _harness.ApiResponse(
            content=[
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "tool_use", "id": "toolu_read",
                 "name": _harness.TOOL_NAME_PREFIX + "list_agents", "input": {}},
            ],
            stop_reason="tool_use", model="m", usage={}, message_id="1",
            request_id=None, elapsed_seconds=0.0,
        ),
        _harness.ApiResponse(
            content=[{"type": "tool_use", "id": "toolu_write",
                      "name": _harness.TOOL_NAME_PREFIX + "create_task",
                      "input": {"title": "x"}}],
            stop_reason="tool_use", model="m", usage={}, message_id="2",
            request_id=None, elapsed_seconds=0.0,
        ),
    ])
    bodies: list[dict] = []

    def send(body):
        bodies.append(json.loads(json.dumps(body)))
        return next(responses)

    decision = await _harness.decide_as_manager(office, USER_TEXT, send=send)
    assert decision.tool_name == "create_task" and decision.model_calls == 2
    assert [name for name, _ in office.read_log] == ["list_agents"]
    second = bodies[1]["messages"]
    # The assistant turn (thinking block included) is echoed unchanged and the
    # read result comes back as one tool_result user message.
    assert second[1]["role"] == "assistant"
    assert second[1]["content"][0]["type"] == "thinking"
    assert second[2]["content"][0]["type"] == "tool_result"
    assert second[2]["content"][0]["tool_use_id"] == "toolu_read"


async def test_every_tool_in_the_deciding_response_reaches_the_report():
    """EV-3 (compat EV-4): run_decision_loop stops at the first non-read
    block, but the other calls of that response are still recorded, so a
    forbidden delete_task after create_task becomes a forbidden effect."""
    from tests.evals import _live_report as live_report

    def send(body):
        return _harness.ApiResponse(
            content=[
                {"type": "tool_use", "id": "toolu_1",
                 "name": _harness.TOOL_NAME_PREFIX + "create_task", "input": {"title": "x"}},
                {"type": "tool_use", "id": "toolu_2",
                 "name": _harness.TOOL_NAME_PREFIX + "delete_task", "input": {"task_id": "t"}},
                {"type": "tool_use", "id": "toolu_3",
                 "name": _harness.TOOL_NAME_PREFIX + "get_board", "input": {}},
            ],
            stop_reason="tool_use", model="m", usage={}, message_id="1",
            request_id=None, elapsed_seconds=0.0,
        )

    live_report.reset_registry()
    live_report.begin_case("node")
    try:
        decision = await _harness.decide_as_manager(_office(), USER_TEXT, send=send)
    finally:
        live_report.end_case()
    details = live_report.details_for("node")
    live_report.reset_registry()
    assert decision.tool_name == "create_task"
    assert decision.other_tool_names == ["delete_task", "get_board"]
    assert details["decision_tool"] == "create_task"
    assert details["decision_tools"] == ["create_task", "delete_task", "get_board"]
    declared = {"forbidden_effects": ["delete_task"]}
    assert live_report.with_declared_effects(declared, details)["forbidden_effects"] == [
        "decision: called delete_task, which this case declares forbidden",
    ]


async def test_a_final_text_decision_records_no_tools():
    from tests.evals import _live_report as live_report

    def send(body):
        return _harness.ApiResponse(
            content=[{"type": "text", "text": "Which framework?"}], stop_reason="end_turn",
            model="m", usage={}, message_id="1", request_id=None, elapsed_seconds=0.0,
        )

    live_report.reset_registry()
    live_report.begin_case("node")
    try:
        decision = await _harness.decide_as_manager(_office(), USER_TEXT, send=send)
    finally:
        live_report.end_case()
    details = live_report.details_for("node")
    live_report.reset_registry()
    assert decision.kind == "final_text" and decision.tool_names == []
    assert details["decision_tool"] is None and details["decision_tools"] == []


async def test_truncated_or_refused_responses_are_errors_not_verdicts():
    from tests.evals import _live_report as live_report

    for stop_reason, category in (("max_tokens", "output_truncated"),
                                  ("refusal", "refusal")):
        def send(body, stop_reason=stop_reason):
            return _harness.ApiResponse(
                content=[{"type": "text", "text": "partial"}], stop_reason=stop_reason,
                model="m", usage={}, message_id="1", request_id=None,
                elapsed_seconds=0.0,
            )

        with pytest.raises(live_report.EvalHarnessError) as raised:
            await _harness.decide_as_manager(_office(), USER_TEXT, send=send)
        assert raised.value.category == category


def _scrub(text: str) -> str:
    return _REVIEWER_RULE.sub("", text)


def _scrub_values(value):
    """Scrub every string inside a tool definition, keeping its structure."""
    if isinstance(value, str):
        return _scrub(value)
    if isinstance(value, list):
        return [_scrub_values(item) for item in value]
    if isinstance(value, dict):
        return {key: _scrub_values(item) for key, item in value.items()}
    return value


async def test_removed_production_rule_is_absent_from_the_request(monkeypatch):
    """Mutation: drop the reviewer-difference rule from every production source."""
    baseline = json.dumps(await _capture(_office()), ensure_ascii=False)
    assert _REVIEWER_RULE.search(baseline), "sanity: production states the rule"

    original_tools = mcp_tool_server._get_manager_tools

    def scrubbed_tools():
        return _scrub_values(original_tools())

    monkeypatch.setattr(mcp_tool_server, "_get_manager_tools", scrubbed_tools)
    monkeypatch.setattr(
        claude_md_writer, "MANAGER_CLAUDE_MD", _scrub(claude_md_writer.MANAGER_CLAUDE_MD)
    )
    monkeypatch.setattr(
        claude_md_writer, "SHARED_OFFICE_CLAUDE_MD",
        _scrub(claude_md_writer.SHARED_OFFICE_CLAUDE_MD),
    )
    mutated = json.dumps(await _capture(_office()), ensure_ascii=False)
    leftover = _REVIEWER_RULE.findall(mutated)
    assert not leftover, (
        f"the eval re-supplied a rule production no longer states: {leftover}"
    )

    # The detector sees a rule smuggled in through eval-owned data (here the
    # stub roster), so the absence above is meaningful.
    original_roster = StubOffice.team_roster
    monkeypatch.setattr(
        StubOffice, "team_roster",
        lambda self: original_roster(self) + "\nThe reviewer must differ from the executor.",
    )
    smuggled = json.dumps(await _capture(_office()), ensure_ascii=False)
    assert _REVIEWER_RULE.search(smuggled)


def test_eval_sources_contain_no_answer_leading_literals():
    offenders = {}
    for folder in ("live", "runtime"):
        for path in sorted((EVALS_ROOT / folder).rglob("*")):
            if path.suffix not in {".py", ".json", ".md"} or not path.is_file():
                continue
            if "fixtures" in path.parts:
                continue  # synthetic office content, not eval instructions
            text = path.read_text(encoding="utf-8")
            found = [literal for literal in _FORBIDDEN_EVAL_LITERALS if literal in text]
            if found:
                offenders[str(path.relative_to(EVALS_ROOT))] = found
    assert not offenders, f"answer-leading eval literals returned: {offenders}"


def test_stub_read_tools_are_real_manager_reads():
    catalog = {tool["name"] for tool in mcp_tool_server.select_session_tools(
        "manager", "", "manager", None, "workstream:x",
    )}
    assert READ_ONLY_MANAGER_TOOLS <= catalog, READ_ONLY_MANAGER_TOOLS - catalog
    writes = mcp_tool_server._BOARD_WRITE_ACTIONS - {"get_action_request"}
    assert not READ_ONLY_MANAGER_TOOLS & writes
    for tool in mcp_tool_server.select_session_tools(
        "manager", "", "manager", None, "workstream:x",
    ):
        if tool["name"] in READ_ONLY_MANAGER_TOOLS:
            assert not tool["name"].startswith(("create_", "update_", "delete_"))


def test_stub_consult_only_set_matches_the_backend():
    """B7c-03: the consult-only annotation and ``assignable_names`` follow
    the set the backend actually refuses as assignee or reviewer."""
    task_service = import_backend("app.tasks.task_service")
    assert _stub_office._CONSULT_PATHS == task_service._CONSULT_ONLY_AGENTS
    assert CONSULT_ONLY == frozenset(task_service._CONSULT_ONLY_AGENTS)


def _backend_agent(agent: RosterAgent) -> SimpleNamespace:
    return SimpleNamespace(
        id=agent.profile_id,
        name=agent.name,
        display_name=agent.display_name,
        avatar_emoji=agent.avatar_emoji,
        role_description=agent.role_description,
        model=agent.model,
        allowed_tools=list(agent.allowed_tools),
        agent_type=agent.agent_type,
        skills=[SimpleNamespace(display_name=skill) for skill in agent.skills],
    )


async def test_stub_roster_renders_exactly_the_backend_roster(monkeypatch):
    """B7c-03: the whole roster text (preamble, headings, section order and
    entries) equals ``_build_team_roster`` for the same agents, including a
    custom agent with skills and case-local consult-only agents."""
    context_builder = import_backend("app.ws.context_builder")
    roster = (
        *SYSTEM_ROSTER,
        RosterAgent("flow-architect", "Flow Architect", "Flow engineering — designs flows.",
                    agent_type="system", avatar_emoji="🧭"),
        RosterAgent("data-curator", "Data Curator", "Data stewardship — owns collections.",
                    agent_type="system", avatar_emoji="🗄️"),
        RosterAgent("billing-analyst", "Billing Analyst", "Billing — owns invoices.",
                    skills=("Invoice review", "Refund policy")),
    )
    office = StubOffice(office_name="Office", workstream_name="Work", roster=roster)

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [_backend_agent(agent) for agent in roster]

    class _Session:
        async def execute(self, _statement):
            return _Result()

    async def no_queue(_session, _office_id):
        return {}

    monkeypatch.setattr(context_builder, "_fetch_queued_counts_by_agent", no_queue)
    rendered = await context_builder._build_team_roster(_Session(), uuid.uuid4())
    assert office.team_roster() == rendered
    assert {"planner", "flow-architect", "data-curator"}.isdisjoint(
        office.assignable_names()
    )


def test_stub_roster_matches_the_backend_roster_format():
    context_builder = import_backend("app.ws.context_builder")
    for agent in SYSTEM_ROSTER:
        backend_agent = _backend_agent(agent)
        assert agent.roster_entry() == context_builder._format_agent_entry(backend_agent)
