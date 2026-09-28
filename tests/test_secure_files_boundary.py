"""Adversarial checks of the actual image helper on synthetic Linux roots."""

import base64
import ctypes
import errno
import io
import json
import os
import random
import stat
import time
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


# ``fs_tree`` failure modes hold in both the strict and the lazy tree.
TREE_MODES = pytest.mark.parametrize(
    "mode", [{}, {"lazy": True}], ids=["strict", "lazy"]
)


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
        "fs_hash",
        "fs_write_revision",
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


@TREE_MODES
@pytest.mark.parametrize(
    "path", [".claude", ".scripts/demo/.secrets.json", "ssh-keys", ".claude-auth"]
)
def test_protected_tree_roots_and_rename_destinations_rejected(workspace, path, mode):
    assert request(workspace, "fs_tree", subfolder=path, **mode)["status"] == 400
    assert (
        request(workspace, "fs_rename", old_path="outputs/report.txt", new_path=path)[
            "status"
        ]
        == 400
    )
    assert (workspace / "outputs" / "report.txt").read_text() == "public report"


# A workspace on a case-insensitive host filesystem (macOS APFS through a
# Docker Desktop bind) resolves these names to the protected entries, so the
# policy must refuse them too. The test root is case-sensitive, so each
# variant is created literally: a refusal here is the policy, not the host.
_CASE_VARIANT_TREE = {
    ".CLAUDE/settings.json": b"SETTINGS-SENTINEL",
    ".CLAUDE/CLAUDE.md": b"PLAYBOOK-SENTINEL",
    ".CLAUDE/skills/demo/SKILL.md": b"SKILL-SENTINEL",
    ".scripts/x/.SECRETS.JSON": b"SECRET-SENTINEL",
    ".scripts/y/.Secrets.json": b"SECRET-SENTINEL",
    ".Env": b"ENV-SENTINEL",
    ".GIT/config": b"GIT-SENTINEL",
    "agents/x/.Claude/settings.json": b"HOOK-SENTINEL",
}


def _case_variant_workspace(root):
    for relative, data in _CASE_VARIANT_TREE.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(data)


def _case_variant_tree_intact(root):
    return all(
        (root / relative).read_bytes() == data
        for relative, data in _CASE_VARIANT_TREE.items()
    )


@pytest.mark.parametrize(
    "path",
    [
        ".CLAUDE",
        ".CLAUDE/settings.json",
        ".CLAUDE/CLAUDE.md",
        ".scripts/x/.SECRETS.JSON",
        ".scripts/y/.Secrets.json",
        ".Env",
        ".GIT/config",
        "agents/x/.Claude/settings.json",
    ],
)
@pytest.mark.parametrize(
    "action",
    [
        "fs_read",
        "fs_stat",
        "fs_download",
        "fs_hash",
        "fs_write",
        "fs_write_revision",
        "fs_upload_chunk",
        "fs_delete",
        "fs_mkdir",
        "fs_download_zip",
    ],
)
def test_case_variants_of_protected_names_are_refused(workspace, path, action):
    _case_variant_workspace(workspace)
    result = request(
        workspace,
        action,
        path=path,
        offset=0,
        length=1,
        content="replacement",
        chunk_base64="eA==",
    )
    assert result["status"] == 400, result
    assert "content" not in result and "content_base64" not in result
    assert _case_variant_tree_intact(workspace)


@pytest.mark.parametrize("path", [".CLAUDE", ".Env", ".scripts/x/.SECRETS.JSON"])
def test_case_variants_cannot_be_renamed_away(workspace, path):
    _case_variant_workspace(workspace)
    result = request(workspace, "fs_rename", old_path=path, new_path="staging")
    assert result["status"] == 400
    assert not (workspace / "staging").exists()
    assert _case_variant_tree_intact(workspace)


@pytest.mark.parametrize(
    "destination",
    [".ENV", ".Git", ".scripts/z/.SECRETS.JSON", ".CLAUDE/rules.md", ".SSH"],
)
def test_case_variant_destinations_are_refused(workspace, destination):
    result = request(
        workspace, "fs_rename", old_path="outputs/report.txt", new_path=destination
    )
    assert result["status"] == 400
    assert (workspace / "outputs" / "report.txt").read_text() == "public report"
    assert not (workspace / destination).exists()


