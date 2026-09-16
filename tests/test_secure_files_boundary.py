"""Adversarial checks of the actual image helper on synthetic Linux roots."""

import base64
import ctypes
import errno
import io
import os
import stat
import zipfile

import pytest

from src._agent_image import secure_files as files


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "outputs").mkdir()
    (root / "outputs" / "report.txt").write_text("public report")
    return root


def request(root, action, **params):
    return files.execute({"action": action, "params": params}, root)


@pytest.mark.parametrize(
    "path",
    [
        ".claude-auth/.credentials.json",
        "ssh-keys/id_ed25519",
        ".cubicle/state.json",
        ".scripts/demo/.secrets.json",
        ".scripts/demo/.secrets.json.corrupt",
        ".claude/settings.json",
        ".claude.json",
        ".mcp.json",
        ".env",
        ".ssh/config",
        "outputs/../.claude-auth/.credentials.json",
        r".scripts\demo\.secrets.json",
    ],
)
@pytest.mark.parametrize(
    "action",
    [
        "fs_read",
        "fs_stat",
        "fs_download",
        "fs_download_chunk",
        "fs_write",
        "fs_upload_chunk",
        "fs_delete",
        "fs_mkdir",
        "fs_download_zip",
    ],
)
def test_all_operations_reject_runtime_paths(workspace, path, action):
    result = request(
        workspace,
        action,
        path=path,
        offset=0,
        length=1,
        content="replacement",
        chunk_base64="eA==",
    )
    assert result["status"] == 400
    assert "content" not in result and "content_base64" not in result


@pytest.mark.parametrize("action", ["fs_rename", "fs_delete"])
def test_mutating_ancestor_of_secret_fails_before_any_change(workspace, action):
    folder = workspace / ".scripts" / "demo"
    folder.mkdir(parents=True)
    (folder / "main.py").write_text("print('public')")
    (folder / ".secrets.json").write_text("PRIVATE-SENTINEL")
    result = request(
        workspace,
        action,
        path=".scripts/demo",
        old_path=".scripts/demo",
        new_path="public-copy",
    )
    assert result["status"] == 400
    assert (folder / "main.py").read_text() == "print('public')"
    assert (folder / ".secrets.json").read_text() == "PRIVATE-SENTINEL"
    assert not (workspace / "public-copy").exists()


@pytest.mark.parametrize(
    "path", [".claude", ".scripts/demo/.secrets.json", "ssh-keys", ".claude-auth"]
)
def test_protected_tree_roots_and_rename_destinations_rejected(workspace, path):
    assert request(workspace, "fs_tree", subfolder=path)["status"] == 400
    assert (
        request(workspace, "fs_rename", old_path="outputs/report.txt", new_path=path)[
            "status"
        ]
        == 400
    )
    assert (workspace / "outputs" / "report.txt").read_text() == "public report"


def test_script_and_skill_code_remain_usable_but_archive_omits_secrets(workspace):
    for path in [".scripts/demo/main.py", ".claude/skills/demo/SKILL.md"]:
        assert (
            request(workspace, "fs_write", path=path, content="public code")["size"]
            == 11
        )
        assert request(workspace, "fs_read", path=path)["content"] == "public code"
    (workspace / ".scripts/demo/.secrets.json").write_text("PRIVATE-SENTINEL")
    archive = request(workspace, "fs_download_zip", path=".scripts/demo")
    with zipfile.ZipFile(
        io.BytesIO(base64.b64decode(archive["content_base64"]))
    ) as opened:
        assert opened.namelist() == ["main.py"]
        assert opened.read("main.py") == b"public code"
    assert request(workspace, "fs_list_skills")["skills"][0]["name"] == "demo"


