"""Workstream directories follow their workstream and never lose content (D5/X46).

Before this change a rename orphaned ``workstreams/<old-slug>/`` and the next
sync ``rmtree``'d it unless it held spec.md/learnings — deleting task-owned
outputs, intake records and plan.md. These tests pin the replacement:
renames MOVE the directory (identity map), merges never overwrite, orphans are
archived unless they hold nothing but the regenerable CLAUDE.md, and the
agent-writable identity map can never steer a move outside ``workstreams/``.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from src.config_sync.claude_md_writer import ClaudeMdWriter
from src.config_sync.workstream_dirs import (
    DELETED_PREFIX,
    MAP_DIRNAME,
    MAP_FILENAME,
    RELOCATING_PREFIX,
)
from src.paths import workstream_dir_slug

ALPHA = str(uuid.UUID(int=1))
BETA = str(uuid.UUID(int=2))
GAMMA = str(uuid.UUID(int=3))
DELTA = str(uuid.UUID(int=4))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def _ws(workstream_id: str, name: str, code: str = "AB") -> dict:
    """A synced workstream row as a current backend sends it (with the
    declared ``workspace_dir``)."""
    return {
        "id": workstream_id,
        "name": name,
        "short_code": code,
        "workspace_dir": workstream_dir_slug(name, code),
    }


def _legacy_ws(workstream_id: str, name: str, code: str = "AB") -> dict:
    """The same row from an older backend: no ``workspace_dir``."""
    return {"id": workstream_id, "name": name, "short_code": code}


def _map(workspace: Path) -> dict:
    return json.loads((workspace / MAP_DIRNAME / MAP_FILENAME).read_text())


def _block_aside(root: Path, *workstream_ids: str) -> None:
    """A file at ``.deleted-<id>``: a deleted workstream's directory that
    cannot be archived cannot be moved aside either, so its name stays
    frozen (the last-resort path)."""
    for workstream_id in workstream_ids:
        (root / f"{DELETED_PREFIX}{workstream_id}").write_text("blocks the move")


def _archive_entries(workspace: Path) -> list[str]:
    archive = workspace / "workstreams" / ".archived"
    return sorted(p.name for p in archive.iterdir()) if archive.is_dir() else []


def test_rename_moves_directory_with_task_outputs_and_spec(workspace: Path) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    old = workspace / "workstreams" / "alpha"
    (old / "tasks" / "t1").mkdir(parents=True)
    (old / "tasks" / "t1" / "report.md").write_text("done work")
    (old / "spec.md").write_text("# REQ-1")
    (old / "intake").mkdir()
    (old / "intake" / "001-scope.json").write_text("{}")

    writer.sync_workstream_directories([_ws(ALPHA, "Beta Launch")])

    new = workspace / "workstreams" / "beta-launch"
    assert not old.exists()
    assert (new / "tasks" / "t1" / "report.md").read_text() == "done work"
    assert (new / "spec.md").read_text() == "# REQ-1"
    assert (new / "intake" / "001-scope.json").exists()
    assert "# Workstream: Beta Launch" in (new / "CLAUDE.md").read_text()
    assert _archive_entries(workspace) == []
    assert _map(workspace)["workstreams"] == {ALPHA: "beta-launch"}


def test_swapped_names_swap_directories(workspace: Path) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "One"), _ws(BETA, "Two", "CD")])
    (workspace / "workstreams" / "one" / "owner.txt").write_text("alpha")
    (workspace / "workstreams" / "two" / "owner.txt").write_text("beta")

    writer.sync_workstream_directories([_ws(ALPHA, "Two"), _ws(BETA, "One", "CD")])

    assert (workspace / "workstreams" / "two" / "owner.txt").read_text() == "alpha"
    assert (workspace / "workstreams" / "one" / "owner.txt").read_text() == "beta"
    assert _archive_entries(workspace) == []


def test_rename_merges_into_target_written_by_backend_first(workspace: Path) -> None:
    """The backend re-materialises spec.md into the new directory right after a
    rename; the daemon's move merges instead of clobbering, drops identical
    duplicates, and archives only genuinely conflicting files."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    old = workspace / "workstreams" / "alpha"
    (old / "spec.md").write_text("# REQ-1")
    (old / "plan.md").write_text("old plan")
    (old / "tasks" / "t1").mkdir(parents=True)
    (old / "tasks" / "t1" / "out.txt").write_text("x")
    new = workspace / "workstreams" / "beta"
    new.mkdir()
    (new / "spec.md").write_text("# REQ-1")
    (new / "plan.md").write_text("new plan")

    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])

    assert (new / "tasks" / "t1" / "out.txt").read_text() == "x"
    assert (new / "spec.md").read_text() == "# REQ-1"
    assert (new / "plan.md").read_text() == "new plan"
    archived = _archive_entries(workspace)
    assert archived == ["alpha"]
    remnant = workspace / "workstreams" / ".archived" / "alpha"
    assert sorted(p.name for p in remnant.iterdir()) == ["plan.md"]
    assert (remnant / "plan.md").read_text() == "old plan"


def test_orphan_with_only_claude_md_is_removed(workspace: Path) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")])
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    assert not (workspace / "workstreams" / "beta").exists()
    assert _archive_entries(workspace) == []


def test_deleted_workstream_with_task_outputs_is_archived_never_deleted(
    workspace: Path,
) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")])
    beta = workspace / "workstreams" / "beta"
    (beta / "tasks" / "t9").mkdir(parents=True)
    (beta / "tasks" / "t9" / "app.py").write_text("print(1)")
    archive = workspace / "workstreams" / ".archived"
    archive.mkdir()
    (archive / "beta").mkdir()
    (archive / "beta" / "older.md").write_text("an older archive")

    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])

    assert not beta.exists()
    entries = _archive_entries(workspace)
    assert "beta" in entries and len(entries) == 2
    assert (archive / "beta" / "older.md").read_text() == "an older archive"
    newest = next(name for name in entries if name != "beta")
    assert (archive / newest / "tasks" / "t9" / "app.py").read_text() == "print(1)"


def test_late_write_into_renamed_directory_is_merged_forward(workspace: Path) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])
    # A session that started before the rename recreates the old path.
    late = workspace / "workstreams" / "alpha" / "tasks" / "t2"
    late.mkdir(parents=True)
    (late / "result.txt").write_text("late")

    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])

    assert not (workspace / "workstreams" / "alpha").exists()
    moved = workspace / "workstreams" / "beta" / "tasks" / "t2" / "result.txt"
    assert moved.read_text() == "late"


def test_interrupted_move_is_resumed(workspace: Path) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    root = workspace / "workstreams"
    (root / "alpha" / "spec.md").write_text("spec")
    # Simulate a crash between staging and placement.
    os.rename(root / "alpha", root / f"{RELOCATING_PREFIX}{ALPHA}")

    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])

    assert (root / "beta" / "spec.md").read_text() == "spec"
    assert not (root / f"{RELOCATING_PREFIX}{ALPHA}").exists()


def test_first_sync_moves_non_latin_workstream_out_of_legacy_office_dir(
    workspace: Path,
) -> None:
    root = workspace / "workstreams"
    (root / "office" / "tasks" / "t1").mkdir(parents=True)
    (root / "office" / "spec.md").write_text("legacy spec")
    (root / "office" / "tasks" / "t1" / "a.txt").write_text("a")

    ClaudeMdWriter(str(workspace)).sync_workstream_directories(
        [_ws(ALPHA, "Продажі", "PR")]
    )

    assert not (root / "office").exists()
    assert (root / "ws-pr" / "spec.md").read_text() == "legacy spec"
    assert (root / "ws-pr" / "tasks" / "t1" / "a.txt").exists()


def test_first_sync_keeps_shared_legacy_dir_together_and_archives_it(
    workspace: Path,
) -> None:
    root = workspace / "workstreams"
    (root / "office").mkdir(parents=True)
    (root / "office" / "spec.md").write_text("mixed")

    ClaudeMdWriter(str(workspace)).sync_workstream_directories(
        [_ws(ALPHA, "Продажі", "PR"), _ws(BETA, "Маркетинг", "MA")]
    )

    # Mixed content from two workstreams cannot be split: neither gets it,
    # and the sweep archives it instead of deleting it.
    assert not (root / "ws-pr" / "spec.md").exists()
    assert not (root / "ws-ma" / "spec.md").exists()
    assert _archive_entries(workspace) == ["office"]
    assert (root / ".archived" / "office" / "spec.md").read_text() == "mixed"


def test_tampered_map_cannot_move_directories_outside_workstreams(
    workspace: Path,
) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    agents = workspace / "agents" / "precious"
    agents.mkdir(parents=True)
    (agents / "CLAUDE.md").write_text("keep me")
    map_path = workspace / MAP_DIRNAME / MAP_FILENAME
    map_path.write_text(
        json.dumps(
            {
                "workstreams": {ALPHA: "../agents", "not-a-uuid": "alpha"},
                "retired": {"../agents": ALPHA},
            }
        )
    )

    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])

    assert (agents / "CLAUDE.md").read_text() == "keep me"
    assert (workspace / "workstreams" / "beta" / "CLAUDE.md").exists()
    assert _map(workspace)["workstreams"] == {ALPHA: "beta"}


def test_symlink_in_workstreams_is_never_followed(workspace: Path, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("host data")
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    root = workspace / "workstreams"
    (root / "planted").symlink_to(outside)
    (root / "alpha" / "tasks").mkdir()
    (root / "alpha" / "tasks" / "link").symlink_to(outside)
    # An archive root replaced by a link must not receive archived content.
    (root / ".archived").symlink_to(outside)

    writer.sync_workstream_directories([_ws(ALPHA, "Beta"), _ws(BETA, "Gone", "GO")])
    # The deleted workstream leaves real content behind, so the next sync
    # MUST archive it — and the archive root is a planted link.
    (root / "gone" / "tasks").mkdir()
    (root / "gone" / "tasks" / "x.txt").write_text("gone work")
    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])

    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]
    assert (outside / "secret.txt").read_text() == "host data"
    assert (root / "planted").is_symlink()
    assert (root / "beta" / "tasks" / "link").is_symlink()
    # Archiving through the link was refused: the content is kept in place.
    assert (root / "gone" / "tasks" / "x.txt").read_text() == "gone work"
    assert (root / ".archived").is_symlink()


def test_legacy_duplicate_directory_stays_with_the_remaining_workstream(
    workspace: Path,
) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "alpha", "BE")])
    (workspace / "workstreams" / "alpha" / "spec.md").write_text("shared")

    writer.sync_workstream_directories(
        [_ws(ALPHA, "Alpha"), _ws(BETA, "Renamed", "BE")]
    )

    assert (workspace / "workstreams" / "alpha" / "spec.md").read_text() == "shared"
    assert (workspace / "workstreams" / "renamed" / "CLAUDE.md").exists()


def test_workstreams_root_symlink_is_refused(workspace: Path, tmp_path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (workspace / "workstreams").symlink_to(outside)
    ClaudeMdWriter(str(workspace)).sync_workstream_directories([_ws(ALPHA, "Alpha")])
    assert list(outside.iterdir()) == []


def test_empty_sync_touches_neither_directories_nor_map(workspace: Path) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    (workspace / "workstreams" / "alpha" / "tasks").mkdir()
    before = _map(workspace)

    writer.sync_workstream_directories([])

    assert (workspace / "workstreams" / "alpha" / "tasks").is_dir()
    assert _map(workspace) == before


def test_workstream_without_id_keeps_legacy_orphan_behaviour(workspace: Path) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([{"name": "Alpha"}])
    (workspace / "workstreams" / "alpha" / "spec.md").write_text("s")
    writer.sync_workstream_directories([{"name": "Renamed"}])
    assert _archive_entries(workspace) == ["alpha"]
    archived = workspace / "workstreams" / ".archived" / "alpha" / "spec.md"
    assert archived.read_text() == "s"
    assert (workspace / "workstreams" / "renamed" / "CLAUDE.md").exists()


def test_failed_staging_is_retried_instead_of_archived(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    root = workspace / "workstreams"
    (root / "alpha" / "tasks").mkdir()
    (root / "alpha" / "tasks" / "out.txt").write_text("work")

    real_rename = os.rename

    def refuse_staging(src, dst, *args, **kwargs):
        if str(dst).startswith(RELOCATING_PREFIX):
            raise PermissionError("busy")
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", refuse_staging)
    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])
    # The move failed: the old directory is kept (not archived) and the map
    # still records it, so the next sync retries.
    assert (root / "alpha" / "tasks" / "out.txt").read_text() == "work"
    assert _archive_entries(workspace) == []
    assert _map(workspace)["workstreams"] == {ALPHA: "alpha"}

    monkeypatch.setattr(os, "rename", real_rename)
    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])
    assert not (root / "alpha").exists()
    assert (root / "beta" / "tasks" / "out.txt").read_text() == "work"
    assert _map(workspace)["workstreams"] == {ALPHA: "beta"}


def test_stale_map_entry_never_takes_another_live_workstream_directory(
    workspace: Path,
) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])
    root = workspace / "workstreams"
    # Rollback: an older daemon (no map upkeep) ran while ALPHA was renamed to
    # "Alpha" and a NEW workstream took the name "Beta" and wrote into it. The
    # older daemon writes each workstream's CLAUDE.md (no id marker) into
    # its slugified name.
    _older_daemon_claude_md(root, "Alpha", "AB")
    _older_daemon_claude_md(root, "Beta", "BE")
    (root / "beta" / "tasks").mkdir()
    (root / "beta" / "tasks" / "owner.txt").write_text("the new Beta")

    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")])

    assert (root / "beta" / "tasks" / "owner.txt").read_text() == "the new Beta"
    assert not (root / "alpha" / "tasks").exists()
    assert _archive_entries(workspace) == []
    assert _map(workspace)["workstreams"] == {ALPHA: "alpha", BETA: "beta"}


def _older_daemon_claude_md(root: Path, name: str, code: str) -> None:
    """The CLAUDE.md a daemon older than the id marker writes (heading and
    short code only) into ``slugify(name)``."""
    directory = root / name.lower()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "CLAUDE.md").write_text(
        f"# Workstream: {name}\n\n**Short code:** `{code}` · **Priority:** `medium`\n"
    )


def _adopt_rename_setup(workspace: Path) -> Path:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")])
    root = workspace / "workstreams"
    (root / "beta" / "spec.md").write_text("beta spec")
    (root / "beta" / "tasks" / "t1").mkdir(parents=True)
    (root / "beta" / "tasks" / "t1" / "out.md").write_text("beta work")
    return root


def _adopt_rename_sync(workspace: Path) -> None:
    ClaudeMdWriter(str(workspace)).sync_workstream_directories(
        [_ws(ALPHA, "Alpha"), _ws(BETA, "Gamma", "BE"), _ws(GAMMA, "Beta", "CE")]
    )


@pytest.mark.parametrize("claude_md", ["marker", "pre-marker", "none"])
def test_rename_and_new_workstream_with_the_old_name_in_one_sync_moves_it(
    workspace: Path, claude_md: str
) -> None:
    """ADOPT-RENAME: Beta is renamed to "Gamma" and a NEW workstream "Beta"
    is created in the same sync (the daemon was offline, or sync revisions
    were coalesced). The map authorizes the move and nothing vetoes it, so
    the directory moves with the renamed workstream; the new one starts with
    an empty directory."""
    root = _adopt_rename_setup(workspace)
    if claude_md == "pre-marker":
        _older_daemon_claude_md(root, "Beta", "BE")
    elif claude_md == "none":
        (root / "beta" / "CLAUDE.md").unlink()

    _adopt_rename_sync(workspace)

    assert (root / "gamma" / "spec.md").read_text() == "beta spec"
    assert (root / "gamma" / "tasks" / "t1" / "out.md").read_text() == "beta work"
    assert sorted(p.name for p in (root / "beta").iterdir()) == ["CLAUDE.md"]
    assert _archive_entries(workspace) == []
    assert _map(workspace)["workstreams"] == {
        ALPHA: "alpha",
        BETA: "gamma",
        GAMMA: "beta",
    }


