"""Skill discovery: bounded raw heads in the helper, parsing on the host (F04).

The container helper (``python3 -I -S``, no PyYAML) must not parse SKILL.md
frontmatter. It returns bounded raw heads; the daemon host enriches them with
the shared skill-metadata contract and must never fail the listing.
"""

from __future__ import annotations

import pytest

from src import skill_discovery
from src._agent_image import secure_files as files


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    (root / ".claude" / "skills").mkdir(parents=True)
    return root


def listing(root):
    return files.execute({"action": "fs_list_skills", "params": {}}, root)


def test_helper_returns_raw_heads_without_parsing(workspace):
    folder = workspace / ".claude/skills/folded"
    folder.mkdir()
    text = "---\nname: Folded\ndescription: >\n  Line one.\n  Line two.\n---\nBody\n"
    (folder / "SKILL.md").write_text(text)
    skill = listing(workspace)["skills"][0]
    assert skill["skill_md_head"] == text
    assert skill["skill_md_head_truncated"] is False
    assert skill["skill_md_size"] == len(text)
    # Legacy keys stay present but neutral: the helper no longer guesses.
    assert skill["display_name"] == "folded"
    assert skill["description"] == ""


def test_oversized_skill_md_no_longer_hides_the_listing(workspace, monkeypatch):
    monkeypatch.setattr(files, "MAX_READ_BYTES", 1024)
    big = workspace / ".claude/skills/big"
    big.mkdir()
    head = "---\nname: Big\ndescription: A large playbook. Use when needed.\n---\n"
    (big / "SKILL.md").write_text(head + "x" * 200_000)
    small = workspace / ".claude/skills/small"
    small.mkdir()
    (small / "SKILL.md").write_text("---\nname: Small\n---\n")
    skills = {s["name"]: s for s in listing(workspace)["skills"]}
    assert set(skills) == {"big", "small"}
    assert skills["big"]["skill_md_head_truncated"] is True
    assert len(skills["big"]["skill_md_head"]) == files.SKILL_MD_HEAD_BYTES
    enriched = skill_discovery.enrich_skill(skills["big"])
    assert enriched["description"] == "A large playbook. Use when needed."
    assert enriched["metadata"]["head_truncated"] is True


def test_head_budget_exhaustion_is_reported_not_fatal(workspace, monkeypatch):
    # The budget counts JSON-escaped bytes (SEC3): "---\nname:" escapes to
    # exactly 10 bytes (the newline is two), so the next space does not fit.
    # The raw-byte budget this pin used to encode let heads outgrow the
    # response limit.
    monkeypatch.setattr(files, "SKILL_HEADS_TOTAL_BYTES", 10)
    for name in ("alpha", "beta"):
        folder = workspace / ".claude/skills" / name
        folder.mkdir()
        (folder / "SKILL.md").write_text("---\nname: X\n---\nbody\n")
    skills = {s["name"]: s for s in listing(workspace)["skills"]}
    assert skills["alpha"]["skill_md_head"] == "---\nname:"
    assert skills["alpha"]["skill_md_head_truncated"] is True
    assert skills["beta"]["skill_md_head"] is None
    enriched = skill_discovery.enrich_skill(skills["beta"])
    assert enriched["metadata"]["status"] == "not_evaluated"
    # alpha's truncated head never closes its frontmatter: nothing from it
    # may be shown as the description.
    alpha = skill_discovery.enrich_skill(skills["alpha"])
    assert alpha["metadata"]["status"] == "not_evaluated"
    assert "does not fit" in alpha["metadata"]["reason"]
    assert alpha["display_name"] == skills["alpha"]["display_name"]
    assert "skill_md_head" not in alpha


def test_folder_without_skill_md_is_not_evaluated():
    enriched = skill_discovery.enrich_skill(
        {
            "name": "draft",
            "display_name": "x",
            "description": "y",
            "files": [],
            "has_skill_md": False,
            "skill_md_head": None,
            "skill_md_head_truncated": False,
            "skill_md_size": None,
        }
    )
    assert enriched["metadata"]["status"] == "not_evaluated"
    assert enriched["metadata"]["reason"] == "no SKILL.md"
    assert enriched["display_name"] == "draft"
    assert enriched["description"] == ""
    assert "skill_md_head" not in enriched


def test_non_dict_entries_are_skipped_and_non_list_passes_through():
    ok = {
        "name": "ok",
        "has_skill_md": True,
        "files": [],
        "skill_md_head": "---\nname: OK\n---\n",
        "skill_md_head_truncated": False,
    }
    result = skill_discovery.enrich_discovered_listing(
        {"skills": ["junk", None, 3, ok]}
    )
    assert [skill["name"] for skill in result["skills"]] == ["ok"]
    odd = {"skills": "nope"}
    assert skill_discovery.enrich_discovered_listing(odd) is odd