@pytest.mark.parametrize(
    "name",
    [
        ".credentials.json.backup",
        ".claude.json.backup",
        ".mcp.json.backup",
        ".secrets.json.corrupt.backup",
        ".npmrc.backup",
        ".pypirc.backup",
        ".netrc.backup",
        ".env.production.local",
        ".env.staging",
        ".env.example.backup",
        ".env.sample.backup",
    ],
)
def test_runtime_backup_families_are_denied_and_omitted_from_zip(workspace, name):
    protected = workspace / "outputs" / name
    protected.write_text("PRIVATE-SENTINEL")
    relative_path = f"outputs/{name}"
    for action in [
        "fs_read",
        "fs_stat",
        "fs_download",
        "fs_download_chunk",
        "fs_write",
        "fs_upload_chunk",
        "fs_delete",
        "fs_mkdir",
        "fs_tree",
        "fs_download_zip",
        "fs_rename",
    ]:
        result = request(
            workspace,
            action,
            path=relative_path,
            subfolder=relative_path,
            old_path=relative_path,
            new_path="outputs/renamed.txt",
            content="replacement",
            chunk_base64="eA==",
            offset=0,
            length=1,
        )
        assert result["status"] == 400
        assert "PRIVATE-SENTINEL" not in str(result)
        assert protected.read_text() == "PRIVATE-SENTINEL"
    archive = request(workspace, "fs_download_zip", path="outputs")
    with zipfile.ZipFile(
        io.BytesIO(base64.b64decode(archive["content_base64"]))
    ) as opened:
        assert opened.namelist() == ["report.txt"]
        assert opened.read("report.txt") == b"public report"
    for action in ["fs_rename", "fs_delete"]:
        result = request(
            workspace,
            action,
            path="outputs",
            old_path="outputs",
            new_path="renamed-outputs",
        )
        assert result["status"] == 400
        assert protected.read_text() == "PRIVATE-SENTINEL"
        assert (workspace / "outputs/report.txt").read_text() == "public report"


@pytest.mark.parametrize("name", [".env.example", ".env.sample"])
def test_explicit_environment_templates_remain_public(workspace, name):
    assert request(workspace, "fs_write", path=name, content="KEY=")["size"] == 4
    downloaded = request(workspace, "fs_download", path=name)
    assert base64.b64decode(downloaded["content_base64"]) == b"KEY="


@pytest.mark.parametrize("kind", ["external", "internal", "dangling", "directory"])
@pytest.mark.parametrize(
    "action",
    [
        "fs_read",
        "fs_stat",
        "fs_download",
        "fs_download_chunk",
        "fs_write",
        "fs_upload_chunk",
        "fs_delete",
        "fs_rename",
    ],
)
def test_leaf_links_never_follow_or_mutate_targets(workspace, tmp_path, kind, action):
    secret = tmp_path / "other-office.txt"
    secret.write_text("EXTERNAL-SENTINEL")
    targets = {
        "external": secret,
        "internal": workspace / "outputs/report.txt",
        "dangling": tmp_path / "missing",
        "directory": tmp_path,
    }
    (workspace / "alias").symlink_to(targets[kind])
    result = request(
        workspace,
        action,
        path="alias",
        old_path="alias",
        new_path="moved",
        content="replace",
        offset=0,
        length=1,
        chunk_base64="eA==",
    )
    assert result["status"] == 400
    assert secret.read_text() == "EXTERNAL-SENTINEL"
    assert (workspace / "outputs/report.txt").read_text() == "public report"
    assert (workspace / "alias").is_symlink()


@pytest.mark.parametrize("action", ["fs_download_zip", "fs_list_skills"])
def test_recursive_read_rejects_symlink_descendant_without_returning_partial_content(
    workspace, tmp_path, action
):
    secret = tmp_path / "other-office.txt"
    secret.write_text("EXTERNAL-SENTINEL")
    folder = (
        workspace / ".claude/skills/demo"
        if action == "fs_list_skills"
        else workspace / "outputs"
    )
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "leak.txt").symlink_to(secret)
    result = request(workspace, action, path="outputs", subfolder="outputs")
    assert result["status"] == 400
    assert "EXTERNAL-SENTINEL" not in str(result)
    assert "content_base64" not in result