def test_stale_map_entry_without_a_claude_md_known_limitation(
    workspace: Path,
) -> None:
    """A stale map entry (an older daemon ran without the rollback helper)
    whose directory has lost its CLAUDE.md: nothing vetoes the move the map
    authorizes, so the recorded workstream takes the directory the new one
    wrote into (a documented known limitation, communicator.md). With the
    older daemon's CLAUDE.md present the move is vetoed (the stale-entry
    test above)."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])
    root = workspace / "workstreams"
    (root / "beta" / "CLAUDE.md").unlink()
    (root / "beta" / "tasks").mkdir()
    (root / "beta" / "tasks" / "owner.txt").write_text("the new Beta")

    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")])

    assert (root / "alpha" / "tasks" / "owner.txt").read_text() == "the new Beta"
    assert sorted(p.name for p in (root / "beta").iterdir()) == ["CLAUDE.md"]


def test_deep_shared_tree_merge_is_bounded(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.config_sync import workstream_dirs

    monkeypatch.setattr(workstream_dirs, "_MERGE_MAX_DEPTH", 3)
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    root = workspace / "workstreams"
    nest = "/".join(["d"] * 6)
    (root / "alpha" / nest).mkdir(parents=True)
    (root / "alpha" / nest / "deep.txt").write_text("too deep")
    (root / "alpha" / "d" / "top.txt").write_text("moved")
    (root / "beta" / nest).mkdir(parents=True)

    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])

    assert (root / "beta" / "d" / "top.txt").read_text() == "moved"
    assert (root / "beta" / "CLAUDE.md").exists()
    # Without the bound the merge would reach the bottom and move deep.txt
    # into beta; with it, the part below the bound stays behind and is
    # archived, never deleted.
    assert not (root / "beta" / nest / "deep.txt").exists()
    assert _archive_entries(workspace) == ["alpha"]
    archived = workspace / "workstreams" / ".archived" / "alpha" / nest / "deep.txt"
    assert archived.read_text() == "too deep"


def test_interrupted_move_never_overwrites_a_conflicting_target(
    workspace: Path,
) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    root = workspace / "workstreams"
    (root / "alpha" / "spec.md").write_text("staged spec")
    (root / "alpha" / "tasks").mkdir()
    (root / "alpha" / "tasks" / "out.txt").write_text("work")
    os.rename(root / "alpha", root / f"{RELOCATING_PREFIX}{ALPHA}")
    # The backend re-materialised a different spec at the new name first.
    (root / "beta").mkdir()
    (root / "beta" / "spec.md").write_text("target spec")

    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])

    assert (root / "beta" / "spec.md").read_text() == "target spec"
    assert (root / "beta" / "tasks" / "out.txt").read_text() == "work"
    assert not (root / f"{RELOCATING_PREFIX}{ALPHA}").exists()
    [archived] = _archive_entries(workspace)
    assert (root / ".archived" / archived / "spec.md").read_text() == "staged spec"


def test_unexpected_layout_error_does_not_stop_the_sync(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.config_sync import workstream_dirs

    def boom(self, *args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(workstream_dirs.WorkstreamLayout, "prepare", boom)
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    assert (workspace / "workstreams" / "alpha" / "CLAUDE.md").exists()

    monkeypatch.undo()
    monkeypatch.setattr(workstream_dirs.WorkstreamLayout, "finish", boom)
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    assert (workspace / "workstreams" / "alpha" / "CLAUDE.md").exists()


def test_deeply_nested_map_json_is_treated_as_unreadable(workspace: Path) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    (workspace / MAP_DIRNAME / MAP_FILENAME).write_text("[" * 200_000)
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    assert _map(workspace)["workstreams"] == {ALPHA: "alpha"}


def test_older_backend_keeps_the_legacy_layout(workspace: Path) -> None:
    """Paired with a backend that declares no ``workspace_dir``, the daemon
    keeps the legacy directories that backend writes spec.md/intake to."""
    root = workspace / "workstreams"
    (root / "office").mkdir(parents=True)
    (root / "office" / "spec.md").write_text("legacy spec")
    writer = ClaudeMdWriter(str(workspace))

    writer.sync_workstream_directories(
        [_legacy_ws(ALPHA, "Продажі", "PR"), _legacy_ws(BETA, "Beta", "BE")]
    )

    assert (root / "office" / "spec.md").read_text() == "legacy spec"
    assert "# Workstream: Продажі" in (root / "office" / "CLAUDE.md").read_text()
    assert not (root / "ws-pr").exists()
    assert _map(workspace)["workstreams"] == {ALPHA: "office", BETA: "beta"}

    # Once the backend declares the layout, the lone owner moves out.
    writer.sync_workstream_directories(
        [_ws(ALPHA, "Продажі", "PR"), _ws(BETA, "Beta", "BE")]
    )
    assert not (root / "office").exists()
    assert (root / "ws-pr" / "spec.md").read_text() == "legacy spec"


# ---------------------------------------------------------------------------
# CM1: one workstream's directory never fails the whole sync
# ---------------------------------------------------------------------------


def test_file_at_new_workstream_name_does_not_fail_the_sync(workspace: Path) -> None:
    """A regular file at a workstream's directory name used to raise out of
    ``sync_all`` (config retry -> worker admission paused); now that one
    workstream is skipped and every other CLAUDE.md is still written."""
    root = workspace / "workstreams"
    root.mkdir()
    (root / "alpha").write_text("not a directory")
    writer = ClaudeMdWriter(str(workspace))

    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")])

    assert (root / "alpha").read_text() == "not a directory"
    assert "# Workstream: Beta" in (root / "beta" / "CLAUDE.md").read_text()


def test_full_disk_on_a_workstream_claude_md_fails_the_sync(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B4-bugs-1: ENOSPC on one workstream's CLAUDE.md used to be logged as
    "retried on the next sync" while no sync was scheduled: ``sync_all``
    returned, admission reopened and workers read stale instructions. The
    error is now raised after the pass (the other workstreams are written),
    so config sync keeps admission closed and retries."""
    import errno as errno_module

    from src.config_sync import workstream_dirs

    rows = [
        {**_ws(ALPHA, "Alpha"), "context_notes": "v1 alpha"},
        {**_ws(BETA, "Beta", "BE"), "context_notes": "v1 beta"},
    ]
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_all({"agents": [], "workstreams": rows})
    root = workspace / "workstreams"
    original = workstream_dirs.atomic_replace_file

    def full_disk_for_alpha(directory_fd, filename, content, *args, **kwargs):
        if "v2 alpha" in content:
            raise OSError(errno_module.ENOSPC, "No space left on device")
        return original(directory_fd, filename, content, *args, **kwargs)

    monkeypatch.setattr(workstream_dirs, "atomic_replace_file", full_disk_for_alpha)
    rows = [
        {**_ws(ALPHA, "Alpha"), "context_notes": "v2 alpha"},
        {**_ws(BETA, "Beta", "BE"), "context_notes": "v2 beta"},
    ]

    with pytest.raises(OSError) as raised:
        writer.sync_all({"agents": [], "workstreams": rows})

    assert raised.value.errno == errno_module.ENOSPC
    assert "workstream directory alpha" in str(raised.value)
    assert "v1 alpha" in (root / "alpha" / "CLAUDE.md").read_text()
    assert "v2 beta" in (root / "beta" / "CLAUDE.md").read_text()


def test_an_environmental_error_opening_workstreams_fails_the_sync(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B4-bugs-1: exhausted descriptors on the ``workstreams/`` open used to
    return quietly, so renamed directories were neither moved nor recorded
    while admission reopened."""
    import errno as errno_module
    from contextlib import contextmanager

    from src.config_sync import claude_md_writer

    @contextmanager
    def no_descriptors(_workspace):
        raise OSError(errno_module.EMFILE, "Too many open files")
        yield  # pragma: no cover

    monkeypatch.setattr(claude_md_writer, "open_workstreams_root", no_descriptors)

    with pytest.raises(OSError) as raised:
        ClaudeMdWriter(str(workspace)).sync_workstream_directories(
            [_ws(ALPHA, "Alpha")]
        )

    assert raised.value.errno == errno_module.EMFILE


def test_a_session_caused_error_opening_workstreams_skips_them(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CM1 rule still holds at the root: a planted entry (ENOTDIR) is
    logged and the workstream step skipped, never a sync failure."""
    import errno as errno_module
    from contextlib import contextmanager

    from src.config_sync import claude_md_writer

    @contextmanager
    def planted(_workspace):
        raise OSError(errno_module.ENOTDIR, "Not a directory")
        yield  # pragma: no cover

    monkeypatch.setattr(claude_md_writer, "open_workstreams_root", planted)

    ClaudeMdWriter(str(workspace)).sync_workstream_directories([_ws(ALPHA, "Alpha")])


def test_file_at_rename_target_keeps_the_staged_directory_for_retry(
    workspace: Path,
) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    root = workspace / "workstreams"
    (root / "alpha" / "tasks").mkdir()
    (root / "alpha" / "tasks" / "out.txt").write_text("work")
    (root / "beta").write_text("a stray file")

    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])

    # Not archived: the moved directory waits, staged, for the name to free.
    staged = root / f"{RELOCATING_PREFIX}{ALPHA}"
    assert (staged / "tasks" / "out.txt").read_text() == "work"
    assert _archive_entries(workspace) == []
    assert (root / "beta").read_text() == "a stray file"

    (root / "beta").unlink()
    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])

    assert not staged.exists()
    assert (root / "beta" / "tasks" / "out.txt").read_text() == "work"
    assert "# Workstream: Beta" in (root / "beta" / "CLAUDE.md").read_text()
    assert _map(workspace)["workstreams"] == {ALPHA: "beta"}


def test_link_at_rename_target_is_never_merged_through(
    workspace: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    root = workspace / "workstreams"
    (root / "alpha" / "spec.md").write_text("spec")
    (root / "beta").symlink_to(outside)

    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])

    assert list(outside.iterdir()) == []
    assert (root / f"{RELOCATING_PREFIX}{ALPHA}" / "spec.md").read_text() == "spec"
    assert (root / "beta").is_symlink()


def test_pending_move_never_claims_its_old_name(workspace: Path) -> None:
    """DL-3: a move still staged is recorded at its target, not at the name it
    came from. Before the fix, a workstream that took that name later was
    treated as sharing it: renaming it archived its spec and outputs
    instead of moving them."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "Bravo", "BR")])
    root = workspace / "workstreams"
    (root / "alpha" / "alpha.txt").write_text("alpha work")
    (root / "bravo" / "spec.md").write_text("bravo spec")
    (root / "bravo" / "tasks").mkdir()
    (root / "bravo" / "tasks" / "out.md").write_text("bravo output")
    (root / "beta").write_text("a stray file")

    # Alpha -> "Beta" waits (a file sits there); Bravo -> "Alpha" moves in.
    writer.sync_workstream_directories([_ws(ALPHA, "Beta"), _ws(BETA, "Alpha", "BR")])

    assert (root / "alpha" / "spec.md").read_text() == "bravo spec"
    assert _map(workspace)["workstreams"] == {ALPHA: "beta", BETA: "alpha"}
    assert _map(workspace)["staged"] == {ALPHA: "alpha"}

    writer.sync_workstream_directories([_ws(ALPHA, "Beta"), _ws(BETA, "Charlie", "BR")])

    assert (root / "charlie" / "spec.md").read_text() == "bravo spec"
    assert (root / "charlie" / "tasks" / "out.md").read_text() == "bravo output"
    assert not (root / "alpha").exists()
    assert _archive_entries(workspace) == []
    assert (root / f"{RELOCATING_PREFIX}{ALPHA}" / "alpha.txt").exists()

    (root / "beta").unlink()
    writer.sync_workstream_directories([_ws(ALPHA, "Beta"), _ws(BETA, "Charlie", "BR")])

    assert (root / "beta" / "alpha.txt").read_text() == "alpha work"
    assert _map(workspace)["workstreams"] == {ALPHA: "beta", BETA: "charlie"}
    assert _map(workspace)["staged"] == {}


def test_new_workstream_at_a_pending_moves_old_name_keeps_its_directory(
    workspace: Path,
) -> None:
    """WSD-4: a workstream created with the name a still-staged move came
    from owns that directory; renaming it moves its content."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    root = workspace / "workstreams"
    (root / "beta").write_text("a stray file")
    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])
    # A new workstream takes the name "Alpha".
    writer.sync_workstream_directories([_ws(ALPHA, "Beta"), _ws(GAMMA, "Alpha", "AL")])
    (root / "alpha" / "spec.md").write_text("new alpha spec")

    writer.sync_workstream_directories([_ws(ALPHA, "Beta"), _ws(GAMMA, "Gamma", "AL")])

    assert (root / "gamma" / "spec.md").read_text() == "new alpha spec"
    assert _archive_entries(workspace) == []


# ---------------------------------------------------------------------------
# CM2: a chain rename waits for a directory whose own move failed to stage
# ---------------------------------------------------------------------------


def test_chain_rename_never_merges_into_a_held_directory(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")])
    root = workspace / "workstreams"
    (root / "alpha" / "alpha.txt").write_text("alpha work")
    (root / "beta" / "beta.txt").write_text("beta work")

    real_rename = os.rename

    def refuse_beta_staging(src, dst, *args, **kwargs):
        if str(dst) == f"{RELOCATING_PREFIX}{BETA}":
            raise PermissionError("busy")
        return real_rename(src, dst, *args, **kwargs)

    # Chain: Alpha -> "Beta" while Beta -> "Gamma"; Beta's staging fails.
    monkeypatch.setattr(os, "rename", refuse_beta_staging)
    writer.sync_workstream_directories([_ws(ALPHA, "Beta"), _ws(BETA, "Gamma", "BE")])

    assert (root / "beta" / "beta.txt").read_text() == "beta work"
    assert not (root / "beta" / "alpha.txt").exists()
    staged = root / f"{RELOCATING_PREFIX}{ALPHA}"
    assert (staged / "alpha.txt").read_text() == "alpha work"
    assert _archive_entries(workspace) == []

    monkeypatch.setattr(os, "rename", real_rename)
    writer.sync_workstream_directories([_ws(ALPHA, "Beta"), _ws(BETA, "Gamma", "BE")])

    assert not staged.exists()
    assert sorted(p.name for p in (root / "beta").iterdir()) == [
        "CLAUDE.md",
        "alpha.txt",
    ]
    assert (root / "gamma" / "beta.txt").read_text() == "beta work"
    assert not (root / "gamma" / "alpha.txt").exists()
    assert _map(workspace)["workstreams"] == {ALPHA: "beta", BETA: "gamma"}


# ---------------------------------------------------------------------------
# CM3: a renamed workstream never adopts a deleted workstream's directory
# ---------------------------------------------------------------------------


def test_rename_onto_deleted_workstreams_name_archives_its_content(
    workspace: Path,
) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")])
    root = workspace / "workstreams"
    (root / "alpha" / "spec.md").write_text("alpha spec")
    (root / "beta" / "spec.md").write_text("deleted beta spec")
    (root / "beta" / "learnings.md").write_text("beta lessons")

    # Beta is deleted and Alpha is renamed to "Beta" in the same sync.
    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])

    assert (root / "beta" / "spec.md").read_text() == "alpha spec"
    assert not (root / "beta" / "learnings.md").exists()
    assert _archive_entries(workspace) == ["beta"]
    archived = root / ".archived" / "beta"
    assert (archived / "spec.md").read_text() == "deleted beta spec"
    assert (archived / "learnings.md").read_text() == "beta lessons"


