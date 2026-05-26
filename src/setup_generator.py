"""AI-powered generation primitives backed by ``docker exec ... claude``.

This module owns the host-side helpers that drive one-shot Claude CLI
invocations inside an office's Docker container for various
"generate something with AI" surfaces. The CLI is already
authenticated in the container, so no credentials reach the backend.

Three flows live here:

* :func:`generate_office_config` + :func:`analyze_office_description`
  — multi-phase setup-wizard generation (instructions, roster,
  per-agent details, skills). Streams progress events to the backend
  via ``router.publish_event`` since the round-trip can take minutes.
* :func:`generate_agent_from_description` — single-shot Agents-page
  "Create with AI" flow. Returns an AgentCreate-shaped draft.
* :func:`generate_workstream_context_note` — single-shot Manager-page
  workstream context-note generator.

Single-shot flows use :func:`_run_chunk` with ``max_retries=0`` so the
wall-clock budget fits in the backend's 180 s RequestBridge timeout
(see ``backend/app/transport/ai_generation.py``). The multi-phase
flow keeps the default 2 retries because each chunk is small and
the streamed progress lets users tolerate the extra wait.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Per-chunk timeout for the docker-exec call. Opus 4.7 with thinking
# is noticeably slower than Sonnet (typical chunk: 30-90s; long agent-
# detail chunks: up to 3 min under load), so we budget generously.
# 360s = 6 min, which still keeps the wizard from hanging silently —
# every phase emits a progress event so a long wait is visible to the
# user and the multi-phase split keeps individual chunks small.

# Default model for ALL setup-wizard Claude CLI calls. The platform
# standard is Opus 4.7 (the latest "thinking" Opus) — ``_model_defaults``
# is the single source of truth so a tier rollout edits one file.
# ``CBCL_GENERATION_MODEL`` env var is an advanced testing override
# (e.g. to validate a new alias before promoting it to the default);
# production operators should leave it unset so they get the platform
# standard.
from .orchestrator._model_defaults import FALLBACK_MANAGER_MODEL  # noqa: E402


# Max retries per chunk for the multi-phase setup-wizard flow. The
# single-shot Agents / Workstream generators override this to 0.

# Standard Claude CLI tool names. Used to filter hallucinated tool
# names out of generated agent configs so the AgentCreate validator
# downstream doesn't choke on, say, "MakeCoffee". MCP tool patterns
# (``mcp__*``) are not in this set — those are added to allowed_tools
# via the dedicated PUT /allowed-mcp-tools endpoint, not by the
# generator.

# Canonical set of system-agent slugs. Sourced from the communicator's
# ``SYSTEM_AGENT_CLAUDE_MD`` (which is the runtime owner of system-agent
# CLAUDE.md content) so a future system-agent rename has ONE source of
# truth on the communicator side. Cross-process mirrors of the same
# truth (``backend/app/agents/service.py:SYSTEM_AGENT_NAMES``) are
# accepted duplication — different process boundary.
from .config_sync.claude_md_content import SYSTEM_AGENT_CLAUDE_MD  # noqa: E402

SYSTEM_AGENT_SLUGS: frozenset[str] = frozenset(SYSTEM_AGENT_CLAUDE_MD)


# ── Wave 4 decomposition: extracted helper modules ────────────────────
# Pure utilities + constants live in sibling modules now. Re-imported
# here so the public surface (`from src.setup_generator import X`)
# keeps working unchanged for every caller in the codebase.
from ._setup_json import (  # noqa: E402, F401
    _extract_first_json_object,
    _parse_json_response,
    _repair_common_json_errors,
    _strip_code_fences,
)
from ._setup_cli import (  # noqa: E402, F401
    _CHUNK_TIMEOUT,
    _DEFAULT_GENERATION_MODEL,
    _MAX_RETRIES,
    _PROBE_MODEL,
    _STANDARD_TOOL_NAMES,
    _empty_cli_output_error,
    _normalize_allowed_tools,
    _probe_claude_works,
    _run_chunk,
    _run_claude_cli,
)
from ._setup_skill_io import (  # noqa: E402, F401
    _slugify_skill_name,
    write_skill_to_workspace,
)
from ._setup_prompts import (  # noqa: E402, F401
    AGENT_DETAIL_PROMPT,
    AGENT_FROM_DESCRIPTION_PROMPT,
    ANALYZE_SYSTEM_PROMPT,
    COHESION_REVIEW_PROMPT,
    INSTRUCTIONS_PROMPT,
    OFFICE_BUILD_FRAMING,
    ROSTER_PROMPT,
    SINGLE_SKILL_PROMPT,
    SKILLS_PROMPT,
    STANDALONE_SKILL_PROMPT,
    SYNTHESIZE_VISION_PROMPT,
    WORKSTREAM_CONTEXT_PROMPT,
    _AGENT_OUTPUT_CONTRACT,
    _SKILL_BASE_RULES,
    _SKILL_JSON_OUTPUT_SHAPE,
    _SKILL_MD_TEMPLATE_BLOCK,
    _build_user_prompt,
    _build_vision_user_prompt,
    _format_catalog_for_prompt,
)


# Known-good probe model used by ``_run_claude_cli`` to disambiguate
# "model unavailable" from "auth broken" when the configured model
# returns empty. Same dated alias the cbcl-setup auth check uses
# (``verify_claude_in_container``) — proven to resolve on every
# account tier that has a working Claude CLI install.


# ---------------------------------------------------------------------------
# Shared framing — every downstream prompt opens with this paragraph so
# the model treats its slice as part of a coherent virtual-office build,
# not as a one-shot JSON extraction. Centralised so the framing only has
# to be edited in ONE place when we tune the office-creation north-star.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Shared agent-output contract
# ---------------------------------------------------------------------------
#
# Single source of truth for the ``system_prompt`` + ``claude_md_content``
# spec used by BOTH agent-generation flows (wizard Phase 3 +
# Agents page "Create with AI"). The writer
# (``config_sync/claude_md_writer.py``) composes the final CLAUDE.md
# from: agent.system_prompt → skills/connectors sections →
# SHARED_AGENT_WORK_RULES → generic Completion block → agent's
# claude_md_content (under "## Office-Specific Notes"). The AI must
# focus on ROLE-SPECIFIC, OFFICE-SPECIFIC enrichment that complements
# the baseline rather than duplicating it.


# ---------------------------------------------------------------------------
# Phase 1.5: Office Vision Synthesis
# ---------------------------------------------------------------------------
#
# Runs at the end of analyze_office_description (after the 4 per-field
# extraction calls), produces a tight 200-word vision doc that becomes
# the SPINE for every downstream generation phase. Without this the
# instructions / roster / agent-detail prompts each saw a different
# slice (raw user description, analyzed requirements, partial roster)
# and quietly produced incompatible interpretations of the office.


# ---------------------------------------------------------------------------
# Phase 5 (new): Cohesion + Gap Review
# ---------------------------------------------------------------------------
#
# Runs as the LAST phase of generate_office_config. Reads the entire
# generated config (instructions + roster + per-agent details + skills)
# and produces a structured assessment the Review step surfaces to the
# user. The user sees confidence + gaps + suggested additions BEFORE
# accepting, so they can opt in to AI-proposed improvements (an extra
# agent, an additional skill, a new workflow) the original description
# missed.


# ---------------------------------------------------------------------------
# System prompts for each phase
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Single-agent generation (Agents page "Create with AI" flow)
# ---------------------------------------------------------------------------


async def generate_agent_from_description(
    container_name: str,
    description: str,
    office_name: str,
    office_description: str | None,
    available_skills: list[dict],
    available_connectors: list[dict],
    skill_catalog: list[dict] | None = None,
) -> dict[str, Any]:
    """Generate a complete AgentCreate-shaped dict from a free-text description.

    Runs ONE Claude CLI call with retries. Returns a dict with the keys
    listed in ``AGENT_FROM_DESCRIPTION_PROMPT`` (slug-only skill /
    connector references — the backend resolves them to UUIDs before
    returning to the UI).

    ``available_skills`` and ``available_connectors`` are
    ``[{"name", "display_name", "description"}]`` lists so the model
    can pick relevant ones without inventing slugs the office doesn't
    have.

    ``skill_catalog`` is the slim catalog metadata from
    :func:`app.skills.templates.get_catalog_for_generator`. The model
    picks template ``id``s into ``skill_template_ids`` — the backend
    installs the picks server-side (idempotent) before returning the
    resolved ``skill_ids`` to the UI.
    """
    skills_block = (
        "\n".join(
            f"- {s['name']}: {s.get('display_name', '')} — "
            f"{s.get('description') or '(no description)'}"
            for s in available_skills
        )
        or "(none — return [])"
    )
    connectors_block = (
        "\n".join(
            f"- {c['name']}: {c.get('display_name', '')} — "
            f"{c.get('description') or '(no description)'}"
            for c in available_connectors
        )
        or "(none — return [])"
    )
    catalog_block = _format_catalog_for_prompt(skill_catalog or [])

    user_prompt = (
        f"Office: {office_name}\n"
        + (f"Office description: {office_description}\n" if office_description else "")
        + "\n## Available skills in this office\n"
        + skills_block
        + "\n\n## Available connectors in this office\n"
        + connectors_block
        + "\n\n" + catalog_block
        + "\n\n## User's request\n"
        + description.strip()
    )

    # Single-shot — no auto-retry. The backend's RequestBridge times
    # out at 180s; a 120s claude + tee overhead fits with margin.
    # If the user wants to retry they click "Generate" again.
    result = await _run_chunk(
        container_name,
        AGENT_FROM_DESCRIPTION_PROMPT,
        user_prompt,
        timeout=_CHUNK_TIMEOUT,
        max_retries=0,
    )

    # Defensive defaults — Claude usually returns everything but the
    # frontend renders the form even on partial output, so unset
    # fields shouldn't crash the user's review screen.
    result.setdefault("avatar_emoji", "\U0001f916")
    result.setdefault("model", _DEFAULT_GENERATION_MODEL)
    result.setdefault("allowed_tools", ["Read", "Write"])
    result.setdefault("skill_names", [])
    result.setdefault("skill_template_ids", [])
    result.setdefault("connector_names", [])
    result.setdefault("system_prompt", "")
    result.setdefault("claude_md_content", "")

    # Validate ``skill_template_ids`` against the catalog ID set so a
    # hallucinated id doesn't reach the install path and 404. We
    # ALSO dedupe template-name picks out of skill_names — if the
    # model picked the same capability twice, the catalog wins.
    valid_template_ids = {t["id"] for t in (skill_catalog or [])}
    template_id_to_name = {t["id"]: t["name"] for t in (skill_catalog or [])}
    catalog_names_picked: set[str] = set()
    if isinstance(result.get("skill_template_ids"), list):
        filtered_tids = [
            tid for tid in result["skill_template_ids"]
            if isinstance(tid, str) and tid in valid_template_ids
        ]
        result["skill_template_ids"] = filtered_tids
        catalog_names_picked = {
            template_id_to_name[tid] for tid in filtered_tids
        }
    else:
        result["skill_template_ids"] = []

    if isinstance(result.get("skill_names"), list):
        result["skill_names"] = [
            s for s in result["skill_names"]
            if isinstance(s, str) and s and s not in catalog_names_picked
        ]
    else:
        result["skill_names"] = []

    result["allowed_tools"] = _normalize_allowed_tools(result.get("allowed_tools"))

    # Defence against the model picking a system-agent slug for a
    # custom agent (would silently break the office at runtime — the
    # accept path creates a duplicate agent_type='custom' row that
    # collides with the existing system row). When this fires the
    # caller gets back a slug derived from display_name instead.
    slug = (result.get("name") or "").strip().lower()
    if slug in SYSTEM_AGENT_SLUGS:
        logger.warning(
            "Custom agent slug %r collides with a system agent — falling "
            "back to a display_name-derived slug", slug,
        )
        display = (result.get("display_name") or "custom-agent").strip()
        fallback = re.sub(r"[^a-z0-9-]+", "-", display.lower()).strip("-")
        result["name"] = fallback or "custom-agent"

    return result


# ---------------------------------------------------------------------------
# Workstream context-note generation (Manager page workstream flow)
# ---------------------------------------------------------------------------


async def generate_workstream_context_note(
    container_name: str,
    workstream_name: str,
    brief: str,
    office_name: str | None = None,
) -> str:
    """Synthesise a polished markdown context note from a free-text brief.

    Returns just the markdown string (the ``context_notes`` field
    from the JSON response). Raises on Claude CLI / parse failure;
    the backend turns that into a 5xx for the UI to surface.
    """
    user_prompt = (
        (f"Office: {office_name}\n" if office_name else "")
        + f"Workstream: {workstream_name}\n\n"
        + "## User's brief (goals, processes, responsibilities, tools)\n"
        + brief.strip()
    )

    # Single-shot — see ``generate_agent_from_description`` for the
    # rationale (one-shot retries are the user's job for this surface).
    result = await _run_chunk(
        container_name,
        WORKSTREAM_CONTEXT_PROMPT,
        user_prompt,
        timeout=_CHUNK_TIMEOUT,
        max_retries=0,
    )
    text = (result.get("context_notes") or "").strip()
    if not text:
        raise RuntimeError(
            "Generator returned empty context_notes — retry or refine the brief."
        )
    return text


# ---------------------------------------------------------------------------
# Shared skill-prompt fragments
# ---------------------------------------------------------------------------
#
# Both the office-wizard's per-skill prompt (SINGLE_SKILL_PROMPT) and the
# standalone Create-Skill-with-AI prompt (STANDALONE_SKILL_PROMPT, below)
# need to agree on (a) what a SKILL.md file looks like, (b) the
# allowed-tools whitelist, and (c) the JSON output shape. Before this
# extraction the two prompts had near-identical 30-line template blocks
# that had already drifted (one said "250-500 words", the other
# "250-600 words") — exactly the kind of silent divergence a shared
# constant prevents.
#
# Keep edits to these constants in ONE place — both prompts compose them.


# Per-skill variant of SKILLS_PROMPT — generates ONE playbook per call.
# Switched to this in 2026-05-22 because the bundled "all skills in one
# call" variant could push past 120s on offices with 5+ custom skills.
# Splitting also gives the UI per-skill progress instead of a long
# silent wait while the model writes 5×500 words.


# ---------------------------------------------------------------------------
# Standalone skill generation (user-driven; not part of the office wizard)
# ---------------------------------------------------------------------------
#
# Sibling to ``generate_agent_from_description`` — same one-shot Claude CLI
# pattern, returns a single skill JSON. Used by the "Create skill with AI"
# entry point on the Skills page, where the user supplies a one-paragraph
# overview and gets back a full SKILL.md playbook + parameter schema.
#
# Distinct from ``SKILLS_PROMPT`` / ``SINGLE_SKILL_PROMPT``: those are
# wired into the office-setup wizard and assume an agent roster + Office
# Vision Brief in the user message. The standalone flow has none of that
# context — just the user's overview. The prompt below stands alone:
# Claude Skill best-practices baked in, no missing-context placeholders.


async def generate_skill_from_overview(
    container_name: str,
    overview: str,
    requested_name: str | None = None,
    requested_display_name: str | None = None,
    office_name: str | None = None,
    office_description: str | None = None,
) -> dict[str, Any]:
    """Generate a complete SKILL.md draft from a one-paragraph overview.

    Returns a dict with the keys listed in STANDALONE_SKILL_PROMPT
    output spec: ``{name, display_name, description, playbook_content,
    parameter_schema}``. The backend calls ``fs_write`` to land the
    SKILL.md in the daemon's workspace, then writes the DB row using
    the remaining fields.

    ``requested_name`` / ``requested_display_name`` come from the
    user's input on the Create Skill dialog — passing them through
    prevents Claude from inventing a slug that conflicts with what
    the user typed. ``office_name`` / ``office_description`` give the
    model just enough context to write a Process step that fits the
    office's domain rather than generic boilerplate.
    """
    parts = []
    if office_name:
        parts.append(f"Office: {office_name}")
    if office_description:
        parts.append(f"Office description: {office_description}")
    if requested_name:
        parts.append(f"User-requested skill slug: {requested_name}")
    if requested_display_name:
        parts.append(
            f"User-requested display name: {requested_display_name}"
        )
    parts.append("")
    parts.append("## User's overview of the skill")
    parts.append(overview.strip())
    user_prompt = "\n".join(parts)

    # Single-shot — matches the agent / workstream-context flows.
    # The user clicks Generate again if they want a retry; auto-retry
    # would double the 180s RequestBridge budget and risk wedging the
    # UI longer than the user can stand.
    result = await _run_chunk(
        container_name,
        STANDALONE_SKILL_PROMPT,
        user_prompt,
        timeout=_CHUNK_TIMEOUT,
        max_retries=0,
    )

    # Defensive defaults. Claude usually returns the full set; falling
    # back rather than 500-ing keeps the operator unblocked when the
    # model omits one optional field.
    result.setdefault("name", (requested_name or "new-skill").strip())
    result.setdefault(
        "display_name",
        (requested_display_name or result["name"]).strip(),
    )
    result.setdefault("description", "")
    result.setdefault("playbook_content", "")
    result.setdefault("parameter_schema", [])

    # Surface the playbook gap explicitly — the backend turns this
    # error into a 502 the user sees as a generic "generation failed"
    # toast, prompting a retry rather than silently creating an
    # empty-playbook skill row.
    if not str(result.get("playbook_content", "")).strip():
        raise RuntimeError(
            "Generator returned an empty SKILL.md — retry, or expand "
            "the overview."
        )

    return result


# ---------------------------------------------------------------------------
# Core CLI runner (unchanged)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Chunked generation
# ---------------------------------------------------------------------------

async def generate_office_config(
    router: object,
    request_id: str,
    office_name: str,
    office_description: str,
    requirements: dict[str, Any],
    skill_catalog: list[dict[str, Any]],
    container_name: str,
) -> None:
    """Generate office configuration in chunks with real-time progress.

    Flow (post-2026-05-23 uplift — vision-anchored, gap-aware):

    1. **Vision** — pulled from ``requirements["vision"]`` if the
       analyze flow synthesised one; otherwise regenerated here from
       the four requirement fields. This is the SPINE every
       downstream phase reads — same vision = consistent output.
    2. **Instructions** — structured office CLAUDE.md materialising
       the vision (Mission, Workflows, Quality Standards,
       Escalation, …). Embeds the catalog so references to tools /
       skills line up with what the office will actually have.
    3. **Roster** — each agent gets BOTH ``skill_template_ids``
       (catalog picks the wizard installs) and ``skill_names``
       (net-new slugs authored in Phase 5). Now also emits
       ``proposed_workstreams`` (1-3 starter workstreams), a
       ``roster_rationale``, and a ``proposed_because`` flag on
       AI-suggested agents the user didn't ask for.
    4. **Agent details (parallel)** — per-agent ``system_prompt`` +
       ``claude_md_content``. Each call sees the vision + the full
       roster context so handoff sections reference REAL teammates.
    5. **Skills (parallel)** — SKILL.md per net-new slug. Each call
       sees the vision + the agents using this skill (so the
       playbook fits their tools and tone).
    6. **Cohesion review** — single Claude call that reads the
       entire generated config and produces a structured assessment
       (coverage, gaps, redundancies, suggested additions). The
       Review step surfaces this so the user can opt into AI-
       proposed improvements BEFORE accepting.

    Returns ``skill_templates_to_install`` so the frontend's accept
    path can fire ``/install-template`` for each one BEFORE creating
    agents. Also returns ``vision``, ``proposed_workstreams``,
    ``roster_rationale``, and ``cohesion_review``.
    """
    try:
        base_context = _build_user_prompt(office_name, office_description, requirements)
        catalog_block = _format_catalog_for_prompt(skill_catalog)

        # ── Phase 0: Office Vision (synthesise OR regenerate) ───────────
        # We use the vision the analyze flow produced when available;
        # if it's missing (older clients, partial failure during
        # analyze, or the user edited requirements and we want a fresh
        # vision) we re-synthesise from the requirements + office
        # description so downstream phases ALWAYS have a coherent
        # anchor. Cost: ~10-20s for the regeneration path.
        #
        # 6-tile UI: vision / instructions / roster / agent details /
        # skills / cohesion. ``total_steps`` is bumped per-phase as
        # the per-agent and per-skill counts become known.
        vision = (requirements.get("vision") or "").strip()
        if not vision:
            await _publish_progress(
                router, request_id,
                message="Synthesising office vision...",
                step_number=1, total_steps=6,
            )
            vision_result = await _run_chunk(
                container_name, SYNTHESIZE_VISION_PROMPT,
                _build_vision_user_prompt(office_name, office_description, requirements),
                timeout=_CHUNK_TIMEOUT, max_retries=1,
            )
            vision = (vision_result.get("vision") or "").strip()
            logger.info(
                "Phase 0 complete: vision regenerated (%d chars)", len(vision),
            )
        else:
            await _publish_progress(
                router, request_id,
                message="Reusing office vision from analyze phase...",
                step_number=1, total_steps=6,
            )
            logger.info(
                "Phase 0 reused vision from analyze (%d chars)", len(vision),
            )

        # The vision block becomes a HEADER every downstream prompt
        # sees, so the model treats it as the spine — not optional
        # decoration. Centralised so the framing only has to be
        # constructed once.
        vision_block = (
            "## Office Vision Brief (your anchor — every choice must "
            "trace back to this)\n\n"
            f"{vision if vision else '(synthesis returned empty — fall back to the analyzed requirements below)'}\n"
        )

        # ── Phase 1: Office Instructions ────────────────────────────────
        await _publish_progress(
            router, request_id,
            message="Generating office instructions...",
            step_number=2, total_steps=6,
        )

        instructions_result = await _run_chunk(
            container_name, INSTRUCTIONS_PROMPT,
            f"{vision_block}\n\n{base_context}\n\n{catalog_block}",
        )
        instructions = instructions_result.get("instructions", "")
        logger.info("Phase 1 complete: instructions (%d chars)", len(instructions))

        # ── Phase 2: Agent Roster (vision-anchored, gap-aware) ──────────
        await _publish_progress(
            router, request_id,
            message="Designing agent roster...",
            step_number=3, total_steps=6,
        )

        roster_result = await _run_chunk(
            container_name, ROSTER_PROMPT,
            f"{vision_block}\n\n{base_context}\n\n## Office Instructions\n{instructions}\n\n{catalog_block}",
        )
        agents = roster_result.get("agents", [])
        proposed_workstreams = roster_result.get("proposed_workstreams", []) or []
        roster_rationale = roster_result.get("roster_rationale", "") or ""

        # Validate template IDs against the actual catalog and dedupe
        # skill_names against the catalog names so the wizard doesn't
        # try to author a SKILL.md the platform already ships.
        valid_template_ids = {t["id"] for t in skill_catalog}
        template_id_to_name = {t["id"]: t["name"] for t in skill_catalog}
        catalog_names = set(template_id_to_name.values())
        all_template_ids: set[str] = set()
        all_skill_names: set[str] = set()
        for a in agents:
            raw_templates = a.get("skill_template_ids") or []
            templates = [
                t for t in raw_templates
                if isinstance(t, str) and t in valid_template_ids
            ]
            a["skill_template_ids"] = templates
            all_template_ids.update(templates)

            # Hidden hazard: the model sometimes lists the same capability
            # in both skill_template_ids AND skill_names. Strip duplicates
            # against catalog NAMES (not ids) since the name is what the
            # accept path uses to link an agent to its skills.
            picked_template_names = {
                template_id_to_name[t] for t in templates
            }
            raw_skill_names = a.get("skill_names") or []
            skill_names = [
                s for s in raw_skill_names
                if isinstance(s, str)
                and s.strip()
                and s not in picked_template_names
                and s not in catalog_names  # safety: don't shadow catalog
            ]
            a["skill_names"] = skill_names
            all_skill_names.update(skill_names)

        agent_count = len(agents)
        skill_count = len(all_skill_names)
        # Hardening pass over the roster: drop entries whose slug
        # collides with a system agent (would silently break the
        # office) and filter ``allowed_tools`` against the canonical
        # set. Both gates use module-level helpers so the same
        # invariants apply to ``generate_agent_from_description``.
        cleaned_agents: list[dict[str, Any]] = []
        for a in agents:
            slug = (a.get("name") or "").strip().lower()
            if not slug or slug in SYSTEM_AGENT_SLUGS:
                logger.warning(
                    "Phase 2: dropping invalid/system-named agent slug %r",
                    slug,
                )
                continue
            a["allowed_tools"] = _normalize_allowed_tools(a.get("allowed_tools"))
            cleaned_agents.append(a)
        agents = cleaned_agents
        agent_count = len(agents)
        skill_count = len(all_skill_names)

        # 4 phases (vision + instructions + roster + cohesion) + N
        # agents + max(1, M) skills. ``max(1, M)`` reserves a step for
        # the skills phase even when the catalog covers everything and
        # we skip the per-skill iteration entirely.
        total_steps = 4 + agent_count + max(1, skill_count)

        logger.info(
            "Phase 2 complete: %d agents (%d template picks, %d new skill slugs)",
            agent_count, len(all_template_ids), len(all_skill_names),
        )

        await _publish_progress(
            router, request_id,
            message=f"Agent roster ready ({agent_count} agents)",
            step_number=3, total_steps=total_steps,
        )

        # Team summary feeds the agent-detail prompt so each agent's
        # CLAUDE.md "Communication & Handoffs" section references real
        # teammates. Now includes per-agent allowed_tools + skill picks
        # so the detail prompt can reason about WHAT each teammate can
        # actually do (not just their role description).
        team_summary_lines: list[str] = []
        for a in agents:
            tools = ", ".join(a.get("allowed_tools", [])) or "(none)"
            skills_summary = ", ".join(
                a.get("skill_template_ids", []) + a.get("skill_names", [])
            ) or "(none)"
            team_summary_lines.append(
                f"- **{a.get('display_name', a['name'])}** "
                f"(`{a['name']}`) — {a.get('role_description', '')}\n"
                f"  Tools: {tools}\n"
                f"  Skills: {skills_summary}"
            )
        team_summary = "\n".join(team_summary_lines)

        # ── Phase 3: Individual Agent Details ───────────────────────────
        #
        # Run every agent's detail-generation Claude call CONCURRENTLY.
        # The earlier sequential loop took (per-chunk ~30-90s × N) which
        # dominated the wizard's wall-clock budget — Opus 4.7 with
        # thinking pushed a six-agent office past the 4-minute mark.
        # Each call hits a separate ``docker exec ... claude --print``
        # subprocess, so they don't share CLI state and the parallelism
        # is safe. Progress events are emitted as each task COMPLETES
        # via ``asyncio.as_completed`` so the UI's step counter still
        # advances live instead of jumping by N at the end. Per-agent
        # failures are isolated: one agent throwing leaves the others'
        # results intact (we re-raise once at the end so the wizard
        # surfaces a failure when ANY agent's details didn't author).

        async def _author_agent_detail(
            agent: dict[str, Any], idx: int,
        ) -> tuple[int, dict[str, Any]]:
            agent_name = agent.get("display_name", agent["name"])
            skill_lines: list[str] = []
            for tid in agent.get("skill_template_ids", []):
                t = next((x for x in skill_catalog if x["id"] == tid), None)
                if t:
                    skill_lines.append(
                        f"- {t['name']} (catalog · {t.get('category', '?')}): "
                        f"{t.get('description', '')}"
                    )
            for sn in agent.get("skill_names", []):
                skill_lines.append(f"- {sn} (custom — authored in next phase)")
            skills_for_agent = "\n".join(skill_lines) or "(none)"

            agent_context = (
                f"{vision_block}\n\n"
                f"Generate system_prompt + claude_md_content for this agent.\n\n"
                f"## This agent\n"
                f"Name: {agent.get('name', '')}\n"
                f"Display Name: {agent_name}\n"
                f"Role: {agent.get('role_description', '')}\n"
                f"Model: {agent.get('model', _DEFAULT_GENERATION_MODEL)}\n"
                f"Allowed tools: {', '.join(agent.get('allowed_tools', []))}\n\n"
                f"## Skills assigned to this agent\n{skills_for_agent}\n\n"
                f"## Office context\nOffice: {office_name}\n"
                f"Office instructions (excerpt):\n{instructions[:1200]}\n\n"
                f"## Full custom roster (use these names in your handoff section)\n"
                f"{team_summary}\n\n"
                f"## Original office requirements (for tone + voice)\n"
                f"{base_context}"
            )

            detail = await _run_chunk(
                container_name, AGENT_DETAIL_PROMPT, agent_context,
            )
            return idx, detail

        # Kick all agents off concurrently. Wrapping each coroutine in
        # ``asyncio.create_task`` immediately schedules it; pass the
        # task list to ``as_completed`` so we can stream a progress
        # event each time one finishes (regardless of order).
        detail_tasks = [
            asyncio.create_task(_author_agent_detail(a, i))
            for i, a in enumerate(agents)
        ]
        completed_count = 0
        first_error: Exception | None = None
        for task in asyncio.as_completed(detail_tasks):
            try:
                idx, detail = await task
            except Exception as exc:  # noqa: BLE001 — preserved + re-raised
                first_error = first_error or exc
                logger.warning(
                    "Phase 3 agent detail failed: %s — continuing", exc,
                )
                completed_count += 1
                continue

            agent = agents[idx]
            agent["system_prompt"] = detail.get("system_prompt", "")
            agent["claude_md_content"] = detail.get("claude_md_content", "")
            completed_count += 1

            # Progress event uses the COMPLETION ORDINAL (not the
            # agent's input index) so the step number marches
            # monotonic 4, 5, 6, … as expected. The +3 offset accounts
            # for the three preceding phases (vision + instructions +
            # roster). The message names the agent that just finished
            # so the UI's tile reflects real progress.
            await _publish_progress(
                router, request_id,
                message=(
                    f"Creating agent {completed_count}/{agent_count}: "
                    f"{agent.get('display_name', agent['name'])}..."
                ),
                step_number=3 + completed_count, total_steps=total_steps,
            )
            logger.info(
                "Phase 3 [%d/%d]: agent '%s' complete",
                completed_count, agent_count, agent["name"],
            )

        if first_error is not None:
            # One or more agents failed. The wizard's contract is
            # "all-or-nothing" — accepting a config with empty
            # system_prompt / claude_md_content would crash the
            # accept path. Bubble the first exception so the caller's
            # try/except publishes a setup_generation_failed event.
            raise first_error

        # ── Phase 4: Net-new Skills (one Claude call PER skill) ─────────
        #
        # Per-skill iteration replaces the old all-skills-in-one-call
        # variant. Why: a single SKILLS_PROMPT response with 5+ playbooks
        # × 500 words each could push past the chunk timeout. Per-skill
        # calls keep each response tiny (~500 words) and give the UI a
        # per-slug progress message ("Authoring skill 3/5: claims-triage")
        # instead of a long silent wait. The old SKILLS_PROMPT is kept
        # around for callers that want the legacy single-shot shape
        # (no live callers today, but the prompt is still exported).
        skills: list[dict[str, Any]] = []
        sorted_slugs = sorted(all_skill_names)

        if sorted_slugs:
            # Phase 4 mirrors Phase 3's concurrency pattern. Each skill
            # gets its own Claude CLI subprocess (separate ``docker exec``
            # call, no shared state) so running them in parallel is safe
            # and turns a 5-skill office from ~5×60s sequential into
            # ~60s wall-clock. ``asyncio.as_completed`` streams a progress
            # event per finished skill so the UI's counter still
            # advances live instead of jumping by N at the end.
            # Per-skill failures are tolerated (Review step lets the
            # user add/edit skills before accept) — we log + skip
            # the failed slug; the rest still ship.

            async def _author_skill(slug: str) -> tuple[str, dict[str, Any]]:
                # The using-agents context now includes each agent's
                # allowed_tools + role so the playbook's
                # ``allowed-tools`` frontmatter only lists what the
                # using agents actually have (a playbook calling for
                # Bash when none of its using agents has Bash is a
                # defect the new prompt rule rejects).
                using_blocks: list[str] = []
                for a in agents:
                    if slug not in a.get("skill_names", []):
                        continue
                    tools = ", ".join(a.get("allowed_tools", [])) or "(none)"
                    using_blocks.append(
                        f"- **{a.get('display_name', a['name'])}** "
                        f"(`{a['name']}`) — {a.get('role_description', '')}\n"
                        f"  Allowed tools: {tools}"
                    )
                using_section = (
                    "\n".join(using_blocks)
                    if using_blocks
                    else "(no agents currently list this slug — best-effort author)"
                )

                skill_context = (
                    f"{vision_block}\n\n"
                    f"Skill slug: {slug}\n\n"
                    f"## Agents using this skill (their tools constrain "
                    f"your allowed-tools)\n{using_section}\n\n"
                    f"## Office context\nOffice: {office_name}\n"
                    f"Instructions (excerpt):\n{instructions[:1200]}\n\n"
                    f"## Full roster\n{team_summary}\n\n"
                    f"## Original office requirements\n{base_context}\n\n"
                    f"{catalog_block}\n\n"
                    "Remember: do NOT re-author anything already in the "
                    "catalog above. Output ONE skill object — not an array."
                )
                skill_obj = await _run_chunk(
                    container_name, SINGLE_SKILL_PROMPT, skill_context,
                )
                return slug, skill_obj

            skill_tasks = [
                asyncio.create_task(_author_skill(slug)) for slug in sorted_slugs
            ]
            skill_completed = 0
            total_skills = len(sorted_slugs)
            for task in asyncio.as_completed(skill_tasks):
                skill_completed += 1
                try:
                    slug, skill_obj = await task
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Skill generation failed: %s — skipping", exc,
                    )
                    continue

                # Validate the returned object shape + slug match. A
                # legacy SKILLS_PROMPT-style ``{"skills": [...]}`` payload
                # is gracefully unwrapped if the model returns the wrong
                # envelope.
                if "skills" in skill_obj and isinstance(skill_obj["skills"], list):
                    candidates = skill_obj["skills"]
                    skill_obj = candidates[0] if candidates else {}
                returned_slug = skill_obj.get("name") or ""
                if returned_slug != slug:
                    # Model renamed the slug — fix up rather than drop
                    # so the agent-to-skill linkage from Phase 2 still
                    # resolves at accept time.
                    skill_obj["name"] = slug
                skills.append(skill_obj)

                # Step counter uses COMPLETION ORDINAL, same pattern
                # as Phase 3. The +3 offset accounts for vision +
                # instructions + roster phases that already completed.
                await _publish_progress(
                    router, request_id,
                    message=(
                        f"Authoring skill {skill_completed}/{total_skills}: {slug}..."
                    ),
                    step_number=3 + agent_count + skill_completed,
                    total_steps=total_steps,
                )
                logger.info(
                    "Phase 4 [%d/%d]: skill '%s' authored",
                    skill_completed, total_skills, slug,
                )
        else:
            skills_step = 3 + agent_count + 1
            await _publish_progress(
                router, request_id,
                message="Catalog covers all skills — no custom playbooks needed",
                step_number=skills_step, total_steps=total_steps,
            )
            logger.info("Phase 4 skipped: all skill needs covered by catalog")

        # Prune dangling skill references BEFORE the cohesion review
        # runs — otherwise the reviewer grades skills that won't
        # actually ship (Phase 4 silently skips per-skill failures,
        # leaving dangling refs on the agents). The cohesion review
        # should reflect the ACTUAL roster the user will accept.
        authored_slugs = {s.get("name") for s in skills if s.get("name")}
        for agent in agents:
            agent_skill_names = agent.get("skill_names") or []
            agent["skill_names"] = [
                s for s in agent_skill_names if s in authored_slugs
            ]

        # ── Phase 5: Cohesion + Gap review ─────────────────────────────
        # Final pass — reads the whole generated config and produces
        # a structured assessment the Review step surfaces to the user
        # (confidence score, coverage map, gaps, redundancies, AI
        # suggestions). Best-effort: if it fails or returns malformed
        # JSON we ship the office without the review rather than fail
        # the whole wizard. The Review screen handles a missing
        # cohesion_review gracefully.
        cohesion_review: dict[str, Any] | None = None
        cohesion_step = total_steps  # Last step.
        await _publish_progress(
            router, request_id,
            message="Reviewing cohesion + flagging gaps...",
            step_number=cohesion_step, total_steps=total_steps,
        )

        # Build a focused snapshot of the config for the reviewer to
        # read — full claude_md_content per agent + each authored
        # skill's playbook. This payload can be large; we truncate
        # individual sections at sensible bounds so a 12-agent office
        # doesn't push the reviewer's own context window over budget.
        agents_for_review = []
        for a in agents:
            agents_for_review.append(
                f"### {a.get('display_name', a['name'])} (`{a['name']}`)\n"
                f"Role: {a.get('role_description', '')}\n"
                f"Tools: {', '.join(a.get('allowed_tools', []))}\n"
                f"Skills (catalog): {', '.join(a.get('skill_template_ids', []))}\n"
                f"Skills (custom): {', '.join(a.get('skill_names', []))}\n"
                f"\n**system_prompt (excerpt):**\n"
                f"{(a.get('system_prompt') or '')[:600]}\n"
                f"\n**claude_md (excerpt):**\n"
                f"{(a.get('claude_md_content') or '')[:1200]}\n"
            )

        skills_for_review = []
        for s in skills:
            skills_for_review.append(
                f"### {s.get('display_name') or s.get('name')} "
                f"(`{s.get('name')}`)\n"
                f"{(s.get('description') or '').strip()}\n"
                f"Playbook excerpt:\n"
                f"{(s.get('playbook_content') or '')[:600]}\n"
            )

        workstreams_for_review = "\n".join(
            f"- **{w.get('name', '?')}** — {w.get('description', '')} "
            f"(rationale: {w.get('rationale', '?')})"
            for w in proposed_workstreams
        ) or "(none proposed)"

        cohesion_user_prompt = (
            f"{vision_block}\n\n"
            "## Office Instructions (full)\n"
            f"{instructions}\n\n"
            "## Roster rationale (from Phase 2)\n"
            f"{roster_rationale or '(not provided)'}\n\n"
            "## Custom agents\n"
            + ("\n\n".join(agents_for_review) or "(none)") + "\n\n"
            "## Authored custom skills\n"
            + ("\n\n".join(skills_for_review) or "(none — catalog covers everything)") + "\n\n"
            "## Proposed workstreams\n"
            f"{workstreams_for_review}\n"
        )

        try:
            cohesion_review = await _run_chunk(
                container_name, COHESION_REVIEW_PROMPT, cohesion_user_prompt,
                timeout=_CHUNK_TIMEOUT, max_retries=1,
            )
            logger.info(
                "Phase 5 complete: cohesion review (score=%s, gaps=%d, "
                "redundancies=%d, suggestions=%d)",
                cohesion_review.get("confidence_score", "?"),
                len(cohesion_review.get("identified_gaps", []) or []),
                len(cohesion_review.get("redundancies", []) or []),
                len(cohesion_review.get("suggested_additions", []) or []),
            )
        except (json.JSONDecodeError, RuntimeError, TimeoutError) as exc:
            # Cohesion is advisory — never block the wizard on it. Log
            # WARN so it's visible without escalating to an error
            # banner the user has to dismiss. Narrowed from a bare
            # ``Exception`` so structural failures (cancel, SystemExit,
            # KeyboardInterrupt) still propagate.
            logger.warning("Phase 5 cohesion review failed: %s — shipping without it", exc)
            cohesion_review = None

        # ── Assemble final config ───────────────────────────────────────
        # (Dangling skill_names already pruned before the cohesion
        # review so the reviewer graded the actual shipped roster.)
        for agent in agents:
            agent.setdefault("model", _DEFAULT_GENERATION_MODEL)
            agent.setdefault("avatar_emoji", "\U0001f916")
            agent.setdefault("allowed_tools", ["Read", "Write"])
            agent.setdefault("system_prompt", "")
            agent.setdefault("claude_md_content", "")
            agent.setdefault("skill_template_ids", [])
            agent.setdefault("skill_names", [])

        config = {
            "instructions": instructions,
            "agents": agents,
            "skills": skills,
            "skill_templates_to_install": sorted(all_template_ids),
            # New fields surfaced to the Review step's "What the AI
            # noticed" panel + pre-fill the workstream picker.
            "vision": vision,
            "roster_rationale": roster_rationale,
            "proposed_workstreams": proposed_workstreams,
            "cohesion_review": cohesion_review,
        }

        await router.publish_event({
            "type": "setup_generation_complete",
            "request_id": request_id,
            "total_steps": total_steps,
            "config": config,
        })

        logger.info(
            "Office config generated: %d agents, %d new skills, %d catalog installs",
            len(agents), len(skills), len(all_template_ids),
        )

    except Exception as exc:
        logger.error("Config generation failed: %s", exc, exc_info=True)
        await router.publish_event({
            "type": "setup_generation_failed",
            "request_id": request_id,
            "error": str(exc),
        })


# ---------------------------------------------------------------------------
# Description analysis — one Claude call PER field
# ---------------------------------------------------------------------------
#
# The legacy ``ANALYZE_SYSTEM_PROMPT`` asked for all four requirement
# fields (responsibility_areas, desired_agents, workflows,
# additional_context) in a single JSON response. That worked when the
# user's description was short but pushed past the chunk timeout on
# detailed multi-paragraph briefs — and gave no progress feedback
# while we waited. We now run ONE focused prompt per field so each
# call's response is tiny (~150 words of plain text), each completion
# emits a progress event the UI shows live, and a slow field doesn't
# block the others.

_ANALYSIS_FIELD_PROMPTS: dict[str, tuple[str, str]] = {
    # field_key → (display_label, system_prompt)
    "responsibility_areas": (
        "Responsibility areas",
        """\
