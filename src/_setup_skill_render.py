"""Canonical SKILL.md rendering for AI-generated skills (F08).

The skill-generation prompts ask the model for METADATA plus a markdown
``body`` with no frontmatter; the platform renders SKILL.md itself through
the shared ``skill_metadata`` contract (the ONLY place that parses,
validates or renders SKILL.md frontmatter — D9). This module is the
compatibility adapter every generation path funnels through:

* a new-contract response (``body``) is rendered with ``render_skill_md``;
* a legacy response (``playbook_content`` written by an older prompt, with
  model-authored — possibly invalid — YAML) keeps its body verbatim and gets
  canonical frontmatter whose ``name`` is the slug of record.

Pure functions: no I/O, no model calls.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ._setup_skill_io import SKILL_SLUG_MAX, _slugify_skill_name, skill_slug_unclamped
from .skill_metadata import (
    NATIVE_KEYS,
    PORTABLE_DESCRIPTION_CAP,
    STATUS_OK,
    effective_description,
    normalize_whitespace,
    parse_skill_md,
    render_frontmatter,
    render_skill_md,
    split_frontmatter,
)

# The standard Claude CLI tool names generation may assign: a generated
# skill's ``allowed-tools`` frontmatter and a generated agent's
# ``allowed_tools`` (``_setup_cli`` imports this one set). Defined here so
# this module stays import-light and pure.
STANDARD_TOOL_NAMES = frozenset(
    {"Read", "Write", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"}
)

# Parameter-entry caps — the backend ``SkillParameterSchema`` column
# widths, so a normalized entry always validates there.
_PARAM_NAME_MAX = 255
_PARAM_TYPE_MAX = 50
_PARAM_DESCRIPTION_MAX = 5000
_PARAM_DEFAULT_MAX = 5000


class SkillRenderError(ValueError):
    """A generated skill has no usable playbook body."""


# An unindented, lowercase native SKILL.md key and its colon at the start of
# a line — how genuine frontmatter opens even when the model broke its YAML
# (typically an unquoted colon in ``description``). Longest keys first so
# ``allowed-tools`` is never read as a shorter key.
_NATIVE_KEY_LINE_RE = re.compile(
    "(?:"
    + "|".join(re.escape(key) for key in sorted(NATIVE_KEYS, key=len, reverse=True))
    + r")[ \t]*:(?:[ \t]|$)"
)
_DESCRIPTION_LINE_RE = re.compile(r"description[ \t]*:[ \t]*(.*?)[ \t]*$")


def skill_slug_of_record(raw: object) -> str:
    """Slugify and clamp a generated skill name to 64 characters.

    The daemon clamps here so the roster, the agent links and the rendered
    frontmatter ``name`` all agree before the config ever leaves the daemon.
    Mirrors the backend's ``generated_skill_slug``, with ``new-skill`` in
    place of its empty result.
    """
    return _slugify_skill_name(raw if isinstance(raw, str) else "")


def skill_merge_key(raw: object) -> str:
    """Identity of a generated skill name: its FULL (unclamped) slug.

    Two long names sharing their first 64 characters stay distinct. A name
    with no usable slug characters (for example a non-Latin name) keys on
    its whitespace-normalized text behind a NUL sentinel, so distinct names
    never share one key and none can equal a real slug such as
    ``new-skill``. Blank or non-string input has the empty key. The slug
    allocator and the improve merge both use this one rule.
    """
    if not isinstance(raw, str) or not raw.strip():
        return ""
    text = raw.strip()
    return skill_slug_unclamped(text) or "\x00" + normalize_whitespace(text).casefold()


class SkillSlugAllocator:
    """Assign each distinct generated skill name its own <=64-char slug.

    Clamping alone would merge two long names that share their first 64
    characters into one skill — the second skill's content silently lost.
    The allocator keys on the FULL (unclamped) slug: the same name always
    gets the same slug (agents referencing it stay linked), and a different
    name whose clamped slug is already taken gets a ``-2``/``-3`` suffix
    (still within 64 characters).
    """

    def __init__(self) -> None:
        self._by_full: dict[str, str] = {}
        self._used: set[str] = set()

    def slug_for(self, raw: object) -> str:
        """Allocate (or return the already-allocated) slug for ``raw``."""
        text = raw.strip() if isinstance(raw, str) else ""
        full = skill_merge_key(text)
        if full in self._by_full:
            return self._by_full[full]
        base = skill_slug_of_record(text)
        candidate = base
        counter = 2
        while candidate in self._used:
            suffix = f"-{counter}"
            candidate = base[: SKILL_SLUG_MAX - len(suffix)].rstrip("-") + suffix
            counter += 1
        self._by_full[full] = candidate
        self._used.add(candidate)
        return candidate

    def resolve(self, raw: object) -> str:
        """The allocated slug for ``raw`` if known, else its clamped slug.

        Never allocates: used for agent references that may name a
        catalog or pre-existing skill rather than a generated one.
        """
        text = raw.strip() if isinstance(raw, str) else ""
        return self._by_full.get(skill_merge_key(text)) or skill_slug_of_record(text)


def _clean_tools(raw: object) -> list[str]:
    """Filter a model ``allowed_tools`` value to known standard tool names.

    Order-preserving and deduplicated; anything else (hallucinated tool
    names, non-strings, a bare string) is dropped.
    """
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.replace(",", " ").split()]
    if not isinstance(raw, list):
        return []
    tools: list[str] = []
    for item in raw:
        if isinstance(item, str) and item in STANDARD_TOOL_NAMES and item not in tools:
            tools.append(item)
    return tools


def _fallback_description(body: str, slug: str, display_name: str | None) -> str:
    parsed = parse_skill_md(body, slug)
    derived = effective_description(parsed, PORTABLE_DESCRIPTION_CAP)
    if derived and derived.lstrip("#").strip():
        return derived.lstrip("#").strip()
    label = normalize_whitespace(display_name or "") or slug
    return f"{label} playbook."


def clean_description(value: object) -> str:
    """Whitespace-normalize and cap a model description (1,024 chars)."""
    if not isinstance(value, str):
        return ""
    text = normalize_whitespace(value)
    if len(text) > PORTABLE_DESCRIPTION_CAP:
        text = text[: PORTABLE_DESCRIPTION_CAP - 1].rstrip() + "…"
    return text


def canonical_skill_markdown(
    slug: str,
    *,
    description: object = None,
    display_name: str | None = None,
    body: object = None,
    playbook_content: object = None,
    allowed_tools: object = None,
) -> tuple[str, str]:
    """Return ``(skill_md, description)`` for a generated skill.

    ``body`` (the new contract) wins over ``playbook_content`` (legacy).
    The description is the model's, normalized and capped; when the model
    gave none it falls back to the body's first line, then the display
    name. Raises :class:`SkillRenderError` when neither field carries a
    non-empty playbook — the caller treats that skill as failed instead of
    persisting an empty SKILL.md.
    """
    desc = clean_description(description)
    tools = _clean_tools(allowed_tools)
    text: str | None = None
    if isinstance(body, str) and body.strip():
        text = body
    elif isinstance(playbook_content, str) and playbook_content.strip():
        text = playbook_content
    if text is None:
        raise SkillRenderError(f"generated skill {slug!r} has an empty playbook")

    frontmatter, rest, _info = split_frontmatter(text)
    if frontmatter is not None:
        parsed = parse_skill_md(text, slug)
        if _is_skill_frontmatter(frontmatter, parsed):
            # Legacy (or a disobedient new-contract body): keep the body
            # byte for byte, keep a valid frontmatter description /
            # allowed-tools when the model gave none, and re-render
            # canonical frontmatter so ``name`` is the slug of record and
            # the YAML always parses.
            if not desc and parsed["status"] == STATUS_OK:
                desc = (
                    effective_description(
                        parsed, PORTABLE_DESCRIPTION_CAP, frontmatter_only=True
                    )
                    or ""
                )
            elif not desc:
                desc = _salvage_description(frontmatter)
            if not tools:
                tools = _clean_tools(parsed["fields"].get("allowed-tools"))
            text = rest
        # Otherwise the leading ``---`` block is not skill metadata (a
        # non-mapping, or content that does not open with a SKILL.md key —
        # e.g. a horizontal-rule-wrapped title and rules). It is playbook
        # content: the whole text stays the body (CM5).
    if not text.strip():
        raise SkillRenderError(f"generated skill {slug!r} has an empty playbook")
    if not desc:
        desc = _fallback_description(text, slug, display_name)
    extra = {"allowed-tools": tools} if tools else None
    if text.lstrip("\ufeff").lstrip("\r\n").startswith("---"):
        # The body itself opens with a ``---`` line (an unclosed opener, a
        # non-metadata block, or a second block after the stripped one).
        # It stays body text verbatim behind the canonical block.
        return _render_before_opening_rule(slug, desc, text, extra), desc
    return render_skill_md(slug, desc, text, extra), desc


def _is_skill_frontmatter(frontmatter: str, parsed: dict[str, Any]) -> bool:
    """Whether a leading ``---`` block is SKILL.md metadata to replace.

    True for an empty block, for one that parses as a YAML mapping carrying
    at least one native SKILL.md key (``name``, ``description``,
    ``allowed-tools`` …), and for genuine but broken frontmatter: a block
    whose first non-blank, non-comment line is an unindented native key and
    its colon (``description: Reviews contracts: use when …`` is invalid
    YAML, yet plainly metadata — kept as body it would read as a stale
    duplicate instruction block). Anything else — a list or scalar, YAML
    comments only, or unrelated keys such as a rule-wrapped title — is
    playbook text the model wrapped in rules, and must not be discarded.
    """
    if not frontmatter.strip():
        return True
    if parsed["status"] == STATUS_OK and parsed["fields"]:
        return True
    first = _first_content_line(frontmatter)
    return first is not None and _NATIVE_KEY_LINE_RE.match(first) is not None


def _first_content_line(frontmatter: str) -> str | None:
    """The first line that is neither blank nor a YAML comment, unstripped."""
    for line in frontmatter.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            return line.rstrip("\r")
    return None


def _salvage_description(frontmatter: str) -> str:
    """A single-line ``description:`` value from broken frontmatter.

    Used only when the block did not parse, so the model's own summary is
    not replaced by a body-derived fallback. Surrounding matching quotes
    are removed; a block scalar indicator (``>``, ``|``) yields nothing.
    """
    for line in frontmatter.splitlines():
        match = _DESCRIPTION_LINE_RE.fullmatch(line.rstrip("\r"))
        if match is None:
            continue
        value = match.group(1)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not value or value[0] in ">|":
            return ""
        return clean_description(value)
    return ""


def _render_before_opening_rule(
    slug: str, description: str, body: str, extra: dict[str, Any] | None
) -> str:
    """Canonical frontmatter in front of a body that opens with ``---``.

    ``render_skill_md`` refuses such a body (it would read as frontmatter);
    a blank line after the canonical block keeps the body's own ``---``
    line as body text, byte for byte.
    """
    mapping: dict[str, Any] = {
        "name": slug,
        "description": normalize_whitespace(description),
        **(extra or {}),
    }
    text = body.lstrip("\ufeff").lstrip("\r\n")
    if not text.endswith("\n"):
        text += "\n"
    return render_frontmatter(mapping) + "\n" + text


def normalize_parameter_schema(raw: object) -> list[dict[str, Any]]:
    """Coerce a model ``parameter_schema`` to valid entries (X23/X25).

    Follows the backend's generated-schema rules
    (``app/skills/parameters.normalize_generated_parameter_schema``;
    ``tests/test_parameter_schema_parity.py`` pins the two together): wraps a
    bare object in a list; drops non-object, nameless, duplicate and
    over-long-named entries (a cut name would be a different identifier);
    reads ``is_secret`` string and number spellings (``"true"``, ``"yes"``,
    ``1``) as true, so a secret is never mistaken for a plain value; falls
    back to type ``string`` for a missing or over-long type; and stringifies
    a typed ``default_value`` the way the backend stores it (booleans as
    ``true``/``false``, containers as compact JSON). One mistyped entry never
    fails a finished generation or the office's Skills list.
    """
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        if len(name) > _PARAM_NAME_MAX or name in seen:
            continue
        seen.add(name)
        ptype = item.get("type")
        ptype = ptype.strip() if isinstance(ptype, str) and ptype.strip() else "string"
        if len(ptype) > _PARAM_TYPE_MAX:
            ptype = "string"
        description = item.get("description")
        description = (
            description.strip()[:_PARAM_DESCRIPTION_MAX]
            if isinstance(description, str) and description.strip()
            else None
        )
        entries.append(
            {
                "name": name,
                "type": ptype,
                "is_secret": _coerce_secret_flag(item.get("is_secret")),
                "description": description,
                "default_value": _stringify_default(item.get("default_value")),
            }
        )
    return entries


_TRUE_WORDS = frozenset({"true", "yes", "1", "on"})


def _coerce_secret_flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return isinstance(value, str) and value.strip().lower() in _TRUE_WORDS


def _stringify_default(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, str):
        text = value
    elif isinstance(value, (int, float)):
        text = str(value)
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(value)
    return text[:_PARAM_DEFAULT_MAX]