def test_new_workstream_with_a_deleted_workstreams_name_starts_fresh(
    workspace: Path,
) -> None:
    """WSD-5: a workstream CREATED with a deleted workstream's name (a new id,
    so no map entry) never inherits the deleted one's spec, intake records
    or learnings; they are archived before its CLAUDE.md is written."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")])
    root = workspace / "workstreams"
    (root / "beta" / "spec.md").write_text("deleted beta spec")
    (root / "beta" / "intake").mkdir()
    (root / "beta" / "intake" / "001-scope.json").write_text("{}")
    (root / "beta" / "learnings.md").write_text("beta lessons")

    # While the daemon was away, Beta was deleted and a new "Beta" created.
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(GAMMA, "Beta", "BE")])

    assert sorted(p.name for p in (root / "beta").iterdir()) == ["CLAUDE.md"]
    assert _archive_entries(workspace) == ["beta"]
    archived = root / ".archived" / "beta"
    assert (archived / "spec.md").read_text() == "deleted beta spec"
    assert (archived / "intake" / "001-scope.json").exists()
    assert (archived / "learnings.md").read_text() == "beta lessons"
    assert _map(workspace)["workstreams"] == {ALPHA: "alpha", GAMMA: "beta"}


def test_failed_archive_is_retried_for_a_new_workstream_with_the_name(
    workspace: Path,
) -> None:
    """WSD1-new-claimant: a new workstream named for a deleted one waits for
    the archive on every sync, not only the first (its map record at the
    name must not drop the deleted workstream's claim)."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")])
    root = workspace / "workstreams"
    (root / "beta" / "spec.md").write_text("deleted beta spec")
    (root / "beta" / "learnings.md").write_text("beta lessons")
    (root / "beta" / "CLAUDE.md").write_text("deleted beta instructions")
    (root / ".archived").write_text("not a directory")
    _block_aside(root, BETA)
    rows = [_ws(ALPHA, "Alpha"), _ws(GAMMA, "Beta", "CE")]

    writer.sync_workstream_directories(rows)
    writer.sync_workstream_directories(rows)

    assert _map(workspace)["deleted"] == {BETA: "beta"}
    assert (root / "beta" / "CLAUDE.md").read_text() == "deleted beta instructions"

    (root / ".archived").unlink()
    writer.sync_workstream_directories(rows)

    archived = root / ".archived" / "beta"
    assert (archived / "spec.md").read_text() == "deleted beta spec"
    assert (archived / "learnings.md").read_text() == "beta lessons"
    assert sorted(p.name for p in (root / "beta").iterdir()) == ["CLAUDE.md"]
    assert "`CE`" in (root / "beta" / "CLAUDE.md").read_text()
    assert _map(workspace)["deleted"] == {}


def _deleted_beta_with_a_broken_archive(workspace: Path) -> Path:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")])
    root = workspace / "workstreams"
    (root / "beta" / "spec.md").write_text("deleted beta spec")
    (root / "beta" / "learnings.md").write_text("beta lessons")
    (root / "beta" / "CLAUDE.md").write_text("deleted beta instructions")
    (root / ".archived").write_text("not a directory")
    _block_aside(root, BETA)
    return root


def _assert_beta_frozen(workspace: Path, root: Path) -> None:
    assert sorted(p.name for p in (root / "beta").iterdir()) == [
        "CLAUDE.md",
        "learnings.md",
        "spec.md",
    ]
    assert (root / "beta" / "CLAUDE.md").read_text() == "deleted beta instructions"
    assert _map(workspace)["deleted"] == {BETA: "beta"}
    assert "beta" not in _map(workspace)["workstreams"].values()


def test_a_waiting_claimant_renamed_before_the_archive_starts_fresh(
    workspace: Path,
) -> None:
    """Invariant F (R3-WSD1-RENAME, R3-WSD1-CLAIMANT-RENAME,
    R3-WAITING-CLAIMANT-RENAME): a new workstream waiting at a deleted
    workstream's name is never recorded there, so when it is renamed while
    the archive still fails it gets a fresh directory and the deleted
    workstream's directory stays frozen until it is archived."""
    root = _deleted_beta_with_a_broken_archive(workspace)
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(GAMMA, "Beta", "CE")])
    _assert_beta_frozen(workspace, root)

    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(GAMMA, "Gamma", "CE")])

    _assert_beta_frozen(workspace, root)
    assert sorted(p.name for p in (root / "gamma").iterdir()) == ["CLAUDE.md"]

    (root / ".archived").unlink()
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(GAMMA, "Gamma", "CE")])

    archived = root / ".archived" / "beta"
    assert (archived / "spec.md").read_text() == "deleted beta spec"
    assert (archived / "learnings.md").read_text() == "beta lessons"
    assert not (root / "beta").exists()
    assert sorted(p.name for p in (root / "gamma").iterdir()) == ["CLAUDE.md"]
    assert _map(workspace)["deleted"] == {}


def test_an_unstaged_rename_onto_a_frozen_name_never_takes_it_along(
    workspace: Path,
) -> None:
    """Invariant F: a workstream renamed onto a deleted workstream's name
    whose own directory was missing (so nothing was staged) waits unrecorded
    there; renamed again, it never carries that directory off."""
    root = _deleted_beta_with_a_broken_archive(workspace)
    writer = ClaudeMdWriter(str(workspace))
    (root / "alpha" / "CLAUDE.md").unlink()
    (root / "alpha").rmdir()
    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])
    _assert_beta_frozen(workspace, root)
    assert _map(workspace)["workstreams"][ALPHA] == "alpha"

    writer.sync_workstream_directories([_ws(ALPHA, "Delta")])

    _assert_beta_frozen(workspace, root)
    assert sorted(p.name for p in (root / "delta").iterdir()) == ["CLAUDE.md"]


def _wsd1_state_then_repaired(workspace: Path) -> Path:
    """B deleted while A is renamed onto its name and .archived is broken
    (A's move stays staged, B's claim is kept), then .archived repaired."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")])
    root = workspace / "workstreams"
    (root / "alpha" / "spec.md").write_text("alpha spec")
    (root / "beta" / "spec.md").write_text("deleted beta spec")
    (root / ".archived").write_text("not a directory")
    _block_aside(root, BETA)
    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])
    assert _map(workspace)["deleted"] == {BETA: "beta"}
    (root / ".archived").unlink()
    return root


def test_a_stale_deleted_claim_never_archives_the_directory_now_at_the_name(
    workspace: Path,
) -> None:
    """R3-WSD1-STALE-DELETED: a sync archives B's directory and places A's
    staged move at the name, but its map update is lost (a crash, or a failed
    save). The next sync finds another directory than the one B's claim
    recorded, drops the claim and leaves A's directory alone."""
    root = _wsd1_state_then_repaired(workspace)
    # The whole update is lost, write-ahead record included (a map restored
    # from before the sync, or written by an older daemon).
    _sync_losing_the_map(workspace, [_ws(ALPHA, "Beta")])
    assert (root / "beta" / "spec.md").read_text() == "alpha spec"
    assert _map(workspace)["deleted"] == {BETA: "beta"}  # stale on disk

    ClaudeMdWriter(str(workspace)).sync_workstream_directories([_ws(ALPHA, "Beta")])

    assert (root / "beta" / "spec.md").read_text() == "alpha spec"
    [archived] = _archive_entries(workspace)
    assert (root / ".archived" / archived / "spec.md").read_text() == (
        "deleted beta spec"
    )
    assert _map(workspace)["deleted"] == {}


def test_an_old_deleted_claim_without_identity_yields_to_a_recorded_workstream(
    workspace: Path,
) -> None:
    """A map written before identities were recorded: the claim is dropped
    when a workstream (not a staged move) is recorded at the name, and kept
    (with its identity recorded now) otherwise."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Beta"), _ws(GAMMA, "Gamma", "GA")])
    root = workspace / "workstreams"
    (root / "beta" / "spec.md").write_text("alpha spec")
    (root / "gamma" / "spec.md").write_text("gamma spec")
    raw = _map(workspace)
    raw["workstreams"].pop(GAMMA)
    raw["deleted"] = {BETA: "beta", str(uuid.UUID(int=9)): "gamma"}
    raw.pop("deleted_identity", None)
    (workspace / MAP_DIRNAME / MAP_FILENAME).write_text(json.dumps(raw))

    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])

    assert (root / "beta" / "spec.md").read_text() == "alpha spec"
    [archived] = _archive_entries(workspace)
    assert archived == "gamma"
    assert _map(workspace)["deleted"] == {}


def test_failed_archive_of_deleted_workstreams_directory_is_retried(
    workspace: Path,
) -> None:
    """WSD-1/DL-2: the deleted workstream's claim survives a failed archive.

    Before the fix the claim lasted one pass: the next sync merged the
    renamed workstream into the deleted one's directory (adopting its spec
    and learnings), and the first sync already wrote the renamed
    workstream's CLAUDE.md into it."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")])
    root = workspace / "workstreams"
    (root / "alpha" / "spec.md").write_text("alpha spec")
    (root / "beta" / "spec.md").write_text("deleted beta spec")
    (root / "beta" / "learnings.md").write_text("beta lessons")
    (root / "beta" / "CLAUDE.md").write_text("deleted beta instructions")
    # Archiving through a regular file at .archived fails, and so does the
    # move aside.
    (root / ".archived").write_text("not a directory")
    _block_aside(root, BETA)
    staged = root / f"{RELOCATING_PREFIX}{ALPHA}"

    # Beta is deleted and Alpha is renamed to "Beta" in the same sync.
    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])

    assert (staged / "spec.md").read_text() == "alpha spec"
    assert (root / "beta" / "spec.md").read_text() == "deleted beta spec"
    assert (root / "beta" / "CLAUDE.md").read_text() == "deleted beta instructions"
    assert _map(workspace)["deleted"] == {BETA: "beta"}

    # Still broken on the next sync: nothing of Alpha lands in beta/.
    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])

    assert (staged / "spec.md").read_text() == "alpha spec"
    assert sorted(p.name for p in (root / "beta").iterdir()) == [
        "CLAUDE.md",
        "learnings.md",
        "spec.md",
    ]
    assert (root / "beta" / "spec.md").read_text() == "deleted beta spec"
    assert (root / "beta" / "CLAUDE.md").read_text() == "deleted beta instructions"
    assert _map(workspace)["deleted"] == {BETA: "beta"}

    # Repaired: the deleted workstream's content is archived first, then
    # Alpha's staged directory takes the name.
    (root / ".archived").unlink()
    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])

    assert not staged.exists()
    assert sorted(p.name for p in (root / "beta").iterdir()) == [
        "CLAUDE.md",
        "spec.md",
    ]
    assert (root / "beta" / "spec.md").read_text() == "alpha spec"
    assert "# Workstream: Beta" in (root / "beta" / "CLAUDE.md").read_text()
    assert _archive_entries(workspace) == ["beta"]
    archived = root / ".archived" / "beta"
    assert (archived / "spec.md").read_text() == "deleted beta spec"
    assert (archived / "learnings.md").read_text() == "beta lessons"
    assert _map(workspace)["deleted"] == {}
    assert _map(workspace)["workstreams"] == {ALPHA: "beta"}


def test_unclaimed_directory_of_deleted_workstream_keeps_its_claim_until_archived(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deleted workstream's directory the orphan sweep cannot archive keeps
    its claim, so a workstream renamed onto the name later never adopts it."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")])
    root = workspace / "workstreams"
    (root / "beta" / "spec.md").write_text("deleted beta spec")
    (root / "beta" / "CLAUDE.md").write_text("deleted beta instructions")
    (root / ".archived").write_text("not a directory")
    _block_aside(root, BETA)

    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])

    assert (root / "beta" / "spec.md").read_text() == "deleted beta spec"
    assert _map(workspace)["deleted"] == {BETA: "beta"}

    # Alpha is renamed onto the name while the archive still fails.
    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])

    assert (root / "beta" / "spec.md").read_text() == "deleted beta spec"
    assert (root / "beta" / "CLAUDE.md").read_text() == "deleted beta instructions"
    assert (root / f"{RELOCATING_PREFIX}{ALPHA}").is_dir()

    (root / ".archived").unlink()
    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])

    assert not (root / "beta" / "spec.md").exists()
    assert _archive_entries(workspace) == ["beta"]
    assert _map(workspace)["deleted"] == {}


# ---------------------------------------------------------------------------
# CM4: workstream writes never follow a link, not even to change ownership
# ---------------------------------------------------------------------------


def test_workstream_writes_use_descriptors_not_paths(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Any path-based chown (they follow links) is recorded, not just the
    # writer's own helper; config sync must use fchown throughout.
    path_chowns: list[str] = []
    monkeypatch.setattr(
        os, "chown", lambda path, *args, **kwargs: path_chowns.append(str(path))
    )
    descriptor_chowns: list[int] = []
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: descriptor_chowns.append(fd))

    ClaudeMdWriter(str(workspace)).sync_workstream_directories([_ws(ALPHA, "Alpha")])

    assert path_chowns == []
    # The workstreams root, the workstream directory and the new CLAUDE.md.
    assert len(descriptor_chowns) >= 3
    assert (workspace / "workstreams" / "alpha" / "CLAUDE.md").exists()