def test_escaped_heads_never_outgrow_the_response_limit(workspace):
    """SEC3 repro: 80 skills whose SKILL.md is 40 KiB of 0xFF. Each raw byte
    decodes to U+FFFD and escapes to 6 bytes, so the raw 4 MiB budget produced
    ~15.7 MB of JSON — over the 12 MiB limit — and the whole office's
    discovery answered 413. The escaped budget keeps the listing whole."""
    import json

    for index in range(80):
        folder = workspace / ".claude/skills" / f"skill-{index:02d}"
        folder.mkdir()
        (folder / "SKILL.md").write_bytes(b"\xff" * (40 * 1024))
    result = listing(workspace)
    assert "error" not in result, result.get("error")
    encoded = json.dumps(result, ensure_ascii=True).encode()
    assert len(encoded) <= files.MAX_RESPONSE_BYTES
    heads = [skill["skill_md_head"] for skill in result["skills"]]
    assert len(heads) == 80
    escaped_total = sum(
        len(json.dumps(head, ensure_ascii=True)) - 2 for head in heads if head
    )
    assert escaped_total <= files.SKILL_HEADS_TOTAL_BYTES
    assert heads[0] is not None and heads[-1] is None
    enriched = skill_discovery.enrich_skill(result["skills"][-1])
    assert enriched["metadata"]["status"] == "not_evaluated"


def test_empty_skill_md_is_an_empty_head_not_unevaluated(workspace):
    folder = workspace / ".claude/skills" / "blank"
    folder.mkdir()
    (folder / "SKILL.md").write_bytes(b"")
    skill = listing(workspace)["skills"][0]
    assert skill["skill_md_head"] == ""
    assert skill["skill_md_head_truncated"] is False


def test_whole_heads_are_kept_when_the_budget_allows(workspace):
    text = "---\nname: Ünïcode\ndescription: Emoji ✓ 🚀. Use when needed.\n---\n"
    folder = workspace / ".claude/skills" / "unicode"
    folder.mkdir()
    (folder / "SKILL.md").write_text(text)
    skill = listing(workspace)["skills"][0]
    assert skill["skill_md_head"] == text
    assert skill["skill_md_head_truncated"] is False


def test_folded_description_reaches_the_listing_in_full():
    entry = {
        "name": "report",
        "display_name": "report",
        "description": "",
        "files": [],
        "has_skill_md": True,
        "skill_md_head": (
            "---\nname: Weekly report\ndescription: >-\n  Builds the weekly report."
            "\n  Use when: the Friday digest is due.\n---\n"
        ),
        "skill_md_head_truncated": False,
        "skill_md_size": 120,
    }
    enriched = skill_discovery.enrich_skill(entry)
    assert enriched["display_name"] == "Weekly report"
    assert enriched["description"] == (
        "Builds the weekly report. Use when: the Friday digest is due."
    )
    assert "skill_md_head" not in enriched


def test_invalid_yaml_shows_what_claude_code_loads():
    entry = {
        "name": "broken",
        "has_skill_md": True,
        "files": [],
        "skill_md_head": "---\ndescription: Use when: the user asks\n---\n# Heading line\n",
        "skill_md_head_truncated": False,
    }
    enriched = skill_discovery.enrich_skill(entry)
    assert enriched["metadata"]["status"] == "invalid_yaml"
    # Claude Code loads NO fields: the label is the directory, the
    # description falls back to the first body line.
    assert enriched["display_name"] == "broken"
    assert enriched["metadata"]["description_source"] == "body_first_line"


def test_old_image_output_is_marked_not_evaluated_and_kept():
    legacy = {
        "skills": [
            {
                "name": "legacy",
                "display_name": "Legacy Label",
                "description": ">",
                "files": [],
                "has_skill_md": True,
            }
        ]
    }
    skill = skill_discovery.enrich_discovered_listing(legacy)["skills"][0]
    assert skill["display_name"] == "Legacy Label"
    assert skill["metadata"]["status"] == "not_evaluated"
    assert "predates" in skill["metadata"]["reason"]


def test_enrichment_error_never_fails_the_listing(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(skill_discovery.skill_metadata, "parse_skill_md", boom)
    result = skill_discovery.enrich_discovered_listing(
        {
            "skills": [
                {
                    "name": "a",
                    "has_skill_md": True,
                    "files": [],
                    "skill_md_head": "---\nname: a\n---\n",
                    "skill_md_head_truncated": False,
                }
            ]
        }
    )
    skill = result["skills"][0]
    assert skill["metadata"]["status"] == "not_evaluated"
    assert "skill_md_head" not in skill


def test_error_results_pass_through_unchanged():
    error = {"error": "Only regular, single-link files are supported", "status": 400}
    assert skill_discovery.enrich_discovered_listing(error) is error
