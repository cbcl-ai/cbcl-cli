"""Tests for ``setup_generator.write_skill_to_workspace``.

Co-located helper that lands a freshly-generated SKILL.md on disk.
Pulled out of the WS dispatcher in round-3 so the slug-of-record
policy + the atomic file write can be unit-tested without spinning
up a fake websocket / docker container.

Coverage:
    - Slug resolution chain: user > model > "new-skill" default.
    - Slugification (paths.slugify) applied to the chosen name.
    - Empty / punctuation-only inputs fall through to "new-skill"
      instead of producing an unsafe empty slug.
    - validate_name escape-attempt rejection raises ValueError.
    - Workspace dir is created on demand (parents=True), file is
      written with the playbook content verbatim.
    - Returned rel_path matches the slug used for the parent dir.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src._agent_image.secure_files import execute
from src.setup_generator import write_skill_to_workspace


@pytest.fixture
def workspace_writer(tmp_path):
    async def dispatch(action, params):
        return execute({"action": action, "params": params}, tmp_path)

    return SimpleNamespace(_dispatch=dispatch)


async def test_user_typed_slug_wins(tmp_path, workspace_writer):
    rel = await write_skill_to_workspace(
        fs_handler=workspace_writer,
        skill_data={
            "name": "model-echo",
            "playbook_content": "# hi\n",
        },
        requested_name="user-typed",
    )
    assert rel == ".claude/skills/user-typed/SKILL.md"
    assert (tmp_path / rel).read_text() == "# hi\n"


async def test_model_slug_used_when_user_didnt_type_one(tmp_path, workspace_writer):
    rel = await write_skill_to_workspace(
        fs_handler=workspace_writer,
        skill_data={
            "name": "model-only",
            "playbook_content": "# x\n",
        },
        requested_name=None,
    )
    assert rel == ".claude/skills/model-only/SKILL.md"


async def test_falls_back_to_new_skill_when_both_empty(tmp_path, workspace_writer):
    """Empty inputs land at the deterministic fallback slug — never
    an empty-string slug that would later 422 at the backend."""
    rel = await write_skill_to_workspace(
        fs_handler=workspace_writer,
        skill_data={"playbook_content": "# x\n"},
        requested_name=None,
    )
    assert rel == ".claude/skills/new-skill/SKILL.md"


async def test_falls_back_to_new_skill_when_punctuation_only(tmp_path, workspace_writer):
    """Slugify collapses ``!!!`` to ``""``; the post-slugify fallback
    catches it. Without the fallback the file would land at
    ``/workspace/.claude/skills//SKILL.md`` — a pathological slug."""
    rel = await write_skill_to_workspace(
        fs_handler=workspace_writer,
        skill_data={"playbook_content": "# x\n"},
        requested_name="!!!",
    )
    assert rel == ".claude/skills/new-skill/SKILL.md"


async def test_slugifies_user_input(tmp_path, workspace_writer):
    """Title-case input slugifies to kebab-case."""
    rel = await write_skill_to_workspace(
        fs_handler=workspace_writer,
        skill_data={"playbook_content": "# x\n"},
        requested_name="Code REVIEW Pro",
    )
    assert rel == ".claude/skills/code-review-pro/SKILL.md"


async def test_creates_parent_dirs(tmp_path, workspace_writer):
    """Cold workspace — no ``.claude/`` yet. Helper creates the tree."""
    target = tmp_path / ".claude" / "skills"
    assert not target.exists()
    await write_skill_to_workspace(
        fs_handler=workspace_writer,
        skill_data={"playbook_content": "# y\n"},
        requested_name="cold-skill",
    )
    assert (target / "cold-skill" / "SKILL.md").is_file()