def test_case_variant_skill_path_stays_allowed(workspace):
    """``.Claude/SKILLS`` IS the skills folder on a case-insensitive host."""
    playbook = workspace / ".Claude" / "SKILLS" / "demo" / "SKILL.md"
    playbook.parent.mkdir(parents=True)
    playbook.write_text("# demo")
    result = request(workspace, "fs_read", path=".Claude/SKILLS/demo/SKILL.md")
    assert result.get("content") == "# demo", result


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
    lazy = request(workspace, "fs_tree", subfolder=relative_path, lazy=True)
    assert lazy["status"] == 400 and "PRIVATE-SENTINEL" not in str(lazy)
    listed = request(workspace, "fs_tree", subfolder="outputs", lazy=True)
    assert [child["name"] for child in listed["children"]] == ["report.txt"]
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
    if action == "fs_list_skills":
        # Discovery lists without following the link: it is skipped and
        # counted (R3-DISC-LINK), never read, so nothing leaks.
        [skill] = result["skills"]
        assert skill["skipped_entries"] == 1
        assert [item["name"] for item in skill["files"]] == []
    else:
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
    lazy = {"lazy": True}
    assert (
        request(workspace, "fs_tree", subfolder="parent-alias", **lazy)["status"] == 400
    )
    assert request(alias, "fs_tree", **lazy)["status"] == 400


