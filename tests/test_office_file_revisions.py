"""Layer B of the truthful Office file writes (remediation F02).

The container helper gains ``sha256``/``utf8_valid`` on ``fs_read``, a cheap
``fs_hash`` and a compare-and-swap ``fs_write_revision`` that runs inside the
helper's workspace lock. The daemon relay maps an OLD image's unknown-action
answer for these actions to ``426 office_image_upgrade_required`` so the
backend falls back to read-back writes instead of mistaking it for a refusal.
These run the real helper on synthetic Linux roots (``/proc/self/fdinfo``).
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from src import fs_handler as relay
from src._agent_image import secure_files as files


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    (root / ".claude" / "skills" / "demo").mkdir(parents=True)
    return root


def request(root, action, **params):
    return files.execute({"action": action, "params": params}, root)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


SKILL = ".claude/skills/demo/SKILL.md"


def test_read_reports_raw_sha256_and_utf8_validity(workspace):
    (workspace / SKILL).write_bytes(b"---\nname: demo\n---\nbody\n")
    result = request(workspace, "fs_read", path=SKILL)
    assert result["content"].startswith("---")
    assert result["sha256"] == sha(b"---\nname: demo\n---\nbody\n")
    assert result["utf8_valid"] is True

    raw = b"caf\xe9 latin-1 bytes\n"
    (workspace / SKILL).write_bytes(raw)
    result = request(workspace, "fs_read", path=SKILL)
    assert result["utf8_valid"] is False
    # The revision is of the RAW bytes, not of the lossy decoded text.
    assert result["sha256"] == sha(raw)
    assert result["sha256"] != sha(result["content"].encode())


def test_read_of_binary_keeps_content_null_but_hashes(workspace):
    payload = bytes(range(256))
    (workspace / ".claude/skills/demo/blob.bin").write_bytes(payload)
    result = request(workspace, "fs_read", path=".claude/skills/demo/blob.bin")
    assert result["content"] is None
    assert result["file_kind"] == "binary"
    assert result["size"] == 256
    assert result["sha256"] == sha(payload)


def test_hash_file_missing_and_directory(workspace):
    (workspace / SKILL).write_bytes(b"hello")
    assert request(workspace, "fs_hash", path=SKILL) == {
        "path": SKILL,
        "exists": True,
        "type": "file",
        "size": 5,
        "sha256": sha(b"hello"),
    }
    assert request(workspace, "fs_hash", path=".claude/skills/demo/nope.md") == {
        "path": ".claude/skills/demo/nope.md",
        "exists": False,
    }
    assert (
        request(workspace, "fs_hash", path=".claude/skills/missing/SKILL.md")["exists"]
        is False
    )
    folder = request(workspace, "fs_hash", path=".claude/skills/demo")
    assert folder["exists"] is True and folder["type"] == "directory"


def test_write_revision_matching_expected_replaces(workspace):
    (workspace / SKILL).write_bytes(b"old")
    result = request(
        workspace,
        "fs_write_revision",
        path=SKILL,
        content="new",
        expected_sha256=sha(b"old"),
    )
    assert result["outcome"] == "written"
    assert result["sha256"] == sha(b"new")
    assert result["previous_sha256"] == sha(b"old")
    assert (workspace / SKILL).read_bytes() == b"new"


def test_write_revision_mismatch_refuses_without_touching_the_file(workspace):
    target = workspace / SKILL
    target.write_bytes(b"current")
    before = os.stat(target)
    result = request(
        workspace,
        "fs_write_revision",
        path=SKILL,
        content="stale edit",
        expected_sha256=sha(b"what the editor loaded"),
    )
    assert result["status"] == 409
    assert result["code"] == "revision_conflict"
    assert result["current_sha256"] == sha(b"current")
    after = os.stat(target)
    assert target.read_bytes() == b"current"
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_write_revision_expect_absent_refuses_existing_file(workspace):
    (workspace / SKILL).write_bytes(b"real playbook")
    result = request(
        workspace, "fs_write_revision", path=SKILL, content="stub", expect_absent=True
    )
    assert result["status"] == 409
    assert (workspace / SKILL).read_bytes() == b"real playbook"
    created = request(
        workspace,
        "fs_write_revision",
        path=".claude/skills/fresh/SKILL.md",
        content="stub",
        expect_absent=True,
    )
    assert created["outcome"] == "written" and created["previous_sha256"] is None
    assert (workspace / ".claude/skills/fresh/SKILL.md").read_text() == "stub"


@pytest.mark.parametrize(
    "precondition",
    [{"expect_absent": True}, {"expected_sha256": "a" * 64}],
)
def test_write_revision_over_a_folder_with_a_precondition_is_a_conflict(
    workspace, precondition
):
    """A folder at the path conflicts (409) like the read-back path says,
    rather than the generic 'only regular files' policy error (400)."""
    folder = workspace / ".claude/skills/demo/templates"
    folder.mkdir()
    result = request(
        workspace,
        "fs_write_revision",
        path=".claude/skills/demo/templates",
        content="x",
        **precondition,
    )
    assert result["status"] == 409
    assert result["code"] == "revision_conflict"
    assert result["current_sha256"] is None
    assert folder.is_dir()


def test_write_revision_identical_content_is_unchanged_without_rename(workspace):
    target = workspace / SKILL
    target.write_bytes(b"same")
    inode = os.stat(target).st_ino
    result = request(
        workspace,
        "fs_write_revision",
        path=SKILL,
        content="same",
        expected_sha256=sha(b"an older revision"),
        expect_absent=True,
    )
    # Identical content wins over the precondition: a retried write that
    # already landed must not be reported as a conflict.
    assert result["outcome"] == "unchanged"
    assert os.stat(target).st_ino == inode


def test_write_revision_rejects_malformed_expected_hash(workspace):
    result = request(
        workspace, "fs_write_revision", path=SKILL, content="x", expected_sha256="abc"
    )
    assert result["status"] == 400


@pytest.mark.parametrize(
    "path",
    [".claude/skills/gone/SKILL.md", "reports/2026/q3/summary.md"],
)
def test_refused_revision_write_creates_no_missing_parent(workspace, path):
    """B3-bugs-02: a revision-checked write whose parent folder is missing is
    a conflict, and it must not leave the parent folders behind."""
    result = request(
        workspace, "fs_write_revision", path=path, content="edit",
        expected_sha256=sha(b"what the editor loaded"),
    )
    assert result["status"] == 409
    assert result["code"] == "revision_conflict"
    assert result["current_sha256"] is None
    assert not (workspace / path).parent.exists()
    listing = request(workspace, "fs_list_skills")
    assert "gone" not in {entry["name"] for entry in listing["skills"]}


def test_exclusive_create_under_a_missing_parent_still_writes(workspace):
    result = request(
        workspace, "fs_write_revision", path="reports/2026/q3/summary.md",
        content="new", expect_absent=True,
    )
    assert result["outcome"] == "written" and result["previous_sha256"] is None
    assert (workspace / "reports/2026/q3/summary.md").read_text() == "new"


def _changing_during_read(monkeypatch):
    """Alternate a file descriptor's re-validation between its real stat and a
    grown file, so the start and end stats of every read disagree (the real
    "file changed while being read" check fires, as for an appending writer)."""
    original = files.SecureWorkspace._validate_fd
    calls = [0]

    def validate(self, descriptor, *, directory):
        metadata = original(self, descriptor, directory=directory)
        if directory:
            return metadata
        calls[0] += 1
        if calls[0] % 2:
            return metadata
        return os.stat_result((
            metadata.st_mode, metadata.st_ino, metadata.st_dev, metadata.st_nlink,
            metadata.st_uid, metadata.st_gid, metadata.st_size + 1024,
            int(metadata.st_atime), int(metadata.st_mtime), int(metadata.st_ctime),
        ))

    monkeypatch.setattr(files.SecureWorkspace, "_validate_fd", validate)


def test_binary_read_while_written_returns_metadata_without_revision(
    workspace, monkeypatch
):
    """B3-bugs-03: a binary still being written keeps its metadata read (the
    revision is best-effort); a text read of a changing file stays a 400."""
    (workspace / ".claude/skills/demo/blob.bin").write_bytes(bytes(range(256)))
    (workspace / SKILL).write_bytes(b"text")
    _changing_during_read(monkeypatch)
    result = request(workspace, "fs_read", path=".claude/skills/demo/blob.bin")
    assert "error" not in result, result
    assert result["file_kind"] == "binary"
    assert result["content"] is None
    assert result["sha256"] is None
    text = request(workspace, "fs_read", path=SKILL)
    assert text["status"] == 400
    assert "changed while being read" in text["error"]
    strict = request(workspace, "fs_hash", path=".claude/skills/demo/blob.bin")
    assert strict["status"] == 400


@pytest.mark.asyncio
async def test_relay_accepts_revision_actions_and_maps_old_helper(monkeypatch):
    assert {"fs_hash", "fs_write_revision"} <= relay._ACTIONS
    # Every upgrade-gated action is forwarded, so an old image's refusal
    # reaches the 426 translation instead of the relay's own 400.
    assert relay._IMAGE_UPGRADE_ACTIONS <= relay._ACTIONS
    assert relay._ACTIONS == relay._BASE_ACTIONS | relay._IMAGE_UPGRADE_ACTIONS
    handler = relay.FsHandler(
        "a" * 64, office_id="00000000-0000-0000-0000-000000000001"
    )

    async def fake_verify(self, container_id):
        return None

    async def fake_run(arguments, payload, *, timeout, limit):
        # What an image built BEFORE this change answers for a new action.
        return (
            0,
            json.dumps(
                {"error": "Unknown or invalid filesystem request", "status": 400}
            ).encode(),
        )

    monkeypatch.setattr(relay.FsHandler, "_verify_container", fake_verify)
    monkeypatch.setattr(relay, "_run_process", fake_run)
    result = await handler._dispatch("fs_write_revision", {"path": SKILL})
    assert result["status"] == 426
    assert result["code"] == "office_image_upgrade_required"
    # A legacy action with the same helper error is passed through verbatim.
    legacy = await handler._dispatch("fs_write", {"path": SKILL})
    assert legacy == {"error": "Unknown or invalid filesystem request", "status": 400}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "params"),
    [
        # A malformed precondition and a path outside the jail: a CURRENT
        # helper refuses both with its ordinary 400.
        (
            "fs_write_revision",
            {"path": SKILL, "content": "x", "expected_sha256": "abc"},
        ),
        ("fs_hash", {"path": "../outside.md"}),
    ],
)
async def test_relay_keeps_a_genuine_revision_refusal_as_400(
    monkeypatch, workspace, action, params
):
    """T18 negative control: only the OLD image's unknown-action answer maps
    to 426. A current helper's real refusal of a revision action must reach
    the backend as the 400 it is, not as "upgrade the image"."""
    handler = relay.FsHandler(
        "c" * 64, office_id="00000000-0000-0000-0000-000000000003"
    )

    async def fake_verify(self, container_id):
        return None

    async def fake_run(arguments, payload, *, timeout, limit):
        # Run the real helper of this image on the synthetic workspace.
        answer = files.execute(json.loads(payload), workspace)
        return 0, json.dumps(answer).encode()

    monkeypatch.setattr(relay.FsHandler, "_verify_container", fake_verify)
    monkeypatch.setattr(relay, "_run_process", fake_run)
    result = await handler._dispatch(action, params)
    assert result["status"] == 400, result
    assert result.get("code") != "office_image_upgrade_required"
    assert result["error"] != relay._UNKNOWN_ACTION_ERROR


@pytest.mark.asyncio
async def test_relay_enriches_skill_listing_on_the_host(monkeypatch):
    handler = relay.FsHandler(
        "b" * 64, office_id="00000000-0000-0000-0000-000000000002"
    )

    async def fake_verify(self, container_id):
        return None

    listing = {
        "skills": [
            {
                "name": "folded",
                "display_name": "folded",
                "description": "",
                "files": [
                    {
                        "name": "SKILL.md",
                        "size": 10,
                        "type": "file",
                        "is_skill_md": True,
                    }
                ],
                "has_skill_md": True,
                "skill_md_head": (
                    "---\nname: folded\ndescription: >\n  Summarises reports.\n"
                    "  Use when a weekly report is due.\n---\n# Body\n"
                ),
                "skill_md_head_truncated": False,
                "skill_md_size": 10,
            }
        ]
    }

    async def fake_run(arguments, payload, *, timeout, limit):
        return 0, json.dumps(listing).encode()

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    monkeypatch.setattr(relay.FsHandler, "_verify_container", fake_verify)
    monkeypatch.setattr(relay, "_run_process", fake_run)
    await handler.handle_request(
        {"request_id": "r1", "action": "fs_list_skills", "params": {}}, send
    )
    skill = sent[0]["data"]["skills"][0]
    assert skill["description"] == (
        "Summarises reports. Use when a weekly report is due."
    )
    assert "skill_md_head" not in skill
    assert skill["metadata"]["status"] == "ok"


@pytest.mark.asyncio
async def test_relay_routes_skill_publication_and_maps_old_helper(monkeypatch):
    """F03: fs_skill_* reach the helper; an image built before them answers
    with its generic unknown-action error, relayed as 426 so the backend
    falls back (single-file legacy write) or refuses — never half-publishes."""
    actions = {
        "fs_skill_status",
        "fs_skill_stage_begin",
        "fs_skill_stage_put",
        "fs_skill_commit",
        "fs_skill_abort",
        "fs_skill_retire",
    }
    assert actions <= relay._ACTIONS
    handler = relay.FsHandler(
        "c" * 64, office_id="00000000-0000-0000-0000-000000000003"
    )
    sent: list[bytes] = []

    async def fake_verify(self, container_id):
        return None

    async def fake_run(arguments, payload, *, timeout, limit):
        sent.append(payload)
        return (
            0,
            json.dumps(
                {"error": "Unknown or invalid filesystem request", "status": 400}
            ).encode(),
        )

    monkeypatch.setattr(relay.FsHandler, "_verify_container", fake_verify)
    monkeypatch.setattr(relay, "_run_process", fake_run)
    for action in sorted(actions):
        result = await handler._dispatch(action, {"skill_name": "demo"})
        assert result["status"] == 426, action
        assert result["code"] == "office_image_upgrade_required"
        assert "whole-skill publication" in result["error"]
    assert [json.loads(p)["action"] for p in sent] == sorted(actions)


@pytest.mark.asyncio
async def test_relay_passes_skill_publication_results_through(monkeypatch):
    handler = relay.FsHandler(
        "d" * 64, office_id="00000000-0000-0000-0000-000000000004"
    )

    async def fake_verify(self, container_id):
        return None

    answer = {
        "error": "The skill folder changed",
        "status": 409,
        "code": "skill_bundle_conflict",
        "current_digest": "a" * 64,
    }

    async def fake_run(arguments, payload, *, timeout, limit):
        return 0, json.dumps(answer).encode()

    monkeypatch.setattr(relay.FsHandler, "_verify_container", fake_verify)
    monkeypatch.setattr(relay, "_run_process", fake_run)
    assert await handler._dispatch("fs_skill_commit", {}) == answer
