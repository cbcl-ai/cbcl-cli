"""Retained task-Agent filters against the REAL backend roster shapes (X47/X53).

``test_workspace_composition`` pins the filters on hand-written dicts; this
module builds the three shapes with the backend's own serializers, so a
field renamed or dropped on either side fails here instead of silently
emptying a retained Agent's connectors or skill parameters again:

* ``app.agents.instances.profile_snapshot`` — the claim receipt snapshot
  the retained Agent starts from (connectors carry ``id`` + ``is_enabled``);
* ``app.ws.sync_config._serialize_agent`` — the live ``sync_config`` roster
  (connectors carry ``is_enabled`` but no ``id``; skills carry
  ``parameter_schema``);
* ``app.agents.schemas.AgentResponse`` — the REST ``GET /agents`` roster the
  dispatcher refetches (``SkillSummary`` / ``ConnectorSummary``: no
  ``parameter_schema``, no ``is_enabled``).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from src.agent_instance_workspace import _refresh_skill_params
from src.config_sync._descriptor_io import open_dir_nofollow
from src.config_sync.claude_md_templates._custom_agent import (
    generate_custom_agent_claude_md,
)
from src.orchestrator.agent_supervisor import retained_connectors
from src.orchestrator.task_dispatcher import merge_rest_roster
from tests.backend_boundary import import_backend

instances = import_backend("app.agents.instances")
sync_config = import_backend("app.ws.sync_config")
agent_schemas = import_backend("app.agents.schemas")


def _connector(name: str, *, enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        display_name=name.upper(),
        description="",
        connection_type="mcp",
        mcp_server_name=name,
        parameter_schema=[],
        is_enabled=enabled,
    )


def _skill(name: str, parameters: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        display_name=name.title(),
        description=f"Use when {name} work is requested.",
        parameter_schema=parameters,
    )


def _profile(connectors: list, skills: list) -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        office_id=uuid.uuid4(),
        name="sales-writer",
        agent_type="custom",
        display_name="Sales Writer",
        avatar_emoji="S",
        role_description="Writes sales material.",
        system_prompt="Write clearly.",
        model="opus",
        allowed_tools=["Read", "Write"],
        is_active=True,
        subagents=None,
        claude_md_content=None,
        effort=None,
        secret_env_allowlist=None,
        skills=skills,
        connectors=connectors,
        created_at=now,
        updated_at=now,
    )


TONE = {"name": "TONE", "type": "string", "description": "Voice", "is_secret": False}
API_KEY = {"name": "API_KEY", "type": "string", "description": "", "is_secret": True}


def test_snapshot_connectors_survive_only_while_live_and_enabled() -> None:
    crm, mail, dropped = (
        _connector("crm", enabled=True),
        _connector("mail", enabled=True),
        _connector("dropped", enabled=True),
    )
    snapshot = instances.profile_snapshot(_profile([crm, mail, dropped], []))
    # Later: mail is disabled, dropped is unassigned (the live sync_config).
    mail.is_enabled = False
    live = sync_config._serialize_agent(_profile([crm, mail], []))
    # The two shapes do not share ``id`` — the old id-keyed filter lost all.
    assert all("id" not in connector for connector in live["connectors"])
    kept = retained_connectors(snapshot["connectors"], live["connectors"])
    assert [connector["name"] for connector in kept] == ["crm"]


def test_rest_connector_summaries_are_unknown_not_enabled() -> None:
    crm = _connector("crm", enabled=True)
    snapshot = instances.profile_snapshot(_profile([crm], []))
    rest = agent_schemas.AgentResponse.model_validate(_profile([crm], [])).model_dump(
        mode="json"
    )
    assert "is_enabled" not in rest["connectors"][0]
    assert retained_connectors(snapshot["connectors"], rest["connectors"]) == []
    # The custom-agent CLAUDE.md does not advertise an unknown connector.
    assert "**CRM**" not in generate_custom_agent_claude_md(rest)
    assert "**CRM**" in generate_custom_agent_claude_md(
        sync_config._serialize_agent(_profile([crm], []))
    )


def test_rest_roster_refresh_keeps_skill_parameters(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("src.agent_instance_workspace.fchown_to_agent", lambda p: None)
    style = _skill("style", [TONE, API_KEY])
    profile = _profile([_connector("crm", enabled=True)], [style])
    synced = sync_config._serialize_agent(profile)
    rest = agent_schemas.AgentResponse.model_validate(profile).model_dump(mode="json")
    assert "parameter_schema" not in rest["skills"][0]

    merged = merge_rest_roster([synced], [rest])
    assert merged[0]["skills"] == synced["skills"]
    assert merged[0]["connectors"] == synced["connectors"]

    root = tmp_path / "workspace"
    (root / ".claude/skills/style").mkdir(parents=True)
    (root / ".claude/skills/style/params.json").write_text(
        json.dumps({"TONE": "warm", "API_KEY": "must-not-copy"})
    )
    target = tmp_path / "target"
    (target / ".claude/skills/style").mkdir(parents=True)
    snapshot = instances.profile_snapshot(profile)
    # Live roster in the REST shape: unknown schema keeps the snapshot set.
    with open_dir_nofollow(target) as target_fd:
        _refresh_skill_params(root, target_fd, snapshot["skills"], rest["skills"])
    written = json.loads((target / ".claude/skills/style/params.json").read_text())
    assert written == {"TONE": "warm"}
    # Live roster in the sync shape: the live schema still classifies.
    with open_dir_nofollow(target) as target_fd:
        _refresh_skill_params(
            root, target_fd, snapshot["skills"], synced["skills"]
        )
    written = json.loads((target / ".claude/skills/style/params.json").read_text())
    assert written == {"TONE": "warm"}


def test_pinned_work_policy_contract_matches_backend():
    """B2-hygiene-14: the snapshot key, the capability and the block shape
    are defined on both sides; a one-sided rename silently drops the policy
    from retained task Agents (the renderer returns "" for a missing key)."""
    from src.config_sync.office_work_policy import (
        OFFICE_WORK_POLICY_KEY,
        render_pinned_work_policy,
    )
    from src.health.reporter import DAEMON_CAPABILITIES

    work_policy = import_backend("app.offices.work_policy")
    assert instances.OFFICE_WORK_POLICY_KEY == OFFICE_WORK_POLICY_KEY
    assert work_policy.WORK_POLICY_CAPABILITY in DAEMON_CAPABILITIES
    block = work_policy.work_policy_block("Cite sources.", 2)
    section = render_pinned_work_policy({instances.OFFICE_WORK_POLICY_KEY: block})
    assert "Cite sources." in section
    assert "revision 2" in section
