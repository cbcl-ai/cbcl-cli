"""Tests for ClaudeMdWriter and related CLAUDE.md content modules."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config_sync.claude_md_content import (
    ANALYST_CLAUDE_MD,
    AUDITOR_CLAUDE_MD,
    AUTOMATION_SCRIPT_DEV_CLAUDE_MD,
    MANAGER_ASSISTANT_CLAUDE_MD,
    MANAGER_CLAUDE_MD,
    SHARED_AGENT_WORK_RULES,
    SHARED_OFFICE_CLAUDE_MD,
    SYSTEM_AGENT_CLAUDE_MD,
    generate_custom_agent_claude_md,
    generate_workstream_claude_md,
)
from src.config_sync.claude_md_writer import ClaudeMdWriter
from src.config_sync.workspace_setup import WorkspaceSetup


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a temporary workspace directory."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


# ---------------------------------------------------------------------------
# WorkspaceSetup tests
# ---------------------------------------------------------------------------

class TestWorkspaceSetup:
    def test_ensure_structure_creates_all_dirs(self, workspace: Path) -> None:
        setup = WorkspaceSetup(str(workspace))
        setup.ensure_structure()

        # Matches WorkspaceSetup.ensure_structure — the `shared/outputs`
        # convention was consolidated into a single top-level `outputs/`
        # and per-agent directories live under `agents/`.
        expected = [
            workspace / "agents",
            workspace / "agents" / "manager",
            workspace / "workstreams",
            workspace / ".claude" / "skills",
            workspace / ".scripts",
            workspace / ".cubicle",
            workspace / "outputs",
        ]
        for d in expected:
            assert d.is_dir(), f"Expected directory missing: {d}"

    def test_ensure_structure_idempotent(self, workspace: Path) -> None:
        setup = WorkspaceSetup(str(workspace))
        setup.ensure_structure()
        setup.ensure_structure()  # Should not raise
        assert (workspace / "agents").is_dir()


# ---------------------------------------------------------------------------
# Office CLAUDE.md tests
# ---------------------------------------------------------------------------

class TestOfficeClaude:
    def test_office_name_substitution(self, workspace: Path) -> None:
        writer = ClaudeMdWriter(str(workspace))
        writer.ensure_directory_structure()
        writer.write_office_claude_md({"office_name": "My Test Office"})

        content = (workspace / "CLAUDE.md").read_text()
        assert "# Office: My Test Office" in content

    # NOTE: Manager-specific content (kanban tools, Review section,
    # Agent Selection Guide) moved out of SHARED_OFFICE_CLAUDE_MD (the
    # short header every agent sees) into MANAGER_CLAUDE_MD (the
    # Manager's CLAUDE.md). The assertions below target whichever
    # constant owns each piece today.

    def test_manager_tool_set_documented(self, workspace: Path) -> None:
        # F9 trim (audit): the canonical Manager tool list lives in
        # SHARED_OFFICE_CLAUDE_MD's "Canonical Tool Reference" section
        # (loaded automatically alongside MANAGER_CLAUDE_MD). Manager
        # CLAUDE.md no longer duplicates the list — it covers patterns,
        # not enumeration. Verify every Manager-callable tool appears
        # in the shared reference and the positive allowlist still
        # appears in MANAGER_CLAUDE_MD.
        manager_tools = [
            "create_task", "update_task", "move_task", "archive_task",
            "delete_task", "get_board", "get_task_detail", "add_activity",
            "save_file", "list_files", "get_file",
        ]
        shared = SHARED_OFFICE_CLAUDE_MD.format(office_name="Test")
        for tool in manager_tools:
            assert tool in shared, (
                f"Missing tool '{tool}' in SHARED_OFFICE_CLAUDE_MD "
                "canonical reference"
            )
        # Positive allowlist still in MANAGER_CLAUDE_MD.
        assert "Your Allowed Tools — Positive Allowlist" in MANAGER_CLAUDE_MD
        for tool in ("create_task", "save_file", "search_kb"):
            assert tool in MANAGER_CLAUDE_MD, (
                f"Missing tool '{tool}' in MANAGER_CLAUDE_MD allowlist"
            )

    def test_shared_office_md_no_phantom_tools(self, workspace: Path) -> None:
        # The SHARED header is seen by workers; it must NEVER reference
        # Manager-only memory/kb-write tools that workers cannot call.
        content = SHARED_OFFICE_CLAUDE_MD.format(office_name="Test")
        phantom_tools = [
            "memory.save",
            "memory.recall",
            "mcp__memory__save",
            "mcp__memory__recall",
            "kb.save",
        ]
        for phantom in phantom_tools:
            assert phantom not in content, (
                f"Phantom tool {phantom} in shared office CLAUDE.md"
            )

    def test_manager_md_contains_review_section(self) -> None:
        assert "Review" in MANAGER_CLAUDE_MD
        assert "Manager Assistant" in MANAGER_CLAUDE_MD

    def test_manager_md_contains_agent_selection_guide(self) -> None:
        # Section heading was renamed from "Agent Selection Guide" to
        # "Agent Selection — MANDATORY pre-assignment audit" in the
        # specialist-first overhaul. The system-agent names must still
        # be mentioned because the guide lists them as fallbacks.
        assert "Agent Selection" in MANAGER_CLAUDE_MD
        assert "pre-assignment audit" in MANAGER_CLAUDE_MD
        assert "Manager Assistant" in MANAGER_CLAUDE_MD
        assert "Analyst" in MANAGER_CLAUDE_MD
        assert "Auditor" in MANAGER_CLAUDE_MD

    def test_office_md_always_overwrites(self, workspace: Path) -> None:
        writer = ClaudeMdWriter(str(workspace))
        writer.ensure_directory_structure()

        # Write with one name
        writer.write_office_claude_md({"office_name": "AlphaOffice"})
        assert "# Office: AlphaOffice" in (workspace / "CLAUDE.md").read_text()

        # Overwrite with different name
        writer.write_office_claude_md({"office_name": "BetaOffice"})
        content = (workspace / "CLAUDE.md").read_text()
        assert "# Office: BetaOffice" in content
        assert "AlphaOffice" not in content


# ---------------------------------------------------------------------------
# System Agent CLAUDE.md tests
# ---------------------------------------------------------------------------

class TestSystemAgentClaude:
    def test_all_system_agents_have_entries(self) -> None:
        expected = ["analyst", "manager-assistant", "auditor", "automation-script-developer"]
        for name in expected:
            assert name in SYSTEM_AGENT_CLAUDE_MD, f"Missing system agent: {name}"

    def test_analyst_has_correct_tool_names(self) -> None:
        content = ANALYST_CLAUDE_MD
        assert "mcp__cubicle-tools__search_kb" in content
        assert "mcp__cubicle-tools__save_file" in content
        assert "mcp__cubicle-tools__attach_to_task" in content
        assert "mcp__cubicle-tools__add_activity" in content
        assert "mcp__cubicle-tools__update_status" in content

    def test_analyst_has_research_methodology(self) -> None:
        assert "Research Methodology" in ANALYST_CLAUDE_MD
        assert "Gather" in ANALYST_CLAUDE_MD
        assert "Analyze" in ANALYST_CLAUDE_MD
        assert "Synthesize" in ANALYST_CLAUDE_MD
        assert "Recommend" in ANALYST_CLAUDE_MD

    def test_analyst_has_output_formats(self) -> None:
        assert "Research Reports" in ANALYST_CLAUDE_MD
        assert "Plans and Implementation Roadmaps" in ANALYST_CLAUDE_MD
        assert "Comparisons and Evaluations" in ANALYST_CLAUDE_MD

    def test_auditor_has_review_approaches(self) -> None:
        assert "Reviewing Code or Technical Implementations" in AUDITOR_CLAUDE_MD
        assert "Reviewing Research and Analysis" in AUDITOR_CLAUDE_MD
        assert "Reviewing Plans and Strategies" in AUDITOR_CLAUDE_MD
        assert "Reviewing Documents and Reports" in AUDITOR_CLAUDE_MD

    def test_auditor_has_audit_report_format(self) -> None:
        assert "Audit Report Format" in AUDITOR_CLAUDE_MD
        assert "PASS / FAIL / CONDITIONAL" in AUDITOR_CLAUDE_MD

    def test_automation_script_dev_has_script_lifecycle(self) -> None:
        content = AUTOMATION_SCRIPT_DEV_CLAUDE_MD
        assert "mcp__cubicle-tools__register_script" in content
        assert "mcp__cubicle-tools__execute_script" in content
        assert "mcp__cubicle-tools__get_script_status" in content

    def test_automation_script_dev_has_testing_strategy(self) -> None:
        # The section name stayed stable ("Testing Strategy"); the
        # specific sub-headings evolved from "Dry-run mode / Limited
        # execution" into the more prescriptive "Test Run 1 — Dry run"
        # / "Test Run 2 — Real execution" protocol. Assert on the
        # protocol markers the current doc actually uses.
        content = AUTOMATION_SCRIPT_DEV_CLAUDE_MD
        assert "Testing Strategy" in content
        assert "Test Run 1" in content
        assert "Dry run" in content
        assert "Test Run 2" in content

    def test_automation_script_dev_has_variable_schema(self) -> None:
        assert "Variable Schema Design" in AUTOMATION_SCRIPT_DEV_CLAUDE_MD
        assert "UPPER_SNAKE_CASE" in AUTOMATION_SCRIPT_DEV_CLAUDE_MD

    def test_manager_assistant_has_correct_tools(self) -> None:
        content = MANAGER_ASSISTANT_CLAUDE_MD
        assert "mcp__cubicle-tools__search_kb" in content
        assert "mcp__cubicle-tools__save_file" in content
        assert "mcp__cubicle-tools__update_status" in content

    def test_all_workers_have_common_sections(self) -> None:
        """All worker CLAUDE.md files must include delivery, communication, scope, completion.

        Manager Assistant is excluded — it has a special Board Operator role
        with a different prompt structure.
        """
        for name, content in SYSTEM_AGENT_CLAUDE_MD.items():
            if name == "manager-assistant":
                # Board Operator has its own structure
                assert "Communication" in content, f"{name} missing Communication"
                assert "Scope" in content, f"{name} missing Scope"
                continue
            assert "Delivering Your Work" in content, f"{name} missing Delivering"
            assert "Communication" in content, f"{name} missing Communication"
            assert "Scope" in content, f"{name} missing Scope"
            assert "Completion" in content, f"{name} missing Completion"

    def test_no_phantom_tools_in_worker_claudes(self) -> None:
        phantom_tools = ["memory.save", "memory.recall", "mcp__memory__", "kb.save"]
        for name, content in SYSTEM_AGENT_CLAUDE_MD.items():
            for phantom in phantom_tools:
                assert phantom not in content, f"Phantom tool {phantom} in {name}"

    def test_manager_assistant_escalation_uses_real_tool_names(self) -> None:
        """The MA's blocked-task playbook must reference the ACTUAL tool
        names workers can call — ``escalate_blocker``,
        ``request_clarification`` etc. — NOT the internal action verb
        ``propose_action`` (which isn't registered as a callable tool).

        Regression test for the TO-007.T50 incident: the playbook
        previously told the agent to "call the propose_action tool",
        which doesn't exist, so the MA fell through to "just post a
        comment" and the user got no Inbox item.
        """
        content = MANAGER_ASSISTANT_CLAUDE_MD
        # The MA MUST know to reach for these named tools on Path C.
        assert "escalate_blocker" in content, (
            "MA playbook missing escalate_blocker — the primary tool "
            "for Path C (user-decision blockers)"
        )
        assert "request_clarification" in content, (
            "MA playbook missing request_clarification"
        )
        # Bare 'propose_action' is a real backend action verb but
        # NOT a tool name. The playbook may mention it in a comment
        # explaining how the tools route internally, but it must
        # never appear as ``the propose_action tool`` in an
        # instruction that asks the agent to call it.
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if "`propose_action`" in line and "tool" in line.lower():
                # Allow the phrase when it's explicitly explaining
                # that the tools transform-to ``propose_action`` —
                # which uses words like "transform", "internally",
                # "action verb". A bare instruction like "call the
                # propose_action tool" must NOT exist.
                lowered = line.lower()
                explanatory_markers = (
                    "transform", "internally", "action verb",
                    "routes to", "internal action",
                )
                assert any(m in lowered for m in explanatory_markers), (
                    f"MA playbook line {i + 1} instructs the agent to "
                    f"call ``propose_action`` as a tool, but no such "
                    f"tool exists. Use ``escalate_blocker`` / "
                    f"``request_clarification`` / one of the typed "
                    f"Action Request tools instead.\nLine: {line!r}"
                )

    def test_system_agents_always_overwritten(self, workspace: Path) -> None:
        writer = ClaudeMdWriter(str(workspace))
        agents = [
            {"name": "analyst", "agent_type": "system", "display_name": "Analyst"},
        ]

        # Write once
        writer.sync_agent_directories(agents)
        original = (workspace / "agents" / "analyst" / "CLAUDE.md").read_text()
        assert original == ANALYST_CLAUDE_MD

        # Modify the file manually
        (workspace / "agents" / "analyst" / "CLAUDE.md").write_text("corrupted")

        # Sync again — should overwrite
        writer.sync_agent_directories(agents)
        restored = (workspace / "agents" / "analyst" / "CLAUDE.md").read_text()
        assert restored == ANALYST_CLAUDE_MD


# ---------------------------------------------------------------------------
# Custom Agent CLAUDE.md tests
# ---------------------------------------------------------------------------

class TestCustomAgentClaude:
    def test_generate_from_system_prompt(self) -> None:
        agent = {
            "name": "python-dev",
            "display_name": "Senior Python Developer",
            "system_prompt": "You are a senior Python developer.",
            "skills": [],
        }
        content = generate_custom_agent_claude_md(agent)
        assert "# Senior Python Developer" in content
        assert "You are a senior Python developer." in content
        assert "Delivering Your Work" in content
        assert "mcp__cubicle-tools__save_file" in content

    def test_generate_includes_skills(self) -> None:
        # Skill rendering uses a `### <Display Name>` heading followed
        # by the playbook path — the earlier `**Name** (📖 Playbook)`
        # format was replaced when skill docs were refactored to pure
        # Markdown headings for better auto-discovery by Claude. MCP
        # connectivity moved to its own `## Service Connectors`
        # section rather than being inlined in the skill heading.
        agent = {
            "name": "dev",
            "display_name": "Developer",
            "system_prompt": "You code.",
            "skills": [
                {"name": "code-review", "display_name": "Code Review"},
                {"name": "slack", "display_name": "Slack"},
            ],
        }
        content = generate_custom_agent_claude_md(agent)
        assert "## Skills" in content
        assert "### Code Review" in content
        assert ".claude/skills/code-review/SKILL.md" in content
        assert "### Slack" in content
        assert ".claude/skills/slack/SKILL.md" in content

    def test_generate_includes_subagents(self, workspace: Path) -> None:
        """Subagents now render via the writer's ``_build_subagents_section``
        for BOTH system and custom agents (uniform layout). The
        backend ships ``subagents`` as ``list[dict]`` (per
        ``backend/app/agents/schemas.py``); the earlier in-template
        ``.items()`` block in ``_custom_agent.py`` assumed dict-of-
        dicts and was removed to fix that drift + dedupe the rendered
        section. The integrated path is tested here via the writer.
        """
        writer = ClaudeMdWriter(str(workspace))
        agents = [
            {
                "name": "dev",
                "agent_type": "custom",
                "display_name": "Developer",
                "system_prompt": "You code.",
                "subagents": [
                    {
                        "name": "test-runner",
                        "description": "Runs tests",
                        "allowed_tools": ["Bash", "Read"],
                        "when_to_use": "When you need to verify your changes",
                    },
                    {"name": "linter", "description": "Checks style"},
                ],
            },
        ]
        writer.sync_agent_directories(agents)
        content = (workspace / "agents" / "dev" / "CLAUDE.md").read_text()
        assert "## Your Subagents" in content
        assert "`test-runner`" in content
        assert "Runs tests" in content
        assert "**Tools**: Bash, Read" in content
        assert "When you need to verify your changes" in content
        assert "`linter`" in content
        assert "Checks style" in content

    def test_custom_with_claude_md_content(self, workspace: Path) -> None:
        """``claude_md_content`` is now APPENDED as an enrichment.

        Earlier behaviour replaced the generated baseline with the user
        string, which dropped the SHARED_AGENT_WORK_RULES block (file
        delivery, tool error handling, review mode). That made custom
        agents routinely fail to register deliverables. New behaviour:
        always emit the baseline, then append the office-owner content
        under a dedicated "Office-Specific Notes" section.
        """
        writer = ClaudeMdWriter(str(workspace))
        agents = [
            {
                "name": "custom-agent",
                "agent_type": "custom",
                "display_name": "Custom",
                "system_prompt": "You are a specialist.",
                "claude_md_content": "# My Custom Instructions\n\nDo things my way.",
            },
        ]
        writer.sync_agent_directories(agents)
        content = (workspace / "agents" / "custom-agent" / "CLAUDE.md").read_text()

        # Baseline must remain — system_prompt, shared rules, completion.
        assert "You are a specialist." in content
        assert "## Delivering Your Work" in content
        assert "## Tool Error Handling" in content

        # Custom content appended under the enrichment section.
        assert "## Office-Specific Notes" in content
        assert "# My Custom Instructions" in content
        assert "Do things my way." in content
        # Enrichment must come AFTER the baseline rules.
        assert content.index("Delivering Your Work") < content.index("Office-Specific Notes")

    def test_custom_without_claude_md_content(self, workspace: Path) -> None:
        """When claude_md_content is null, generate from system_prompt."""
        writer = ClaudeMdWriter(str(workspace))
        agents = [
            {
                "name": "custom-agent",
                "agent_type": "custom",
                "display_name": "My Agent",
                "system_prompt": "You are a specialist.",
                "claude_md_content": None,
                "skills": [],
            },
        ]
        writer.sync_agent_directories(agents)
        content = (workspace / "agents" / "custom-agent" / "CLAUDE.md").read_text()
        assert "# My Agent" in content
        assert "You are a specialist." in content
        assert "Delivering Your Work" in content

    def test_generate_has_standard_sections(self) -> None:
        agent = {"name": "a", "display_name": "A", "system_prompt": "test"}
        content = generate_custom_agent_claude_md(agent)
        assert "Delivering Your Work" in content
        assert "attach_to_task" in content
        assert "Communication" in content
        assert "Completion" in content
        assert "mcp__cubicle-tools__update_status" in content


# ---------------------------------------------------------------------------
# Workstream CLAUDE.md tests
# ---------------------------------------------------------------------------

class TestWorkstreamClaude:
    def test_generate_with_context_notes(self) -> None:
        ws = {
            "name": "Website Redesign",
            "short_code": "WR",
            "priority": "high",
            "description": "Full website overhaul",
            "goals": "Launch by Q2",
            "context_notes": "Use React 18 and Tailwind CSS.\nNo jQuery.",
        }
        content = generate_workstream_claude_md(ws)
        assert "# Workstream: Website Redesign" in content
        # Priority appears in a single-line metadata bar; substring match
        # tolerates surrounding formatting (`code` fences, separators).
        assert "Priority:" in content and "high" in content
        assert "Short code:" in content and "WR" in content
        assert "Full website overhaul" in content
        assert "Launch by Q2" in content
        assert "Use React 18 and Tailwind CSS." in content
        assert "No jQuery." in content
        # Per-workstream output dir convention is documented to agents.
        assert "/workspace/outputs/WR/" in content

    def test_generate_without_context_notes(self) -> None:
        ws = {
            "name": "API Migration",
            "priority": "medium",
            "description": "Migrate to v2 API",
            "goals": "Complete migration",
            "context_notes": None,
        }
        content = generate_workstream_claude_md(ws)
        assert "# Workstream: API Migration" in content
        assert "No additional context yet." in content
        assert "Good things to put here:" in content

    def test_generate_minimal(self) -> None:
        ws = {"name": "Minimal"}
        content = generate_workstream_claude_md(ws)
        assert "# Workstream: Minimal" in content
        assert "Priority:" in content and "medium" in content
        assert "No description provided." in content
        assert "No goals defined yet." in content

    def test_workstream_sync(self, workspace: Path) -> None:
        writer = ClaudeMdWriter(str(workspace))
        workstreams = [
            {
                "name": "Website Redesign",
                "priority": "high",
                "description": "Redesign the site",
                "goals": "Launch by Q2",
                "context_notes": "Custom notes here",
            },
            {
                "name": "API Migration",
                "priority": "medium",
                "description": "Migrate APIs",
            },
        ]
        writer.sync_workstream_directories(workstreams)

        ws1 = workspace / "workstreams" / "website-redesign" / "CLAUDE.md"
        ws2 = workspace / "workstreams" / "api-migration" / "CLAUDE.md"
        assert ws1.exists()
        assert ws2.exists()
        assert "Custom notes here" in ws1.read_text()
        assert "Migrate APIs" in ws2.read_text()


# ---------------------------------------------------------------------------
# Orphan cleanup tests
# ---------------------------------------------------------------------------

class TestOrphanCleanup:
    def test_orphan_agent_directories_removed(self, workspace: Path) -> None:
        writer = ClaudeMdWriter(str(workspace))

        # First sync with two agents
        agents = [
            {"name": "agent-a", "agent_type": "custom", "display_name": "A", "system_prompt": "test"},
            {"name": "agent-b", "agent_type": "custom", "display_name": "B", "system_prompt": "test"},
        ]
        writer.sync_agent_directories(agents)
        assert (workspace / "agents" / "agent-a").is_dir()
        assert (workspace / "agents" / "agent-b").is_dir()

        # Second sync without agent-b
        agents_updated = [
            {"name": "agent-a", "agent_type": "custom", "display_name": "A", "system_prompt": "test"},
        ]
        writer.sync_agent_directories(agents_updated)
        assert (workspace / "agents" / "agent-a").is_dir()
        assert not (workspace / "agents" / "agent-b").exists()

    def test_orphan_workstream_directories_removed(self, workspace: Path) -> None:
        writer = ClaudeMdWriter(str(workspace))

        # First sync with two workstreams
        workstreams = [
            {"name": "Project Alpha"},
            {"name": "Project Beta"},
        ]
        writer.sync_workstream_directories(workstreams)
        assert (workspace / "workstreams" / "project-alpha").is_dir()
        assert (workspace / "workstreams" / "project-beta").is_dir()

        # Second sync without Project Beta
        writer.sync_workstream_directories([{"name": "Project Alpha"}])
        assert (workspace / "workstreams" / "project-alpha").is_dir()
        assert not (workspace / "workstreams" / "project-beta").exists()


# ---------------------------------------------------------------------------
# sync_all end-to-end test
# ---------------------------------------------------------------------------

class TestSyncAll:
    def test_sync_all_creates_everything(self, workspace: Path) -> None:
        writer = ClaudeMdWriter(str(workspace))
        config = {
            "office_name": "Test Office",
            "agents": [
                {"name": "analyst", "agent_type": "system", "display_name": "Analyst"},
                {"name": "auditor", "agent_type": "system", "display_name": "Auditor"},
                {"name": "manager-assistant", "agent_type": "system", "display_name": "Manager Assistant"},
                {"name": "automation-script-developer", "agent_type": "system", "display_name": "Automation Script Developer"},
                {
                    "name": "dev",
                    "agent_type": "custom",
                    "display_name": "Developer",
                    "system_prompt": "You code.",
                    "skills": [],
                },
            ],
            "workstreams": [
                {
                    "name": "Main Project",
                    "priority": "high",
                    "description": "The main project",
                    "goals": "Ship it",
                },
            ],
        }
        writer.sync_all(config)

        # Office CLAUDE.md
        office_md = workspace / "CLAUDE.md"
        assert office_md.exists()
        assert "Test Office" in office_md.read_text()

        # Agent directories
        assert (workspace / "agents" / "analyst" / "CLAUDE.md").exists()
        assert (workspace / "agents" / "auditor" / "CLAUDE.md").exists()
        assert (workspace / "agents" / "manager-assistant" / "CLAUDE.md").exists()
        assert (workspace / "agents" / "automation-script-developer" / "CLAUDE.md").exists()
        assert (workspace / "agents" / "dev" / "CLAUDE.md").exists()

        # System agents have constant content
        analyst_content = (workspace / "agents" / "analyst" / "CLAUDE.md").read_text()
        assert analyst_content == ANALYST_CLAUDE_MD

        # Custom agent has generated content
        dev_content = (workspace / "agents" / "dev" / "CLAUDE.md").read_text()
        assert "# Developer" in dev_content
        assert "You code." in dev_content

        # Workstream directory
        ws_md = workspace / "workstreams" / "main-project" / "CLAUDE.md"
        assert ws_md.exists()
        assert "Main Project" in ws_md.read_text()
        assert "Ship it" in ws_md.read_text()

    def test_sync_all_with_empty_config(self, workspace: Path) -> None:
        writer = ClaudeMdWriter(str(workspace))
        writer.sync_all({})

        assert (workspace / "CLAUDE.md").exists()
        assert (workspace / "agents").is_dir()
        assert (workspace / "workstreams").is_dir()


# ---------------------------------------------------------------------------
# W5-P3: prompt-content invariants (audit-driven, regression-only)
# ---------------------------------------------------------------------------


class TestManagerSelfCheckChecklist:
    """W5-P3-H1: the Manager's self-check used to be a single dense
    paragraph with three OR-joined conditions. Models parse a
    numbered checklist more reliably. Pin the structured shape so
    a future "tighten the wording" pass doesn't regress to prose."""

    def test_self_check_is_a_numbered_checklist(self) -> None:
        # Look for the four numbered steps that frame the rule.
        for marker in (
            "Self-check, every turn",
            "1. Am I about to call",
            "2. Does my reply contain the deliverable",
            "3. Am I reading files to",
            "4. If all three answer",
        ):
            assert marker in MANAGER_CLAUDE_MD, (
                f"Manager self-check missing checklist marker: {marker!r}"
            )

    def test_self_check_keeps_the_planning_only_clause(self) -> None:
        """The planning-context-only clause is the load-bearing
        constraint — the checklist reframe must keep it."""
        assert "planning context only" in MANAGER_CLAUDE_MD.lower()
        for tool in ("Read", "Glob", "Grep", "WebSearch", "WebFetch"):
            assert f"`{tool}`" in MANAGER_CLAUDE_MD, (
                f"Manager planning-tools list missing {tool}"
            )

    def test_self_check_names_the_escalation_path(self) -> None:
        """A "yes" answer must point at the next concrete action,
        not just say STOP — otherwise the model has nowhere to go."""
        text = MANAGER_CLAUDE_MD.lower()
        assert "create a task" in text
        assert "tell the user" in text


class TestWorkerBlockedTaskTemplate:
    """W5-P3-H2: the shared worker playbook used to say "describe what
    you tried" in free-form prose, but the Manager Assistant playbook
    expects ``details.blocker_class`` plus the ``ESCALATED (...)``
    template. Workers had no way to learn the template — every block
    came out unstructured. Pin the template + enum in the shared
    playbook so all worker roles emit MA-compatible blocks."""

    def test_blocker_template_present_verbatim(self) -> None:
        assert "ESCALATED (<blocker_class>):" in SHARED_AGENT_WORK_RULES
        for line in (
            "Original error:",
            "What I was trying to do:",
            "What I already tried:",
            "What's needed to resume:",
        ):
            assert line in SHARED_AGENT_WORK_RULES, (
                f"Blocked-task template missing line: {line!r}"
            )

    def test_full_blocker_class_enum_documented(self) -> None:
        """All 8 enum values from worker-spec.md MUST appear in the
        playbook table — the MA branches on these strings, so a
        missing one means workers can't generate the routing
        signal."""
        for klass in (
            "auth_failed",
            "missing_credential",
            "permission_denied",
            "missing_data",
            "ambiguous_spec",
            "broken_dependency",
            "external_outage",
            "unknown",
        ):
            assert f"`{klass}`" in SHARED_AGENT_WORK_RULES, (
                f"blocker_class enum value missing: {klass!r}"
            )

    def test_blocker_path_still_routes_via_update_status(self) -> None:
        """The structured template must be carried by the
        ``update_status`` call's ``comment`` field — NOT split into
        a separate ``question`` checkpoint that would dead-letter
        the routing signal."""
        text = SHARED_AGENT_WORK_RULES
        assert "mcp__cubicle-tools__update_status" in text
        assert "Do NOT post a separate `question` checkpoint" in text

    def test_blocker_path_does_not_promise_self_unblock(self) -> None:
        """Per the blocked-bounce-cap invariant: workers do NOT
        come back to the task on their own."""
        assert "do NOT come back to this task on your own" in SHARED_AGENT_WORK_RULES.lower() \
            or "do not come back to this task on your own" in SHARED_AGENT_WORK_RULES.lower()


class TestNoEmojiInPriorityHints:
    """W5-P3-H4 cross-check: no priority-emoji glyphs leak into the
    Manager / Manager-Assistant playbooks either (the worker_prompt
    hint table is covered separately in test_worker_prompt.py)."""

    def test_no_priority_emoji_in_manager_claude_md(self) -> None:
        for emoji in ("🔥", "🟠", "🟢", "⚪", "🔴", "🟡"):
            assert emoji not in MANAGER_CLAUDE_MD, (
                f"MANAGER_CLAUDE_MD still carries priority emoji {emoji!r}"
            )

    def test_no_priority_emoji_in_ma_claude_md(self) -> None:
        for emoji in ("🔥", "🟠", "🟢", "⚪", "🔴", "🟡"):
            assert emoji not in MANAGER_ASSISTANT_CLAUDE_MD, (
                f"MANAGER_ASSISTANT_CLAUDE_MD still carries priority emoji "
                f"{emoji!r}"
            )