def test_symlinked_roots_and_parent_components_rejected(workspace, tmp_path):
    alias = tmp_path / "root-alias"
    alias.symlink_to(workspace, target_is_directory=True)
    assert request(alias, "fs_read", path="outputs/report.txt")["status"] == 400
    (workspace / "parent-alias").symlink_to(
        workspace / "outputs", target_is_directory=True
    )
    assert (
        request(workspace, "fs_read", path="parent-alias/report.txt")["status"] == 400
    )
    assert request(workspace, "fs_download_zip", path="parent-alias")["status"] == 400
    assert request(workspace, "fs_tree", subfolder="parent-alias")["status"] == 400
    assert request(alias, "fs_tree")["status"] == 400


@pytest.mark.parametrize(
    "kind", ["external", "internal", "dangling", "directory", "hardlink", "fifo"]
)
def test_tree_omits_unsupported_entries_and_preserves_healthy_siblings(
    workspace, tmp_path, kind
):
    secret = tmp_path / "private.txt"
    secret.write_text("EXTERNAL-SENTINEL")
    target = workspace / "outputs/unsupported"
    if kind == "hardlink":
        os.link(secret, target)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.symlink_to(
            {
                "external": secret,
                "internal": workspace / "outputs/report.txt",
                "dangling": tmp_path / "missing",
                "directory": tmp_path,
            }[kind]
        )
    result = request(workspace, "fs_tree", subfolder="outputs")
    assert [child["name"] for child in result["children"]] == ["report.txt"]
    assert result["skipped_entries"] == 1
    assert "EXTERNAL-SENTINEL" not in str(result)
    assert secret.read_text() == "EXTERNAL-SENTINEL"


