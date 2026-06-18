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
import time
import json
import logging
import os
import re
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
from .orchestrator._model_defaults import FALLBACK_WORKER_MODEL  # noqa: E402


# Req #5: the generation AI picks a best-fit tier per custom agent. We
# accept only the three bare family aliases (each resolves to the latest
# model in that tier at run time) and fall back to the platform default
# (opus) on a missing / hallucinated / concrete value. System agents are
# NOT affected — they're seeded by the backend, not the wizard.
_ALLOWED_MODEL_TIERS = frozenset({"opus", "sonnet", "haiku"})


def _normalize_model_tier(value: object) -> str:
    """Validate an AI-chosen model tier; fall back to opus (req #5)."""
    if isinstance(value, str):
        tier = value.strip().lower()
        if tier.endswith("[1m]"):
            tier = tier[:-4].strip()
        if tier in _ALLOWED_MODEL_TIERS:
            return tier
    return FALLBACK_WORKER_MODEL


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
from .config_sync.claude_md_writer import (  # noqa: E402
    GENERATED_CONTENT_SENTINEL,
    _is_generated_content,
)

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
    IMPROVE_CONFIG_PROMPT,
    INSTRUCTIONS_PROMPT,
    OFFICE_BUILD_FRAMING,
    ROSTER_PROMPT,
    SINGLE_SKILL_PROMPT,
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
    # Req #5: honour the tier the AI picked for this agent's role
    # (opus/sonnet/haiku), validated; fall back to opus on a bad/missing
    # value. The bare alias resolves to the latest model in that tier at
    # run time.
    result["model"] = _normalize_model_tier(result.get("model"))
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
# Office-instructions generation (item-1 — Settings → Office Instructions)
# ---------------------------------------------------------------------------

OFFICE_INSTRUCTIONS_PROMPT = """You write the OFFICE INSTRUCTIONS for a Cubicle AI office — a shared CLAUDE.md that guides how the office's AI Manager and worker agents approach ALL work in this office.

Cubicle context: an AI Manager decomposes user requests into tasks (each with a 9-field Task Brief), groups related work into Scopes, and assigns tasks to specialized agents (system agents: Analyst, Automation Script Developer, Auditor, Manager Assistant, Planner; plus the office's custom agents); a designated reviewer closes each task. The instructions you write are read by every agent as standing guidance for this office.

Write the BEST possible instructions for THIS office: authoritative, comprehensive, well-structured Markdown a real operator would be proud of. Do NOT transcribe the user's request verbatim — design the strongest instructions for the office's purpose, filling gaps and improving weak input.

Cover (use clear `##` sections; omit one only if truly irrelevant):
- Mission / Focus Areas — what this office is for and the kinds of work it does.
- Conventions & Working Style — how work should be approached (research-first, scope-first for multi-step work, sensible decomposition, delegating to the right agent).
- Quality Standards — what "good" looks like; review/verification expectations; definition of done.
- Domain Knowledge & Terminology — project-specific terms, references, and context agents must know.
- Constraints & Guardrails — what to avoid; safety/compliance; data-handling rules.
- Team Notes — when to use which agents and how the team collaborates (when the office's purpose calls for it).

Rules:
- Be specific and actionable; no filler or generic platitudes.
- Use Markdown headings + bullet lists. Keep it tight and high-signal, scaled to the office's complexity.
- MODE "improve": refine and extend the CURRENT instructions per the user's request — preserve what's good, fix what's asked, and return the COMPLETE updated document (never a diff).
- MODE "regenerate": produce a fresh, complete set from scratch for the office's purpose + the user's request.

Return ONLY valid JSON, no prose, no code fences:
{"instructions": "<the full Markdown office instructions>"}"""


async def generate_office_instructions(
    container_name: str,
    office_name: str,
    office_description: str | None,
    current_instructions: str,
    directive: str,
    mode: str,
) -> str:
    """Generate (or improve) the office CLAUDE.md from a user directive.

    Returns the markdown ``instructions`` string. Raises on Claude CLI /
    parse failure; the backend turns that into a 5xx for the UI. Runs at
    the platform generation effort (xhigh on Opus) via ``_run_chunk``.
    """
    is_improve = mode == "improve" and bool(current_instructions.strip())
    user_prompt = (
        f"Office: {office_name}\n"
        + (
            f"Office description: {office_description}\n"
            if office_description else ""
        )
        + f"\nMODE: {'improve' if is_improve else 'regenerate'}\n"
        + (
            "\n## Current office instructions (improve these — return the "
            "complete updated document)\n"
            + current_instructions.strip() + "\n"
            if is_improve else ""
        )
        + "\n## User's request\n"
        + directive.strip()
    )
    # Single-shot — see ``generate_agent_from_description`` for the
    # rationale (the user retries by hand on this surface).
    result = await _run_chunk(
        container_name,
        OFFICE_INSTRUCTIONS_PROMPT,
        user_prompt,
        timeout=_CHUNK_TIMEOUT,
        max_retries=0,
    )
    text = (result.get("instructions") or "").strip()
    if not text:
        raise RuntimeError(
            "Generator returned empty instructions — retry or refine the request."
        )
    return text


