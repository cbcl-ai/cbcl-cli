"""The two protected-path copies agree, case variants included (FF-1).

``communicator/src/_agent_image/secure_files.py`` is the Files boundary; the
backend mirror (``app.transport.office_fs.protected_path``) gates the
shared-filesystem local mode. A workspace on a case-insensitive host
filesystem (macOS APFS through a Docker Desktop bind) resolves ``.GIT`` or
``.CLAUDE`` to the protected entry, so both copies casefold. This pins them to
each other in the lane that can import both.
"""

from __future__ import annotations

import pytest

from src._agent_image import secure_files as helper
from tests.backend_boundary import import_backend

office_fs = import_backend("app.transport.office_fs")

_SAMPLES = [
    ".claude",
    ".claude/settings.json",
    ".claude/skills/demo/SKILL.md",
    ".claude/skills/demo/.env",
    ".scripts/demo/.secrets.json",
    ".scripts/demo/main.py",
    "outputs/report.md",
    ".CLAUDE",
    ".CLAUDE/settings.json",
    ".CLAUDE/CLAUDE.md",
    ".Claude/SKILLS/x",
    ".Claude/SKILLS/demo/SKILL.md",
    ".Claude/SKILLS/demo/.ENV",
    "x/.SECRETS.JSON",
    ".scripts/x/.Secrets.json.bak",
    ".Env",
    ".ENV.PRODUCTION",
    ".Env.Sample",
    ".GIT/config",
    ".Ssh/id_rsa",
    ".CUBICLE/sessions.json",
    "agents/x/.CLAUDE/settings.json",
    ".Cubicle-Files-tmp",
]

_PROTECTED = {
    ".claude",
    ".claude/settings.json",
    ".claude/skills/demo/.env",
    ".scripts/demo/.secrets.json",
    ".CLAUDE",
    ".CLAUDE/settings.json",
    ".CLAUDE/CLAUDE.md",
    ".Claude/SKILLS/demo/.ENV",
    "x/.SECRETS.JSON",
    ".scripts/x/.Secrets.json.bak",
    ".Env",
    ".ENV.PRODUCTION",
    ".GIT/config",
    ".Ssh/id_rsa",
    ".CUBICLE/sessions.json",
    "agents/x/.CLAUDE/settings.json",
    ".Cubicle-Files-tmp",
}


def test_policy_constants_match():
    assert office_fs._PROTECTED_NAMES == frozenset(helper._PROTECTED_NAMES)
    assert office_fs._PROTECTED_FILE_PREFIXES == helper._PROTECTED_FILE_PREFIXES


@pytest.mark.parametrize("sample", _SAMPLES)
def test_both_copies_decide_alike(sample):
    parts = tuple(sample.split("/"))
    expected = sample in _PROTECTED
    assert helper.protected_path(parts) is expected, sample
    assert office_fs.protected_path(parts) is expected, sample