@pytest.mark.parametrize("kind", ["leaf_link", "directory_link", "hardlink"])
def test_tree_omits_entry_changed_between_stat_and_open(
    workspace, tmp_path, monkeypatch, kind
):
    external = tmp_path / "external"
    external.mkdir()
    secret = external / "private.txt"
    secret.write_text("EXTERNAL-SENTINEL")
    target = workspace / "outputs/racing"
    if kind == "directory_link":
        target.mkdir()
    else:
        target.write_text("initial")
    original_open = os.open
    swapped = False

    def swap(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "racing" and not swapped and "dir_fd" in kwargs:
            swapped = True
            if kind == "directory_link":
                target.rmdir()
                target.symlink_to(external, target_is_directory=True)
            else:
                target.unlink()
                if kind == "hardlink":
                    os.link(secret, target)
                else:
                    target.symlink_to(secret)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(files.os, "open", swap)
    result = request(workspace, "fs_tree", subfolder="outputs")
    assert swapped
    assert [child["name"] for child in result["children"]] == ["report.txt"]
    assert result["skipped_entries"] == 1
    assert "private.txt" not in str(result) and "EXTERNAL-SENTINEL" not in str(result)


@pytest.mark.parametrize("operation", ["stat", "open"])
@pytest.mark.parametrize(
    "error_code", [errno.ENOENT, errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM]
)
def test_tree_skips_only_unavailable_child_and_keeps_siblings(
    workspace, monkeypatch, operation, error_code
):
    (workspace / "outputs/unavailable.txt").write_text("unavailable")
    original = getattr(os, operation)

    def fail_child(path, *args, **kwargs):
        if path == "unavailable.txt" and "dir_fd" in kwargs:
            raise OSError(error_code, "synthetic child failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(files.os, operation, fail_child)
    result = request(workspace, "fs_tree", subfolder="outputs")
    assert [child["name"] for child in result["children"]] == ["report.txt"]
    assert result["skipped_entries"] == 1


@pytest.mark.parametrize("error_code", [errno.EIO, errno.EMFILE, errno.ENFILE])
def test_tree_systemic_child_error_returns_no_partial_tree(
    workspace, monkeypatch, error_code
):
    (workspace / "outputs/z-failed.txt").write_text("unavailable")
    original = os.open

    def fail_child(path, *args, **kwargs):
        if path == "z-failed.txt" and "dir_fd" in kwargs:
            raise OSError(error_code, "synthetic systemic failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(files.os, "open", fail_child)
    result = request(workspace, "fs_tree", subfolder="outputs")
    assert result["status"] == 400
    assert "children" not in result and "skipped_entries" not in result


def test_tree_root_replacement_during_child_read_never_becomes_omission(
    workspace, tmp_path, monkeypatch
):
    original_open = os.open
    replaced = False

    def replace_root(path, flags, *args, **kwargs):
        nonlocal replaced
        if path == "report.txt" and not replaced and "dir_fd" in kwargs:
            replaced = True
            workspace.rename(tmp_path / "old-root")
            workspace.mkdir()
            raise FileNotFoundError(errno.ENOENT, "synthetic disappearance")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(files.os, "open", replace_root)
    result = request(workspace, "fs_tree")
    assert replaced and result["status"] == 400
    assert "root changed" in result["error"] and "children" not in result


def test_tree_does_not_hide_missing_mount_identity_support(workspace, monkeypatch):
    original = files._mount_id

    def unavailable(descriptor):
        if os.readlink(f"/proc/self/fd/{descriptor}").endswith("/report.txt"):
            raise files.FilesPolicyError(
                "Secure Files requires Linux mount identity support"
            )
        return original(descriptor)

    monkeypatch.setattr(files, "_mount_id", unavailable)
    result = request(workspace, "fs_tree", subfolder="outputs")
    assert result["status"] == 400 and "mount identity support" in result["error"]
    assert "children" not in result


def test_tree_expired_deadline_never_returns_partial_success(workspace):
    with files.SecureWorkspace(workspace) as selected:
        selected.deadline = 0
        with pytest.raises(TimeoutError):
            selected._tree({})
        assert selected.entry_limit == files.MAX_ENTRIES


def test_tree_metadata_budget_allows_more_than_two_thousand_entries(workspace):
    for index in range(2100):
        (workspace / "outputs" / f"item-{index}.txt").touch()
    result = request(workspace, "fs_tree", subfolder="outputs")
    assert len(result["children"]) == 2101
    assert "skipped_entries" not in result
    # Content exports retain their stricter budget.
    archive = request(workspace, "fs_download_zip", path="outputs")
    assert archive["status"] == 400 and "2000-entry limit" in archive["error"]


def test_tree_above_ten_thousand_entries_returns_explicit_error_without_partial_tree(
    workspace,
):
    for index in range(10_001):
        (workspace / "outputs" / f"item-{index}.txt").touch()
    result = request(workspace, "fs_tree", subfolder="outputs")
    assert result["status"] == 400 and "10000-entry limit" in result["error"]
    assert "children" not in result and "skipped_entries" not in result


@pytest.mark.parametrize("fail", [False, True])
def test_tree_budget_is_restored_before_other_operations(workspace, monkeypatch, fail):
    with files.SecureWorkspace(workspace) as selected:
        if fail:
            monkeypatch.setattr(files, "MAX_TREE_ENTRIES", 1)
            with pytest.raises(files.FilesPolicyError, match="entry limit"):
                selected._tree({})
        else:
            selected._tree({})
        assert selected.entry_limit == files.MAX_ENTRIES == 2000
        # Reusing this workspace for a content/mutation traversal remains strict.
        selected.entries = 2000
        with pytest.raises(files.FilesPolicyError, match="2000-entry limit"):
            selected._delete({"path": "outputs"})
        assert (workspace / "outputs/report.txt").read_text() == "public report"


@pytest.mark.parametrize("special", ["hardlink", "fifo"])
def test_special_and_multilink_files_are_rejected_without_blocking(
    workspace, tmp_path, special
):
    target = workspace / "outputs/special"
    if special == "hardlink":
        secret = tmp_path / "external-secret"
        secret.write_text("EXTERNAL-SENTINEL")
        os.link(secret, target)
    else:
        os.mkfifo(target)
    for action in [
        "fs_read",
        "fs_stat",
        "fs_download",
        "fs_delete",
        "fs_write",
        "fs_upload_chunk",
    ]:
        assert (
            request(
                workspace,
                action,
                path="outputs/special",
                content="replace",
                offset=0,
                chunk_base64="eA==",
            )["status"]
            == 400
        )
    assert request(workspace, "fs_download_zip", path="outputs")["status"] == 400
    if special == "hardlink":
        assert secret.read_text() == "EXTERNAL-SENTINEL"


@pytest.mark.parametrize(
    "action", ["fs_read", "fs_write", "fs_upload_chunk", "fs_download_zip"]
)
def test_swap_after_stat_before_leaf_open_is_rejected(
    workspace, tmp_path, monkeypatch, action
):
    secret = tmp_path / "secret"
    secret.write_text("EXTERNAL-SENTINEL")
    target = workspace / "outputs/report.txt"
    original_open = os.open
    swapped = False

    def swapping_open(path, flags, *arguments, **keywords):
        nonlocal swapped
        if path == "report.txt" and not swapped and "dir_fd" in keywords:
            swapped = True
            target.unlink()
            target.symlink_to(secret)
        return original_open(path, flags, *arguments, **keywords)

    monkeypatch.setattr(files.os, "open", swapping_open)
    result = request(
        workspace,
        action,
        path="outputs" if action == "fs_download_zip" else "outputs/report.txt",
        content="replace",
        offset=0,
        chunk_base64="eA==",
    )
    assert swapped
    assert result["status"] == 400
    assert secret.read_text() == "EXTERNAL-SENTINEL"
    assert "EXTERNAL-SENTINEL" not in str(result)


def test_parent_swap_cannot_redirect_descriptor_read_to_external_tree(
    workspace, tmp_path, monkeypatch
):
    external = tmp_path / "other-office"
    external.mkdir()
    (external / "report.txt").write_text("EXTERNAL-SENTINEL")
    original_open = os.open
    swapped = False

    def swapping_open(path, flags, *arguments, **keywords):
        nonlocal swapped
        if path == "outputs" and not swapped and "dir_fd" in keywords:
            swapped = True
            (workspace / "outputs").rename(workspace / "original")
            (workspace / "outputs").symlink_to(external, target_is_directory=True)
        return original_open(path, flags, *arguments, **keywords)

    monkeypatch.setattr(files.os, "open", swapping_open)
    result = request(workspace, "fs_read", path="outputs/report.txt")
    assert swapped and result["status"] == 400
    assert "EXTERNAL-SENTINEL" not in str(result)


def test_replaced_workspace_root_rejected_before_open(workspace, tmp_path):
    with files.SecureWorkspace(workspace) as selected:
        workspace.rename(tmp_path / "old")
        workspace.mkdir()
        (workspace / "secret.txt").write_text("REPLACEMENT-SENTINEL")
        with pytest.raises(ValueError, match="root changed"):
            selected.read_bytes("secret.txt")


def test_mutating_during_read_fails_instead_of_returning_unstable_evidence(
    workspace, monkeypatch
):
    original_read = os.read
    mutated = False

    def mutate(descriptor, size):
        nonlocal mutated
        content = original_read(descriptor, size)
        if not mutated:
            mutated = True
            (workspace / "outputs/report.txt").write_text("changed")
        return content

    monkeypatch.setattr(files.os, "read", mutate)
    result = request(workspace, "fs_read", path="outputs/report.txt")
    assert result["status"] == 400


@pytest.mark.parametrize(
    "limit", ["MAX_ENTRIES", "MAX_ZIP_INPUT_BYTES", "MAX_ZIP_BYTES"]
)
def test_zip_limits_fail_without_partial_archive(workspace, monkeypatch, limit):
    (workspace / "outputs/second.txt").write_text("another")
    monkeypatch.setattr(files, limit, 1)
    assert request(workspace, "fs_download_zip", path="outputs")["status"] == 400


def test_deadline_and_read_size_limits_are_enforced(workspace):
    with files.SecureWorkspace(workspace) as selected:
        with pytest.raises(ValueError, match="read limit"):
            selected.read_bytes("outputs/report.txt", limit=1)
        selected.deadline = 0
        with pytest.raises(TimeoutError):
            selected.read_bytes("outputs/report.txt")


def test_mount_identity_mismatch_fails_closed(workspace, monkeypatch):
    with files.SecureWorkspace(workspace) as selected:
        monkeypatch.setattr(files, "_mount_id", lambda _descriptor: "other-mount")
        with pytest.raises(ValueError, match="Mounted"):
            selected.read_bytes("outputs/report.txt")


def test_kernel_lock_holds_across_helper_instances_and_is_released(workspace):
    with files.SecureWorkspace(workspace):
        assert request(workspace, "fs_download_zip", path="outputs")["status"] == 429
    assert "content_base64" in request(workspace, "fs_download_zip", path="outputs")


def test_ordinary_mutations_are_descriptor_relative_and_root_cannot_be_removed(
    workspace,
):
    assert request(workspace, "fs_mkdir", path="new/nested") == {"path": "new/nested"}
    result = request(
        workspace, "fs_rename", old_path="outputs/report.txt", new_path="new/report.txt"
    )
    if result.get("status") == 501:
        assert "unsupported" in result["error"]
        assert (workspace / "outputs/report.txt").read_text() == "public report"
        assert not (workspace / "new/report.txt").exists()
    else:
        assert result["new_path"] == "new/report.txt"
        assert (workspace / "new/report.txt").read_text() == "public report"
    assert request(workspace, "fs_delete", path="new") == {"path": "new"}
    assert not (workspace / "new").exists()
    assert request(workspace, "fs_delete", path="")["status"] == 400
    assert stat.S_ISDIR(workspace.stat().st_mode)


@pytest.mark.parametrize(
    "error_code, expected",
    [
        (errno.EEXIST, 400),
        (errno.EINVAL, 501),
        (errno.ENOSYS, 501),
        (errno.EOPNOTSUPP, 501),
    ],
)
def test_native_no_clobber_rename_errors_are_explicit_and_preserve_source(
    workspace, monkeypatch, error_code, expected
):
    class NativeRename:
        def __call__(
            self, source_parent, source, destination_parent, destination, flags
        ):
            assert flags == 1
            ctypes.set_errno(error_code)
            return -1

    class NativeLibrary:
        renameat2 = NativeRename()

    monkeypatch.setattr(
        files.ctypes, "CDLL", lambda *_arguments, **_keywords: NativeLibrary()
    )
    result = request(
        workspace, "fs_rename", old_path="outputs/report.txt", new_path="renamed.txt"
    )
    assert result["status"] == expected
    assert (workspace / "outputs/report.txt").read_text() == "public report"
    assert not (workspace / "renamed.txt").exists()


def test_native_rename_success_uses_verified_parent_descriptors(workspace, monkeypatch):
    class NativeRename:
        def __call__(
            self, source_parent, source, destination_parent, destination, flags
        ):
            assert flags == 1
            assert (
                os.stat(source, dir_fd=source_parent, follow_symlinks=False).st_nlink
                == 1
            )
            os.rename(
                source,
                destination,
                src_dir_fd=source_parent,
                dst_dir_fd=destination_parent,
            )
            return 0

    class NativeLibrary:
        renameat2 = NativeRename()

    monkeypatch.setattr(
        files.ctypes, "CDLL", lambda *_arguments, **_keywords: NativeLibrary()
    )
    result = request(
        workspace, "fs_rename", old_path="outputs/report.txt", new_path="renamed.txt"
    )
    assert result == {"old_path": "outputs/report.txt", "new_path": "renamed.txt"}
    assert (workspace / "renamed.txt").read_text() == "public report"