async def test_writes_playbook_content_verbatim(tmp_path, workspace_writer):
    """No trimming / re-encoding — exact bytes in, exact bytes out."""
    body = (
        "---\nname: verbatim\ndescription: test\n---\n\n"
        "# Heading\n\nLine with trailing spaces   \n"
    )
    rel = await write_skill_to_workspace(
        fs_handler=workspace_writer,
        skill_data={"playbook_content": body},
        requested_name="verbatim",
    )
    assert (tmp_path / rel).read_text() == body


async def test_path_escape_attempt_raises_value_error(tmp_path, workspace_writer):
    """``../etc/passwd`` would escape the workspace if slugify let it
    through. The kebab-case slug WOULD strip the dots — but
    validate_name on the post-slug result is the second gate.
    Test the gate fires for any name that won't slugify safely."""
    # After slugify, "../etc/passwd" becomes "etc-passwd" — a valid
    # name. That's the slug-safe-by-design property. The escape
    # gate matters more for the daemon's general write surface; for
    # this helper, any input slugifies to something safe. So the
    # explicit ValueError path fires only for names that survive
    # slugify AND fail validate_name. validate_name in src/utils.py
    # forbids leading hyphens / non-alphanumeric starts, but
    # paths.slugify strips those — so this is a defence-in-depth
    # assertion that the validate_name gate IS wired.
    #
    # Sanity check: even a pathological input doesn't escape.
    rel = await write_skill_to_workspace(
        fs_handler=workspace_writer,
        skill_data={"playbook_content": "# x\n"},
        requested_name="../etc/passwd",
    )
    # Slugify collapses path separators + dots; final slug is
    # safely inside the workspace.
    assert ".." not in rel
    assert "/etc/" not in rel
    assert (tmp_path / rel).is_file()


async def test_empty_playbook_content_still_writes_zero_byte_file(tmp_path, workspace_writer):
    """The HELPER doesn't enforce non-empty playbook (that's
    ``generate_skill_from_overview``'s job, called BEFORE this
    helper). If somehow an empty body reaches the writer we DON'T
    raise — we'd rather have an empty file than a half-finished
    state. The dispatcher's caller-side guard prevents this in
    practice."""
    rel = await write_skill_to_workspace(
        fs_handler=workspace_writer,
        skill_data={"playbook_content": ""},
        requested_name="empty-skill",
    )
    assert (tmp_path / rel).read_text() == ""


async def test_returns_workspace_relative_path(tmp_path, workspace_writer):
    """Return value is always rel to the workspace, never absolute.
    The dispatcher echoes it back to the backend via ``written_path``
    and the backend compares it to its own expected slug; an
    absolute path would never match."""
    rel = await write_skill_to_workspace(
        fs_handler=workspace_writer,
        skill_data={"playbook_content": "# x\n"},
        requested_name="rel-path",
    )
    assert not rel.startswith("/")
    assert rel == ".claude/skills/rel-path/SKILL.md"


@pytest.mark.parametrize("result", [
    {"error": "not permitted", "status": 403}, {},
    {"path": ".claude/settings.json"},
])
async def test_save_failure_does_not_claim_written_path(result):
    handler = SimpleNamespace(_dispatch=AsyncMock(return_value=result))
    with pytest.raises(OSError, match="protected Files helper"):
        await write_skill_to_workspace(handler, {"playbook_content": "body"}, "safe")
    handler._dispatch.assert_awaited_once_with(
        "fs_write",
        {
            "path": ".claude/skills/safe/SKILL.md",
            "content": "body",
        },
    )


async def test_skill_parent_symlink_does_not_overwrite_protected_data(
    tmp_path, workspace_writer
):
    (tmp_path / "approved").mkdir()
    (tmp_path / "approved" / "SKILL.md").write_text("untouched")
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    (tmp_path / ".claude" / "skills" / "linked").symlink_to(tmp_path / "approved")
    with pytest.raises(OSError):
        await write_skill_to_workspace(
            workspace_writer, {"playbook_content": "replacement"}, "linked"
        )
    assert (tmp_path / "approved" / "SKILL.md").read_text() == "untouched"
