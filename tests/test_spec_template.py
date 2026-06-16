"""Unit tests for the Phase 10 workstream spec template + REQ-ID lint.

Pins the load-bearing facts of the spec convention:
  - the skeleton carries all seven canonical sections,
  - the rendered skeleton stays within the token budget,
  - convention paths mirror the workstream CLAUDE.md location,
  - the append-only REQ/FLOW id lint catches gaps and duplicates.
"""
from __future__ import annotations

from src.config_sync.claude_md_templates._spec_template import (
    OFFICE_SPECS_DIR,
    SPEC_SECTION_HEADINGS,
    WORKSTREAM_SPEC_TEMPLATE,
    lint_req_ids,
    office_spec_path,
    render_spec_template,
    workstream_spec_path,
)


def _approx_tokens(text: str) -> int:
    """Rough token estimate (chars/4 — the standard ballpark)."""
    return len(text) // 4


def test_template_has_all_seven_sections() -> None:
    rendered = render_spec_template("Auth Project")
    for heading in SPEC_SECTION_HEADINGS:
        assert f"## {heading}" in rendered, f"missing section: {heading}"
    assert len(SPEC_SECTION_HEADINGS) == 7


def test_template_carries_req_and_flow_structure() -> None:
    assert "REQ-1" in WORKSTREAM_SPEC_TEMPLATE
    assert "FLOW-1" in WORKSTREAM_SPEC_TEMPLATE
    assert "_acceptance:_" in WORKSTREAM_SPEC_TEMPLATE
    # Status table is the REQ → status coverage surface.
    assert "| REQ | Status | Notes |" in WORKSTREAM_SPEC_TEMPLATE


def test_template_states_authority_order() -> None:
    # The behavior authority chain must be stated in the spec itself.
    assert "platform rules >" in WORKSTREAM_SPEC_TEMPLATE
    assert "task brief" in WORKSTREAM_SPEC_TEMPLATE


def test_rendered_skeleton_within_token_budget() -> None:
    # The empty skeleton is a small fraction of the ≤2k-token filled-spec
    # target, leaving the Planner headroom to fill it.
    rendered = render_spec_template("Some Reasonably Named Workstream")
    assert _approx_tokens(rendered) <= 2000


def test_render_fills_title_and_revision() -> None:
    rendered = render_spec_template("Recruitment Drive", revision=3)
    assert "# Spec: Recruitment Drive" in rendered
    assert "**Revision:** 3" in rendered
    assert "`draft`" in rendered


def test_blank_title_degrades_gracefully() -> None:
    rendered = render_spec_template("   ")
    assert "Untitled Workstream" in rendered


def test_workstream_spec_path_mirrors_claude_md_location() -> None:
    # Same /workspace/workstreams/<slug>/ dir as the workstream CLAUDE.md,
    # so the worker STEP 0.0 read picks both up from one place.
    assert workstream_spec_path("Auth Project") == (
        "/workspace/workstreams/auth-project/spec.md"
    )


def test_office_spec_path_under_office_specs_dir() -> None:
    assert OFFICE_SPECS_DIR == "/workspace/specs/office"
    assert office_spec_path("Data Model") == (
        "/workspace/specs/office/data-model.md"
    )


# ---- REQ-ID lint ---------------------------------------------------------


def test_lint_clean_sequential_ids() -> None:
    content = "- **REQ-1** a\n- **REQ-2** b\n- **REQ-3** c\n- **FLOW-1** x\n"
    assert lint_req_ids(content) == []


def test_lint_flags_gap() -> None:
    content = "- **REQ-1** a\n- **REQ-3** c\n"  # REQ-2 missing
    problems = lint_req_ids(content)
    assert any("REQ-2 is missing" in p for p in problems)


def test_lint_flags_duplicate() -> None:
    content = "- **REQ-1** a\n- **REQ-1** dup\n- **REQ-2** b\n"
    problems = lint_req_ids(content)
    assert any("REQ-1 appears more than once" in p for p in problems)


def test_lint_flags_not_starting_at_one() -> None:
    content = "- **REQ-2** a\n- **REQ-3** b\n"
    problems = lint_req_ids(content)
    assert any("must start at 1" in p for p in problems)


def test_lint_empty_content_is_clean() -> None:
    assert lint_req_ids("no ids here") == []


def test_template_skeleton_lints_clean() -> None:
    # The shipped skeleton uses REQ-1/REQ-2 and FLOW-1 — must be clean so a
    # freshly-rendered spec doesn't start life flagged.
    assert lint_req_ids(WORKSTREAM_SPEC_TEMPLATE) == []