def test_link_at_workstream_directory_is_never_written_through(
    workspace: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = workspace / "workstreams"
    root.mkdir()
    (root / "alpha").symlink_to(outside)

    ClaudeMdWriter(str(workspace)).sync_workstream_directories(
        [_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")]
    )

    assert list(outside.iterdir()) == []
    assert (root / "alpha").is_symlink()
    assert (root / "beta" / "CLAUDE.md").exists()


def test_link_at_claude_md_is_replaced_not_followed(
    workspace: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "target.md"
    outside.write_text("host file")
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    claude_md = workspace / "workstreams" / "alpha" / "CLAUDE.md"
    claude_md.unlink()
    claude_md.symlink_to(outside)

    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])

    assert outside.read_text() == "host file"
    assert not claude_md.is_symlink()
    assert "# Workstream: Alpha" in claude_md.read_text()


# ---------------------------------------------------------------------------
# MV2: a backend rollback keeps the declared layout
# ---------------------------------------------------------------------------


def test_backend_rollback_keeps_the_declared_layout(workspace: Path) -> None:
    """A rolled-back backend declares no ``workspace_dir``. A workstream the
    newer backend already moved to ``ws-<code>`` stays there (its registered
    paths were rewritten and are never rewritten back); nothing is moved back
    into, or archived from, the shared legacy ``office`` directory."""
    root = workspace / "workstreams"
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories(
        [_ws(ALPHA, "Продажі", "PR"), _ws(BETA, "Маркетинг", "MA")]
    )
    (root / "ws-pr" / "spec.md").write_text("pr spec")
    (root / "ws-ma" / "tasks").mkdir()
    (root / "ws-ma" / "tasks" / "out.txt").write_text("ma work")
    assert _map(workspace)["declared"] == {ALPHA: "ws-pr", BETA: "ws-ma"}
    assert _map(workspace)["legacy"] == {ALPHA: "office", BETA: "office"}

    writer.sync_workstream_directories(
        [_legacy_ws(ALPHA, "Продажі", "PR"), _legacy_ws(BETA, "Маркетинг", "MA")]
    )

    assert (root / "ws-pr" / "spec.md").read_text() == "pr spec"
    assert (root / "ws-ma" / "tasks" / "out.txt").read_text() == "ma work"
    assert "# Workstream: Продажі" in (root / "ws-pr" / "CLAUDE.md").read_text()
    # Both resolve to the shared legacy ``office``: neither gets its
    # CLAUDE.md written there (each would carry the other's instructions).
    assert not (root / "office").exists()
    assert _archive_entries(workspace) == []
    assert _map(workspace)["workstreams"] == {ALPHA: "ws-pr", BETA: "ws-ma"}
    assert _map(workspace)["declared"] == {ALPHA: "ws-pr", BETA: "ws-ma"}


def test_backend_rollback_writes_claude_md_where_workers_are_pointed(
    workspace: Path,
) -> None:
    """WSD-2: a rolled-back backend sends task payloads without
    ``workspace_dir``, so workers are pointed at the legacy ``office``
    directory. The kept workstream's CLAUDE.md is written there too, and on
    re-upgrade that copy merges away with nothing archived."""
    from src.orchestrator.workstream_identity import workstream_directory_for_task

    root = workspace / "workstreams"
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Продажі", "PR")])

    writer.sync_workstream_directories([_legacy_ws(ALPHA, "Продажі", "PR")])

    old_backend_task = {
        "workstream_id": ALPHA,
        "workstream_context": {"name": "Продажі", "short_code": "PR"},
    }
    directory = workstream_directory_for_task(old_backend_task)
    assert directory == "office"
    assert "# Workstream: Продажі" in (root / directory / "CLAUDE.md").read_text()
    assert "# Workstream: Продажі" in (root / "ws-pr" / "CLAUDE.md").read_text()
    assert _archive_entries(workspace) == []

    # The backend is upgraded again and declares ws-pr.
    writer.sync_workstream_directories([_ws(ALPHA, "Продажі", "PR")])

    assert not (root / "office").exists()
    assert _archive_entries(workspace) == []
    assert _map(workspace)["workstreams"] == {ALPHA: "ws-pr"}


def test_full_disk_on_the_kept_legacy_claude_md_fails_the_sync(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B4-bugs-1: the copy workers of a rolled-back backend read (the legacy
    ``office`` directory) is retried too when the disk is full, instead of
    being logged as retried while no sync is scheduled."""
    import errno as errno_module

    from src.config_sync import workstream_dirs

    root = workspace / "workstreams"
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Продажі", "PR")])
    original = workstream_dirs.atomic_replace_file

    def full_disk_in_office(directory_fd, filename, content, *args, **kwargs):
        office = root / "office"
        if office.is_dir() and os.path.samestat(os.fstat(directory_fd), office.stat()):
            raise OSError(errno_module.ENOSPC, "No space left on device")
        return original(directory_fd, filename, content, *args, **kwargs)

    monkeypatch.setattr(workstream_dirs, "atomic_replace_file", full_disk_in_office)

    with pytest.raises(OSError) as raised:
        writer.sync_workstream_directories([_legacy_ws(ALPHA, "Продажі", "PR")])

    assert raised.value.errno == errno_module.ENOSPC
    assert "legacy workstream directory office" in str(raised.value)
    assert "# Workstream: Продажі" in (root / "ws-pr" / "CLAUDE.md").read_text()


def test_backend_rollback_keeps_the_legacy_projection_directory(
    workspace: Path,
) -> None:
    """The rolled-back backend writes new projections to the legacy
    directory; the daemon keeps it out of the orphan sweep, and a single
    owner's merges forward once the layout is declared again."""
    root = workspace / "workstreams"
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Продажі", "PR")])
    (root / "ws-pr" / "spec.md").write_text("declared spec")

    writer.sync_workstream_directories([_legacy_ws(ALPHA, "Продажі", "PR")])
    # The older backend materialises into its legacy directory.
    (root / "office" / "intake").mkdir(parents=True)
    (root / "office" / "intake" / "001-scope.json").write_text("{}")
    (root / "office" / "spec.md").write_text("rollback spec")
    writer.sync_workstream_directories([_legacy_ws(ALPHA, "Продажі", "PR")])

    assert (root / "office" / "spec.md").read_text() == "rollback spec"
    assert _archive_entries(workspace) == []

    # The backend is upgraded again and declares ws-pr.
    writer.sync_workstream_directories([_ws(ALPHA, "Продажі", "PR")])

    assert not (root / "office").exists()
    assert (root / "ws-pr" / "intake" / "001-scope.json").exists()
    assert (root / "ws-pr" / "spec.md").read_text() == "declared spec"
    # The conflicting rollback-window spec is archived, never deleted.
    [archived] = _archive_entries(workspace)
    assert (root / ".archived" / archived / "spec.md").read_text() == "rollback spec"


def test_rollback_window_content_never_moves_with_a_workstream_created_then(
    workspace: Path,
) -> None:
    """MV2: the legacy ``office`` directory a rolled-back backend wrote a kept
    workstream's projections to is not carried off by another non-Latin
    workstream created during the rollback. On re-upgrade it moves with
    neither (mixed content) and is archived, never adopted."""
    root = workspace / "workstreams"
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Продажі", "PR")])
    writer.sync_workstream_directories([_legacy_ws(ALPHA, "Продажі", "PR")])
    # The older backend writes Продажі's projections to office/.
    (root / "office" / "intake").mkdir(parents=True)
    (root / "office" / "intake" / "001-a.json").write_text("{}")
    (root / "office" / "tasks" / "ta").mkdir(parents=True)
    (root / "office" / "tasks" / "ta" / "a.txt").write_text("a work")
    # Маркетинг is created while the backend is still rolled back.
    writer.sync_workstream_directories(
        [_legacy_ws(ALPHA, "Продажі", "PR"), _legacy_ws(BETA, "Маркетинг", "MA")]
    )
    assert _map(workspace)["kept_legacy"] == {ALPHA: "office"}

    # The backend is upgraded again and declares ws-pr and ws-ma.
    writer.sync_workstream_directories(
        [_ws(ALPHA, "Продажі", "PR"), _ws(BETA, "Маркетинг", "MA")]
    )

    assert sorted(p.name for p in (root / "ws-ma").iterdir()) == ["CLAUDE.md"]
    assert not (root / "office").exists()
    [archived] = _archive_entries(workspace)
    assert (root / ".archived" / archived / "intake" / "001-a.json").exists()
    assert (root / ".archived" / archived / "tasks" / "ta" / "a.txt").exists()
    assert _map(workspace)["kept_legacy"] == {}


def _rollback_window_with_a_second_non_latin_workstream(
    workspace: Path, writer: ClaudeMdWriter
) -> Path:
    """Продажі kept at ws-pr during a backend rollback (projections in
    office/), and Маркетинг created at office meanwhile with its own work."""
    root = workspace / "workstreams"
    writer.sync_workstream_directories([_ws(ALPHA, "Продажі", "PR")])
    writer.sync_workstream_directories([_legacy_ws(ALPHA, "Продажі", "PR")])
    (root / "office" / "intake").mkdir(parents=True)
    (root / "office" / "intake" / "001-a.json").write_text("{}")
    writer.sync_workstream_directories(
        [_legacy_ws(ALPHA, "Продажі", "PR"), _legacy_ws(BETA, "Маркетинг", "MA")]
    )
    (root / "office" / "tasks" / "tb").mkdir(parents=True)
    (root / "office" / "tasks" / "tb" / "b.txt").write_text("b work")
    (root / "office" / "learnings.md").write_text("b lessons")
    return root


def _assert_office_archived_not_adopted(workspace: Path, root: Path) -> None:
    assert not (root / "ws-pr" / "tasks").exists()
    assert not (root / "ws-pr" / "learnings.md").exists()
    assert not (root / "office").exists()
    [archived] = _archive_entries(workspace)
    assert (root / ".archived" / archived / "intake" / "001-a.json").exists()
    assert (root / ".archived" / archived / "tasks" / "tb" / "b.txt").exists()
    saved = _map(workspace)
    assert "office" not in saved["retired"]
    assert saved["kept_legacy"] == {} and saved["shared_legacy"] == []


def test_rollback_window_directory_renamed_away_is_archived_not_adopted(
    workspace: Path,
) -> None:
    """KEPT-LEGACY rename: Маркетинг renamed to a Latin name during the
    rollback leaves office/ (mixed content) behind; on re-upgrade it is
    archived, never merged into the kept workstream's ws-pr."""
    writer = ClaudeMdWriter(str(workspace))
    root = _rollback_window_with_a_second_non_latin_workstream(workspace, writer)
    writer.sync_workstream_directories(
        [_legacy_ws(ALPHA, "Продажі", "PR"), _legacy_ws(BETA, "Marketing", "MA")]
    )
    assert "office" not in _map(workspace)["retired"]
    assert _map(workspace)["shared_legacy"] == ["office"]

    writer.sync_workstream_directories(
        [_ws(ALPHA, "Продажі", "PR"), _ws(BETA, "Marketing", "MA")]
    )

    _assert_office_archived_not_adopted(workspace, root)


def test_rollback_window_directory_of_a_deleted_workstream_is_not_adopted(
    workspace: Path,
) -> None:
    """KEPT-LEGACY-ADOPT: Маркетинг deleted during the rollback; its content
    in office/ is archived on re-upgrade, never merged into ws-pr."""
    writer = ClaudeMdWriter(str(workspace))
    root = _rollback_window_with_a_second_non_latin_workstream(workspace, writer)
    writer.sync_workstream_directories([_legacy_ws(ALPHA, "Продажі", "PR")])
    assert "office" not in _map(workspace)["retired"]

    writer.sync_workstream_directories([_ws(ALPHA, "Продажі", "PR")])

    _assert_office_archived_not_adopted(workspace, root)


def test_a_legacy_directory_gaining_a_second_kept_workstream_loses_its_claude_md(
    workspace: Path,
) -> None:
    """KEPT-LEGACY-STALE-MD: Marketing renamed to a non-Latin name during a
    backend rollback also resolves to office/; the CLAUDE.md written there
    for Продажі alone is removed, so Маркетинг's workers never read it."""
    root = workspace / "workstreams"
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories(
        [_ws(ALPHA, "Продажі", "PR"), _ws(BETA, "Marketing", "MA")]
    )
    writer.sync_workstream_directories(
        [_legacy_ws(ALPHA, "Продажі", "PR"), _legacy_ws(BETA, "Marketing", "MA")]
    )
    assert (
        (root / "office" / "CLAUDE.md").read_text().startswith("# Workstream: Продажі")
    )

    writer.sync_workstream_directories(
        [_legacy_ws(ALPHA, "Продажі", "PR"), _legacy_ws(BETA, "Маркетинг", "MA")]
    )

    assert not (root / "office" / "CLAUDE.md").exists()
    assert (
        (root / "ws-ma" / "CLAUDE.md").read_text().startswith("# Workstream: Маркетинг")
    )


def test_backend_rollback_renames_follow_the_declared_layout(
    workspace: Path,
) -> None:
    root = workspace / "workstreams"
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    (root / "alpha" / "spec.md").write_text("spec")

    writer.sync_workstream_directories([_legacy_ws(ALPHA, "Beta")])

    assert (root / "beta" / "spec.md").read_text() == "spec"
    assert not (root / "alpha").exists()
    assert _map(workspace)["declared"] == {ALPHA: "beta"}


def test_workstream_created_under_a_rolled_back_backend_uses_the_legacy_layout(
    workspace: Path,
) -> None:
    root = workspace / "workstreams"
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])

    writer.sync_workstream_directories(
        [_legacy_ws(ALPHA, "Alpha"), _legacy_ws(BETA, "Продажі", "PR")]
    )

    assert (root / "office" / "CLAUDE.md").exists()
    assert not (root / "ws-pr").exists()
    assert _map(workspace)["declared"] == {ALPHA: "alpha"}


# -- Adoption check: a directory another workstream's CLAUDE.md names -------


def _break_archive(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".archived").write_text("not a directory")


def _repair_archive(root: Path) -> None:
    (root / ".archived").unlink()


def test_claude_md_carries_the_workstream_id_and_is_read_back(workspace: Path) -> None:
    from src.config_sync.claude_md_templates import generate_workstream_claude_md
    from src.config_sync.workstream_dirs import (
        directory_attribution,
        open_workstreams_root,
    )

    content = generate_workstream_claude_md(_ws(ALPHA, "Alpha", "AL"))
    assert content.splitlines()[:2] == [
        "# Workstream: Alpha",
        f"<!-- workstream-id: {ALPHA} -->",
    ]
    assert "workstream-id" not in generate_workstream_claude_md({"name": "No id"})
    ClaudeMdWriter(str(workspace)).sync_workstream_directories(
        [_ws(ALPHA, "Alpha", "AL")]
    )
    with open_workstreams_root(workspace) as root_fd:
        assert directory_attribution(root_fd, "alpha") == (ALPHA, "AL")


