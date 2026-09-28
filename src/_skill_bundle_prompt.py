"""Multi-file skill generation (F08, ``output_format: skill_bundle_v1``).

A backend that can publish a whole skill folder atomically (it saw the
daemon's ``skill_bundles_v1`` capability) asks ``generate_skill`` for a
bundle. The standalone prompt then allows up to four companion files next
to SKILL.md; the backend validates them (``app/skills/generated_bundle.py``)
before anything is written. Every other caller keeps the single-file
contract of ``STANDALONE_SKILL_PROMPT`` unchanged.

The prompt states every rule that validator enforces (folders, file types
per folder, the file-name pattern, sizes, the shell shebang), rendered
from the constants below. ``tests/test_skill_bundle_parity.py`` keeps them
equal to the backend's, so a model that follows the prompt is never
refused for a rule it was not told.
"""

from __future__ import annotations

from typing import Any

from src._setup_prompts import STANDALONE_SKILL_PROMPT

SKILL_BUNDLE_OUTPUT_FORMAT = "skill_bundle_v1"
SKILL_BUNDLE_VERSION = 1

# Guidance for the model only. The daemon does not cut a longer list: a
# cut could drop a file SKILL.md links, and the backend refuses an
# oversized list itself (``generated_bundle.MAX_FILES``) with a clear
# message.
PROMPT_MAX_COMPANION_FILES = 4

# Mirrors of the backend validator's rules (``generated_bundle``):
# the per-folder extension allowlist, the file-name pattern, the names a
# companion file may never take, and the size limits.
COMPANION_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "references": (".csv", ".json", ".md", ".txt", ".yaml", ".yml"),
    "scripts": (".py", ".sh"),
    "templates": (".csv", ".html", ".j2", ".json", ".md", ".txt", ".yaml", ".yml"),
    "assets": (".csv", ".json", ".txt", ".yaml"),
}
FILE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
RESERVED_FILE_NAMES = ("params.json",)
MAX_FILE_KB = 64
MAX_TOTAL_KB = 128

_COMPANION_DIRS = tuple(f"{directory}/" for directory in COMPANION_EXTENSIONS)
_FILE_TYPE_LINES = "\n".join(
    f"  - ``{directory}/``: {' '.join(extensions)}"
    for directory, extensions in COMPANION_EXTENSIONS.items()
)

_SKILL_BUNDLE_OUTPUT_SHAPE = f"""\
## Optional companion files

You MAY add a ``files`` array to the JSON object:

```json
"files": [
  {{"path": "references/rubric.md", "content": "...", "usage": "read"}},
  {{"path": "scripts/score.py", "content": "...", "usage": "execute"}}
]
```

- Add a file ONLY when it removes repetition from the body or supplies
  deterministic mechanics (a calculation, a validator, a fixed template).
  Most skills need none. At most {PROMPT_MAX_COMPANION_FILES} files.
- A path is one folder and one file name, one level deep. Text files
  only, with these file types per folder:
{_FILE_TYPE_LINES}
- A file name starts with a letter or digit and uses only letters,
  digits, ``.``, ``_`` and ``-``: no spaces, at most 100 characters. Never
  name a file ``{RESERVED_FILE_NAMES[0]}``, and never give two files names
  that differ only in letter case.
- Each file is at most {MAX_FILE_KB} KB; SKILL.md and all files together
  are at most {MAX_TOTAL_KB} KB.
- Link every file directly from the body with a relative markdown link
  (``[rubric](references/rubric.md)``) outside code blocks. A file the body
  never mentions is discarded; a link to a file you did not return makes
  the whole skill fail.
- A reference file longer than 100 lines starts with a short contents
  list.
- Scripts are Python 3.12 standard library (the file must parse) or bash;
  a ``.sh`` script starts with ``#!/usr/bin/env bash``. The body runs them
  from the skill's own folder as ``python3 scripts/<file>`` or
  ``bash scripts/<file>`` (mark them ``"usage": "execute"``) and says so
  once, e.g. "Run from this skill's folder: ``python3 scripts/score.py
  /absolute/path/leads.csv``" — agents work in another directory, so
  inputs and outputs are absolute paths. A script checks its input and
  exits non-zero with a clear message when the input is wrong.
- Escape file contents exactly like ``body`` so the JSON parses."""

STANDALONE_SKILL_BUNDLE_PROMPT = (
    STANDALONE_SKILL_PROMPT + "\n\n" + _SKILL_BUNDLE_OUTPUT_SHAPE
)


def normalize_bundle_files(raw: object) -> list[dict[str, str]]:
    """Shape-check the model's ``files`` list (the backend validates fully).

    Keeps well-formed ``{path, content, usage}`` entries under the four
    companion directories and drops anything else. The list is not cut to
    :data:`PROMPT_MAX_COMPANION_FILES`: a cut could drop a file SKILL.md
    links, and the backend refuses an oversized list with a clear message.
    """
    if not isinstance(raw, list):
        return []
    files: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        content = entry.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            continue
        if not path.startswith(_COMPANION_DIRS):
            continue
        usage = entry.get("usage")
        files.append(
            {
                "path": path,
                "content": content,
                "usage": usage if usage in ("read", "execute") else "read",
            }
        )
    return files


def apply_bundle_output(result: dict[str, Any], output_format: str | None) -> None:
    """Finish a generated skill dict for the requested output format."""
    raw_files = result.pop("files", None)
    result.pop("bundle_version", None)
    if output_format != SKILL_BUNDLE_OUTPUT_FORMAT:
        return
    result["files"] = normalize_bundle_files(raw_files)
    result["bundle_version"] = SKILL_BUNDLE_VERSION
