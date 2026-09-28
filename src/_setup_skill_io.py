"""Skill filesystem write helpers used by AI generation flows.

Extracted from ``setup_generator.py`` (Wave 4 decomposition).
Re-exported from ``setup_generator`` for back-compat.
"""

from __future__ import annotations

import re
from typing import Any


# Claude Code / Agent Skills cap a skill ``name`` at 64 characters, and the
# slug of record is also the directory (``.claude/skills/<slug>/``) and the
# backend row name. Same limit as ``backend/app/skills/slug_rules.py``.
SKILL_SLUG_MAX = 64


def skill_slug_unclamped(raw: str) -> str:
    """The skill slug rule, before the 64-character limit (``""`` if empty).

    Byte-for-byte the backend's ``app/core/utils.slugify`` (and the
    frontend's ``lib/utils.ts`` ``slugify``): lowercase, whitespace and
    underscores become ``-``, every other character outside ``[a-z0-9-]``
    is DROPPED (``"Q&A"`` → ``qa``, ``"Data.Curator_2"`` →
    ``datacurator-2``), runs of ``-`` collapse and edge hyphens are
    trimmed. Pinned against the backend by the shared
    ``tests/fixtures/skill_slug_cases.json`` (T22).
    """
    value = raw.lower()
    value = re.sub(r"[\s_]+", "-", value)
    value = re.sub(r"[^a-z0-9\-]", "", value)
    value = re.sub(r"-{2,}", "-", value)
    return value.strip("-")


def derived_skill_slug(raw: object) -> str:
    """Mirror of the backend's ``slug_rules.derived_skill_slug``.

    :func:`skill_slug_unclamped` of the stripped input, at most
    :data:`SKILL_SLUG_MAX` characters with edge hyphens removed; ``""`` when
    nothing usable remains (non-strings included).
    """
    if not isinstance(raw, str):
        return ""
    return skill_slug_unclamped(raw.strip())[:SKILL_SLUG_MAX].strip("-")


def _slugify_skill_name(raw: str) -> str:
    """Slug for the SKILL.md filesystem layout — NEVER returns "office".

    ``paths.slugify`` falls back to the workspace-naming default
    ``"office"`` for any input that collapses to an empty string; for skills
    that would land every empty-slug skill at ``.claude/skills/office/`` and
    let the second one overwrite the first. The skill-domain fallback is
    ``"new-skill"`` instead. Otherwise this is :func:`derived_skill_slug`:
    the backend rule, clamped to 64 characters (C10/T22).
    """
    return derived_skill_slug(raw) or "new-skill"


class SkillAlreadyExistsError(FileExistsError):
    """A create-only write found an existing SKILL.md; nothing was changed."""


def resolve_skill_path(skill_data: dict[str, Any], requested_name: str | None) -> str:
    """The workspace-relative SKILL.md path for a generated skill.

    User-typed (backend-pinned) name wins, model echo is the fallback,
    ``"new-skill"`` the last resort. Raises ``ValueError`` when the final
    slug is rejected by :func:`validate_name`.
    """
    from src.utils import validate_name

    raw = (requested_name or str(skill_data.get("name") or "")).strip()
    final_name = _slugify_skill_name(raw)
    validate_name(final_name)
    return f".claude/skills/{final_name}/SKILL.md"


def _status(result: Any) -> int | None:
    if isinstance(result, dict) and result.get("error"):
        try:
            return int(result.get("status", 500))
        except (TypeError, ValueError):
            return 500
    return None


async def _create_only(fs_handler: Any, rel_path: str, content: str) -> None:
    """Exclusive create: never replaces an existing SKILL.md.

    Uses the helper's compare-and-swap ``fs_write_revision`` (atomic inside
    the workspace lock). An office image that predates it answers 426; the
    fallback then reads first and writes only after a definitive 404 — a
    narrow window, but never a blind overwrite.
    """
    result = await fs_handler._dispatch(
        "fs_write_revision",
        {"path": rel_path, "content": content, "expect_absent": True},
    )
    status = _status(result)
    if status is None:
        if result.get("path") != rel_path:
            raise OSError(
                "The protected Files helper could not save the generated SKILL.md."
            )
        return
    if status == 409:
        raise SkillAlreadyExistsError(rel_path)
    if status != 426 and result.get("code") != "office_image_upgrade_required":
        raise OSError(
            "The protected Files helper could not save the generated SKILL.md."
        )
    probe = await fs_handler._dispatch("fs_read", {"path": rel_path})
    probe_status = _status(probe)
    if probe_status is None:
        raise SkillAlreadyExistsError(rel_path)
    if probe_status != 404:
        raise OSError("Could not confirm that the SKILL.md path is free.")
    written = await fs_handler._dispatch(
        "fs_write", {"path": rel_path, "content": content}
    )
    if _status(written) is not None or written.get("path") != rel_path:
        raise OSError(
            "The protected Files helper could not save the generated SKILL.md."
        )


async def write_skill_to_workspace(
    fs_handler: Any,
    skill_data: dict[str, Any],
    requested_name: str | None,
    *,
    create_only: bool = False,
) -> str:
    """Save a generated SKILL.md through the protected container Files relay.

    Sibling of :func:`generate_skill_from_overview`. The slug resolution
    chain mirrors the backend: user-typed (pinned) name wins, model echo is
    fallback, ``"new-skill"`` is the last-resort default.

    ``create_only`` (sent by current backends, X12/X29) makes the write
    exclusive: an existing SKILL.md raises :class:`SkillAlreadyExistsError`
    and is left untouched. Without it the legacy replace-write is kept for
    older backends and the hire chain.

    Raises ``ValueError`` if the final slug is rejected by
    :func:`validate_name`; ``OSError`` for helper failures.

    Returns the workspace-relative path (e.g.
    ``.claude/skills/my-skill/SKILL.md``).
    """
    rel_path = resolve_skill_path(skill_data, requested_name)
    content = str(skill_data.get("playbook_content") or "")
    if create_only:
        await _create_only(fs_handler, rel_path, content)
        return rel_path
    result = await fs_handler._dispatch(
        "fs_write",
        {"path": rel_path, "content": content},
    )
    if (
        not isinstance(result, dict)
        or result.get("error")
        or result.get("path") != rel_path
    ):
        raise OSError(
            "The protected Files helper could not save the generated SKILL.md."
        )
    return rel_path
