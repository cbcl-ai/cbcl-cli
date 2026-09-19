"""Real Linux ownership/Files regression and the upload-directory repair boundary."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import _upload_directory_setup as setup
from src._agent_image import secure_files


@pytest.fixture
def workspace():
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("Real upload ownership acceptance requires isolated Linux root")
    # pytest's own private temp parents prevent uid 1000 traversal.
    with tempfile.TemporaryDirectory(prefix="cbcl-upload-ownership-") as temporary:
        parent = Path(temporary)
        parent.chmod(0o755)
        root = parent / "workspace"
        root.mkdir()
        os.chown(root, 1000, 1000)
        yield root


def upload_as_agent(root: Path, name: str, offset: int, data: bytes, done=False):
    payload = {
        "root": str(root),
        "helper": str(Path(secure_files.__file__).resolve()),
        "request": {
            "action": "fs_upload_chunk",
            "params": {
                "path": name,
                "offset": offset,
                "chunk_base64": base64.b64encode(data).decode(),
                "done": done,
            },
        },
    }
    process = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import json,runpy,sys; p=json.load(sys.stdin); "
            "h=runpy.run_path(p['helper']); "
            "print(json.dumps(h['execute'](p['request'],p['root'])))",
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        user=1000,
        group=1000,
        extra_groups=[],
        check=True,
        timeout=5,
    )
    return json.loads(process.stdout)


@pytest.mark.parametrize("directory", setup.MANAGED_UPLOAD_DIRECTORIES)
def test_legacy_root_directory_repaired_and_real_agent_upload_succeeds(
    workspace, directory
):
    target = workspace / directory
    target.mkdir(mode=0o755)
    uploaded = target / "report.pdf"
    uploaded.write_bytes(b"legacy")
    nested_project = target / "user-project"
    nested_project.mkdir()
    unrelated = workspace / "other-project"
    unrelated.mkdir()
    before = upload_as_agent(workspace, f"{directory}/report.pdf", 0, b"new ")
    assert before["status"] == 400

    result = setup.repair_upload_directories(workspace)

    assert result["ok"] is True
    assert result["directories"][directory]["status"] == "repaired"
    assert (target.stat().st_uid, target.stat().st_gid) == (1000, 1000)
    assert target.stat().st_mode & 0o777 == 0o755
    # The migration touches no file or project-directory ownership/content.
    assert uploaded.stat().st_uid == nested_project.stat().st_uid == 0
    assert unrelated.stat().st_uid == 0
    assert uploaded.read_bytes() == b"legacy"
    assert (
        upload_as_agent(workspace, f"{directory}/report.pdf", 0, b"new ")[
            "bytes_written"
        ]
        == 4
    )
    assert (
        upload_as_agent(workspace, f"{directory}/report.pdf", 4, b"report")[
            "bytes_written"
        ]
        == 6
    )
    assert (
        upload_as_agent(workspace, f"{directory}/report.pdf", 10, b"", True)["done"]
        is True
    )
    assert uploaded.read_bytes() == b"new report"


def test_new_office_upload_directories_are_created_and_setup_is_idempotent(workspace):
    assert setup.repair_upload_directories(workspace) == {
        "ok": True,
        "directories": {name: {"status": "created"} for name in ("inbox", "source")},
    }
    assert setup.repair_upload_directories(workspace) == {
        "ok": True,
        "directories": {name: {"status": "ready"} for name in ("inbox", "source")},
    }
    for name in ("inbox", "source"):
        assert (workspace / name).stat().st_uid == 1000
        assert (
            upload_as_agent(workspace, f"{name}/empty.txt", 0, b"", True)["total_size"]
            == 0
        )


def test_symlink_upload_directory_never_changes_its_target(workspace):
    outside = workspace.parent / "other-office"
    outside.mkdir(mode=0o700)
    (outside / "sentinel").write_bytes(b"private")
    (workspace / "inbox").symlink_to(outside, target_is_directory=True)

    result = setup.repair_upload_directories(workspace)

    assert result["ok"] is False
    assert result["directories"]["inbox"]["status"] == "error"
    assert result["directories"]["source"]["status"] == "created"
    assert outside.stat().st_uid == 0
    assert outside.stat().st_mode & 0o777 == 0o700
    assert (outside / "sentinel").read_bytes() == b"private"
    assert (workspace / "inbox").is_symlink()


def test_symlink_in_workspace_ancestors_is_rejected(workspace):
    alias = workspace.parent / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    with pytest.raises(OSError):
        setup.repair_upload_directories(alias)
    assert list(workspace.iterdir()) == []


def test_non_directory_and_custom_owner_are_preserved(workspace):
    (workspace / "inbox").write_bytes(b"user file")
    (workspace / "source").mkdir()
    os.chown(workspace / "source", 1234, 1234)

    result = setup.repair_upload_directories(workspace)

    assert result["ok"] is False
    assert result["directories"]["inbox"]["status"] == "error"
    assert result["directories"]["source"] == {
        "status": "skipped",
        "reason": "custom owner",
    }
    assert (workspace / "inbox").read_bytes() == b"user file"
    assert (workspace / "source").stat().st_uid == 1234


def test_same_device_nested_mount_identity_is_rejected(workspace, monkeypatch):
    (workspace / "inbox").mkdir()
    inode = (workspace / "inbox").stat().st_ino
    original = setup._mount_id
    monkeypatch.setattr(
        setup,
        "_mount_id",
        lambda descriptor: (
            "nested-mount"
            if os.fstat(descriptor).st_ino == inode
            else original(descriptor)
        ),
    )
    result = setup.repair_upload_directories(workspace)
    assert result["ok"] is False
    assert result["directories"]["inbox"]["status"] == "error"
    assert (workspace / "inbox").stat().st_uid == 0


def test_real_linux_submount_is_rejected_without_mutation(workspace, monkeypatch):
    parent, child = Path("/dev"), Path("/dev/shm")
    if not child.is_dir():
        pytest.skip("This isolated Linux runner has no /dev/shm submount")
    with_parent = os.open(parent, setup._DIRECTORY_FLAGS)
    with_child = os.open(child, setup._DIRECTORY_FLAGS)
    try:
        if setup._mount_id(with_parent) == setup._mount_id(with_child):
            pytest.skip("/dev/shm is not a distinct mount on this runner")
    finally:
        os.close(with_parent)
        os.close(with_child)
    before = child.stat()
    monkeypatch.setattr(setup, "MANAGED_UPLOAD_DIRECTORIES", ("shm",))

    assert setup.repair_upload_directories(parent)["ok"] is False

    after = child.stat()
    assert (before.st_uid, before.st_gid, before.st_mode) == (
        after.st_uid,
        after.st_gid,
        after.st_mode,
    )


def test_active_files_operation_prevents_repair(workspace):
    with secure_files.SecureWorkspace(workspace):
        with pytest.raises(BlockingIOError):
            setup.repair_upload_directories(workspace)
    assert list(workspace.iterdir()) == []


def test_startup_uses_isolated_daemon_owned_code_in_selected_container():
    from src._chown import AGENT_GID, AGENT_UID
    from src.docker.container_manager import _ensure_bind_mount_ownership

    assert (setup.AGENT_UID, setup.AGENT_GID) == (AGENT_UID, AGENT_GID)
    container = Mock()
    container.exec_run.return_value = SimpleNamespace(exit_code=0, output=b"")

    _ensure_bind_mount_ownership(container, "selected-office")

    command = container.exec_run.call_args
    assert command.args[0] == setup.container_setup_command()
    assert command.args[0][:4] == ["/usr/local/bin/python3", "-I", "-S", "-c"]
    assert command.kwargs == {"user": "0", "workdir": "/"}
    compile(command.args[0][4], "<upload-directory-setup>", "exec")


def test_startup_reports_refused_directory_repair(caplog):
    from src.docker.container_manager import _ensure_bind_mount_ownership

    container = Mock()
    container.exec_run.return_value = SimpleNamespace(exit_code=1, output=b"")
    _ensure_bind_mount_ownership(container, "selected-office")
    assert "managed upload directory setup was refused" in caplog.text
