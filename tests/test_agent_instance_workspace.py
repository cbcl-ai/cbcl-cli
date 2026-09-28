"""Retained playbooks must not archive credentials or follow worker links."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

import pytest

import src.agent_instance_workspace as snapshot_module
from src.agent_instance_workspace import prepare_instance_workspace


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    skill = root / ".claude/skills/research"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Research\nRetained playbook v1")
    (root / "CLAUDE.md").write_text("Current office instructions")
    profile_id, instance_id = str(uuid.uuid4()), str(uuid.uuid4())
    profile = {
        "name": "researcher",
        "display_name": "Researcher",
        "agent_type": "custom",
        "allowed_tools": ["Read"],
        "system_prompt": "Research carefully.",
        "skills": [
            {
                "name": "research",
                "display_name": "Research",
                "parameter_schema": [
                    {"name": "QUERY", "type": "string", "is_secret": False},
                    {"name": "TOKEN", "type": "string", "is_secret": True},
                ],
            }
        ],
    }
    task = {
        "agent_instance_id": instance_id,
        "profile_id": profile_id,
        "profile_revision": "revision-1",
        "task_id": "task-1",
    }
    archive = tmp_path / "private-archive"
    chowned = []

    def record_ownership(descriptor):
        info = os.fstat(descriptor)
        chowned.append((info.st_dev, info.st_ino))

    # Every ownership change goes through an open descriptor (CM4, SEC-1).
    for module in ("src.agent_instance_workspace", "src.config_sync._descriptor_io"):
        monkeypatch.setattr(f"{module}.fchown_to_agent", record_ownership)
    return root, skill, profile, task, archive, chowned


def identity(path):
    info = os.lstat(path)
    return info.st_dev, info.st_ino


def prepare(workspace):
    root, _skill, profile, task, archive, _chowned = workspace
    result = prepare_instance_workspace(str(root), archive, profile, task)
    assert result == f"/workspace/agents/.instances/{task['agent_instance_id']}"
    return root / result.removeprefix("/workspace/")


def test_retains_instruction_files_but_refreshes_only_declared_nonsecret_params(
    workspace,
):
    root, skill, profile, task, archive, _chowned = workspace
    (skill / "params.json").write_text(
        json.dumps(
            {
                "QUERY": "first",
                "TOKEN": "legacy-secret",
                "UNKNOWN": "unclassified-secret",
            }
        )
    )
    for name in (
        ".env",
        ".env.production",
        ".credentials.json",
        ".mcp.json",
        ".secrets.json",
        ".netrc",
    ):
        (skill / name).write_text("credential-value")
    (skill / "notes").mkdir()
    (skill / "notes/.env.local").write_text("nested-secret")
    (skill / ".git").mkdir()
    (skill / ".git/config").write_text("remote-with-token")
    (skill / "run.sh").write_text("#!/bin/sh\ntrue\n")
    (skill / "run.sh").chmod(0o755)
    target = prepare(workspace)
    saved_files = [
        p for p in (archive / task["agent_instance_id"]).rglob("*") if p.is_file()
    ]
    assert not any(
        p.name == "params.json" or p.name.startswith(".env") for p in saved_files
    )
    assert not any(
        "credential-value" in p.read_text()
        or "legacy-secret" in p.read_text()
        or "nested-secret" in p.read_text()
        for p in saved_files
    )
    assert json.loads((target / ".claude/skills/research/params.json").read_text()) == {
        "QUERY": "first"
    }
    assert (target / ".claude/skills/research/run.sh").stat().st_mode & 0o111
    assert "is not this Agent's assignment" in (target / "CLAUDE.md").read_text()
    assert "QUERY" in (target / "CLAUDE.md").read_text()
    original = (target / "CLAUDE.md").read_text()
    (skill / "SKILL.md").write_text("New profile playbook v2")
    (skill / "params.json").write_text(
        json.dumps({"QUERY": "second", "TOKEN": "rotated-secret"})
    )
    (target / "CLAUDE.md").write_text("Worker altered its own instructions")
    (target / ".claude/skills/stray").mkdir()
    (target / ".claude/skills/stray/SKILL.md").write_text("Not assigned")
    prepare(workspace)
    assert (target / "CLAUDE.md").read_text() == original
    assert "v1" in (target / ".claude/skills/research/SKILL.md").read_text()
    assert not (target / ".claude/skills/stray").exists()
    assert json.loads((target / ".claude/skills/research/params.json").read_text()) == {
        "QUERY": "second"
    }
    assert (root / "CLAUDE.md").read_text() == "Current office instructions"


@pytest.mark.parametrize(
    "kind", ["external_file", "internal_secret", "directory", "hardlink", "fifo"]
)
def test_refuses_linked_or_special_skill_content(workspace, tmp_path, kind):
    _root, skill, _profile, _task, archive, _chowned = workspace
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "token"
    secret.write_text("secret")
    if kind == "external_file":
        (skill / "innocent.md").symlink_to(secret)
    elif kind == "internal_secret":
        (skill / ".env").write_text("secret")
        (skill / "innocent.md").symlink_to(skill / ".env")
    elif kind == "directory":
        (skill / "notes").symlink_to(outside, target_is_directory=True)
    elif kind == "hardlink":
        os.link(secret, skill / "innocent.md")
    else:
        os.mkfifo(skill / "fifo")
    with pytest.raises((ValueError, OSError)):
        prepare(workspace)
    assert not list(archive.glob(".snapshot-*"))
    assert secret.read_text() == "secret"


def test_does_not_follow_retained_worker_links_during_restore_or_chown(
    workspace, tmp_path
):
    _root, _skill, _profile, _task, _archive, chowned = workspace
    target = prepare(workspace)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "unchanged"
    sentinel.write_text("original")
    (target / "CLAUDE.md").unlink()
    (target / "CLAUDE.md").symlink_to(sentinel)
    (target / "retained-link").symlink_to(outside, target_is_directory=True)
    chowned.clear()
    prepare(workspace)
    assert sentinel.read_text() == "original"
    assert not (target / "CLAUDE.md").is_symlink()
    assert identity(target / "retained-link") not in chowned
    assert identity(outside) not in chowned
    assert identity(sentinel) not in chowned


def test_missing_mutable_params_are_empty_but_never_archived(workspace):
    target = prepare(workspace)
    assert (
        json.loads((target / ".claude/skills/research/params.json").read_text()) == {}
    )


def test_retained_playbook_survives_deleted_catalog_with_empty_runtime_params(
    workspace,
):
    target = prepare(workspace)
    shutil.rmtree(workspace[1])
    prepare(workspace)
    assert "v1" in (target / ".claude/skills/research/SKILL.md").read_text()
    assert (
        json.loads((target / ".claude/skills/research/params.json").read_text()) == {}
    )


def test_rejects_symlinked_assigned_skill_directory(workspace, tmp_path):
    _root, skill, _profile, _task, _archive, _chowned = workspace
    outside = tmp_path / "external-skill"
    skill.rename(outside)
    skill.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe path"):
        prepare(workspace)


def test_linked_mutable_params_refuse_launch(workspace, tmp_path):
    _root, skill, _profile, _task, _archive, _chowned = workspace
    secret = tmp_path / "token"
    secret.write_text('{"QUERY":"secret"}')
    (skill / "params.json").symlink_to(secret)
    with pytest.raises(OSError):
        prepare(workspace)


def test_archive_identity_integrity_and_private_location_are_enforced(workspace):
    root, _skill, profile, task, archive, _chowned = workspace
    prepare(workspace)
    with pytest.raises(ValueError, match="outside"):
        prepare_instance_workspace(str(root), root / "archive", profile, task)
    with pytest.raises(ValueError, match="backend identity"):
        prepare_instance_workspace(
            str(root), archive, profile, {**task, "profile_revision": "wrong"}
        )
    # Current archives keep the Profile-owned parts, integrity-checked (X51).
    (archive / task["agent_instance_id"] / "profile.json").write_text(
        '{"system_prompt": "tampered"}'
    )
    with pytest.raises(ValueError, match="integrity"):
        prepare(workspace)


@pytest.mark.parametrize(
    "prior_session,existing_workspace", [(True, False), (True, True), (False, True)]
)
def test_missing_archive_refuses_existing_agent_before_copying_current_playbooks(
    workspace, monkeypatch, prior_session, existing_workspace
):
    root, skill, _profile, task, archive, _chowned = workspace
    target = root / "agents/.instances" / task["agent_instance_id"]
    if existing_workspace:
        prepare(workspace)
        original = (target / "CLAUDE.md").read_text()
        shutil.rmtree(archive / task["agent_instance_id"])
    if prior_session:
        task["prior_session_id"] = "retained-session"
    (skill / "SKILL.md").write_text("Current playbook must never replace retained work")

    def unexpected_staging(*args, **kwargs):
        pytest.fail("Missing retained archive must be refused before staging")

    monkeypatch.setattr(
        "src.agent_instance_workspace.tempfile.mkdtemp", unexpected_staging
    )
    with pytest.raises(
        ValueError, match="Restore.*private instruction archive.*new Agent assignment"
    ):
        prepare(workspace)
    assert not (archive / task["agent_instance_id"]).exists()
    if existing_workspace:
        assert (target / "CLAUDE.md").read_text() == original
        assert "v1" in (target / ".claude/skills/research/SKILL.md").read_text()
    else:
        assert not target.exists()


def test_resume_with_existing_archive_preserves_original_playbook(workspace):
    _root, skill, _profile, task, _archive, _chowned = workspace
    target = prepare(workspace)
    original = (target / "CLAUDE.md").read_text()
    task["prior_session_id"] = "retained-session"
    (skill / "SKILL.md").write_text("Current catalog playbook v2")
    prepare(workspace)
    assert (target / "CLAUDE.md").read_text() == original
    assert "v1" in (target / ".claude/skills/research/SKILL.md").read_text()


@pytest.mark.parametrize("classification", ["secret", "removed", "public"])
def test_current_parameter_metadata_restricts_retained_nonsecret_allowlist(
    workspace, classification
):
    root, skill, profile, task, archive, _chowned = workspace
    (skill / "params.json").write_text(json.dumps({"QUERY": "original"}))
    target = prepare(workspace)
    original = (target / "CLAUDE.md").read_text()
    manifest = (archive / task["agent_instance_id"] / "manifest.json").read_text()
    (skill / "params.json").write_text(
        json.dumps({"QUERY": "current", "TOKEN": "must-remain-excluded"})
    )
    # Even newly public parameters must also be public in the retained schema.
    current_skills = (
        []
        if classification == "removed"
        else [
            {
                "name": "research",
                "parameter_schema": [
                    {"name": "QUERY", "is_secret": classification == "secret"},
                    {"name": "TOKEN", "is_secret": False},
                ],
            }
        ]
    )
    prepare_instance_workspace(
        str(root),
        archive,
        profile,
        task,
        current_skills=current_skills,
    )
    expected = {"QUERY": "current"} if classification == "public" else {}
    assert (
        json.loads((target / ".claude/skills/research/params.json").read_text())
        == expected
    )
    assert (target / "CLAUDE.md").read_text() == original
    assert (
        archive / task["agent_instance_id"] / "manifest.json"
    ).read_text() == manifest
    assert profile["skills"][0]["parameter_schema"][0]["is_secret"] is False


@pytest.mark.parametrize("classification", ["omitted", None, "false"])
def test_parameter_secrecy_uses_schema_default_but_rejects_malformed_flags(
    workspace, classification
):
    root, skill, profile, task, archive, _chowned = workspace
    parameter = {"name": "QUERY"}
    if classification != "omitted":
        parameter["is_secret"] = classification
    profile["skills"][0]["parameter_schema"] = [parameter]
    (skill / "params.json").write_text(json.dumps({"QUERY": "existing-value"}))
    prepare_instance_workspace(
        str(root), archive, profile, task, current_skills=profile["skills"]
    )
    destination = root / "agents/.instances" / task["agent_instance_id"]
    params = json.loads(
        (destination / ".claude/skills/research/params.json").read_text()
    )
    assert params == (
        {"QUERY": "existing-value"} if classification == "omitted" else {}
    )


# ---------------------------------------------------------------------------
# X51 — platform rules are rendered fresh; only Profile-owned parts are pinned
# ---------------------------------------------------------------------------


def _manifest(archive, task):
    return json.loads(
        (archive / task["agent_instance_id"] / "manifest.json").read_text()
    )


def test_new_archive_keeps_only_profile_owned_parts(workspace):
    _root, _skill, profile, task, archive, _chowned = workspace
    profile["claude_md_content"] = "Office note for researchers."
    profile["office_work_policy"] = None
    profile["connectors"] = [
        {"id": "c1", "name": "crm", "display_name": "CRM", "is_enabled": True}
    ]
    prepare(workspace)
    saved = archive / task["agent_instance_id"]
    manifest = _manifest(archive, task)
    # Identity keys are the legacy three; the format marker is additive.
    assert {key: manifest[key] for key in (
        "agent_instance_id", "profile_id", "profile_revision"
    )} == {
        "agent_instance_id": task["agent_instance_id"],
        "profile_id": task["profile_id"],
        "profile_revision": task["profile_revision"],
    }
    assert manifest["instructions"] == "profile-parts-v1"
    # The render inputs are the Profile-owned parts; the rendered CLAUDE.md
    # is kept only as a rollback copy a legacy-format daemon restores (it is
    # listed in ``files`` with its digest, exactly like a legacy archive).
    assert "profile.json" not in manifest["files"]
    rollback = (saved / "CLAUDE.md").read_bytes()
    assert manifest["files"]["CLAUDE.md"] == hashlib.sha256(rollback).hexdigest()
    assert b"Research carefully." in rollback
    # A legacy-format daemon restores every listed file after checking its
    # digest: each one exists and verifies, so a rollback keeps a playbook.
    for relative, digest in manifest["files"].items():
        assert hashlib.sha256((saved / relative).read_bytes()).hexdigest() == digest
    parts = json.loads((saved / "profile.json").read_text())
    assert parts["system_prompt"] == "Research carefully."
    assert parts["claude_md_content"] == "Office note for researchers."
    assert parts["office_work_policy"] is None
    assert "connectors" not in parts
    platform_marker = "## Communication"
    assert platform_marker not in (saved / "profile.json").read_text()


def test_platform_rules_follow_the_running_daemon_but_profile_parts_stay(
    workspace, monkeypatch
):
    _root, _skill, profile, task, archive, _chowned = workspace
    target = prepare(workspace)
    first = (target / "CLAUDE.md").read_text()
    assert "Research carefully." in first
    # A daemon upgrade changes the shared platform rules...
    from src.config_sync.claude_md_templates import _custom_agent

    monkeypatch.setattr(
        _custom_agent,
        "SHARED_AGENT_WORK_RULES",
        "## Communication\n\nUPGRADED PLATFORM RULE",
    )
    # ...while the live Profile has been edited (the snapshot stays pinned).
    edited = {**profile, "system_prompt": "Edited live profile prompt."}
    task["prior_session_id"] = "retained-session"
    prepare_instance_workspace(
        str(workspace[0]), workspace[4], edited, task
    )
    resumed = (target / "CLAUDE.md").read_text()
    assert "UPGRADED PLATFORM RULE" in resumed
    assert "Research carefully." in resumed
    assert "Edited live profile prompt." not in resumed
    # The archived rollback copy keeps the snapshot-time rules and is never
    # what the running daemon restores.
    rollback = (archive / task["agent_instance_id"] / "CLAUDE.md").read_text()
    assert "UPGRADED PLATFORM RULE" not in rollback
    assert rollback == first


def test_retained_note_precedes_office_notes(workspace):
    _root, _skill, profile, _task, _archive, _chowned = workspace
    profile["claude_md_content"] = "Always cite sources."
    text = (prepare(workspace) / "CLAUDE.md").read_text()
    note = text.index("## Retained task Agent configuration")
    office = text.index("## Office Notes")
    assert note < office
    assert text.index("Always cite sources.") > office


def test_revoked_connector_is_not_rendered_on_a_later_attempt(workspace):
    root, _skill, profile, task, archive, _chowned = workspace
    profile["connectors"] = [
        {
            "id": "c1",
            "name": "crm",
            "display_name": "CRM",
            "connection_type": "mcp",
            "mcp_server_name": "crm",
            "is_enabled": True,
        }
    ]
    target = prepare(workspace)
    assert "CRM" in (target / "CLAUDE.md").read_text()
    # The supervisor drops a revoked connector from the attempt's profile.
    prepare_instance_workspace(str(root), archive, {**profile, "connectors": []}, task)
    assert "CRM" not in (target / "CLAUDE.md").read_text()


def test_legacy_archive_resumes_byte_for_byte(workspace, monkeypatch):
    """Archives written before X51 stored the rendered CLAUDE.md; they keep
    resuming exactly as archived, with unchanged identity keys."""
    import hashlib

    root, skill, profile, task, archive, _chowned = workspace
    legacy_text = "# Legacy retained playbook\n\nFrozen platform text v0.\n"
    saved = archive / task["agent_instance_id"]
    (saved / ".claude/skills/research").mkdir(parents=True)
    (saved / "CLAUDE.md").write_text(legacy_text)
    (saved / ".claude/skills/research/SKILL.md").write_text("# Research\nlegacy")
    files = {
        relative: hashlib.sha256((saved / relative).read_bytes()).hexdigest()
        for relative in ("CLAUDE.md", ".claude/skills/research/SKILL.md")
    }
    (saved / "manifest.json").write_text(
        json.dumps(
            {
                "agent_instance_id": task["agent_instance_id"],
                "profile_id": task["profile_id"],
                "profile_revision": task["profile_revision"],
                "files": files,
            },
            sort_keys=True,
        )
    )
    task["prior_session_id"] = "retained-session"
    from src.config_sync.claude_md_templates import _custom_agent

    monkeypatch.setattr(_custom_agent, "SHARED_AGENT_WORK_RULES", "NEW RULES")
    target = prepare(workspace)
    assert (target / "CLAUDE.md").read_text() == legacy_text
    assert (target / ".claude/skills/research/SKILL.md").read_text() == (
        "# Research\nlegacy"
    )
    # A tampered legacy CLAUDE.md is still refused.
    (saved / "CLAUDE.md").write_text("tampered")
    with pytest.raises(ValueError, match="integrity"):
        prepare(workspace)


def test_rendered_instructions_refuse_a_planted_link(workspace, tmp_path, monkeypatch):
    root, _skill, profile, task, archive, _chowned = workspace
    target = prepare(workspace)
    outside = tmp_path / "outside.md"
    outside.write_text("original")
    real_create = snapshot_module._create_file

    def plant_link_after_restore(parent_fd, name, data, mode):
        real_create(parent_fd, name, data, mode)
        link = target / "CLAUDE.md"
        if not link.exists() and not link.is_symlink():
            link.symlink_to(outside)

    monkeypatch.setattr(snapshot_module, "_create_file", plant_link_after_restore)
    with pytest.raises(FileExistsError):
        prepare_instance_workspace(str(root), archive, profile, task)
    assert outside.read_text() == "original"


# ---------------------------------------------------------------------------
# F03 snapshot coordination / X15 / X52 / CM4 — skill copies
# ---------------------------------------------------------------------------


def _archived_skill_files(archive, task, name="research"):
    root = archive / task["agent_instance_id"] / ".claude/skills" / name
    return {
        str(path.relative_to(root)): path.read_text()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.fixture
def no_backoff(monkeypatch):
    sleeps = []
    monkeypatch.setattr(snapshot_module, "_sleep", sleeps.append)
    return sleeps


def test_whole_directory_swap_during_copy_archives_exactly_the_new_version(
    workspace, monkeypatch, no_backoff
):
    root, skill, _profile, task, archive, _chowned = workspace
    (skill / "old-only.md").write_text("old resource")
    staged = root / ".claude/skills/.staged-research"
    staged.mkdir()
    (staged / "SKILL.md").write_text("# Research\nplaybook v2")
    (staged / "new-only.md").write_text("new resource")
    real_copy = snapshot_module._copy_skill
    top_level_copies = []

    def publisher_swaps_mid_copy(source, target, parts=()):
        if not parts:
            top_level_copies.append(target)
            if len(top_level_copies) == 1:
                # renameat2 exchange / journaled fallback: whole directories
                # move; the opened (old) directory is never edited in place.
                skill.rename(root / ".claude/skills/.retired-research")
                staged.rename(skill)
        return real_copy(source, target, parts)

    monkeypatch.setattr(snapshot_module, "_copy_skill", publisher_swaps_mid_copy)
    target = prepare(workspace)
    assert len(top_level_copies) == 2
    assert _archived_skill_files(archive, task) == {
        "SKILL.md": "# Research\nplaybook v2",
        "new-only.md": "new resource",
    }
    restored = target / ".claude/skills/research"
    assert not (restored / "old-only.md").exists()
    assert (restored / "SKILL.md").read_text() == "# Research\nplaybook v2"
    assert len(no_backoff) == 1


def test_skill_removed_and_recreated_during_copy_is_copied_again(
    workspace, monkeypatch, no_backoff
):
    root, skill, _profile, task, archive, _chowned = workspace
    real_copy = snapshot_module._copy_skill
    calls = []

    def remove_then_recreate(source, target, parts=()):
        if not parts:
            calls.append(target)
            if len(calls) == 1:
                # The opened directory is unlinked (nlink 0) mid-copy.
                shutil.rmtree(skill)
                skill.mkdir()
                (skill / "SKILL.md").write_text("# Research\nrecreated")
        return real_copy(source, target, parts)

    monkeypatch.setattr(snapshot_module, "_copy_skill", remove_then_recreate)
    prepare(workspace)
    assert len(calls) == 2
    assert _archived_skill_files(archive, task) == {
        "SKILL.md": "# Research\nrecreated"
    }


def test_journaled_swap_window_retries_until_the_skill_is_live_again(
    workspace, monkeypatch
):
    root, skill, _profile, task, archive, _chowned = workspace
    retired = root / ".claude/skills/.previous-research"
    skill.rename(retired)  # the window between the two journaled renames
    sleeps = []

    def finish_swap(delay):
        sleeps.append(delay)
        if not skill.exists():
            retired.rename(skill)

    monkeypatch.setattr(snapshot_module, "_sleep", finish_swap)
    unavailable = []
    prepare_instance_workspace(
        str(root), archive, workspace[2], task, unavailable_skills=unavailable
    )
    assert unavailable == []
    assert len(sleeps) == 1
    assert _archived_skill_files(archive, task)["SKILL.md"].endswith(
        "Retained playbook v1"
    )


def test_skill_replaced_during_every_attempt_refuses_the_snapshot(
    workspace, monkeypatch, no_backoff
):
    root, skill, _profile, task, archive, _chowned = workspace
    other = root / ".claude/skills/.other-research"
    other.mkdir()
    (other / "SKILL.md").write_text("# Research\nother")
    real_copy = snapshot_module._copy_skill

    def swap_every_time(source, target, parts=()):
        if not parts:
            parked = root / ".claude/skills/.parked"
            skill.rename(parked)
            other.rename(skill)
            parked.rename(other)
        return real_copy(source, target, parts)

    monkeypatch.setattr(snapshot_module, "_copy_skill", swap_every_time)
    with pytest.raises(ValueError, match="'research' changed during each of 5"):
        prepare(workspace)
    assert len(no_backoff) == snapshot_module.SKILL_COPY_ATTEMPTS - 1
    assert not (archive / task["agent_instance_id"]).exists()
    assert not list(archive.glob(".snapshot-*"))


def test_new_snapshot_continues_without_missing_or_incomplete_skills(
    workspace, no_backoff
):
    root, _skill, profile, task, archive, _chowned = workspace
    profile["skills"] += [
        {"name": "sop-intake", "display_name": "Intake SOP"},
        {
            "name": "half-installed",
            "display_name": "Half installed",
            "parameter_schema": [{"name": "MODE", "is_secret": False}],
        },
    ]
    incomplete = root / ".claude/skills/half-installed"
    incomplete.mkdir()
    (incomplete / "helper.py").write_text("print('no playbook')")
    (incomplete / "params.json").write_text(json.dumps({"MODE": "fast"}))
    unavailable = []
    cwd = prepare_instance_workspace(
        str(root), archive, profile, task, unavailable_skills=unavailable
    )
    target = root / cwd.removeprefix("/workspace/")
    assert unavailable == [
        {
            "name": "half-installed",
            "reason": "its folder has no SKILL.md (incomplete install)",
        },
        {"name": "sop-intake", "reason": "not installed in the office skills folder"},
    ]
    manifest = _manifest(archive, task)
    assert manifest["skills"] == {
        "research": {"status": "copied", "bundle_sha256": None},
        "half-installed": {
            "status": "unavailable",
            "reason": "its folder has no SKILL.md (incomplete install)",
        },
        "sop-intake": {
            "status": "unavailable",
            "reason": "not installed in the office skills folder",
        },
    }
    # Identity keys are untouched by the additive records.
    assert {key: manifest[key] for key in (
        "agent_instance_id", "profile_id", "profile_revision"
    )} == {key: task[key] for key in (
        "agent_instance_id", "profile_id", "profile_revision"
    )}
    text = (target / "CLAUDE.md").read_text()
    assert "## Unavailable assigned skills" in text
    assert "- `sop-intake`: not installed in the office skills folder" in text
    assert "- `half-installed`: its folder has no SKILL.md" in text
    # The skill index lists only what this Agent actually has.
    assert "`.claude/skills/research/SKILL.md`" in text
    assert "`.claude/skills/sop-intake/SKILL.md`" not in text
    assert "`.claude/skills/half-installed/SKILL.md`" not in text
    rollback = (archive / task["agent_instance_id"] / "CLAUDE.md").read_text()
    assert "## Unavailable assigned skills" in rollback
    for name in ("sop-intake", "half-installed"):
        restored = target / ".claude/skills" / name
        assert not (restored / "SKILL.md").exists()
        assert not (restored / "params.json").exists()
        assert (restored / "UNAVAILABLE.md").is_file()
    assert not (target / ".claude/skills/half-installed/helper.py").exists()
    assert (target / ".claude/skills/research/SKILL.md").is_file()


def test_resumed_snapshot_keeps_its_skill_record_and_stays_strict(
    workspace, no_backoff
):
    root, _skill, profile, task, archive, _chowned = workspace
    profile["skills"].append({"name": "sop-intake"})
    prepare(workspace)
    original = (
        root / "agents/.instances" / task["agent_instance_id"] / "CLAUDE.md"
    ).read_text()
    # Installing the skill later does not change the retained Agent, and the
    # resumed attempt reports nothing new.
    installed = root / ".claude/skills/sop-intake"
    installed.mkdir()
    (installed / "SKILL.md").write_text("# Intake\nlate")
    task["prior_session_id"] = "retained-session"
    unavailable = []
    cwd = prepare_instance_workspace(
        str(root), archive, profile, task, unavailable_skills=unavailable
    )
    target = root / cwd.removeprefix("/workspace/")
    assert unavailable == []
    assert (target / "CLAUDE.md").read_text() == original
    assert not (target / ".claude/skills/sop-intake/SKILL.md").exists()
    # A retained skill that is gone from the archive is refused, never
    # silently dropped on resume.
    manifest_path = archive / task["agent_instance_id"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["files"][".claude/skills/research/SKILL.md"]
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    with pytest.raises(ValueError, match="missing assigned skill 'research'"):
        prepare(workspace)


def test_nested_params_json_is_kept_but_root_params_json_is_not(workspace):
    _root, skill, _profile, task, archive, _chowned = workspace
    (skill / "params.json").write_text(json.dumps({"QUERY": "runtime value"}))
    (skill / "templates").mkdir()
    (skill / "templates/params.json").write_text('{"layout": "resource"}')
    target = prepare(workspace)
    manifest = _manifest(archive, task)
    assert ".claude/skills/research/templates/params.json" in manifest["files"]
    assert ".claude/skills/research/params.json" not in manifest["files"]
    assert "runtime value" not in json.dumps(_archived_skill_files(archive, task))
    restored = target / ".claude/skills/research"
    assert (restored / "templates/params.json").read_text() == '{"layout": "resource"}'
    assert json.loads((restored / "params.json").read_text()) == {
        "QUERY": "runtime value"
    }
    # The kept resource also restores on a resumed attempt.
    task["prior_session_id"] = "retained-session"
    prepare(workspace)
    assert (restored / "templates/params.json").read_text() == '{"layout": "resource"}'


def test_resume_refuses_an_archived_root_params_json(workspace):
    _root, _skill, _profile, task, archive, _chowned = workspace
    prepare(workspace)
    saved = archive / task["agent_instance_id"]
    planted = saved / ".claude/skills/research/params.json"
    planted.write_text('{"TOKEN": "secret"}')
    manifest = json.loads((saved / "manifest.json").read_text())
    manifest["files"][".claude/skills/research/params.json"] = hashlib.sha256(
        planted.read_bytes()
    ).hexdigest()
    (saved / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    with pytest.raises(ValueError, match="protected runtime data"):
        prepare(workspace)


@pytest.mark.parametrize(
    "marker,expected",
    [
        (json.dumps({"format": 1, "bundle_sha256": "a" * 64}), "a" * 64),
        (json.dumps({"format": 1, "bundle_sha256": "not-a-digest"}), None),
        ("{not json", None),
        (None, None),
        # B4-hygiene-07: over the Files helper's 512 KiB manifest cap (the
        # helper treats the folder as unmanaged), so no identity either.
        pytest.param(
            json.dumps(
                {"format": 1, "bundle_sha256": "a" * 64, "pad": "x" * (600 * 1024)}
            ),
            None,
            id="over-the-helper-manifest-cap",
        ),
    ],
)
def test_bundle_identity_is_recorded_when_the_skill_carries_one(
    workspace, marker, expected
):
    _root, skill, _profile, task, archive, _chowned = workspace
    if marker is not None:
        (skill / ".cubicle-bundle.json").write_text(marker)
    prepare(workspace)
    assert _manifest(archive, task)["skills"]["research"] == {
        "status": "copied",
        "bundle_sha256": expected,
    }


def test_unsafe_skill_content_names_the_skill_and_file(workspace):
    _root, skill, _profile, _task, archive, _chowned = workspace
    (skill / "notes").mkdir()
    os.mkfifo(skill / "notes/pipe")
    with pytest.raises(ValueError, match="'research'.*'notes/pipe' is a special file"):
        prepare(workspace)
    assert not list(archive.glob(".snapshot-*"))


def test_workspace_failure_text_is_precise_but_path_free():
    text = snapshot_module.workspace_failure_text(
        PermissionError(13, "Permission denied", "/home/operator/.cubicle/private")
    )
    assert text == "Task Agent workspace could not be prepared: Permission denied"
    assert snapshot_module.workspace_failure_text(
        snapshot_module.WorkspaceRefusal(
            "Assigned skill 'x': Skill file 'a\nb' is a special file"
        )
    ).endswith("Skill file 'a?b' is a special file")
    # Text this module did not author is never shown (SEC-4).
    assert snapshot_module.workspace_failure_text(
        RuntimeError("Symlink loop from '/home/operator/workspace/agents'")
    ) == (
        "Task Agent workspace could not be prepared: unexpected RuntimeError; "
        "see the daemon log"
    )


def test_rendered_instructions_are_owned_through_the_open_descriptor(
    workspace, tmp_path, monkeypatch
):
    """CM4: ownership follows the created file, never a path swapped to a link."""
    root, _skill, profile, task, archive, chowned = workspace
    target = prepare(workspace)
    outside = tmp_path / "outside.md"
    outside.write_text("original")
    created = []
    real_create = snapshot_module._create_file

    def swap_after_create(parent_fd, name, data, mode):
        real_create(parent_fd, name, data, mode)
        if name == "CLAUDE.md":
            created.append(identity(target / "CLAUDE.md"))
            # A worker replaces the new CLAUDE.md with a link right away.
            (target / "CLAUDE.md").unlink()
            (target / "CLAUDE.md").symlink_to(outside)

    monkeypatch.setattr(snapshot_module, "_create_file", swap_after_create)
    chowned.clear()
    prepare_instance_workspace(str(root), archive, profile, task)
    assert created and created[0] in chowned
    assert identity(outside) not in chowned
    assert outside.read_text() == "original"


def test_restore_makes_no_path_based_ownership_change(workspace, monkeypatch):
    calls = []
    monkeypatch.setattr(
        snapshot_module.os, "chown", lambda *args, **kwargs: calls.append(args)
    )
    prepare(workspace)
    task = workspace[3]
    task["prior_session_id"] = "retained-session"
    prepare(workspace)
    assert calls == []


# ---------------------------------------------------------------------------
# SEC-1 / SEC-2 — the restore never follows a link a worker swapped in
# ---------------------------------------------------------------------------


@pytest.fixture
def host_private(tmp_path):
    """A host directory outside the workspace a worker would like to reach."""
    private = tmp_path / "host-private"
    (private / ".claude").mkdir(parents=True)
    (private / ".claude/credentials").write_text("host credential")
    (private / "CLAUDE.md").write_text("host instructions")
    return private


def _host_private_state(private):
    return {
        str(path.relative_to(private)): (path.read_text(), identity(path))
        for path in sorted(private.rglob("*"))
        if path.is_file()
    }


def _swap_for_link(directory, destination):
    parked = directory.with_name(directory.name + ".parked")
    directory.rename(parked)
    directory.symlink_to(destination, target_is_directory=True)


@pytest.mark.parametrize("phase", ["new", "resume"])
def test_restore_refuses_a_task_directory_swapped_for_a_link(
    workspace, monkeypatch, host_private, phase
):
    root, _skill, _profile, task, _archive, chowned = workspace
    target = root / "agents/.instances" / task["agent_instance_id"]
    if phase == "resume":
        prepare(workspace)
        task["prior_session_id"] = "retained-session"
        # After the fast symlink check, before the restore: swap the Agent
        # directory for a link to host-private data.
        real_render = snapshot_module._render_claude_md

        def swap_then_render(*args, **kwargs):
            if not target.is_symlink():
                _swap_for_link(target, host_private)
            return real_render(*args, **kwargs)

        monkeypatch.setattr(snapshot_module, "_render_claude_md", swap_then_render)
    else:
        target.parent.mkdir(parents=True)
        real_snapshot = snapshot_module._snapshot_skill

        def swap_during_copy(root_path, name, destination):
            record = real_snapshot(root_path, name, destination)
            # The Agent directory appears as a link while skills are copied.
            target.symlink_to(host_private, target_is_directory=True)
            return record

        monkeypatch.setattr(snapshot_module, "_snapshot_skill", swap_during_copy)
    before = _host_private_state(host_private)
    private_dirs = {identity(host_private), identity(host_private / ".claude")}
    chowned.clear()
    with pytest.raises(OSError):
        prepare(workspace)
    assert _host_private_state(host_private) == before
    assert not (host_private / ".claude/settings.json").exists()
    assert not (host_private / ".claude/skills").exists()
    assert not private_dirs.intersection(chowned)


def test_restore_refuses_a_link_swapped_in_for_the_instances_directory(
    workspace, monkeypatch, host_private
):
    root, _skill, _profile, task, _archive, chowned = workspace
    prepare(workspace)
    task["prior_session_id"] = "retained-session"
    instances = root / "agents/.instances"
    (host_private / task["agent_instance_id"]).mkdir()
    real_render = snapshot_module._render_claude_md

    def swap_then_render(*args, **kwargs):
        if not instances.is_symlink():
            _swap_for_link(instances, host_private)
        return real_render(*args, **kwargs)

    monkeypatch.setattr(snapshot_module, "_render_claude_md", swap_then_render)
    before = _host_private_state(host_private)
    chowned.clear()
    with pytest.raises(OSError):
        prepare(workspace)
    assert _host_private_state(host_private) == before
    assert not list((host_private / task["agent_instance_id"]).iterdir())
    assert identity(host_private) not in chowned


def test_planted_settings_link_between_removal_and_write_is_refused(
    workspace, monkeypatch, tmp_path
):
    """SEC-2: a worker recreating .claude with a settings.json link after the
    removal cannot make the daemon write or re-own the link target."""
    root, _skill, _profile, task, _archive, chowned = workspace
    target = prepare(workspace)
    task["prior_session_id"] = "retained-session"
    outside = tmp_path / "host-passwd"
    outside.write_text("root:x:0:0")
    real_remove = snapshot_module._remove_entry

    def plant_after_removal(parent_fd, name, *, directory_allowed):
        real_remove(parent_fd, name, directory_allowed=directory_allowed)
        if name == ".claude":
            (target / ".claude").mkdir()
            (target / ".claude/settings.json").symlink_to(outside)

    monkeypatch.setattr(snapshot_module, "_remove_entry", plant_after_removal)
    chowned.clear()
    with pytest.raises(ValueError, match="changed while its instructions"):
        prepare(workspace)
    assert outside.read_text() == "root:x:0:0"
    assert identity(outside) not in chowned


def test_claude_directory_is_handed_over_only_after_the_daemon_wrote_into_it(
    workspace,
):
    root, skill, _profile, task, _archive, chowned = workspace
    (skill / "params.json").write_text(json.dumps({"QUERY": "value"}))
    target = prepare(workspace)
    claude = target / ".claude"
    handover = chowned.index(identity(claude))
    written_inside = [
        identity(path) for path in claude.rglob("*") if path.is_file()
    ]
    assert identity(claude / "settings.json") in written_inside
    assert identity(claude / "skills/research/params.json") in written_inside
    for file_identity in written_inside:
        assert chowned.index(file_identity) < handover
    for directory in (claude / "skills/research", claude / "skills"):
        assert chowned.index(identity(directory)) < handover


# ---------------------------------------------------------------------------
# SNAP-1 — one exact-case SKILL.md rule for creation and resume
# ---------------------------------------------------------------------------


def test_differently_cased_playbook_is_unavailable_even_on_case_insensitive_hosts(
    workspace, monkeypatch, no_backoff
):
    root, skill, profile, task, archive, _chowned = workspace
    (skill / "SKILL.md").rename(skill / "skill.md")
    real_is_file = Path.is_file

    def case_insensitive_is_file(path, *args, **kwargs):
        # APFS / Docker Desktop: "SKILL.md" resolves to "skill.md".
        if real_is_file(path, *args, **kwargs):
            return True
        parent = path.parent
        return parent.is_dir() and any(
            entry.lower() == path.name.lower() and real_is_file(parent / entry)
            for entry in os.listdir(parent)
        )

    monkeypatch.setattr(Path, "is_file", case_insensitive_is_file)
    unavailable = []
    prepare_instance_workspace(
        str(root), archive, profile, task, unavailable_skills=unavailable
    )
    assert _manifest(archive, task)["skills"]["research"]["status"] == "unavailable"
    assert [item["name"] for item in unavailable] == ["research"]
    # The recorded state resumes: no permanent wedge.
    task["prior_session_id"] = "retained-session"
    prepare(workspace)
    prepare(workspace)


def test_archive_without_skill_records_resumes_without_the_strict_check(workspace):
    """Archives from before the skills map (X51 format) keep resuming."""
    _root, _skill, _profile, task, archive, _chowned = workspace
    target = prepare(workspace)
    saved = archive / task["agent_instance_id"]
    manifest = json.loads((saved / "manifest.json").read_text())
    del manifest["skills"]
    # A playbook an older daemon accepted on a case-insensitive host.
    old_key = ".claude/skills/research/SKILL.md"
    new_key = ".claude/skills/research/skill.md"
    (saved / old_key).rename(saved / new_key)
    manifest["files"][new_key] = manifest["files"].pop(old_key)
    (saved / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    task["prior_session_id"] = "retained-session"
    prepare(workspace)
    assert (target / ".claude/skills/research/skill.md").is_file()


# ---------------------------------------------------------------------------
# SEC-4 — failure text never carries a host path
# ---------------------------------------------------------------------------


def _failure_text(workspace):
    with pytest.raises(Exception) as raised:
        prepare(workspace)
    return snapshot_module.workspace_failure_text(raised.value)


def _assert_path_free(text, root):
    assert text.startswith("Task Agent workspace could not be prepared: ")
    assert str(root) not in text
    assert "/" not in text


@pytest.mark.parametrize("phase", ["before_check", "after_check"])
def test_self_loop_at_the_agent_directory_yields_a_path_free_failure(
    workspace, monkeypatch, phase
):
    root, _skill, _profile, task, _archive, _chowned = workspace
    target = prepare(workspace)
    task["prior_session_id"] = "retained-session"

    def make_loop():
        shutil.rmtree(target)
        target.symlink_to(target.name)  # ln -s <id> agents/.instances/<id>

    if phase == "before_check":
        make_loop()
    else:
        real_render = snapshot_module._render_claude_md

        def loop_then_render(*args, **kwargs):
            if not target.is_symlink():
                make_loop()
            return real_render(*args, **kwargs)

        monkeypatch.setattr(snapshot_module, "_render_claude_md", loop_then_render)
    _assert_path_free(_failure_text(workspace), root)


def test_self_loop_at_the_output_directory_yields_a_path_free_failure(workspace):
    root, _skill, _profile, task, _archive, _chowned = workspace
    task["output_dir"] = "/workspace/outputs/WS"
    (root / "outputs").mkdir()
    (root / "outputs/WS").symlink_to("WS")
    _assert_path_free(_failure_text(workspace), root)


def test_malformed_live_params_names_the_skill_without_a_path(workspace):
    root, skill, _profile, _task, _archive, _chowned = workspace
    (skill / "params.json").write_text("{not json")
    text = _failure_text(workspace)
    assert text.endswith("'research' has a params.json that is not valid JSON")
    assert str(root) not in text


def test_deeply_nested_live_params_is_an_authored_refusal(workspace):
    root, skill, _profile, _task, _archive, _chowned = workspace
    (skill / "params.json").write_text("[" * 200_000 + "]" * 200_000)
    text = _failure_text(workspace)
    assert text.endswith("'research' has a params.json that is not valid JSON")
    assert str(root) not in text


# ---------------------------------------------------------------------------
# SNAPSHOT-RESTORE-CASEFOLD — archives from the exact-case protection rule
# ---------------------------------------------------------------------------


def _add_archived_file(archive, task, relative, content):
    saved = archive / task["agent_instance_id"]
    path = saved / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    manifest = json.loads((saved / "manifest.json").read_text())
    manifest["files"][relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    (saved / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))


@pytest.mark.parametrize(
    "relative",
    [
        ".claude/skills/research/.ENV",
        ".claude/skills/research/.Git/config",
        ".claude/skills/research/.NPMRC",
        ".claude/skills/research/notes/.Env.Local",
        ".claude/skills/research/.Claude/settings.json",
    ],
)
def test_names_protected_only_case_insensitively_are_skipped_on_restore(
    workspace, caplog, relative
):
    """An older daemon's exact-case rule archived these legitimately; the
    task Agent must still resume, without them."""
    _root, _skill, _profile, task, archive, _chowned = workspace
    target = prepare(workspace)
    _add_archived_file(archive, task, relative, "archived-secret-value")
    task["prior_session_id"] = "retained-session"
    caplog.set_level("WARNING", logger="src.agent_instance_workspace")
    prepare(workspace)
    assert not (target / relative).exists()
    assert (target / ".claude/skills/research/SKILL.md").is_file()
    warnings = [
        record.getMessage()
        for record in caplog.records
        if "now a protected name" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert task["agent_instance_id"] in warnings[0]
    assert repr(relative) in warnings[0]
    assert "archived-secret-value" not in caplog.text


@pytest.mark.parametrize(
    "relative",
    [
        ".claude/skills/research/.env",
        ".claude/skills/research/.git/config",
        ".claude/skills/research/.env.production",
        ".claude/skills/research/params.json",
    ],
)
def test_names_protected_under_the_exact_case_rule_still_refuse_the_restore(
    workspace, relative
):
    _root, _skill, _profile, task, archive, _chowned = workspace
    target = prepare(workspace)
    _add_archived_file(archive, task, relative, "secret")
    task["prior_session_id"] = "retained-session"
    with pytest.raises(ValueError, match="protected runtime data"):
        prepare(workspace)
    assert not (target / relative).exists()