You extract the RESPONSIBILITY AREAS for an AI office from a free-text
description.

Read the user's description and produce a concise bullet list of the
ongoing areas this office is responsible for. Expand brief mentions
into clear descriptions; cover the long tail the user implied but
didn't spell out.

Output a JSON object exactly like this:

{
  "responsibility_areas": "- Area 1: short description\\n- Area 2: short description\\n- Area 3: short description"
}

The value MUST be a single string with newline-separated bullets.
Output ONLY the JSON. No markdown, no code blocks, no extra text.""",
    ),
    "desired_agents": (
        "Desired agents",
        """\
You extract the DESIRED AGENTS for an AI office from a free-text
description.

Read the user's description and produce a list of specialised AI
agents this office needs. Each agent is one line in the format
``- Name: one-sentence role``. Be specific to the office's domain.
Include 3–6 agents — enough coverage without bloat.

Output a JSON object exactly like this:

{
  "desired_agents": "- Agent Name: one-sentence role\\n- Agent Name: one-sentence role"
}

The value MUST be a single string with newline-separated bullets.
Output ONLY the JSON. No markdown, no code blocks, no extra text.""",
    ),
    "workflows": (
        "Workflows",
        """\
You extract the WORKFLOWS an AI office runs from a free-text
description.

Read the user's description and produce a numbered list of the
end-to-end workflows the team executes. Each line: ``N. step``.
Order matters — start at intake / trigger, end at outcome / delivery.

