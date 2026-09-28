"""Skill metadata contract v1 — the ONE parser/validator/renderer for SKILL.md.

KEEP BYTE-IDENTICAL: this file exists twice,

* ``communicator/src/skill_metadata.py`` (canonical copy, daemon host process)
* ``backend/app/skills/skill_metadata.py`` (backend copy)

and ``communicator/tests/test_skill_metadata_parity.py`` fails when the two
differ. Both suites run the same shared fixture file
(``tests/fixtures/skill_metadata_cases.json`` in each package, also kept
byte-identical). Edit the communicator copy, then copy it verbatim.

Dependencies: the standard library and PyYAML only. The office container's
Files helper runs as ``python3 -I -S`` (no site-packages, so no PyYAML); it
must never parse YAML. It returns bounded raw text and parsing happens here,
on the daemon host or in the backend.

What the contract decides
-------------------------
A ``SKILL.md`` may start with a YAML frontmatter block. The opening ``---``
must be the file's first line (a leading UTF-8 BOM is tolerated with a
warning; trailing spaces/tabs on a delimiter line are allowed). The block
closes at the first later line that is ``---`` (again allowing trailing
spaces/tabs); ``----``, ``---x`` and `` ---`` do not close it. CRLF is
handled. Without a closing line the whole file, markers included, is body
text — which is how Claude Code treats it.

The block is parsed by a strict ``yaml.SafeLoader`` subclass:

* every plain scalar stays a string (``yes``, ``1``, ``2024-01-01`` keep
  their text); only an empty value or ``~``/``null`` resolves to null;
* duplicate keys and non-string keys are errors (reported with line numbers);
* anchors, aliases and explicit tags are refused, and node count/depth are
  capped — such blocks are reported ``not_evaluated`` because what the
  native parser would load is unknown, never guessed;
* frontmatter larger than ``MAX_FRONTMATTER_BYTES`` is ``not_evaluated``.

Two targets
-----------
``native_cubicle`` — what Claude Code (the CLI inside the office container)
loads, plus Cubicle's own requirements. Claude Code documents: the ``name``
field is only a display label (default: the directory name);
``description`` falls back to the first non-empty markdown line; the
``when_to_use`` text is appended to the description and the combined listing
text is truncated at 1,536 characters; boolean fields accept
yes/no/on/off/true/false/1/0 in any letter case; when the YAML does not
parse the skill loads with NO fields set; unknown keys are ignored. The
contract mirrors that: invalid or duplicate-key YAML yields status
``invalid_yaml`` and the *effective* label/description fall back to the
directory name and the first body line — exactly what the CLI shows.

``portable`` — claude.ai uploads / the Skills API. Advisory only here:
``name`` ≤64 chars of ``[a-z0-9-]`` without "anthropic"/"claude", no XML
tags, ``description`` ≤1,024 chars, and only the keys ``name``,
``description``, ``license``, ``compatibility``, ``metadata`` and
``allowed-tools``.

Public API (all results are JSON-safe)
--------------------------------------
``split_frontmatter`` — delimiter detection only (no YAML).
``parse_skill_md`` — full parse: status, effective label/description,
typed native fields, unknown keys, errors, warnings, portable advice.
``validate`` — blocking errors vs warnings for a target. Errors block NEW
platform-authored content; for existing user files callers surface them as
warnings and never block reads or sync.
``render_frontmatter`` / ``render_skill_md`` — the only way platform-authored
content gets frontmatter (``yaml.safe_dump``, verified to round-trip).
``ensure_frontmatter`` — repair for PLATFORM-AUTHORED content only.
``set_frontmatter_description`` — targeted edit of the description value
span, preserving comments, key order and every other byte.
``effective_description`` — the normalized, capped description to store in
the catalog/DB. ``extract_link_targets`` — relative markdown links in a body.

Truthfulness rule: nothing here guesses. Unknown native behaviour is
``not_evaluated`` (``native_frontmatter_loaded`` is then ``None``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

import yaml

__all__ = [
    "CONTRACT_VERSION",
    "DEFAULT_CATALOG_DESCRIPTION_CAP",
    "LISTING_DESCRIPTION_CAP",
    "MAX_FRONTMATTER_BYTES",
    "NATIVE_KEYS",
    "PORTABLE_DESCRIPTION_CAP",
    "PORTABLE_KEYS",
    "STATUS_INVALID_YAML",
    "STATUS_NOT_EVALUATED",
    "STATUS_NOT_MAPPING",
    "STATUS_NO_FRONTMATTER",
    "STATUS_OK",
    "TARGET_NATIVE_CUBICLE",
    "TARGET_PORTABLE",
    "FrontmatterEditError",
    "StrictMetadataLoader",
    "effective_description",
    "ensure_frontmatter",
    "extract_link_targets",
    "normalize_whitespace",
    "parse_skill_md",
    "render_frontmatter",
    "render_skill_md",
    "set_frontmatter_description",
    "split_frontmatter",
    "validate",
]

CONTRACT_VERSION = 1

MAX_FRONTMATTER_BYTES = 32 * 1024
MAX_YAML_NODES = 512
MAX_YAML_DEPTH = 32
LISTING_DESCRIPTION_CAP = 1536
PORTABLE_DESCRIPTION_CAP = 1024
PORTABLE_NAME_CAP = 64
PORTABLE_COMPATIBILITY_CAP = 500
BODY_LINE_ADVISORY_LIMIT = 500
SHORT_DESCRIPTION_CHARS = 40
DEFAULT_CATALOG_DESCRIPTION_CAP = 1024
_DUMP_WIDTH = 10**9

STATUS_OK = "ok"
STATUS_NO_FRONTMATTER = "no_frontmatter"
STATUS_INVALID_YAML = "invalid_yaml"
STATUS_NOT_MAPPING = "not_mapping"
STATUS_NOT_EVALUATED = "not_evaluated"

TARGET_NATIVE_CUBICLE = "native_cubicle"
TARGET_PORTABLE = "portable"
TARGETS = (TARGET_NATIVE_CUBICLE, TARGET_PORTABLE)

# Claude Code native frontmatter keys grouped by accepted value type.
STRING_KEYS = (
    "name",
    "description",
    "when_to_use",
    "argument-hint",
    "model",
    "effort",
    "context",
    "agent",
    "shell",
    "license",
    "compatibility",
)
BOOLEAN_KEYS = ("disable-model-invocation", "user-invocable", "background")
LIST_OR_STRING_KEYS = ("allowed-tools", "disallowed-tools", "paths", "arguments")
MAPPING_KEYS = ("hooks", "metadata")
NATIVE_KEYS = frozenset(STRING_KEYS + BOOLEAN_KEYS + LIST_OR_STRING_KEYS + MAPPING_KEYS)
PORTABLE_KEYS = frozenset(
    ("name", "description", "license", "compatibility", "metadata", "allowed-tools")
)

_TRUE_WORDS = frozenset(("true", "yes", "on", "1"))
_FALSE_WORDS = frozenset(("false", "no", "off", "0"))

_DELIMITER_RE = re.compile(r"---[ \t]*")
_HEADING_RE = re.compile(r"#{1,6}[ \t]+(.*?)[ \t#]*$")
_XML_TAG_RE = re.compile(r"<\s*/?\s*[A-Za-z][A-Za-z0-9_.:-]*(\s[^<>]*)?/?\s*>")
_PORTABLE_NAME_RE = re.compile(r"[a-z0-9-]+")
_TRIGGER_RE = re.compile(r"\bwhen(ever)?\b", re.IGNORECASE)
_FIRST_PERSON_RE = re.compile(
    r"\bI(?:'m|'ll|'ve|'d)?\s+[a-z]|\b(?i:me|my|mine|we|we're|we'll|we've|our|ours)\b"
)
_SECOND_PERSON_RE = re.compile(r"^\s*(?i:you|your)\b|\b(?i:you can|you'll)\b")


class FrontmatterEditError(ValueError):
    """A targeted frontmatter edit cannot be applied safely."""


# --------------------------------------------------------------------------
# Delimiter detection
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Split:
    text: str  # input without a leading BOM
    bom: bool
    newline: str
    has_block: bool
    unclosed: bool
    frontmatter_start: int = 0
    frontmatter_end: int = 0
    body_start: int = 0

    @property
    def frontmatter_text(self) -> str:
        return self.text[self.frontmatter_start : self.frontmatter_end]

    @property
    def body(self) -> str:
        if self.has_block:
            return self.text[self.body_start :]
        return self.text


def _strip_cr(line: str) -> str:
    return line.removesuffix("\r")


def _split(text: str) -> _Split:
    bom = text.startswith("\ufeff")
    if bom:
        text = text[1:]
    first_break = text.find("\n")
    newline = "\r\n" if first_break > 0 and text[first_break - 1] == "\r" else "\n"
    if first_break == -1 or not _DELIMITER_RE.fullmatch(_strip_cr(text[:first_break])):
        return _Split(text, bom, newline, has_block=False, unclosed=False)
    position = first_break + 1
    while position < len(text):
        line_break = text.find("\n", position)
        line_end = len(text) if line_break == -1 else line_break
        if _DELIMITER_RE.fullmatch(_strip_cr(text[position:line_end])):
            body_start = len(text) if line_break == -1 else line_break + 1
            return _Split(
                text,
                bom,
                newline,
                has_block=True,
                unclosed=False,
                frontmatter_start=first_break + 1,
                frontmatter_end=position,
                body_start=body_start,
            )
        if line_break == -1:
            break
        position = line_break + 1
    return _Split(text, bom, newline, has_block=False, unclosed=True)


def split_frontmatter(text: str) -> tuple[str | None, str, dict[str, Any]]:
    """Locate the frontmatter block without parsing YAML.

    Returns ``(frontmatter_text | None, body, info)``. ``frontmatter_text``
    is the raw text between the delimiter lines (``None`` when there is no
    closed block). ``body`` is everything after the closing line, or the
    whole input (minus a leading BOM) when there is no closed block. ``info``
    keys: ``bom``, ``newline`` (``"\\n"`` or ``"\\r\\n"``), ``unclosed``,
    ``frontmatter_bytes`` (UTF-8 size or ``None``), ``too_large`` and
    ``body_first_line`` (1-based line number of the body in the file).
    """
    if not isinstance(text, str):
        raise TypeError("split_frontmatter expects str")
    split = _split(text)
    frontmatter = split.frontmatter_text if split.has_block else None
    size = (
        len(frontmatter.encode("utf-8", "surrogatepass"))
        if frontmatter is not None
        else None
    )
    body_first_line = (
        split.text.count("\n", 0, split.body_start) + 1 if split.has_block else 1
    )
    info = {
        "bom": split.bom,
        "newline": split.newline,
        "unclosed": split.unclosed,
        "frontmatter_bytes": size,
        "too_large": size is not None and size > MAX_FRONTMATTER_BYTES,
        "body_first_line": body_first_line,
    }
    return frontmatter, split.body, info


# --------------------------------------------------------------------------
# Strict YAML loader
# --------------------------------------------------------------------------


class _ContractYAMLError(yaml.YAMLError):
    code = "invalid_yaml"
    status = STATUS_INVALID_YAML

    def __init__(self, message: str, mark: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.problem_mark = mark


class _DuplicateKeyError(_ContractYAMLError):
    code = "duplicate_key"
    first_line: int | None = None  # 0-based line of the first definition


class _NonStringKeyError(_ContractYAMLError):
    code = "non_string_key"


class _UnsupportedFeatureError(_ContractYAMLError):
    code = "unsupported_yaml_feature"
    status = STATUS_NOT_EVALUATED


class StrictMetadataLoader(yaml.SafeLoader):
    """SafeLoader restricted to the metadata contract (see module docstring)."""

    yaml_implicit_resolvers: ClassVar[dict] = {}

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._contract_node_count = 0
        self._contract_depth = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        event = self.peek_event()
        if isinstance(event, yaml.AliasEvent):
            raise _UnsupportedFeatureError(
                f"YAML alias '*{event.anchor}' is not supported", event.start_mark
            )
        anchor = getattr(event, "anchor", None)
        if anchor is not None:
            raise _UnsupportedFeatureError(
                f"YAML anchor '&{anchor}' is not supported", event.start_mark
            )
        tag = getattr(event, "tag", None)
        if tag is not None and tag != "!":
            raise _UnsupportedFeatureError(
                f"explicit YAML tag '{tag}' is not supported", event.start_mark
            )
        self._contract_node_count += 1
        if self._contract_node_count > MAX_YAML_NODES:
            raise _UnsupportedFeatureError(
                f"frontmatter has more than {MAX_YAML_NODES} YAML nodes",
                event.start_mark,
            )
        self._contract_depth += 1
        try:
            if self._contract_depth > MAX_YAML_DEPTH:
                raise _UnsupportedFeatureError(
                    f"frontmatter nests deeper than {MAX_YAML_DEPTH} levels",
                    event.start_mark,
                )
            return super().compose_node(parent, index)
        finally:
            self._contract_depth -= 1

    def construct_mapping(self, node: Any, deep: bool = False) -> dict:
        if not isinstance(node, yaml.MappingNode):
            raise yaml.constructor.ConstructorError(
                None,
                None,
                f"expected a mapping node, but found {node.id}",
                node.start_mark,
            )
        mapping: dict[str, Any] = {}
        first_lines: dict[str, int] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=True)
            if not isinstance(key, str):
                raise _NonStringKeyError(
                    "frontmatter keys must be strings", key_node.start_mark
                )
            if key in mapping:
                error = _DuplicateKeyError(
                    f"duplicate key '{key}'", key_node.start_mark
                )
                error.first_line = first_lines[key]
                raise error
            first_lines[key] = key_node.start_mark.line
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


StrictMetadataLoader.add_implicit_resolver(
    "tag:yaml.org,2002:null",
    re.compile(r"^(?:~|null|Null|NULL|)$"),
    ["~", "n", "N", ""],
)


@dataclass(frozen=True)
class _LoadResult:
    data: Any = None
    node: Any = None
    status: str = STATUS_OK
    code: str | None = None
    message: str | None = None
    line: int | None = None  # file line (see ``_load_yaml`` ``line_base``)


def _mark_line(error: BaseException, text: str) -> int | None:
    mark = getattr(error, "problem_mark", None) or getattr(error, "context_mark", None)
    if mark is not None and getattr(mark, "line", None) is not None:
        return int(mark.line)
    position = getattr(error, "position", None)
    if isinstance(position, int):
        return text.count("\n", 0, position)
    return None


def _load_yaml(text: str, line_base: int = 1) -> _LoadResult:
    """Load ``text`` with the strict loader; never raises for str input.

    ``line_base`` converts YAML's 0-based line numbers into the caller's
    numbering: 1 for lines of ``text`` itself, 2 for SKILL.md file lines
    (the frontmatter starts on the line after the opening ``---``).
    """

    def file_line(line: int | None) -> int | None:
        return None if line is None else line + line_base

    loader: StrictMetadataLoader | None = None
    try:
        # The Reader rejects non-printable characters while constructing.
        loader = StrictMetadataLoader(text)
        node = loader.get_single_node()
        data = loader.construct_document(node) if node is not None else None
        return _LoadResult(data=data, node=node)
    except _ContractYAMLError as error:
        message = error.message
        first_line = getattr(error, "first_line", None)
        if first_line is not None:
            message = f"{message} (first defined on line {file_line(first_line)})"
        return _LoadResult(
            status=error.status,
            code=error.code,
            message=message,
            line=file_line(_mark_line(error, text)),
        )
    except yaml.YAMLError as error:
        problem = getattr(error, "problem", None) or str(error).splitlines()[0]
        return _LoadResult(
            status=STATUS_INVALID_YAML,
            code="invalid_yaml",
            message=f"frontmatter is not valid YAML: {problem}",
            line=file_line(_mark_line(error, text)),
        )
    except RecursionError:
        return _LoadResult(
            status=STATUS_NOT_EVALUATED,
            code="unsupported_yaml_feature",
            message="frontmatter nesting exceeded the parser's recursion limit",
        )
    finally:
        if loader is not None:
            loader.dispose()


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def normalize_whitespace(value: str) -> str:
    """Collapse every whitespace run to one space and strip the ends."""
    return " ".join(value.split())


def _issue(code: str, message: str, line: int | None = None) -> dict[str, Any]:
    issue: dict[str, Any] = {"code": code, "message": message}
    if line is not None:
        issue["line"] = line
    return issue


def _first_body_line(body: str) -> str | None:
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        heading = _HEADING_RE.fullmatch(stripped)
        text = heading.group(1) if heading else stripped
        text = normalize_whitespace(text)
        return text or stripped
    return None


def _type_name(value: Any) -> str:
    if isinstance(value, dict):
        return "mapping"
    if isinstance(value, list):
        return "list"
    if value is None:
        return "null"
    return "string"


def _coerce_native(key: str, value: Any, line: int | None) -> tuple[Any, dict | None]:
    """Return (typed value or None when unset, warning or None)."""
    if value is None:
        return None, None
    if key in BOOLEAN_KEYS:
        if isinstance(value, str):
            word = value.strip().lower()
            if word in _TRUE_WORDS:
                return True, None
            if word in _FALSE_WORDS:
                return False, None
        return None, _issue(
            "invalid_boolean",
            f"'{key}' must be true/false (yes/no/on/off/1/0); got {value!r} — "
            "the field is ignored",
            line,
        )
    if key in STRING_KEYS:
        if isinstance(value, str):
            return value, None
    elif key in LIST_OR_STRING_KEYS:
        if isinstance(value, str):
            return value, None
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return list(value), None
        expected = "a string or a list of strings"
        return None, _issue(
            "invalid_type", f"'{key}' must be {expected}; the field is ignored", line
        )
    elif key in MAPPING_KEYS:
        if isinstance(value, dict):
            return value, None
        return None, _issue(
            "invalid_type",
            f"'{key}' must be a mapping, got a {_type_name(value)}; the field is ignored",
            line,
        )
    return None, _issue(
        "invalid_type",
        f"'{key}' must be a string, got a {_type_name(value)}; the field is ignored",
        line,
    )


def _top_level_entries(node: Any) -> list[tuple[Any, Any]]:
    if isinstance(node, yaml.MappingNode):
        return list(node.value)
    return []


def _inline_comment_warnings(
    frontmatter: str, node: Any, first_line: int
) -> list[dict]:
    warnings: list[dict] = []
    for key_node, value_node in _top_level_entries(node):
        if not isinstance(value_node, yaml.ScalarNode) or value_node.style is not None:
            continue
        end = value_node.end_mark.index
        if end == value_node.start_mark.index:
            continue
        line_end = frontmatter.find("\n", end)
        rest = frontmatter[end : len(frontmatter) if line_end == -1 else line_end]
        if re.match(r"[ \t]+#", rest):
            warnings.append(
                _issue(
                    "inline_comment",
                    f"text after ' #' in '{key_node.value}' is a YAML comment and is "
                    "not part of the value; quote the value to keep it",
                    value_node.end_mark.line + first_line,
                )
            )
    return warnings


def _quality_warnings(
    description: str | None, frontmatter_description: str | None, has_when_to_use: bool
) -> list[dict]:
    warnings: list[dict] = []
    if description is None or frontmatter_description is None:
        return warnings
    if len(frontmatter_description) > PORTABLE_DESCRIPTION_CAP:
        warnings.append(
            _issue(
                "description_too_long",
                f"description is {len(frontmatter_description)} characters; keep it "
                f"within {PORTABLE_DESCRIPTION_CAP}",
            )
        )
    if len(description) < SHORT_DESCRIPTION_CHARS:
        warnings.append(
            _issue(
                "short_description",
                "description is very short; say what the skill does and when to use it",
            )
        )
    if not has_when_to_use and not _TRIGGER_RE.search(description):
        warnings.append(
            _issue(
                "weak_trigger",
                "description has no 'Use when …' clause; name the situations that "
                "should trigger the skill",
            )
        )
    if _FIRST_PERSON_RE.search(frontmatter_description):
        warnings.append(
            _issue(
                "first_person_description",
                "write the description in the third person ('Processes …'), not 'I'/'we'",
            )
        )
    if _SECOND_PERSON_RE.search(frontmatter_description):
        warnings.append(
            _issue(
                "second_person_description",
                "write the description in the third person ('Processes …'), not 'you'",
            )
        )
    return warnings


def _portable_report(
    status: str, mapping: dict[str, Any] | None, key_lines: dict[str, int]
) -> dict[str, Any]:
    issues: list[dict] = []
    if status == STATUS_NO_FRONTMATTER:
        issues.append(_issue("missing_frontmatter", "portable skills need frontmatter"))
    elif status != STATUS_OK or mapping is None:
        issues.append(
            _issue("invalid_frontmatter", "frontmatter must be a valid YAML mapping")
        )
    else:
        name = mapping.get("name")
        if not isinstance(name, str) or not name.strip():
            issues.append(_issue("missing_name", "portable skills need a 'name'"))
        else:
            line = key_lines.get("name")
            if len(name) > PORTABLE_NAME_CAP:
                issues.append(
                    _issue(
                        "name_too_long",
                        f"'name' must be at most {PORTABLE_NAME_CAP} characters",
                        line,
                    )
                )
            if not _PORTABLE_NAME_RE.fullmatch(name):
                issues.append(
                    _issue(
                        "name_invalid_characters",
                        "'name' may only use lowercase letters, digits and hyphens",
                        line,
                    )
                )
            if "anthropic" in name.lower() or "claude" in name.lower():
                issues.append(
                    _issue(
                        "name_reserved_word",
                        "'name' must not contain 'anthropic' or 'claude'",
                        line,
                    )
                )
            if _XML_TAG_RE.search(name):
                issues.append(
                    _issue("name_xml_tag", "'name' must not contain XML tags", line)
                )
        description = mapping.get("description")
        if not isinstance(description, str) or not description.strip():
            issues.append(
                _issue("missing_description", "portable skills need a 'description'")
            )
        else:
            line = key_lines.get("description")
            if len(description.strip()) > PORTABLE_DESCRIPTION_CAP:
                issues.append(
                    _issue(
                        "description_too_long",
                        f"'description' must be at most {PORTABLE_DESCRIPTION_CAP} "
                        "characters",
                        line,
                    )
                )
            if _XML_TAG_RE.search(description):
                issues.append(
                    _issue(
                        "description_xml_tag",
                        "'description' must not contain XML tags",
                        line,
                    )
                )
        for key in mapping:
            if key not in PORTABLE_KEYS:
                issues.append(
                    _issue(
                        "unsupported_key",
                        f"'{key}' is not accepted by portable uploads",
                        key_lines.get(key),
                    )
                )
        compatibility = mapping.get("compatibility")
        if compatibility is not None and (
            not isinstance(compatibility, str)
            or len(compatibility) > PORTABLE_COMPATIBILITY_CAP
        ):
            issues.append(
                _issue(
                    "compatibility_invalid",
                    "'compatibility' must be a string of at most "
                    f"{PORTABLE_COMPATIBILITY_CAP} characters",
                    key_lines.get("compatibility"),
                )
            )
        metadata = mapping.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            issues.append(
                _issue(
                    "metadata_invalid",
                    "'metadata' must be a mapping",
                    key_lines.get("metadata"),
                )
            )
    return {"compatible": not issues, "issues": issues}


def parse_skill_md(text: str, directory_name: str) -> dict[str, Any]:
    """Parse a SKILL.md into the JSON-safe contract result (never raises for str).

    Keys: ``contract_version``, ``status`` (ok | no_frontmatter |
    invalid_yaml | not_mapping | not_evaluated), ``native_frontmatter_loaded``
    (True/False, or None when not evaluated), ``label``/``label_source``
    (frontmatter | directory), ``description``/``description_source``
    (frontmatter | body_first_line | when_to_use | none | not_evaluated),
    ``listing_truncated``, ``fields`` (typed native keys), ``unknown_keys``,
    ``errors``, ``warnings`` (each ``{code, message, line?}`` with 1-based file
    line numbers) and ``portable`` (``{compatible, issues}``).
    """
    if not isinstance(text, str):
        raise TypeError("parse_skill_md expects str")
    split = _split(text)
    errors: list[dict] = []
    warnings: list[dict] = []
    fields: dict[str, Any] = {}
    unknown_keys: dict[str, Any] = {}
    mapping: dict[str, Any] | None = None
    key_lines: dict[str, int] = {}
    body = split.body
    # YAML line 0 of the frontmatter is file line 2 (the opener is line 1).
    first_line = 2

    if split.bom:
        warnings.append(
            _issue("bom_present", "file starts with a UTF-8 byte order mark", 1)
        )

    if not split.has_block:
        status = STATUS_NO_FRONTMATTER
        if split.unclosed:
            errors.append(
                _issue(
                    "unclosed_frontmatter",
                    "the opening '---' has no closing '---' line, so the whole file "
                    "(markers included) is read as body text",
                    1,
                )
            )
        else:
            warnings.append(
                _issue("missing_frontmatter", "SKILL.md has no YAML frontmatter")
            )
    else:
        frontmatter = split.frontmatter_text
        size = len(frontmatter.encode("utf-8", "surrogatepass"))
        if size > MAX_FRONTMATTER_BYTES:
            status = STATUS_NOT_EVALUATED
            errors.append(
                _issue(
                    "frontmatter_too_large",
                    f"frontmatter is {size} bytes; the contract evaluates at most "
                    f"{MAX_FRONTMATTER_BYTES}",
                )
            )
        else:
            loaded = _load_yaml(frontmatter, line_base=first_line)
            status = loaded.status
            if status != STATUS_OK:
                errors.append(
                    _issue(
                        loaded.code or "invalid_yaml", loaded.message or "", loaded.line
                    )
                )
            else:
                data = {} if loaded.data is None else loaded.data
                if not isinstance(data, dict):
                    status = STATUS_NOT_MAPPING
                    errors.append(
                        _issue(
                            "not_mapping",
                            f"frontmatter must be a YAML mapping, got a {_type_name(data)}",
                            first_line,
                        )
                    )
                else:
                    mapping = data
                    for key_node, _value_node in _top_level_entries(loaded.node):
                        key_lines[key_node.value] = (
                            key_node.start_mark.line + first_line
                        )
                    for key, value in mapping.items():
                        if key in NATIVE_KEYS:
                            typed, problem = _coerce_native(
                                key, value, key_lines.get(key)
                            )
                            if problem is not None:
                                warnings.append(problem)
                            elif typed is not None:
                                fields[key] = typed
                        else:
                            unknown_keys[key] = value
                    warnings.extend(
                        _inline_comment_warnings(frontmatter, loaded.node, first_line)
                    )

    native_loaded: bool | None
    if status == STATUS_OK:
        native_loaded = True
    elif status == STATUS_NOT_EVALUATED:
        native_loaded = None
    else:
        native_loaded = False

    label = directory_name
    label_source = "directory"
    description: str | None = None
    description_source = "none"
    frontmatter_description: str | None = None
    when_to_use: str | None = None

    if status == STATUS_NOT_EVALUATED:
        description_source = "not_evaluated"
    else:
        if status == STATUS_OK:
            name = fields.get("name")
            if isinstance(name, str) and normalize_whitespace(name):
                label = normalize_whitespace(name)
                label_source = "frontmatter"
            raw_description = fields.get("description")
            if isinstance(raw_description, str) and normalize_whitespace(
                raw_description
            ):
                frontmatter_description = normalize_whitespace(raw_description)
            raw_when = fields.get("when_to_use")
            if isinstance(raw_when, str) and normalize_whitespace(raw_when):
                when_to_use = normalize_whitespace(raw_when)
        base = frontmatter_description
        if base is not None:
            description_source = "frontmatter"
        else:
            base = _first_body_line(body)
            if base is not None:
                description_source = "body_first_line"
        parts = [part for part in (base, when_to_use) if part]
        if parts:
            description = " ".join(parts)
            if base is None:
                description_source = "when_to_use"
        if frontmatter_description is None:
            warnings.append(
                _issue(
                    "missing_description",
                    "no usable frontmatter description; Claude Code falls back to "
                    "the first non-empty body line",
                )
            )

    listing_truncated = (
        description is not None and len(description) > LISTING_DESCRIPTION_CAP
    )
    if listing_truncated:
        warnings.append(
            _issue(
                "listing_truncated",
                f"description plus when_to_use is {len(description or '')} characters; "
                f"Claude Code truncates the listing at {LISTING_DESCRIPTION_CAP}",
            )
        )
    warnings.extend(
        _quality_warnings(description, frontmatter_description, when_to_use is not None)
    )
    body_lines = len(body.splitlines())
    if body_lines > BODY_LINE_ADVISORY_LIMIT:
        warnings.append(
            _issue(
                "body_too_long",
                f"SKILL.md body has {body_lines} lines; keep it under "
                f"{BODY_LINE_ADVISORY_LIMIT} and move detail into reference files",
            )
        )

    return {
        "contract_version": CONTRACT_VERSION,
        "status": status,
        "native_frontmatter_loaded": native_loaded,
        "label": label,
        "label_source": label_source,
        "description": description,
        "description_source": description_source,
        "listing_truncated": listing_truncated,
        "fields": fields,
        "unknown_keys": unknown_keys,
        "errors": errors,
        "warnings": warnings,
        "portable": _portable_report(status, mapping, key_lines),
    }


def validate(
    parsed: Mapping[str, Any], target: str = TARGET_NATIVE_CUBICLE
) -> dict[str, Any]:
    """Split a ``parse_skill_md`` result into blocking errors and warnings.

    ``native_cubicle``: errors are the parse errors (invalid YAML, duplicate
    or non-string keys, non-mapping frontmatter, unclosed frontmatter,
    unsupported or oversized frontmatter); everything else is a warning.
    ``portable``: additionally every portable issue is an error.

    Errors are BLOCKING for new platform-authored content (generation,
    templates, installs). For existing user-edited files callers must show
    them as warnings and never block reads or sync.
    """
    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}; expected one of {TARGETS}")
    if parsed.get("contract_version") != CONTRACT_VERSION:
        raise ValueError("parsed result was produced by a different contract version")
    errors = [dict(issue) for issue in parsed.get("errors", [])]
    if target == TARGET_PORTABLE:
        errors.extend(
            dict(issue) for issue in parsed.get("portable", {}).get("issues", [])
        )
    error_codes = {issue["code"] for issue in errors}
    warnings = [
        dict(issue)
        for issue in parsed.get("warnings", [])
        if issue["code"] not in error_codes
    ]
    return {"target": target, "ok": not errors, "errors": errors, "warnings": warnings}


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _check_renderable(value: Any, path: str) -> Any:
    """Validate a value for rendering; return what the strict loader will read."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_check_renderable(item, f"{path}[]") for item in value]
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"frontmatter key at {path} must be a string")
            result[key] = _check_renderable(item, f"{path}.{key}")
        return result
    raise ValueError(
        f"frontmatter value at {path} has unsupported type {type(value).__name__}"
    )


