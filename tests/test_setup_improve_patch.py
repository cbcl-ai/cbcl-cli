"""T5.3.5 — improve-config PATCH protocol + merge semantics.

The improve pass now emits a PATCH (only the changed items) which the
orchestrator merges over the current draft, instead of re-emitting the
entire ``GeneratedConfig``. These tests lock the merge semantics:

* a patch with one changed agent + one removed agent applied to a
  multi-agent config yields the correct merged config (the other
  agents are untouched);
* skill add / remove;
* instructions / vision override only when present;
* a legacy full-config response (the pre-T5.3.5 shape) still works.

The prompt-shape assertions guard against drift back to "return the
whole config".
"""
from __future__ import annotations

from src._setup_prompts import IMPROVE_CONFIG_PROMPT
from src.setup_generator import _merge_improve_patch


def _eight_agent_config() -> dict:
    """A multi-agent draft to merge patches against."""
    agents = [
        {
            "name": f"agent-{i}",
            "display_name": f"Agent {i}",
            "model": "sonnet" if i % 2 else "opus",
            "role_description": f"Role {i}",
            "system_prompt": f"Prompt {i}",
            "claude_md_content": f"## Notes {i}",
            "allowed_tools": ["Read"],
            "skill_names": [],
            "skill_template_ids": [],
            "avatar_emoji": "🤖",
        }
        for i in range(8)
    ]
    return {
        "instructions": "Office instructions.",
        "vision": "## Mission\nDo the thing.",
        "agents": agents,
        "skills": [
            {"name": "skill-a", "display_name": "Skill A", "playbook_content": "A"},
            {"name": "skill-b", "display_name": "Skill B", "playbook_content": "B"},
        ],
        "skill_templates_to_install": ["tmpl-1"],
    }


# ── Prompt shape ────────────────────────────────────────────────────


def test_improve_prompt_describes_patch_shape() -> None:
    """The prompt must instruct a patch, not a full-config echo."""
    p = IMPROVE_CONFIG_PROMPT
    for key in (
        "changed_agents",
        "removed_agent_names",
        "changed_skills",
        "removed_skill_names",
    ):
        assert key in p, key
    # The old "return the whole thing" directive must be gone.
    assert "return the whole thing" not in p.lower()
    assert "COMPLETE revised config" not in p
    # It still must mention emitting ONLY changed items.
    assert "ONLY" in p


# ── Merge: change one of eight + remove one ─────────────────────────


def test_patch_changes_one_agent_and_removes_one() -> None:
    cfg = _eight_agent_config()
    patch = {
        "changed_agents": [
            {
                "name": "agent-3",
                "display_name": "Agent 3 (rigorous)",
                "model": "opus",
                "role_description": "Now rigorous.",
                "system_prompt": "Be rigorous.",
                "claude_md_content": "## Notes 3 revised",
                "allowed_tools": ["Read", "Write"],
                "skill_names": [],
                "skill_template_ids": [],
                "avatar_emoji": "🔎",
            }
        ],
        "removed_agent_names": ["agent-5"],
    }

    merged = _merge_improve_patch(cfg, patch)
    names = [a["name"] for a in merged["agents"]]

    # agent-5 removed; the other seven survive; order preserved.
    assert "agent-5" not in names
    assert names == [
        "agent-0", "agent-1", "agent-2", "agent-3",
        "agent-4", "agent-6", "agent-7",
    ]

    # agent-3 was replaced with the patched object.
    a3 = next(a for a in merged["agents"] if a["name"] == "agent-3")
    assert a3["display_name"] == "Agent 3 (rigorous)"
    assert a3["system_prompt"] == "Be rigorous."

    # The untouched agents are byte-for-byte the originals.
    a0 = next(a for a in merged["agents"] if a["name"] == "agent-0")
    assert a0 == cfg["agents"][0]

    # Untouched top-level fields preserved.
    assert merged["instructions"] == "Office instructions."
    assert merged["vision"] == cfg["vision"]
    assert merged["skill_templates_to_install"] == ["tmpl-1"]
    assert len(merged["skills"]) == 2


