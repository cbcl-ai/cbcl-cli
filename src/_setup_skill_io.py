"""Skill filesystem write helpers used by AI generation flows.

Extracted from ``setup_generator.py`` (Wave 4 decomposition).
Re-exported from ``setup_generator`` for back-compat.
"""

from __future__ import annotations

import re
from typing import Any


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


async def write_skill_to_workspace(
    fs_handler: Any,
    skill_data: dict[str, Any],
    requested_name: str | None,
) -> str:
    """Save a generated SKILL.md through the protected container Files relay.

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
    from src.utils import validate_name

    raw = (requested_name or str(skill_data.get("name") or "")).strip()
    final_name = _slugify_skill_name(raw)
    validate_name(final_name)

    rel_path = f".claude/skills/{final_name}/SKILL.md"
    result = await fs_handler._dispatch(
        "fs_write",
        {
            "path": rel_path,
            "content": str(skill_data.get("playbook_content") or ""),
        },
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