# ---------------------------------------------------------------------------
# Per-field agent generation (system prompt + agent CLAUDE.md instructions)
# ---------------------------------------------------------------------------
#
# Drives the "Update with AI" buttons on the agent config dialog's System
# Prompt + Agent Instructions surfaces. Same one-shot Claude-CLI + JSON
# contract as the office-instructions generator. Two system prompts (one per
# field); a shared user-prompt builder threads the agent + office context so
# the generated text is coherent with the agent's role, tools, and skills.

AGENT_SYSTEM_PROMPT_GEN_PROMPT = """You write the SYSTEM PROMPT for a single worker agent in a Cubicle AI office.

Cubicle context: an AI Manager decomposes user requests into tasks (each a 9-field Task Brief) and assigns them to specialized agents; each agent runs in its own Claude session, executes the task with its tools, and submits the result for review. The SYSTEM PROMPT you write is the Claude system message for THIS agent — it defines the agent's identity, expertise, working process, and output standards, and is sent on every task this agent runs.

Write the BEST possible system prompt for THIS agent given its role, tools, and the office's purpose: authoritative, specific, and high-signal. Do NOT transcribe the user's request verbatim — design the strongest prompt for the agent's job, filling gaps and improving weak input.

Cover (prose + tight `##` sections as appropriate; omit any that don't apply):
- Identity & expertise — who this agent is and what it's an expert at.
- Process — how it approaches a task from brief to delivery (research-first where relevant; verify before submitting).
- Output standards — what "good" looks like for this agent's deliverables.
- Boundaries — what it should NOT do; when to escalate or propose a task instead.

Rules:
- Be specific and actionable; no filler or generic platitudes. Scale length to the role's complexity (typically 150-500 words).
- Reference the agent's actual tools/skills where it sharpens the guidance; never invent tools it doesn't have.
- MODE "improve": refine the CURRENT system prompt per the user's request — preserve what's good, fix what's asked, return the COMPLETE updated prompt (never a diff).
- MODE "regenerate": produce a fresh, complete system prompt for the agent's role + the user's request.

Return ONLY valid JSON, no prose, no code fences:
{"content": "<the full system prompt>"}"""

AGENT_INSTRUCTIONS_GEN_PROMPT = """You write the OPERATIONAL INSTRUCTIONS (a CLAUDE.md file) for a single worker agent in a Cubicle AI office.

Cubicle context: an AI Manager assigns tasks (each a 9-field Task Brief) to specialized agents; each agent loads its CLAUDE.md at the start of every task as standing operational guidance (distinct from the system prompt: the CLAUDE.md is the agent's project-specific PLAYBOOK — how it works in THIS office, conventions, quality bar, tool/skill usage, deliverable format). The agent already has shared boilerplate (delivery protocol, activity reporting, completion) appended by the platform — write the role/domain-specific guidance, not generic platform rules.

Write the BEST possible instructions for THIS agent given its role, tools, skills, and the office's purpose: authoritative, comprehensive, well-structured Markdown. Do NOT transcribe the user's request verbatim — design the strongest playbook, filling gaps and improving weak input.

Cover (use clear `##` sections; omit one only if truly irrelevant):
- How this agent approaches its work (methodology, research-first / scope-first where relevant).
- Conventions & working style specific to this role and office.
- Quality standards & definition of done for this agent's deliverables.
- Tools & skills — how and when to use the agent's specific tools/skills/connectors (never invent ones it lacks).
- Domain knowledge & terminology the agent must know.

Rules:
- Be specific and actionable; Markdown headings + bullet lists; tight and high-signal, scaled to the role.
- MODE "improve": refine and extend the CURRENT instructions per the user's request — preserve what's good, return the COMPLETE updated document (never a diff).
- MODE "regenerate": produce a fresh, complete playbook for the agent's role + the user's request.

Return ONLY valid JSON, no prose, no code fences:
{"content": "<the full Markdown instructions>"}"""


