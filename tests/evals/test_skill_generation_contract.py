"""Offline pins for the F08 skill-generation contract.

Every skill-authoring prompt (the wizard's per-skill ``SINGLE_SKILL_PROMPT``,
the standalone Create-Skill-with-AI ``STANDALONE_SKILL_PROMPT`` and the
improve pass's net-new-skill bullet in ``IMPROVE_CONFIG_PROMPT``) shares one
contract: the model returns metadata plus a frontmatter-free markdown
``body`` and the platform renders SKILL.md itself. These pins fail if the
retired template (a 250-600 word floor, a MANDATORY section list, an
Anti-Patterns section, triggers hidden in a body "When to Use" section, a
model-written frontmatter block) comes back, or if a required part of the
new contract disappears. Pure string checks — no model calls.
"""

from __future__ import annotations

import pytest

from src import _setup_prompts as prompts


def _flat(text: str) -> str:
    """Collapse whitespace so pins survive re-wrapping."""
    return " ".join(text.split())


SKILL_PROMPTS = {
    "wizard-skill": prompts.SINGLE_SKILL_PROMPT,
    "standalone-skill": prompts.STANDALONE_SKILL_PROMPT,
}

# The retired contract: each phrase appeared in the pre-F08 prompts.
RETIRED_PHRASES = (
    "250-600 words",
    "MANDATORY",
    "## Anti-Patterns",
    "## When to Use",
    "SKILL.md template",
)


@pytest.mark.parametrize("prompt", SKILL_PROMPTS.values(), ids=SKILL_PROMPTS)
def test_skill_prompts_drop_the_retired_template(prompt: str) -> None:
    for phrase in RETIRED_PHRASES:
        assert phrase not in prompt, phrase
    # The model no longer writes the frontmatter or a playbook_content
    # string that carries it.
    assert '"playbook_content"' not in prompt
    assert "playbook_content" not in prompt


@pytest.mark.parametrize("prompt", SKILL_PROMPTS.values(), ids=SKILL_PROMPTS)
def test_skill_prompts_state_the_description_form(prompt: str) -> None:
    text = _flat(prompt)
    assert "third person" in text
    assert "<Does X for Y>. Use when <concrete requests, inputs or situations>." in text
    assert "hard limit 1,024" in text
    assert '"description": "Does X for Y. Use when ..."' in text


@pytest.mark.parametrize("prompt", SKILL_PROMPTS.values(), ids=SKILL_PROMPTS)
def test_skill_prompts_ask_for_the_shortest_complete_body(prompt: str) -> None:
    text = _flat(prompt)
    assert "SHORTEST COMPLETE body" in text
    assert "no minimum length and no mandatory section list" in text
    assert "stay under 800 words and 500 lines" in text
    assert "exact ordered steps for fragile, repeatable mechanics" in text


@pytest.mark.parametrize("prompt", SKILL_PROMPTS.values(), ids=SKILL_PROMPTS)
def test_skill_prompts_return_a_body_without_frontmatter(prompt: str) -> None:
    text = _flat(prompt)
    assert "``body`` is the markdown playbook WITHOUT frontmatter" in text
    assert "the platform writes the frontmatter" in text
    assert "Do NOT start ``body`` with a ``---`` frontmatter block." in text
    assert '"body": "# Skill Display Name' in text


@pytest.mark.parametrize("prompt", SKILL_PROMPTS.values(), ids=SKILL_PROMPTS)
def test_skill_prompts_describe_on_demand_reading_truthfully(prompt: str) -> None:
    """D1: the native Skill tool is disallowed — skills are listed in the
    agent's CLAUDE.md and opened with Read on demand."""
    text = _flat(prompt)
    assert "the agent opens the full SKILL.md with ``Read``" in text
    assert "Nothing loads a skill automatically." in text
    from tests.evals._prompt_composition import skill_autoload_claims

    assert skill_autoload_claims(text) == []  # D1 (T28)


@pytest.mark.parametrize("prompt", SKILL_PROMPTS.values(), ids=SKILL_PROMPTS)
def test_skill_prompts_forbid_secret_parameters(prompt: str) -> None:
    """D2: secret skill parameter values never reach agent sessions."""
    text = _flat(prompt)
    assert "Secret skill parameter values are NOT available to agents" in text
    assert "Never declare a credential or secret parameter." in text
    assert "Office Secret or Connector" in text


def test_improve_prompt_net_new_skill_bullet_uses_the_same_contract() -> None:
    text = _flat(prompts.IMPROVE_CONFIG_PROMPT)
    bullet = text.split("* **Add a skill**", 1)[1].split("* **Remove / adjust", 1)[0]
    assert '"<Does X>. Use when <triggers>."' in bullet
    assert "the shortest complete markdown playbook with NO frontmatter" in bullet
    assert "the platform writes the SKILL.md frontmatter" in bullet
    assert "never a secret" in bullet
    for phrase in RETIRED_PHRASES:
        assert phrase not in prompts.IMPROVE_CONFIG_PROMPT, phrase
    # An adjusted skill carries a new body, not a frontmatter-bearing
    # playbook_content string.
    assert "full new ``body``" in text


def test_retired_batch_skills_prompt_is_gone() -> None:
    assert not hasattr(prompts, "SKILLS_PROMPT")
