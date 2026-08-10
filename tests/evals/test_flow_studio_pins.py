"""Flow Studio pins — the Manager surface (FS-P2.T9, spec §1.1 / §7).

The Manager's Flow Studio contract is prompt-enforced in two places that
must not drift apart: the classification ladder's FLOW TIER (checked
FIRST — an enabled runnable flow beats every other tier) and the
operate-never-design split (the Manager starts/stops/reads runs; flow
DESIGN belongs to the design surface / the Flow Architect — the agent
lands in P3, so the rule is phrased at the surface level). The consent
posture mirrors ``hire_agent``: the ``run_flow`` card's Run click makes
the BACKEND start the run — never the Manager mid-reply.

Each sentence pinned here is load-bearing routing text; deleting or
paraphrasing it away fails the eval (the test_pivot4_pins posture).
"""
from __future__ import annotations

from src._agent_image._mcp.tools_manager import get_manager_tools
from src.config_sync.claude_md_templates._manager import MANAGER_CLAUDE_MD

_MANAGER_NORM = " ".join(MANAGER_CLAUDE_MD.split())


def _norm(text: str) -> str:
    return " ".join(text.split())


def _tool(name: str) -> dict:
    for tool in get_manager_tools():
        if tool["name"] == name:
            return tool
    raise AssertionError(f"{name} not found in the Manager catalog")


# ---------------------------------------------------------------------------
# The FLOW TIER — exists, and sits BEFORE the tier ladder proper.
# ---------------------------------------------------------------------------


def test_flow_tier_exists_and_precedes_the_tier_ladder() -> None:
    idx_section = MANAGER_CLAUDE_MD.index("## Right-size the work")
    idx_flow_tier = MANAGER_CLAUDE_MD.index(
        "**FLOW TIER — checked FIRST, before every tier below.**"
    )
    idx_tier0 = MANAGER_CLAUDE_MD.index("**Tier 0 — Direct one-shot.**")
    idx_tier3 = MANAGER_CLAUDE_MD.index("**Tier 3 — A program:")
    assert idx_section < idx_flow_tier < idx_tier0 < idx_tier3


def test_flow_tier_routes_through_the_consent_card() -> None:
    assert 'ask_user_choice(kind="run_flow", flow_name="<slug>")' in (
        _MANAGER_NORM
    )
    assert (
        "the user's Run click makes the BACKEND start the run, never you"
        in _MANAGER_NORM
    )
    # Declined / no match → the existing ladder, unchanged.
    assert (
        'Declined ("Not now") or no trigger match → classify on the '
        "ladder below, unchanged." in _MANAGER_NORM
    )


# ---------------------------------------------------------------------------
# Operate, never design.
# ---------------------------------------------------------------------------


def test_flow_runs_section_pins_operate_never_design() -> None:
    assert "## Flow runs — you OPERATE runs, you never design flows" in (
        MANAGER_CLAUDE_MD
    )
    assert "**You NEVER edit flow definitions or graphs.**" in _MANAGER_NORM
    # Forward-compatible phrasing (the Architect ships in P3): design is
    # routed to the design surface, named with the agent.
    assert "the Flow Architect" in _MANAGER_NORM
    assert "**One run per workstream runs at a time**" in _MANAGER_NORM
    assert "**Amendments ride `amend_intake` with `flow_run_id`.**" in (
        _MANAGER_NORM
    )


# ---------------------------------------------------------------------------
# Tool descriptions — the consent + async posture (descriptions are
# prompts; the four-question convention's when-NOT-to-use is the teeth).
# ---------------------------------------------------------------------------


def test_start_flow_run_description_pins_consent_and_async() -> None:
    desc = _norm(_tool("start_flow_run")["description"])
    assert "never start a run the user didn't ask for or consent to" in desc
    assert "the NORMAL path is proposing via ask_user_choice(kind='run_flow')" in desc
    assert "never poll it" in desc


def test_stop_flow_run_description_pins_the_new_run_semantics() -> None:
    desc = _norm(_tool("stop_flow_run")["description"])
    assert "archives the run's open board tasks" in desc
    assert "keeps its manifest" in desc
    assert "stop + start mints a NEW run" in desc


def test_get_flow_run_description_forbids_polling() -> None:
    desc = _norm(_tool("get_flow_run")["description"])
    assert "not a polling target" in desc