@pytest.mark.parametrize("aside", ["moved", "blocked"])
@pytest.mark.parametrize("reuse_code", [False, True])
def test_unrecorded_directory_of_a_deleted_workstream_is_archived_not_adopted(
    workspace: Path, reuse_code: bool, aside: str
) -> None:
    """Model seed 21: the map was set aside (a rollback) before the deletion
    was processed, and the deleted workstream's orphan could not be
    archived; that sync records a claim for it (under the id its CLAUDE.md
    names). A NEW workstream with the same name (and, as the backend may
    assign, the same short code) never adopts it:
    it is moved aside to ``.deleted-<id>`` (or, when even that fails, the
    name is frozen while the archive fails) and archived once it works."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Gamma", "GA"), _ws(BETA, "Beta")])
    root = workspace / "workstreams"
    (root / "gamma" / "tasks").mkdir()
    (root / "gamma" / "tasks" / "old.txt").write_text("deleted gamma work")
    (workspace / MAP_DIRNAME / MAP_FILENAME).unlink()
    _break_archive(root)
    if aside == "blocked":
        _block_aside(root, ALPHA)
    writer.sync_workstream_directories([_ws(BETA, "Beta")])  # the orphan stays
    assert (root / "gamma" / "tasks" / "old.txt").exists()
    # The failed archive leaves a claim under the id its CLAUDE.md names.
    assert _map(workspace)["deleted"] == {ALPHA: "gamma"}
    assert _map(workspace)["workstreams"] == {BETA: "beta"}

    code = "GA" if reuse_code else "GM"
    writer.sync_workstream_directories([_ws(BETA, "Beta"), _ws(GAMMA, "Gamma", code)])

    if aside == "moved":
        moved = root / f"{DELETED_PREFIX}{ALPHA}"
        assert (moved / "tasks" / "old.txt").exists()
        assert _names(root / "gamma") == ["CLAUDE.md"]
        assert f"workstream-id: {GAMMA}" in (root / "gamma" / "CLAUDE.md").read_text()
        assert _map(workspace)["deleted"] == {ALPHA: moved.name}
        assert _map(workspace)["workstreams"][GAMMA] == "gamma"
    else:
        assert f"workstream-id: {ALPHA}" in (root / "gamma" / "CLAUDE.md").read_text()
        assert GAMMA not in _map(workspace)["workstreams"]

    _repair_archive(root)
    writer.sync_workstream_directories([_ws(BETA, "Beta"), _ws(GAMMA, "Gamma", code)])

    assert sorted(p.name for p in (root / "gamma").iterdir()) == ["CLAUDE.md"]
    assert f"workstream-id: {GAMMA}" in (root / "gamma" / "CLAUDE.md").read_text()
    [archived] = _archive_entries(workspace)
    assert (root / ".archived" / archived / "tasks" / "old.txt").exists()
    assert _map(workspace)["workstreams"][GAMMA] == "gamma"
    assert _map(workspace)["deleted"] == {}
    assert not (root / f"{DELETED_PREFIX}{ALPHA}").is_dir()


def test_an_orphan_the_sweep_could_not_archive_is_never_adopted(
    workspace: Path,
) -> None:
    """Writes at a name the map does not know (an operator restored an older
    map) are nobody's: the sweep archives them, but the archive fails. A
    workstream that takes the name later gets a fresh directory; the orphan
    is moved aside (a claim like a deleted workstream's), then archived once
    the archive works."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    root = workspace / "workstreams"
    (root / "beta" / "tasks").mkdir(parents=True)
    (root / "beta" / "tasks" / "late.txt").write_text("someone's late write")
    _break_archive(root)
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    assert (root / "beta" / "tasks" / "late.txt").exists()
    assert list(_map(workspace)["deleted"].values()) == ["beta"]

    rows = [_ws(ALPHA, "Alpha"), _ws(GAMMA, "Beta", "BE")]
    writer.sync_workstream_directories(rows)

    assert _names(root / "beta") == ["CLAUDE.md"]
    assert f"workstream-id: {GAMMA}" in (root / "beta" / "CLAUDE.md").read_text()
    [aside] = [p for p in root.iterdir() if p.name.startswith(DELETED_PREFIX)]
    assert (aside / "tasks" / "late.txt").read_text() == "someone's late write"

    _repair_archive(root)
    writer.sync_workstream_directories(rows)

    [archived] = _archive_entries(workspace)
    assert (root / ".archived" / archived / "tasks" / "late.txt").exists()
    assert not aside.exists()
    assert _map(workspace)["deleted"] == {}
    assert _map(workspace)["workstreams"][GAMMA] == "beta"


def test_without_a_map_a_deleted_workstreams_directory_is_not_adopted(
    workspace: Path,
) -> None:
    """Model seed 55: the map was set aside (a rollback) while a deletion was
    unprocessed; the directory at a new workstream's name is the deleted
    one's. Without a map its CLAUDE.md decides: archived, not adopted."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Office", "OF")])
    root = workspace / "workstreams"
    (root / "office" / "tasks").mkdir()
    (root / "office" / "tasks" / "old.txt").write_text("deleted office work")
    (workspace / MAP_DIRNAME / MAP_FILENAME).unlink()

    writer.sync_workstream_directories([_ws(BETA, "Office", "OF")])

    assert sorted(p.name for p in (root / "office").iterdir()) == ["CLAUDE.md"]
    [archived] = _archive_entries(workspace)
    assert (root / ".archived" / archived / "tasks" / "old.txt").exists()
    assert _map(workspace)["workstreams"] == {BETA: "office"}


def test_without_a_map_a_legacy_guess_naming_another_workstream_is_not_moved(
    workspace: Path,
) -> None:
    """Without a map a non-Latin workstream's previous directory is guessed
    to be ``office``; when that directory's CLAUDE.md names a deleted
    workstream the guess is dropped and nothing moves into ws-ma."""
    root = workspace / "workstreams"
    (root / "office" / "tasks").mkdir(parents=True)
    (root / "office" / "tasks" / "old.txt").write_text("someone else's")
    (root / "office" / "CLAUDE.md").write_text(
        f"# Workstream: Office\n<!-- workstream-id: {GAMMA} -->\n\n"
        "**Short code:** `OF` · **Priority:** `medium`\n"
    )

    ClaudeMdWriter(str(workspace)).sync_workstream_directories(
        [_ws(ALPHA, "Маркетинг", "MA")]
    )

    assert sorted(p.name for p in (root / "ws-ma").iterdir()) == ["CLAUDE.md"]
    [archived] = _archive_entries(workspace)
    assert (root / ".archived" / archived / "tasks" / "old.txt").exists()
    assert _map(workspace)["workstreams"] == {ALPHA: "ws-ma"}


def test_a_pre_marker_claude_md_naming_no_current_short_code_is_not_adopted(
    workspace: Path,
) -> None:
    """A CLAUDE.md from before the id marker names only a short code; one no
    current workstream has is a deleted workstream's."""
    root = workspace / "workstreams"
    _older_daemon_claude_md(root, "Gamma", "GX")
    (root / "gamma" / "spec.md").write_text("old spec")

    ClaudeMdWriter(str(workspace)).sync_workstream_directories(
        [_ws(ALPHA, "Gamma", "GA")]
    )

    assert not (root / "gamma" / "spec.md").exists()
    [archived] = _archive_entries(workspace)
    assert (root / ".archived" / archived / "spec.md").read_text() == "old spec"


def test_a_pre_marker_claude_md_with_the_claimants_short_code_is_its_own(
    workspace: Path,
) -> None:
    """The upgrade path: an older daemon's directory for this workstream."""
    root = workspace / "workstreams"
    _older_daemon_claude_md(root, "Gamma", "GA")
    (root / "gamma" / "spec.md").write_text("own spec")

    ClaudeMdWriter(str(workspace)).sync_workstream_directories(
        [_ws(ALPHA, "Gamma", "GA")]
    )

    assert (root / "gamma" / "spec.md").read_text() == "own spec"
    assert _archive_entries(workspace) == []


def test_an_unreadable_claude_md_blocks_the_name_without_archiving(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uncertain ownership fails closed: nothing is archived or adopted, the
    workstream is not recorded there and it is retried on the next sync."""
    from src.config_sync import workstream_dirs

    root = workspace / "workstreams"
    (root / "gamma").mkdir(parents=True)
    (root / "gamma" / "spec.md").write_text("unknown spec")
    (root / "gamma" / "CLAUDE.md").write_text("# Workstream: Gamma\n")

    def unreadable(root_fd: int, name: str) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(workstream_dirs, "directory_attribution", unreadable)
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Gamma", "GA")])

    assert (root / "gamma" / "CLAUDE.md").read_text() == "# Workstream: Gamma\n"
    assert (root / "gamma" / "spec.md").exists()
    assert _archive_entries(workspace) == []
    assert ALPHA not in _map(workspace)["workstreams"]

    monkeypatch.undo()
    writer.sync_workstream_directories([_ws(ALPHA, "Gamma", "GA")])
    assert f"workstream-id: {ALPHA}" in (root / "gamma" / "CLAUDE.md").read_text()
    assert _map(workspace)["workstreams"] == {ALPHA: "gamma"}


# -- R4-SEC-3: a regenerable-only removal never deletes a late file ----------


def _retire_with_hook(workspace: Path, monkeypatch: pytest.MonkeyPatch, hook) -> Path:
    from src.config_sync import workstream_dirs

    root = workspace / "workstreams"
    (root / "alpha").mkdir(parents=True)
    (root / "alpha" / "CLAUDE.md").write_text("# Workstream: Alpha\n")
    hook(workstream_dirs, root / "alpha")
    with workstream_dirs.open_workstreams_root(workspace) as root_fd:
        removed = workstream_dirs.retire_directory(root_fd, "alpha", "alpha")
    assert removed is False
    return root


def test_a_file_written_after_the_regenerable_check_is_archived_not_deleted(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def hook(module, directory: Path) -> None:
        real = module.only_regenerable

        def checked_then_written(parent_fd: int, name: str) -> bool:
            result = real(parent_fd, name)
            (directory / "spec.md").write_text("late spec")
            return result

        monkeypatch.setattr(module, "only_regenerable", checked_then_written)

    root = _retire_with_hook(workspace, monkeypatch, hook)

    [archived] = _archive_entries(workspace)
    assert (root / ".archived" / archived / "spec.md").read_text() == "late spec"


def test_a_file_written_after_the_unlink_is_archived_not_deleted(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def hook(module, directory: Path) -> None:
        real = os.unlink

        def unlink_then_written(name, *, dir_fd=None):
            real(name, dir_fd=dir_fd)
            (directory / "late.txt").write_text("late work")

        monkeypatch.setattr(module.os, "unlink", unlink_then_written)

    root = _retire_with_hook(workspace, monkeypatch, hook)
    monkeypatch.undo()

    [archived] = _archive_entries(workspace)
    assert (root / ".archived" / archived / "late.txt").read_text() == "late work"


# -- R4: the directory's CLAUDE.md can only veto -----------------------------


def _map_path(workspace: Path) -> Path:
    return workspace / MAP_DIRNAME / MAP_FILENAME


def _sync_losing_the_map(workspace: Path, rows: list[dict]) -> None:
    """A sync whose map update is lost entirely (the map file afterwards is
    the one before it: no final save, no write-ahead record)."""
    before = _map_path(workspace).read_bytes()
    ClaudeMdWriter(str(workspace)).sync_workstream_directories(rows)
    _map_path(workspace).write_bytes(before)


def _names(directory: Path) -> list[str]:
    return sorted(
        str(p.relative_to(directory)) for p in directory.rglob("*") if p.is_file()
    )


def test_a_swap_replayed_from_a_stale_map_is_vetoed(workspace: Path) -> None:
    """R4-STALE-CURRENT-SWAP (swap)."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")])
    root = workspace / "workstreams"
    (root / "alpha" / "a.txt").write_text("a")
    (root / "beta" / "b.txt").write_text("b")
    swapped = [_ws(ALPHA, "Beta"), _ws(BETA, "Alpha", "BE")]
    _sync_losing_the_map(workspace, swapped)

    writer.sync_workstream_directories(swapped)
    writer.sync_workstream_directories(swapped)

    assert _names(root / "alpha") == ["CLAUDE.md", "b.txt"]
    assert _names(root / "beta") == ["CLAUDE.md", "a.txt"]
    assert f"workstream-id: {BETA}" in (root / "alpha" / "CLAUDE.md").read_text()
    assert _map(workspace)["workstreams"] == {ALPHA: "beta", BETA: "alpha"}


def test_a_chain_replayed_from_a_stale_map_is_vetoed(workspace: Path) -> None:
    """R4-STALE-CURRENT-SWAP (chain)."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")])
    root = workspace / "workstreams"
    (root / "alpha" / "a.txt").write_text("a")
    (root / "beta" / "b.txt").write_text("b")
    chained = [_ws(ALPHA, "Beta"), _ws(BETA, "Gamma", "BE")]
    _sync_losing_the_map(workspace, chained)

    writer.sync_workstream_directories(chained)

    assert _names(root / "beta") == ["CLAUDE.md", "a.txt"]
    assert _names(root / "gamma") == ["CLAUDE.md", "b.txt"]
    assert _archive_entries(workspace) == []
    assert _map(workspace)["workstreams"] == {ALPHA: "beta", BETA: "gamma"}


def test_a_stale_current_claim_never_archives_the_new_workstreams_directory(
    workspace: Path,
) -> None:
    """R4-STALE-CURRENT-CLAIM: D is deleted and a new same-slug N created in
    one sync whose map update is lost; D's claim read again from `current`
    names N's live directory, whose CLAUDE.md names N."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Marketing", "MA")])
    root = workspace / "workstreams"
    (root / "marketing" / "d.txt").write_text("d")
    _sync_losing_the_map(workspace, [_ws(BETA, "Marketing", "MB")])
    (root / "marketing" / "tasks").mkdir()
    (root / "marketing" / "tasks" / "n.txt").write_text("n")
    (root / "marketing" / "spec.md").write_text("n spec")

    for _ in range(2):
        writer.sync_workstream_directories([_ws(BETA, "Marketing", "MB")])

    assert _names(root / "marketing") == ["CLAUDE.md", "spec.md", "tasks/n.txt"]
    [archived] = _archive_entries(workspace)
    assert _names(root / ".archived" / archived) == ["CLAUDE.md", "d.txt"]
    assert _map(workspace)["workstreams"] == {BETA: "marketing"}


def test_a_forged_map_entry_never_archives_a_live_directory(
    workspace: Path,
) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")])
    root = workspace / "workstreams"
    (root / "beta" / "b.txt").write_text("b")
    forged = _map(workspace)
    forged["workstreams"] = {ALPHA: "alpha", GAMMA: "beta"}
    _map_path(workspace).write_text(json.dumps(forged))

    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")])

    assert _names(root / "beta") == ["CLAUDE.md", "b.txt"]
    assert _archive_entries(workspace) == []


def test_a_deleted_claim_with_a_reused_inode_is_vetoed_by_the_marker(
    workspace: Path,
) -> None:
    """R4-IDENTITY-INODE-REUSE: the recorded identity matches (the inode was
    freed and reused by the new workstream's directory) but the directory's
    CLAUDE.md names that current workstream: the claim is stale."""
    from src.config_sync.workstream_dirs import directory_identity

    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(BETA, "Beta", "BE")])
    root = workspace / "workstreams"
    (root / "beta" / "tasks").mkdir()
    (root / "beta" / "tasks" / "g.txt").write_text("gamma work")
    reused = _map(workspace)
    info = os.stat(root / "beta")
    reused["workstreams"] = {}
    reused["deleted"] = {ALPHA: "beta"}
    reused["deleted_identity"] = {ALPHA: [info.st_dev, info.st_ino]}
    _map_path(workspace).write_text(json.dumps(reused))
    # The marker names BETA; make it the claimant of the reused name.
    (root / "beta" / "CLAUDE.md").write_text(
        (root / "beta" / "CLAUDE.md").read_text().replace(BETA, GAMMA)
    )
    with open_root(workspace) as root_fd:
        assert directory_identity("beta", root_fd) == (info.st_dev, info.st_ino)

    writer.sync_workstream_directories([_ws(GAMMA, "Beta", "CE")])

    assert _names(root / "beta") == ["CLAUDE.md", "tasks/g.txt"]
    assert _archive_entries(workspace) == []
    assert _map(workspace)["deleted"] == {}


def open_root(workspace: Path):
    from src.config_sync.workstream_dirs import open_workstreams_root

    return open_workstreams_root(workspace)


@pytest.mark.parametrize("claimant", ["new", "renamed"])
def test_late_writes_at_a_retired_name_are_merged_forward_before_it_is_taken(
    workspace: Path, claimant: str
) -> None:
    """R4-RETIRED-LATE-WRITE-ADOPTED: a session that started before Alpha
    was renamed writes into its old name; a workstream that takes the name
    next never adopts those writes."""
    writer = ClaudeMdWriter(str(workspace))
    rows = [_ws(ALPHA, "Alpha")]
    if claimant == "renamed":
        rows.append(_ws(BETA, "Beta", "BE"))
    writer.sync_workstream_directories(rows)
    root = workspace / "workstreams"
    if claimant == "renamed":
        (root / "beta" / "b.txt").write_text("b")
    rows[0] = _ws(ALPHA, "Alpha Old")
    writer.sync_workstream_directories(rows)
    (root / "alpha" / "tasks" / "t2").mkdir(parents=True)
    (root / "alpha" / "tasks" / "t2" / "a2.txt").write_text("late a")

    if claimant == "new":
        rows.append(_ws(GAMMA, "Alpha", "CE"))
    else:
        rows[1] = _ws(BETA, "Alpha", "BE")
    writer.sync_workstream_directories(rows)

    assert (root / "alpha-old" / "tasks" / "t2" / "a2.txt").read_text() == "late a"
    assert "tasks/t2/a2.txt" not in _names(root / "alpha")
    if claimant == "renamed":
        assert _names(root / "alpha") == ["CLAUDE.md", "b.txt"]


