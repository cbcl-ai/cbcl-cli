"""Skill filesystem write helpers used by AI generation flows.

Extracted from ``setup_generator.py`` (Wave 4 decomposition).
Re-exported from ``setup_generator`` for back-compat.
"""

from __future__ import annotations

import re
from pathlib import Path
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

    from src._chown import chown_to_agent
    from src.fs_handler import _collect_new_parents

    rel_path = f".claude/skills/{final_name}/SKILL.md"
    full_path = _safe_resolve(workspace, rel_path)
    # Chown each new parent directory the mkdir is about to create
    # PLUS the SKILL.md file itself. Mirrors fs_handler._write so
    # AI-generated skill files end up agent-writable for subsequent
    # in-container Edit operations — without this an agent that
    # uses generate-skill cannot later refine its own playbook via
    # the standard file-editing tools.
    new_parents = _collect_new_parents(full_path.parent, workspace)
    full_path.parent.mkdir(parents=True, exist_ok=True)
    for parent in new_parents:
        chown_to_agent(parent)
    full_path.write_text(str(skill_data.get("playbook_content") or ""))
    chown_to_agent(full_path)
    return rel_path