def test_patch_adds_new_agent_appended_last() -> None:
    cfg = _eight_agent_config()
    patch = {
        "changed_agents": [
            {
                "name": "strategist",
                "display_name": "Content Strategist",
                "model": "opus",
                "role_description": "Strategy.",
                "system_prompt": "Strategise.",
                "claude_md_content": "## Notes",
                "allowed_tools": ["Read"],
                "skill_names": [],
                "skill_template_ids": [],
                "avatar_emoji": "🧭",
            }
        ],
    }
    merged = _merge_improve_patch(cfg, patch)
    names = [a["name"] for a in merged["agents"]]
    assert len(merged["agents"]) == 9
    assert names[-1] == "strategist"


def test_patch_skill_add_and_remove() -> None:
    cfg = _eight_agent_config()
    patch = {
        "changed_skills": [
            {"name": "skill-c", "display_name": "Skill C", "playbook_content": "C"},
            # Replace existing skill-a in place.
            {"name": "skill-a", "display_name": "Skill A v2", "playbook_content": "A2"},
        ],
        "removed_skill_names": ["skill-b"],
    }
    merged = _merge_improve_patch(cfg, patch)
    skill_names = [s["name"] for s in merged["skills"]]
    assert "skill-b" not in skill_names
    assert skill_names == ["skill-a", "skill-c"]
    a = next(s for s in merged["skills"] if s["name"] == "skill-a")
    assert a["display_name"] == "Skill A v2"


def test_patch_instructions_override_only_when_present() -> None:
    cfg = _eight_agent_config()
    # Patch with no instructions key — preserve.
    merged = _merge_improve_patch(cfg, {"removed_agent_names": ["agent-0"]})
    assert merged["instructions"] == "Office instructions."
    # Patch with instructions — override.
    merged2 = _merge_improve_patch(cfg, {"instructions": "New instructions."})
    assert merged2["instructions"] == "New instructions."
    # vision untouched (patch shouldn't normally change it).
    assert merged2["vision"] == cfg["vision"]


def test_patch_does_not_mutate_input_config() -> None:
    cfg = _eight_agent_config()
    original_count = len(cfg["agents"])
    _merge_improve_patch(cfg, {"removed_agent_names": ["agent-0", "agent-1"]})
    # The caller's current_config must be untouched.
    assert len(cfg["agents"]) == original_count


def test_empty_patch_preserves_everything() -> None:
    """A patch touching nothing (model decided no change) is a no-op
    merge that returns the current config intact."""
    cfg = _eight_agent_config()
    merged = _merge_improve_patch(cfg, {"changed_agents": []})
    assert [a["name"] for a in merged["agents"]] == [
        a["name"] for a in cfg["agents"]
    ]
    assert merged["instructions"] == cfg["instructions"]


# ── Legacy full-config fallback ─────────────────────────────────────


def test_legacy_full_config_response_still_accepted() -> None:
    """A GENUINE full-config echo (re-emits the WHOLE roster, no patch keys) is
    still accepted as a wholesale replace. GEN-07: 'full' now means the agents
    list covers the current roster (count >= current, or every current slug),
    not merely 'the response has an agents key'."""
    cfg = _eight_agent_config()
    # Re-emit all 8 slugs (a real full echo), reducing one to a different tier.
    full_agents = [dict(a) for a in cfg["agents"]]
    full_agents[0]["model"] = "haiku"
    legacy = {
        "instructions": "Rewritten whole-config instructions.",
        "agents": full_agents,
        "skills": [],
        "vision": "## Mission\nWhole new vision.",
        "skill_templates_to_install": [],
    }
    merged = _merge_improve_patch(cfg, legacy)
    assert [a["name"] for a in merged["agents"]] == [f"agent-{i}" for i in range(8)]
    assert merged["instructions"] == "Rewritten whole-config instructions."
    assert merged["vision"] == "## Mission\nWhole new vision."
    assert next(a for a in merged["agents"] if a["name"] == "agent-0")["model"] == "haiku"


def test_partial_agents_key_merges_not_replaces() -> None:
    """GEN-07 regression: a half-compliant patch that wrote ``agents`` instead
    of ``changed_agents`` (one agent, no patch keys) must MERGE — updating that
    agent and PRESERVING the rest — never silently blank the roster."""
    cfg = _eight_agent_config()
    patch = {"agents": [{"name": "agent-3", "model": "haiku",
                         "role_description": "updated"}]}
    merged = _merge_improve_patch(cfg, patch)
    assert [a["name"] for a in merged["agents"]] == [f"agent-{i}" for i in range(8)]
    assert next(a for a in merged["agents"] if a["name"] == "agent-3")["model"] == "haiku"