async def generate_agent_field(
    container_name: str,
    *,
    field: str,
    directive: str,
    mode: str,
    current_value: str,
    office_name: str,
    office_description: str | None,
    office_instructions: str,
    agent_name: str,
    role_description: str,
    model: str,
    allowed_tools: list[str],
    skill_names: list[str],
    connector_names: list[str],
) -> str:
    """Generate (or improve) ONE agent field — ``system_prompt`` or
    ``claude_md_content`` — from a user directive + the agent/office context.

    Returns the generated ``content`` string. Raises on Claude CLI / parse
    failure (the backend maps that to a 5xx). Runs at the platform generation
    effort (xhigh on Opus) via ``_run_chunk``.
    """
    system_prompt = (
        AGENT_SYSTEM_PROMPT_GEN_PROMPT
        if field == "system_prompt"
        else AGENT_INSTRUCTIONS_GEN_PROMPT
    )
    field_label = (
        "system prompt" if field == "system_prompt" else "agent instructions"
    )
    is_improve = mode == "improve" and bool(current_value.strip())

    parts = [f"Office: {office_name}"]
    if office_description:
        parts.append(f"Office description: {office_description}")
    if office_instructions.strip():
        parts.append(
            "Office instructions (context — keep this agent consistent with "
            "them):\n" + office_instructions.strip()
        )
    parts.append("")
    parts.append(f"Agent: {agent_name or '(unnamed)'}")
    if role_description.strip():
        parts.append(f"Agent role: {role_description.strip()}")
    if model:
        parts.append(f"Agent model: {model}")
    if allowed_tools:
        parts.append(f"Agent tools: {', '.join(allowed_tools)}")
    if skill_names:
        parts.append(f"Agent skills: {', '.join(skill_names)}")
    if connector_names:
        parts.append(f"Agent connectors: {', '.join(connector_names)}")
    parts.append("")
    parts.append(f"MODE: {'improve' if is_improve else 'regenerate'}")
    if is_improve:
        parts.append(
            f"\n## Current {field_label} (improve these — return the complete "
            f"updated version)\n" + current_value.strip()
        )
    parts.append("\n## User's request\n" + directive.strip())
    user_prompt = "\n".join(parts)

    result = await _run_chunk(
        container_name,
        system_prompt,
        user_prompt,
        timeout=_CHUNK_TIMEOUT,
        max_retries=0,
    )
    text = (result.get("content") or "").strip()
    if not text:
        raise RuntimeError(
            f"Generator returned empty {field_label} — retry or refine the request."
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

# Top-level keys that mark a model response as a T5.3.5 PATCH rather
# than a legacy full-config echo. If ANY of these is present we treat
# the response as a patch and merge it over the current draft; if NONE
# is present we fall back to the legacy "this IS the full config" path.
_IMPROVE_PATCH_KEYS = frozenset({
    "changed_agents",
    "removed_agent_names",
    "changed_skills",
    "removed_skill_names",
})


def _merge_improve_patch(
    current_config: dict[str, Any],
    response: object,
) -> dict[str, Any]:
    """Merge an improve-pass response over the current draft config.

    T5.3.5 — the improve pass emits a PATCH (only the changed items):

        {
          "instructions"?: str,
          "vision"?: str,
          "changed_agents"?: [<full agent objects>],
          "removed_agent_names"?: [<slug>],
          "changed_skills"?: [<full skill objects>],
          "removed_skill_names"?: [<slug>],
        }

    Agents / skills are keyed by their ``name`` slug: a ``changed_*``
    entry replaces the existing same-slug item or appends when the
    slug is new; a ``removed_*`` slug drops the item. ``instructions``
    / ``vision`` override only when present. Everything the patch
    doesn't mention is preserved verbatim from ``current_config``.

    Legacy fallback: if the response carries NONE of the patch keys
    (it's the pre-T5.3.5 full-config echo, recognised by an ``agents``
    key), it's accepted as the whole config — same behaviour as before
    — so an older / non-compliant model response still works. Missing
    optional fields are backfilled from ``current_config`` either way.

    Raises ``RuntimeError`` on a response that is neither a usable
    patch nor a full config, so the caller surfaces a clean failure
    instead of letting the user accept a half-empty draft.
    """
    if not isinstance(response, dict):
        raise RuntimeError(
            "Improve returned a non-object response. Retry the "
            "improvement with a more specific directive."
        )

    # A legacy full-config echo is recognised by the ``agents`` array —
    # it always re-emits the whole roster. A patch never carries
    # ``agents`` (it uses ``changed_agents`` / ``removed_agent_names``).
    # So: ``agents`` present → legacy; otherwise → patch. The patch keys
    # below (incl. a lone ``instructions`` / ``vision`` override) all
    # take the patch path and merge over the current draft.
    is_legacy_full = "agents" in response
    is_patch = (not is_legacy_full) and bool(
        (_IMPROVE_PATCH_KEYS | {"instructions", "vision"}) & response.keys()
    )

    if not is_patch and not is_legacy_full:
        # Neither shape — the model returned a bare diff or a single
        # unrecognised key. Refuse rather than silently blanking the
        # draft.
        raise RuntimeError(
            "Improve returned a malformed response (no patch keys and "
            "no ``agents`` field). Retry with a more specific directive."
        )

    if not is_patch:
        # ── Legacy full-config path (backwards compatible) ──────────
        # The response IS the config; backfill anything it dropped from
        # the current draft so a missing ``vision`` / ``skills`` doesn't
        # blank the Review screen.
        merged = dict(response)
        for key in ("instructions", "vision", "skill_templates_to_install"):
            if key not in merged:
                merged[key] = current_config.get(key)
        merged.setdefault("skills", current_config.get("skills") or [])
        merged.setdefault("agents", current_config.get("agents") or [])
        return merged

    # ── Patch path (T5.3.5) ─────────────────────────────────────────
    # Start from a copy of the current draft and apply the patch.
    merged: dict[str, Any] = {
        "instructions": current_config.get("instructions"),
        "vision": current_config.get("vision"),
        "skill_templates_to_install": current_config.get(
            "skill_templates_to_install"
        ),
        "agents": [dict(a) for a in (current_config.get("agents") or [])],
        "skills": [dict(s) for s in (current_config.get("skills") or [])],
    }

    # Scalar overrides — only when the patch explicitly carries them.
    if isinstance(response.get("instructions"), str):
        merged["instructions"] = response["instructions"]
    if isinstance(response.get("vision"), str):
        merged["vision"] = response["vision"]

    def _apply(
        items: list[dict[str, Any]],
        changed: object,
        removed: object,
    ) -> list[dict[str, Any]]:
        """Replace-or-append ``changed`` by ``name`` slug, drop ``removed``."""
        by_name: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for it in items:
            slug = (it.get("name") or "").strip()
            if not slug:
                # Keep nameless entries (shouldn't happen) under a
                # synthetic key so they survive the round-trip.
                slug = f"__anon_{len(order)}"
            if slug not in by_name:
                order.append(slug)
            by_name[slug] = it

        if isinstance(changed, list):
            for entry in changed:
                if not isinstance(entry, dict):
                    continue
                slug = (entry.get("name") or "").strip()
                if not slug:
                    continue
                if slug not in by_name:
                    order.append(slug)
                by_name[slug] = entry

        if isinstance(removed, list):
            for slug in removed:
                if not isinstance(slug, str):
                    continue
                slug = slug.strip()
                if slug in by_name:
                    del by_name[slug]
                    order = [s for s in order if s != slug]

        return [by_name[s] for s in order if s in by_name]

    merged["agents"] = _apply(
        merged["agents"],
        response.get("changed_agents"),
        response.get("removed_agent_names"),
    )
    merged["skills"] = _apply(
        merged["skills"],
        response.get("changed_skills"),
        response.get("removed_skill_names"),
    )

    return merged


async def improve_office_config(
    router: object,
    request_id: str,
    office_name: str,
    current_config: dict[str, Any],
    directive: str,
    container_name: str,
) -> None:
    """Apply a user directive to a drafted office config.

    Path-B "Improve with AI": runs ONE Claude call that takes the
    current draft + the user's free-text adjustment and returns a
    revised draft. The user can call this repeatedly (each call is
    a new request_id) until they're happy with the Review screen.

    Publishes ``setup_generation_complete`` / ``setup_generation_failed``
    to the same request_id stream the frontend already polls via
    ``useGenerationStatus`` — no new poll path needed.
    """
    try:
        await _publish_progress(
            router, request_id,
            message="Applying your improvements...",
            step_number=1, total_steps=1,
        )

        # The user message carries the FULL current draft so the
        # model has everything it needs to make a coherent patch
        # without us cherry-picking which parts to send.
        vision = (current_config.get("vision") or "").strip()
        user_prompt = (
            f"## Office\n{office_name}\n\n"
            "## Office Vision (read-only — preserve)\n"
            f"{vision or '(empty — preserve as empty)'}\n\n"
            "## Current Draft Config\n"
            f"```json\n{json.dumps(current_config, indent=2, ensure_ascii=False)}\n```\n\n"
            "## User Directive\n"
            f"{directive.strip()}\n"
        )

        result = await _run_chunk(
            container_name, IMPROVE_CONFIG_PROMPT, user_prompt,
            timeout=_CHUNK_TIMEOUT, max_retries=1,
        )

        # T5.3.5: the improve pass now emits a PATCH (only the changed
        # items) which we merge over ``current_config``. A legacy
        # full-config response (the pre-T5.3.5 shape) is still accepted
        # so nothing breaks if the model ignores the patch instruction.
        result = _merge_improve_patch(current_config, result)

        # Per-agent sanity floor — same as generate_office_config. Req
        # #5: validate the AI's per-agent tier choice (opus/sonnet/haiku).
        # If the merged config omits ``model`` for an existing agent,
        # PRESERVE that agent's current tier (matched by name) rather
        # than silently resetting a deliberate sonnet/haiku choice to
        # opus. Falls back to opus only when neither the merged output
        # nor the prior config has a usable tier.
        prior_models = {
            a.get("name"): a.get("model")
            for a in (current_config.get("agents") or [])
            if a.get("name")
        }
        for agent in result.get("agents", []) or []:
            chosen = agent.get("model") or prior_models.get(agent.get("name"))
            agent["model"] = _normalize_model_tier(chosen)
            agent.setdefault("avatar_emoji", "\U0001f916")
            agent.setdefault("allowed_tools", ["Read", "Write"])
            agent.setdefault("system_prompt", "")
            agent.setdefault("claude_md_content", "")
            agent.setdefault("skill_template_ids", [])
            agent.setdefault("skill_names", [])

        await router.publish_event({
            "type": "setup_generation_complete",
            "request_id": request_id,
            "total_steps": 1,
            "config": result,
        })

        logger.info(
            "Office config improved: directive=%d chars, %d agents, %d skills",
            len(directive),
            len(result.get("agents", []) or []),
            len(result.get("skills", []) or []),
        )

    except Exception as exc:
        logger.error("Config improve failed: %s", exc, exc_info=True)
        await router.publish_event({
            "type": "setup_generation_failed",
            "request_id": request_id,
            "error": str(exc),
        })


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
    3. **Roster** — the complete custom team the mission needs. Each
       agent gets BOTH ``skill_template_ids`` (catalog picks the wizard
       installs) and ``skill_names`` (net-new slugs authored in phase
       4). No workstreams, no rationale, no "proposed" flags — the
       roster is authoritative.
    4. **Agent details + Skills (interleaved, parallel)** — per-agent
       ``system_prompt`` + ``claude_md_content`` AND per-skill
       SKILL.md. Both pools are scheduled concurrently after the
       roster lands because skills only need Phase 2's output
       (slug + allowed_tools + role), not Phase 3's prompts.
       Single ``as_completed`` loop streams progress as each call
       returns; wall-clock collapses to max(longest agent, longest
       skill) instead of the sum of the two phases.

    Returns ``skill_templates_to_install`` so the apply-config path can
    install each one, plus the authoritative ``vision`` brief. NO
    workstreams are produced — those are the user's concern post-setup.
    """
    try:
        base_context = _build_user_prompt(office_name, office_description, requirements)
        catalog_block = _format_catalog_for_prompt(skill_catalog)

        # ── Phase 0: Office Vision (reuse-only, fast regen fallback) ───
        # The analyze flow ALWAYS produces a vision; only the very rare
        # "analyzer failed" path needs regen here. Skipping the regen
        # call when a vision exists saves ~1 min on every wizard run.
        # When regen IS needed, do it FIRST (synchronously) because the
        # downstream phases need it as their anchor.
        vision = (requirements.get("vision") or "").strip()
        if not vision:
            await _publish_progress(
                router, request_id,
                message="Synthesising office vision...",
                step_number=1, total_steps=4,
            )
            heartbeat = asyncio.create_task(_heartbeat_emitter(
                router, request_id,
                message_template="Still synthesising vision... ({elapsed_s}s)",
                step_number=1, total_steps=4,
            ))
            try:
                vision_result = await _run_chunk(
                    container_name, SYNTHESIZE_VISION_PROMPT,
                    _build_vision_user_prompt(office_name, office_description, requirements),
                    timeout=_CHUNK_TIMEOUT, max_retries=1,
                )
                vision = (vision_result.get("vision") or "").strip()
            finally:
                # Await the cancel so a heartbeat mid-publish doesn't
                # emit a stale step_number=1 frame after we've moved on
                # to step 2 (would briefly rewind the progress bar in
                # the UI).
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            logger.info(
                "Phase 0 complete: vision regenerated (%d chars)", len(vision),
            )

        # Emit the vision content RIGHT AWAY so the frontend can render
        # a "What the AI heard" preview while the next phases churn.
        # Without this, the user stared at a spinner for 8+ min with no
        # signal that the AI actually understood their description.
        await _publish_progress(
            router, request_id,
            message="Vision ready — building team & instructions",
            step_number=1, total_steps=4,
            payload={"vision": vision},
        )
        logger.info("Phase 0 emitted: vision (%d chars)", len(vision))

        # The vision block becomes a HEADER every downstream prompt
        # sees, so the model treats it as the spine — not optional
        # decoration. Centralised so the framing only has to be
        # constructed once.
        vision_block = (
            "## Office Vision Brief (your anchor — every choice must "
            "trace back to this)\n\n"
            f"{vision if vision else '(synthesis returned empty — fall back to the analyzed requirements below)'}\n"
        )

        # ── Phase 1 ‖ Phase 2: Instructions + Roster (PARALLEL) ─────────
        #
        # Both calls depend ONLY on vision + requirements + catalog —
        # neither needs the other's output. Previously the wizard ran
        # them sequentially (~3-8 min each, ~12 min combined). The
        # ``asyncio.wait(FIRST_COMPLETED)`` loop below collapses
        # wall-clock to the longer of the two (~8 min for a 6-agent
        # office on Opus) AND emits the faster one's payload to the
        # frontend the moment it lands.
        #
        # The roster prompt originally included an "Office Instructions"
        # context excerpt; dropping that lets us parallelise. The roster
        # prompt is already self-sufficient (vision + requirements +
        # catalog is enough context) and the Review step shows both side
        # by side anyway.
        await _publish_progress(
            router, request_id,
            message="Building instructions + roster in parallel...",
            step_number=2, total_steps=4,
        )

        instructions_user = (
            f"{vision_block}\n\n{base_context}\n\n{catalog_block}"
        )
        roster_user = (
            f"{vision_block}\n\n{base_context}\n\n{catalog_block}"
        )

        instructions_task = asyncio.create_task(_run_chunk(
            container_name, INSTRUCTIONS_PROMPT, instructions_user,
        ), name="phase-1-instructions")
        roster_task = asyncio.create_task(_run_chunk(
            container_name, ROSTER_PROMPT, roster_user,
        ), name="phase-2-roster")

        heartbeat = asyncio.create_task(_heartbeat_emitter(
            router, request_id,
            message_template=(
                "Drafting instructions + roster... ({elapsed_s}s — "
                "Opus thinks before it speaks)"
            ),
            step_number=2, total_steps=4,
            interval_s=15.0,
        ))

        instructions = ""
        agents: list[dict[str, Any]] = []
        pending: set[asyncio.Task] = {instructions_task, roster_task}
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED,
                )
                for completed in done:
                    if completed is instructions_task:
                        instructions_result = completed.result()
                        instructions = instructions_result.get("instructions", "")
                        logger.info(
                            "Phase 1 done: instructions (%d chars)",
                            len(instructions),
                        )
                        await _publish_progress(
                            router, request_id,
                            message="Instructions ready",
                            step_number=2, total_steps=4,
                            payload={"instructions": instructions},
                        )
                    else:  # roster_task
                        roster_result = completed.result()
                        # Defensive ``or []`` — the model occasionally
                        # emits ``"agents": null`` instead of an empty
                        # array, which would crash the downstream
                        # ``for a in agents`` loops.
                        agents = roster_result.get("agents") or []
                        # Emit lightweight roster preview so the UI
                        # shows the team taking shape while skills /
                        # agents still churn downstream. Includes skill
                        # picks so the user sees what each agent will
                        # be equipped with.
                        roster_preview = [
                            {
                                "name": a.get("name", ""),
                                "display_name": a.get("display_name", ""),
                                "avatar_emoji": a.get("avatar_emoji", "🤖"),
                                "role_description": a.get("role_description", ""),
                                "skill_template_ids": a.get(
                                    "skill_template_ids", []
                                ),
                                "skill_names": a.get("skill_names", []),
                            }
                            for a in agents
                        ]
                        logger.info(
                            "Phase 2 done: %d agents in roster", len(agents),
                        )
                        await _publish_progress(
                            router, request_id,
                            message=f"Roster ready — {len(agents)} agents",
                            step_number=2, total_steps=4,
                            payload={"agents": roster_preview},
                        )
        finally:
            # Critical: on exception OR normal completion, cancel any
            # task still in ``pending`` so a doomed wizard run doesn't
            # leak a 6-minute ``docker exec`` subprocess burning
            # Claude API spend. ``return_exceptions=True`` swallows the
            # CancelledError so the original phase-1/2 exception (if
            # any) surfaces to the caller cleanly.
            for stragglers in pending:
                stragglers.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

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
        seen_slugs: set[str] = set()
        for a in agents:
            slug = (a.get("name") or "").strip().lower()
            if not slug or slug in SYSTEM_AGENT_SLUGS:
                logger.warning(
                    "Phase 2: dropping invalid/system-named agent slug %r",
                    slug,
                )
                continue
            if slug in seen_slugs:
                # Two custom agents with the same slug would collide on the
                # backend's UNIQUE(office_id, name) constraint and fail the
                # whole atomic apply. Drop the later duplicate here (the
                # config builder is the right layer) so a model hiccup
                # can't abort an otherwise-good office.
                logger.warning(
                    "Phase 2: dropping duplicate custom agent slug %r", slug,
                )
                continue
            seen_slugs.add(slug)
            a["name"] = slug
            a["allowed_tools"] = _normalize_allowed_tools(a.get("allowed_tools"))
            cleaned_agents.append(a)
        agents = cleaned_agents
        agent_count = len(agents)
        skill_count = len(all_skill_names)

        # 2 fixed phases (vision + parallel instructions/roster) + N
        # agents + max(1, M) skills. ``max(1, M)`` reserves a step for
        # the skills phase even when the catalog covers everything and
        # we skip the per-skill iteration entirely.
        total_steps = 2 + agent_count + max(1, skill_count)

        logger.info(
            "Phase 2 complete: %d agents (%d template picks, %d new skill slugs)",
            agent_count, len(all_template_ids), len(all_skill_names),
        )

        await _publish_progress(
            router, request_id,
            message=f"Agent roster ready ({agent_count} agents)",
            step_number=2, total_steps=total_steps,
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

        # ── Phase 3 + 4 (interleaved, parallel): Agent details + Skills ──
        #
        # Both phases only need Phase 2's output (the roster's slug,
        # allowed_tools, role_description, skill_names). Skills do NOT
        # depend on agent system_prompt / claude_md_content, so the two
        # pools can be scheduled into ONE ``as_completed`` loop. Wall-
        # clock collapses from (agent_phase + skill_phase) to
        # max(longest agent call, longest skill call). On a 7-agent /
        # 14-skill office this is the largest single speedup in the
        # wizard.
        #
        # Each call hits a separate ``docker exec ... claude --print``
        # subprocess so they don't share CLI state and parallelism is
        # safe. Per-agent failures are fatal (raise at the end so the
        # wizard surfaces them); per-skill failures are tolerated
        # (logged + skipped — the Review step lets the user add/edit
        # missing skills before accept).
        skills: list[dict[str, Any]] = []
        sorted_slugs = sorted(all_skill_names)
        total_skills = len(sorted_slugs)

        # Index the catalog once so per-agent template lookups are
        # O(1) instead of O(catalog_size). The catalog can hit 50+
        # entries on prod and each agent's detail call would
        # otherwise re-scan it once per picked template id.
        _catalog_by_id: dict[str, dict] = {
            t["id"]: t for t in skill_catalog
        }

        async def _author_agent_detail(
            agent: dict[str, Any], idx: int,
        ) -> tuple[str, int, dict[str, Any]]:
            agent_name = agent.get("display_name", agent["name"])
            skill_lines: list[str] = []
            for tid in agent.get("skill_template_ids", []):
                t = _catalog_by_id.get(tid)
                if t:
                    skill_lines.append(
                        f"- {t['name']} (catalog · {t.get('category', '?')}): "
                        f"{t.get('description', '')}"
                    )
            for sn in agent.get("skill_names", []):
                skill_lines.append(f"- {sn} (custom — authored in parallel)")
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
            return "agent", idx, detail

        async def _author_skill(slug: str) -> tuple[str, str, dict[str, Any]]:
            # The using-agents context lists each agent's allowed_tools
            # + role so the playbook's ``allowed-tools`` frontmatter only
            # references what the using agents actually have.
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
            return "skill", slug, skill_obj

        # CRITICAL: cap concurrent Claude CLI calls so we don't blow
        # past the Anthropic API tier's concurrent-request limit. The
        # 0.2.45 "parallel" run silently serialized in production
        # because firing 24 concurrent ``docker exec ... claude``
        # subprocesses overran the API tier; the API queued them and
        # the wall-clock looked sequential (6 agents × 40s = 4 min).
        # ``CBCL_WIZARD_PARALLEL_CAP`` (default 6) keeps us under the
        # default Claude Max tier's burst limit so the parallelism
        # actually shows in the wall-clock.
        parallel_cap = int(os.environ.get("CBCL_WIZARD_PARALLEL_CAP", "6"))
        sem = asyncio.Semaphore(max(1, parallel_cap))

        async def _capped_agent_detail(
            agent: dict[str, Any], idx: int,
        ) -> tuple[str, int, dict[str, Any]]:
            async with sem:
                return await _author_agent_detail(agent, idx)

        async def _capped_skill(slug: str) -> tuple[str, str, dict[str, Any]]:
            async with sem:
                return await _author_skill(slug)

        agent_tasks = [
            asyncio.create_task(_capped_agent_detail(a, i))
            for i, a in enumerate(agents)
        ]
        skill_tasks = [
            asyncio.create_task(_capped_skill(slug))
            for slug in sorted_slugs
        ]
        all_tasks = agent_tasks + skill_tasks

        # Sentinel sets used by the fail-fast cancel path AND the
        # ``_safe_await`` kind classifier. MUST be defined BEFORE
        # ``_safe_await`` is created because the closure resolves
        # ``agent_task_set`` lazily and tests / refactors could
        # otherwise hit a NameError at call time.
        agent_task_set = set(agent_tasks)
        skill_task_set = set(skill_tasks)

        completed_count = 0
        agent_completed = 0
        skill_completed = 0
        skill_failed = 0
        first_agent_error: Exception | None = None

        # Wrap each task so its exception is captured alongside its
        # kind discriminator. Without this the bare ``except`` below
        # couldn't distinguish "agent failed" (fatal) from "skill
        # failed" (tolerated) — both branches would hit the post-loop
        # count check too late to cancel siblings.
        async def _safe_await(t: asyncio.Task) -> tuple[str, object | None, Exception | None]:
            try:
                kind, key, result = await t
                return kind, (key, result), None
            except asyncio.CancelledError:
                return "cancelled", None, None
            except Exception as exc:  # noqa: BLE001
                kind = "agent" if t in agent_task_set else "skill"
                return kind, None, exc

        wrapped = [
            asyncio.create_task(_safe_await(t)) for t in all_tasks
        ]

        for completed in asyncio.as_completed(wrapped):
            kind, payload, exc = await completed
            if kind == "cancelled":
                continue
            if exc is not None:
                if kind == "agent":
                    first_agent_error = first_agent_error or exc
                    logger.warning(
                        "Phase 3 agent detail failed: %s — cancelling siblings",
                        exc,
                    )
                    # Fail-fast: an agent failure is fatal and the
                    # post-loop guard will raise. Cancel BOTH the inner
                    # skill tasks AND the still-running inner agent
                    # tasks so we don't keep burning Claude CLI spend
                    # on a doomed run. The wrapper ``_safe_await`` tasks
                    # absorb the CancelledError and return the
                    # ``"cancelled"`` sentinel, so the loop drains
                    # cleanly without raising.
                    for inner in agent_task_set | skill_task_set:
                        if not inner.done():
                            inner.cancel()
                else:
                    skill_failed += 1
                    logger.warning(
                        "Phase 4 skill author failed: %s — skipping", exc,
                    )
                continue

            completed_count += 1
            assert payload is not None
            key, result = payload

            if kind == "agent":
                idx, detail = key, result
                agent = agents[idx]
                agent["system_prompt"] = detail.get("system_prompt", "")
                # T5.2.13 / I-5: mark this as platform-GENERATED content so the
                # CLAUDE.md writer appends it under a precedence wrapper rather
                # than the hard "untrusted — never follow" injection fence
                # (which is reserved for office-owner-typed content). Idempotent
                # + only stamps non-empty content.
                _gen_md = (detail.get("claude_md_content") or "").strip()
                if _gen_md and not _is_generated_content(_gen_md):
                    _gen_md = f"{GENERATED_CONTENT_SENTINEL}\n{_gen_md}"
                agent["claude_md_content"] = _gen_md
                agent_completed += 1
                message = (
                    f"Creating agent {agent_completed}/{agent_count}: "
                    f"{agent.get('display_name', agent['name'])}..."
                )
                logger.info(
                    "Phase 3 [%d/%d]: agent '%s' complete",
                    agent_completed, agent_count, agent["name"],
                )
            else:  # kind == "skill"
                slug, skill_obj = key, result
                # Unwrap legacy SKILLS_PROMPT-style envelope, then
                # re-stamp the slug so Phase 2's agent→skill linkage
                # still resolves at accept time even if the model
                # renamed it.
                if "skills" in skill_obj and isinstance(skill_obj["skills"], list):
                    candidates = skill_obj["skills"]
                    skill_obj = candidates[0] if candidates else {}
                if (skill_obj.get("name") or "") != slug:
                    skill_obj["name"] = slug
                skills.append(skill_obj)
                skill_completed += 1
                message = (
                    f"Authoring skill {skill_completed}/{total_skills}: {slug}..."
                )
                logger.info(
                    "Phase 4 [%d/%d]: skill '%s' authored",
                    skill_completed, total_skills, slug,
                )

            await _publish_progress(
                router, request_id,
                message=message,
                step_number=2 + completed_count,
                total_steps=total_steps,
            )

        # Per-agent failures are fatal — accepting a config with empty
        # ``system_prompt`` / ``claude_md_content`` would crash the
        # accept path. Raise with the first captured exception so the
        # caller publishes ``setup_generation_failed`` with the real
        # underlying error.
        if agent_completed < agent_count:
            if first_agent_error is not None:
                raise first_agent_error
            raise RuntimeError(
                f"Agent detail generation incomplete: "
                f"{agent_completed}/{agent_count} authored. "
                "See WARNING logs above for the failing agents."
            )

        # Surface partial skill failures so ops can spot recurring slugs
        # that need a prompt tweak. The wizard still ships (skills are
        # editable on the Review step), but a silent skip would let
        # systematic failures go unnoticed.
        if skill_failed > 0:
            logger.warning(
                "Phase 4 partial failure: %d/%d skills failed — affected slugs were pruned from agent rosters",
                skill_failed, total_skills,
            )

        if not sorted_slugs:
            # Emit a synthetic completion event so the UI's skills tile
            # lights up + advances even when the catalog covers every
            # slug and no per-skill calls fired. The message MUST start
            # with "Authoring skill" so ``looksLikeSkill`` in the
            # frontend's GeneratingStep matches and the matching tile
            # flips active → done. Without the prefix, the tile would
            # stay "pending" the entire run.
            await _publish_progress(
                router, request_id,
                message="Authoring skill 0/0: catalog covers all needs",
                step_number=2 + agent_count + 1,
                total_steps=total_steps,
            )
            logger.info("Phase 4 skipped: all skill needs covered by catalog")

        # Prune dangling skill references — Phase 4 silently skips
        # per-skill failures, leaving agents with skill_names that
        # don't resolve to any authored playbook. Drop them so the
        # accept path doesn't try to assign a non-existent skill.
        authored_slugs = {s.get("name") for s in skills if s.get("name")}
        for agent in agents:
            agent_skill_names = agent.get("skill_names") or []
            agent["skill_names"] = [
                s for s in agent_skill_names if s in authored_slugs
            ]

        # ── Assemble final config ───────────────────────────────────────
        for agent in agents:
            # Req #5: the roster prompt now asks the AI to pick a best-fit
            # tier (opus/sonnet/haiku) per agent. Validate it and fall
            # back to opus on a bad/missing value. The bare alias resolves
            # to the latest model in that tier at run time. (System agents
            # are seeded by the backend, not here, so they stay pinned to
            # opus regardless.)
            agent["model"] = _normalize_model_tier(agent.get("model"))
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
            # Authoritative design brief — shown read-only on the Review
            # step as a "What we're building" summary. Not a suggestion.
            "vision": vision,
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
            # Route through _run_chunk (not _run_claude_cli directly) so the
            # per-field analysis runs at the platform generation effort
            # (xhigh on Opus) AND inherits the --effort graceful-degrade for
            # older container CLIs. max_retries=0 keeps this interactive
            # wizard step snappy — matching the prior no-retry behaviour.
            parsed = await _run_chunk(
                container_name, prompt, description, max_retries=0,
            )
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
    payload: dict[str, Any] | None = None,
) -> None:
    """Publish a wizard progress event.

    ``payload`` carries optional structured content the frontend can
    surface live (vision text, the agent roster, office instructions)
    instead of just rendering a spinner. Backwards compatible — older
    frontends ignore the field.
    """
    event: dict[str, Any] = {
        "type": "setup_generation_progress",
        "request_id": request_id,
        "message": message,
        "step_number": step_number,
        "total_steps": total_steps,
    }
    if payload:
        event["payload"] = payload
    await router.publish_event(event)


async def _heartbeat_emitter(
    router: object,
    request_id: str,
    message_template: str,
    step_number: int,
    total_steps: int,
    interval_s: float = 12.0,
) -> None:
    """Emit a 'still thinking' event every ``interval_s`` so the UI
    has live signal during multi-minute Claude calls.

    ``message_template`` must include ``{elapsed_s}`` — replaced with
    the integer seconds since the heartbeat started. Cancellable;
    designed to be torn down via ``asyncio.create_task`` + ``cancel()``
    by the calling phase the moment the underlying work completes.
    """
    started = time.monotonic()
    try:
        while True:
            await asyncio.sleep(interval_s)
            elapsed = int(time.monotonic() - started)
            try:
                await _publish_progress(
                    router, request_id,
                    message=message_template.format(elapsed_s=elapsed),
                    step_number=step_number,
                    total_steps=total_steps,
                )
            except Exception:  # noqa: BLE001
                # Router teardown / WS drop during shutdown — let the
                # caller's cancel deal with the rest.
                return
    except asyncio.CancelledError:
        pass