def test_a_stale_retired_name_holding_another_workstreams_content_is_not_merged(
    workspace: Path,
) -> None:
    """R4-SEC-5 (sweep): Alpha is renamed to Delta (alpha retired to it); a
    new Alpha is laid out at alpha but the map update is lost, then it is
    renamed away. alpha's CLAUDE.md names the new workstream, so it is not
    merged into Delta."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    writer.sync_workstream_directories([_ws(ALPHA, "Delta")])
    root = workspace / "workstreams"
    _sync_losing_the_map(workspace, [_ws(ALPHA, "Delta"), _ws(BETA, "Alpha", "BE")])
    (root / "alpha" / "b.txt").write_text("b")

    writer.sync_workstream_directories([_ws(ALPHA, "Delta"), _ws(BETA, "Gamma", "BE")])

    assert "b.txt" not in _names(root / "delta")


def test_another_current_workstreams_stale_copy_is_archived_not_adopted(
    workspace: Path,
) -> None:
    """A directory whose id marker names another current workstream, where the
    map records nobody (an operator restored an older map), is never adopted.
    It is archived first, as the orphan sweep archives it when nobody claims
    the name: waiting would keep the new workstream out of its directory for
    ever."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    root = workspace / "workstreams"
    (root / "beta").mkdir()
    (root / "beta" / "CLAUDE.md").write_text((root / "alpha" / "CLAUDE.md").read_text())
    (root / "beta" / "x.txt").write_text("alpha's stray copy")
    rows = [_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")]

    writer.sync_workstream_directories(rows)

    [archived] = _archive_entries(workspace)
    assert _names(root / ".archived" / archived) == ["CLAUDE.md", "x.txt"]
    assert _names(root / "beta") == ["CLAUDE.md"]
    writer.sync_workstream_directories(rows)
    assert _names(root / "beta") == ["CLAUDE.md"]
    assert f"workstream-id: {BETA}" in (root / "beta" / "CLAUDE.md").read_text()
    assert _map(workspace)["workstreams"] == {ALPHA: "alpha", BETA: "beta"}


def test_another_current_workstreams_stale_copy_waits_while_the_archive_fails(
    workspace: Path,
) -> None:
    """It is not moved aside either: a ``.deleted-<id>`` claim is only ever a
    deleted workstream's."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    root = workspace / "workstreams"
    (root / "beta").mkdir()
    (root / "beta" / "CLAUDE.md").write_text((root / "alpha" / "CLAUDE.md").read_text())
    (root / "beta" / "x.txt").write_text("alpha's stray copy")
    _break_archive(root)
    rows = [_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")]

    writer.sync_workstream_directories(rows)

    assert _names(root / "beta") == ["CLAUDE.md", "x.txt"]
    assert f"workstream-id: {ALPHA}" in (root / "beta" / "CLAUDE.md").read_text()
    assert not (root / f"{DELETED_PREFIX}{ALPHA}").exists()
    assert _map(workspace)["deleted"] == {}
    assert BETA not in _map(workspace)["workstreams"]

    _repair_archive(root)
    writer.sync_workstream_directories(rows)
    writer.sync_workstream_directories(rows)
    [archived] = _archive_entries(workspace)
    assert _names(root / ".archived" / archived) == ["CLAUDE.md", "x.txt"]
    assert f"workstream-id: {BETA}" in (root / "beta" / "CLAUDE.md").read_text()


def test_a_copy_the_map_still_records_for_another_workstream_waits(
    workspace: Path,
) -> None:
    """The map records Gamma at beta (a stale entry), but beta's marker names
    Alpha: nothing is archived this pass; Beta waits."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    root = workspace / "workstreams"
    (root / "beta").mkdir()
    (root / "beta" / "CLAUDE.md").write_text((root / "alpha" / "CLAUDE.md").read_text())
    (root / "beta" / "x.txt").write_text("alpha's stray copy")
    recorded = _map(workspace)
    recorded["workstreams"][GAMMA] = "beta"
    _map_path(workspace).write_text(json.dumps(recorded))

    writer.sync_workstream_directories(
        [_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE"), _ws(GAMMA, "Gamma", "GA")]
    )

    assert _names(root / "beta") == ["CLAUDE.md", "x.txt"]
    assert _archive_entries(workspace) == []
    assert BETA not in _map(workspace)["workstreams"]


# -- R4: the map is written ahead of every move ------------------------------


class _Crash(BaseException):
    """The daemon process dying (not an ``Exception`` a sync catches)."""


def _sync_crashing_before_the_save(
    workspace: Path, rows: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sync that makes every move and dies before its final save: the map
    file keeps the last write-ahead record."""
    from src.config_sync.workstream_dirs import WorkstreamDirectoryMap

    with monkeypatch.context() as patched:

        def crash(self, *, strict: bool = False) -> None:
            raise _Crash

        patched.setattr(WorkstreamDirectoryMap, "save", crash)
        with pytest.raises(_Crash):
            ClaudeMdWriter(str(workspace)).sync_workstream_directories(rows)


def _full_disk_for(monkeypatch: pytest.MonkeyPatch, *, records: bool | None) -> None:
    """ENOSPC on the write-ahead records (``records``), on the other map
    writes (the final save), or on every map write (None)."""
    import errno as errno_module

    from src.config_sync.workstream_dirs import WorkstreamDirectoryMap

    original = WorkstreamDirectoryMap.write_payload

    def write_payload(self, payload: dict, *, strict: bool = False) -> None:
        if records is None or ("pending" in payload) == records:
            raise OSError(errno_module.ENOSPC, "No space left on device")
        original(self, payload, strict=strict)

    monkeypatch.setattr(WorkstreamDirectoryMap, "write_payload", write_payload)


def test_each_move_is_recorded_before_it_is_made(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.config_sync.workstream_dirs import WorkstreamDirectoryMap

    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    root = workspace / "workstreams"
    (root / "alpha" / "a.txt").write_text("a")
    seen: list[tuple[list[dict], set[str]]] = []
    original = WorkstreamDirectoryMap.write_payload

    def spy(self, payload: dict, *, strict: bool = False) -> None:
        seen.append((payload.get("pending", []), {p.name for p in root.iterdir()}))
        original(self, payload, strict=strict)

    monkeypatch.setattr(WorkstreamDirectoryMap, "write_payload", spy)
    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])

    staged = f"{RELOCATING_PREFIX}{ALPHA}"
    stage = {"op": "stage", "owner": ALPHA, "source": "alpha", "target": None}
    [on_disk] = [
        on_disk
        for steps, on_disk in seen
        if steps and {k: steps[-1][k] for k in stage} == stage
    ]
    assert "alpha" in on_disk and staged not in on_disk
    [on_disk] = [
        on_disk
        for steps, on_disk in seen
        if steps and steps[-1]["op"] == "place" and steps[-1]["source"] == staged
    ]
    assert staged in on_disk and "beta" not in on_disk
    assert seen[-1][0] == []  # the final save clears the record
    assert "pending" not in _map(workspace)
    assert _names(root / "beta") == ["CLAUDE.md", "a.txt"]


@pytest.mark.parametrize("change", ["chain", "swap"])
def test_a_sync_that_dies_after_its_moves_is_completed_from_the_record(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")])
    root = workspace / "workstreams"
    (root / "alpha" / "a.txt").write_text("a")
    (root / "beta" / "b.txt").write_text("b")
    alpha_to, beta_to = ("Beta", "Gamma") if change == "chain" else ("Beta", "Alpha")
    rows = [_ws(ALPHA, alpha_to), _ws(BETA, beta_to, "BE")]
    _sync_crashing_before_the_save(workspace, rows, monkeypatch)
    assert _map(workspace)["pending"]

    writer.sync_workstream_directories(rows)
    beta_dir = beta_to.lower()
    assert _names(root / "beta") == ["CLAUDE.md", "a.txt"]
    assert _names(root / beta_dir) == ["CLAUDE.md", "b.txt"]
    assert _map(workspace)["workstreams"] == {ALPHA: "beta", BETA: beta_dir}
    assert "pending" not in _map(workspace)

    # Renamed again: both directories follow, nothing is archived.
    writer.sync_workstream_directories([_ws(ALPHA, "Delta"), _ws(BETA, "Omega", "BE")])
    assert _names(root / "delta") == ["CLAUDE.md", "a.txt"]
    assert _names(root / "omega") == ["CLAUDE.md", "b.txt"]
    assert _archive_entries(workspace) == []


def test_a_new_workstream_laid_out_before_a_lost_save_follows_its_rename(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4-SEC-5 liveness: Alpha is renamed to Delta (alpha retired to it); a
    new Alpha is laid out at alpha by a sync that dies before its save, gets
    content, and is renamed to Gamma. Its content moves to gamma: it is
    neither merged into Delta nor archived."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    writer.sync_workstream_directories([_ws(ALPHA, "Delta")])
    root = workspace / "workstreams"
    _sync_crashing_before_the_save(
        workspace, [_ws(ALPHA, "Delta"), _ws(BETA, "Alpha", "BE")], monkeypatch
    )
    (root / "alpha" / "b.txt").write_text("b")

    writer.sync_workstream_directories([_ws(ALPHA, "Delta"), _ws(BETA, "Gamma", "BE")])

    assert _names(root / "gamma") == ["CLAUDE.md", "b.txt"]
    assert _names(root / "delta") == ["CLAUDE.md"]
    assert _archive_entries(workspace) == []
    assert _map(workspace)["workstreams"] == {ALPHA: "delta", BETA: "gamma"}


def test_a_full_disk_on_the_record_moves_nothing_and_closes_admission(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno as errno_module

    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(GAMMA, "Old", "CE")])
    root = workspace / "workstreams"
    (root / "alpha" / "a.txt").write_text("a")
    (root / "old" / "o.txt").write_text("o")
    before = _map_path(workspace).read_bytes()

    with monkeypatch.context() as patched:
        _full_disk_for(patched, records=None)
        with pytest.raises(OSError) as raised:
            writer.sync_workstream_directories([_ws(ALPHA, "Beta")])
    assert raised.value.errno == errno_module.ENOSPC

    # Nothing moved, merged or archived (GAMMA's directory is not swept).
    assert _names(root / "alpha") == ["CLAUDE.md", "a.txt"]
    assert _names(root / "old") == ["CLAUDE.md", "o.txt"]
    assert _archive_entries(workspace) == []
    assert not any(p.name.startswith(RELOCATING_PREFIX) for p in root.iterdir())
    assert _map_path(workspace).read_bytes() == before

    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])
    assert _names(root / "beta") == ["CLAUDE.md", "a.txt"]
    assert not (root / "alpha").exists()


def test_a_full_disk_on_the_final_save_keeps_the_record_for_the_next_sync(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno as errno_module

    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")])
    root = workspace / "workstreams"
    (root / "alpha" / "a.txt").write_text("a")
    (root / "beta" / "b.txt").write_text("b")
    swapped = [_ws(ALPHA, "Beta"), _ws(BETA, "Alpha", "BE")]

    with monkeypatch.context() as patched:
        _full_disk_for(patched, records=False)
        with pytest.raises(OSError) as raised:
            writer.sync_workstream_directories(swapped)
    assert raised.value.errno == errno_module.ENOSPC
    assert _names(root / "beta") == ["CLAUDE.md", "a.txt"]
    assert _map(workspace)["pending"]
    assert _map(workspace)["workstreams"] == {ALPHA: "alpha", BETA: "beta"}

    writer.sync_workstream_directories([_ws(ALPHA, "Beta"), _ws(BETA, "Gamma", "BE")])

    assert _names(root / "beta") == ["CLAUDE.md", "a.txt"]
    assert _names(root / "gamma") == ["CLAUDE.md", "b.txt"]
    assert _archive_entries(workspace) == []
    assert "pending" not in _map(workspace)


def test_a_session_breaking_the_map_directory_moves_nothing_without_failing(
    workspace: Path,
) -> None:
    """A planted file at ``.cubicle`` is session-caused: nothing moves (the
    map cannot record it) and the sync does not fail."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(GAMMA, "Old", "CE")])
    root = workspace / "workstreams"
    (root / "alpha" / "a.txt").write_text("a")
    (root / "old" / "o.txt").write_text("o")
    _map_path(workspace).unlink()
    (workspace / MAP_DIRNAME).rmdir()
    (workspace / MAP_DIRNAME).write_text("planted")

    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])

    assert _names(root / "alpha") == ["CLAUDE.md", "a.txt"]
    assert _names(root / "old") == ["CLAUDE.md", "o.txt"]
    assert _archive_entries(workspace) == []


def test_an_older_reader_sees_the_map_from_before_the_interrupted_sync(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record is additive: every section an older daemon reads is the map
    as it was before the sync (what a lost save left before), and the new
    sections are ignored by its reader like any unknown key."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")])
    before = _map(workspace)

    _sync_crashing_before_the_save(
        workspace, [_ws(ALPHA, "Gamma"), _ws(BETA, "Delta", "BE")], monkeypatch
    )

    interrupted = _map(workspace)
    rows_sections = ("declared", "legacy")
    assert {
        k: v
        for k, v in interrupted.items()
        if k != "pending" and k not in rows_sections
    } == {k: v for k, v in before.items() if k not in rows_sections}
    assert [step["op"] for step in interrupted["pending"]].count("stage") == 2
    # Where the backend declares each workstream is a fact of the rows, saved
    # ahead of the moves (an older reader uses it only while a rolled-back
    # backend declares nothing).
    assert interrupted["declared"] == {ALPHA: "gamma", BETA: "delta"}
    assert interrupted["legacy"] == {ALPHA: "gamma", BETA: "delta"}


def test_a_forged_record_is_validated_like_the_rest_of_the_map(
    workspace: Path,
) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    root = workspace / "workstreams"
    (root / "alpha" / "a.txt").write_text("a")
    forged = _map(workspace)
    forged["pending"] = [
        {"op": "place", "owner": ALPHA, "source": None, "target": "../../etc"},
        {"op": "retire", "owner": ALPHA, "source": "/tmp", "identity": [1, 2]},
        {"op": "stage", "owner": "not-an-id", "source": "alpha", "identity": None},
        {"op": "unlink", "owner": ALPHA, "source": "alpha"},
        "garbage",
    ]
    _map_path(workspace).write_text(json.dumps(forged))

    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])

    assert _names(root / "alpha") == ["CLAUDE.md", "a.txt"]
    assert _map(workspace)["workstreams"] == {ALPHA: "alpha"}
    assert "pending" not in _map(workspace)


def test_a_removed_inode_reused_at_a_recorded_source_counts_as_moved(
    workspace: Path,
) -> None:
    """A step whose source directory the sync REMOVED (a CLAUDE.md-only
    directory) is complete even when a directory created there since reused
    the inode: the claim is not revived against the new directory."""
    from src.config_sync.workstream_dirs import (
        WorkstreamDirectoryMap,
        directory_identity,
        reconcile_pending,
    )

    root = workspace / "workstreams"
    (root / "beta").mkdir(parents=True)
    (root / "beta" / "n.txt").write_text("the claimant's projection")
    with open_root(workspace) as root_fd:
        identity = list(directory_identity("beta", root_fd))
        dir_map = WorkstreamDirectoryMap(workspace)
        dir_map.deleted = {ALPHA: "beta"}
        dir_map.deleted_identity = {ALPHA: tuple(identity)}
        dir_map.pending = [
            {
                "op": "retire",
                "owner": ALPHA,
                "source": "beta",
                "target": None,
                "identity": identity,
            },
            {
                "op": "removed",
                "owner": None,
                "source": "beta",
                "target": None,
                "identity": identity,
            },
        ]
        assert reconcile_pending(root_fd, dir_map) == 1
    assert dir_map.deleted == {} and dir_map.deleted_identity == {}
    assert dir_map.pending == []


