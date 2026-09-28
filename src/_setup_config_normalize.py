"""Type-safe normalization of a setup-wizard config before it is published.

The wizard's generate and improve passes assemble a config from several
independent model responses. The backend re-validates that config on every
poll and at apply, so one mistyped field (a JSON ``null`` system prompt, a
bare-object ``parameter_schema``, a missing display name) used to turn a
finished multi-minute generation into a permanent "failed" status (X25), and
names that skipped the agent-name rules broke the atomic apply (X27).

Everything here is pure (no I/O, no model calls) and idempotent, so the
generate path, the improve path and the tests share one implementation.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from typing import Any

from ._setup_skill_render import normalize_parameter_schema

logger = logging.getLogger(__name__)

# Backend ``agents/schemas.py:_AGENT_NAME_RE`` — the rule every REST create
# path enforces; the String(100) column bounds the length.
_AGENT_NAME_MAX = 100
_AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_DEFAULT_AVATAR = "\U0001f916"
_STRING_AGENT_FIELDS = (
    "display_name",
    "role_description",
    "system_prompt",
    "claude_md_content",
    "model",
)
_LIST_AGENT_FIELDS = ("allowed_tools", "skill_names", "skill_template_ids")


# C4d-G6: a skill the wizard could not author is dropped and unlinked from
# its agents, but those agents' prompts (written in parallel) may still
# name it. ``generation_warnings`` tells the user on the Review step. The
# key is additive: older backends and frontends ignore it, and apply-config
# never persists it.
GENERATION_WARNINGS_KEY = "generation_warnings"
_GENERATION_WARNINGS_MAX = 10
_GENERATION_WARNING_MAX_CHARS = 300
_SKILL_WARNING_RE = re.compile(r"^Skill `([^`]+)`")


def _join_names(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def skill_generation_warning(slug: str, agent_names: Iterable[str]) -> str:
    """One Review-step warning for a skill that could not be authored.

    Always at most ``_GENERATION_WARNING_MAX_CHARS`` characters without
    cutting a word: when the named list does not fit, the agents are
    counted instead of named.
    """
    names = list(dict.fromkeys(n for n in agent_names if n))
    if not names:
        return (
            f"Skill `{slug}` could not be generated and was left out of the "
            "office. Add it after setup if you need it."
        )
    tail = (
        ". Their instructions may still mention it: edit them, or add the "
        "skill after setup."
        if len(names) > 1
        else ". Its instructions may still mention it: edit them, or add the "
        "skill after setup."
    )
    head = f"Skill `{slug}` could not be generated, so it was not assigned to "
    text = head + _join_names(names) + tail
    if len(text) <= _GENERATION_WARNING_MAX_CHARS:
        return text
    return head + f"{len(names)} agents" + tail


def _generation_warnings(
    prior: object,
    dropped: dict[str, list[str]],
    kept_skill_names: set[str],
) -> list[str]:
    """Carry prior warnings forward (minus skills now present) + new drops."""
    out: list[str] = []
    candidates: list[str] = [
        w for w in (prior if isinstance(prior, list) else []) if isinstance(w, str)
    ]
    candidates += [
        skill_generation_warning(slug, names) for slug, names in dropped.items()
    ]
    for warning in candidates:
        text = warning.strip()
        match = _SKILL_WARNING_RE.match(text)
        if match and match.group(1) in kept_skill_names:
            continue  # the skill exists now; the warning is stale
        if not text or len(text) > _GENERATION_WARNING_MAX_CHARS or text in out:
            continue
        out.append(text)
        if len(out) >= _GENERATION_WARNINGS_MAX:
            break
    return out


def agent_slug(raw: object) -> str:
    """Slugify an agent name to the backend's ``_AGENT_NAME_RE`` shape.

    Lowercase; any character outside ``[a-z0-9._-]`` becomes ``-``; runs of
    separators collapse; leading/trailing separators are removed and the
    result is clamped to 100 characters. Runs of dots collapse to one,
    because the backend refuses ``..`` in an agent name. Returns ``""`` when
    nothing usable remains (the caller drops that agent). Mirrors the
    backend's ``setup_content.generated_agent_slug``;
    ``tests/test_agent_slug_parity.py`` pins the pair.
    """
    if not isinstance(raw, str):
        return ""
    slug = re.sub(r"[^a-z0-9._-]+", "-", raw.strip().lower())
    slug = re.sub(r"-{2,}", "-", slug)
    slug = re.sub(r"\.{2,}", ".", slug)
    slug = slug.strip("-._")
    slug = slug[:_AGENT_NAME_MAX].rstrip("-._")
    return slug if _AGENT_NAME_RE.fullmatch(slug) and ".." not in slug else ""


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _as_str_list(value: object) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item.strip() not in out:
            out.append(item.strip())
    return out


def harden_roster(
    agents: Iterable[object],
    *,
    reserved: frozenset[str],
    normalize_tools: Callable[[object], list[str]],
) -> list[dict[str, Any]]:
    """Slugify, dedupe and filter a generated roster (X27).

    Drops non-object entries, names that slugify to nothing and names that
    collide with a system agent (``reserved``); a later duplicate slug is
    dropped so the backend's ``UNIQUE(office_id, name)`` can never fail the
    whole atomic apply. ``normalize_tools`` filters ``allowed_tools`` to the
    standard CLI tool names (``_setup_cli._normalize_allowed_tools``).
    """
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in agents:
        if not isinstance(entry, dict):
            continue
        slug = agent_slug(entry.get("name"))
        if not slug or slug in reserved:
            logger.warning(
                "Setup roster: dropping invalid/system agent name %r",
                entry.get("name"),
            )
            continue
        if slug in seen:
            logger.warning("Setup roster: dropping duplicate agent slug %r", slug)
            continue
        seen.add(slug)
        agent = dict(entry)
        agent["name"] = slug
        agent["allowed_tools"] = normalize_tools(agent.get("allowed_tools"))
        cleaned.append(agent)
    return cleaned


def normalize_agent(entry: dict[str, Any]) -> dict[str, Any]:
    """Coerce one generated agent's field types (never drops content)."""
    agent = dict(entry)
    agent["name"] = _as_text(agent.get("name")).strip()
    for field in _STRING_AGENT_FIELDS:
        agent[field] = _as_text(agent.get(field))
    if not agent["display_name"].strip():
        agent["display_name"] = agent["name"].replace("-", " ").title() or "Agent"
    avatar = agent.get("avatar_emoji")
    agent["avatar_emoji"] = (
        avatar if isinstance(avatar, str) and avatar.strip() else _DEFAULT_AVATAR
    )
    for field in _LIST_AGENT_FIELDS:
        agent[field] = _as_str_list(agent.get(field))
    if "effort" in agent and not isinstance(agent["effort"], str):
        del agent["effort"]
    return agent


