"""07 review — tool-description group pins (tool descriptions are prompts).

The T5.3.7 bar: every factual tool-description change lands with the eval
that pins the fact. These pins cover the 2026-08 tool-descriptions review
group: the worker ``add_activity`` "answer" event (the functional bug — the
MA playbook teaches it, the schema rejected it), the get_file/list_files
metadata truth, the propose_update_task live routing, the escalate_blocker
playbook pointer, the bind_script_variable-era Office-Secrets story, the
list_agents skills/connectors split, the retired phase tags, and the
assignment-schedule cron specials. No backend import — these run in the
cbcl-cli mirror layout too.
"""
from __future__ import annotations

from src._agent_image._mcp.tools_manager import get_manager_tools
from src._agent_image._mcp.tools_worker import get_worker_tools


def _tool(tools: list[dict], name: str) -> dict:
    for t in tools:
        if t["name"] == name:
            return t
    raise AssertionError(f"{name} not in catalog")


def _norm(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# add_activity — the worker enum serves "answer"
# ---------------------------------------------------------------------------


def test_worker_add_activity_enum_serves_answer():
    """The MA (served the worker catalog) is playbook-mandated to reply to
    blocked-task questions via add_activity(event_type="answer")
    (_manager_assistant.py, triage path A) and the backend tool-call
    validator accepts it — the worker inputSchema enum must serve it too,
    or the CLI rejects the call the playbook mandates."""
    tool = _tool(get_worker_tools(), "add_activity")
    enum = tool["inputSchema"]["properties"]["event_type"]["enum"]
    assert "answer" in enum
    # The description teaches when to use it.
    assert "answer" in _norm(tool["description"])


# ---------------------------------------------------------------------------
# get_file / list_files — metadata, not content (both catalogs)
# ---------------------------------------------------------------------------


def test_get_file_descriptions_claim_metadata_not_content():
    """The office_get_file handler returns METADATA only ("Agents read
    content via the Read tool" — backend/app/ws/office_file_handler.py);
    both role descriptions must say so, and neither may claim the tool
    fetches content."""
    for tools in (get_manager_tools(), get_worker_tools()):
        desc = _norm(_tool(tools, "get_file")["description"])
        assert "metadata" in desc
        assert "Read tool" in desc
        assert "full content" not in desc


def test_list_files_descriptions_do_not_route_content_to_get_file():
    """list_files must not tell the model get_file reads content — the
    truth is get_file returns the file_path and the Read tool reads it."""
    for tools in (get_manager_tools(), get_worker_tools()):
        desc = _norm(_tool(tools, "list_files")["description"])
        assert "pair with `get_file` for that" not in desc
        assert "`get_file` returns the file_path" in desc


# ---------------------------------------------------------------------------
# propose_update_task — live routing, no Phase-B fiction
# ---------------------------------------------------------------------------


def test_propose_update_task_states_live_routing():
    """update_task requests default to category=workstream → the Manager's
    auto-decide turn (backend action_requests service). No retired
    phase-plan fiction, no 'manual approval' story."""
    desc = _norm(_tool(get_worker_tools(), "propose_update_task")["description"])
    assert "Phase B" not in desc
    assert "manual approval" not in desc
    assert "auto-decide" in desc


# ---------------------------------------------------------------------------
# escalate_blocker — pointer at a document the worker can actually read
# ---------------------------------------------------------------------------


def test_escalate_blocker_points_at_the_playbook_template():
    """The legacy worker-spec doc is retired; the blocker protocol the
    worker can read lives in its playbook (CLAUDE.md, Communication
    section — _shared_agent.py's ESCALATED template)."""
    desc = _norm(_tool(get_worker_tools(), "escalate_blocker")["description"])
    assert "worker-spec" not in desc
    assert "ESCALATED template" in desc
    assert "Communication section" in desc


# ---------------------------------------------------------------------------
# list_office_secrets — the bind_script_variable-era story (both catalogs)
# ---------------------------------------------------------------------------


def test_list_office_secrets_carries_the_asd_binding_split():
    """ASD playbook rule 5: secret EXISTS → the ASD binds it itself via
    bind_script_variable (no user click); secret MISSING → the user adds
    it in Settings → Security, then the ASD binds. The pre-Phase-1.5
    'the user binds via the Variables UI' story must not survive in
    either catalog's description."""
    for tools in (get_manager_tools(), get_worker_tools()):
        desc = _norm(_tool(tools, "list_office_secrets")["description"])
        assert "bind_script_variable" in desc
        assert "Variables UI" not in desc
        assert "Settings → Security" in desc


# ---------------------------------------------------------------------------
# list_agents — skills carry descriptions; connectors carry connection types
# ---------------------------------------------------------------------------


def test_list_agents_puts_connection_types_on_connectors():
    """Skills have not carried connection_type since the Connector split
    (RF-7) — the roster wire hardcodes it None on every skill; the real
    field lives on the connectors list."""
    desc = _norm(_tool(get_manager_tools(), "list_agents")["description"])
    assert "skills (with descriptions) and connectors (with connection types)" in desc
    assert "descriptions and connection types" not in desc


# ---------------------------------------------------------------------------
# list_script_templates — no internal phase-plan tags
# ---------------------------------------------------------------------------


def test_list_script_templates_carries_no_phase_tag():
    """The marketplace shipped long ago; internal build-plan labels are
    noise re-served every session."""
    for tools in (get_manager_tools(), get_worker_tools()):
        desc = _tool(tools, "list_script_templates")["description"]
        assert "Phase 2" not in desc


# ---------------------------------------------------------------------------
# assignment schedules — cron specials match the shared parser
# ---------------------------------------------------------------------------


def test_assignment_schedule_cron_descriptions_list_all_four_specials():
    """The operations service parses cadence with the SHARED
    app/scripts/cron_parser, which accepts @hourly/@daily/@weekly/@monthly
    (rest-api.md §15.2); under-advertising makes the model claim hourly or
    monthly ops need a raw 5-field expression."""
    mgr = get_manager_tools()
    create = _tool(mgr, "schedule_assignment")["inputSchema"]["properties"]
    update = _tool(mgr, "update_assignment_schedule")["inputSchema"]["properties"]
    for props in (create, update):
        desc = props["cron_expr"]["description"]
        for special in ("@hourly", "@daily", "@weekly", "@monthly"):
            assert special in desc, f"{special} missing from cron_expr description"
