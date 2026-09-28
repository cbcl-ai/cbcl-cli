"""Skill metadata contract v1 (``src/skill_metadata.py``).

Fixture-driven cases come from ``tests/fixtures/skill_metadata_cases.json``;
the backend suite runs the byte-identical fixture file against its
byte-identical module copy (parity enforced by
``tests/test_skill_metadata_parity.py``). The targeted tests below cover the
helpers, bounds and properties that do not fit a static fixture.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from src import skill_metadata as sm

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "skill_metadata_cases.json"
FIXTURES = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

_CODE_LISTS = {
    "error_codes": lambda parsed: [issue["code"] for issue in parsed["errors"]],
    "warning_codes": lambda parsed: [issue["code"] for issue in parsed["warnings"]],
    "portable_issue_codes": lambda parsed: [
        issue["code"] for issue in parsed["portable"]["issues"]
    ],
}


def _check_parse_case(case: dict[str, Any]) -> None:
    parsed = sm.parse_skill_md(case["input"], case["directory"])
    assert parsed["contract_version"] == FIXTURES["contract_version"]
    # Every result must be JSON-safe.
    json.dumps(parsed)
    for key, expected in case["expect"].items():
        if key in _CODE_LISTS:
            actual = _CODE_LISTS[key](parsed)
            assert Counter(actual) == Counter(expected), (key, actual)
        elif key == "error_lines":
            lines = {issue["code"]: issue.get("line") for issue in parsed["errors"]}
            for code, line in expected.items():
                assert lines.get(code) == line, (code, lines)
        elif key == "portable_compatible":
            assert parsed["portable"]["compatible"] is expected
        else:
            assert parsed[key] == expected, (key, parsed[key])


@pytest.mark.parametrize(
    "case", FIXTURES["parse_cases"], ids=[c["id"] for c in FIXTURES["parse_cases"]]
)
def test_parse_fixture(case: dict[str, Any]) -> None:
    _check_parse_case(case)


@pytest.mark.parametrize(
    "case",
    FIXTURES["set_description_cases"],
    ids=[c["id"] for c in FIXTURES["set_description_cases"]],
)
def test_set_description_fixture(case: dict[str, Any]) -> None:
    if case.get("expect_error"):
        with pytest.raises(sm.FrontmatterEditError):
            sm.set_frontmatter_description(case["input"], case["value"])
        return
    output = sm.set_frontmatter_description(case["input"], case["value"])
    assert output == case["expect_output"]
    reparsed = sm.parse_skill_md(output, "skill")
    assert reparsed["status"] == sm.STATUS_OK
    assert reparsed["fields"]["description"] == sm.normalize_whitespace(case["value"])


@pytest.mark.parametrize(
    "case", FIXTURES["ensure_cases"], ids=[c["id"] for c in FIXTURES["ensure_cases"]]
)
def test_ensure_frontmatter_fixture(case: dict[str, Any]) -> None:
    output, report = sm.ensure_frontmatter(
        case["input"], case["name"], case["description"]
    )
    assert report["action"] == case["expect_action"]
    assert output == case["expect_output"]
    # Idempotent: a second pass leaves the result untouched.
    again, second = sm.ensure_frontmatter(output, case["name"], "Other text.")
    assert again == output
    assert second["action"] == "unchanged"


# --------------------------------------------------------------------------
# The original F04 bug
# --------------------------------------------------------------------------


def _legacy_line_parser(content: str) -> dict[str, str]:
    """Verbatim copy of the removed-in-later-packages backend parser."""
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    result: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.startswith(" ") and not line.startswith("-"):
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value:
                result[key] = value
    return result


def test_folded_description_bug_is_fixed() -> None:
    text = (
        "---\nname: pdf\ndescription: >\n  Extracts text from PDF files.\n"
        "  Use when a PDF is shared.\n---\n# PDF\n"
    )
    assert _legacy_line_parser(text)["description"] == ">"
    parsed = sm.parse_skill_md(text, "pdf")
    assert parsed["description"] == (
        "Extracts text from PDF files. Use when a PDF is shared."
    )


def test_invalid_yaml_is_not_hidden_by_lenient_parsing() -> None:
    text = (
        "---\nname: x\ndescription: Screens candidates: scores CVs\n---\n# Screening\n"
    )
    assert _legacy_line_parser(text)["description"] == "Screens candidates: scores CVs"
    parsed = sm.parse_skill_md(text, "x")
    assert parsed["status"] == sm.STATUS_INVALID_YAML
    assert parsed["native_frontmatter_loaded"] is False
    assert parsed["description"] == "Screening"
    assert parsed["description_source"] == "body_first_line"


# --------------------------------------------------------------------------
# split_frontmatter
# --------------------------------------------------------------------------


def test_split_frontmatter_basic_and_info() -> None:
    frontmatter, body, info = sm.split_frontmatter("---\nname: a\n---\nbody\n")
    assert frontmatter == "name: a\n"
    assert body == "body\n"
    assert info == {
        "bom": False,
        "newline": "\n",
        "unclosed": False,
        "frontmatter_bytes": 8,
        "too_large": False,
        "body_first_line": 4,
    }


def test_split_frontmatter_bom_crlf_and_unclosed() -> None:
    frontmatter, body, info = sm.split_frontmatter("\ufeff---\r\nname: a\r\n---\r\nb")
    assert frontmatter == "name: a\r\n"
    assert body == "b"
    assert info["bom"] is True
    assert info["newline"] == "\r\n"

    frontmatter, body, info = sm.split_frontmatter("---\nname: a\n")
    assert frontmatter is None
    assert body == "---\nname: a\n"
    assert info["unclosed"] is True


@pytest.mark.parametrize(
    "text", ["", "---", "no frontmatter", "--- \n", "----\nname: a\n---\n"]
)
def test_split_frontmatter_without_block(text: str) -> None:
    frontmatter, body, _ = sm.split_frontmatter(text)
    assert frontmatter is None
    assert body == text


def test_split_frontmatter_marks_oversized_block() -> None:
    text = "---\nnotes: " + "a" * (sm.MAX_FRONTMATTER_BYTES + 1) + "\n---\nbody"
    frontmatter, _, info = sm.split_frontmatter(text)
    assert frontmatter is not None
    assert info["too_large"] is True


def test_split_frontmatter_rejects_non_text() -> None:
    with pytest.raises(TypeError):
        sm.split_frontmatter(b"---\n---\n")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Strict loader bounds and security
# --------------------------------------------------------------------------


def test_oversized_frontmatter_is_not_evaluated() -> None:
    text = "---\nnotes: " + "a" * (sm.MAX_FRONTMATTER_BYTES + 1) + "\n---\nbody"
    parsed = sm.parse_skill_md(text, "big")
    assert parsed["status"] == sm.STATUS_NOT_EVALUATED
    assert parsed["native_frontmatter_loaded"] is None
    assert parsed["description"] is None
    assert [issue["code"] for issue in parsed["errors"]] == ["frontmatter_too_large"]


def test_deep_nesting_is_not_evaluated_without_recursion_error() -> None:
    depth = sm.MAX_YAML_DEPTH + 5
    text = "---\nroot: " + "[" * depth + "]" * depth + "\n---\n"
    parsed = sm.parse_skill_md(text, "deep")
    assert parsed["status"] == sm.STATUS_NOT_EVALUATED
    assert parsed["errors"][0]["code"] == "unsupported_yaml_feature"


def test_node_count_cap_is_not_evaluated() -> None:
    items = "\n".join(f"  - item{index}" for index in range(sm.MAX_YAML_NODES + 5))
    parsed = sm.parse_skill_md(f"---\nallowed-tools:\n{items}\n---\n", "many")
    assert parsed["status"] == sm.STATUS_NOT_EVALUATED


def test_alias_bomb_is_refused_quickly() -> None:
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x]"]
    for index in range(1, 9):
        previous = f"*a{index - 1}"
        lines.append(f"a{index}: &a{index} [{', '.join([previous] * 9)}]")
    started = time.monotonic()
    parsed = sm.parse_skill_md("---\n" + "\n".join(lines) + "\n---\n", "bomb")
    assert time.monotonic() - started < 2.0
    assert parsed["status"] == sm.STATUS_NOT_EVALUATED


@pytest.mark.parametrize(
    "payload",
    [
        "name: !!python/object/apply:os.system ['true']",
        "name: !!binary aGVsbG8=",
        "name: !custom value",
        "name: !!str explicit",
    ],
)
def test_explicit_tags_are_never_evaluated(payload: str) -> None:
    parsed = sm.parse_skill_md(f"---\n{payload}\n---\n", "tags")
    assert parsed["status"] == sm.STATUS_NOT_EVALUATED
    assert parsed["fields"] == {}


def test_plain_scalars_stay_strings_null_is_null() -> None:
    text = (
        "---\nname: types\ndescription: Keeps types. Use when checking types.\n"
        "a: yes\nb: 1\nc: 2024-01-01\nd: 1.5e3\ne: .inf\nf: ~\ng:\nh: null\n"
        "i: 'null'\n---\n"
    )
    parsed = sm.parse_skill_md(text, "types")
    assert parsed["unknown_keys"] == {
        "a": "yes",
        "b": "1",
        "c": "2024-01-01",
        "d": "1.5e3",
        "e": ".inf",
        "f": None,
        "g": None,
        "h": None,
        "i": "null",
    }


def test_merge_key_is_an_ordinary_string_key() -> None:
    parsed = sm.parse_skill_md("---\nname: m\n<<: value\n---\n", "m")
    assert parsed["status"] == sm.STATUS_OK
    assert parsed["unknown_keys"] == {"<<": "value"}


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("true", True),
        ("True", True),
        ("YES", True),
        ("on", True),
        ("1", True),
        ("false", False),
        ("No", False),
        ("OFF", False),
        ("0", False),
    ],
)
def test_boolean_coercion_rule(word: str, expected: bool) -> None:
    parsed = sm.parse_skill_md(f"---\nuser-invocable: {word}\n---\n", "b")
    assert parsed["fields"]["user-invocable"] is expected


def test_body_over_500_lines_warns() -> None:
    body = "\n".join(
        f"line {index}" for index in range(sm.BODY_LINE_ADVISORY_LIMIT + 1)
    )
    text = "---\nname: long\ndescription: Long body. Use when testing length.\n---\n"
    parsed = sm.parse_skill_md(text + body, "long")
    assert "body_too_long" in [issue["code"] for issue in parsed["warnings"]]
    parsed = sm.parse_skill_md(text + "short body\n", "long")
    assert "body_too_long" not in [issue["code"] for issue in parsed["warnings"]]


def test_issue_lines_are_file_lines() -> None:
    text = "---\nname: k\ndescription: Tracks KPIs #note\nbackground: maybe\n---\n"
    parsed = sm.parse_skill_md(text, "k")
    lines = {issue["code"]: issue.get("line") for issue in parsed["warnings"]}
    assert lines["inline_comment"] == 3
    assert lines["invalid_boolean"] == 4


def test_parse_rejects_non_text() -> None:
    with pytest.raises(TypeError):
        sm.parse_skill_md(b"---\n---\n", "x")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# validate()
# --------------------------------------------------------------------------


def test_validate_native_blocks_parse_errors_only() -> None:
    good = sm.parse_skill_md(
        "---\nname: Good_Name\ndescription: Short.\nwhen_to_use: When asked.\n---\n",
        "good",
    )
    result = sm.validate(good, sm.TARGET_NATIVE_CUBICLE)
    assert result["ok"] is True
    assert result["errors"] == []

    bad = sm.parse_skill_md("---\nname: a\nname: b\n---\n", "bad")
    result = sm.validate(bad)
    assert result["ok"] is False
    assert [issue["code"] for issue in result["errors"]] == ["duplicate_key"]


def test_validate_portable_adds_portable_issues() -> None:
    parsed = sm.parse_skill_md(
        "---\nname: Good_Name\ndescription: Short.\nwhen_to_use: When asked.\n---\n",
        "good",
    )
    result = sm.validate(parsed, sm.TARGET_PORTABLE)
    assert result["ok"] is False
    codes = {issue["code"] for issue in result["errors"]}
    assert codes == {"name_invalid_characters", "unsupported_key"}

    missing = sm.parse_skill_md("# no frontmatter\n", "x")
    result = sm.validate(missing, sm.TARGET_PORTABLE)
    error_codes = {issue["code"] for issue in result["errors"]}
    warning_codes = {issue["code"] for issue in result["warnings"]}
    assert "missing_frontmatter" in error_codes
    assert not error_codes & warning_codes


def test_validate_rejects_unknown_target_and_version() -> None:
    parsed = sm.parse_skill_md("", "x")
    with pytest.raises(ValueError):
        sm.validate(parsed, "claude_ai")
    with pytest.raises(ValueError):
        sm.validate({**parsed, "contract_version": 99})


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

_TRICKY_STRINGS = [
    "yes",
    "no",
    "on",
    "1",
    "0x1F",
    "1e3",
    ".inf",
    "2024-01-01",
    "~",
    "null",
    "",
    "  leading",
    "trailing  ",
    "key: value",
    "a # comment",
    "#hash",
    "---",
    "...",
    "- item",
    "? question",
    '"double"',
    "'single'",
    "multi\nline",
    "crlf\r\nline",
    "tab\there",
    "é ü 中文 😀",
    "\u0007bell",
    " separator",
    "\ufeffbom",
    "*alias",
    "&anchor",
    "!tag",
    "%directive",
    "@at",
    "`tick",
    "{a: 1}",
    "[1]",
    "<b>x</b>",
    "a\n---\nb",
    "---\n",
]


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("value", _TRICKY_STRINGS)
def test_render_frontmatter_round_trips(value: str, newline: str) -> None:
    block = sm.render_frontmatter(
        {
            "flag": True,
            "description": value,
            "name": "skill",
            "metadata": {"note": value, "list": [value, False, 3]},
        },
        newline=newline,
    )
    assert block.startswith("---" + newline)
    assert block.endswith(newline + "---" + newline)
    frontmatter, body, _ = sm.split_frontmatter(block + "body")
    assert body == "body"
    assert frontmatter is not None
    parsed = sm.parse_skill_md(block + "body", "skill")
    assert parsed["status"] == sm.STATUS_OK
    assert parsed["fields"]["description"] == value
    assert parsed["fields"]["metadata"] == {
        "note": value,
        "list": [value, "false", "3"],
    }
    # Deterministic key order: name, description, then insertion order.
    keys = [
        line.split(":", 1)[0]
        for line in block.split(newline)
        if line and not line.startswith((" ", "-")) and ":" in line
    ]
    assert keys[:2] == ["name", "description"]


def test_render_frontmatter_empty_and_invalid() -> None:
    assert sm.render_frontmatter({}) == "---\n---\n"
    with pytest.raises(ValueError):
        sm.render_frontmatter({"ratio": 0.5})
    with pytest.raises(TypeError):
        sm.render_frontmatter({1: "x"})  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        sm.render_frontmatter({"name": "x"}, newline="\r")


def test_render_skill_md() -> None:
    text = sm.render_skill_md(
        "pdf-tools",
        "  Extracts text from PDF files.\n  Use when a PDF\n arrives.  ",
        "\n\n# Body\ntext",
        extra={"allowed-tools": ["Read", "Bash"], "disable-model-invocation": False},
    )
    assert text == (
        "---\nname: pdf-tools\ndescription: Extracts text from PDF files. Use when a PDF arrives.\n"
        "allowed-tools:\n- Read\n- Bash\ndisable-model-invocation: false\n---\n\n"
        "# Body\ntext\n"
    )
    parsed = sm.parse_skill_md(text, "pdf-tools")
    assert parsed["status"] == sm.STATUS_OK
    assert parsed["fields"]["disable-model-invocation"] is False
    assert parsed["warnings"] == []


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"name": "", "description": "d", "body": "b"}, ValueError),
        ({"name": "n", "description": "  ", "body": "b"}, ValueError),
        ({"name": "n", "description": "d", "body": "---\nname: x\n---\n"}, ValueError),
        (
            {"name": "n", "description": "d", "body": "b", "extra": {"name": "x"}},
            ValueError,
        ),
        ({"name": 1, "description": "d", "body": "b"}, TypeError),
    ],
)
def test_render_skill_md_rejects_bad_input(kwargs: dict, error: type) -> None:
    with pytest.raises(error):
        sm.render_skill_md(**kwargs)


# --------------------------------------------------------------------------
# set_frontmatter_description / ensure_frontmatter
# --------------------------------------------------------------------------


def test_set_description_preserves_native_keys_and_order() -> None:
    text = (
        "---\n"
        "# Owner: data team\n"
        "name: exporter\n"
        "description: Old.\n"
        "allowed-tools:\n  - Read\n  - Bash\n"
        "disable-model-invocation: no\n"
        "metadata:\n  tier: gold  # billing tier\n"
        "---\nBody\n"
    )
    output = sm.set_frontmatter_description(text, "Exports CSVs. Use when asked.")
    assert output == text.replace(
        "description: Old.", 'description: "Exports CSVs. Use when asked."'
    )
    parsed = sm.parse_skill_md(output, "exporter")
    assert parsed["fields"]["allowed-tools"] == ["Read", "Bash"]
    assert parsed["fields"]["disable-model-invocation"] is False
    assert parsed["fields"]["metadata"] == {"tier": "gold"}


def test_set_description_escapes_control_characters() -> None:
    output = sm.set_frontmatter_description(
        "---\nname: x\n---\n", "Bell \u0007 and \u0085 marks."
    )
    parsed = sm.parse_skill_md(output, "x")
    assert parsed["status"] == sm.STATUS_OK
    assert parsed["fields"]["description"] == "Bell \u0007 and marks."


def test_set_description_rejects_empty_value() -> None:
    with pytest.raises(ValueError):
        sm.set_frontmatter_description("---\nname: x\n---\n", "   ")


def test_set_description_is_idempotent() -> None:
    text = "---\nname: x\ndescription: >\n  old\n---\nbody"
    once = sm.set_frontmatter_description(text, "New value. Use when needed.")
    twice = sm.set_frontmatter_description(once, "New value. Use when needed.")
    assert once == twice


def test_ensure_frontmatter_report_contents() -> None:
    content = "---\nname: a\nname: b\n---\nBody\n"
    output, report = sm.ensure_frontmatter(content, "skill", "Does it. Use when.")
    assert report["action"] == "replaced"
    assert report["status_before"] == sm.STATUS_INVALID_YAML
    assert report["error_codes_before"] == ["duplicate_key"]
    assert report["discarded_frontmatter"] == "name: a\nname: b\n"
    assert report["bom_removed"] is False
    assert output.endswith("---\nBody\n")


def test_ensure_frontmatter_replaces_not_evaluated_block() -> None:
    content = "---\nname: &a x\n---\nBody\n"
    output, report = sm.ensure_frontmatter(content, "skill", "Does it. Use when.")
    assert report["action"] == "replaced"
    assert sm.parse_skill_md(output, "skill")["status"] == sm.STATUS_OK


def test_ensure_frontmatter_requires_name_and_description() -> None:
    with pytest.raises(ValueError):
        sm.ensure_frontmatter("body", "", "desc")
    with pytest.raises(ValueError):
        sm.ensure_frontmatter("body", "name", "")


# --------------------------------------------------------------------------
# effective_description / extract_link_targets
# --------------------------------------------------------------------------


def test_effective_description_caps_and_filters() -> None:
    long_text = "Word " * 400 + "Use when needed."
    parsed = sm.parse_skill_md(f"---\nname: x\ndescription: {long_text}\n---\n", "x")
    value = sm.effective_description(parsed)
    assert value is not None
    assert len(value) == sm.DEFAULT_CATALOG_DESCRIPTION_CAP
    assert value.endswith("\u2026")
    assert sm.effective_description(parsed, cap=40) == (
        sm.normalize_whitespace(long_text)[:39].rstrip() + "\u2026"
    )

    fallback = sm.parse_skill_md("# Heading only\n", "x")
    assert sm.effective_description(fallback) == "Heading only"
    assert sm.effective_description(fallback, frontmatter_only=True) is None

    broken = sm.parse_skill_md("---\nname: a\nname: b\n---\n# H\n", "x")
    assert sm.effective_description(broken, frontmatter_only=True) is None
    assert sm.effective_description({"contract_version": 2, "description": "x"}) is None
    with pytest.raises(ValueError):
        sm.effective_description(parsed, cap=1)


def test_extract_link_targets() -> None:
    body = (
        "See [the form](reference/forms.md) and ![chart](assets/chart.png).\n"
        "Run [script](scripts/run.py#main) or [query](data.csv?x=1).\n"
        "External [site](https://example.com), [mail](mailto:a@b.c),\n"
        "[root](/etc/passwd), [anchor](#top), [proto](//host/x).\n"
        "Duplicate [again](reference/forms.md) and <reference/other.md> autolink.\n"
        "Inline `[code](not/a/link.md)` is ignored.\n"
        "```\n[fenced](fenced/link.md)\n```\n"
        '[ref]: templates/letter.md "Title"\n'
        "[^1]: footnote text\n"
        "[angle](<docs/with space.md>)\n"
    )
    assert sm.extract_link_targets(body) == [
        "reference/forms.md",
        "assets/chart.png",
        "scripts/run.py",
        "data.csv",
        "templates/letter.md",
        "docs/with space.md",
    ]