def _plain(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    return value


def render_frontmatter(mapping: Mapping[str, Any], newline: str = "\n") -> str:
    """Render a delimited frontmatter block (``---`` … ``---`` + newline).

    Key order: ``name``, ``description``, then the remaining keys in their
    given order. Values may be str, bool, int, None, lists and mappings. The
    result is verified to load with the strict loader to the same data
    (booleans/integers come back as strings; ``parse_skill_md`` coerces the
    native boolean keys). Raises ``ValueError`` when that is impossible.
    """
    if newline not in ("\n", "\r\n"):
        raise ValueError("newline must be '\\n' or '\\r\\n'")
    ordered: dict[str, Any] = {}
    for key in ("name", "description"):
        if key in mapping:
            ordered[key] = mapping[key]
    for key, value in mapping.items():
        if not isinstance(key, str):
            raise TypeError("frontmatter keys must be strings")
        if key not in ordered:
            ordered[key] = value
    expected = _check_renderable(ordered, "frontmatter")
    dumped = ""
    if ordered:
        dumped = yaml.safe_dump(
            _plain(ordered),
            sort_keys=False,
            allow_unicode=True,
            width=_DUMP_WIDTH,
            default_flow_style=False,
        )
    block = "---\n" + dumped + "---\n"
    if newline != "\n":
        block = block.replace("\n", newline)
    # Verify the FINAL text: the block must close at our delimiter and load
    # through the strict loader to exactly the expected data.
    split = _split(block)
    if not split.has_block or split.body_start != len(block):
        raise ValueError("rendered frontmatter does not close at its delimiter")
    frontmatter = split.frontmatter_text
    if len(frontmatter.encode("utf-8", "surrogatepass")) > MAX_FRONTMATTER_BYTES:
        raise ValueError("rendered frontmatter exceeds MAX_FRONTMATTER_BYTES")
    loaded = _load_yaml(frontmatter)
    if (
        loaded.status != STATUS_OK
        or ({} if loaded.data is None else loaded.data) != expected
    ):
        raise ValueError(
            f"frontmatter does not round-trip: {loaded.message or 'value changed'}"
        )
    return block


def _require_text(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a string")
    normalized = normalize_whitespace(value)
    if not normalized:
        raise ValueError(f"{what} must not be empty")
    return normalized


def render_skill_md(
    name: str,
    description: str,
    body: str,
    extra: Mapping[str, Any] | None = None,
) -> str:
    """Render a platform-authored SKILL.md: frontmatter, blank line, body.

    ``name`` and ``description`` are whitespace-normalized and must be
    non-empty; ``extra`` adds further keys (it may not repeat name or
    description). Raises ``ValueError`` if ``body`` itself starts with a
    ``---`` line (use ``ensure_frontmatter`` for content that may already
    carry frontmatter).
    """
    mapping: dict[str, Any] = {
        "name": _require_text(name, "name"),
        "description": _require_text(description, "description"),
    }
    for key, value in (extra or {}).items():
        if key in ("name", "description"):
            raise ValueError(f"extra must not set {key!r}")
        mapping[key] = value
    if not isinstance(body, str):
        raise TypeError("body must be a string")
    body_split = _split(body)
    if body_split.has_block or body_split.unclosed:
        raise ValueError("body already starts with a '---' frontmatter line")
    text = body_split.text.lstrip("\r\n")
    if text and not text.endswith("\n"):
        text += "\n"
    return render_frontmatter(mapping) + "\n" + text


def ensure_frontmatter(
    content: str, name: str, description: str
) -> tuple[str, dict[str, Any]]:
    """Give PLATFORM-AUTHORED content valid frontmatter with a description.

    Never use this on user-edited files. Behaviour by current state:

    * valid frontmatter with a description → unchanged (``unchanged``);
    * valid frontmatter without a description → the description is inserted
      with ``set_frontmatter_description`` (``description_added``);
    * no frontmatter (or an unclosed opener) → a canonical ``{name,
      description}`` block is prepended; the text is kept as the body
      (``prepended``);
    * invalid, non-mapping or not-evaluable frontmatter → that block is
      replaced by a canonical ``{name, description}`` block and the body is
      kept byte for byte (``replaced``; the discarded block is returned in
      the report).

    A leading BOM is removed when a block is prepended or replaced. Returns
    ``(content, report)``; the report carries ``action``,
    ``status_before``, ``error_codes_before``, ``bom_removed``,
    ``discarded_frontmatter`` (or None) and ``warning_codes_after``.
    """
    if not isinstance(content, str):
        raise TypeError("content must be a string")
    clean_name = _require_text(name, "name")
    clean_description = _require_text(description, "description")
    parsed = parse_skill_md(content, clean_name)
    split = _split(content)
    report: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "action": "unchanged",
        "status_before": parsed["status"],
        "error_codes_before": [issue["code"] for issue in parsed["errors"]],
        "bom_removed": False,
        "discarded_frontmatter": None,
    }
    status = parsed["status"]
    result: str | None = None
    if status == STATUS_OK and parsed["description_source"] == "frontmatter":
        result = content
    elif status == STATUS_OK:
        try:
            result = set_frontmatter_description(content, clean_description)
            report["action"] = "description_added"
        except FrontmatterEditError:
            # Valid but not editable in place (e.g. a flow-style mapping):
            # platform-authored content gets the canonical block instead.
            result = None
    if result is None:
        block = render_frontmatter(
            {"name": clean_name, "description": clean_description}, split.newline
        )
        report["bom_removed"] = split.bom
        if split.has_block:
            result = block + split.text[split.body_start :]
            report["action"] = "replaced"
            report["discarded_frontmatter"] = split.frontmatter_text
        else:
            result = block + split.newline + split.text
            report["action"] = "prepended"
    after = parse_skill_md(result, clean_name)
    if after["status"] != STATUS_OK or after["description_source"] != "frontmatter":
        raise ValueError("ensure_frontmatter produced content that does not parse")
    report["warning_codes_after"] = [issue["code"] for issue in after["warnings"]]
    return result, report


def _quote_scalar(value: str) -> str:
    dumped: str = yaml.safe_dump(
        value, default_style='"', allow_unicode=True, width=_DUMP_WIDTH
    )
    scalar = dumped.rstrip("\n")
    if "\n" in scalar or not (scalar.startswith('"') and scalar.endswith('"')):
        raise FrontmatterEditError(
            "description cannot be rendered as a one-line scalar"
        )
    return scalar


def _trimmed_end(text: str, start: int, end: int) -> int:
    while end > start and text[end - 1] in " \t\r\n":
        end -= 1
    return end


def set_frontmatter_description(text: str, value: str) -> str:
    """Set the frontmatter ``description`` with a targeted text edit.

    Only the existing value's span (from the composed YAML node's start/end
    marks) is replaced, by a double-quoted one-line scalar; comments, key
    order, line endings, a BOM and every other key stay byte for byte. A
    missing key is inserted after ``name:`` (or first); a file without
    frontmatter gets a minimal ``description``-only block prepended.
    ``value`` is whitespace-normalized and must not be empty. Raises
    ``FrontmatterEditError`` for invalid/unclosed/unsupported frontmatter
    and ``ValueError`` for an empty value.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    new_value = _require_text(value, "description")
    scalar = _quote_scalar(new_value)
    split = _split(text)
    newline = split.newline
    prefix = "\ufeff" if split.bom else ""
    if not split.has_block:
        if split.unclosed:
            raise FrontmatterEditError(
                "the frontmatter has an opening '---' but no closing '---' line"
            )
        result = (
            prefix
            + "---"
            + newline
            + "description: "
            + scalar
            + newline
            + "---"
            + newline
            + newline
            + split.text
        )
    else:
        frontmatter = split.frontmatter_text
        if len(frontmatter.encode("utf-8", "surrogatepass")) > MAX_FRONTMATTER_BYTES:
            raise FrontmatterEditError("frontmatter is too large to edit safely")
        loaded = _load_yaml(frontmatter, line_base=2)
        if loaded.status != STATUS_OK:
            line = "" if loaded.line is None else f" (line {loaded.line})"
            raise FrontmatterEditError(
                f"cannot edit the description: {loaded.message}{line}"
            )
        node = loaded.node
        if node is None:
            new_frontmatter = "description: " + scalar + newline + frontmatter
        elif not isinstance(node, yaml.MappingNode) or not isinstance(
            loaded.data, dict
        ):
            raise FrontmatterEditError(
                "cannot edit the description: frontmatter is not a YAML mapping"
            )
        elif node.flow_style:
            raise FrontmatterEditError(
                "cannot edit the description: flow-style '{…}' frontmatter is not "
                "supported; use one 'key: value' per line"
            )
        else:
            new_frontmatter = _edit_mapping(frontmatter, node, scalar, newline)
        result = (
            prefix
            + split.text[: split.frontmatter_start]
            + new_frontmatter
            + split.text[split.frontmatter_end :]
        )
    check = parse_skill_md(result, "_")
    if check["status"] != STATUS_OK or check["fields"].get("description") != new_value:
        raise FrontmatterEditError(
            "the edited frontmatter did not re-parse to the new value"
        )
    return result


def _edit_mapping(frontmatter: str, node: Any, scalar: str, newline: str) -> str:
    entries = list(node.value)
    for key_node, value_node in entries:
        if key_node.value == "description":
            start = value_node.start_mark.index
            end = _trimmed_end(frontmatter, start, value_node.end_mark.index)
            replacement = scalar
            if start == end and (start == 0 or frontmatter[start - 1] not in " \t"):
                replacement = " " + scalar
            return frontmatter[:start] + replacement + frontmatter[end:]
    indent = " " * int(entries[0][0].start_mark.column)
    line = indent + "description: " + scalar
    for key_node, value_node in entries:
        if key_node.value == "name":
            end = _trimmed_end(
                frontmatter, value_node.start_mark.index, value_node.end_mark.index
            )
            line_break = frontmatter.find("\n", end)
            if line_break == -1:
                return frontmatter + newline + line + newline
            return (
                frontmatter[: line_break + 1]
                + line
                + newline
                + frontmatter[line_break + 1 :]
            )
    first_key = entries[0][0].start_mark
    line_start = first_key.index - first_key.column
    return frontmatter[:line_start] + line + newline + frontmatter[line_start:]


# --------------------------------------------------------------------------
# Catalog helpers
# --------------------------------------------------------------------------


def _cap_text(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[: cap - 1].rstrip() + "\u2026"


def effective_description(
    parsed: Mapping[str, Any],
    cap: int = DEFAULT_CATALOG_DESCRIPTION_CAP,
    *,
    frontmatter_only: bool = False,
) -> str | None:
    """Return the description to store in a catalog/DB row, or None.

    The text is whitespace-normalized and capped at ``cap`` characters (an
    ellipsis marks a cut). With ``frontmatter_only`` only a description the
    native loader actually reads from a valid frontmatter qualifies — use it
    when refreshing a stored value so a fallback body line never replaces a
    curated description.
    """
    if cap < 2:
        raise ValueError("cap must be at least 2")
    if parsed.get("contract_version") != CONTRACT_VERSION:
        return None
    if frontmatter_only and (
        parsed.get("status") != STATUS_OK
        or parsed.get("description_source") != "frontmatter"
    ):
        return None
    description = parsed.get("description")
    if not isinstance(description, str):
        return None
    normalized = normalize_whitespace(description)
    if not normalized:
        return None
    return _cap_text(normalized, cap)


_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_INLINE_CODE_RE = re.compile(r"(`+).+?\1")
_INLINE_LINK_RE = re.compile(
    r"!?\[[^\]\n]*\]\(\s*(<[^>\n]*>|[^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)"
)
_REFERENCE_RE = re.compile(r"^[ \t]{0,3}\[(?!\^)[^\]\n]+\]:[ \t]*(<[^>\n]*>|\S+)")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def extract_link_targets(body: str) -> list[str]:
    """Return relative markdown link targets in ``body`` (first-seen order).

    Covers inline links/images and reference definitions outside fenced
    code blocks and inline code. Absolute URLs (any ``scheme:``),
    ``//host`` and ``/absolute`` paths and pure ``#anchor`` links are
    skipped; ``#fragment`` and ``?query`` suffixes are removed.
    """
    targets: list[str] = []
    seen: set[str] = set()
    fence: str | None = None
    for line in body.splitlines():
        fence_match = _FENCE_RE.match(line)
        if fence is not None:
            if (
                fence_match
                and fence_match.group(1)[0] == fence[0]
                and len(fence_match.group(1)) >= len(fence)
            ):
                fence = None
            continue
        if fence_match:
            fence = fence_match.group(1)
            continue
        visible = _INLINE_CODE_RE.sub("", line)
        candidates = [match.group(1) for match in _INLINE_LINK_RE.finditer(visible)]
        reference = _REFERENCE_RE.match(visible)
        if reference:
            candidates.append(reference.group(1))
        for raw in candidates:
            target = (
                raw[1:-1].strip() if raw.startswith("<") and raw.endswith(">") else raw
            )
            target = target.split("#", 1)[0].split("?", 1)[0]
            if not target or target.startswith("/") or _SCHEME_RE.match(target):
                continue
            if target not in seen:
                seen.add(target)
                targets.append(target)
    return targets
