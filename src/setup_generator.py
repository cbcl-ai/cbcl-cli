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
_CHUNK_TIMEOUT = 360

# Default model for ALL setup-wizard Claude CLI calls. The platform
# standard is Opus 4.7 (the latest "thinking" Opus) — ``_model_defaults``
# is the single source of truth so a tier rollout edits one file.
# ``CBCL_GENERATION_MODEL`` env var is an advanced testing override
# (e.g. to validate a new alias before promoting it to the default);
# production operators should leave it unset so they get the platform
# standard.
from .orchestrator._model_defaults import FALLBACK_MANAGER_MODEL  # noqa: E402

_DEFAULT_GENERATION_MODEL = (
    os.environ.get("CBCL_GENERATION_MODEL", "").strip()
    or FALLBACK_MANAGER_MODEL
)

# Max retries per chunk for the multi-phase setup-wizard flow. The
# single-shot Agents / Workstream generators override this to 0.
_MAX_RETRIES = 2

# Standard Claude CLI tool names. Used to filter hallucinated tool
# names out of generated agent configs so the AgentCreate validator
# downstream doesn't choke on, say, "MakeCoffee". MCP tool patterns
# (``mcp__*``) are not in this set — those are added to allowed_tools
# via the dedicated PUT /allowed-mcp-tools endpoint, not by the
# generator.
_STANDARD_TOOL_NAMES = frozenset(
    {"Read", "Write", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"}
)

# Canonical set of system-agent slugs. Sourced from the communicator's
# ``SYSTEM_AGENT_CLAUDE_MD`` (which is the runtime owner of system-agent
# CLAUDE.md content) so a future system-agent rename has ONE source of
# truth on the communicator side. Cross-process mirrors of the same
# truth (``backend/app/agents/service.py:SYSTEM_AGENT_NAMES``) are
# accepted duplication — different process boundary.
from .config_sync.claude_md_content import SYSTEM_AGENT_CLAUDE_MD  # noqa: E402

SYSTEM_AGENT_SLUGS: frozenset[str] = frozenset(SYSTEM_AGENT_CLAUDE_MD)


def _empty_cli_output_error(
    *,
    model: str = "",
    stderr: str = "",
    container_name: str = "",
    probe_succeeded: bool | None = None,
) -> RuntimeError:
    """Shared error for the "Claude CLI produced no output" failure.

    Two distinct root causes the message disambiguates between:
      * ``probe_succeeded=True`` — a haiku probe DID get a response,
        so auth + CLI are fine; the configured ``model`` is the
        problem (not in this account's plan, or CLI too old to
        recognise the alias). Suggests trying the exact model.
      * ``probe_succeeded=False`` — even haiku came back empty, so
        auth itself is the issue. Suggests ``cbcl auth``.
      * ``probe_succeeded=None`` — no probe was run. Falls back to
        the generic both-causes message.
    """
    if probe_succeeded is True:
        # The configured model fails but a haiku probe works → auth
        # is fine but the container's Claude CLI can't resolve the
        # alias (CLI version too old to know it, or transient API
        # rejection). Suggest rebuilding the agent image to refresh
        # the CLI.
        msg = (
            f"Claude CLI returned empty output for model "
            f"``{model or '<unknown>'}``. The container's auth is "
            "fine (a haiku probe succeeded) — most likely the "
            "container's bundled Claude CLI is too old to recognise "
            "the model alias. Rebuild the agent image with "
            "`cbcl setup --force-rebuild-image` (or pull the latest "
            "image manually). Verify with: "
        )
        if container_name:
            msg += (
                f"`docker exec {container_name} claude --print "
                f"-p hello --model {model or '<alias>'}`"
            )
        else:
            msg += (
                f"`claude --print -p hello --model "
                f"{model or '<alias>'}` inside the office container"
            )
    elif probe_succeeded is False:
        # Auth-itself failure (even haiku empty).
        msg = (
            "Claude CLI returned empty output AND a fallback haiku "
            "probe also came back empty. The office container's "
            "Claude auth is most likely missing or expired — run "
            "`cbcl auth` to re-authenticate."
        )
    else:
        # Probe timed out / docker error — can't disambiguate.
        msg = (
            "Claude CLI returned empty output"
            + (f" for model ``{model}``" if model else "")
            + ". A haiku probe didn't complete cleanly so we can't "
            "tell yet whether this is auth or a model-alias issue. "
            "Try `cbcl auth` first; if that doesn't help, rebuild "
            "the agent image with `cbcl setup --force-rebuild-image`."
        )
    if stderr:
        msg += f" stderr: {stderr}"
    return RuntimeError(msg)


# Known-good probe model used by ``_run_claude_cli`` to disambiguate
# "model unavailable" from "auth broken" when the configured model
# returns empty. Same dated alias the cbcl-setup auth check uses
# (``verify_claude_in_container``) — proven to resolve on every
# account tier that has a working Claude CLI install.
_PROBE_MODEL = "claude-haiku-4-5-20251001"


def _probe_claude_works(container_name: str) -> bool | None:
    """Run a 5s haiku probe to test if the container's Claude works
    at all. Returns True if the probe got a non-empty response,
    False if it also came back empty (auth is broken), None on
    timeout / docker error (can't tell either way).

    Cheap (haiku, single token) so safe to call from the
    empty-output diagnostic path.
    """
    try:
        result = subprocess.run(
            [
                "docker", "exec", container_name,
                "claude", "--print",
                "-p", "ok",
                "--output-format", "text",
                "--model", _PROBE_MODEL,
                "--max-turns", "1",
                "--permission-mode", "bypassPermissions",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return False
        return bool(result.stdout.strip())
    except Exception:
        return None


def _normalize_allowed_tools(raw: Any) -> list[str]:
    """Filter a raw `allowed_tools` value against the standard tool set.

    Defends against the AI returning hallucinated tool names (``Edit``,
    ``PerformSearch``, …) that would break the downstream AgentCreate
    validator. Always returns a non-empty list — falls back to
    ``["Read", "Write"]`` when the raw value is missing or filters
    down to empty.
    """
    if not isinstance(raw, list):
        return ["Read", "Write"]
    filtered = [
        t for t in raw
        if isinstance(t, str) and t in _STANDARD_TOOL_NAMES
    ]
    return filtered or ["Read", "Write"]


def _build_vision_user_prompt(
    office_name: str,
    description: str,
    requirements: dict[str, Any],
) -> str:
    """Compose the user prompt for ``SYNTHESIZE_VISION_PROMPT``.

    Called from BOTH ``analyze_office_description`` (Phase 1.5 primary
    synthesis) and ``generate_office_config`` (Phase 0 fallback when
    the analyze flow couldn't produce one). Centralised so the next
    prompt-tune can't drift between the two call sites.
    """
    label = office_name or "(unnamed office)"
    return (
        f"# Office: {label}\n\n"
        "## Original user description\n"
        f"{description}\n\n"
        "## Analyzed responsibility areas\n"
        f"{requirements.get('responsibility_areas', '(none extracted)')}\n\n"
        "## Analyzed desired agents\n"
        f"{requirements.get('desired_agents', '(none extracted)')}\n\n"
        "## Analyzed workflows\n"
        f"{requirements.get('workflows', '(none extracted)')}\n\n"
        "## Analyzed additional context\n"
        f"{requirements.get('additional_context', '(none extracted)')}\n"
    )

# ---------------------------------------------------------------------------
# Shared framing — every downstream prompt opens with this paragraph so
# the model treats its slice as part of a coherent virtual-office build,
# not as a one-shot JSON extraction. Centralised so the framing only has
# to be edited in ONE place when we tune the office-creation north-star.
# ---------------------------------------------------------------------------

OFFICE_BUILD_FRAMING = """\
You are designing one slice of a CUBICLE VIRTUAL OFFICE.

A Cubicle office is a small team of AI agents working a Kanban board
under a single AI Manager. Custom agents you design layer on top of
FOUR mandatory SYSTEM AGENTS that every office ships with. The
system agents are INVISIBLE to the roster — you neither list them
nor regenerate them — but they shape what the custom team should
look like.

## System Agents (always present — design AROUND them)

  * **analyst** — Research, comparison, planning, market sensing.
    Tools: Read, Glob, Grep, WebSearch, WebFetch, Write.
    A custom "Research Specialist" / "Market Analyst" agent is
    almost always a duplicate of the Analyst — sharpen to a
    domain action instead.

  * **auditor** — Verifies deliverables against acceptance criteria.
    Tools: Read, Glob, Grep, Bash.
    NEVER design a "Quality Reviewer" / "QA Agent" — that's the
    Auditor.

  * **automation-script-developer** — Writes long-running Python
    scripts (batch automation > 20 items, scheduled work, API loops,
    integrations). Tools: Read, Write, Bash, Glob, Grep, WebSearch,
    WebFetch.
    NEVER design a "Scripting Agent" / "Integration Engineer" /
    "Automation Engineer" — that's the Auto Script Dev.

  * **manager-assistant** — Quick lookups, board triage, orphan-task
    routing (the Board Operator). Tools: Read, Write, Glob, Grep,
    WebSearch, WebFetch.
    NEVER design a "Coordinator" / "Triage Agent" / "Project Manager"
    — that's the Manager Assistant.

A custom agent earns its slot only if its work is DOMAIN-SPECIFIC
and cannot be reduced to one of the four above.

## Prime directive

Cohesion. Every choice you make should serve the office's overall
purpose, not just satisfy your local slice. If you spot a GAP in
what the user described — a critical responsibility no agent owns,
a workflow with no handoff to a reviewer, a skill nobody has — flag
it via the appropriate output channel; do NOT silently smooth it
over."""


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

_AGENT_OUTPUT_CONTRACT = """\
## ``system_prompt`` — WHO this agent IS (the role signature)

This is the actual ``--system-prompt`` the Claude CLI loads at the
start of every task. It anchors behaviour for the WHOLE session.
Write 250-450 words of agent-facing prose, no markdown headers, no
lists. The structure MUST be:

1. **Identity** — one sentence: "You are the {office}'s {role}."
2. **Mission** — 2-3 sentences expanding what THIS agent owns
   end-to-end in THIS office. Reference real domain terms from the
   Vision.
3. **Core principles** — 3-5 sentences, each a principle this agent
   never compromises on. Each principle must be ROLE-SPECIFIC and
   ACTIONABLE — "always cite sources with file_path + line for code
   claims", "never accept a candidate's resume without confirming
   visa eligibility for the office's target market", "every
   migration ships with both up and down SQL". Generic principles
   ("be thorough", "communicate clearly") are FORBIDDEN.
4. **Decision-making style** — 1-2 sentences on how this agent
   resolves ambiguity. "Prefer evidence over intuition; ask via
   activity question when X is unclear."
5. **Communication tone** — 1 sentence on tone calibrated to the
   office's domain (direct/warm/formal/forensic).
6. **Anti-patterns** — 2-3 sentences naming patterns this agent
   actively rejects. Role-specific failure modes.

MUST NOT contain:
- Step-by-step processes (those belong in claude_md_content).
- File paths, tool names, or output-format templates.
- Lists of tools the agent has — already in allowed_tools.
- Generic rules like "be helpful" or "respect the user".
- Quality-bar criteria (those go in claude_md_content's ``### Quality Bar``).
- The blocker_class enum, save_file protocol, tool-error handling,
  reviewer mode — those land in the shared baseline. Don't repeat.

## ``claude_md_content`` — HOW this agent WORKS (office-specific enrichment)

500-1000 words of markdown. This is rendered as an "Office-Specific
Notes" enrichment BELOW the shared baseline in the agent's composed
CLAUDE.md. The baseline already covers: artifact rules, blocker_class
taxonomy + comment template, tool-error handling, ``## Communication``,
``## When You Are a Reviewer``, ``## Scope``, ``## Existing Knowledge``,
``## Completion (when executing, not reviewing)``. DO NOT REPEAT any of
those — repetition produces duplicate H2 sections in the final file.

MUST follow this EXACT outline (use H3 headers — the parent context is
already ``## Office-Specific Notes``, so these sit as children of it
rather than colliding with the baseline's H2 headers):

```
### Mission
1-2 sentences. The agent's job in THIS office in plain English.
Expanded form of the system_prompt's identity sentence.

### Core Responsibilities
3-6 bullets. What this agent OWNS end-to-end. Action verbs ("authors",
"reviews", "sources"). Each bullet should be a specific outcome, not
a generic activity. "Sources 20-50 qualified candidates per role per
week from LinkedIn + AngelList + domain-specific job boards" beats
"researches candidates".

### Standard Operating Procedure
Numbered steps for the agent's TYPICAL task flow. Step 1 is always
"Read the Task Brief end-to-end before doing anything else." Then
agent-specific steps that name REAL tools, REAL skills (by slug),
REAL file paths. STOP at the work step — do NOT add a final "submit"
step (the baseline's ``## Completion`` already covers submission).
Length: 4-8 steps.

### Tool Usage Patterns
For EACH tool in allowed_tools, ONE line on when to reach for it.
Be specific to this agent's domain. Cover BOTH the right uses and the
common wrong uses for THIS agent (e.g., "WebSearch — competitor signals
and current events; not for KB content — use ``search_kb`` instead").

### Skills Application (omit section entirely if no skills assigned)
For EACH skill assigned (catalog + custom), ONE line on the trigger
condition: "**{skill-name}** — invoke when {specific condition}."

### Handoffs
The agent's handoff matrix. Cover the handoffs this agent will
plausibly use; you do NOT need to enumerate all four system agents
if the agent's role doesn't naturally interact with all of them.
Use one of these CALLABLE mechanisms (every name below is a real
worker-side MCP tool):

- ``propose_task(...)`` — propose a brand-new task with brief +
  rationale. Use for out-of-scope follow-ups (e.g. "this surfaced a
  separate bug that needs its own task").
- ``propose_subtask(...)`` — propose decomposing the current task
  into smaller ones.
- ``propose_update_task(task_id, changes={"reviewer": "<slug>"},
  justification=...)`` — ask the Manager to flip the reviewer
  (e.g., ``{"reviewer": "auditor"}`` for verification handoff,
  ``{"reviewer": "<custom-teammate>"}`` for domain review).
- ``propose_artifact_handoff(...)`` — hand a deliverable to a
  specific named agent for their downstream work.
- ``escalate_blocker(...)`` — tell the Manager you cannot proceed
  (use ONLY when a ``question`` activity isn't enough — see the
  baseline's escalation guidance).
- ``request_clarification(...)`` — ask the Manager a structured
  question that blocks progress.
- ``add_activity(event_type="question", ...)`` — lightweight inline
  question that does not block the task.

Cover at minimum: how this agent gets DELIVERABLES REVIEWED (typically
``propose_update_task`` with ``{"reviewer": "auditor"}`` unless the
role itself is review). For each plausible custom teammate handoff,
ONE sentence: "When X, hand off to {teammate} via {mechanism}."

### Output Format
Agent-specific deliverable format. Filename convention, structure,
section requirements. Reference the per-workstream output directory
pattern (``/workspace/outputs/{workstream_short_code}/``). 2-5 lines.

### Quality Bar
What PASS looks like for this agent's deliverables — the criteria
the reviewer would use. 2-4 bullets. Concrete + measurable where
possible ("all citations include file:line", "test coverage ≥ 80%
of changed lines", "every claim sourced to a URL").

### Office-Specific Conventions
3-5 short bullets capturing domain rules / house style for THIS
office. Things a new hire would otherwise have to absorb by osmosis.
Skip the section if you genuinely have nothing office-specific to
add (don't pad with generic engineering / writing advice).
```

DO NOT include sections that overlap the shared baseline. Specifically
NEVER author headers matching: ``Communication``, ``Tool Error Handling``,
``Existing Knowledge``, ``Delivering Your Work``, ``Scope``,
``When You Are a Reviewer``, ``Completion``, ``Escalation Rules``,
``Completion Checklist``, ``STOP — If your task involves writing a Python
script``. The baseline owns those — duplicating them produces conflicting
guidance for the agent at session start.

## Rules

- Be SPECIFIC to this agent's role + the office's domain. Generic
  guidance is FORBIDDEN.
- Reference REAL tools from allowed_tools and REAL skills from the
  inputs by name / slug. Every MCP tool name you cite MUST be a real
  worker-side tool (the list above is exhaustive for handoffs;
  ``save_file``, ``attach_to_task``, ``get_my_brief``, ``search_kb``,
  ``list_files``, ``get_file`` are the safe-to-cite work tools).
- Speak TO the agent (use "you"), not ABOUT it.
- system_prompt + claude_md_content MUST cover DIFFERENT ground.
- If you notice OVERLAP with a teammate's role, SHARPEN your boundary
  rather than claiming joint ownership.
"""


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

SYNTHESIZE_VISION_PROMPT = OFFICE_BUILD_FRAMING + """

You are producing the OFFICE VISION — the load-bearing document every
downstream generation step reads. It is the SINGLE source of truth for
what this office is, who it serves, and what "good" looks like.

The user message gives you the office name, the original free-text
description, AND the four analyzed requirement fields
(responsibility_areas, desired_agents, workflows, additional_context).
Your job is to SYNTHESISE them into one coherent vision — find the
through-line that ties responsibilities to workflows to the agent set.

## Output structure (use these EXACT H2 headers)

```
## Mission
One paragraph (2-4 sentences). Who the office serves and what
"done" means for it. Concrete; no aspirational filler.

## Scope
What the office DOES. What it DELIBERATELY does NOT do (the
"out of scope" list is as important as the in-scope one).

## Operating Model
How the office gets work done end-to-end. One paragraph naming the
core flow: where work originates, how it moves through agents, where
it lands. Reference workflow names from the user's description.

## North-Star Signals
3-5 short bullets. The signals that tell us this office is healthy.
e.g. "shortlist quality > 70% acceptance rate", "no escalation
delays > 4h", "100% of releases reviewed before merge".

## Critical Gaps Noticed
Optional. If the user's description has obvious holes for the
mission they described (a responsibility with no plausible owner,
a workflow with no review gate, a critical skill nobody mentioned),
list each as one short bullet. If the description is complete, write
"None — coverage looks complete."
```

## Rules

- 200-400 words total. This is read by every downstream phase, so
  every sentence should pay rent.
- Specific. Quote real terms from the user's description.
- The "Critical Gaps Noticed" section is the AI's FIRST chance to be
  proactive — use it. Downstream phases will use this to propose
  agents/skills the user didn't ask for.

Output a JSON object with a single field:

{
  "vision": "## Mission\\n...\\n\\n## Scope\\n..."
}

Output ONLY the JSON. No markdown code blocks, no extra text."""


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

COHESION_REVIEW_PROMPT = OFFICE_BUILD_FRAMING + """

You are the COHESION REVIEWER — the last AI pass over a freshly-
generated office config before the user sees it. You read the office
vision, the instructions, the full agent roster (with role
descriptions), and the skill assignments. You produce a structured
assessment of how well the generated config actually covers the
office's mission.

Three things to look for:

1. **Coverage** — for every responsibility area in the vision /
   instructions, identify which custom agent(s) own it. Strength is
   ``strong`` (one clear owner, well-equipped), ``partial`` (an owner
   exists but lacks tools/skills or shares the area ambiguously), or
   ``missing`` (no owner).

2. **Gaps** — responsibilities, workflows, or critical capabilities
   the office's mission requires but no agent + skill combo covers.
   For each gap, name what's missing and (if obvious) suggest the
   agent or skill that would fill it.

3. **Redundancies** — two or more agents with substantially
   overlapping ownership. Surface these so the user can either
   collapse them or sharpen the boundary.

4. **Suggested additions** — proactive recommendations the user
   should consider. Each: one of ``agent`` / ``skill`` / ``workflow``,
   a one-line summary, and a rationale tied to a specific gap or
   north-star signal.

## Output

{
  "confidence_score": 0-100,
  "summary": "One paragraph (2-4 sentences) for the user's eyes.",
  "coverage": [
    {"responsibility": "...", "owners": ["agent-slug"], "strength": "strong|partial|missing"}
  ],
  "identified_gaps": [
    {"area": "...", "why_critical": "...", "suggested_agent": "agent-slug or null", "suggested_skill": "skill-slug or null"}
  ],
  "redundancies": [
    {"agents": ["agent-slug-a", "agent-slug-b"], "overlapping_area": "...", "suggested_merge": "Sharpen X to own A; sharpen Y to own B."}
  ],
  "suggested_additions": [
    {"kind": "agent|skill|workflow", "name": "human-friendly name", "summary": "...", "rationale": "..."}
  ]
}

## Rules

- ``confidence_score`` is your honest assessment of how well this
  config covers the mission. 90+ = ready as-is. 70-89 = ready but
  suggestions worth reviewing. <70 = critical gaps the user should
  address before accepting.
- ``owners`` uses agent SLUGS (matching ``name`` in the roster),
  not display names.
- ``coverage`` should hit every major responsibility from the
  vision — don't cherry-pick. 5-10 entries is typical.
- ``identified_gaps`` should be SHARP. Don't pad with nice-to-haves
  here (those go in ``suggested_additions``). A "gap" is something
  the mission CANNOT be delivered without.
- All four arrays may be empty if the config is genuinely complete
  and tight. An empty ``identified_gaps`` is a real signal — don't
  invent gaps to pad the response.

Output ONLY the JSON. No markdown code blocks, no extra text."""


# ---------------------------------------------------------------------------
# System prompts for each phase
# ---------------------------------------------------------------------------

ANALYZE_SYSTEM_PROMPT = """\
You are an expert at analyzing office descriptions and extracting structured requirements.

Given a free-text description of an AI office, extract and expand the information into four fields.

CRITICAL: Every value MUST be a plain text string. NOT an array, NOT an object. Use newlines and bullet characters within the string for structure.

Output a JSON object exactly like this example:

{
  "responsibility_areas": "- Lead generation and qualification\\n- Sales pipeline management\\n- CRM data enrichment\\n- Outreach sequence creation",
  "desired_agents": "- Sales Researcher: Researches prospects and companies before outreach\\n- Outreach Specialist: Creates personalized email sequences\\n- Pipeline Analyst: Tracks conversion rates and pipeline health",
  "workflows": "1. Research target companies and contacts\\n2. Enrich prospect data from multiple sources\\n3. Create personalized outreach sequences\\n4. Track responses and engagement\\n5. Qualify leads and update pipeline",
  "additional_context": "We use Salesforce as our CRM. Target market is mid-market SaaS companies in North America. Team of 5 SDRs."
}

All four values are STRINGS containing formatted text. Be thorough — expand brief mentions into detailed descriptions.

Output ONLY the JSON object. No markdown, no code blocks, no extra text."""


INSTRUCTIONS_PROMPT = OFFICE_BUILD_FRAMING + """

You author the office-level CLAUDE.md — the shared playbook every agent
reads before starting work in this office. It is the single source of truth
for how the team operates: mission, workflows, conventions, escalation.

You are NOT producing instructions from scratch — you are MATERIALISING
the Vision Brief (provided in the user message) into the office's
CLAUDE.md. Every section MUST trace back to the Vision. If a section can
be written without referencing the Vision, it is too generic — rewrite it.

The instructions MUST follow this EXACT outline. Every H2 section below is
required. Write in specific, actionable language tailored to THIS office —
generic advice ("write clearly", "be helpful") is forbidden.

```
# {Office Name}

## Mission
One concrete paragraph: who this office serves, what success looks like,
what the team measures itself on. No aspirational filler.

## Domain & Focus
What the office DOES. What it DELIBERATELY does not do. Domain terminology
and key concepts agents will encounter. If the office overlaps with
adjacent domains, name the boundary.

## Key Workflows
Two to five END-TO-END workflows the team runs repeatedly. Each workflow is
its own H3 subsection containing a numbered list with explicit handoffs
between agents. Example shape:

  ### Workflow A: Lead Qualification
  1. Sales Researcher pulls inbound lead, enriches data via web research.
  2. Outreach Specialist drafts personalized sequence; reviews with Auditor.
  3. Pipeline Analyst tracks engagement; flags qualified leads for handoff.

Reference real workflow names from the requirements — do not invent.

## Quality Standards
What "done" looks like in this office. Output format expectations,
review bars, anti-patterns. Be concrete (e.g. "every report ends with
a Recommended Next Steps section").

## Communication Norms
How agents address each other in task Activity. When to ask vs. assume.
Tone for user-facing deliverables. **Explicit handoff conventions
between custom agents and the four SYSTEM agents** — when to delegate
to Analyst for research, when to route to Auditor for review (via
``reviewer=auditor`` on the task), when to escalate to Automation
Script Developer for batch work, when to ask Manager Assistant for
triage / quick lookups. Tag conventions (`@manager`, `@reviewer`) if
applicable.

## Tools & Resources
Pointers — not enumeration — to where the office's skills, scripts, and
connectors live and when each is appropriate. Skills directory is
`/workspace/.claude/skills/`. Scripts live in `/workspace/.scripts/`.
Outputs go to `/workspace/outputs/{workstream-slug}/`.

## Escalation Paths
When to mark a task `blocked` (credentials missing, dependency broken,
ambiguous spec). When to escalate to the user via Manager. When to propose
a new task vs. handle inline. Reference the `blocker_class` taxonomy
(auth_failed, missing_credential, permission_denied, missing_data,
ambiguous_spec, broken_dependency, external_outage).

## Conventions
File naming, output directories, label usage on tasks, scope organization.
Anything the team has standardized that a new agent would otherwise have
to guess at.
```

## Rules

- 700-1400 words total. Be SPECIFIC to this office — every section must
  reflect the actual requirements supplied in the user message.
- Speak to the AGENTS who will read this, not the office owner.
- Quote real workflow names, agent roles, and tools from the inputs.
- If you genuinely have no specific guidance for a section, write a one-line
  honest placeholder ("To be refined as the team converges on practice")
  rather than padding with generic advice.
- Use H2 headers exactly as listed; agents pattern-match on these.

Output a JSON object with a single field:
{
  "instructions": "# {Office Name}\\n\\n## Mission\\n..."
}

Output ONLY the JSON. No markdown code blocks, no extra text."""


ROSTER_PROMPT = OFFICE_BUILD_FRAMING + """

You design the CUSTOM agent roster for this office. The user message
includes:

- The **Office Vision Brief** — your anchor. Every agent must serve a
  responsibility from the Vision; the roster as a whole must cover
  every responsibility AND every workflow handoff named in the Vision.
- The office instructions you already authored (Mission, Workflows,
  Quality Standards, etc.).
- A **Skill Catalog** — pre-built SKILL.md playbooks the platform
  ships. PREFER catalog skills over inventing new ones; catalog
  entries are battle-tested and arrive with reference files attached.
- The original analyzed requirements (responsibilities, desired
  agents, workflows, additional context).

## Be proactive — propose what the user missed

Beyond the agents the user explicitly asked for, you are EXPECTED to
propose 0-2 additional agents the user didn't think to mention but
the Vision Brief implies the office needs. Common patterns:

- Recruitment office without an "Onboarding Coordinator" — propose one.
- Sales office without a "Customer Success Specialist" — propose one.
- Engineering office without an "On-Call Engineer" — propose one.
- Any office that handles user-facing deliverables without a domain
  reviewer beyond the Auditor.

For each AI-proposed agent, set ``proposed_because`` to a one-sentence
rationale tied to a specific Vision responsibility. The Review step
shows these as flagged-for-confirmation cards so the user can accept,
remove, or merge.

## Skill assignment rules

For each agent, fill BOTH of these fields:

- ``skill_template_ids``: catalog ``id`` values (e.g.
  "anthropic-doc-coauthoring", "code-review"). The setup wizard
  installs these for you. Pick everything that legitimately applies —
  agents share templates, and re-installing is idempotent.
- ``skill_names``: lowercase-hyphenated slugs for NEW skills the
  catalog does NOT cover. These get authored from scratch in a later
  phase. Each slug must be unique across the whole roster (multiple
  agents CAN reuse the same slug — it's still one skill, written once).

CRITICAL: if a capability is already in the catalog, use
``skill_template_ids`` — NEVER duplicate the same capability into
``skill_names``. Examples:
- Need code review? Use template ``code-review``. Do NOT add
  "code-review" to skill_names.
- Need PDF processing? Use template ``anthropic-pdf``.
- Need bespoke "insurance-claim-triage"? Add it to skill_names.

## Per-agent fields

- ``name``: lowercase-with-hyphens slug, unique across the roster.
  MUST NOT match a system agent (analyst, automation-script-developer,
  auditor, manager-assistant).
- ``display_name``: human-readable.
- ``avatar_emoji``: a relevant emoji (not a robot face).
- ``role_description``: ONE sentence — what this agent owns
  end-to-end. Use ACTION verbs ("authors", "reviews", "sources"),
  not framings ("focuses on", "is responsible for").
- ``model``: "claude-opus-4-7" by default (uniform thinking-mode
  Opus produces consistent multi-step planning). Use
  "claude-sonnet-4-6" ONLY for agents whose work is genuinely
  bounded execution-tier.
- ``allowed_tools``: subset of [Read, Write, Bash, Glob, Grep,
  WebSearch, WebFetch]. Heuristics:
    - Research / analysis: Read, Glob, Grep, WebSearch, WebFetch, Write
    - Development / scripts: Read, Write, Bash, Glob, Grep
    - Frontend / design: Read, Write, Glob, Grep
    - Review / audit: Read, Glob, Grep, Bash
- ``skill_template_ids``: list of catalog ``id``s (can be empty).
- ``skill_names``: list of NEW skill slugs (can be empty).
- ``proposed_because``: null when the agent comes straight from the
  user's request. Set to a one-sentence rationale ONLY when this
  agent is your proactive proposal (gap-filling). The Review UI
  shows these specially so the user can opt in/out.

Do NOT include system_prompt or claude_md_content — those are
generated separately per-agent so each one gets focused attention.

## Workstream proposals

The office runs on workstreams (project containers). Propose 1-3
starter workstreams the office should have based on the Vision's
workflows. The Review step pre-creates these for the user.

Each: ``{name, description, rationale}``. Names are human-readable
("Q3 Inbound Recruitment", not slugs). Rationale references the
Vision workflow it materialises.

## Roster Rationale

Write a one-paragraph ``roster_rationale`` (3-5 sentences) explaining
WHY this roster covers the Vision's mission without overlapping the
system agents. The Cohesion review (next phase) cross-checks your
rationale against the actual roster — be honest.

## Output

{
  "agents": [
    {
      "name": "slug-name",
      "display_name": "Human Name",
      "avatar_emoji": "🔍",
      "role_description": "Action verb + what they own.",
      "model": "claude-opus-4-7",
      "allowed_tools": ["Read", "Write", "Glob", "Grep"],
      "skill_template_ids": ["code-review"],
      "skill_names": ["domain-specific-skill"],
      "proposed_because": null
    }
  ],
  "proposed_workstreams": [
    {
      "name": "Workstream Name",
      "description": "1-2 sentences on purpose.",
      "rationale": "Maps to Vision workflow X / responsibility Y."
    }
  ],
  "roster_rationale": "One paragraph defending the roster shape."
}

Generate between 2 and 12 agents with SPECIFIC, NON-OVERLAPPING roles.
Two agents that do "research" with different framing is a smell — combine
them or sharpen the boundary.

Output ONLY the JSON. No markdown code blocks, no extra text."""


AGENT_DETAIL_PROMPT = OFFICE_BUILD_FRAMING + """

You author TWO documents for ONE specific agent in the office's
roster. Both are read by Claude at session start: the
``system_prompt`` is the actual ``--system-prompt`` of every task
the agent runs; the ``claude_md_content`` is appended to a composed
``/workspace/agents/{name}/CLAUDE.md`` baseline that already includes
universal best-practice rules (artifacts, blocker_class, tool
errors, etc.).

The user message gives you:
- The **Office Vision Brief** — your anchor. Every section MUST
  trace back. If you write something that would be true for a
  generic agent, it's wrong — rewrite to be specific to this agent
  IN THIS office.
- This agent's slot in the roster (name, role, tools, skills).
- The **full custom roster** (other agents' names, roles, tools,
  skills) — your Core Responsibilities MUST NOT overlap with any
  teammate's, and your Communication & Handoffs section MUST
  reference them by name with concrete mechanisms.
- Office instructions excerpt.

If you notice overlap with a teammate, prefer SHARPENING your own
boundary over claiming joint ownership — the Cohesion pass
downstream surfaces true conflicts. Don't fudge them here.

""" + _AGENT_OUTPUT_CONTRACT + """

Output a JSON object with exactly these two fields:

{
  "system_prompt": "You are the {office}'s {role}. ...",
  "claude_md_content": "## Mission\\n...\\n\\n## Core Responsibilities\\n..."
}

Output ONLY the JSON. No markdown code blocks, no extra text."""


# ---------------------------------------------------------------------------
# Single-agent generation (Agents page "Create with AI" flow)
# ---------------------------------------------------------------------------

AGENT_FROM_DESCRIPTION_PROMPT = OFFICE_BUILD_FRAMING + """

You design a NEW custom agent for an existing Cubicle office from
the user's free-text description. Unlike the wizard (which designs
the full custom roster from scratch), you add ONE specialist on top
of an existing custom roster, skill catalog, and connector set —
your agent must complement what's already there without duplicating
any teammate's role.

The user message includes:
- ``Office`` — the office name + description for context.
- ``Available skills in this office`` — already-registered skills
  you can pick from via ``skill_names``. NEVER invent slugs that
  aren't in this list.
- ``Available connectors in this office`` — already-registered
  connectors for ``connector_names``. NEVER invent.
- ``Skill Catalog`` — pre-built playbooks the platform ships; pick
  via ``skill_template_ids`` and the backend installs them before
  returning the draft to the user.
- The user's free-text description of what the new agent should do.

## Output

A JSON object with EXACTLY these fields:

{
  "name": "lowercase-hyphenated-slug",
  "display_name": "Human-Readable Name",
  "avatar_emoji": "🔍 (a relevant emoji — not the default robot)",
  "role_description": "One sentence — action verb + what they own.",
  "system_prompt": "<see contract below>",
  "claude_md_content": "<see contract below>",
  "model": "claude-opus-4-7",
  "allowed_tools": ["Read", "Write", "..."],
  "skill_names": ["existing-office-skill-slug", "..."],
  "skill_template_ids": ["catalog-template-id", "..."],
  "connector_names": ["existing-connector-name", "..."]
}

""" + _AGENT_OUTPUT_CONTRACT + """

## Field-specific rules

- ``name`` — lowercase-hyphenated slug, derived from display_name.
  MUST NOT match a system agent slug (the four system agents listed
  in the framing above). If your derived slug collides, qualify with
  a domain prefix (e.g. "marketing-analyst" instead of "analyst").
- ``allowed_tools`` — subset of [Read, Write, Bash, Glob, Grep,
  WebSearch, WebFetch]. Heuristics:
    - Research / analysis: Read, Glob, Grep, WebSearch, WebFetch, Write
    - Development / scripts: Read, Write, Bash, Glob, Grep
    - Frontend / design: Read, Write, Glob, Grep
    - Review / audit: Read, Glob, Grep, Bash
- ``skill_names`` — pick ONLY from the "Available skills" list. If
  none fit the new agent's role, return ``[]`` (do NOT invent slugs
  that don't exist; the wizard's catalog install only runs in the
  office-creation flow, not here).
- ``skill_template_ids`` — pick ONLY from the "Skill Catalog" ``id``
  values. PREFER templates over inventing new playbooks; the catalog
  entries are battle-tested. The backend installs the picked
  templates server-side BEFORE saving the agent (idempotent).
- Do NOT duplicate the same capability across ``skill_names`` and
  ``skill_template_ids``. If a catalog template covers what the
  agent needs, use ONLY the template id.
- ``connector_names`` — pick ONLY from the "Available connectors"
  list. NEVER invent.

Output ONLY the JSON object. No markdown code blocks, no prose."""


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

WORKSTREAM_CONTEXT_PROMPT = """\
You are an expert at writing workstream context notes for AI agents.

A workstream is a project / initiative inside an AI office. Its
"Context Notes" become part of the workstream's CLAUDE.md — every
agent working on a task in this workstream reads it before starting.
The notes must be PRACTICAL: process, conventions, responsibilities,
constraints. They should NOT restate generic agent rules.

The user will give you a free-text brief about the workstream. Read
it carefully and produce a polished markdown context note.

Output a JSON object:

{
  "context_notes": "## Goal\\n...\\n\\n## Scope & Responsibilities\\n...\\n\\n## Process & Workflow\\n...\\n\\n## Tools, Techniques & Conventions\\n...\\n\\n## Constraints & Edge Cases\\n..."
}

## Required sections (use these EXACT H2 headers)

- ## Goal — One-paragraph statement of the workstream's purpose and success criterion.
- ## Scope & Responsibilities — What belongs here / what does not. If the user mentioned specific roles or owners, capture them.
- ## Process & Workflow — Concrete steps, hand-offs, review gates. Numbered when sequential, bulleted when parallel.
- ## Tools, Techniques & Conventions — Specific tools, APIs, file conventions, naming, output formats the team uses.
- ## Constraints & Edge Cases — Compliance, deadlines, anti-patterns, known pitfalls.

## Style

- 300-700 words of markdown total.
- Be specific. Expand brief mentions into actionable guidance.
- If the user didn't cover a section, write a brief honest placeholder
  ("To be defined — capture once X is decided") rather than inventing facts.
- Speak to the agents working on this workstream, not to the user.

Output ONLY the JSON object. No markdown code blocks, no prose."""


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


SKILLS_PROMPT = """\
You are an expert skill-playbook author for the Cubicle platform.

You write SKILL.md playbooks for capabilities the office needs that are NOT
already in the curated catalog. The platform-shipped catalog handles common
needs (code review, doc co-authoring, PDF/PPTX/XLSX, frontend design, web
research, etc.) — your job is the DOMAIN-SPECIFIC long tail.

The user message lists the required NEW skill slugs (the ones an agent's
``skill_names`` field referenced, after de-duplicating against the catalog).
Generate ONE entry per slug.

## SKILL.md template (MANDATORY)

Each ``playbook_content`` MUST follow this exact structure:

```
---
name: {skill-slug}
description: {one-sentence summary}
allowed-tools:
  - Read
  - {other tools the skill genuinely uses}
---

# {Skill Display Name}

## When to Use
1-2 sentences naming the trigger conditions — what kind of task makes
this skill the right move. Be specific; "research things" is useless.

## Process
Numbered steps the agent follows when applying this skill. Reference
concrete tools, file paths, decision points. Be opinionated.

## Inputs
What the skill needs to do its work (data, credentials, prerequisites).

## Output Format
What the skill produces. File destination
(`/workspace/outputs/{workstream-slug}/`), structure, naming.

## Quality Checklist
Bullet list the agent runs BEFORE submitting work that used this skill.

## Anti-Patterns
What NOT to do. Common mistakes specific to this capability.
```

## Rules

- 250-500 words per playbook. Long enough to be actionable, short enough
  to read once per session.
- Be DOMAIN-SPECIFIC. If the slug is "claims-triage", write about claims
  taxonomy, severity thresholds, escalation rules — not generic "review
  the data and produce a report".
- Tools in ``allowed-tools`` MUST be drawn from
  [Read, Write, Bash, Glob, Grep, WebSearch, WebFetch].
- Per-skill ``parameter_schema`` is usually empty; include entries only
  when the skill genuinely needs a configurable input (API endpoint,
  output style toggle). Each entry: ``{name, type, is_secret, description}``.

## Output

{
  "skills": [
    {
      "name": "skill-slug",
      "display_name": "Skill Display Name",
      "description": "One sentence.",
      "playbook_content": "---\\nname: skill-slug\\n...",
      "parameter_schema": []
    }
  ]
}

If no NEW skills are required (the catalog covers everything), return
``{"skills": []}``.

Output ONLY the JSON. No markdown code blocks, no extra text."""


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

_SKILL_MD_TEMPLATE_BLOCK = """\
## SKILL.md template (MANDATORY structure)

The ``playbook_content`` MUST follow this exact structure:

```
---
name: {skill-slug}
description: {one-sentence summary}
allowed-tools:
  - Read
  - {other tools the skill genuinely uses}
---

# {Skill Display Name}

## When to Use
1-2 sentences naming the trigger conditions — what kind of task
makes this skill the right move. Be specific; "research things"
is useless.

## Process
Numbered steps the agent follows when applying this skill. Reference
concrete tools, file paths, decision points. Be opinionated.

## Inputs
What the skill needs to do its work (data, credentials, prerequisites).

## Output Format
What the skill produces. File destination
(`/workspace/outputs/{workstream-slug}/`), structure, naming.

## Quality Checklist
Bullet list the agent runs BEFORE submitting work that used this skill.

## Anti-Patterns
What NOT to do. Common mistakes specific to this capability.
```"""

_SKILL_BASE_RULES = """\
- 250-600 words per playbook. Long enough to be actionable, short enough
  to read once per session.
- Be DOMAIN-SPECIFIC. If the slug is "claims-triage", write about
  claims taxonomy, severity thresholds, escalation rules — not
  generic "review the data and produce a report".
- Tools in ``allowed-tools`` MUST be drawn from
  [Read, Write, Bash, Glob, Grep, WebSearch, WebFetch].
- ``parameter_schema`` is usually empty; include entries only when
  the skill genuinely needs a configurable input (API endpoint,
  output style toggle). Each entry:
  ``{name, type, is_secret, description, default_value}``."""

_SKILL_JSON_OUTPUT_SHAPE = """\
## Output (one skill object — NOT an array)

```json
{
  "name": "skill-slug",
  "display_name": "Skill Display Name",
  "description": "One sentence — identical to the SKILL.md frontmatter description.",
  "playbook_content": "---\\nname: skill-slug\\n...full SKILL.md as a JSON-escaped string...",
  "parameter_schema": []
}
```

Output ONLY the JSON. No markdown code blocks, no commentary, no preamble."""


# Per-skill variant of SKILLS_PROMPT — generates ONE playbook per call.
# Switched to this in 2026-05-22 because the bundled "all skills in one
# call" variant could push past 120s on offices with 5+ custom skills.
# Splitting also gives the UI per-skill progress instead of a long
# silent wait while the model writes 5×500 words.
SINGLE_SKILL_PROMPT = OFFICE_BUILD_FRAMING + f"""

Write a single SKILL.md playbook for the slug named in the user
message. The platform-shipped catalog handles common needs (code
review, doc co-authoring, PDF/PPTX/XLSX, frontend design, web
research, etc.) — your job is the DOMAIN-SPECIFIC long tail.

The user message tells you WHICH AGENTS will use this skill and gives
you each of their role descriptions + allowed_tools + assigned
skills. The playbook MUST FIT those specific agents — match their
tone, restrict the ``allowed-tools`` section to a subset they actually
have, and write Process steps the agents can actually execute. A
playbook that asks the agent to use Bash when none of the using agents
has Bash allowed is a defect.

Also anchor the skill in the **Office Vision Brief** (in the user
message). The "When to Use" section should reference a real Vision
responsibility / workflow — not a generic trigger.

{_SKILL_MD_TEMPLATE_BLOCK}

## Rules

{_SKILL_BASE_RULES}

{_SKILL_JSON_OUTPUT_SHAPE}"""


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


STANDALONE_SKILL_PROMPT = f"""\
You are an expert SKILL.md playbook author for the Cubicle platform. Cubicle
agents auto-discover SKILL.md files in ``.claude/skills/`` and use them as
opt-in playbooks for specific tasks. Your output IS the playbook.

The user supplies a one-paragraph overview describing the capability they
want. You produce ONE comprehensive SKILL.md, optional ``parameter_schema``
entries, and tidy ``name`` / ``display_name`` / ``description`` metadata.

{_SKILL_MD_TEMPLATE_BLOCK}

## Best-practice rules (NON-negotiable)

{_SKILL_BASE_RULES}
- **Process-first, output-second**. Always. The user reads SKILL.md
  to learn HOW the skill runs; output format is a contract, not the
  point.
- **Concrete tool names**, never vague verbs. "Use ``Grep`` with
  ``--type py`` to find call sites" beats "search the codebase".
- **Refer to parameters by name**. If a parameter is declared, the
  playbook MUST reference it (otherwise why declare it).
- **Allowed-tools is a subset**. Only include tools the Process step
  actually invokes. Adding everything "just in case" defeats the
  purpose of restricting agent reach.
- **Slug honesty**: if the user provided a ``name`` in the user
  message, slugify it faithfully — NEVER invent a different slug.
  The backend pins the user's typed name as the final slug; an
  invented one is silently overridden, wasting your context budget
  on a name nobody sees.

{_SKILL_JSON_OUTPUT_SHAPE}"""


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


def _slugify_skill_name(raw: str) -> str:
    """Slugify for SKILL.md filesystem layout — NEVER returns "office".

    ``paths.slugify`` falls back to the workspace-naming default
    ``"office"`` for any input that collapses to an empty string
    (it's used for workspace dir names where "office" is a sensible
    default). That fallback is WRONG for skill names — if the daemon
    landed every empty-slug skill at ``.claude/skills/office/`` the
    second AI-generated skill with a bogus name would silently
    overwrite the first one's SKILL.md. The backend's slug authority
    is ``backend/app/core/utils.slugify`` which returns ``""`` for
    empty input + relies on its own ``_resolve_skill_name`` to layer
    in ``"new-skill"`` as the SKILL-domain default. Mirror that
    behaviour here so the two slug-of-records agree.

    Two-arg regex matches ``core.utils.slugify`` semantics: lowercase,
    collapse runs of non-alphanumeric to ``-``, strip leading /
    trailing hyphens. The single divergence from
    ``core.utils.slugify`` is that ``core.utils`` first replaces
    ``[\\s_]`` then strips, which produces the same result for every
    practical input (verified by the slug-equivalence audit in the
    round-3 review).
    """
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return slug or "new-skill"


def write_skill_to_workspace(
    workspace: Path,
    skill_data: dict[str, Any],
    requested_name: str | None,
) -> str:
    """Land a freshly-generated SKILL.md on the workspace, return the rel path.

    Sibling of :func:`generate_skill_from_overview` — kept here (next
    to the generation logic + the shared prompt constants) instead of
    in the WS dispatcher so the slug-of-record policy + atomic write
    are co-located with the only call site that produces them.

    The slug resolution chain mirrors what the backend's
    ``_resolve_skill_name`` does on the platform side: user-typed name
    wins, model echo is fallback, ``"new-skill"`` is the last-resort
    default. Doing the same resolution here lets the backend trust
    the returned ``written_path`` verbatim for the typical case
    (matching slugs); the backend still defends against drift by
    re-writing at the canonical path when the slugs disagree.

    Raises ``ValueError`` if the final slug is rejected by
    :func:`validate_name` (e.g. the model returned an unsafe value).
    The caller surfaces that as a user-facing 502.

    Returns the workspace-relative path (e.g.
    ``.claude/skills/my-skill/SKILL.md``) so the dispatcher can echo
    it back to the backend.
    """
    from src.fs_handler import _safe_resolve
    from src.utils import validate_name

    raw = (requested_name or str(skill_data.get("name") or "")).strip()
    final_name = _slugify_skill_name(raw)
    # Defence-in-depth — refuse a name that escapes the workspace or
    # contains chars the bind mount can't handle. validate_name is
    # the same gate used elsewhere in the daemon for user-controlled
    # filename segments.
    validate_name(final_name)

    rel_path = f".claude/skills/{final_name}/SKILL.md"
    full_path = _safe_resolve(workspace, rel_path)
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(str(skill_data.get("playbook_content") or ""))
    return rel_path


# ---------------------------------------------------------------------------
# Core CLI runner (unchanged)
# ---------------------------------------------------------------------------

async def _run_claude_cli(
    container_name: str,
    system_prompt: str,
    user_prompt: str,
    timeout: int = _CHUNK_TIMEOUT,
) -> str:
    """Run a Claude CLI query inside the Docker container."""
    sys_file = f"/tmp/cubicle_sys_{uuid.uuid4().hex[:8]}.txt"
    user_file = f"/tmp/cubicle_user_{uuid.uuid4().hex[:8]}.txt"

    try:
        await asyncio.to_thread(
            subprocess.run,
            ["docker", "exec", "-i", container_name, "tee", sys_file],
            input=system_prompt, capture_output=True, text=True, timeout=10,
        )
        await asyncio.to_thread(
            subprocess.run,
            ["docker", "exec", "-i", container_name, "tee", user_file],
            input=user_prompt, capture_output=True, text=True, timeout=10,
        )

        result = await asyncio.to_thread(
            subprocess.run,
            [
                "docker", "exec", container_name,
                "bash", "-c",
                f'cat "{user_file}" | claude --print'
                f" --output-format text"
                f" --max-turns 1"
                f" --model {_DEFAULT_GENERATION_MODEL}"
                f" --permission-mode bypassPermissions"
                f' --system-prompt-file "{sys_file}"',
            ],
            capture_output=True, text=True, timeout=timeout,
        )

        if result.returncode != 0:
            stderr = result.stderr.strip()[:500]
            stdout = result.stdout.strip()[:500]
            raise RuntimeError(
                f"Claude CLI failed (rc={result.returncode}): {stderr or stdout}"
            )

        stdout = result.stdout.strip()
        if not stdout:
            # rc=0 + empty stdout. Disambiguate auth vs
            # model-unavailable by running a haiku probe — same model
            # cbcl-setup uses for its auth check. If the probe ALSO
            # comes back empty, auth is broken; if it succeeds, the
            # configured model is the problem (most likely not in
            # this account's plan, or CLI too old).
            probe_result = await asyncio.to_thread(
                _probe_claude_works, container_name,
            )
            raise _empty_cli_output_error(
                model=_DEFAULT_GENERATION_MODEL,
                stderr=result.stderr.strip()[:500],
                container_name=container_name,
                probe_succeeded=probe_result,
            )
        return stdout

    finally:
        asyncio.create_task(asyncio.to_thread(
            subprocess.run,
            ["docker", "exec", container_name, "rm", "-f", sys_file, user_file],
            capture_output=True, timeout=5,
        ))


async def _run_chunk(
    container_name: str,
    system_prompt: str,
    user_prompt: str,
    timeout: int = _CHUNK_TIMEOUT,
    max_retries: int = _MAX_RETRIES,
) -> dict[str, Any]:
    """Run a single chunk with bounded retries. Returns parsed JSON.

    ``max_retries=0`` is the right call for single-shot UI flows
    (Agents "Create with AI", workstream context-note generator) where
    the user is staring at a spinner and would rather retry by hand
    than wait through hidden retries. The multi-phase setup wizard
    keeps the default of 2 since each chunk is small and the streamed
    progress hides the wait.
    """
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            raw = await _run_claude_cli(
                container_name, system_prompt, user_prompt, timeout=timeout,
            )
            return _parse_json_response(raw)
        except Exception as exc:
            last_error = exc
            if attempt < max_retries:
                logger.warning(
                    "Chunk failed (attempt %d/%d): %s — retrying...",
                    attempt + 1, max_retries + 1, exc,
                )
                await asyncio.sleep(2)
    raise last_error  # type: ignore[misc]


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

# Keep the legacy single-shot prompt exported for any caller that
# wants the old shape (no live callers today; preserved for
# back-compat). The per-field variant is the one the wizard uses.
_LEGACY_ANALYZE_SYSTEM_PROMPT = ANALYZE_SYSTEM_PROMPT  # noqa: F841


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

def _format_catalog_for_prompt(skill_catalog: list[dict[str, Any]]) -> str:
    """Format the slim catalog metadata for embedding in a prompt.

    Groups by category so the model can scan for relevance fast. Empty
    catalog returns an empty marker so prompts don't render a stray
    header.
    """
    if not skill_catalog:
        return "## Skill Catalog\n(empty — every skill must be authored from scratch)"

    by_category: dict[str, list[dict[str, Any]]] = {}
    for entry in skill_catalog:
        by_category.setdefault(entry.get("category", "Other"), []).append(entry)

    lines = [
        "## Skill Catalog (use ``id`` values in skill_template_ids)",
        "",
        "PREFER these over inventing new skills. Each entry already has a",
        "battle-tested SKILL.md the platform installs intact.",
        "",
    ]
    for category in sorted(by_category):
        lines.append(f"### {category}")
        for entry in sorted(by_category[category], key=lambda e: e["id"]):
            src = entry.get("source", "bundled")
            lines.append(
                f"- ``{entry['id']}`` ({src}) — {entry['display_name']}: "
                f"{entry['description']}"
            )
        lines.append("")
    return "\n".join(lines).rstrip()


def _build_user_prompt(
    office_name: str,
    office_description: str,
    requirements: dict[str, Any],
) -> str:
    parts = [f"Office: '{office_name}'"]
    if office_description:
        parts.append(f"Description: {office_description}")
    if requirements.get("responsibility_areas"):
        parts.append(f"\nResponsibility areas:\n{requirements['responsibility_areas']}")
    if requirements.get("desired_agents"):
        parts.append(f"\nDesired agents:\n{requirements['desired_agents']}")
    if requirements.get("workflows"):
        parts.append(f"\nWorkflows:\n{requirements['workflows']}")
    if requirements.get("additional_context"):
        parts.append(f"\nAdditional context:\n{requirements['additional_context']}")
    return "\n".join(parts)


def _parse_json_response(raw_text: str) -> dict[str, Any]:
    """Parse a Claude JSON response, tolerating common malformations.

    Real-world Claude output frequently arrives with one of:

    * Wrapped in a ```json fence (or a bare ``` fence with no lang).
    * A leading apology paragraph before the JSON object.
    * Trailing commas before ``}`` / ``]`` (allowed in JS, fatal in JSON).
    * Missing commas between adjacent objects in an array — the
      ``Expecting ',' delimiter`` error the setup wizard kept hitting.

    We try the parser in stages, cheapest-first, so a clean response
    still hits the fast path and only malformed payloads pay the
    repair cost. Stages:

    1. Bare ``json.loads``.
    2. Strip code fences and retry.
    3. Slice out the first balanced ``{...}`` block (drops prose
       around it) and retry.
    4. Apply two repair regexes (trailing commas, ``}<ws>{`` →
       ``},{``) and retry.

    If everything fails we raise the LAST exception so the caller sees
    the actual parse error in logs, not a generic "couldn't repair".
    A stdlib-only implementation is deliberate — adding ``json-repair``
    to the deps balloons the agent image without buying much: the four
    rules above cover every Claude failure we've actually seen, and an
    unknown new failure mode should land in a bug ticket, not get
    silently papered over by a magic repair lib.
    """
    text = raw_text.strip()
    # Defence-in-depth for the silent-auth-failure case; ``_run_claude_cli``
    # already raises this for the production path, but a future caller
    # bypassing it would otherwise hit ``json.loads("")`` and get an
    # opaque ``Expecting value: line 1 column 1 (char 0)`` error.
    if not text:
        raise _empty_cli_output_error()
    # Stage 1 — fast path.
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        first_error = exc

    # Stage 2 — strip code fences.
    text = _strip_code_fences(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Stage 3 — extract first balanced {...} block.
    extracted = _extract_first_json_object(text)
    if extracted is not None:
        try:
            return json.loads(extracted)
        except json.JSONDecodeError:
            text = extracted

    # Stage 4 — apply repair regexes.
    repaired = _repair_common_json_errors(text)
    if repaired != text:
        try:
            return json.loads(repaired)
        except json.JSONDecodeError as exc:
            # Re-raise the repaired-stage error rather than the original
            # so the log line points at the failure mode the repair pass
            # couldn't handle. The caller will retry the whole CLI call;
            # this just helps the next debugger.
            raise exc

    raise first_error


def _strip_code_fences(text: str) -> str:
    """Strip ``` fences (with or without a language tag) from a payload.

    Keeps the inside verbatim — earlier versions used ``strip()`` per
    line which silently ate user-content leading whitespace.
    """
    s = text.strip()
    if not s.startswith("```"):
        return s
    try:
        first_nl = s.index("\n")
    except ValueError:
        return s
    last_fence = s.rfind("```")
    if last_fence > first_nl:
        return s[first_nl + 1:last_fence].strip()
    return s[first_nl + 1:].strip()


def _extract_first_json_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` substring, or None.

    Walks the text and counts unescaped braces so a JSON value
    containing literal ``{`` / ``}`` inside a string doesn't unbalance
    the count. Stops at the first depth-0 close. Returns None when no
    balanced block is found.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


# Trailing comma before a closer: ``{"a": 1,}`` / ``[1, 2, 3,]``.
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
# Two adjacent object/array values with no separator. Matches
# "} {" / "} [" / "] {" / "] [" / "} \"" / "] \"" patterns where the
# whitespace can include newlines. We only insert a comma — never
# anything that would change the data type.
_MISSING_COMMA_RE = re.compile(r"([}\]\"])(\s*\n\s*)([{\[\"])")


def _repair_common_json_errors(text: str) -> str:
    """Two cheap regex passes that fix the failure modes we see most.

    Order matters: trailing-comma removal first (a ``,}`` looks like
    a malformed gap to the missing-comma pass otherwise).
    """
    fixed = _TRAILING_COMMA_RE.sub(r"\1", text)
    fixed = _MISSING_COMMA_RE.sub(r"\1,\2\3", fixed)
    return fixed


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