def test_define_flow_description_scopes_executes_nothing_to_prose() -> None:
    """Post-Flow-Studio honesty: 'executes nothing' is true only of
    PROSE flows — runnable graph flows execute via the engine, and
    their shape is the Studio's (Flow Architect's) design surface,
    never this tool's."""
    desc = _norm(_tool("define_flow")["description"])
    assert "A PROSE flow guides your routing and intake" in desc
    assert "Runnable flows DO execute" in desc
    assert "designed in the Flow Studio by the Flow Architect" in desc
    # The pre-Studio blanket claim must not resurface.
    assert "A flow guides your routing and intake — it executes nothing" not in desc


def test_update_flow_description_fences_off_runnable_flows() -> None:
    """The Manager playbook's 'never edit a runnable flow's shape'
    rule needs call-time backing — the description is the only
    barrier the model reads at call time."""
    desc = _norm(_tool("update_flow")["description"])
    assert "a RUNNABLE flow's shape" in desc
    assert "the Flow Studio's design surface (the Flow Architect)" in desc
    assert "desyncs it from the graph the engine actually runs" in desc


def test_ask_user_choice_carries_the_run_flow_kind() -> None:
    tool = _tool("ask_user_choice")
    props = tool["inputSchema"]["properties"]
    assert "run_flow" in props["kind"]["enum"]
    assert "flow_name" in props
    assert "derived_preview" in props
    desc = _norm(tool["description"])
    assert "EXACTLY two options keyed 'run' then 'not_now'" in desc
    assert "the user's Run click makes the BACKEND start the run" in desc


# ===========================================================================
# FS-P3.T2 — the two consult-only agent playbooks (Architect / Curator).
# Each pinned sentence is load-bearing behavior text: deleting or
# paraphrasing it away silently removes the behavior it enforces.
# ===========================================================================

from src.config_sync.claude_md_templates._system_agents import (  # noqa: E402
    DATA_CURATOR_CLAUDE_MD,
    FLOW_ARCHITECT_CLAUDE_MD,
    SYSTEM_AGENT_CLAUDE_MD,
)

_ARCHITECT_NORM = " ".join(FLOW_ARCHITECT_CLAUDE_MD.split())
_CURATOR_NORM = " ".join(DATA_CURATOR_CLAUDE_MD.split())


def test_both_playbooks_are_registered_in_the_template_map() -> None:
    # The writer syncs /workspace/agents/<name>/CLAUDE.md from this map —
    # an unregistered playbook is a shipped no-op.
    assert SYSTEM_AGENT_CLAUDE_MD["flow-architect"] is FLOW_ARCHITECT_CLAUDE_MD
    assert SYSTEM_AGENT_CLAUDE_MD["data-curator"] is DATA_CURATOR_CLAUDE_MD
    assert len(SYSTEM_AGENT_CLAUDE_MD) == 8


# ── Flow Architect ──────────────────────────────────────────────────────


def test_architect_pins_the_consent_first_posture() -> None:
    assert (
        "**Consent-first: you never enable a flow yourself.**"
        in _ARCHITECT_NORM
    )
    assert (
        "Your report says the flow is ready to enable; it never says you "
        "enabled it." in _ARCHITECT_NORM
    )


def test_architect_pins_the_extraction_method() -> None:
    # The ordered extraction method: data → documents → routing → report.
    assert "## Extraction method (mode: extract)" in FLOW_ARCHITECT_CLAUDE_MD
    idx_collections = _ARCHITECT_NORM.index("**Extract the collections.**")
    idx_sections = _ARCHITECT_NORM.index("**Extract the section libraries.**")
    idx_routing = _ARCHITECT_NORM.index(
        "**Extract the routing and author the graph.**"
    )
    idx_report = _ARCHITECT_NORM.index(
        "**Report what you extracted, with counts.**"
    )
    assert idx_collections < idx_sections < idx_routing < idx_report
    # The quoter's defining feature: per-row parameter panels.
    assert "model them as a `params_schema` field" in _ARCHITECT_NORM
    # Extraction delivers data, not just shape.
    assert (
        "Populate the rows with `upsert_row` — extraction delivers data, "
        "not just shape." in _ARCHITECT_NORM
    )


def test_architect_pins_the_include_when_v1_caveat() -> None:
    """The v1 engine resolves NO include_when flag (flow_blocks.py
    skips every section carrying one) — both Architect prompt surfaces
    must say so, or the agent ships conditional sections that silently
    never render (the spec-§13 DPA-annex example included)."""
    assert (
        "**V1 caveat: `include_when` is not resolved yet**"
        in _ARCHITECT_NORM
    )
    assert "ALWAYS SKIPPED at generate time" in _ARCHITECT_NORM
    assert "Author sections unconditionally in v1" in _ARCHITECT_NORM

    from src._agent_image._mcp.tools_flow_architect import (
        get_flow_architect_tools,
    )

    for tool in get_flow_architect_tools():
        if tool["name"] == "write_template":
            desc = _norm(tool["description"])
            assert "V1 NEVER RESOLVES IT" in desc
            assert "author sections unconditionally in v1" in desc
            doc_yaml = _norm(
                tool["inputSchema"]["properties"]["doc_yaml"]["description"]
            )
            assert "NOT resolved in v1" in doc_yaml
            return
    raise AssertionError("write_template not in the Architect catalog")