@TREE_MODES
@pytest.mark.parametrize(
    "kind", ["external", "internal", "dangling", "directory", "hardlink", "fifo"]
)
def test_tree_omits_unsupported_entries_and_preserves_healthy_siblings(
    workspace, tmp_path, kind, mode
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
    result = request(workspace, "fs_tree", subfolder="outputs", **mode)
    assert [child["name"] for child in result["children"]] == ["report.txt"]
    assert result["skipped_entries"] == 1
    assert "partial" not in result
    assert "EXTERNAL-SENTINEL" not in str(result)
    assert secret.read_text() == "EXTERNAL-SENTINEL"


@TREE_MODES
@pytest.mark.parametrize("kind", ["leaf_link", "directory_link", "hardlink"])
def test_tree_omits_entry_changed_between_stat_and_open(
    workspace, tmp_path, monkeypatch, kind, mode
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
    result = request(workspace, "fs_tree", subfolder="outputs", **mode)
    assert swapped
    assert [child["name"] for child in result["children"]] == ["report.txt"]
    assert result["skipped_entries"] == 1
    assert "private.txt" not in str(result) and "EXTERNAL-SENTINEL" not in str(result)


@TREE_MODES
@pytest.mark.parametrize("operation", ["stat", "open"])
@pytest.mark.parametrize(
    "error_code", [errno.ENOENT, errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM]
)
def test_tree_skips_only_unavailable_child_and_keeps_siblings(
    workspace, monkeypatch, operation, error_code, mode
):
    (workspace / "outputs/unavailable.txt").write_text("unavailable")
    original = getattr(os, operation)

    def fail_child(path, *args, **kwargs):
        if path == "unavailable.txt" and "dir_fd" in kwargs:
            raise OSError(error_code, "synthetic child failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(files.os, operation, fail_child)
    result = request(workspace, "fs_tree", subfolder="outputs", **mode)
    assert [child["name"] for child in result["children"]] == ["report.txt"]
    assert result["skipped_entries"] == 1


@TREE_MODES
@pytest.mark.parametrize("error_code", [errno.EIO, errno.EMFILE, errno.ENFILE])
def test_tree_systemic_child_error_returns_no_partial_tree(
    workspace, monkeypatch, error_code, mode
):
    (workspace / "outputs/z-failed.txt").write_text("unavailable")
    original = os.open

    def fail_child(path, *args, **kwargs):
        if path == "z-failed.txt" and "dir_fd" in kwargs:
            raise OSError(error_code, "synthetic systemic failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(files.os, "open", fail_child)
    result = request(workspace, "fs_tree", subfolder="outputs", **mode)
    assert result["status"] == 400
    assert "children" not in result and "skipped_entries" not in result


@TREE_MODES
def test_tree_root_replacement_during_child_read_never_becomes_omission(
    workspace, tmp_path, monkeypatch, mode
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
    result = request(workspace, "fs_tree", **mode)
    assert replaced and result["status"] == 400
    assert "root changed" in result["error"] and "children" not in result


@TREE_MODES
def test_tree_does_not_hide_missing_mount_identity_support(
    workspace, monkeypatch, mode
):
    original = files._mount_id

    def unavailable(descriptor):
        if os.readlink(f"/proc/self/fd/{descriptor}").endswith("/report.txt"):
            raise files.FilesPolicyError(
                "Secure Files requires Linux mount identity support"
            )
        return original(descriptor)

    monkeypatch.setattr(files, "_mount_id", unavailable)
    result = request(workspace, "fs_tree", subfolder="outputs", **mode)
    assert result["status"] == 400 and "mount identity support" in result["error"]
    assert "children" not in result


@TREE_MODES
def test_tree_expired_deadline_never_returns_partial_success(workspace, mode):
    with files.SecureWorkspace(workspace) as selected:
        selected.deadline = 0
        with pytest.raises(TimeoutError):
            selected._tree(dict(mode))
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


# -- lazy tree (``fs_tree`` with ``lazy: true``, the Files page) ---------------


def _descendants(node):
    for child in node.get("children", []):
        yield child
        yield from _descendants(child)


def _child_names(node):
    return [child["name"] for child in node["children"]]


def _child(node, *names):
    for name in names:
        [node] = [child for child in node["children"] if child["name"] == name]
    return node


def _assert_unloaded(node):
    assert node["type"] == "folder"
    assert node["children_loaded"] is False
    assert node["children"] == [] and node["size"] == 0
    assert "truncated" not in node


def test_lazy_tree_above_ten_thousand_entries_never_fails_and_returns_partial(
    workspace,
):
    # The incident's shape: per-task build folders far past the strict
    # tree's 10,000-entry limit.
    for task in range(40):
        folder = workspace / "build" / f"t{task:02d}"
        folder.mkdir(parents=True)
        for index in range(260):
            (folder / f"chunk-{index}.js").touch()
    strict = request(workspace, "fs_tree")
    assert strict["status"] == 400 and "10000-entry limit" in strict["error"]

    result = request(workspace, "fs_tree", lazy=True)
    assert "error" not in result and result["root"] == "/workspace"
    assert result["partial"] is True
    nodes = list(_descendants(result))
    assert len(nodes) <= files.LAZY_TREE_ENTRIES
    unloaded = [node for node in nodes if "children_loaded" in node]
    assert unloaded
    for node in unloaded:
        _assert_unloaded(node)
    # Breadth first: the top levels are complete, the budget ends deeper.
    assert _child_names(result) == ["build", "outputs"]
    assert len(_child(result, "build")["children"]) == 40
    assert _child_names(_child(result, "outputs")) == ["report.txt"]
    listed = [node for node in _child(result, "build")["children"] if node["children"]]
    assert listed and all(len(node["children"]) == 260 for node in listed)
    assert all("children_loaded" not in node for node in listed)
    # An unloaded folder opens on demand, completely.
    opened = request(workspace, "fs_tree", subfolder=unloaded[-1]["path"], lazy=True)
    assert len(opened["children"]) == 260
    assert "partial" not in opened and "skipped_entries" not in opened


def test_lazy_tree_single_folder_above_ten_thousand_entries_is_truncated(workspace):
    for index in range(10_001):
        (workspace / "outputs" / f"item-{index:05d}.txt").touch()
    result = request(workspace, "fs_tree", subfolder="outputs", lazy=True)
    assert "error" not in result
    assert len(result["children"]) == files.LAZY_DIRECTORY_ENTRIES
    assert result["truncated"] is True and result["total_entries"] == 10_002
    assert result["partial"] is True
    assert _child_names(result)[:2] == ["item-00000.txt", "item-00001.txt"]


def test_lazy_tree_truncates_folder_past_directory_budget_in_tree_order(workspace):
    outputs = workspace / "outputs"
    file_names = [
        f"{'File' if index % 2 else 'file'}-{index:05d}.txt"
        for index in range(files.LAZY_DIRECTORY_ENTRIES)
    ]
    for name in file_names:
        (outputs / name).touch()
    folder_names = [
        f"{'Dir' if index % 3 else 'dir'}-{index:02d}" for index in range(40)
    ]
    for name in folder_names:
        (outputs / name).mkdir()
    # Not listable: never counted, or skipped and counted.
    (outputs / ".hidden").touch()
    (outputs / "node_modules").mkdir()
    (outputs / "alias").symlink_to(outputs / "report.txt")

    result = request(workspace, "fs_tree", subfolder="outputs", lazy=True)
    order = files._tree_order
    expected = sorted(folder_names, key=order) + sorted(
        [*file_names, "report.txt"], key=order
    )
    assert _child_names(result) == expected[: files.LAZY_DIRECTORY_ENTRIES]
    assert result["truncated"] is True
    assert result["total_entries"] == len(expected)
    assert result["partial"] is True and result["skipped_entries"] == 1
    # The listed (empty) folders are fully loaded, in the strict shape.
    for folder in result["children"][:40]:
        assert folder["type"] == "folder" and folder["children"] == []
        assert "children_loaded" not in folder and "truncated" not in folder


def test_lazy_tree_picks_the_first_names_from_a_large_scan(workspace, monkeypatch):
    """Names are kept in bounded memory while scanning; an entry that fails
    full validation (a hard-linked file) is replaced by the next one."""
    monkeypatch.setattr(files, "LAZY_DIRECTORY_ENTRIES", 3)
    outputs = workspace / "outputs"
    folder_names = [f"d{index:02d}" for index in range(30)]
    for name in folder_names:
        (outputs / name).mkdir()
    file_names = [f"f{index:02d}.txt" for index in range(30)]
    for name in file_names:
        (outputs / name).touch()
    result = request(workspace, "fs_tree", subfolder="outputs", lazy=True)
    assert _child_names(result) == ["d00", "d01", "d02"]
    assert result["truncated"] is True and result["total_entries"] == 61

    for name in folder_names:
        (outputs / name).rmdir()
    os.link(outputs / "f00.txt", outputs / "a-hardlink.txt")
    result = request(workspace, "fs_tree", subfolder="outputs", lazy=True)
    assert _child_names(result) == ["f01.txt", "f02.txt", "f03.txt"]
    # f00.txt has two links now: both names are skipped.
    assert result["skipped_entries"] == 2
    assert result["total_entries"] == 30  # 29 f-files and report.txt


def test_lazy_tree_marks_depth_limited_folders_unloaded(workspace):
    deep = workspace / "outputs" / "a" / "b" / "c" / "d" / "e" / "f"
    deep.mkdir(parents=True)
    (deep / "far.txt").write_text("far")
    strict = request(workspace, "fs_tree", subfolder="outputs")
    strict_e = _child(strict, "a", "b", "c", "d", "e")
    assert strict_e["children"] == [] and "children_loaded" not in strict_e
    assert "partial" not in strict

    result = request(workspace, "fs_tree", subfolder="outputs", lazy=True)
    assert _child(result, "a", "b", "c", "d")["children"]
    edge = _child(result, "a", "b", "c", "d", "e")
    _assert_unloaded(edge)
    assert result["partial"] is True

    opened = request(workspace, "fs_tree", subfolder=edge["path"], lazy=True)
    assert _child_names(opened) == ["f"]
    [far] = _child(opened, "f")["children"]
    assert far["path"] == "outputs/a/b/c/d/e/f/far.txt" and far["size"] == 3
    assert opened["size"] == 3 and "partial" not in opened


@pytest.mark.parametrize(
    "moment", ["between_folders", "during_scan", "during_validation"]
)
def test_lazy_tree_soft_time_budget_returns_partial(workspace, monkeypatch, moment):
    for name in ("a", "b"):
        folder = workspace / "outputs" / name
        folder.mkdir()
        for leaf in ("x.txt", "y.txt"):
            (folder / leaf).write_text("x")
    clock = [0.0]
    monkeypatch.setattr(files, "_lazy_clock", lambda: clock[0])

    def expire():
        clock[0] = float(files.LAZY_TREE_SECONDS)

    expand = files.SecureWorkspace._lazy_expand
    scan = files.SecureWorkspace._lazy_scan
    child = files.SecureWorkspace._lazy_child

    def expiring_expand(self, base, parts, node, relative, identity, depth, state):
        # After the requested folder is listed, before its first subfolder.
        if moment == "between_folders" and relative:
            expire()
        return expand(self, base, parts, node, relative, identity, depth, state)

    def expiring_scan(self, descriptor, current, keep, deadline):
        if moment == "during_scan" and current[-1] == "a":
            expire()
        return scan(self, descriptor, current, keep, deadline)

    def expiring_child(self, descriptor, current, name):
        if moment == "during_validation" and current[-1] == "a":
            expire()
        return child(self, descriptor, current, name)

    monkeypatch.setattr(files.SecureWorkspace, "_lazy_expand", expiring_expand)
    monkeypatch.setattr(files.SecureWorkspace, "_lazy_scan", expiring_scan)
    monkeypatch.setattr(files.SecureWorkspace, "_lazy_child", expiring_child)
    result = request(workspace, "fs_tree", subfolder="outputs", lazy=True)
    assert "error" not in result and result["partial"] is True
    # The requested folder is always listed; the rest waits for expansion.
    assert _child_names(result) == ["a", "b", "report.txt"]
    for name in ("a", "b"):
        _assert_unloaded(_child(result, name))
    assert result["size"] == len("public report")


class _CountingScandir:
    """``os.scandir`` stand-in that reports each entry it yields."""

    def __init__(self, inner, on_entry):
        self.inner = inner
        self.on_entry = on_entry

    def __enter__(self):
        self.inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self.inner.__exit__(*exc)

    def __iter__(self):
        for entry in self.inner:
            self.on_entry()
            yield entry


def test_lazy_tree_requested_folder_scan_past_its_budget_lists_what_it_found(
    workspace, monkeypatch
):
    """Opening a folder too large to scan in time never fails: the requested
    folder lists the first names its scan found, marked truncated, without a
    total (the folder's size is not known)."""
    outputs = workspace / "outputs"
    for index in range(40):
        (outputs / f"item-{index:02d}.txt").write_text("x")
    (outputs / "sub").mkdir()
    (outputs / "sub" / "inner.txt").write_text("x")
    all_names = {path.name for path in outputs.iterdir()}
    found = 10
    monkeypatch.setattr(files, "_LAZY_SCAN_CHECK_EVERY", 1)
    clock = [0.0]
    monkeypatch.setattr(files, "_lazy_clock", lambda: clock[0])
    original_scandir = os.scandir
    calls = [0]

    def budget_ends_during_the_first_scan(target):
        calls[0] += 1
        entries = original_scandir(target)
        if calls[0] != 1:  # only the requested folder's scan
            return entries
        yielded = [0]

        def on_entry():
            yielded[0] += 1
            if yielded[0] > found:
                # The scan's own budget has run out; the soft budget has not.
                clock[0] = float(files.LAZY_SCAN_SECONDS)

        return _CountingScandir(entries, on_entry)

    monkeypatch.setattr(files.os, "scandir", budget_ends_during_the_first_scan)
    result = request(workspace, "fs_tree", subfolder="outputs", lazy=True)

    assert "error" not in result, result
    assert result["truncated"] is True and "total_entries" not in result
    assert result["partial"] is True
    names = _child_names(result)
    assert len(names) == found and set(names) <= all_names
    folders = [
        child["name"] for child in result["children"] if child["type"] == "folder"
    ]
    files_listed = [
        child["name"] for child in result["children"] if child["type"] == "file"
    ]
    assert names == sorted(folders, key=files._tree_order) + sorted(
        files_listed, key=files._tree_order
    )


def test_lazy_tree_requested_folder_whose_scan_finds_nothing_in_time_still_answers(
    workspace, monkeypatch
):
    clock = [0.0]
    monkeypatch.setattr(files, "_lazy_clock", lambda: clock[0])
    original_scandir = os.scandir

    def budget_already_spent(target):
        clock[0] = float(files.LAZY_TREE_SECONDS)
        return original_scandir(target)

    monkeypatch.setattr(files.os, "scandir", budget_already_spent)
    result = request(workspace, "fs_tree", subfolder="outputs", lazy=True)
    assert "error" not in result, result
    assert result["children"] == [] and result["size"] == 0
    assert result["truncated"] is True and "total_entries" not in result
    assert result["partial"] is True and "children_loaded" not in result


def test_lazy_tree_requested_folder_checks_stop_at_the_soft_budget(
    workspace, monkeypatch
):
    """Checking the requested folder's entries stops at the soft budget: the
    entries checked so far are listed, the folder is truncated with the
    scanned total, and its subfolders wait for expansion."""
    outputs = workspace / "outputs"
    for name in ("a", "b"):
        (outputs / name).mkdir()
        (outputs / name / "x.txt").write_text("x")
    for index in range(10):
        (outputs / f"f{index:02d}.txt").write_text("x")
    clock = [0.0]
    monkeypatch.setattr(files, "_lazy_clock", lambda: clock[0])
    child = files.SecureWorkspace._lazy_child
    checked = [0]

    def expiring_child(self, descriptor, current, name):
        listed = child(self, descriptor, current, name)
        if current == ("outputs",):
            checked[0] += 1
            if checked[0] == 3:
                clock[0] = float(files.LAZY_TREE_SECONDS)
        return listed

    monkeypatch.setattr(files.SecureWorkspace, "_lazy_child", expiring_child)
    result = request(workspace, "fs_tree", subfolder="outputs", lazy=True)

    assert "error" not in result, result
    assert _child_names(result) == ["a", "b", "f00.txt"]
    assert result["truncated"] is True
    assert result["total_entries"] == 13  # a, b, ten f-files and report.txt
    assert result["partial"] is True
    for name in ("a", "b"):
        _assert_unloaded(_child(result, name))
    assert result["size"] == 1


def test_lazy_scan_checks_each_name_without_rechecking_the_folder_path(
    workspace, monkeypatch
):
    """The per-entry checks of a scan cost the name only: the folder's own
    path is checked once per folder, not once per entry (a deep path made one
    huge folder exceed the helper's CPU limit)."""
    parts = ["l0", "l1", "l2", "l3", "l4"]
    deep = workspace.joinpath(*parts)
    deep.mkdir(parents=True)
    for index in range(300):
        (deep / f"item-{index:03d}.txt").touch()
    path_calls = []
    protected_calls = []
    original_path_parts = files.path_parts
    original_protected = files.protected_path

    def counting_path_parts(relative_path):
        path_calls.append(relative_path)
        return original_path_parts(relative_path)

    def counting_protected(checked_parts):
        protected_calls.append(checked_parts)
        return original_protected(checked_parts)

    monkeypatch.setattr(files, "path_parts", counting_path_parts)
    monkeypatch.setattr(files, "protected_path", counting_protected)
    result = request(workspace, "fs_tree", subfolder="/".join(parts), lazy=True)

    assert len(result["children"]) == 300 and "partial" not in result
    assert len(path_calls) <= 3
    assert len([call for call in protected_calls if len(call) > 1]) <= 4


_RULE_PREFIXES = [
    (),
    ("outputs",),
    (".claude", "skills"),
    (".claude",),
    ("ssh-keys",),
    ("sub", ".ENV"),
    ("C:drive",),
    ("bad\nname",),
    ("x\\y",),
    ("é" * 127,),
    ("a",) * 19,
    ("a",) * 20,
    tuple(f"{index}" + "p" * 249 for index in range(8)),
]
_RULE_NAMES = [
    "report.txt",
    "normal",
    "skills",
    "SKILLS",
    "ſkills",
    ".claude",
    ".CLAUDE",
    "ssh-keys",
    "SSH-KEYS",
    ".ssh",
    ".git",
    ".env",
    ".env.local",
    ".env.example",
    ".ENV.PRODUCTION",
    ".credentials.json.bak",
    ".cubicle-files-x",
    ".npmrc.old",
    "C:drive",
    "c:",
    "Z:",
    "1:x",
    "line\nbreak",
    "tab\tname",
    "del\x7f",
    "nul\x00",
    "back\\slash",
    "a" * 39,
    "a" * 40,
    "a" * 41,
    "a" * 255,
    "a" * 256,
    "é" * 127,
    "é" * 128,
    "latin1-\udcff.txt",
    "\udcff",
    "日本語のファイル名",
    ".",
    "..",
    "",
    "a/b",
]


@pytest.mark.parametrize("prefix", _RULE_PREFIXES, ids=range(len(_RULE_PREFIXES)))
def test_child_rules_equal_the_whole_path_rules(prefix):
    rules = files._ChildRules(prefix)
    for name in _RULE_NAMES:
        child = (*prefix, name)
        assert rules.protected(name) == files.protected_path(child), (prefix, name)
        assert rules.addressable(name) == files._tree_addressable(child), (
            prefix,
            name,
        )


@pytest.mark.parametrize("keep", [1, 2, 3, 5, 50])
def test_first_names_keeps_the_first_names_in_tree_order(keep):
    generator = random.Random(keep)
    names = sorted(
        {
            generator.choice(["", "A", "a", "B", "é", "Z", "_"])
            + "".join(generator.choice("abcXYZ019-_") for _ in range(6))
            for _ in range(400)
        }
    )
    for attempt in range(5):
        generator.shuffle(names)
        first = files._FirstNames(keep)
        for name in names:
            first.add(name)
        listed = first.first()
        assert first.count == len(names)
        assert listed == sorted(names, key=files._tree_order)[: len(listed)]
        assert len(listed) >= min(len(names), 2 * keep)
        assert len(listed) <= 4 * keep or len(listed) == len(names)


@pytest.mark.parametrize("room", [9, 10])
def test_lazy_tree_entry_budget_lists_breadth_first(workspace, monkeypatch, room):
    monkeypatch.setattr(files, "LAZY_TREE_ENTRIES", room)
    monkeypatch.setattr(files, "LAZY_DIRECTORY_ENTRIES", 4)
    for name, count in (("a", 2), ("b", 6), ("c", 1)):
        folder = workspace / "outputs" / name
        folder.mkdir()
        for index in range(count):
            (folder / f"{name}{index}.txt").write_text("x")
    result = request(workspace, "fs_tree", subfolder="outputs", lazy=True)
    assert _child_names(result) == ["a", "b", "c", "report.txt"]
    assert _child_names(_child(result, "a")) == ["a0.txt", "a1.txt"]
    assert result["partial"] is True
    if room == 10:
        # b's first four fill the budget; c waits.
        b = _child(result, "b")
        assert _child_names(b) == ["b0.txt", "b1.txt", "b2.txt", "b3.txt"]
        assert b["truncated"] is True and b["total_entries"] == 6 and b["size"] == 4
        _assert_unloaded(_child(result, "c"))
    else:
        # b's first four do not fit: b waits whole; the smaller c still fits.
        _assert_unloaded(_child(result, "b"))
        assert _child_names(_child(result, "c")) == ["c0.txt"]
    assert len(list(_descendants(result))) <= room


def test_lazy_tree_byte_budget_truncates_requested_folder(workspace, monkeypatch):
    outputs = workspace / "outputs"
    for name in ("a", "звіти"):
        (outputs / name).mkdir()
        (outputs / name / "x.txt").write_text("x")
    cost = files._lazy_node_bytes
    budget = cost("outputs", "outputs") + cost("a", "outputs/a")
    budget += cost("звіти", "outputs/звіти")
    monkeypatch.setattr(files, "LAZY_TREE_BYTES", budget)
    result = request(workspace, "fs_tree", subfolder="outputs", lazy=True)
    assert _child_names(result) == ["a", "звіти"]
    assert result["truncated"] is True and result["total_entries"] == 3
    for name in ("a", "звіти"):
        _assert_unloaded(_child(result, name))
    assert result["partial"] is True


def test_lazy_tree_byte_budget_leaves_a_folder_that_does_not_fit_unloaded(
    workspace, monkeypatch
):
    outputs = workspace / "outputs"
    (outputs / "a").mkdir()
    for leaf in ("x.txt", "y.txt"):
        (outputs / "a" / leaf).write_text("x")
    (outputs / "b").mkdir()
    (outputs / "b" / "z.txt").write_text("z")
    cost = files._lazy_node_bytes
    budget = sum(
        cost(name, path)
        for name, path in (
            ("outputs", "outputs"),
            ("a", "outputs/a"),
            ("b", "outputs/b"),
            ("report.txt", "outputs/report.txt"),
            ("x.txt", "outputs/a/x.txt"),
        )
    )
    monkeypatch.setattr(files, "LAZY_TREE_BYTES", budget)
    result = request(workspace, "fs_tree", subfolder="outputs", lazy=True)
    assert "truncated" not in result
    _assert_unloaded(_child(result, "a"))
    assert _child_names(_child(result, "b")) == ["z.txt"]
    assert result["partial"] is True


@pytest.mark.parametrize("change", ["replaced", "removed", "linked"])
def test_lazy_tree_leaves_folder_changed_before_expansion_unloaded(
    workspace, tmp_path, monkeypatch, change
):
    outputs = workspace / "outputs"
    (outputs / "a").mkdir()
    (outputs / "a" / "x.txt").write_text("x")
    external = tmp_path / "external"
    external.mkdir()
    (external / "secret.txt").write_text("EXTERNAL-SENTINEL")
    original_open = os.open
    opened = 0

    def change_on_reopen(path, flags, *args, **kwargs):
        nonlocal opened
        if path == "a" and "dir_fd" in kwargs:
            opened += 1
            if opened == 2:  # the expansion, after the listing validated it
                (outputs / "a").rename(tmp_path / "moved-away")
                if change == "replaced":
                    (outputs / "a").mkdir()
                    (outputs / "a" / "new.txt").write_text("new")
                elif change == "linked":
                    (outputs / "a").symlink_to(external, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(files.os, "open", change_on_reopen)
    result = request(workspace, "fs_tree", subfolder="outputs", lazy=True)
    assert opened == 2
    _assert_unloaded(_child(result, "a"))
    assert result["partial"] is True
    for leaked in ("EXTERNAL-SENTINEL", "secret.txt", "new.txt", "x.txt"):
        assert leaked not in str(result)


def test_lazy_tree_skips_and_counts_names_files_cannot_address(workspace):
    outputs = workspace / "outputs"
    for name in ("C:drive.txt", "line\nbreak.txt", "back\\slash.txt"):
        (outputs / name).write_text("x")
    undecodable = os.fsencode(outputs) + b"/latin1-\xff.txt"
    os.close(os.open(undecodable, os.O_CREAT | os.O_WRONLY, 0o644))
    result = request(workspace, "fs_tree", subfolder="outputs", lazy=True)
    assert _child_names(result) == ["report.txt"]
    assert result["skipped_entries"] == 4
    assert "partial" not in result
    # Every listed name is valid UTF-8 for the browser.
    json.dumps(result, ensure_ascii=False).encode("utf-8")


def test_lazy_tree_skips_names_deeper_than_files_can_address(workspace):
    parts = [f"l{index:02d}" for index in range(files.MAX_DEPTH + 1)]
    (workspace.joinpath(*parts)).mkdir(parents=True)
    subfolder = "/".join(parts[: files.MAX_DEPTH - 2])
    result = request(workspace, "fs_tree", subfolder=subfolder, lazy=True)
    last = _child(result, parts[files.MAX_DEPTH - 2], parts[files.MAX_DEPTH - 1])
    assert last["path"] == "/".join(parts[: files.MAX_DEPTH])
    assert last["children"] == [] and "children_loaded" not in last
    assert result["skipped_entries"] == 1


def test_lazy_tree_matches_the_strict_tree_when_everything_fits(workspace):
    for relative, data in {
        "outputs/data/rows.csv": "a,b\n1,2\n",
        "outputs/Zeta.txt": "z",
        "outputs/alpha/beta/gamma.md": "# g",
        "agents/writer/CLAUDE.md": "notes",
        "outputs/.hidden.txt": "hidden",
        "node_modules/pkg/index.js": "x",
    }.items():
        (workspace / relative).parent.mkdir(parents=True, exist_ok=True)
        (workspace / relative).write_text(data)
    (workspace / "outputs" / "empty").mkdir()
    for subfolder in ("", "outputs", "outputs/alpha"):
        strict = request(workspace, "fs_tree", subfolder=subfolder)
        assert request(workspace, "fs_tree", subfolder=subfolder, lazy=True) == strict
        assert request(workspace, "fs_tree", subfolder=subfolder, lazy=False) == strict


@pytest.mark.parametrize("value", ["true", 1, 0, [], {}])
def test_lazy_tree_flag_must_be_a_boolean(workspace, value):
    result = request(workspace, "fs_tree", lazy=value)
    assert result["status"] == 400 and "children" not in result


def test_lazy_tree_missing_subfolder_is_not_found(workspace):
    result = request(workspace, "fs_tree", subfolder="outputs/none", lazy=True)
    assert result["status"] == 404 and "children" not in result


@pytest.mark.parametrize("fail", [False, True])
def test_lazy_tree_budget_is_restored_before_other_operations(
    workspace, monkeypatch, fail
):
    for index in range(20):
        (workspace / "outputs" / f"item-{index}.txt").touch()
    with files.SecureWorkspace(workspace) as selected:
        if fail:
            selected.deadline = 0
            with pytest.raises(TimeoutError):
                selected._tree({"lazy": True})
            selected.deadline = time.monotonic() + files.DEADLINE_SECONDS
        else:
            monkeypatch.setattr(files, "MAX_TREE_ENTRIES", 1)
            monkeypatch.setattr(files, "LAZY_TREE_ENTRIES", 1)
            assert selected._tree({"lazy": True})["partial"] is True
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
    assert request(workspace, "fs_mkdir", path="new/nested") == {
        "path": "new/nested",
        "created": True,
    }
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


def test_mkdir_retry_finds_the_folder_and_succeeds(workspace):
    """A retry after a lost answer meets the folder the first attempt made
    (EEXIST): it reports the folder exists instead of failing."""
    assert request(workspace, "fs_mkdir", path="new/nested")["created"] is True
    assert request(workspace, "fs_mkdir", path="new/nested") == {
        "path": "new/nested",
        "created": False,
    }
    assert request(workspace, "fs_mkdir", path="outputs") == {
        "path": "outputs",
        "created": False,
    }
    assert (workspace / "new" / "nested").is_dir()


def test_mkdir_over_a_file_or_link_is_refused_and_untouched(workspace, tmp_path):
    result = request(workspace, "fs_mkdir", path="outputs/report.txt")
    assert result["status"] == 400
    assert "file already exists" in result["error"]
    assert (workspace / "outputs" / "report.txt").read_text() == "public report"

    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "outputs" / "link").symlink_to(outside, target_is_directory=True)
    linked = request(workspace, "fs_mkdir", path="outputs/link")
    assert linked["status"] == 400
    assert "created" not in linked
    assert (workspace / "outputs" / "link").is_symlink()