def test_empty_agents_key_preserves_roster() -> None:
    """GEN-07: ``{"agents": []}`` must NOT wipe the roster."""
    cfg = _eight_agent_config()
    merged = _merge_improve_patch(cfg, {"agents": []})
    assert [a["name"] for a in merged["agents"]] == [f"agent-{i}" for i in range(8)]


def test_more_new_agents_than_current_merges_not_replaces() -> None:
    """GEN-07 hardening: a misused patch that lists MORE agents than the current
    roster but does NOT cover the existing slugs (e.g. 3 net-new agents on a
    2-agent roster) must MERGE — a count-based heuristic would have wrongly
    treated it as a full echo and dropped the existing 2."""
    cfg = {
        "agents": [
            {"name": "keep-a", "model": "opus"},
            {"name": "keep-b", "model": "opus"},
        ],
        "skills": [], "instructions": "I", "vision": "V",
    }
    patch = {"agents": [
        {"name": "new-c", "model": "opus", "role_description": "c"},
        {"name": "new-d", "model": "opus", "role_description": "d"},
        {"name": "new-e", "model": "opus", "role_description": "e"},
    ]}
    merged = _merge_improve_patch(cfg, patch)
    names = sorted(a["name"] for a in merged["agents"])
    assert names == ["keep-a", "keep-b", "new-c", "new-d", "new-e"]


def test_full_echo_into_empty_roster_sets_it() -> None:
    """GEN-07: improving a draft that has no agents yet — any ``agents`` list is
    the full config (nothing to preserve)."""
    cfg = {"agents": [], "skills": []}
    merged = _merge_improve_patch(cfg, {"agents": [{"name": "a", "model": "opus"}]})
    assert [a["name"] for a in merged["agents"]] == ["a"]


def test_legacy_full_config_backfills_missing_optional_fields() -> None:
    """A legacy response that drops ``vision`` / ``skills`` backfills
    from the current draft (matches pre-T5.3.5 behaviour)."""
    cfg = _eight_agent_config()
    legacy = {"agents": cfg["agents"]}  # only agents — everything else dropped
    merged = _merge_improve_patch(cfg, legacy)
    assert merged["vision"] == cfg["vision"]
    assert merged["instructions"] == cfg["instructions"]
    assert merged["skills"] == cfg["skills"]
    assert merged["skill_templates_to_install"] == cfg["skill_templates_to_install"]


# ── Malformed responses ─────────────────────────────────────────────


def test_non_dict_response_raises() -> None:
    cfg = _eight_agent_config()
    import pytest

    with pytest.raises(RuntimeError):
        _merge_improve_patch(cfg, ["not", "a", "dict"])


def test_unrecognised_response_raises() -> None:
    """No patch keys AND no ``agents`` — neither shape, refuse."""
    cfg = _eight_agent_config()
    import pytest

    with pytest.raises(RuntimeError):
        _merge_improve_patch(cfg, {"some_random_key": 1})


# --- GEN-01/GEN-03: generated content must carry the provenance sentinel -----

def test_stamp_generated_claude_md_is_idempotent_and_skips_empty():
    from src.setup_generator import _stamp_generated_claude_md
    from src.config_sync.claude_md_writer import (
        GENERATED_CONTENT_SENTINEL,
        _is_generated_content,
    )

    # Empty stays empty (no stray sentinel on a blank field).
    assert _stamp_generated_claude_md("") == ""
    assert _stamp_generated_claude_md(None) == ""

    # Non-empty gets stamped and reads as generated.
    out = _stamp_generated_claude_md("## SOP\nRead the brief first.")
    assert out.startswith(GENERATED_CONTENT_SENTINEL)
    assert _is_generated_content(out)

    # Idempotent — a second stamp does not double the sentinel.
    assert _stamp_generated_claude_md(out) == out
    assert out.count(GENERATED_CONTENT_SENTINEL) == 1