def test_architect_pins_the_block_contract() -> None:
    assert (
        "## The block contract (spec §3 — the 13 block types)"
        in FLOW_ARCHITECT_CLAUDE_MD
    )
    # All 13 type names present (the vocabulary the graphs must use).
    for block_type in ("collect", "select", "gate", "ai", "work",
                      "generate", "action", "if", "switch", "for-each",
                      "parallel", "wait", "call-flow"):
        assert f"`{block_type}`" in FLOW_ARCHITECT_CLAUDE_MD, (
            f"block type {block_type} missing from the Architect playbook"
        )
    assert "**Collection references must be EXACT.**" in _ARCHITECT_NORM


def test_architect_pins_revision_honesty_and_the_one_shot_contract() -> None:
    assert (
        "**Graph edits land as new revisions, never destructive "
        "rewrites**" in _ARCHITECT_NORM
    )
    assert "your report must NAME what changed" in _ARCHITECT_NORM
    assert "**No board writes, no run operations.**" in _ARCHITECT_NORM
    assert (
        "**One-shot session — NEVER end your turn to wait.**"
        in _ARCHITECT_NORM
    )
    assert (
        "ending your turn EXITS the process and kills any still-running "
        "background work" in _ARCHITECT_NORM
    )


# ── Data Curator ────────────────────────────────────────────────────────


def test_curator_pins_archive_dont_delete() -> None:
    assert (
        "**Archive, don't delete — the default recommendation.**"
        in _CURATOR_NORM
    )
    assert (
        "delete only on an explicit directive that names what may be "
        "destroyed" in _CURATOR_NORM
    )


def test_curator_pins_the_ref_refusal() -> None:
    assert (
        "**Refuse deletes that break refs — and SAY which refs.**"
        in _CURATOR_NORM
    )
    assert (
        "name the referencing collection(s) and field(s) in your report"
        in _CURATOR_NORM
    )
    # Collection deletion is REST-only — no delete tool exists in the
    # Curator catalog, so the playbook must instruct a refusal in the
    # REPORT, never a relay of a backend teaching error the agent can
    # never receive (fix-lane finding 5).
    assert (
        "a directive to delete a collection gets a REFUSAL in your report"
        in _CURATOR_NORM
    )
    assert "relay that verbatim" not in _CURATOR_NORM
    assert (
        "a delete-protection refusal NAMES the flows" not in _CURATOR_NORM
    )


def test_curator_pins_additive_migrations_and_rows_live_local() -> None:
    assert "**Migrations are additive first.**" in _CURATOR_NORM
    assert (
        "**Schemas live platform-side; rows live on the user's machine**"
        in _CURATOR_NORM
    )
    assert "## The per-row `params_schema` pattern" not in _CURATOR_NORM  # inline, not a heading
    assert "**The per-row `params_schema` pattern**" in _CURATOR_NORM
    assert (
        "Never bulk-rewrite rows without an explicit directive naming the "
        "transformation." in _CURATOR_NORM
    )


# ── Tool descriptions are prompts (the T5.3.7 review bar) ───────────────


def test_update_flow_graph_description_pins_draft_and_replace_facts() -> None:
    from src._agent_image._mcp.tools_flow_architect import (
        get_flow_architect_tools,
    )

    for tool in get_flow_architect_tools():
        if tool["name"] == "update_flow_graph":
            desc = _norm(tool["description"])
            assert "This tool never enables the flow" in desc
            assert "REPLACES the stored graph wholesale" in desc
            assert "NEW revisions, never destructive rewrites" in desc
            return
    raise AssertionError("update_flow_graph not in the Architect catalog")


def test_delete_row_description_pins_the_archive_default() -> None:
    from src._agent_image._mcp.tools_data_curator import (
        get_data_curator_tools,
    )

    for tool in get_data_curator_tools():
        if tool["name"] == "delete_row":
            desc = _norm(tool["description"])
            assert "archive-don't-delete" in desc
            assert "not silently dropped" in desc
            return
    raise AssertionError("delete_row not in the Curator catalog")