# -- R4-FROZEN-NAME-STILL-TARGETED: a claimed name is freed, not frozen ------


def _deleted_alpha_claimed_by_a_new_workstream(workspace: Path) -> Path:
    """D 'Alpha' (ALPHA) has content; .archived is broken; D is deleted and a
    new N 'Alpha' (BETA) is created."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(GAMMA, "Keep", "KE")])
    root = workspace / "workstreams"
    (root / "alpha" / "tasks").mkdir()
    (root / "alpha" / "tasks" / "d.txt").write_text("d")
    (root / "alpha" / "spec.md").write_text("D's spec")
    _break_archive(root)
    return root


def test_a_deleted_directory_that_cannot_be_archived_is_moved_aside_for_its_claimant(
    workspace: Path,
) -> None:
    """The claimant's CLAUDE.md, backend projections and worker outputs land
    in a directory of its own; the deleted workstream's is archived whole."""
    root = _deleted_alpha_claimed_by_a_new_workstream(workspace)
    writer = ClaudeMdWriter(str(workspace))
    rows = [_ws(BETA, "Alpha", "NB"), _ws(GAMMA, "Keep", "KE")]

    writer.sync_workstream_directories(rows)

    aside = root / f"{DELETED_PREFIX}{ALPHA}"
    assert _names(aside) == ["CLAUDE.md", "spec.md", "tasks/d.txt"]
    assert _names(root / "alpha") == ["CLAUDE.md"]
    assert f"workstream-id: {BETA}" in (root / "alpha" / "CLAUDE.md").read_text()
    assert _map(workspace)["deleted"] == {ALPHA: aside.name}
    assert _map(workspace)["workstreams"][BETA] == "alpha"
    # N's backend projection and a worker output, while the archive fails.
    (root / "alpha" / "spec.md").write_text("N's spec")
    (root / "alpha" / "tasks").mkdir()
    (root / "alpha" / "tasks" / "n.txt").write_text("n")
    writer.sync_workstream_directories(rows)
    assert aside.is_dir()

    _repair_archive(root)
    writer.sync_workstream_directories(rows)
    writer.sync_workstream_directories(rows)

    assert _names(root / "alpha") == ["CLAUDE.md", "spec.md", "tasks/n.txt"]
    assert (root / "alpha" / "spec.md").read_text() == "N's spec"
    [archived] = _archive_entries(workspace)
    assert _names(root / ".archived" / archived) == [
        "CLAUDE.md",
        "spec.md",
        "tasks/d.txt",
    ]
    assert (root / ".archived" / archived / "spec.md").read_text() == "D's spec"
    assert not aside.exists()
    assert _map(workspace)["deleted"] == {}


def test_a_move_aside_interrupted_before_the_save_keeps_the_claim(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _deleted_alpha_claimed_by_a_new_workstream(workspace)
    rows = [_ws(BETA, "Alpha", "NB"), _ws(GAMMA, "Keep", "KE")]
    _sync_crashing_before_the_save(workspace, rows, monkeypatch)
    aside = root / f"{DELETED_PREFIX}{ALPHA}"
    assert aside.is_dir()
    assert _map(workspace)["deleted"] == {}  # the base: D was current then

    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories(rows)
    assert _map(workspace)["deleted"] == {ALPHA: aside.name}

    _repair_archive(root)
    writer.sync_workstream_directories(rows)
    [archived] = _archive_entries(workspace)
    assert _names(root / ".archived" / archived) == [
        "CLAUDE.md",
        "spec.md",
        "tasks/d.txt",
    ]
    assert _map(workspace)["deleted"] == {}


def test_a_frozen_name_still_receives_the_claimants_backend_writes_known_limitation(
    workspace: Path,
) -> None:
    """KNOWN LIMITATION (last resort): when the deleted workstream's
    directory can be neither archived nor moved aside, the daemon only
    freezes its own writes. The claimant's backend projections and worker
    outputs, addressed by the declared ``workspace_dir``, still land in the
    frozen directory (its workers read the deleted workstream's CLAUDE.md
    meanwhile) and are archived with it once the archive works. This test
    pins that documented behaviour; see docs/04-components/communicator.md."""
    root = _deleted_alpha_claimed_by_a_new_workstream(workspace)
    _block_aside(root, ALPHA)
    writer = ClaudeMdWriter(str(workspace))
    rows = [_ws(BETA, "Alpha", "NB"), _ws(GAMMA, "Keep", "KE")]
    writer.sync_workstream_directories(rows)
    assert f"workstream-id: {ALPHA}" in (root / "alpha" / "CLAUDE.md").read_text()
    (root / "alpha" / "tasks" / "n.txt").write_text("n")

    _repair_archive(root)
    writer.sync_workstream_directories(rows)

    [archived] = _archive_entries(workspace)
    assert "tasks/n.txt" in _names(root / ".archived" / archived)
    assert _names(root / "alpha") == ["CLAUDE.md"]
    assert f"workstream-id: {BETA}" in (root / "alpha" / "CLAUDE.md").read_text()


# -- Round 4, found by the model test (test_workstream_dirs_model.py) --------


def _unreadable(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """The daemon cannot read these directories' CLAUDE.md (a session made
    them unreadable)."""
    from src.config_sync import workstream_dirs

    original = workstream_dirs.directory_attribution

    def read(root_fd: int, name: str):
        if name in names:
            raise PermissionError(13, "Permission denied", name)
        return original(root_fd, name)

    monkeypatch.setattr(workstream_dirs, "directory_attribution", read)


def _alpha_held_at_its_old_name(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Alpha (with a.txt) is renamed to Beta while alpha's CLAUDE.md cannot be
    read: its move is held, beta is laid out, and a worker writes b.txt
    there (its declared directory)."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    root = workspace / "workstreams"
    (root / "alpha" / "a.txt").write_text("a")
    _unreadable(monkeypatch, "alpha")
    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])
    assert _names(root / "alpha") == ["CLAUDE.md", "a.txt"]
    assert f"workstream-id: {ALPHA}" in (root / "beta" / "CLAUDE.md").read_text()
    (root / "beta" / "b.txt").write_text("b")
    return root


@pytest.mark.parametrize("lost_save", [False, True])
def test_a_held_workstreams_new_directory_follows_it_when_renamed_again(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, lost_save: bool
) -> None:
    """The new name of a held workstream holds its writes too (an alias): a
    rename before the move completes merges them forward instead of
    archiving them, also when the sync that laid the alias out died before
    its save (the ``alias`` record)."""
    if lost_save:
        writer = ClaudeMdWriter(str(workspace))
        writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
        root = workspace / "workstreams"
        (root / "alpha" / "a.txt").write_text("a")
        _unreadable(monkeypatch, "alpha")
        _sync_crashing_before_the_save(workspace, [_ws(ALPHA, "Beta")], monkeypatch)
        (root / "beta" / "b.txt").write_text("b")
    else:
        root = _alpha_held_at_its_old_name(workspace, monkeypatch)
    writer = ClaudeMdWriter(str(workspace))

    writer.sync_workstream_directories([_ws(ALPHA, "Gamma")])
    monkeypatch.undo()
    writer.sync_workstream_directories([_ws(ALPHA, "Gamma")])
    writer.sync_workstream_directories([_ws(ALPHA, "Gamma")])

    assert _names(root / "gamma") == ["CLAUDE.md", "a.txt", "b.txt"]
    assert _archive_entries(workspace) == []
    assert not (root / "alpha").exists() and not (root / "beta").exists()
    assert _map(workspace)["workstreams"] == {ALPHA: "gamma"}


@pytest.mark.parametrize("lost_save", [False, True])
def test_a_held_workstreams_new_legacy_directory_follows_it_when_renamed_again(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, lost_save: bool
) -> None:
    """With a rolled-back backend (no declared directories to track), the
    new name of a held workstream is still its alias: what a worker wrote
    there merges forward on the next rename instead of being archived."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_legacy_ws(ALPHA, "Alpha")])
    root = workspace / "workstreams"
    (root / "alpha" / "a.txt").write_text("a")
    _unreadable(monkeypatch, "alpha")
    if lost_save:
        _sync_crashing_before_the_save(
            workspace, [_legacy_ws(ALPHA, "Beta")], monkeypatch
        )
    else:
        writer.sync_workstream_directories([_legacy_ws(ALPHA, "Beta")])
    assert _map(workspace).get("declared", {}) == {}
    (root / "beta" / "b.txt").write_text("b")

    writer.sync_workstream_directories([_legacy_ws(ALPHA, "Gamma")])
    monkeypatch.undo()
    writer.sync_workstream_directories([_legacy_ws(ALPHA, "Gamma")])
    writer.sync_workstream_directories([_legacy_ws(ALPHA, "Gamma")])

    assert _names(root / "gamma") == ["CLAUDE.md", "a.txt", "b.txt"]
    assert _archive_entries(workspace) == []
    assert _map(workspace)["workstreams"] == {ALPHA: "gamma"}


def test_held_workstreams_swapping_their_new_names_keep_their_writes_apart(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Model seed 2765: Alpha and Beta are renamed while their CLAUDE.md files
    cannot be read (both moves held), workers write to the new names, then
    the two swap names. Each new name holds the other's late writes, so
    neither is merged into the other while the moves are held; once they
    can run, each workstream's writes join its own directory, never
    across."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE")])
    root = workspace / "workstreams"
    (root / "alpha" / "a.txt").write_text("alpha")
    (root / "beta" / "b.txt").write_text("beta")
    _unreadable(monkeypatch, "alpha", "beta")
    writer.sync_workstream_directories([_ws(ALPHA, "Gamma"), _ws(BETA, "Delta", "BE")])
    (root / "gamma" / "late-a.txt").write_text("alpha late")
    (root / "delta" / "late-b.txt").write_text("beta late")

    writer.sync_workstream_directories([_ws(ALPHA, "Delta"), _ws(BETA, "Gamma", "BE")])
    monkeypatch.undo()
    for _ in range(3):
        writer.sync_workstream_directories(
            [_ws(ALPHA, "Delta"), _ws(BETA, "Gamma", "BE")]
        )

    assert _names(root / "delta") == ["CLAUDE.md", "a.txt", "late-a.txt"]
    assert _names(root / "gamma") == ["CLAUDE.md", "b.txt", "late-b.txt"]
    assert _map(workspace)["workstreams"] == {ALPHA: "delta", BETA: "gamma"}


def test_a_new_workstream_waits_for_another_workstreams_held_directory(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new Alpha is never recorded at the old alpha while Alpha-now-Beta's
    content is held there; it gets a directory of its own once the move
    completes."""
    root = _alpha_held_at_its_old_name(workspace, monkeypatch)
    writer = ClaudeMdWriter(str(workspace))
    rows = [_ws(ALPHA, "Beta"), _ws(GAMMA, "Alpha", "GA")]

    writer.sync_workstream_directories(rows)

    assert GAMMA not in _map(workspace)["workstreams"]
    assert f"workstream-id: {ALPHA}" in (root / "alpha" / "CLAUDE.md").read_text()
    monkeypatch.undo()
    writer.sync_workstream_directories(rows)
    writer.sync_workstream_directories(rows)
    assert _names(root / "beta") == ["CLAUDE.md", "a.txt", "b.txt"]
    assert _names(root / "alpha") == ["CLAUDE.md"]
    assert f"workstream-id: {GAMMA}" in (root / "alpha" / "CLAUDE.md").read_text()
    assert _archive_entries(workspace) == []


def test_a_new_workstream_is_not_laid_out_when_the_map_cannot_record_it(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno as errno_module

    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    root = workspace / "workstreams"
    _full_disk_for(monkeypatch, records=True)

    with pytest.raises(OSError) as raised:
        writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(BETA, "Beta")])

    assert raised.value.errno == errno_module.ENOSPC
    assert not (root / "beta").exists()


def test_a_stale_layout_of_another_workstream_holding_only_claude_md_is_removed(
    workspace: Path,
) -> None:
    """Nothing but a CLAUDE.md (regenerated wherever its workstream is): the
    claimant does not wait for it."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    root = workspace / "workstreams"
    (root / "beta").mkdir()
    (root / "beta" / "CLAUDE.md").write_text((root / "alpha" / "CLAUDE.md").read_text())
    recorded = _map(workspace)
    recorded["workstreams"][GAMMA] = "beta"  # recorded, so it is not archived
    _map_path(workspace).write_text(json.dumps(recorded))
    rows = [_ws(ALPHA, "Alpha"), _ws(BETA, "Beta", "BE"), _ws(GAMMA, "Gamma", "GA")]

    writer.sync_workstream_directories(rows)
    writer.sync_workstream_directories(rows)

    assert f"workstream-id: {BETA}" in (root / "beta" / "CLAUDE.md").read_text()
    assert _archive_entries(workspace) == []


def test_late_writes_of_a_deleted_workstream_are_never_adopted(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alpha is renamed to Delta and a session writes into alpha; one sync
    then deletes Delta and creates a new Alpha. The late writes are the
    deleted workstream's: archived, not adopted."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(GAMMA, "Keep", "KE")])
    writer.sync_workstream_directories([_ws(ALPHA, "Delta"), _ws(GAMMA, "Keep", "KE")])
    root = workspace / "workstreams"
    (root / "alpha" / "tasks").mkdir(parents=True)
    (root / "alpha" / "tasks" / "late.txt").write_text("late d")
    assert _map(workspace)["retired"] == {"alpha": ALPHA}

    writer.sync_workstream_directories(
        [_ws(BETA, "Alpha", "BE"), _ws(GAMMA, "Keep", "KE")]
    )
    writer.sync_workstream_directories(
        [_ws(BETA, "Alpha", "BE"), _ws(GAMMA, "Keep", "KE")]
    )

    assert "tasks/late.txt" not in _names(root / "alpha")
    archived = [
        name
        for entry in _archive_entries(workspace)
        for name in _names(root / ".archived" / entry)
    ]
    assert "tasks/late.txt" in archived
    assert f"workstream-id: {BETA}" in (root / "alpha" / "CLAUDE.md").read_text()


def test_a_swept_deleted_directory_records_its_claim_with_the_archive(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alpha is deleted; the sync that archives its directory dies before
    its save. A new Alpha's projection then lands at alpha: the next sync
    knows the claim is resolved and never archives it."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(GAMMA, "Keep", "KE")])
    root = workspace / "workstreams"
    (root / "alpha" / "a.txt").write_text("a")
    _sync_crashing_before_the_save(workspace, [_ws(GAMMA, "Keep", "KE")], monkeypatch)
    assert not (root / "alpha").exists()
    (root / "alpha" / "intake").mkdir(parents=True)
    (root / "alpha" / "intake" / "n.json").write_text("{}")

    writer.sync_workstream_directories(
        [_ws(BETA, "Alpha", "BE"), _ws(GAMMA, "Keep", "KE")]
    )

    assert _names(root / "alpha") == ["CLAUDE.md", "intake/n.json"]
    [archived] = _archive_entries(workspace)
    assert _names(root / ".archived" / archived) == ["CLAUDE.md", "a.txt"]


def test_late_writes_wait_while_their_workstreams_directory_is_not_usable(
    workspace: Path,
) -> None:
    """A claimant of the old name waits while the late writes cannot be merged
    forward; the retired entry is kept, so they still reach their owner."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    writer.sync_workstream_directories([_ws(ALPHA, "Delta")])
    root = workspace / "workstreams"
    (root / "alpha" / "tasks").mkdir(parents=True)
    (root / "alpha" / "tasks" / "late.txt").write_text("late a")
    delta = root / "delta"
    delta.rename(workspace / "delta-hold")
    delta.symlink_to(workspace)  # a session's link at Delta's name
    rows = [_ws(ALPHA, "Delta"), _ws(BETA, "Alpha", "BE")]

    writer.sync_workstream_directories(rows)

    assert _map(workspace)["retired"].get("alpha") == ALPHA
    assert "tasks/late.txt" in _names(root / "alpha")
    assert BETA not in _map(workspace)["workstreams"]
    delta.unlink()
    (workspace / "delta-hold").rename(delta)
    writer.sync_workstream_directories(rows)
    writer.sync_workstream_directories(rows)
    assert _names(delta) == ["CLAUDE.md", "tasks/late.txt"]
    assert f"workstream-id: {BETA}" in (root / "alpha" / "CLAUDE.md").read_text()
    assert _archive_entries(workspace) == []


