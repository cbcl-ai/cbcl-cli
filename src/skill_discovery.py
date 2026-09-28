"""Daemon-host enrichment of the container helper's ``fs_list_skills`` result.

The container Files helper runs as ``python3 -I -S`` without PyYAML, so it
never parses SKILL.md frontmatter (remediation D9). It returns bounded raw
heads (``skill_md_head``); this module parses them with the shared
skill-metadata contract and replaces the heads with a compact ``metadata``
summary before the listing leaves the daemon.

Enrichment must never fail the listing: any unexpected error for one skill
marks that skill ``metadata.status = not_evaluated`` and keeps going.
Output from an office image that predates the contract (no head keys) keeps
its legacy ``display_name``/``description`` values and is marked
``not_evaluated`` so the backend does not trust it for catalog updates.
"""

from __future__ import annotations

import logging
from typing import Any

from src import skill_metadata

logger = logging.getLogger(__name__)

_HEAD_KEYS = ("skill_md_head", "skill_md_head_truncated", "skill_md_size")
_SUMMARY_KEYS = (
    "contract_version",
    "status",
    "native_frontmatter_loaded",
    "label",
    "label_source",
    "description",
    "description_source",
    "listing_truncated",
    "errors",
    "warnings",
)
# Body-derived warnings are meaningless when only a truncated head was read.
_BODY_WARNING_CODES = frozenset({"body_too_long"})
_MAX_ISSUES = 20


def _not_evaluated(reason: str) -> dict[str, Any]:
    return {
        "contract_version": skill_metadata.CONTRACT_VERSION,
        "status": skill_metadata.STATUS_NOT_EVALUATED,
        "native_frontmatter_loaded": None,
        "description_source": "not_evaluated",
        "reason": reason,
        "errors": [],
        "warnings": [],
    }


def _summary(parsed: dict[str, Any], *, truncated: bool) -> dict[str, Any]:
    summary = {key: parsed.get(key) for key in _SUMMARY_KEYS}
    warnings = list(parsed.get("warnings") or [])
    if truncated:
        warnings = [w for w in warnings if w.get("code") not in _BODY_WARNING_CODES]
    summary["warnings"] = warnings[:_MAX_ISSUES]
    summary["errors"] = list(parsed.get("errors") or [])[:_MAX_ISSUES]
    summary["portable_compatible"] = bool(
        (parsed.get("portable") or {}).get("compatible")
    )
    if truncated:
        summary["head_truncated"] = True
    return summary


def enrich_skill(entry: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of one listing entry with ``metadata`` and no raw head."""
    enriched = {key: value for key, value in entry.items() if key not in _HEAD_KEYS}
    name = str(entry.get("name") or "")
    if not any(key in entry for key in _HEAD_KEYS):
        enriched["metadata"] = _not_evaluated(
            "office image predates the skill metadata contract"
        )
        return enriched
    if not entry.get("has_skill_md"):
        enriched["metadata"] = _not_evaluated("no SKILL.md")
        enriched["display_name"] = name
        enriched["description"] = ""
        return enriched
    head = entry.get("skill_md_head")
    truncated = bool(entry.get("skill_md_head_truncated"))
    if not isinstance(head, str):
        enriched["metadata"] = _not_evaluated("SKILL.md head budget exhausted")
        return enriched
    if truncated:
        frontmatter, _body, info = skill_metadata.split_frontmatter(head)
        if frontmatter is None and info.get("unclosed"):
            enriched["metadata"] = _not_evaluated(
                "SKILL.md frontmatter does not fit in the bounded head"
            )
            return enriched
    parsed = skill_metadata.parse_skill_md(head, name)
    enriched["metadata"] = _summary(parsed, truncated=truncated)
    enriched["display_name"] = parsed.get("label") or name
    enriched["description"] = parsed.get("description") or ""
    return enriched


def enrich_discovered_listing(result: Any) -> Any:
    """Enrich a successful ``fs_list_skills`` result; pass errors through."""
    if not isinstance(result, dict) or result.get("error"):
        return result
    skills = result.get("skills")
    if not isinstance(skills, list):
        return result
    enriched: list[Any] = []
    for entry in skills:
        if not isinstance(entry, dict):
            continue
        try:
            enriched.append(enrich_skill(entry))
        except Exception:  # never fail the listing for one skill
            logger.warning(
                "skill discovery enrichment failed for %r",
                entry.get("name"),
                exc_info=True,
            )
            fallback = {k: v for k, v in entry.items() if k not in _HEAD_KEYS}
            fallback["metadata"] = _not_evaluated("metadata enrichment failed")
            enriched.append(fallback)
    return {**result, "skills": enriched}
