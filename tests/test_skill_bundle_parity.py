"""The skill-bundle prompt states every rule the backend validator enforces.

``src/_skill_bundle_prompt.py`` tells the model how to shape companion
files; ``backend/app/skills/generated_bundle.py`` refuses a bundle that
breaks a rule, and a refused bundle discards the whole paid generation.
These tests pin the two sides together (C4d-G1, B1-hygiene-17): the wire
constants, the per-folder file types, the file-name pattern, the reserved
names, the size limits and the shell shebang. In a standalone CLI checkout
(no ``backend/``) the backend comparisons skip, following
``tests/backend_boundary.py``.
"""

from __future__ import annotations

import re

import pytest

from src import _skill_bundle_prompt as prompt
from tests import backend_boundary


def _backend():
    return backend_boundary.import_backend("app.skills.generated_bundle")


def test_wire_constants_match() -> None:
    gb = _backend()
    assert prompt.SKILL_BUNDLE_OUTPUT_FORMAT == gb.OUTPUT_FORMAT
    assert prompt.SKILL_BUNDLE_VERSION == gb.BUNDLE_VERSION


def test_folders_and_file_types_match() -> None:
    gb = _backend()
    assert set(prompt.COMPANION_EXTENSIONS) == set(gb.COMPANION_DIRS)
    for directory, extensions in prompt.COMPANION_EXTENSIONS.items():
        assert set(extensions) == set(gb.COMPANION_DIRS[directory]), directory
        assert len(extensions) == len(set(extensions)), directory


def test_every_documented_path_is_accepted_and_every_other_type_refused() -> None:
    gb = _backend()
    every_extension = {
        extension
        for extensions in gb.COMPANION_DIRS.values()
        for extension in extensions
    }
    for directory, extensions in prompt.COMPANION_EXTENSIONS.items():
        for extension in extensions:
            assert gb.companion_path_violation(f"{directory}/file{extension}") is None
        for extension in every_extension - set(extensions):
            assert gb.companion_path_violation(f"{directory}/file{extension}")


def test_file_name_rules_match() -> None:
    gb = _backend()
    assert prompt.FILE_NAME_PATTERN == gb._SEGMENT.pattern
    for name in gb._RESERVED_NAMES:
        # Every reserved name is either impossible under the stated pattern
        # (a leading dot) or named in the prompt.
        assert (
            not re.match(prompt.FILE_NAME_PATTERN, name)
            or name in prompt.RESERVED_FILE_NAMES
        ), name
    for name in prompt.RESERVED_FILE_NAMES:
        assert name in gb._RESERVED_NAMES


def test_size_limits_match() -> None:
    gb = _backend()
    assert prompt.MAX_FILE_KB * 1024 == gb.MAX_FILE_BYTES
    assert prompt.MAX_TOTAL_KB * 1024 == gb.MAX_TOTAL_BYTES
    # The suggested count stays inside the backend gate (SKILL.md included).
    assert prompt.PROMPT_MAX_COMPANION_FILES + 1 <= gb.MAX_FILES


def test_the_stated_shebang_satisfies_the_backend() -> None:
    gb = _backend()
    assert "``#!/usr/bin/env bash``" in prompt.STANDALONE_SKILL_BUNDLE_PROMPT
    assert gb._SHEBANG.match("#!/usr/bin/env bash\necho ok\n")


def test_prompt_states_every_rule() -> None:
    text = prompt.STANDALONE_SKILL_BUNDLE_PROMPT
    for directory, extensions in prompt.COMPANION_EXTENSIONS.items():
        assert f"``{directory}/``: {' '.join(extensions)}" in text, directory
    for pin in (
        "starts with a letter or digit",
        "no spaces, at most 100 characters",
        "``params.json``",
        "differ only in letter case",
        f"at most {prompt.MAX_FILE_KB} KB",
        f"at most {prompt.MAX_TOTAL_KB} KB",
        "outside code blocks",
        "the file must parse",
    ):
        assert pin in text, pin


def test_a_prompt_conforming_bundle_validates_end_to_end() -> None:
    """A bundle written exactly as the prompt describes passes the gate."""
    gb = _backend()
    body = (
        "# Score Leads\n\nUse the [rubric](references/rubric.md) and the "
        "[template](templates/report.md).\n\nRun from this skill's folder: "
        "`python3 scripts/score.py /abs/leads.csv` then "
        "`bash scripts/tidy.sh /abs/out`.\n"
    )
    files = prompt.normalize_bundle_files(
        [
            {"path": "references/rubric.md", "content": "# Rubric\n"},
            {"path": "templates/report.md", "content": "# Report\n"},
            {
                "path": "scripts/score.py",
                "content": "import sys\nprint(sys.argv[1])\n",
                "usage": "execute",
            },
            {
                "path": "scripts/tidy.sh",
                "content": "#!/usr/bin/env bash\necho ok\n",
                "usage": "execute",
            },
        ]
    )
    result = gb.validate_generated_files(files, body)
    assert len(result.files) == 4
    assert result.warnings == []


def test_daemon_does_not_cut_a_list_the_backend_would_accept() -> None:
    """Truncating at the prompt's suggested count broke SKILL.md links."""
    gb = _backend()
    names = [f"references/r{i}.md" for i in range(gb.MAX_FILES - 1)]
    body = "# S\n" + "\n".join(f"[r]({name})" for name in names) + "\n"
    files = prompt.normalize_bundle_files(
        [{"path": name, "content": "x\n"} for name in names]
    )
    assert len(files) == gb.MAX_FILES - 1
    assert len(gb.validate_generated_files(files, body).files) == gb.MAX_FILES - 1


@pytest.mark.parametrize(
    "path",
    ["assets/notes.md", "assets/config.yml", "references/my guide.md"],
)
def test_an_unlinked_stray_file_never_fails_the_bundle(path: str) -> None:
    gb = _backend()
    result = gb.validate_generated_files(
        [
            {"path": "references/rubric.md", "content": "x\n", "usage": "read"},
            {"path": path, "content": "x\n", "usage": "read"},
        ],
        "# S\n[r](references/rubric.md)\n",
    )
    assert [p for p, _d, _m in result.files] == ["references/rubric.md"]
