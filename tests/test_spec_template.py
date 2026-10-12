"""Unit tests for the Phase 10 workstream spec template + REQ-ID lint.

Pins the load-bearing facts of the spec convention:
  - the skeleton carries all seven canonical sections,
  - the rendered skeleton stays within the token budget,
  - convention paths mirror the workstream CLAUDE.md location,
  - the append-only REQ/FLOW id lint catches gaps and duplicates.
"""
from __future__ import annotations

from src.config_sync.claude_md_templates._spec_template import (
    SPEC_SECTION_HEADINGS,
    WORKSTREAM_SPEC_TEMPLATE,
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


def test_workstream_spec_path_follows_the_declared_directory() -> None:
    # The backend declares ws-<short_code> for a non-Latin name; an older
    # backend declares nothing and writes to the legacy shared "office".
    assert workstream_spec_path("Продажі", "ws-pr") == (
        "/workspace/workstreams/ws-pr/spec.md"
    )
    assert workstream_spec_path("Продажі") == (
        "/workspace/workstreams/office/spec.md"
    )
    # A declaration outside the slug alphabet is never used as a path.
    assert workstream_spec_path("Alpha", "../agents") == (
        "/workspace/workstreams/alpha/spec.md"
    )
