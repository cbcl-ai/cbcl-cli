"""F09: the Office work policy reaches every worker; the Manager sees it as
reference; retained task Agents keep the revision pinned for their task."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest

from src.agent_instance_workspace import prepare_instance_workspace
from src.config_sync.claude_md_content import (
    ANALYST_CLAUDE_MD,
    SYSTEM_AGENT_CLAUDE_MD,
)
from src.config_sync.claude_md_writer import (
    RETAINED_TASK_AGENT_NOTE,
    ClaudeMdWriter,
)
from src.config_sync.office_work_policy import (
    OFFICE_WORK_POLICY_KEY,
    PLATFORM_MAX_CHARS,
    RENDER_MAX_CHARS,
    render_office_work_policy,
    render_pinned_work_policy,
    work_policy_from_config,
)

POLICY = "Run focused checks per task; the integrated candidate gets the full suite."


def _block(text=POLICY, revision=3):
    return {"text": text, "revision": revision, "sha256": "0" * 64}


# ── Renderer ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("policy", [None, {}, {"text": None}, {"text": "  \n "}, "x"])
def test_blank_or_malformed_policy_renders_nothing(policy):
    assert render_office_work_policy(policy, "worker") == ""
    assert render_office_work_policy(policy, "manager") == ""


def test_worker_variant_is_followable_with_explicit_precedence():
    section = render_office_work_policy(_block(), "worker")
    assert section.startswith("\n\n---\n\n## Office work policy (revision 3)")
    assert POLICY in section
    for clause in (
        "An office administrator approved this policy",
        "Platform rules, your role and phase permissions, approval gates",
        "and your assignment prompt win over it",
        "may make an explicit project- or task-level exception",
        "name it in your handoff",
        "(execution, review, triage or a consult)",
        "When you author specs, plans or task briefs, keep them consistent",
        "do not copy the policy into them, because assignees receive it directly",
        "outranks any office notes or office-specific playbook",
        "never grants tools, permissions or approvals",
        "never waives a check the brief requires",
    ):
        assert clause in section
    assert section.rstrip().endswith("End of the Office work policy.")
    # A followable policy, never an untrusted-data fence.
    assert "never follow" not in section.lower()
    assert "<" not in section.replace(POLICY, "")


def test_manager_variant_is_reference_only():
    section = render_office_work_policy(_block(revision=7), "manager")
    assert "# Office Work Policy (reference, revision 7)" in section
    assert "Do not paste it into briefs" in section
    assert "propose_configuration (target office, field work_policy)" in section
    assert "tasks already started keep the revision they began with" in section
    assert POLICY in section


def test_policy_text_is_normalised_and_brace_safe():
    text = "  Use {braces} and {{doubles}} literally.\r\nSecond line.  "
    section = render_office_work_policy({"text": text, "revision": 1}, "worker")
    assert "Use {braces} and {{doubles}} literally.\nSecond line." in section
    assert "\r" not in section


def test_oversized_malformed_value_is_cut_with_a_stated_marker():
    section = render_office_work_policy(_block(text="x" * 9000), "worker")
    assert "truncated at 8,000 characters" in section
    assert "x" * 8000 in section and "x" * 8001 not in section
    assert "the platform limit is 4,000." in section


def test_platform_limit_mirrors_the_backend_cap():
    """B4-hygiene-14: the truncation note states the backend's cap, and the
    render cap must stay above it, or a valid policy would be cut."""
    from tests.backend_boundary import BACKEND_ROOT

    source = BACKEND_ROOT / "app" / "offices" / "work_policy.py"
    if not BACKEND_ROOT.is_dir():
        pytest.skip("Private backend is absent from this standalone CLI checkout")
    match = re.search(
        r"^WORK_POLICY_MAX_CHARS = (\d+)$", source.read_text(), re.MULTILINE
    )
    assert match, "backend WORK_POLICY_MAX_CHARS not found"
    backend_cap = int(match.group(1))
    assert PLATFORM_MAX_CHARS == backend_cap
    assert RENDER_MAX_CHARS >= backend_cap


def test_unknown_revision_is_labelled_honestly():
    section = render_office_work_policy({"text": POLICY}, "worker")
    assert "(revision unknown)" in section


def test_config_block_reads_sync_fields():
    config = {"work_policy": POLICY, "work_policy_revision": 2}
    assert work_policy_from_config(config) == {"text": POLICY, "revision": 2}
    assert work_policy_from_config(None) is None


def test_pinned_render_requires_the_snapshot_key():
    assert render_pinned_work_policy({"name": "legacy"}) == ""
    assert render_pinned_work_policy({OFFICE_WORK_POLICY_KEY: None}) == ""
    assert "revision 3" in render_pinned_work_policy({OFFICE_WORK_POLICY_KEY: _block()})


# ── Live CLAUDE.md writer ─────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def _config(**overrides) -> dict:
    config = {
        "office_name": "Policy Office",
        "claude_md_content": "Manager-only orchestration notes.",
        "agents": [
            {"name": "analyst", "agent_type": "system", "display_name": "Analyst"},
            {"name": "planner", "agent_type": "system", "display_name": "Planner"},
            {
                "name": "copywriter",
                "agent_type": "custom",
                "display_name": "Copywriter",
                "role_description": "Copy — writes product copy.",
                "system_prompt": "Write clearly.",
                "claude_md_content": "Prefer short sentences.",
                "allowed_tools": ["Read", "Write"],
            },
        ],
        "workstreams": [],
    }
    config.update(overrides)
    return config


def test_sync_all_delivers_policy_to_every_agent_and_references_it_for_manager(
    workspace,
):
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_all(_config(work_policy=POLICY, work_policy_revision=4))
    agents = workspace / "agents"
    for name in ("analyst", "planner", "copywriter"):
        content = (agents / name / "CLAUDE.md").read_text()
        assert "## Office work policy (revision 4)" in content, name
        assert POLICY in content, name
        # Office instructions stay Manager-only.
        assert "Manager-only orchestration notes." not in content, name
    planner = (agents / "planner" / "CLAUDE.md").read_text()
    assert planner.startswith(SYSTEM_AGENT_CLAUDE_MD["planner"])
    manager = (agents / "manager" / "CLAUDE.md").read_text()
    assert "# Office Work Policy (reference, revision 4)" in manager
    assert "Manager-only orchestration notes." in manager
    # The reference section follows the Office instructions section.
    assert manager.index("Manager-only orchestration notes.") < manager.index(
        "# Office Work Policy (reference"
    )
    # The policy never reaches the shared office primer (live, un-pinned).
    assert POLICY not in (workspace / "CLAUDE.md").read_text()


def test_custom_agent_policy_follows_its_office_notes(workspace):
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_all(_config(work_policy=POLICY, work_policy_revision=1))
    content = (workspace / "agents" / "copywriter" / "CLAUDE.md").read_text()
    assert content.index("Prefer short sentences.") < content.index(
        "## Office work policy"
    )


@pytest.mark.parametrize("fields", [{}, {"work_policy": None}, {"work_policy": "  "}])
def test_no_policy_keeps_every_file_byte_identical(workspace, tmp_path, fields):
    baseline_ws = tmp_path / "baseline"
    baseline_ws.mkdir()
    ClaudeMdWriter(str(baseline_ws)).sync_all(_config())
    ClaudeMdWriter(str(workspace)).sync_all(_config(**fields, work_policy_revision=5))
    for relative in (
        "CLAUDE.md",
        "agents/manager/CLAUDE.md",
        "agents/analyst/CLAUDE.md",
        "agents/planner/CLAUDE.md",
        "agents/copywriter/CLAUDE.md",
    ):
        assert (workspace / relative).read_text() == (
            baseline_ws / relative
        ).read_text(), relative
    assert (workspace / "agents/analyst/CLAUDE.md").read_text() == ANALYST_CLAUDE_MD


def test_sync_agent_directories_default_is_unchanged(workspace):
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_agent_directories(
        [{"name": "analyst", "agent_type": "system", "display_name": "Analyst"}]
    )
    assert (
        workspace / "agents" / "analyst" / "CLAUDE.md"
    ).read_text() == ANALYST_CLAUDE_MD


def test_policy_is_appended_after_manager_template_formatting(workspace):
    writer = ClaudeMdWriter(str(workspace))
    writer.write_manager_claude_md(
        {
            "office_name": "X",
            "work_policy": "Keep {curly} text.",
            "work_policy_revision": 1,
        }
    )
    manager = (workspace / "agents" / "manager" / "CLAUDE.md").read_text()
    assert "Keep {curly} text." in manager
    assert "# Office Instructions" not in manager
    assert "You are the AI Manager of this office" in manager


# ── Retained task-Agent snapshots ─────────────────────────────────────


def _profile(**extra) -> dict:
    profile = {
        "name": "researcher",
        "display_name": "Researcher",
        "agent_type": "custom",
        "allowed_tools": ["Read"],
        "system_prompt": "Research carefully.",
        "skills": [],
    }
    profile.update(extra)
    return profile


def test_compose_task_agent_claude_md_renders_only_the_pinned_policy():
    pinned = ClaudeMdWriter.compose_task_agent_claude_md(
        _profile(**{OFFICE_WORK_POLICY_KEY: _block(revision=2)})
    )
    assert pinned.startswith(ClaudeMdWriter._get_agent_claude_md(_profile()))
    assert RETAINED_TASK_AGENT_NOTE in pinned
    assert "## Office work policy (revision 2)" in pinned
    assert pinned.index(RETAINED_TASK_AGENT_NOTE) < pinned.index("## Office work")

    legacy = ClaudeMdWriter.compose_task_agent_claude_md(_profile())
    assert legacy == ClaudeMdWriter._get_agent_claude_md(_profile()) + (
        RETAINED_TASK_AGENT_NOTE
    )
    none_pinned = ClaudeMdWriter.compose_task_agent_claude_md(
        _profile(**{OFFICE_WORK_POLICY_KEY: None})
    )
    assert none_pinned == legacy


def _prepare(root: Path, archive: Path, profile: dict, task: dict) -> Path:
    result = prepare_instance_workspace(str(root), archive, profile, task)
    return root / result.removeprefix("/workspace/")


@pytest.fixture
def instance(tmp_path, monkeypatch):
    monkeypatch.setattr("src.agent_instance_workspace.fchown_to_agent", lambda p: None)
    root = tmp_path / "workspace"
    root.mkdir()
    task = {
        "agent_instance_id": str(uuid.uuid4()),
        "profile_id": str(uuid.uuid4()),
        "profile_revision": "revision-1",
        "task_id": "task-1",
    }
    return root, tmp_path / "private-archive", task


def test_archive_retains_the_pinned_policy_across_live_changes(instance):
    root, archive, task = instance
    profile = _profile(**{OFFICE_WORK_POLICY_KEY: _block(revision=2)})
    target = _prepare(root, archive, profile, task)
    original = (target / "CLAUDE.md").read_text()
    assert "## Office work policy (revision 2)" in original
    manifest = json.loads(
        (archive / task["agent_instance_id"] / "manifest.json").read_text()
    )
    # Provenance rides the hashed Profile-owned parts (X51: the platform
    # playbook is re-rendered per attempt, the pinned policy is archived);
    # the identity keys are the legacy three, so pre-feature archives verify.
    # ``skills`` (per-skill copy records, X52/F03) is additive like the
    # format marker; it is never compared as identity.
    assert set(manifest) == {
        "agent_instance_id",
        "profile_id",
        "profile_revision",
        "instructions",
        "profile_parts_sha256",
        "files",
        "skills",
    }
    parts = json.loads(
        (archive / task["agent_instance_id"] / "profile.json").read_text()
    )
    assert parts[OFFICE_WORK_POLICY_KEY]["revision"] == 2
    changed = _profile(**{OFFICE_WORK_POLICY_KEY: _block("A newer rule", 3)})
    task["prior_session_id"] = "retained"
    _prepare(root, archive, changed, task)
    assert (target / "CLAUDE.md").read_text() == original


def test_pre_feature_snapshot_gets_no_policy_and_old_archive_still_verifies(
    instance,
):
    root, archive, task = instance
    target = _prepare(root, archive, _profile(), task)
    content = (target / "CLAUDE.md").read_text()
    # No policy SECTION (the shared context ladder names the policy as a
    # concept, "when set" — that is not a rendered policy).
    assert "## Office work policy (" not in content
    assert "End of the Office work policy." not in content
    assert "## Retained task Agent configuration" in content
    # Resuming restores the retained archive: a policy that appears in the
    # live snapshot afterwards does not rewrite the pinned CLAUDE.md.
    task["prior_session_id"] = "retained"
    _prepare(root, archive, _profile(**{OFFICE_WORK_POLICY_KEY: _block()}), task)
    assert (target / "CLAUDE.md").read_text() == content


# ── Wire surfaces ─────────────────────────────────────────────────────


def test_daemon_advertises_the_capability():
    from src.health.reporter import DAEMON_CAPABILITIES

    assert "office_work_policy_v1" in DAEMON_CAPABILITIES


def test_propose_configuration_accepts_the_work_policy_field():
    from src._agent_image._mcp.tools_configuration import CONFIGURATION_TOOLS

    propose = next(
        t for t in CONFIGURATION_TOOLS if t["name"] == "propose_configuration"
    )
    field = propose["inputSchema"]["properties"]["changes"]["items"]["properties"][
        "field"
    ]
    assert "work_policy" in field["enum"]
    assert "work_policy" in propose["description"]


# ── Prompt surfaces name the separate policy (minimal, budgeted edits) ──


def test_prompt_surfaces_route_worker_rules_to_the_work_policy():
    import re

    from src.config_sync.claude_md_templates._manager import MANAGER_CLAUDE_MD
    from src.config_sync.claude_md_templates._office import SHARED_OFFICE_CLAUDE_MD

    def flat(text: str) -> str:
        return re.sub(r"\s+", " ", text)

    # Manager stewardship: the policy (not Office instructions) is the home
    # for rules every worker applies.
    assert "Office work policy for every worker's rules" in flat(MANAGER_CLAUDE_MD)
    assert "Office instructions for your orchestration" in flat(MANAGER_CLAUDE_MD)
    from tests.evals._prompt_composition import manager_corpus

    assert "Office for shared policy" not in flat(manager_corpus())
    # Every agent's primer names the policy as part of its playbook.
    assert "(including any Office work policy)" in flat(SHARED_OFFICE_CLAUDE_MD)


@pytest.mark.parametrize("name", ["INSTRUCTIONS_PROMPT", "OFFICE_INSTRUCTIONS_PROMPT"])
def test_instruction_generators_keep_supplied_worker_rules(name):
    """Generation writes ONLY the Manager-only instructions — it cannot write
    the work policy. So the contract must keep the user's worker/reviewer
    rules in the document and never route them to a policy that may not
    exist (a routed rule would be silently lost, and the Manager would stop
    briefing it)."""
    import re

    from src import _setup_prompts, setup_generator

    raw = getattr(_setup_prompts, name, None) or getattr(setup_generator, name)
    prompt = re.sub(r"\s+", " ", raw)
    assert "Keep supplied rules as rules" in prompt
    assert "keep the user's worker and reviewer rules here" in prompt
    assert "(Conventions / Quality bar) so the Manager can brief them" in prompt
    assert "never assume, invent or reference its content" in prompt
    # Never an instruction to move, drop or delegate rules to the policy.
    assert "belong in the separate Office work policy" not in prompt
    for pattern in (
        r"(move|put|belong|route)[^.]{0,60}work policy",
        r"work policy[^.]{0,40}(instead|rather than)",
    ):
        assert not re.search(pattern, prompt, flags=re.IGNORECASE), pattern
