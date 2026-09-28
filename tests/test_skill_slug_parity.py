"""The daemon and the backend derive the same skill slug (T22, C10).

A generated skill's slug is its folder (``.claude/skills/<slug>/``), the
frontmatter ``name`` and the backend row name, so the daemon's
``skill_slug_of_record`` must follow the backend's
``slug_rules.derived_skill_slug`` exactly: the same character rule (also
the frontend's ``lib/utils.ts`` ``slugify``) and the same 64-character
limit. Both suites run ``tests/fixtures/skill_slug_cases.json``, kept
byte-identical; in a standalone CLI checkout (no ``backend/``) only the
backend comparisons skip.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src._setup_skill_io import (
    SKILL_SLUG_MAX,
    _slugify_skill_name,
    derived_skill_slug,
    resolve_skill_path,
)
from src._setup_skill_render import SkillSlugAllocator, skill_slug_of_record
from tests import backend_boundary

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "skill_slug_cases.json"
DOC = json.loads(FIXTURE.read_text(encoding="utf-8"))
CASES = DOC["cases"]


def _case_id(case: dict) -> str:
    return case["why"]


def test_fixture_limits_match_the_code() -> None:
    assert DOC["max_length"] == SKILL_SLUG_MAX == 64
    assert DOC["fallback"] == "new-skill"


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_daemon_follows_the_shared_rule(case: dict) -> None:
    expected = case["derived"]
    assert derived_skill_slug(case["input"]) == expected
    of_record = expected or DOC["fallback"]
    assert skill_slug_of_record(case["input"]) == of_record
    assert _slugify_skill_name(case["input"]) == of_record
    assert len(of_record) <= SKILL_SLUG_MAX


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_generated_skill_path_uses_the_clamped_slug(case: dict) -> None:
    slug = case["derived"] or DOC["fallback"]
    assert resolve_skill_path({}, case["input"] or "!") == (
        f".claude/skills/{slug}/SKILL.md"
    )


def test_allocator_keeps_long_names_apart_under_the_shared_rule() -> None:
    slugs = SkillSlugAllocator()
    long_name = "Very Long Skill Name " * 8
    first = slugs.slug_for(long_name + "Alpha")
    second = slugs.slug_for(long_name + "Beta")
    assert first == derived_skill_slug(long_name + "Alpha")
    assert second != first and second.endswith("-2")
    assert slugs.resolve("Data.Curator_2") == "datacurator-2"


def test_fixture_copies_are_byte_identical() -> None:
    root = backend_boundary.BACKEND_ROOT
    if not root.is_dir():
        pytest.skip("Private backend is absent from this standalone CLI checkout")
    backend_copy = root / "tests" / "fixtures" / "skill_slug_cases.json"
    digest = hashlib.sha256
    assert digest(FIXTURE.read_bytes()).hexdigest() == (
        digest(backend_copy.read_bytes()).hexdigest()
    ), f"{backend_copy} drifted from {FIXTURE}; copy the communicator file verbatim"


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_backend_rule_matches(case: dict) -> None:
    slug_rules = backend_boundary.import_backend("app.skills.slug_rules")
    assert slug_rules.SKILL_SLUG_MAX == SKILL_SLUG_MAX
    assert slug_rules.derived_skill_slug(case["input"]) == derived_skill_slug(
        case["input"]
    )


def test_allocator_keeps_distinct_names_without_slug_characters_apart():
    """B5-bugs-1: non-Latin names must not all collapse onto one skill."""
    allocator = SkillSlugAllocator()
    names = ["Аналіз ринку", "Звітність", "新技能"]
    slugs = [allocator.slug_for(name) for name in names]
    assert slugs == ["new-skill", "new-skill-2", "new-skill-3"]
    assert [allocator.resolve(name) for name in names] == slugs
    # The same name keeps its slug; whitespace and case do not split it.
    assert allocator.slug_for("  аналіз   РИНКУ ") == "new-skill"
    # A real skill named ``new-skill`` is a different skill.
    assert allocator.slug_for("new-skill") == "new-skill-4"