def test_late_writes_whose_workstreams_name_is_a_link_are_kept(
    workspace: Path,
) -> None:
    """The orphan sweep never archives a current workstream's late writes
    because the name it would merge them into holds a link."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    writer.sync_workstream_directories([_ws(ALPHA, "Delta")])
    root = workspace / "workstreams"
    (root / "alpha" / "tasks").mkdir(parents=True)
    (root / "alpha" / "tasks" / "late.txt").write_text("late a")
    delta = root / "delta"
    delta.rename(workspace / "delta-hold")
    delta.symlink_to(workspace)

    writer.sync_workstream_directories([_ws(ALPHA, "Delta")])

    assert "tasks/late.txt" in _names(root / "alpha")
    assert _archive_entries(workspace) == []
    delta.unlink()
    (workspace / "delta-hold").rename(delta)
    writer.sync_workstream_directories([_ws(ALPHA, "Delta")])
    assert _names(delta) == ["CLAUDE.md", "tasks/late.txt"]


def test_a_legacy_guess_with_an_unreadable_claude_md_is_kept_for_later(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a map (first sync after an upgrade or a rollback), the legacy
    directory of non-Latin Продажі (``office``) cannot be checked yet: the
    guess is kept (its move held), never dropped and archived as an orphan."""
    root = workspace / "workstreams"
    (root / "office").mkdir(parents=True)
    (root / "office" / "CLAUDE.md").write_text(
        "# Workstream: Продажі\n\n**Short code:** `AB` · **Priority:** `medium`\n"
    )
    (root / "office" / "a.txt").write_text("a")
    _unreadable(monkeypatch, "office")
    writer = ClaudeMdWriter(str(workspace))
    rows = [_ws(ALPHA, "Продажі")]

    writer.sync_workstream_directories(rows)
    assert _names(root / "office") == ["CLAUDE.md", "a.txt"]
    monkeypatch.undo()
    writer.sync_workstream_directories(rows)

    assert _names(root / "ws-ab") == ["CLAUDE.md", "a.txt"]
    assert _archive_entries(workspace) == []


def test_a_legacy_guess_at_its_own_name_stays_a_guess_while_unreadable(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alpha's legacy directory is its own name but cannot be read: the
    guess is saved as ``unverified`` (checked again next sync), so a rename
    before it can be read still takes the content along."""
    root = workspace / "workstreams"
    _older_daemon_claude_md(root, "Alpha", "AB")
    (root / "alpha" / "a.txt").write_text("a")
    _unreadable(monkeypatch, "alpha")
    writer = ClaudeMdWriter(str(workspace))

    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    assert _map(workspace)["unverified"] == {ALPHA: "alpha"}
    assert ALPHA not in _map(workspace)["workstreams"]
    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])
    monkeypatch.undo()
    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])
    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])

    assert _names(root / "beta") == ["CLAUDE.md", "a.txt"]
    assert _archive_entries(workspace) == []
    assert "unverified" not in _map(workspace)


def test_an_orphan_with_an_unreadable_claude_md_is_left_until_it_can_be_read(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    root = workspace / "workstreams"
    (root / "zeta").mkdir()
    (root / "zeta" / "CLAUDE.md").write_text("# Workstream: Zeta\n")
    (root / "zeta" / "z.txt").write_text("z")
    _unreadable(monkeypatch, "zeta")

    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    assert _names(root / "zeta") == ["CLAUDE.md", "z.txt"]
    assert _archive_entries(workspace) == []

    monkeypatch.undo()
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    [archived] = _archive_entries(workspace)
    assert _names(root / ".archived" / archived) == ["CLAUDE.md", "z.txt"]


def test_late_writes_join_their_workstream_while_its_own_move_is_staged(
    workspace: Path,
) -> None:
    """One sync renames Alpha-now-Beta onward to Gamma and creates a new
    Alpha: the late writes at alpha join the staged directory, and the new
    Alpha gets alpha in the same sync."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])
    root = workspace / "workstreams"
    (root / "alpha" / "tasks").mkdir(parents=True)
    (root / "alpha" / "tasks" / "late.txt").write_text("late a")

    writer.sync_workstream_directories([_ws(ALPHA, "Gamma"), _ws(BETA, "Alpha", "BE")])

    assert _names(root / "gamma") == ["CLAUDE.md", "tasks/late.txt"]
    assert _names(root / "alpha") == ["CLAUDE.md"]
    assert f"workstream-id: {BETA}" in (root / "alpha" / "CLAUDE.md").read_text()
    assert _archive_entries(workspace) == []


def test_new_late_writes_after_an_interrupted_merge_still_merge_forward(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sync merges late writes forward and dies before its save; a session
    writes into the old name again: the completed merge keeps the name
    retired, so these merge forward too (never archived)."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(GAMMA, "Keep", "KE")])
    rows = [_ws(ALPHA, "Beta"), _ws(GAMMA, "Keep", "KE")]
    writer.sync_workstream_directories(rows)
    root = workspace / "workstreams"
    (root / "alpha").mkdir()
    (root / "alpha" / "one.txt").write_text("1")
    _sync_crashing_before_the_save(workspace, rows, monkeypatch)
    assert "one.txt" in _names(root / "beta")
    (root / "alpha").mkdir()
    (root / "alpha" / "two.txt").write_text("2")

    writer.sync_workstream_directories(rows)

    assert _names(root / "beta") == ["CLAUDE.md", "one.txt", "two.txt"]
    assert _archive_entries(workspace) == []


def test_a_restored_map_entry_is_vetoed_by_a_deleted_workstreams_marker(
    workspace: Path,
) -> None:
    """An operator restores a map that places Alpha at alpha, but alpha now
    holds deleted Gamma's content (its id marker): it is archived, and Alpha
    starts in a directory of its own instead of adopting it."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    backup = _map_path(workspace).read_bytes()
    writer.sync_workstream_directories([_ws(ALPHA, "Beta"), _ws(GAMMA, "Alpha", "GA")])
    root = workspace / "workstreams"
    (root / "alpha" / "g.txt").write_text("gamma's")
    _map_path(workspace).write_bytes(backup)  # the operator's restore

    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])

    assert "g.txt" not in _names(root / "alpha")
    assert any(
        "g.txt" in _names(root / ".archived" / entry)
        for entry in _archive_entries(workspace)
    )
    assert f"workstream-id: {ALPHA}" in (root / "alpha" / "CLAUDE.md").read_text()


def test_a_placement_at_a_freed_name_survives_a_lost_save(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deleted workstream's directory holding only its CLAUDE.md is removed,
    so the new owner of the name waits one sync (the inode is free). That
    sync dies before its save; a worker writes to the declared directory; the
    new workstream is renamed: its content follows it."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(GAMMA, "Keep", "KE")])
    root = workspace / "workstreams"
    _sync_crashing_before_the_save(
        workspace, [_ws(BETA, "Alpha", "BE"), _ws(GAMMA, "Keep", "KE")], monkeypatch
    )
    assert not (root / "alpha").exists()
    (root / "alpha" / "tasks").mkdir(parents=True)
    (root / "alpha" / "tasks" / "n.txt").write_text("n")

    writer.sync_workstream_directories(
        [_ws(BETA, "Beta", "BE"), _ws(GAMMA, "Keep", "KE")]
    )

    assert _names(root / "beta") == ["CLAUDE.md", "tasks/n.txt"]
    assert _archive_entries(workspace) == []


@pytest.mark.parametrize("lost_save", [False, True])
def test_writes_at_a_staged_workstreams_declared_directory_follow_it(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, lost_save: bool
) -> None:
    """Alpha's move to beta waits (a session's link at beta). Its workers
    write to its declared directory once the link is gone; before the next
    sync Alpha is renamed to Delta and a new workstream takes "Beta": what
    was written at beta is Alpha's, merged forward, never adopted. With
    ``lost_save`` the rename to that name was made by a sync whose final
    save failed (the declared name is saved ahead of the moves)."""
    import errno as errno_module

    from src.config_sync.workstream_dirs import WorkstreamDirectoryMap

    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha")])
    root = workspace / "workstreams"
    (root / "alpha" / "a.txt").write_text("a")
    target = "gamma" if lost_save else "beta"
    for name in ("beta", "gamma"):
        (root / name).symlink_to(workspace)
    writer.sync_workstream_directories([_ws(ALPHA, "Beta")])
    assert (root / f"{RELOCATING_PREFIX}{ALPHA}").is_dir()
    if lost_save:
        original = WorkstreamDirectoryMap.write_payload

        def final_save_fails(self, payload: dict, *, strict: bool = False) -> None:
            if "pending" not in payload:
                raise OSError(errno_module.ENOSPC, "No space left on device")
            original(self, payload, strict=strict)

        monkeypatch.setattr(WorkstreamDirectoryMap, "write_payload", final_save_fails)
        with pytest.raises(OSError):
            writer.sync_workstream_directories([_ws(ALPHA, "Gamma")])
        monkeypatch.undo()
    for name in ("beta", "gamma"):
        (root / name).unlink()
    (root / target / "outputs").mkdir(parents=True)
    (root / target / "outputs" / "w.txt").write_text("alpha's output")
    rows = [_ws(ALPHA, "Delta"), _ws(BETA, target.title(), "BE")]

    writer.sync_workstream_directories(rows)
    writer.sync_workstream_directories(rows)

    assert _names(root / "delta") == ["CLAUDE.md", "a.txt", "outputs/w.txt"]
    assert _names(root / target) == ["CLAUDE.md"]
    assert f"workstream-id: {BETA}" in (root / target / "CLAUDE.md").read_text()
    assert _archive_entries(workspace) == []


def test_late_writes_of_a_deleted_workstream_stay_claimed_while_the_archive_fails(
    workspace: Path,
) -> None:
    """Alpha-now-Beta's late writes at alpha, then Beta is deleted while the
    archive fails: alpha is kept as the deleted workstream's (a claim), so a
    new "Alpha" gets a directory of its own and the late writes are archived
    once the archive works."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(GAMMA, "Keep", "KE")])
    writer.sync_workstream_directories([_ws(ALPHA, "Beta"), _ws(GAMMA, "Keep", "KE")])
    root = workspace / "workstreams"
    (root / "alpha" / "tasks").mkdir(parents=True)
    (root / "alpha" / "tasks" / "late.txt").write_text("late a")
    _break_archive(root)
    writer.sync_workstream_directories([_ws(GAMMA, "Keep", "KE")])
    assert "tasks/late.txt" in _names(root / "alpha")
    rows = [_ws(BETA, "Alpha", "BE"), _ws(GAMMA, "Keep", "KE")]

    writer.sync_workstream_directories(rows)

    assert "tasks/late.txt" not in _names(root / "alpha")
    assert f"workstream-id: {BETA}" in (root / "alpha" / "CLAUDE.md").read_text()
    _repair_archive(root)
    writer.sync_workstream_directories(rows)
    writer.sync_workstream_directories(rows)
    archived = [
        name
        for entry in _archive_entries(workspace)
        for name in _names(root / ".archived" / entry)
    ]
    assert "tasks/late.txt" in archived
    assert _map(workspace)["deleted"] == {}


def test_a_held_legacy_guess_is_never_trusted_before_it_can_be_read(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a map, Продажі's legacy guess ``office`` cannot be read (its
    move is held). Renamed to "Office" before it can be read, it is still
    only a guess: once readable, a CLAUDE.md naming a short code no current
    workstream has makes it a deleted workstream's directory, archived, not
    adopted."""
    root = workspace / "workstreams"
    (root / "office").mkdir(parents=True)
    (root / "office" / "CLAUDE.md").write_text(
        "# Workstream: Old\n\n**Short code:** `ZZ` · **Priority:** `medium`\n"
    )
    (root / "office" / "old.txt").write_text("a deleted workstream's")
    _unreadable(monkeypatch, "office")
    writer = ClaudeMdWriter(str(workspace))

    writer.sync_workstream_directories([_ws(ALPHA, "Продажі")])
    assert _map(workspace)["unverified"] == {ALPHA: "office"}
    assert ALPHA not in _map(workspace)["workstreams"]
    writer.sync_workstream_directories([_ws(ALPHA, "Office")])
    assert _names(root / "office") == ["CLAUDE.md", "old.txt"]
    monkeypatch.undo()
    writer.sync_workstream_directories([_ws(ALPHA, "Office")])
    writer.sync_workstream_directories([_ws(ALPHA, "Office")])

    assert _names(root / "office") == ["CLAUDE.md"]
    assert f"workstream-id: {ALPHA}" in (root / "office" / "CLAUDE.md").read_text()
    archived = [
        name
        for entry in _archive_entries(workspace)
        for name in _names(root / ".archived" / entry)
    ]
    assert "old.txt" in archived


def test_a_waiting_workstreams_writes_after_a_hand_archive_follow_it(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleted Alpha's directory cannot be read, so a new "Alpha" waits. The
    user archives it by hand and the new workstream's workers write to its
    declared alpha. The new workstream is renamed to Gamma and another takes
    "Alpha" before the next sync: its writes follow it (the stale claim does
    not hide them)."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha"), _ws(GAMMA, "Keep", "KE")])
    root = workspace / "workstreams"
    (root / "alpha" / "d.txt").write_text("d")
    _unreadable(monkeypatch, "alpha")
    writer.sync_workstream_directories(
        [_ws(BETA, "Alpha", "BE"), _ws(GAMMA, "Keep", "KE")]
    )
    assert _names(root / "alpha") == ["CLAUDE.md", "d.txt"]
    monkeypatch.undo()
    (root / "alpha").rename(workspace / "hand-archived")
    (root / "alpha" / "tasks").mkdir(parents=True)
    (root / "alpha" / "tasks" / "n.txt").write_text("n")
    rows = [
        _ws(BETA, "Gamma", "BE"),
        _ws(GAMMA, "Keep", "KE"),
        _ws(DELTA, "Alpha", "DE"),
    ]

    writer.sync_workstream_directories(rows)
    writer.sync_workstream_directories(rows)

    assert _names(root / "gamma") == ["CLAUDE.md", "tasks/n.txt"]
    assert _names(root / "alpha") == ["CLAUDE.md"]
    assert f"workstream-id: {DELTA}" in (root / "alpha" / "CLAUDE.md").read_text()