def normalize_generated_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return a type-safe copy of a wizard config (X25).

    * ``instructions`` / ``vision`` become strings (a JSON ``null`` never
      reaches the backend's ``str`` fields).
    * every agent gets string fields, a display name, an avatar and list
      fields made of strings;
    * every skill gets a normalized ``parameter_schema``; a skill without a
      usable name or playbook is DROPPED and its slug pruned from the
      agents' ``skill_names`` (the same posture as a failed skill phase —
      an empty SKILL.md is never persisted);
    * ``skill_templates_to_install`` / ``source_warnings`` become string
      lists;
    * ``generation_warnings`` names every dropped skill and the agents it
      was unassigned from (C4d-G6), keeps prior warnings whose skill is
      still missing, and drops those whose skill now exists.
    """
    out = dict(config)
    out["instructions"] = _as_text(config.get("instructions"))
    out["vision"] = _as_text(config.get("vision"))

    skills: list[dict[str, Any]] = []
    kept_skill_names: set[str] = set()
    dropped_skill_names: set[str] = set()
    for entry in config.get("skills") or []:
        if not isinstance(entry, dict):
            continue
        skill = dict(entry)
        name = _as_text(skill.get("name")).strip()
        display_name = _as_text(skill.get("display_name")).strip()
        playbook = _as_text(skill.get("playbook_content"))
        if not (name or display_name) or not playbook.strip():
            if name:
                dropped_skill_names.add(name)
            logger.warning(
                "Setup config: dropping generated skill %r without a playbook",
                name or display_name,
            )
            continue
        skill["name"] = name or None
        skill["display_name"] = display_name or name.replace("-", " ").title()
        description = skill.get("description")
        skill["description"] = (
            _as_text(description) if description is not None else None
        )
        skill["playbook_content"] = playbook
        skill["parameter_schema"] = normalize_parameter_schema(
            skill.get("parameter_schema")
        )
        skill.pop("body", None)
        skills.append(skill)
        if name:
            kept_skill_names.add(name)
    out["skills"] = skills

    agents: list[dict[str, Any]] = []
    unassigned: dict[str, list[str]] = {
        name: [] for name in sorted(dropped_skill_names - kept_skill_names)
    }
    for entry in config.get("agents") or []:
        if not isinstance(entry, dict):
            continue
        agent = normalize_agent(entry)
        if dropped_skill_names:
            for name in agent["skill_names"]:
                if name in unassigned:
                    unassigned[name].append(agent["display_name"])
            agent["skill_names"] = [
                s
                for s in agent["skill_names"]
                if s not in dropped_skill_names or s in kept_skill_names
            ]
        agents.append(agent)
    out["agents"] = agents
    out[GENERATION_WARNINGS_KEY] = _generation_warnings(
        config.get(GENERATION_WARNINGS_KEY), unassigned, kept_skill_names
    )

    out["skill_templates_to_install"] = _as_str_list(
        config.get("skill_templates_to_install")
    )
    warnings = config.get("source_warnings")
    out["source_warnings"] = (
        [w for w in warnings if isinstance(w, str)]
        if isinstance(warnings, list)
        else []
    )
    if "flows" in config:
        flows = config.get("flows")
        out["flows"] = (
            [f for f in flows if isinstance(f, dict)] if isinstance(flows, list) else []
        )
    return out
