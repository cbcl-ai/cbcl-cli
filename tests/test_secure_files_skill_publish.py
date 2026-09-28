"""All-or-nothing skill bundle publication in the Files helper (F03).

Runs the real helper against synthetic Linux roots (``/proc`` mount ids and
``renameat2``), so these tests belong to the Linux container lane.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import re
import uuid

import pytest

from src._agent_image import secure_files as files

SKILL_MD = b"---\nname: demo\ndescription: Demo. Use when testing.\n---\n\n# Demo\n"


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    (root / ".claude" / "skills").mkdir(parents=True)
    return root


def request(root, action, **params):
    return files.execute({"action": action, "params": params}, root)


def entry(path: str, data: bytes, mode: int = 0o644) -> dict:
    return {
        "path": path,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "mode": mode,
    }


def manifest(bundle: dict[str, bytes], modes: dict[str, int] | None = None) -> dict:
    modes = modes or {}
    entries = [
        entry(path, data, modes.get(path, 0o644)) for path, data in bundle.items()
    ]
    return {
        "format": 1,
        "bundle_sha256": files.skill_bundle_sha256(entries),
        "files": entries,
        "source": {"kind": "catalog_github", "commit": "c" * 40, "revision": "git:x"},
    }


def stage(root, skill: str, bundle: dict[str, bytes]) -> str:
    publication_id = uuid.uuid4().hex
    begun = request(
        root, "fs_skill_stage_begin", publication_id=publication_id, skill_name=skill
    )
    assert begun.get("staged") is True, begun
    put = request(
        root,
        "fs_skill_stage_put",
        publication_id=publication_id,
        files=[
            {"path": path, "offset": 0, "data_base64": base64.b64encode(data).decode()}
            for path, data in bundle.items()
        ],
    )
    assert "error" not in put, put
    return publication_id


def status(root, skill: str, **extra) -> dict:
    result = request(root, "fs_skill_status", skill_name=skill, **extra)
    assert "error" not in result, result
    return result


def publish(root, skill, bundle, *, mode="update", modes=None, expected=None):
    if expected is None and mode == "update":
        live = status(root, skill)["live"]
        expected = live["digest"] if live["exists"] else "none"
    publication_id = stage(root, skill, bundle)
    params = {
        "publication_id": publication_id,
        "skill_name": skill,
        "manifest": manifest(bundle, modes),
        "mode": mode,
    }
    if expected is not None:
        params["expected_live_digest"] = expected
    return publication_id, request(root, "fs_skill_commit", **params)


def live_files(root, skill: str) -> dict[str, bytes]:
    base = root / ".claude" / "skills" / skill
    return {
        str(path.relative_to(base)): path.read_bytes()
        for path in sorted(base.rglob("*"))
        if path.is_file()
    }


def area(root):
    return root / ".claude" / ".cubicle-skill-bundles"


def test_first_install_publishes_exact_bundle(workspace):
    bundle = {"SKILL.md": SKILL_MD, "scripts/run.sh": b"#!/bin/sh\necho ok\n"}
    publication_id, result = publish(
        workspace, "demo", bundle, modes={"scripts/run.sh": 0o755}
    )
    assert result["status"] == "published", result
    assert result["swap"] == "noreplace"
    assert result["previous"]["state"] == "none"
    published = live_files(workspace, "demo")
    manifest_bytes = published.pop(".cubicle-bundle.json")
    assert published == bundle
    recorded = json.loads(manifest_bytes)
    assert recorded["publication_id"] == publication_id
    assert (
        recorded["bundle_sha256"]
        == manifest(bundle, {"scripts/run.sh": 0o755})["bundle_sha256"]
    )
    assert recorded["source"]["commit"] == "c" * 40
    script = workspace / ".claude" / "skills" / "demo" / "scripts" / "run.sh"
    assert os.stat(script).st_mode & 0o777 == 0o755
    live = status(workspace, "demo")["live"]
    assert live["managed"] is True and live["modified"] is False
    assert live["digest"] == recorded["bundle_sha256"]
    # Staging and the journal are gone; nothing is left in .claude/skills.
    assert os.listdir(area(workspace) / "staging") == []
    assert os.listdir(area(workspace) / "journal") == []
    assert os.listdir(workspace / ".claude" / "skills") == ["demo"]


def test_replace_exchanges_whole_directory_and_drops_removed_files(workspace):
    publish(
        workspace,
        "demo",
        {"SKILL.md": SKILL_MD, "old.md": b"obsolete", "ref/a.md": b"a"},
    )
    params = workspace / ".claude" / "skills" / "demo" / "params.json"
    params.write_text('{"REGION": "eu"}')
    updated = {"SKILL.md": SKILL_MD + b"v2\n", "ref/a.md": b"a2", "ref/b.md": b"b"}
    _publication_id, result = publish(workspace, "demo", updated)
    assert result["status"] == "published", result
    assert result["swap"] == "exchange"
    assert result["previous"]["state"] == "managed"
    published = live_files(workspace, "demo")
    published.pop(".cubicle-bundle.json")
    assert published.pop("params.json") == b'{"REGION": "eu"}'
    assert published == updated  # old.md removed upstream never survives
    retired = os.listdir(area(workspace) / "retired" / "demo")
    assert retired == [result["previous"]["retired_as"]]
    old = area(workspace) / "retired" / "demo" / retired[0]
    assert (old / "old.md").read_bytes() == b"obsolete"


def test_local_edit_is_reported_and_cas_refuses_stale_expectation(workspace):
    publish(workspace, "demo", {"SKILL.md": SKILL_MD})
    inspected = status(workspace, "demo")["live"]["digest"]
    (workspace / ".claude" / "skills" / "demo" / "SKILL.md").write_bytes(
        SKILL_MD + b"edited\n"
    )
    live = status(workspace, "demo")["live"]
    assert live["modified"] is True and live["digest"] != inspected
    _publication_id, result = publish(
        workspace, "demo", {"SKILL.md": SKILL_MD + b"new\n"}, expected=inspected
    )
    assert result["status"] == 409
    assert result["code"] == "skill_bundle_conflict"
    assert result["current_digest"] == live["digest"]
    assert (workspace / ".claude" / "skills" / "demo" / "SKILL.md").read_bytes() == (
        SKILL_MD + b"edited\n"
    )


def test_replacing_local_edits_reports_modified(workspace):
    publish(workspace, "demo", {"SKILL.md": SKILL_MD})
    (workspace / ".claude" / "skills" / "demo" / "notes.md").write_text("mine")
    _publication_id, result = publish(workspace, "demo", {"SKILL.md": SKILL_MD})
    assert result["previous"]["state"] == "modified"
    assert "notes.md" not in live_files(workspace, "demo")


def test_unmanaged_legacy_folder_is_replaced_and_reported(workspace):
    legacy = workspace / ".claude" / "skills" / "demo"
    legacy.mkdir()
    (legacy / "SKILL.md").write_bytes(b"legacy")
    (legacy / "stale.md").write_bytes(b"stale")
    live = status(workspace, "demo")["live"]
    assert live["exists"] and not live["managed"]
    _publication_id, result = publish(workspace, "demo", {"SKILL.md": SKILL_MD})
    assert result["previous"]["state"] == "unmanaged"
    assert set(live_files(workspace, "demo")) == {"SKILL.md", ".cubicle-bundle.json"}


def test_ensure_links_an_existing_folder_without_republishing(workspace):
    publish(workspace, "demo", {"SKILL.md": SKILL_MD})
    before = live_files(workspace, "demo")
    publication_id, result = publish(
        workspace, "demo", {"SKILL.md": b"other"}, mode="ensure"
    )
    assert result["status"] == "exists"
    assert result["live"]["managed"] is True
    assert live_files(workspace, "demo") == before
    assert publication_id not in os.listdir(area(workspace) / "staging")


def test_ensure_publishes_when_nothing_is_installed(workspace):
    _publication_id, result = publish(
        workspace, "demo", {"SKILL.md": SKILL_MD}, mode="ensure"
    )
    assert result["status"] == "published"


def test_create_refuses_an_existing_folder(workspace):
    (workspace / ".claude" / "skills" / "demo").mkdir()
    _publication_id, result = publish(
        workspace, "demo", {"SKILL.md": SKILL_MD}, mode="create"
    )
    assert result["status"] == 409 and result["code"] == "skill_exists"


def test_update_requires_an_expectation(workspace):
    publication_id = stage(workspace, "demo", {"SKILL.md": SKILL_MD})
    result = request(
        workspace,
        "fs_skill_commit",
        publication_id=publication_id,
        skill_name="demo",
        manifest=manifest({"SKILL.md": SKILL_MD}),
        mode="update",
    )
    assert result["status"] == 400


@pytest.mark.parametrize(
    "staged,declared,reason",
    [
        ({"SKILL.md": SKILL_MD, "extra.md": b"x"}, {"SKILL.md": SKILL_MD}, "extra"),
        ({"SKILL.md": SKILL_MD}, {"SKILL.md": SKILL_MD, "b.md": b"b"}, "missing"),
        ({"SKILL.md": b"tampered"}, {"SKILL.md": SKILL_MD}, "hash"),
        ({"SKILL.md": b"   \n"}, {"SKILL.md": b"   \n"}, "empty"),
        ({"SKILL.md": b"\xff\xfe"}, {"SKILL.md": b"\xff\xfe"}, "utf8"),
    ],
)
def test_staged_set_must_equal_manifest(workspace, staged, declared, reason):
    publication_id = stage(workspace, "demo", staged)
    result = request(
        workspace,
        "fs_skill_commit",
        publication_id=publication_id,
        skill_name="demo",
        manifest=manifest(declared),
        mode="update",
        expected_live_digest="none",
    )
    assert result["status"] == 400, (reason, result)
    assert not (workspace / ".claude" / "skills" / "demo").exists()


def test_manifest_without_skill_md_or_with_wrong_identity_is_refused(workspace):
    publication_id = stage(workspace, "demo", {"README.md": b"r"})
    no_skill = request(
        workspace,
        "fs_skill_commit",
        publication_id=publication_id,
        skill_name="demo",
        manifest=manifest({"README.md": b"r"}),
        mode="create",
    )
    assert no_skill["status"] == 400
    bad = manifest({"SKILL.md": SKILL_MD})
    bad["bundle_sha256"] = "0" * 64
    publication_id = stage(workspace, "demo", {"SKILL.md": SKILL_MD})
    wrong = request(
        workspace,
        "fs_skill_commit",
        publication_id=publication_id,
        skill_name="demo",
        manifest=bad,
        mode="create",
    )
    assert wrong["status"] == 400


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "params.json",
        ".cubicle-bundle.json",
        "ref/.cubicle-bundle.json",
        "../escape.md",
        ".git/config",
        "a//b.md",
        "/abs.md",
        ".claude/settings.json",
    ],
)
def test_stage_put_refuses_reserved_and_unsafe_paths(workspace, path):
    publication_id = uuid.uuid4().hex
    request(
        workspace,
        "fs_skill_stage_begin",
        publication_id=publication_id,
        skill_name="demo",
    )
    result = request(
        workspace,
        "fs_skill_stage_put",
        publication_id=publication_id,
        files=[{"path": path, "offset": 0, "data_base64": "eA=="}],
    )
    assert result["status"] == 400


def test_stage_put_chunks_append_in_order(workspace):
    publication_id = uuid.uuid4().hex
    request(
        workspace,
        "fs_skill_stage_begin",
        publication_id=publication_id,
        skill_name="demo",
    )

    def put(offset, data):
        return request(
            workspace,
            "fs_skill_stage_put",
            publication_id=publication_id,
            files=[
                {
                    "path": "SKILL.md",
                    "offset": offset,
                    "data_base64": base64.b64encode(data).decode(),
                }
            ],
        )

    assert "error" not in put(0, SKILL_MD[:10])
    assert put(5, b"x")["status"] == 400  # offset mismatch
    assert "error" not in put(10, SKILL_MD[10:])
    result = request(
        workspace,
        "fs_skill_commit",
        publication_id=publication_id,
        skill_name="demo",
        manifest=manifest({"SKILL.md": SKILL_MD}),
        mode="create",
    )
    assert result["status"] == "published", result


def test_symlink_in_staging_fails_verification(workspace, tmp_path):
    publication_id = stage(workspace, "demo", {"SKILL.md": SKILL_MD})
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    bundle_dir = area(workspace) / "staging" / publication_id / "bundle"
    (bundle_dir / "link.md").symlink_to(outside)
    result = request(
        workspace,
        "fs_skill_commit",
        publication_id=publication_id,
        skill_name="demo",
        manifest=manifest({"SKILL.md": SKILL_MD}),
        mode="create",
    )
    assert result["status"] == 400
    assert not (workspace / ".claude" / "skills" / "demo").exists()


def test_commit_is_idempotent_by_publication_id(workspace):
    bundle = {"SKILL.md": SKILL_MD}
    publication_id, first = publish(workspace, "demo", bundle)
    assert first["status"] == "published"
    again = request(
        workspace,
        "fs_skill_commit",
        publication_id=publication_id,
        skill_name="demo",
        manifest=manifest(bundle),
        mode="update",
        expected_live_digest="0" * 64,
    )
    assert again["status"] == "published" and again["already_committed"] is True
    committed = status(workspace, "demo", publication_id=publication_id)
    assert committed["committed"] is True and committed["staged"] is False


def test_journaled_fallback_when_exchange_is_unsupported(workspace, monkeypatch):
    publish(workspace, "demo", {"SKILL.md": SKILL_MD, "old.md": b"o"})
    real = files._renameat2

    def no_flags(source_parent, source, destination_parent, destination, flags):
        raise files.UnsupportedFilesystemError("flags unsupported")

    monkeypatch.setattr(files, "_renameat2", no_flags)
    _publication_id, result = publish(workspace, "demo", {"SKILL.md": SKILL_MD + b"2"})
    assert result["swap"] == "journaled", result
    published = live_files(workspace, "demo")
    assert set(published) == {"SKILL.md", ".cubicle-bundle.json"}
    assert os.listdir(area(workspace) / "journal") == []
    monkeypatch.setattr(files, "_renameat2", real)
    # First installs fall back to a plain rename.
    monkeypatch.setattr(files, "_renameat2", no_flags)
    _publication_id, fresh = publish(workspace, "fresh", {"SKILL.md": SKILL_MD})
    assert fresh["swap"] == "rename"


class SimulatedCrash(BaseException):
    """The helper process dying mid-publication (SIGKILL)."""


def test_crash_between_journaled_renames_recovers_forward(workspace, monkeypatch):
    publish(workspace, "demo", {"SKILL.md": SKILL_MD, "old.md": b"o"})

    def no_flags(*_args):
        raise files.UnsupportedFilesystemError("flags unsupported")

    real_move = files.SecureWorkspace._skill_move_to_retired

    def move_then_die(self, *args):
        real_move(self, *args)
        raise SimulatedCrash()

    monkeypatch.setattr(files, "_renameat2", no_flags)
    monkeypatch.setattr(files.SecureWorkspace, "_skill_move_to_retired", move_then_die)
    new_bundle = {"SKILL.md": SKILL_MD + b"new\n"}
    with pytest.raises(SimulatedCrash):
        publish(workspace, "demo", new_bundle)
    # The live folder is missing between the two renames.
    assert not (workspace / ".claude" / "skills" / "demo").exists()
    monkeypatch.setattr(files.SecureWorkspace, "_skill_move_to_retired", real_move)
    live = status(workspace, "demo")["live"]  # every skill action recovers
    assert live["managed"] is True and live["modified"] is False
    published = live_files(workspace, "demo")
    published.pop(".cubicle-bundle.json")
    assert published == new_bundle
    assert os.listdir(area(workspace) / "journal") == []
    assert len(os.listdir(area(workspace) / "retired" / "demo")) == 1


def crash_between_journaled_renames(workspace, monkeypatch, new_bundle) -> None:
    """Publish ``demo`` v1, then die after retiring it (live folder absent)."""
    publish(workspace, "demo", {"SKILL.md": SKILL_MD, "old.md": b"o"})
    real_move = files.SecureWorkspace._skill_move_to_retired

    def move_then_die(self, *args):
        real_move(self, *args)
        raise SimulatedCrash()

    monkeypatch.setattr(files, "_renameat2", lambda *_args: _unsupported())
    monkeypatch.setattr(files.SecureWorkspace, "_skill_move_to_retired", move_then_die)
    with pytest.raises(SimulatedCrash):
        publish(workspace, "demo", new_bundle)
    monkeypatch.setattr(files.SecureWorkspace, "_skill_move_to_retired", real_move)
    assert not (workspace / ".claude" / "skills" / "demo").exists()
    assert len(os.listdir(area(workspace) / "journal")) == 1


def _unsupported():
    raise files.UnsupportedFilesystemError("flags unsupported")


def test_discovery_recovers_an_interrupted_swap_before_listing(workspace, monkeypatch):
    new_bundle = {"SKILL.md": SKILL_MD + b"new\n"}
    crash_between_journaled_renames(workspace, monkeypatch, new_bundle)
    listing = request(workspace, "fs_list_skills")
    assert [skill["name"] for skill in listing["skills"]] == ["demo"]
    published = live_files(workspace, "demo")
    published.pop(".cubicle-bundle.json")
    assert published == new_bundle
    assert os.listdir(area(workspace) / "journal") == []


@pytest.mark.parametrize(
    "action, params",
    [
        ("fs_write_revision", {"expect_absent": True, "content": "stub"}),
        ("fs_write", {"content": "stub"}),
    ],
)
def test_skills_writes_recover_before_writing(workspace, monkeypatch, action, params):
    """A stub write can never fill the gap between the journaled renames."""
    new_bundle = {"SKILL.md": SKILL_MD + b"new\n"}
    crash_between_journaled_renames(workspace, monkeypatch, new_bundle)
    result = request(workspace, action, path=".claude/skills/demo/SKILL.md", **params)
    if action == "fs_write_revision":
        assert result["status"] == 409, result  # the recovered SKILL.md exists
        assert live_files(workspace, "demo")["SKILL.md"] == new_bundle["SKILL.md"]
    else:
        # A plain write replaces a file of the RECOVERED folder in place; the
        # confirmed publication itself completed first.
        assert "error" not in result, result
    assert os.listdir(area(workspace) / "journal") == []
    assert os.listdir(area(workspace) / "staging") == []


def test_occupant_in_the_rename_gap_is_retired_not_the_new_version(
    workspace, monkeypatch
):
    new_bundle = {"SKILL.md": SKILL_MD + b"new\n"}
    crash_between_journaled_renames(workspace, monkeypatch, new_bundle)
    # Something outside the helper (an agent's shell) recreates the folder.
    occupant = workspace / ".claude" / "skills" / "demo"
    occupant.mkdir()
    (occupant / "SKILL.md").write_bytes(b"# TODO stub\n")
    live = status(workspace, "demo")["live"]
    assert live["managed"] is True and live["modified"] is False
    published = live_files(workspace, "demo")
    published.pop(".cubicle-bundle.json")
    assert published == new_bundle
    retired = area(workspace) / "retired" / "demo"
    contents = sorted(
        (retired / name / "SKILL.md").read_bytes() for name in os.listdir(retired)
    )
    assert contents == sorted([SKILL_MD, b"# TODO stub\n"])
    assert os.listdir(area(workspace) / "journal") == []
    assert os.listdir(area(workspace) / "staging") == []
    # R3-BND-4: the recovery's retirement is recorded for a read-back.
    [publication_id] = [
        json.loads(live_files(workspace, "demo")[".cubicle-bundle.json"])[
            "publication_id"
        ]
    ]
    state = status(workspace, "demo", publication_id=publication_id)
    assert state["committed"] is True
    occupant = state["occupant_retired_as"]
    assert occupant.endswith(f"-occupant-{publication_id}")
    assert (retired / occupant / "SKILL.md").read_bytes() == b"# TODO stub\n"


def test_only_skills_root_writes_trigger_recovery():
    targets = files._skills_root_targets
    assert targets({"path": ".claude/skills/demo/SKILL.md"}) == [
        (".claude", "skills", "demo", "SKILL.md")
    ]
    assert targets({"path": ".Claude/Skills/demo"})
    assert targets({"old_path": "notes.md", "new_path": ".claude/skills/x/a.md"}) == [
        (".claude", "skills", "x", "a.md")
    ]
    assert not targets({"path": "outputs/report.md"})
    assert not targets({"path": "../escape"})


def test_crash_after_exchange_moves_old_version_to_retired(workspace, monkeypatch):
    publish(workspace, "demo", {"SKILL.md": SKILL_MD})

    def die(self, *args):
        raise SimulatedCrash()

    monkeypatch.setattr(files.SecureWorkspace, "_skill_move_to_retired", die)
    publication_id = None
    with pytest.raises(SimulatedCrash):
        publication_id, _ = publish(workspace, "demo", {"SKILL.md": b"# v2\n"})
    monkeypatch.undo()
    staging = os.listdir(area(workspace) / "staging")
    assert len(staging) == 1  # the old version waits in the staging slot
    live = status(workspace, "demo")["live"]
    assert live["managed"] is True
    assert (workspace / ".claude" / "skills" / "demo" / "SKILL.md").read_bytes() == (
        b"# v2\n"
    )
    assert os.listdir(area(workspace) / "staging") == []
    assert len(os.listdir(area(workspace) / "retired" / "demo")) == 1


def test_crash_before_swap_drops_journal_and_keeps_live(workspace, monkeypatch):
    publish(workspace, "demo", {"SKILL.md": SKILL_MD})

    def die(*_args):
        raise SimulatedCrash()

    monkeypatch.setattr(files, "_renameat2", die)
    with pytest.raises(SimulatedCrash):
        publish(workspace, "demo", {"SKILL.md": b"# v2\n"})
    monkeypatch.undo()
    assert len(os.listdir(area(workspace) / "journal")) == 1
    status(workspace, "demo")
    assert os.listdir(area(workspace) / "journal") == []
    assert (workspace / ".claude" / "skills" / "demo" / "SKILL.md").read_bytes() == (
        SKILL_MD
    )


def test_a_journal_record_the_helper_died_writing_is_removed(workspace, monkeypatch):
    """Crash sweep finding: a helper that died while writing its journal
    record left the temporary file in ``journal/`` forever, because recovery
    only reads ``<publication_id>.json`` records."""
    publish(workspace, "demo", {"SKILL.md": SKILL_MD})
    real_replace = os.replace

    def die_on_journal_record(source, destination, **kwargs):
        if re.fullmatch(r"[0-9a-f]{32}\.json", str(destination)):
            raise SimulatedCrash()
        return real_replace(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", die_on_journal_record)
    with pytest.raises(SimulatedCrash):
        publish(workspace, "demo", {"SKILL.md": b"# v2\n"})
    monkeypatch.undo()
    leftovers = os.listdir(area(workspace) / "journal")
    assert len(leftovers) == 1 and leftovers[0].endswith(".tmp"), leftovers
    status(workspace, "demo")
    assert os.listdir(area(workspace) / "journal") == []
    assert live_files(workspace, "demo")["SKILL.md"] == SKILL_MD


def test_retention_keeps_newest_two_and_anything_recent(workspace, monkeypatch):
    clock = [1_000_000_000.0]
    monkeypatch.setattr(files, "_now", lambda: clock[0])
    for version in range(4):
        clock[0] += 10
        publish(workspace, "demo", {"SKILL.md": SKILL_MD + str(version).encode()})
    retired = area(workspace) / "retired" / "demo"
    assert len(os.listdir(retired)) == 3  # all within the grace period
    clock[0] += files.SKILL_RETIRED_GRACE_SECONDS + 60
    publish(workspace, "demo", {"SKILL.md": SKILL_MD + b"latest"})
    remaining = sorted(os.listdir(retired), reverse=True)
    assert len(remaining) == files.SKILL_RETIRED_KEEP
    assert int(remaining[0][:10]) == int(clock[0])


def test_stale_staging_is_pruned_and_abort_removes_staging(workspace, monkeypatch):
    clock = [1_000_000_000.0]
    monkeypatch.setattr(files, "_now", lambda: clock[0])
    abandoned = stage(workspace, "demo", {"SKILL.md": SKILL_MD})
    clock[0] += files.SKILL_STAGING_STALE_SECONDS + 1
    kept = stage(workspace, "demo", {"SKILL.md": SKILL_MD})
    assert os.listdir(area(workspace) / "staging") == [kept]
    assert abandoned != kept
    aborted = request(workspace, "fs_skill_abort", publication_id=kept)
    assert aborted["aborted"] is True
    assert os.listdir(area(workspace) / "staging") == []


def test_private_area_is_invisible_to_public_files_and_discovery(workspace):
    publish(workspace, "demo", {"SKILL.md": SKILL_MD})
    stage(workspace, "demo", {"SKILL.md": b"pending"})
    for action in ("fs_read", "fs_write", "fs_delete", "fs_tree"):
        result = request(
            workspace,
            action,
            path=".claude/.cubicle-skill-bundles/journal",
            subfolder=".claude/.cubicle-skill-bundles",
            content="x",
        )
        assert result["status"] == 400
    listing = request(workspace, "fs_list_skills")
    assert [skill["name"] for skill in listing["skills"]] == ["demo"]
    demo = listing["skills"][0]
    assert all(item["name"] != ".cubicle-bundle.json" for item in demo["files"])
    assert (
        demo["bundle"]["bundle_sha256"]
        == status(workspace, "demo")["live"]["bundle_sha256"]
    )
    assert demo["bundle"]["source_kind"] == "catalog_github"
    assert tuple(demo["bundle"]) == files.SKILL_BUNDLE_IDENTITY_KEYS
    assert demo["bundle"]["source_revision"] == "git:x"


def test_unmanaged_discovery_entry_has_no_bundle(workspace):
    legacy = workspace / ".claude" / "skills" / "legacy"
    legacy.mkdir()
    (legacy / "SKILL.md").write_bytes(SKILL_MD)
    listing = request(workspace, "fs_list_skills")
    assert listing["skills"][0]["bundle"] is None


def test_live_params_symlink_blocks_publication(workspace, tmp_path):
    publish(workspace, "demo", {"SKILL.md": SKILL_MD})
    outside = tmp_path / "params.json"
    outside.write_text("{}")
    (workspace / ".claude" / "skills" / "demo" / "params.json").symlink_to(outside)
    _publication_id, result = publish(workspace, "demo", {"SKILL.md": b"# v2\n"})
    assert result["status"] == 409
    assert result["code"] == "skill_params_unsupported"


def staged_ids(root) -> list[str]:
    staging = area(root) / "staging"
    return os.listdir(staging) if staging.exists() else []


def test_definitive_commit_refusals_drop_their_staging(workspace, tmp_path):
    """BND-2: a refused commit never leaves its staged copy behind."""
    publish(workspace, "demo", {"SKILL.md": SKILL_MD})
    # A stale CAS expectation.
    _publication_id, result = publish(
        workspace, "demo", {"SKILL.md": b"# v2\n"}, expected="0" * 64
    )
    assert result["code"] == "skill_bundle_conflict", result
    assert staged_ids(workspace) == []
    # Create-only over an existing folder.
    _publication_id, result = publish(
        workspace, "demo", {"SKILL.md": b"# v2\n"}, mode="create"
    )
    assert result["code"] == "skill_exists", result
    assert staged_ids(workspace) == []
    # A live params.json that cannot be carried over.
    outside = tmp_path / "params.json"
    outside.write_text("{}")
    (workspace / ".claude" / "skills" / "demo" / "params.json").symlink_to(outside)
    _publication_id, result = publish(workspace, "demo", {"SKILL.md": b"# v2\n"})
    assert result["code"] == "skill_params_unsupported", result
    assert staged_ids(workspace) == []
    # A staged set that does not match its manifest.
    publication_id = stage(workspace, "fresh", {"SKILL.md": SKILL_MD})
    result = request(
        workspace,
        "fs_skill_commit",
        publication_id=publication_id,
        skill_name="fresh",
        manifest=manifest({"SKILL.md": SKILL_MD + b"other"}),
        mode="update",
        expected_live_digest="none",
    )
    assert result["status"] == 400, result
    assert staged_ids(workspace) == []
    assert os.listdir(area(workspace) / "journal") == []


def test_status_prunes_abandoned_staging(workspace, monkeypatch):
    clock = [1_000_000_000.0]
    monkeypatch.setattr(files, "_now", lambda: clock[0])
    stage(workspace, "demo", {"SKILL.md": SKILL_MD})
    clock[0] += files.SKILL_STAGING_STALE_SECONDS + 1
    status(workspace, "demo")
    assert staged_ids(workspace) == []


@pytest.mark.parametrize("path", ["Params.json", ".Cubicle-Bundle.json"])
def test_reserved_names_are_refused_in_any_letter_case(workspace, path):
    """BND-5: on a case-insensitive workspace these ARE the reserved files."""
    publication_id = uuid.uuid4().hex
    request(
        workspace,
        "fs_skill_stage_begin",
        publication_id=publication_id,
        skill_name="demo",
    )
    put = request(
        workspace,
        "fs_skill_stage_put",
        publication_id=publication_id,
        files=[{"path": path, "offset": 0, "data_base64": ""}],
    )
    assert put["status"] == 400, put
    assert "reserved" in put["error"]


def test_manifest_with_case_colliding_directories_is_refused(workspace):
    bundle = {"SKILL.md": SKILL_MD, "Refs/a.md": b"a", "refs/b.md": b"b"}
    publication_id = stage(workspace, "demo", {"SKILL.md": SKILL_MD})
    result = request(
        workspace,
        "fs_skill_commit",
        publication_id=publication_id,
        skill_name="demo",
        manifest=manifest(bundle),
        mode="update",
        expected_live_digest="none",
    )
    assert result["status"] == 400, result
    assert "letter case" in result["error"]
    assert staged_ids(workspace) == []


def test_folder_too_large_to_verify_can_be_linked_and_replaced(workspace):
    """BND-7: an oversized folder is reported, not a permanent 400."""
    big = workspace / ".claude" / "skills" / "demo"
    (big / "node_modules").mkdir(parents=True)
    (big / "SKILL.md").write_bytes(SKILL_MD)
    for index in range(files.SKILL_BUNDLE_MAX_FILES + 5):
        (big / "node_modules" / f"f{index}.js").write_bytes(b"x")
    live = status(workspace, "demo")["live"]
    assert live["exists"] is True and live["unverified"] is True
    assert live["has_skill_md"] is True
    assert "file-count" in live["unverified_reason"]
    # ensure links it without republishing.
    _publication_id, linked = publish(
        workspace, "demo", {"SKILL.md": b"# v2\n"}, mode="ensure"
    )
    assert linked["status"] == "exists", linked
    # A top-level change is still caught by the compare-and-swap.
    stale = live["digest"]
    (big / "extra.md").write_bytes(b"new")
    _publication_id, refused = publish(
        workspace, "demo", {"SKILL.md": b"# v2\n"}, expected=stale
    )
    assert refused["code"] == "skill_bundle_conflict", refused
    # update replaces it whole; the old folder is retired, not deleted.
    _publication_id, replaced = publish(workspace, "demo", {"SKILL.md": b"# v2\n"})
    assert replaced["status"] == "published", replaced
    assert replaced["previous"]["unverified"] is True
    assert set(live_files(workspace, "demo")) == {"SKILL.md", ".cubicle-bundle.json"}
    retired = area(workspace) / "retired" / "demo" / replaced["previous"]["retired_as"]
    assert len(os.listdir(retired / "node_modules")) == files.SKILL_BUNDLE_MAX_FILES + 5


def test_case_variants_of_reserved_root_names_count_as_local_changes(workspace):
    """BR-3: on a case-sensitive workspace ``Params.json`` is a user file."""
    publish(workspace, "demo", {"SKILL.md": SKILL_MD})
    root = workspace / ".claude" / "skills" / "demo"
    (root / "params.json").write_text("{}")
    (root / "Params.json").write_text('{"mine": 1}')
    (root / ".Cubicle-Bundle.json").write_text("notes")
    live = status(workspace, "demo")["live"]
    assert live["modified"] is True
    assert live["unsupported_count"] == 2
    _publication_id, result = publish(workspace, "demo", {"SKILL.md": b"# v2\n"})
    assert result["status"] == "published", result
    assert result["previous"]["state"] == "modified"


def test_a_case_variant_that_is_the_reserved_file_is_skipped(workspace):
    """The case-insensitive-workspace case: the same inode is skipped."""
    publish(workspace, "demo", {"SKILL.md": SKILL_MD})
    root = workspace / ".claude" / "skills" / "demo"
    (root / "params.json").write_text("{}")
    os.link(root / "params.json", root / "Params.json")
    live = status(workspace, "demo")["live"]
    assert live["modified"] is False
    assert live["unsupported_count"] == 0


def _count_digests(monkeypatch, fail_on: int | None = None) -> list[int]:
    """Count ``_digest_fd`` calls; optionally fail call ``fail_on`` once
    with the transient "file changed" error."""
    calls: list[int] = []
    real = files.SecureWorkspace._digest_fd

    def counted(self, descriptor, limit, initial=None):
        calls.append(1)
        if fail_on is not None and len(calls) == fail_on:
            raise files.FileChangedError()
        return real(self, descriptor, limit, initial)

    monkeypatch.setattr(files.SecureWorkspace, "_digest_fd", counted)
    return calls


def test_a_file_changing_during_status_is_a_retryable_error(workspace, monkeypatch):
    """BR-1/RR-3: a transient read race is never reported as unverified."""
    publish(workspace, "demo", {"SKILL.md": SKILL_MD, "notes.md": b"n"})
    _count_digests(monkeypatch, fail_on=1)
    result = request(workspace, "fs_skill_status", skill_name="demo")
    assert result["status"] == 400, result
    assert "changed while being read" in result["error"]
    monkeypatch.undo()
    live = status(workspace, "demo")["live"]
    assert live["modified"] is False and "unverified" not in live


def test_a_file_changing_during_commit_fails_without_swapping(workspace, monkeypatch):
    publish(workspace, "demo", {"SKILL.md": SKILL_MD})
    expected = status(workspace, "demo")["live"]["digest"]
    publication_id = stage(workspace, "demo", {"SKILL.md": b"# v2\n"})
    _count_digests(monkeypatch, fail_on=1)
    result = request(
        workspace,
        "fs_skill_commit",
        publication_id=publication_id,
        skill_name="demo",
        manifest=manifest({"SKILL.md": b"# v2\n"}),
        mode="update",
        expected_live_digest=expected,
    )
    assert result["status"] == 400, result
    monkeypatch.undo()
    assert (workspace / ".claude" / "skills" / "demo" / "SKILL.md").read_bytes() == (
        SKILL_MD
    )


def test_too_many_files_is_detected_before_hashing(workspace, monkeypatch):
    """BR-4/RR-4: the size check runs by stat, before any file is hashed."""
    big = workspace / ".claude" / "skills" / "demo"
    (big / "assets").mkdir(parents=True)
    (big / "SKILL.md").write_bytes(SKILL_MD)
    for index in range(files.SKILL_BUNDLE_MAX_FILES + 1):
        (big / "assets" / f"a{index}.txt").write_bytes(b"x")
    calls = _count_digests(monkeypatch)
    live = status(workspace, "demo")["live"]
    assert live["unverified"] is True
    assert live["unverified_reason_code"] == "too_many_files"
    assert live["has_skill_md"] is True
    assert calls == []


def test_too_many_bytes_is_unverified_and_can_be_linked(workspace, monkeypatch):
    monkeypatch.setattr(files, "SKILL_LIVE_HASH_MAX_BYTES", 3 * 1024)
    big = workspace / ".claude" / "skills" / "demo"
    big.mkdir()
    (big / "SKILL.md").write_bytes(SKILL_MD)
    for index in range(4):
        (big / f"asset{index}.bin").write_bytes(b"x" * 1024)
    calls = _count_digests(monkeypatch)
    live = status(workspace, "demo")["live"]
    assert live["unverified"] is True
    assert live["unverified_reason_code"] == "too_many_bytes"
    assert calls == []
    _publication_id, linked = publish(
        workspace, "demo", {"SKILL.md": b"# v2\n"}, mode="ensure"
    )
    assert linked["status"] == "exists", linked


def test_a_slow_scan_is_unverified_instead_of_a_timeout(workspace, monkeypatch):
    publish(workspace, "demo", {"SKILL.md": SKILL_MD})
    # The scan's own deadline has already passed; the helper's has not.
    monkeypatch.setattr(
        files, "SKILL_LIVE_SCAN_MARGIN_SECONDS", files.DEADLINE_SECONDS + 1
    )
    live = status(workspace, "demo")["live"]
    assert live["unverified"] is True
    assert live["unverified_reason_code"] == "scan_time"


def test_an_entry_budget_breach_is_unverified(workspace, monkeypatch):
    big = workspace / ".claude" / "skills" / "demo"
    big.mkdir()
    (big / "SKILL.md").write_bytes(SKILL_MD)
    for index in range(80):
        (big / f"dir{index}").mkdir()
    monkeypatch.setattr(files, "SKILL_BUNDLE_ENTRY_LIMIT", 60)
    live = status(workspace, "demo")["live"]
    assert live["unverified"] is True
    assert live["unverified_reason_code"] == "entry_budget"


def test_one_huge_skill_folder_does_not_break_discovery(workspace):
    """RR-BND-3: the folder degrades; every other skill is still listed."""
    publish(workspace, "good", {"SKILL.md": SKILL_MD, "notes.md": b"n"})
    big = workspace / ".claude" / "skills" / "big"
    (big / "node_modules").mkdir(parents=True)
    (big / "SKILL.md").write_bytes(b"---\nname: big\ndescription: Big.\n---\n")
    for index in range(files.MAX_ENTRIES + 100):
        (big / "node_modules" / f"m{index}.js").write_bytes(b"x")
    listing = request(workspace, "fs_list_skills")
    assert "error" not in listing, listing
    by_name = {entry["name"]: entry for entry in listing["skills"]}
    assert set(by_name) == {"big", "good"}
    assert by_name["big"]["listing_truncated"] is True
    assert "entry limit" in by_name["big"]["listing_error"]
    assert by_name["big"]["has_skill_md"] is True
    assert by_name["big"]["skill_md_head"].startswith("---\nname: big")
    assert by_name["big"]["files"] == []
    good = by_name["good"]
    assert "listing_truncated" not in good
    assert {item["name"] for item in good["files"]} == {"SKILL.md", "notes.md"}
    assert good["bundle"]["source_kind"] == "catalog_github"


def test_links_and_special_files_in_a_skill_never_fail_discovery(workspace):
    """R3-DISC-LINK: an ``npm install`` (``.bin`` links) or a virtualenv
    inside one skill is skipped and counted, never fatal for the office."""
    publish(workspace, "good", {"SKILL.md": SKILL_MD, "notes.md": b"n"})
    skills = workspace / ".claude" / "skills"
    tool = skills / "tool"
    (tool / "node_modules" / "typescript" / "bin").mkdir(parents=True)
    (tool / "node_modules" / ".bin").mkdir()
    (tool / "SKILL.md").write_bytes(b"---\nname: tool\ndescription: T.\n---\n")
    (tool / "package.json").write_bytes(b"{}")
    (tool / "node_modules" / "typescript" / "bin" / "tsc").write_bytes(b"#!")
    (tool / "node_modules" / ".bin" / "tsc").symlink_to("../typescript/bin/tsc")
    (tool / "a.md").write_bytes(b"a")
    os.link(tool / "a.md", tool / "a-hardlink.md")  # both skipped
    os.mkfifo(tool / "pipe")
    (tool / "bad\x01name.md").write_bytes(b"x")
    deep = tool
    for _ in range(files.MAX_DEPTH):
        deep = deep / "d"
    deep.mkdir(parents=True)
    (deep / "deep.md").write_bytes(b"deep")
    venv = skills / "venv"
    (venv / ".venv" / "bin").mkdir(parents=True)
    (venv / "SKILL.md").write_bytes(b"---\nname: venv\ndescription: V.\n---\n")
    (venv / ".venv" / "bin" / "python").symlink_to("/usr/local/bin/python3")
    listing = request(workspace, "fs_list_skills")
    assert "error" not in listing, listing
    by_name = {entry["name"]: entry for entry in listing["skills"]}
    assert set(by_name) == {"good", "tool", "venv"}
    good = by_name["good"]
    assert {item["name"] for item in good["files"]} == {"SKILL.md", "notes.md"}
    assert "skipped_entries" not in good
    tool_entry = by_name["tool"]
    assert tool_entry["has_skill_md"] is True
    assert tool_entry["skill_md_head"].startswith("---\nname: tool")
    assert "listing_truncated" not in tool_entry
    tool_files = {
        item["name"] for item in tool_entry["files"] if item["type"] == "file"
    }
    assert tool_files == {
        "SKILL.md",
        "package.json",
        "node_modules/typescript/bin/tsc",
    }
    # .bin/tsc, the two hard links, the FIFO, the unaddressable name and
    # the folder beyond the depth limit.
    assert tool_entry["skipped_entries"] == 6
    assert by_name["venv"]["has_skill_md"] is True
    assert by_name["venv"]["skipped_entries"] == 1
    # The status view of the same folder agrees: nothing was followed.
    assert status(workspace, "tool")["live"]["exists"] is True


def _vanish_after_scan(monkeypatch, marker: str, remove) -> None:
    """Delete an entry for real right after the listing that contains
    ``marker`` was read, as a writer outside the helper would."""
    import shutil

    real_scandir = os.scandir
    fired = []

    class _Listed:
        def __init__(self, entries):
            self.entries = entries

        def __enter__(self):
            return iter(self.entries)

        def __exit__(self, *exc):
            return False

        def __iter__(self):
            return iter(self.entries)

    def scandir(target=".", *args, **kwargs):
        with real_scandir(target, *args, **kwargs) as listing:
            entries = list(listing)
        if not fired and any(entry.name == marker for entry in entries):
            fired.append(marker)
            path = remove()
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        return _Listed(entries)

    monkeypatch.setattr(files.os, "scandir", scandir)


def _two_skills(workspace):
    publish(workspace, "alpha", {"SKILL.md": SKILL_MD, "notes.md": b"a"})
    publish(workspace, "beta", {"SKILL.md": SKILL_MD})
    return workspace / ".claude" / "skills"


def test_a_sibling_removed_mid_scan_leaves_every_live_skill_listed(
    workspace, monkeypatch
):
    """R3-DISC-VANISH: never an authoritative empty listing."""
    skills = _two_skills(workspace)
    (skills / "draft-tmp").mkdir()
    _vanish_after_scan(monkeypatch, "draft-tmp", lambda: skills / "draft-tmp")
    listing = request(workspace, "fs_list_skills")
    assert [entry["name"] for entry in listing["skills"]] == ["alpha", "beta"]


def test_a_file_removed_mid_scan_keeps_its_skill_listed(workspace, monkeypatch):
    skills = _two_skills(workspace)
    (skills / "alpha" / "4913").write_bytes(b"")  # an editor's probe file
    _vanish_after_scan(monkeypatch, "4913", lambda: skills / "alpha" / "4913")
    listing = request(workspace, "fs_list_skills")
    by_name = {entry["name"]: entry for entry in listing["skills"]}
    assert set(by_name) == {"alpha", "beta"}
    alpha = by_name["alpha"]
    assert {item["name"] for item in alpha["files"]} == {"SKILL.md", "notes.md"}
    assert alpha["has_skill_md"] is True
    assert "skipped_entries" not in alpha  # vanished, not unsupported


def test_a_skill_folder_removed_mid_scan_is_simply_absent(workspace, monkeypatch):
    skills = _two_skills(workspace)
    _vanish_after_scan(monkeypatch, "beta", lambda: skills / "beta")
    listing = request(workspace, "fs_list_skills")
    assert [entry["name"] for entry in listing["skills"]] == ["alpha"]


def test_a_symlinked_skill_folder_is_skipped_not_fatal(workspace, tmp_path):
    publish(workspace, "good", {"SKILL.md": SKILL_MD})
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "SKILL.md").write_bytes(SKILL_MD)
    (workspace / ".claude" / "skills" / "linked").symlink_to(outside)
    listing = request(workspace, "fs_list_skills")
    assert [entry["name"] for entry in listing["skills"]] == ["good"]


@pytest.mark.parametrize("occupant_kind", ["dir", "file"])
def test_an_occupant_in_the_journaled_gap_is_retired_and_the_commit_lands(
    workspace, monkeypatch, occupant_kind
):
    """RR-2: the commit completes like recovery instead of failing."""
    publish(workspace, "demo", {"SKILL.md": SKILL_MD})
    real_move = files.SecureWorkspace._skill_move_to_retired
    moves = []

    def move_then_occupy(self, parent, name, skill, retired_name):
        real_move(self, parent, name, skill, retired_name)
        moves.append(retired_name)
        if len(moves) == 1:
            occupant = workspace / ".claude" / "skills" / "demo"
            if occupant_kind == "dir":
                occupant.mkdir()
                (occupant / "SKILL.md").write_bytes(b"# TODO stub\n")
            else:
                occupant.write_bytes(b"not a folder\n")

    monkeypatch.setattr(files, "_renameat2", lambda *_args: _unsupported())
    monkeypatch.setattr(
        files.SecureWorkspace, "_skill_move_to_retired", move_then_occupy
    )
    new_bundle = {"SKILL.md": SKILL_MD + b"new\n"}
    _publication_id, result = publish(workspace, "demo", new_bundle)
    assert result["status"] == "published", result
    assert result["swap"] == "journaled"
    published = live_files(workspace, "demo")
    published.pop(".cubicle-bundle.json")
    assert published == new_bundle
    assert len(moves) == 2  # the old version, then the occupant
    retired = area(workspace) / "retired" / "demo"
    assert sorted(os.listdir(retired)) == sorted(moves)
    assert os.listdir(area(workspace) / "journal") == []
    assert os.listdir(area(workspace) / "staging") == []
    # R3-1: the occupant is reported, and the replaced state is "modified".
    previous = result["previous"]
    assert previous["occupant_retired_as"] == moves[1]
    assert previous["occupant_retired_as"].endswith(f"-occupant-{_publication_id}")
    assert previous["retired_as"] == moves[0]
    assert previous["state"] == "modified"
    assert status(workspace, "demo")["occupants"] == [moves[1]]


def test_a_failure_after_the_journal_is_uncertain_not_a_refusal(workspace, monkeypatch):
    publish(workspace, "demo", {"SKILL.md": SKILL_MD})

    def fail(self, *_args):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(files, "_renameat2", lambda *_args: _unsupported())
    monkeypatch.setattr(files.SecureWorkspace, "_skill_land_journaled", fail)
    publication_id, result = publish(workspace, "demo", {"SKILL.md": b"# v2\n"})
    assert result["status"] == 500, result
    assert result["code"] == "skill_commit_uncertain"
    monkeypatch.undo()
    # The journal survived; the next action completes the publication and
    # an abort reports it as committed instead of dropping it.
    aborted = request(
        workspace,
        "fs_skill_abort",
        publication_id=publication_id,
        skill_name="demo",
    )
    assert aborted == {
        "publication_id": publication_id,
        "aborted": False,
        "committed": True,
        "occupant_retired_as": None,
    }
    assert (workspace / ".claude" / "skills" / "demo" / "SKILL.md").read_bytes() == (
        b"# v2\n"
    )
    assert os.listdir(area(workspace) / "journal") == []
    assert os.listdir(area(workspace) / "staging") == []


def test_an_error_after_the_exchange_is_uncertain_and_reads_back_committed(
    workspace, monkeypatch
):
    publish(workspace, "demo", {"SKILL.md": SKILL_MD})

    def fail(self, *_args):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(files.SecureWorkspace, "_skill_move_to_retired", fail)
    publication_id, result = publish(workspace, "demo", {"SKILL.md": b"# v2\n"})
    assert result["status"] == 500, result
    assert result["code"] == "skill_commit_uncertain"
    monkeypatch.undo()
    state = status(workspace, "demo", publication_id=publication_id)
    assert state["committed"] is True
    assert (workspace / ".claude" / "skills" / "demo" / "SKILL.md").read_bytes() == (
        b"# v2\n"
    )
    retired = area(workspace) / "retired" / "demo"
    [previous] = os.listdir(retired)
    assert (retired / previous / "SKILL.md").read_bytes() == SKILL_MD


def test_abort_keeps_a_previous_version_its_journal_still_owns(workspace, monkeypatch):
    """After an exchange the staging slot holds the OLD version; an abort
    that finds the publication live never deletes it — recovery retires it."""
    publish(workspace, "demo", {"SKILL.md": SKILL_MD})

    def die(self, *args):
        raise SimulatedCrash()

    monkeypatch.setattr(files.SecureWorkspace, "_skill_move_to_retired", die)
    with pytest.raises(SimulatedCrash):
        publish(workspace, "demo", {"SKILL.md": b"# v2\n"})
    monkeypatch.undo()
    [publication_id] = os.listdir(area(workspace) / "staging")
    monkeypatch.setattr(files, "SKILL_JOURNAL_RECOVERY_LIMIT", 0)
    aborted = request(
        workspace, "fs_skill_abort", publication_id=publication_id, skill_name="demo"
    )
    assert aborted["committed"] is True and aborted["aborted"] is False
    assert os.listdir(area(workspace) / "staging") == [publication_id]
    monkeypatch.undo()
    status(workspace, "demo")
    assert os.listdir(area(workspace) / "staging") == []
    retired = area(workspace) / "retired" / "demo"
    [previous] = os.listdir(retired)
    assert (retired / previous / "SKILL.md").read_bytes() == SKILL_MD


def test_an_occupied_path_on_the_plain_rename_fallback_is_a_conflict(
    workspace, monkeypatch
):
    """Without RENAME_NOREPLACE a folder appearing after the check makes the
    rename refuse: nothing moved, so it is a definitive, tagged conflict."""
    real_rename = os.rename

    def occupy_then_rename(src, dst, *args, **kwargs):
        if (src, dst) == ("bundle", "fresh"):
            occupant = workspace / ".claude" / "skills" / "fresh"
            occupant.mkdir()
            (occupant / "SKILL.md").write_bytes(b"# mine\n")
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(files, "_renameat2", lambda *_args: _unsupported())
    monkeypatch.setattr(files.os, "rename", occupy_then_rename)
    _publication_id, result = publish(workspace, "fresh", {"SKILL.md": SKILL_MD})
    assert result["status"] == 409, result
    assert result["code"] == "skill_bundle_conflict"
    monkeypatch.undo()
    assert live_files(workspace, "fresh") == {"SKILL.md": b"# mine\n"}
    assert staged_ids(workspace) == []
    assert os.listdir(area(workspace) / "journal") == []


@pytest.mark.parametrize(
    "params",
    [
        # A staged set that does not match its manifest.
        {
            "manifest": manifest({"SKILL.md": SKILL_MD + b"other"}),
            "mode": "update",
            "expected_live_digest": "none",
        },
        # An update without its compare-and-swap expectation.
        {"manifest": manifest({"SKILL.md": SKILL_MD}), "mode": "update"},
        # An invalid mode.
        {"manifest": manifest({"SKILL.md": SKILL_MD}), "mode": "replace"},
    ],
)
def test_pre_journal_refusals_are_tagged(workspace, params):
    publication_id = stage(workspace, "fresh", {"SKILL.md": SKILL_MD})
    result = request(
        workspace,
        "fs_skill_commit",
        publication_id=publication_id,
        skill_name="fresh",
        **params,
    )
    assert result["status"] == 400, result
    assert result["code"] == "skill_publication_refused"
    assert not (workspace / ".claude" / "skills" / "fresh").exists()
    assert staged_ids(workspace) == []


# -- the live manifest is never the only evidence of a swap (R3-BND-1) -------


def test_a_manifest_the_reader_could_not_read_is_refused_before_the_swap(
    workspace, monkeypatch
):
    """Writer and reader share one limit: a manifest the helper could not
    read back is refused before anything moves, never written."""
    publish(workspace, "demo", {"SKILL.md": SKILL_MD})
    monkeypatch.setattr(files, "SKILL_BUNDLE_MANIFEST_MAX_BYTES", 600)
    bundle = {"SKILL.md": b"# v2\n", "b" * 200 + ".md": b"b", "c" * 200 + ".md": b"c"}
    _publication_id, result = publish(workspace, "demo", bundle)
    assert result["status"] == 400, result
    assert result["code"] == "skill_publication_refused"
    assert "manifest" in result["error"]
    live = live_files(workspace, "demo")
    assert live["SKILL.md"] == SKILL_MD
    assert staged_ids(workspace) == []
    assert os.listdir(area(workspace) / "journal") == []


def test_a_broken_live_manifest_never_costs_the_replaced_version(
    workspace, monkeypatch
):
    """The deadline hits between RENAME_EXCHANGE and retiring the old
    version, and the new live manifest then cannot be read. Recovery must
    still retire the old version (with the user's edit), and a re-commit and
    an abort of the same publication must never delete it."""
    publish(workspace, "demo", {"SKILL.md": SKILL_MD})
    live = workspace / ".claude" / "skills" / "demo"
    (live / "notes.md").write_bytes(b"USER EDIT\n")
    expected = status(workspace, "demo")["live"]["digest"]
    real_move = files.SecureWorkspace._skill_move_to_retired

    def interrupt(self, parent, name, skill, retired_name):
        if name == "bundle":
            raise TimeoutError("Files operation interrupted")
        return real_move(self, parent, name, skill, retired_name)

    monkeypatch.setattr(files.SecureWorkspace, "_skill_move_to_retired", interrupt)
    new_bundle = {"SKILL.md": b"# v2\n"}
    publication_id = stage(workspace, "demo", new_bundle)
    commit = {
        "publication_id": publication_id,
        "skill_name": "demo",
        "manifest": manifest(new_bundle),
        "mode": "update",
        "expected_live_digest": expected,
    }
    assert request(workspace, "fs_skill_commit", **commit)["status"] == 408
    monkeypatch.undo()
    (live / ".cubicle-bundle.json").write_bytes(b"{not json")
    status(workspace, "demo", publication_id=publication_id)
    request(workspace, "fs_skill_commit", **commit)  # the backend's re-commit
    request(workspace, "fs_skill_abort", publication_id=publication_id)
    copies = sorted(area(workspace).rglob("notes.md"))
    assert [copy.read_bytes() for copy in copies] == [b"USER EDIT\n"]
    assert copies[0].relative_to(area(workspace)).parts[:2] == ("retired", "demo")
    assert (live / "SKILL.md").read_bytes() == b"# v2\n"


def test_a_staging_drop_retires_a_displaced_version_instead_of_deleting_it(
    workspace, monkeypatch
):
    """Even with its journal lost, a committed staging slot that holds
    another version is retired by the prune, not deleted."""
    clock = [1_000_000_000.0]
    monkeypatch.setattr(files, "_now", lambda: clock[0])
    publish(workspace, "demo", {"SKILL.md": SKILL_MD, "notes.md": b"EDIT\n"})

    def die(self, *args):
        raise SimulatedCrash()

    monkeypatch.setattr(files.SecureWorkspace, "_skill_move_to_retired", die)
    with pytest.raises(SimulatedCrash):
        publish(workspace, "demo", {"SKILL.md": b"# v2\n"})
    monkeypatch.undo()
    monkeypatch.setattr(files, "_now", lambda: clock[0])
    for journal in (area(workspace) / "journal").iterdir():
        journal.unlink()  # the journal is gone: only the slot remains
    clock[0] += files.SKILL_STAGING_STALE_SECONDS + 1
    status(workspace, "demo")  # runs the stale-staging prune
    assert staged_ids(workspace) == []
    copies = sorted(area(workspace).rglob("notes.md"))
    assert [copy.read_bytes() for copy in copies] == [b"EDIT\n"]


def test_a_retired_occupant_outlives_later_version_retirements(workspace, monkeypatch):
    """R3-1: versions retired later never push an occupant out early."""
    clock = [1_000_000_000.0]
    monkeypatch.setattr(files, "_now", lambda: clock[0])
    publish(workspace, "demo", {"SKILL.md": SKILL_MD})
    real_move = files.SecureWorkspace._skill_move_to_retired
    moves = []

    def move_then_occupy(self, parent, name, skill, retired_name):
        real_move(self, parent, name, skill, retired_name)
        moves.append(retired_name)
        if len(moves) == 1:
            occupant = workspace / ".claude" / "skills" / "demo"
            occupant.mkdir()
            (occupant / "notes.md").write_bytes(b"AGENT WORK\n")

    with monkeypatch.context() as patch:
        patch.setattr(files, "_renameat2", lambda *_args: _unsupported())
        patch.setattr(files.SecureWorkspace, "_skill_move_to_retired", move_then_occupy)
        _pid, result = publish(workspace, "demo", {"SKILL.md": b"# v2\n"})
    occupant = result["previous"]["occupant_retired_as"]
    for version in range(3):
        clock[0] += files.SKILL_RETIRED_GRACE_SECONDS + 60
        publish(workspace, "demo", {"SKILL.md": b"# v%d\n" % (version + 3)})
    retired = area(workspace) / "retired" / "demo"
    assert (retired / occupant / "notes.md").read_bytes() == b"AGENT WORK\n"
    versions = [name for name in os.listdir(retired) if "occupant" not in name]
    assert len(versions) == files.SKILL_RETIRED_KEEP


def _write_non_utf8_name(directory, name: bytes, data: bytes = b"x") -> None:
    with open(os.path.join(os.fsencode(directory), name), "wb") as handle:
        handle.write(data)


def test_a_non_utf8_name_is_skipped_not_fatal(workspace):
    """B3-bugs-01: a name whose bytes are not UTF-8 (surrogate-escaped by
    os.scandir, e.g. from an unzipped cp1252 archive) is unsupported and
    counted. It never fails discovery, status or commit for the office."""
    publish(workspace, "good", {"SKILL.md": SKILL_MD})
    skills = workspace / ".claude" / "skills"
    bad = skills / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_bytes(b"---\nname: bad\ndescription: B.\n---\n")
    _write_non_utf8_name(bad, b"r\xe9sum\xe9.txt")
    os.mkdir(os.path.join(os.fsencode(skills), b"x\xff"))

    listing = request(workspace, "fs_list_skills")
    assert "error" not in listing, listing
    by_name = {entry["name"]: entry for entry in listing["skills"]}
    assert set(by_name) == {"good", "bad"}
    assert by_name["bad"]["has_skill_md"] is True
    assert by_name["bad"]["skipped_entries"] == 1

    live = status(workspace, "bad")["live"]
    assert live["exists"] is True and live["unsupported_count"] == 1
    stale = "0" * 64
    _publication_id, refused = publish(
        workspace, "bad", {"SKILL.md": SKILL_MD}, expected=stale
    )
    assert refused["status"] == 409, refused
    assert refused["code"] == "skill_bundle_conflict"
    assert refused["current_digest"] == live["digest"]

    _publication_id, result = publish(
        workspace, "bad", {"SKILL.md": SKILL_MD}, expected=live["digest"]
    )
    assert result["status"] == "published", result
    assert set(live_files(workspace, "bad")) == {"SKILL.md", ".cubicle-bundle.json"}


# -- skill delete (fs_skill_retire) and retention of deleted skills -----------


def test_retire_moves_a_folder_files_cannot_delete_whole(workspace, tmp_path):
    """A folder with protected names, a link and more entries than one Files
    call may visit is refused by ``fs_delete`` but retired in one rename."""
    folder = workspace / ".claude" / "skills" / "demo"
    (folder / ".git").mkdir(parents=True)
    (folder / "SKILL.md").write_bytes(SKILL_MD)
    (folder / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
    (folder / ".env").write_bytes(b"TOKEN=1\n")
    (tmp_path / "outside").write_bytes(b"x")
    os.symlink(tmp_path / "outside", folder / "link")
    (folder / "node_modules").mkdir()
    for index in range(files.MAX_ENTRIES + 10):
        (folder / "node_modules" / f"m{index}.js").write_bytes(b"x")
    refused = request(workspace, "fs_delete", path=".claude/skills/demo")
    assert refused["status"] == 400, refused

    result = request(workspace, "fs_skill_retire", skill_name="demo")
    assert "error" not in result, result
    assert not folder.exists()
    retired = area(workspace) / "retired" / "demo" / result["retired_as"]
    assert (retired / ".env").read_bytes() == b"TOKEN=1\n"
    assert (tmp_path / "outside").read_bytes() == b"x"  # the link is not followed
    assert request(workspace, "fs_skill_retire", skill_name="demo")["status"] == 404


def test_retire_refuses_an_invalid_skill_name(workspace):
    result = request(workspace, "fs_skill_retire", skill_name="ssh-keys")
    assert result["status"] == 400, result


def test_a_deleted_skills_retired_versions_are_pruned_after_the_grace(
    workspace, monkeypatch
):
    clock = [1_000_000_000.0]
    monkeypatch.setattr(files, "_now", lambda: clock[0])
    for version in range(3):
        clock[0] += 10
        publish(workspace, "gone", {"SKILL.md": SKILL_MD + str(version).encode()})
        publish(workspace, "kept", {"SKILL.md": SKILL_MD + str(version).encode()})
    request(workspace, "fs_skill_retire", skill_name="gone")
    retired = area(workspace) / "retired"
    assert len(os.listdir(retired / "gone")) == 3  # all within the grace
    clock[0] += files.SKILL_RETIRED_GRACE_SECONDS + 60
    status(workspace, "anything")
    assert sorted(os.listdir(retired)) == ["kept"]
    assert len(os.listdir(retired / "kept")) == files.SKILL_RETIRED_KEEP


def test_pruning_a_huge_retired_tree_never_fails_the_action(workspace, monkeypatch):
    """Pruning runs on its own budget: the action succeeds, and the tree is
    removed over as many calls as it takes."""
    clock = [1_000_000_000.0]
    monkeypatch.setattr(files, "_now", lambda: clock[0])
    publish(workspace, "demo", {"SKILL.md": SKILL_MD})
    modules = workspace / ".claude" / "skills" / "demo" / "node_modules"
    modules.mkdir()
    for index in range(150):
        (modules / f"m{index}.js").write_bytes(b"x")
    for version in range(3):  # the npm-installed folder is retired whole
        clock[0] += 10
        _publication_id, result = publish(
            workspace, "demo", {"SKILL.md": SKILL_MD + str(version).encode()}
        )
        assert result["status"] == "published", result
    monkeypatch.setattr(files, "SKILL_BUNDLE_ENTRY_LIMIT", 60)
    clock[0] += files.SKILL_RETIRED_GRACE_SECONDS + 60
    retired = area(workspace) / "retired" / "demo"
    for attempt in range(6):
        _publication_id, result = publish(
            workspace, "demo", {"SKILL.md": SKILL_MD + b"next" + bytes([attempt])}
        )
        assert result["status"] == "published", result
        if len(os.listdir(retired)) <= files.SKILL_RETIRED_KEEP:
            break
    assert len(os.listdir(retired)) <= files.SKILL_RETIRED_KEEP


def test_an_unrecoverable_journal_blocks_only_its_own_skill(workspace, monkeypatch):
    """A journal whose recovery keeps failing stays for a retry and blocks
    only its own skill; discovery and every other skill carry on."""
    new_bundle = {"SKILL.md": SKILL_MD + b"new\n"}
    crash_between_journaled_renames(workspace, monkeypatch, new_bundle)

    def refused(self, *_args):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(files.SecureWorkspace, "_skill_land_journaled", refused)
    publish(workspace, "other", {"SKILL.md": SKILL_MD})
    listing = request(workspace, "fs_list_skills")
    assert "error" not in listing, listing
    assert [entry["name"] for entry in listing["skills"]] == ["other"]
    state = status(workspace, "demo")
    assert state["recovery_error"] == "I/O error"
    assert state["live"]["exists"] is False
    for action, params in (
        ("fs_skill_retire", {"skill_name": "demo"}),
        ("fs_skill_stage_begin", {"publication_id": "b" * 32, "skill_name": "demo"}),
        ("fs_write", {"path": ".claude/skills/demo/SKILL.md", "content": "stub"}),
        ("fs_write", {"path": ".Claude/Skills/DEMO/SKILL.md", "content": "stub"}),
    ):
        result = request(workspace, action, **params)
        assert result["status"] == 400, (action, result)
        assert "could not be finished" in result["error"]
    assert not (workspace / ".claude" / "skills" / "demo").exists()
    written = request(
        workspace, "fs_write", path=".claude/skills/other/notes.md", content="x"
    )
    assert "error" not in written, written
    assert len(os.listdir(area(workspace) / "journal")) == 1  # kept for a retry
    # Once the cause is gone, the next action finishes the publication.
    monkeypatch.undo()
    assert "recovery_error" not in status(workspace, "demo")
    published = live_files(workspace, "demo")
    published.pop(".cubicle-bundle.json")
    assert published == new_bundle
    assert os.listdir(area(workspace) / "journal") == []