Output a JSON object exactly like this:

{
  "workflows": "1. First step\\n2. Second step\\n3. Third step"
}

The value MUST be a single string with newline-separated numbered
steps. Output ONLY the JSON. No markdown, no code blocks, no extra
text.""",
    ),
    "additional_context": (
        "Additional context",
        """\
You extract ADDITIONAL CONTEXT for an AI office from a free-text
description: tooling, integrations, team size, target market,
constraints, anything the office should know that isn't a
responsibility area, agent, or workflow.

Produce 2–5 short sentences. If the user didn't mention anything
contextual, return an empty string — don't invent.

Output a JSON object exactly like this:

{
  "additional_context": "Sentence one. Sentence two."
}

The value MUST be a single string. Output ONLY the JSON. No
markdown, no code blocks, no extra text.""",
    ),
}


async def analyze_office_description(
    router: object,
    request_id: str,
    description: str,
    container_name: str,
    office_name: str | None = None,
) -> None:
    """Analyze a free-text office description into structured
    requirements via per-field Claude calls running in parallel,
    followed by an Office Vision synthesis call that ties the four
    fields into a single coherent statement.

    Pipeline (all per-call latency is wall-clock not summed):

    1. Phase 1 — 4 parallel ``_ANALYSIS_FIELD_PROMPTS`` calls extract
       ``responsibility_areas`` / ``desired_agents`` / ``workflows`` /
       ``additional_context``. Tiles flip "done" live as each
       finishes via ``asyncio.as_completed``.
    2. Phase 2 — Single ``SYNTHESIZE_VISION_PROMPT`` call combines
       the four field outputs PLUS the original description into a
       tight ~200-400 word Office Vision. This becomes the SPINE
       every downstream generation phase reads (see
       ``generate_office_config``).

    Publishes ``analyze_description_progress`` events for each phase
    transition so the wizard can render per-field progress instead
    of a single long-running spinner. Final
    ``analyze_description_complete`` carries the assembled
    ``requirements`` dict (which now ALSO contains a ``vision`` key
    consumed by the generate-config flow).

    The 5-tile shape (4 fields + 1 vision) is the source of truth
    for the UI's progress counter.
    """
    requirements: dict[str, str] = {}
    fields = list(_ANALYSIS_FIELD_PROMPTS.items())
    # 5 tiles total: 4 field extractions + 1 vision synthesis. The
    # progress events use 0-indexed step_number so the UI's tile-by-
    # tile state mapping stays consistent with the existing pattern.
    total = len(fields) + 1
    vision_step_index = len(fields)
    vision_field_key = "vision"
    vision_field_label = "Office vision"

    async def _publish_status(
        message: str, step_number: int, current_field: str | None = None,
        current_field_label: str | None = None,
    ) -> None:
        await router.publish_event({
            "type": "analyze_description_progress",
            "request_id": request_id,
            "step_number": step_number,
            "total_steps": total,
            "current_field": current_field,
            "current_field_label": current_field_label,
            "message": message,
            "partial_requirements": dict(requirements),
        })

    try:
        # ── Phase 1: per-field analysis, parallel ──────────────────
        # Each field call runs in its own ``docker exec`` subprocess.
        # ``asyncio.as_completed`` streams progress events as each
        # finishes so the UI's tiles still go "done" live.

        async def _extract_field(
            field_key: str, label: str, prompt: str,
        ) -> tuple[str, str, str]:
            raw_text = await _run_claude_cli(
                container_name, prompt, description,
            )
            parsed = _parse_json_response(raw_text)
            value = parsed.get(field_key, "")
            if not isinstance(value, str):
                # Model returned a list / dict despite the prompt
                # spec — coerce to a string the requirements form
                # can render rather than crashing the whole analysis.
                value = str(value)
            return field_key, label, value

        # Kick off all four extractions concurrently. The initial
        # "analysing…" event lists every field as queued so the UI
        # has something to show before the first response lands.
        await _publish_status(
            message=f"Analysing {len(fields)} requirement fields in parallel…",
            step_number=0,
        )

        field_tasks = [
            asyncio.create_task(_extract_field(key, label, prompt))
            for key, (label, prompt) in fields
        ]
        completed = 0
        for task in asyncio.as_completed(field_tasks):
            field_key, label, value = await task
            requirements[field_key] = value
            completed += 1
            logger.info(
                "Analysis field %d/%d done: %s (%d chars)",
                completed, len(fields), field_key, len(value),
            )
            await _publish_status(
                message=f"Captured {label.lower()} ({completed}/{len(fields)})",
                step_number=completed,
                current_field=field_key,
                current_field_label=label,
            )

        # ── Phase 2: Office Vision synthesis ────────────────────────
        # Single Claude call that reads the original description AND
        # the four field outputs and produces the coherence spine
        # every downstream phase reads. Without this each generation
        # prompt rebuilt its own interpretation of the office and
        # silently drifted from the others.
        await _publish_status(
            message="Synthesising office vision…",
            step_number=vision_step_index,
            current_field=vision_field_key,
            current_field_label=vision_field_label,
        )

        vision_result = await _run_chunk(
            container_name, SYNTHESIZE_VISION_PROMPT,
            _build_vision_user_prompt(office_name or "", description, requirements),
            timeout=_CHUNK_TIMEOUT, max_retries=1,
        )
        vision_text = (vision_result.get("vision") or "").strip()
        if not vision_text:
            # Don't fail the whole analysis on a missing vision — the
            # generate phase regenerates from scratch if needed.
            logger.warning("Vision synthesis returned empty payload")
        else:
            requirements["vision"] = vision_text
            logger.info("Vision synthesised (%d chars)", len(vision_text))

        # Final event — UI flips to ``completed`` and advances to
        # the requirements step with the assembled dict pre-filled.
        await router.publish_event({
            "type": "analyze_description_complete",
            "request_id": request_id,
            "requirements": requirements,
        })
        logger.info("Description analysis complete for request %s", request_id)

    except Exception as exc:
        logger.error("Description analysis failed: %s", exc, exc_info=True)
        await router.publish_event({
            "type": "analyze_description_failed",
            "request_id": request_id,
            "error": str(exc),
            "partial_requirements": requirements,
        })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Trailing comma before a closer: ``{"a": 1,}`` / ``[1, 2, 3,]``.
# Two adjacent object/array values with no separator. Matches
# "} {" / "} [" / "] {" / "] [" / "} \"" / "] \"" patterns where the
# whitespace can include newlines. We only insert a comma — never
# anything that would change the data type.


async def _publish_progress(
    router: object,
    request_id: str,
    message: str,
    step_number: int,
    total_steps: int,
) -> None:
    await router.publish_event({
        "type": "setup_generation_progress",
        "request_id": request_id,
        "message": message,
        "step_number": step_number,
        "total_steps": total_steps,
    })
