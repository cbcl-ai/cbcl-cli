"""Authoring entrypoints must not regenerate the retired shared-path rule."""

import re
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src import _setup_prompts as prompts
from src import setup_generator as generator
from src.config_sync.claude_md_templates._manager import MANAGER_CLAUDE_MD
from src.config_sync.claude_md_templates._system_agents import SYSTEM_AGENT_CLAUDE_MD


def assert_current_output_contract(prompt):
    text = " ".join(prompt.split())
    assert "Profile `allowed_tools` describes intended tool use, not an enforced CLI allowlist" in text
    assert "Do not promise read-only execution or resource independence" in text
    assert "current task's supplied output directory" in text
    assert "Without task context, follow an explicitly supplied destination" in text
    assert "/workspace/outputs/{workstream_short_code}/" not in text
    assert "Write the reconciled table to outputs/" not in text


def test_manager_brief_authoring_uses_supplied_script_output():
    invariant = MANAGER_CLAUDE_MD.split("3. **`cubicle.notify_manager()` payload", 1)[
        1
    ].split("\n4. ", 1)[0]
    assert "cubicle.output_dir()" in invariant
    assert "workspace-relative path" in invariant
    assert "/workspace/outputs/" not in invariant


@pytest.mark.parametrize(
    "prompt",
    [
        prompts.AGENT_DETAIL_PROMPT,
        prompts.AGENT_FROM_DESCRIPTION_PROMPT,
        generator.AGENT_INSTRUCTIONS_GEN_PROMPT,
        prompts.SKILLS_PROMPT,  # Kept import-compatible; its pipeline is retired.
        prompts.SINGLE_SKILL_PROMPT,
        prompts.STANDALONE_SKILL_PROMPT,
    ],
    ids=[
        "wizard-profile",
        "new-profile",
        "profile-field",
        "batch-skill",
        "wizard-skill",
        "standalone-skill",
    ],
)
def test_composed_authoring_templates_do_not_override_output_precedence(prompt):
    assert_current_output_contract(prompt)


@pytest.mark.parametrize(
    "entrypoint",
    ["new-profile", "regenerate-field", "improve-field", "standalone-skill"],
)
async def test_live_authoring_entrypoints_deliver_corrected_contract(
    monkeypatch, entrypoint
):
    # Stop only at the model boundary: exercise actual request composition,
    # prompt selection, generated-content preservation and provenance stamping.
    content = "Use the current task's supplied output directory for reports."
    run_chunk = AsyncMock(
        return_value={
            "name": "reporting",
            "content": content,
            "claude_md_content": content,
            "playbook_content": content,
        }
    )
    monkeypatch.setattr(generator, "_run_chunk", run_chunk)
    if entrypoint == "new-profile":
        result = await generator.generate_agent_from_description(
            "isolated-fixture", "Prepare reports", "Fixture", None, [], [], []
        )
        returned_content = result["claude_md_content"]
    elif entrypoint == "standalone-skill":
        result = await generator.generate_skill_from_overview(
            "isolated-fixture", "Prepare reports", requested_name="reporting"
        )
        returned_content = result["playbook_content"]
    else:
        returned_content = await generator.generate_agent_field(
            "isolated-fixture",
            field="claude_md_content",
            directive="Prepare reports",
            mode="improve" if entrypoint == "improve-field" else "regenerate",
            current_value="Preserve the agreed reporting method.",
            office_name="Fixture",
            office_description=None,
            office_instructions="",
            agent_name="reporter",
            role_description="Reports",
            model="opus",
            allowed_tools=["Read", "Write"],
            skill_names=[],
            connector_names=[],
        )
    assert content in returned_content
    run_chunk.assert_awaited_once()
    assert_current_output_contract(run_chunk.await_args.args[1])
    if entrypoint == "improve-field":
        assert "Preserve the agreed reporting method." in run_chunk.await_args.args[2]


@pytest.mark.parametrize(
    "output_dir",
    [
        "/workspace/workstreams/project/tasks/00000000-0000-0000-0000-000000000001",
        "/workspace/outputs/PR/PR-001",
        "/workspace/outputs",
    ],
)
def test_script_playbook_callback_example_uses_actual_runtime_output(
    monkeypatch, output_dir
):
    playbook = SYSTEM_AGENT_CLAUDE_MD["automation-script-developer"]
    callback_section = playbook.split("## Manager callback via", 1)[1].split(
        "## Collections access", 1
    )[0]
    example = re.search(r"```python\n(.*?)\n```", callback_section, re.DOTALL).group(1)
    notify = Mock()
    monkeypatch.setitem(
        sys.modules,
        "cubicle",
        SimpleNamespace(
            output_dir=lambda: output_dir,
            notify_manager=notify,
        ),
    )
    exec(compile(example, "<script-playbook-callback>", "exec"), {})
    assert notify.call_args.kwargs["attachments"] == [
        output_dir.removeprefix("/workspace/") + "/sourced_profiles.json"
    ]
    completion = playbook.split(
        "## Completion (Automation Script Developer-specific)", 1
    )[1]
    assert "non-trivial output file under `cubicle.output_dir()`" in completion
    assert "non-trivial output file in `/workspace/outputs/`" not in completion
