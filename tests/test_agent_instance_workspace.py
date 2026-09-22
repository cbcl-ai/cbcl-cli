"""Retained playbooks must not archive credentials or follow worker links."""

import json
import os
from pathlib import Path
import shutil
import uuid

import pytest

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
    monkeypatch.setattr(
        "src.agent_instance_workspace.chown_to_agent", lambda p: chowned.append(Path(p))
    )
    return root, skill, profile, task, archive, chowned


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
    assert "inherited office skill catalog" in (target / "CLAUDE.md").read_text()
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
    assert target / "retained-link" not in chowned
    assert outside not in chowned


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
    (archive / task["agent_instance_id"] / "CLAUDE.md").write_text("tampered")
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
