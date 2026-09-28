"""A daemon rollback past ``workstream_dirs_v1`` never loses workstream content.

MV1.

A daemon from before ``workstream_dirs_v1`` names each workstream directory
``slugify(name)`` (the shared ``office`` directory for a name without ASCII
letters or digits) and deletes every other directory under ``workstreams/``
that has no top-level ``spec.md`` / ``learnings.md`` — archiving the rest to
``.archived/<name>`` after deleting any older archive of that name. These
tests replay that sweep (``_old_daemon_sync``, a faithful copy of
``claude_md_writer.sync_workstream_directories`` at 1e5d05c4) and pin that
``cbcl workstream-dirs prepare-rollback --apply`` leaves nothing for it to
destroy, while the default dry run changes nothing.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import uuid
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.config_sync import workstream_dirs_rollback
from src.config_sync.claude_md_writer import ClaudeMdWriter
from src.config_sync.workstream_dirs import (
    DELETED_PREFIX,
    MAP_DIRNAME,
    MAP_FILENAME,
    RELOCATING_PREFIX,
)
from src.config_sync.workstream_dirs_rollback import (
    apply_rollback,
    plan_rollback,
    prepare_rollback,
)
from src.paths import slugify, workstream_dir_slug

ALPHA = str(uuid.UUID(int=1))
BETA = str(uuid.UUID(int=2))
GAMMA = str(uuid.UUID(int=3))


@pytest.fixture(autouse=True)
def _no_running_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real cbcl on the test host must not refuse every `--apply` run.

    Without this, the CLI tests pass vacuously (exit code != 0 from the
    daemon refusal) on a machine that runs cbcl. Tests of the refusal
    itself patch `daemon_is_running` again.
    """
    monkeypatch.setattr(workstream_dirs_rollback, "daemon_is_running", lambda: False)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def _ws(workstream_id: str, name: str, code: str) -> dict:
    return {
        "id": workstream_id,
        "name": name,
        "short_code": code,
        "workspace_dir": workstream_dir_slug(name, code),
    }


def _legacy(workstream_id: str, name: str, code: str) -> dict:
    """The row from a rolled-back backend: no ``workspace_dir``."""
    return {"id": workstream_id, "name": name, "short_code": code}


def _block_aside(root: Path, workstream_id: str) -> None:
    """A file at ``.deleted-<id>``: the deleted workstream's directory cannot
    be moved aside either, so the daemon keeps its name frozen."""
    (root / f"{DELETED_PREFIX}{workstream_id}").write_text("blocks the move")


def _old_daemon_sync(workspace: Path, names: list[str]) -> None:
    """The workstream pass of a daemon older than workstream_dirs_v1."""
    ws_dir = workspace / "workstreams"
    ws_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    for name in names:
        slug = slugify(name)
        seen.add(slug)
        (ws_dir / slug).mkdir(exist_ok=True)
        (ws_dir / slug / "CLAUDE.md").write_text(f"# Workstream: {name}\n")
    for child in ws_dir.iterdir():
        if not child.is_dir() or child.name in seen or child.name == ".archived":
            continue
        if any(
            (child / name).exists()
            for name in ("spec.md", "learnings.md", "learnings.migrated.md")
        ):
            archive_root = ws_dir / ".archived"
            archive_root.mkdir(exist_ok=True)
            dest = archive_root / child.name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(child), str(dest))
        else:
            shutil.rmtree(child)


def _payloads(workspace: Path) -> list[str]:
    """Every non-regenerable file's content under workstreams/."""
    root = workspace / "workstreams"
    return sorted(
        path.read_text()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and path.name != "CLAUDE.md"
    )


def _tree(workspace: Path) -> list[str]:
    return sorted(str(path.relative_to(workspace)) for path in workspace.rglob("*"))


def _apply(workspace: Path) -> None:
    outcome = apply_rollback(plan_rollback(workspace))
    assert outcome.failed == []


def test_old_daemon_destroys_a_non_latin_directory_without_the_helper(
    workspace: Path,
) -> None:
    """The hazard itself: task outputs without a spec.md are deleted."""
    ClaudeMdWriter(str(workspace)).sync_workstream_directories(
        [_ws(ALPHA, "Продажі", "PR")]
    )
    (workspace / "workstreams" / "ws-pr" / "tasks").mkdir()
    (workspace / "workstreams" / "ws-pr" / "tasks" / "out.txt").write_text("work")

    _old_daemon_sync(workspace, ["Продажі"])

    assert _payloads(workspace) == []


def test_dry_run_changes_nothing(workspace: Path) -> None:
    ClaudeMdWriter(str(workspace)).sync_workstream_directories(
        [_ws(ALPHA, "Продажі", "PR"), _ws(BETA, "Beta", "BE")]
    )
    (workspace / "workstreams" / "ws-pr" / "spec.md").write_text("pr spec")
    before = _tree(workspace)

    plan = plan_rollback(workspace)

    assert [(step.action, step.source, step.target) for step in plan.steps] == [
        ("move", "ws-pr", "office"),
        ("set-aside", MAP_FILENAME, plan.steps[-1].target),
    ]
    assert _tree(workspace) == before


def test_lone_non_latin_workstream_moves_back_to_the_legacy_directory(
    workspace: Path,
) -> None:
    ClaudeMdWriter(str(workspace)).sync_workstream_directories(
        [_ws(ALPHA, "Продажі", "PR"), _ws(BETA, "Beta", "BE")]
    )
    root = workspace / "workstreams"
    (root / "ws-pr" / "tasks").mkdir()
    (root / "ws-pr" / "tasks" / "out.txt").write_text("pr work")
    (root / "beta" / "spec.md").write_text("beta spec")
    before = _payloads(workspace)

    _apply(workspace)
    _old_daemon_sync(workspace, ["Продажі", "Beta"])

    assert (root / "office" / "tasks" / "out.txt").read_text() == "pr work"
    assert (root / "beta" / "spec.md").read_text() == "beta spec"
    assert _payloads(workspace) == before
    # The map is set aside, never deleted, and records where each
    # directory is after the rollback.
    map_dir = workspace / MAP_DIRNAME
    assert not (map_dir / MAP_FILENAME).exists()
    [aside] = [p for p in map_dir.iterdir() if p.name.startswith(MAP_FILENAME)]
    assert json.loads(aside.read_text())["workstreams"][ALPHA] == "office"


def test_shared_legacy_directory_archives_every_claimant(workspace: Path) -> None:
    ClaudeMdWriter(str(workspace)).sync_workstream_directories(
        [_ws(ALPHA, "Продажі", "PR"), _ws(BETA, "Маркетинг", "MA")]
    )
    root = workspace / "workstreams"
    (root / "ws-pr" / "notes.txt").write_text("pr")
    (root / "ws-ma" / "notes.txt").write_text("ma")
    before = _payloads(workspace)

    _apply(workspace)
    _old_daemon_sync(workspace, ["Продажі", "Маркетинг"])

    # Mixed content cannot be split into the shared ``office`` directory.
    assert not (root / "office" / "notes.txt").exists()
    archived = sorted(p.name for p in (root / ".archived").iterdir())
    assert len(archived) == 2
    assert all(name.startswith(("ws-pr-", "ws-ma-")) for name in archived)
    assert _payloads(workspace) == before


def test_slug_named_archives_are_protected_from_the_old_daemon(
    workspace: Path,
) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories(
        [_ws(ALPHA, "Alpha", "AL"), _ws(BETA, "Beta", "BE")]
    )
    root = workspace / "workstreams"
    (root / "beta" / "spec.md").write_text("deleted beta spec")
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha", "AL")])
    assert (root / ".archived" / "beta" / "spec.md").exists()

    _apply(workspace)
    # Under the older daemon a new "Beta" is created, then deleted with a
    # spec: its archive step deletes ``.archived/beta`` first.
    _old_daemon_sync(workspace, ["Alpha", "Beta"])
    (root / "beta" / "spec.md").write_text("new beta spec")
    _old_daemon_sync(workspace, ["Alpha"])

    payloads = _payloads(workspace)
    assert "deleted beta spec" in payloads
    assert "new beta spec" in payloads


def test_staged_move_is_placed_at_the_legacy_directory(workspace: Path) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha", "AL")])
    root = workspace / "workstreams"
    (root / "alpha" / "tasks").mkdir()
    (root / "alpha" / "tasks" / "out.txt").write_text("staged work")
    # A file at the new name keeps the move staged (CM1).
    (root / "beta").write_text("stray")
    writer.sync_workstream_directories([_ws(ALPHA, "Beta", "AL")])
    assert (root / f"{RELOCATING_PREFIX}{ALPHA}").is_dir()
    (root / "beta").unlink()

    _apply(workspace)
    _old_daemon_sync(workspace, ["Beta"])

    assert (root / "beta" / "tasks" / "out.txt").read_text() == "staged work"
    assert not (root / f"{RELOCATING_PREFIX}{ALPHA}").exists()


def test_deleted_workstreams_directory_is_archived_before_a_staged_move_lands(
    workspace: Path,
) -> None:
    """WSD-1: a deleted workstream's directory the daemon could not archive
    yet is archived by the helper, so the staged move of the workstream
    renamed onto that name moves in instead of merging into it."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories(
        [_ws(ALPHA, "Alpha", "AL"), _ws(BETA, "Beta", "BE")]
    )
    root = workspace / "workstreams"
    (root / "alpha" / "spec.md").write_text("alpha spec")
    (root / "beta" / "spec.md").write_text("deleted beta spec")
    (root / ".archived").write_text("not a directory")
    _block_aside(root, BETA)
    writer.sync_workstream_directories([_ws(ALPHA, "Beta", "AL")])
    assert (root / f"{RELOCATING_PREFIX}{ALPHA}").is_dir()
    (root / ".archived").unlink()

    _apply(workspace)
    _old_daemon_sync(workspace, ["Beta"])

    assert (root / "beta" / "spec.md").read_text() == "alpha spec"
    [archived] = sorted((root / ".archived").iterdir())
    assert (archived / "spec.md").read_text() == "deleted beta spec"


def test_a_failed_deleted_directory_archive_never_lets_the_move_merge_in(
    workspace: Path,
) -> None:
    """DL1-move-merges / RB-1: when archiving the deleted workstream's
    directory fails, the planned move into that name fails too (the staged
    directory is kept whole); the re-run then places it cleanly."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories(
        [_ws(ALPHA, "Alpha", "AL"), _ws(BETA, "Beta", "BE")]
    )
    root = workspace / "workstreams"
    (root / "alpha" / "spec.md").write_text("alpha spec")
    (root / "alpha" / "tasks").mkdir()
    (root / "alpha" / "tasks" / "out.txt").write_text("alpha work")
    (root / "beta" / "spec.md").write_text("deleted beta spec")
    (root / ".archived").write_text("not a directory")
    _block_aside(root, BETA)
    writer.sync_workstream_directories([_ws(ALPHA, "Beta", "AL")])
    staged = root / f"{RELOCATING_PREFIX}{ALPHA}"
    assert staged.is_dir()

    outcome = apply_rollback(plan_rollback(workspace))

    assert len(outcome.failed) == 3
    assert sorted(p.name for p in (root / "beta").iterdir()) == [
        "CLAUDE.md",
        "spec.md",
    ]
    assert (staged / "spec.md").read_text() == "alpha spec"
    assert (staged / "tasks" / "out.txt").read_text() == "alpha work"
    assert (workspace / MAP_DIRNAME / MAP_FILENAME).is_file()

    (root / ".archived").unlink()
    _apply(workspace)
    _old_daemon_sync(workspace, ["Beta"])

    assert (root / "beta" / "tasks" / "out.txt").read_text() == "alpha work"
    assert (root / "beta" / "spec.md").read_text() == "alpha spec"
    [archived] = sorted((root / ".archived").iterdir())
    assert sorted(p.name for p in archived.iterdir()) == ["CLAUDE.md", "spec.md"]
    assert (archived / "spec.md").read_text() == "deleted beta spec"


def test_staged_move_never_takes_over_the_directory_now_at_its_old_name(
    workspace: Path,
) -> None:
    """WSD-4: a workstream created with the name a staged move came from keeps
    its live directory; the helper plans only the staged directory."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha", "AL")])
    root = workspace / "workstreams"
    (root / "alpha" / "tasks").mkdir()
    (root / "alpha" / "tasks" / "out.txt").write_text("staged work")
    (root / "beta").write_text("stray")
    writer.sync_workstream_directories([_ws(ALPHA, "Beta", "AL")])
    writer.sync_workstream_directories(
        [_ws(ALPHA, "Beta", "AL"), _ws(GAMMA, "Alpha", "GA")]
    )
    (root / "alpha" / "spec.md").write_text("new alpha spec")
    (root / "beta").unlink()

    plan = plan_rollback(workspace)
    assert not any(step.source == "alpha" for step in plan.steps)
    _apply(workspace)
    _old_daemon_sync(workspace, ["Beta", "Alpha"])

    assert (root / "alpha" / "spec.md").read_text() == "new alpha spec"
    assert (root / "beta" / "tasks" / "out.txt").read_text() == "staged work"


def test_orphans_the_old_daemon_would_delete_are_archived(workspace: Path) -> None:
    ClaudeMdWriter(str(workspace)).sync_workstream_directories(
        [_ws(ALPHA, "Alpha", "AL")]
    )
    root = workspace / "workstreams"
    (root / "late" / "tasks").mkdir(parents=True)
    (root / "late" / "tasks" / "x.txt").write_text("late write")
    (root / ".cache").mkdir()
    (root / ".cache" / "blob").write_text("hidden work")
    (root / "regenerable").mkdir()
    (root / "regenerable" / "CLAUDE.md").write_text("# Workstream: gone\n")

    plan = plan_rollback(workspace)
    assert any("regenerable" in note for note in plan.notes)
    _apply(workspace)
    _old_daemon_sync(workspace, ["Alpha"])

    assert sorted(_payloads(workspace)) == ["hidden work", "late write"]


def test_an_archived_shared_directory_is_restored_by_renaming_it_back(
    workspace: Path,
) -> None:
    """The documented restore: an archived ws-<code> directory does not come
    back on its own; renamed back before the re-upgrade, it is kept."""
    ClaudeMdWriter(str(workspace)).sync_workstream_directories(
        [_ws(ALPHA, "Продажі", "PR"), _ws(BETA, "Маркетинг", "MA")]
    )
    root = workspace / "workstreams"
    (root / "ws-pr" / "tasks" / "t1").mkdir(parents=True)
    (root / "ws-pr" / "tasks" / "t1" / "out.txt").write_text("pr work")
    _apply(workspace)
    _old_daemon_sync(workspace, ["Продажі", "Маркетинг"])
    workstreams = [_ws(ALPHA, "Продажі", "PR"), _ws(BETA, "Маркетинг", "MA")]

    [archived] = [p for p in (root / ".archived").iterdir() if "ws-pr" in p.name]
    archived.rename(root / "ws-pr")
    ClaudeMdWriter(str(workspace)).sync_workstream_directories(workstreams)

    assert (root / "ws-pr" / "tasks" / "t1" / "out.txt").read_text() == "pr work"


def test_backend_rollback_window_content_merges_into_the_legacy_directory(
    workspace: Path,
) -> None:
    """MV2 then MV1: the daemon kept ws-pr while an older backend wrote
    office/; rolling the daemon back merges ws-pr into office (sole owner)."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Продажі", "PR")])
    root = workspace / "workstreams"
    (root / "ws-pr" / "tasks").mkdir()
    (root / "ws-pr" / "tasks" / "old.txt").write_text("before rollback")
    (root / "ws-pr" / "spec.md").write_text("declared spec")
    legacy_row = {"id": ALPHA, "name": "Продажі", "short_code": "PR"}
    writer.sync_workstream_directories([legacy_row])
    (root / "office").mkdir(exist_ok=True)
    (root / "office" / "spec.md").write_text("rollback spec")
    before = _payloads(workspace)

    plan = plan_rollback(workspace)
    assert ("merge", "ws-pr", "office") in [
        (step.action, step.source, step.target) for step in plan.steps
    ]
    _apply(workspace)
    _old_daemon_sync(workspace, ["Продажі"])

    assert (root / "office" / "tasks" / "old.txt").read_text() == "before rollback"
    assert (root / "office" / "spec.md").read_text() == "rollback spec"
    assert _payloads(workspace) == before


def test_map_without_legacy_records_uses_the_claude_md_heading(
    workspace: Path,
) -> None:
    ClaudeMdWriter(str(workspace)).sync_workstream_directories(
        [_ws(ALPHA, "Продажі", "PR")]
    )
    map_path = workspace / MAP_DIRNAME / MAP_FILENAME
    raw = json.loads(map_path.read_text())
    raw.pop("legacy")
    raw.pop("declared")
    raw["version"] = 1
    map_path.write_text(json.dumps(raw))

    plan = plan_rollback(workspace)

    assert ("move", "ws-pr", "office") in [
        (step.action, step.source, step.target) for step in plan.steps
    ]


def test_reupgrade_after_rollback_moves_the_directory_forward_again(
    workspace: Path,
) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Продажі", "PR")])
    root = workspace / "workstreams"
    (root / "ws-pr" / "tasks").mkdir()
    (root / "ws-pr" / "tasks" / "out.txt").write_text("work")
    _apply(workspace)
    _old_daemon_sync(workspace, ["Продажі"])

    ClaudeMdWriter(str(workspace)).sync_workstream_directories(
        [_ws(ALPHA, "Продажі", "PR")]
    )

    assert (root / "ws-pr" / "tasks" / "out.txt").read_text() == "work"
    assert not (root / "office").exists()


def test_no_map_plans_nothing(workspace: Path) -> None:
    (workspace / "workstreams" / "office").mkdir(parents=True)
    plan = plan_rollback(workspace)
    assert plan.steps == []
    assert plan.notes and "No readable workstream directory map" in plan.notes[0]


def _two_shared_non_latin_workstreams(workspace: Path) -> Path:
    ClaudeMdWriter(str(workspace)).sync_workstream_directories(
        [_ws(ALPHA, "Продажі", "PR"), _ws(BETA, "Маркетинг", "MA")]
    )
    root = workspace / "workstreams"
    for code in ("pr", "ma"):
        (root / f"ws-{code}" / "tasks" / "t1").mkdir(parents=True)
        (root / f"ws-{code}" / "tasks" / "t1" / "out.md").write_text(f"{code} work")
    return root


def test_a_failed_step_keeps_the_map_and_a_rerun_finishes(workspace: Path) -> None:
    """DL-1: after a failed step the map is not set aside, so a re-run (once
    the cause is fixed) still plans the directories the older daemon would
    delete."""
    root = _two_shared_non_latin_workstreams(workspace)
    before = _payloads(workspace)
    (root / ".archived").write_text("not a directory")

    outcome = apply_rollback(plan_rollback(workspace))

    assert len(outcome.failed) == 3
    assert "the map is kept" in outcome.failed[-1]
    assert (workspace / MAP_DIRNAME / MAP_FILENAME).is_file()
    assert (root / "ws-pr").is_dir() and (root / "ws-ma").is_dir()

    (root / ".archived").unlink()
    _apply(workspace)
    _old_daemon_sync(workspace, ["Продажі", "Маркетинг"])

    assert _payloads(workspace) == before
    assert not (workspace / MAP_DIRNAME / MAP_FILENAME).exists()


def test_a_kept_map_records_completed_steps_for_a_restarted_daemon(
    workspace: Path,
) -> None:
    """DL1-ABORT: after a partly failed --apply, restarting the current
    daemon moves the directory the helper moved back forward again; it does
    not archive it and hand the workstream an empty directory."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Продажі", "PR")])
    root = workspace / "workstreams"
    (root / "ws-pr" / "tasks").mkdir()
    (root / "ws-pr" / "tasks" / "a.txt").write_text("pr work")
    (root / "late" / "tasks").mkdir(parents=True)
    (root / "late" / "tasks" / "x.txt").write_text("late write")
    (root / ".archived").write_text("not a directory")

    outcome = apply_rollback(plan_rollback(workspace))

    assert (root / "office" / "tasks" / "a.txt").read_text() == "pr work"
    assert any("late" in line for line in outcome.failed)
    kept = json.loads((workspace / MAP_DIRNAME / MAP_FILENAME).read_text())
    assert kept["workstreams"][ALPHA] == "office"

    # The operator abandons the rollback and restarts the current daemon.
    (root / ".archived").unlink()
    ClaudeMdWriter(str(workspace)).sync_workstream_directories(
        [_ws(ALPHA, "Продажі", "PR")]
    )

    assert (root / "ws-pr" / "tasks" / "a.txt").read_text() == "pr work"
    assert not (root / "office").exists()


def test_a_rerun_after_a_kept_map_update_plans_only_what_is_left(
    workspace: Path,
) -> None:
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Продажі", "PR")])
    root = workspace / "workstreams"
    (root / "ws-pr" / "tasks").mkdir()
    (root / "ws-pr" / "tasks" / "a.txt").write_text("pr work")
    (root / "late").mkdir()
    (root / "late" / "x.txt").write_text("late write")
    (root / ".archived").write_text("not a directory")
    apply_rollback(plan_rollback(workspace))
    (root / ".archived").unlink()

    plan = plan_rollback(workspace)

    assert [(step.action, step.source) for step in plan.steps] == [
        ("archive", "late"),
        ("set-aside", MAP_FILENAME),
    ]
    assert apply_rollback(plan).failed == []
    _old_daemon_sync(workspace, ["Продажі"])
    assert (root / "office" / "tasks" / "a.txt").read_text() == "pr work"


def test_cli_reports_a_failed_step_and_keeps_the_map(workspace: Path) -> None:
    root = _two_shared_non_latin_workstreams(workspace)
    (root / ".archived").write_text("not a directory")

    result = CliRunner().invoke(
        prepare_rollback, ["--workspace", str(workspace), "--apply"]
    )

    assert result.exit_code != 0
    assert "FAILED (source kept): set-aside" in result.output
    assert "re-run with --apply" in result.output
    assert (workspace / MAP_DIRNAME / MAP_FILENAME).is_file()


def test_a_map_an_earlier_run_set_aside_still_plans_the_v1_directories(
    workspace: Path,
) -> None:
    """DL-1: a helper that set the map aside despite failed steps left the
    live map missing; the newest set-aside map is used, for directories an
    older daemon never creates only."""
    root = _two_shared_non_latin_workstreams(workspace)
    before = _payloads(workspace)
    map_dir = workspace / MAP_DIRNAME
    (map_dir / MAP_FILENAME).rename(
        map_dir / f"{MAP_FILENAME}.pre-rollback-20260101T000000Z"
    )
    (map_dir / f"{MAP_FILENAME}.pre-rollback-20250101T000000Z").write_text(
        json.dumps({"version": 2, "workstreams": {}})
    )
    # A directory the older daemon may have created since: never touched.
    (root / "beta").mkdir()
    (root / "beta" / "notes.txt").write_text("beta work")

    plan = plan_rollback(workspace)

    assert any("pre-rollback-20260101T000000Z" in note for note in plan.notes)
    assert sorted((step.action, step.source) for step in plan.steps) == [
        ("archive", "ws-ma"),
        ("archive", "ws-pr"),
    ]
    assert plan.warnings == []
    assert apply_rollback(plan).failed == []
    _old_daemon_sync(workspace, ["Продажі", "Маркетинг", "Beta"])

    assert _payloads(workspace) == sorted([*before, "beta work"])
    assert (root / "beta" / "notes.txt").read_text() == "beta work"


def test_a_set_aside_map_never_moves_a_name_an_older_daemon_may_reuse(
    workspace: Path,
) -> None:
    """DL1-SETASIDE-SCOPE: a set-aside map (from an earlier helper that did
    not record completed steps) names ws-pr and alpha, which an older daemon
    has since created for its own workstreams "WS PR" and "Alpha". They are
    reported, never moved or merged; a real v1 ws-<code> directory still
    is placed."""
    root = workspace / "workstreams"
    for name, heading in (("ws-pr", "WS PR"), ("alpha", "Alpha")):
        (root / name).mkdir(parents=True)
        (root / name / "CLAUDE.md").write_text(f"# Workstream: {heading}\n")
        (root / name / "spec.md").write_text(f"{heading} spec")
    (root / "ws-ma").mkdir()
    (root / "ws-ma" / "CLAUDE.md").write_text("# Workstream: Маркетинг\n")
    (root / "ws-ma" / "notes.txt").write_text("ma work")
    map_dir = workspace / MAP_DIRNAME
    map_dir.mkdir()
    (map_dir / f"{MAP_FILENAME}.pre-rollback-20260101T000000Z").write_text(
        json.dumps(
            {
                "version": 2,
                "workstreams": {ALPHA: "ws-pr", BETA: "alpha", GAMMA: "ws-ma"},
                "legacy": {ALPHA: "office", BETA: "beta", GAMMA: "marketing"},
            }
        )
    )

    plan = plan_rollback(workspace)

    assert [(step.action, step.source, step.target) for step in plan.steps] == [
        ("move", "ws-ma", "marketing")
    ]
    assert sorted(w.split(":")[0] for w in plan.warnings) == [
        "workstreams/alpha",
        "workstreams/ws-pr",
    ]


def test_the_newest_set_aside_map_is_chosen(workspace: Path) -> None:
    map_dir = workspace / MAP_DIRNAME
    map_dir.mkdir()
    prefix = f"{MAP_FILENAME}.pre-rollback-"
    for suffix in ("20250101T000000Z", "20260101T000000Z", "20260101T000000Z-2"):
        (map_dir / f"{prefix}{suffix}").write_text("{}")
    (map_dir / f"{prefix}20260101T000000Z-10").write_text("{}")
    (map_dir / f"{prefix}20270101T000000Z").mkdir()
    (map_dir / f"{prefix}later").write_text("{}")

    assert (
        workstream_dirs_rollback._newest_set_aside_map(workspace)
        == f"{prefix}20260101T000000Z-10"
    )


def test_a_set_aside_deleted_claim_is_reported_not_archived(workspace: Path) -> None:
    """In the set-aside fallback a deleted workstream's recorded directory may
    have been reused by an older daemon: nothing is archived or merged into
    it, and both directories are reported."""
    root = workspace / "workstreams"
    (root / "ws-pr" / "tasks").mkdir(parents=True)
    (root / "ws-pr" / "tasks" / "out.txt").write_text("pr work")
    (root / "beta").mkdir()
    (root / "beta" / "notes.txt").write_text("beta work")
    map_dir = workspace / MAP_DIRNAME
    map_dir.mkdir()
    (map_dir / f"{MAP_FILENAME}.pre-rollback-20260101T000000Z").write_text(
        json.dumps(
            {
                "version": 2,
                "workstreams": {ALPHA: "ws-pr"},
                "legacy": {ALPHA: "beta"},
                "deleted": {BETA: "beta"},
            }
        )
    )

    plan = plan_rollback(workspace)

    assert plan.steps == []
    assert len(plan.warnings) == 2
    assert any("workstreams/beta" in warning for warning in plan.warnings)
    assert any("workstreams/ws-pr" in warning for warning in plan.warnings)


def test_no_map_with_a_v1_directory_left_warns_and_fails_the_cli(
    workspace: Path,
) -> None:
    """DL-1: without any map, a ws-<code> or staged directory with content is
    reported, not silently left for the older daemon to delete."""
    root = workspace / "workstreams"
    (root / "ws-pr" / "tasks").mkdir(parents=True)
    (root / "ws-pr" / "tasks" / "out.txt").write_text("pr work")
    (root / f"{RELOCATING_PREFIX}{ALPHA}").mkdir()
    (root / f"{RELOCATING_PREFIX}{ALPHA}" / "spec.txt").write_text("staged")
    # Not warned: only regenerable content, or the legacy slug of its own
    # workstream ("WS Foo" -> ws-foo).
    (root / "ws-ma").mkdir()
    (root / "ws-ma" / "CLAUDE.md").write_text("# Workstream: Маркетинг\n")
    (root / "ws-foo").mkdir()
    (root / "ws-foo" / "CLAUDE.md").write_text("# Workstream: WS Foo\n")
    (root / "ws-foo" / "notes.txt").write_text("foo work")

    plan = plan_rollback(workspace)

    assert plan.steps == []
    assert sorted(w.split()[0] for w in plan.warnings) == [
        f"workstreams/{RELOCATING_PREFIX}{ALPHA}",
        "workstreams/ws-pr",
    ]
    result = CliRunner().invoke(prepare_rollback, ["--workspace", str(workspace)])
    assert result.exit_code != 0
    assert "WARNING: workstreams/ws-pr" in result.output


def test_a_deleted_claim_a_new_workstream_waits_for_is_archived_first(
    workspace: Path,
) -> None:
    """R3-HELPER-DELETED-CLAIM: the daemon keeps B's claim while a new
    workstream named for it waits (not recorded there); the helper archives
    B's directory before anything else, so the older daemon gives the new
    workstream a fresh directory."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories(
        [_ws(ALPHA, "Alpha", "AL"), _ws(BETA, "Beta", "BE")]
    )
    root = workspace / "workstreams"
    (root / "beta" / "spec.md").write_text("deleted beta spec")
    (root / "beta" / "learnings.md").write_text("beta lessons")
    (root / ".archived").write_text("not a directory")
    _block_aside(root, BETA)
    rows = [_ws(ALPHA, "Alpha", "AL"), _ws(GAMMA, "Beta", "CE")]
    writer.sync_workstream_directories(rows)
    writer.sync_workstream_directories(rows)

    result = CliRunner().invoke(
        prepare_rollback, ["--workspace", str(workspace), "--apply"]
    )
    assert result.exit_code != 0
    assert (workspace / MAP_DIRNAME / MAP_FILENAME).is_file()

    (root / ".archived").unlink()
    plan = plan_rollback(workspace)
    assert ("archive", "beta") in [(step.action, step.source) for step in plan.steps]
    assert apply_rollback(plan).failed == []
    _old_daemon_sync(workspace, ["Alpha", "Beta"])

    assert sorted(p.name for p in (root / "beta").iterdir()) == ["CLAUDE.md"]
    [archived] = sorted((root / ".archived").iterdir())
    assert (archived / "spec.md").read_text() == "deleted beta spec"
    assert (archived / "learnings.md").read_text() == "beta lessons"


def test_a_shared_legacy_directory_is_never_merged_into(workspace: Path) -> None:
    """R3-HELPER-SHARED-LEGACY: the kept workstream is not merged into a
    legacy directory that also holds another workstream's content; both are
    left in place with a warning and a non-zero exit."""
    writer = ClaudeMdWriter(str(workspace))
    root = workspace / "workstreams"
    writer.sync_workstream_directories([_ws(ALPHA, "Продажі", "PR")])
    (root / "ws-pr" / "spec.md").write_text("pr spec")
    writer.sync_workstream_directories([_legacy(ALPHA, "Продажі", "PR")])
    (root / "office" / "intake").mkdir(parents=True)
    (root / "office" / "intake" / "001-a.json").write_text("{}")
    writer.sync_workstream_directories(
        [_legacy(ALPHA, "Продажі", "PR"), _legacy(BETA, "Маркетинг", "MA")]
    )
    (root / "office" / "tasks" / "tb").mkdir(parents=True)
    (root / "office" / "tasks" / "tb" / "b.txt").write_text("b work")
    writer.sync_workstream_directories(
        [_legacy(ALPHA, "Продажі", "PR"), _legacy(BETA, "Marketing", "MA")]
    )
    assert json.loads((workspace / MAP_DIRNAME / MAP_FILENAME).read_text())[
        "shared_legacy"
    ] == ["office"]

    plan = plan_rollback(workspace)

    assert not any(step.target == "office" for step in plan.steps)
    assert any("workstreams/ws-pr" in warning for warning in plan.warnings)
    result = CliRunner().invoke(
        prepare_rollback, ["--workspace", str(workspace), "--apply"]
    )
    assert result.exit_code != 0
    assert (root / "ws-pr" / "spec.md").read_text() == "pr spec"
    assert not (root / "office" / "spec.md").exists()
    assert (workspace / MAP_DIRNAME / MAP_FILENAME).is_file()


def test_a_failed_shared_archive_still_archives_the_other_claimant_on_rerun(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3-RECORD-POPS-SHARED: after one of two workstreams sharing a legacy
    directory is archived and the other's archive fails, the kept map still
    records both, so the re-run archives the second instead of moving it into
    the shared legacy directory."""
    root = _two_shared_non_latin_workstreams(workspace)
    real_archive = workstream_dirs_rollback._archive

    def failing_archive(root_fd, name, stamp):
        if name == "ws-ma":
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        return real_archive(root_fd, name, stamp)

    monkeypatch.setattr(workstream_dirs_rollback, "_archive", failing_archive)
    outcome = apply_rollback(plan_rollback(workspace))
    assert any("ws-ma" in line for line in outcome.failed)
    monkeypatch.setattr(workstream_dirs_rollback, "_archive", real_archive)

    plan = plan_rollback(workspace)

    assert [(step.action, step.source) for step in plan.steps] == [
        ("archive", "ws-ma"),
        ("set-aside", MAP_FILENAME),
    ]
    assert apply_rollback(plan).failed == []
    assert not (root / "office").exists()


def test_an_unattributable_deleted_claim_is_left_with_a_warning(
    workspace: Path,
) -> None:
    """A deleted claim with no recorded identity at a name a workstream is
    recorded at (a map from before identities) is not archived: it is left
    in place with a warning and the command exits non-zero."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Beta", "AL")])
    root = workspace / "workstreams"
    (root / "beta" / "spec.md").write_text("alpha spec")
    map_file = workspace / MAP_DIRNAME / MAP_FILENAME
    raw = json.loads(map_file.read_text())
    raw["deleted"] = {BETA: "beta"}
    raw.pop("deleted_identity", None)
    map_file.write_text(json.dumps(raw))

    plan = plan_rollback(workspace)

    assert not any(step.source == "beta" for step in plan.steps)
    assert any("workstreams/beta" in warning for warning in plan.warnings)
    result = CliRunner().invoke(
        prepare_rollback, ["--workspace", str(workspace), "--apply"]
    )
    assert result.exit_code != 0
    assert (root / "beta" / "spec.md").read_text() == "alpha spec"


def test_a_damaged_map_is_a_problem_not_nothing_to_do(workspace: Path) -> None:
    (workspace / "workstreams" / "alpha").mkdir(parents=True)
    (workspace / MAP_DIRNAME).mkdir()
    (workspace / MAP_DIRNAME / MAP_FILENAME).write_text("{not json")

    plan = plan_rollback(workspace)

    assert plan.steps == []
    assert any("cannot be read" in warning for warning in plan.warnings)


def test_cli_dry_run_by_default_and_apply_refused_while_daemon_runs(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ClaudeMdWriter(str(workspace)).sync_workstream_directories(
        [_ws(ALPHA, "Продажі", "PR")]
    )
    root = workspace / "workstreams"
    runner = CliRunner()

    result = runner.invoke(prepare_rollback, ["--workspace", str(workspace)])
    assert result.exit_code == 0, result.output
    assert "move" in result.output and "Dry run" in result.output
    assert (root / "ws-pr").is_dir()

    monkeypatch.setattr(workstream_dirs_rollback, "daemon_is_running", lambda: True)
    result = runner.invoke(prepare_rollback, ["--workspace", str(workspace), "--apply"])
    assert result.exit_code != 0
    assert "Stop cbcl" in result.output
    assert (root / "ws-pr").is_dir()

    monkeypatch.setattr(workstream_dirs_rollback, "daemon_is_running", lambda: False)
    result = runner.invoke(prepare_rollback, ["--workspace", str(workspace), "--apply"])
    assert result.exit_code == 0, result.output
    assert (root / "office").is_dir()
    assert not (root / "ws-pr").exists()


def test_cli_is_registered_on_cbcl() -> None:
    from src.main import cli

    result = CliRunner().invoke(cli, ["workstream-dirs", "prepare-rollback", "--help"])
    assert result.exit_code == 0
    assert "--apply" in result.output


def _deleted_office_orphan_under_a_legacy_name(
    workspace: Path, *, claim_kept: bool = False
) -> Path:
    """Model seed 2691: the deleted "Office" workstream's directory could not
    be archived; "Фінанси" now has office as its legacy directory. The sync
    that failed to archive it records a claim (under ALPHA, the id its
    CLAUDE.md names); unless ``claim_kept``, the map is then restored from
    a backup that predates the claim, so no map records it."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Office", "OF")])
    root = workspace / "workstreams"
    (root / "office" / "tasks").mkdir()
    (root / "office" / "tasks" / "old.txt").write_text("deleted office work")
    (workspace / MAP_DIRNAME / MAP_FILENAME).unlink()
    (root / ".archived").write_text("broken")
    writer.sync_workstream_directories([_ws(BETA, "Фінанси", "FI")])
    (root / ".archived").unlink()
    (root / "ws-fi" / "tasks").mkdir()
    (root / "ws-fi" / "tasks" / "fi.txt").write_text("fi work")
    map_path = workspace / MAP_DIRNAME / MAP_FILENAME
    saved = json.loads(map_path.read_text())
    assert saved["legacy"] == {BETA: "office"}
    assert saved["deleted"] == {ALPHA: "office"}
    if not claim_kept:
        saved["deleted"] = {}
        saved["deleted_identity"] = {}
        map_path.write_text(json.dumps(saved))
    return root


def test_a_legacy_directory_naming_a_deleted_workstream_is_left_with_a_warning(
    workspace: Path,
) -> None:
    """Model seed 2691: the legacy target's CLAUDE.md names a deleted
    workstream. A marker only vetoes: the helper neither merges into it nor
    archives it, and exits non-zero until it is placed by hand."""
    root = _deleted_office_orphan_under_a_legacy_name(workspace)

    plan = plan_rollback(workspace)

    assert not [s for s in plan.steps if "ws-fi" in (s.source, s.target)]
    assert not [s for s in plan.steps if s.source == "office"]
    assert any("holds workstream" in warning for warning in plan.warnings)
    assert apply_rollback(plan).failed == []
    assert (root / "ws-fi" / "tasks" / "fi.txt").exists()
    assert (root / "office" / "tasks" / "old.txt").exists()


def test_a_claimed_orphan_under_a_legacy_name_is_archived_for_its_workstream(
    workspace: Path,
) -> None:
    """With the claim the daemon recorded for the unarchivable orphan, the map
    authorizes its archive: "Фінанси" then moves to its legacy directory and
    an older daemon loses nothing."""
    root = _deleted_office_orphan_under_a_legacy_name(workspace, claim_kept=True)
    before = _payloads(workspace)

    plan = plan_rollback(workspace)

    assert [(s.action, s.source) for s in plan.steps][:2] == [
        ("archive", "office"),
        ("move", "ws-fi"),
    ]
    assert plan.warnings == []
    assert apply_rollback(plan).failed == []
    _old_daemon_sync(workspace, ["Фінанси"])
    assert (root / "office" / "tasks" / "fi.txt").read_text() == "fi work"
    assert _payloads(workspace) == before


def test_an_unreadable_legacy_directory_is_left_with_a_warning(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _deleted_office_orphan_under_a_legacy_name(workspace)

    def unreadable(root_fd: int, name: str) -> None:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(workstream_dirs_rollback, "directory_attribution", unreadable)
    plan = plan_rollback(workspace)

    assert not [s for s in plan.steps if "ws-fi" in (s.source, s.target)]
    assert any("cannot be read" in warning for warning in plan.warnings)
    assert (root / "ws-fi" / "tasks" / "fi.txt").exists()


def test_an_unrecorded_directory_with_an_unreadable_claude_md_is_left(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nobody's per the map, but whose content it is cannot be told: it is
    left with a warning (the CLI exits non-zero), never handed to an older
    daemon that deletes it or takes it as a same-named workstream's."""
    ClaudeMdWriter(str(workspace)).sync_workstream_directories(
        [_ws(ALPHA, "Alpha", "AL")]
    )
    root = workspace / "workstreams"
    _write_marker(root / "stray", BETA, "Stray")
    (root / "stray" / "work.txt").write_text("someone's")
    original = workstream_dirs_rollback.directory_attribution

    def unreadable(root_fd: int, name: str):
        if name == "stray":
            raise PermissionError(errno.EACCES, "Permission denied")
        return original(root_fd, name)

    monkeypatch.setattr(workstream_dirs_rollback, "directory_attribution", unreadable)
    plan = plan_rollback(workspace)

    assert not [s for s in plan.steps if s.source == "stray"]
    assert any(
        "stray" in warning and "cannot be read" in warning for warning in plan.warnings
    )


@pytest.mark.parametrize("helper_reads_it", [False, True])
def test_an_unverified_legacy_guess_is_not_handed_to_an_older_daemon(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, helper_reads_it: bool
) -> None:
    """The daemon's first sync guesses that Office was at office, but cannot
    check it (the CLAUDE.md there cannot be read: it is a deleted
    workstream's). An older daemon would take office for Office as it is, so
    the helper leaves it with a warning (the CLI exits non-zero) until the
    daemon has checked the guess, also when the helper itself can read the
    file and it names another workstream."""
    from src.config_sync import workstream_dirs

    root = workspace / "workstreams"
    _write_marker(root / "office", BETA, "Delta")
    (root / "office" / "t.txt").write_text("beta's")
    original = workstream_dirs.directory_attribution

    def unreadable(root_fd: int, name: str):
        if name == "office":
            raise PermissionError(errno.EACCES, "Permission denied")
        return original(root_fd, name)

    with monkeypatch.context() as patched:
        patched.setattr(workstream_dirs, "directory_attribution", unreadable)
        ClaudeMdWriter(str(workspace)).sync_workstream_directories(
            [_ws(GAMMA, "Office", "OF")]
        )
    saved = json.loads((workspace / MAP_DIRNAME / MAP_FILENAME).read_text())
    assert saved["unverified"] == {GAMMA: "office"}
    if not helper_reads_it:
        monkeypatch.setattr(
            workstream_dirs_rollback, "directory_attribution", unreadable
        )

    plan = plan_rollback(workspace)

    assert not [step for step in plan.steps if "office" in (step.source, step.target)]
    assert any("workstreams/office" in warning for warning in plan.warnings)
    assert (root / "office" / "t.txt").read_text() == "beta's"


def test_applying_other_steps_keeps_an_unverified_guess_a_guess(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B4-bugs-2: the helper warns about an unchecked legacy guess (Foo at
    foo, whose CLAUDE.md cannot be read and names the deleted Beta) and
    applies its other steps. The map it saves must keep the guess under
    ``unverified``: saved as a trusted entry, a daemon restarted after the
    helper adopted foo for Foo and wrote Foo's CLAUDE.md over Beta's
    leftover content."""
    from src.config_sync import workstream_dirs

    root = workspace / "workstreams"
    _write_marker(root / "foo", BETA, "Delta")
    (root / "foo" / "t.txt").write_text("beta's")
    original = workstream_dirs.directory_attribution

    def unreadable(root_fd: int, name: str):
        if name == "foo":
            raise PermissionError(errno.EACCES, "Permission denied")
        return original(root_fd, name)

    monkeypatch.setattr(workstream_dirs, "directory_attribution", unreadable)
    monkeypatch.setattr(workstream_dirs_rollback, "directory_attribution", unreadable)
    rows = [_ws(ALPHA, "Проект", "PR"), _ws(GAMMA, "Foo", "FO")]
    ClaudeMdWriter(str(workspace)).sync_workstream_directories(rows)
    map_path = workspace / MAP_DIRNAME / MAP_FILENAME
    assert json.loads(map_path.read_text())["unverified"] == {GAMMA: "foo"}

    plan = plan_rollback(workspace)
    assert [(step.source, step.target) for step in plan.steps] == [("ws-pr", "office")]
    assert any("workstreams/foo" in warning for warning in plan.warnings)

    outcome = apply_rollback(plan)

    assert not outcome.failed
    saved = json.loads(map_path.read_text())
    assert saved["workstreams"].get(ALPHA) == "office"
    assert saved["unverified"] == {GAMMA: "foo"}
    assert GAMMA not in saved["workstreams"]
    # The restarted daemon still treats foo as an unchecked guess: it does
    # not write Foo's CLAUDE.md there.
    ClaudeMdWriter(str(workspace)).sync_workstream_directories(rows)
    assert "# Workstream: Delta" in (root / "foo" / "CLAUDE.md").read_text()
    assert (root / "foo" / "t.txt").read_text() == "beta's"


def test_after_lost_saves_the_helper_uses_the_current_names_legacy_directory(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Beta is renamed to Delta; the daemon moves beta to delta, but its final
    saves fail (a full disk). The write-ahead base already carries Delta's
    legacy name, so the helper leaves delta where the older daemon (which now
    sees "Delta") looks for it, instead of moving it to beta for that daemon
    to delete."""
    from src.config_sync.workstream_dirs import WorkstreamDirectoryMap

    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Beta", "BE")])
    root = workspace / "workstreams"
    (root / "beta" / "tasks").mkdir()
    (root / "beta" / "tasks" / "b.txt").write_text("beta work")
    original = WorkstreamDirectoryMap.write_payload

    def final_save_fails(self, payload: dict, *, strict: bool = False) -> None:
        if "pending" not in payload:
            raise OSError(errno.ENOSPC, "No space left on device")
        original(self, payload, strict=strict)

    monkeypatch.setattr(WorkstreamDirectoryMap, "write_payload", final_save_fails)
    for _ in range(2):
        with pytest.raises(OSError):
            writer.sync_workstream_directories([_ws(ALPHA, "Delta", "BE")])
    monkeypatch.undo()
    assert (root / "delta" / "tasks" / "b.txt").exists()

    _apply(workspace)
    _old_daemon_sync(workspace, ["Delta"])

    assert (root / "delta" / "tasks" / "b.txt").read_text() == "beta work"


def test_a_merge_whose_leftover_cannot_be_archived_is_still_recorded(
    workspace: Path,
) -> None:
    """ws-pr is merged into Продажі's existing legacy directory; its leftover
    CLAUDE.md cannot be archived (.archived is broken). The step fails (the
    map is kept, the CLI exits non-zero), but the map records where the
    content went, so the restarted daemon moves it back instead of treating
    it as somebody else's."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Продажі", "PR")])
    root = workspace / "workstreams"
    (root / "ws-pr" / "tasks").mkdir()
    (root / "ws-pr" / "tasks" / "out.txt").write_text("pr work")
    _write_marker(root / "office", ALPHA, "Продажі")
    (root / ".archived").write_text("not a directory")
    plan = plan_rollback(workspace)
    assert [(s.action, s.source, s.target) for s in plan.steps][:1] == [
        ("merge", "ws-pr", "office")
    ]

    outcome = apply_rollback(plan)

    assert outcome.failed
    assert (root / "office" / "tasks" / "out.txt").read_text() == "pr work"
    map_path = workspace / MAP_DIRNAME / MAP_FILENAME
    assert json.loads(map_path.read_text())["workstreams"][ALPHA] == "office"
    writer.sync_workstream_directories([_ws(ALPHA, "Продажі", "PR")])
    assert (root / "ws-pr" / "tasks" / "out.txt").read_text() == "pr work"
    assert not (root / "office").exists()


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("apply_changes", [False, True])
def test_cli_finding_no_workspaces_fails_and_names_where_it_looked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
    apply_changes: bool,
) -> None:
    """R4-HELPER-NO-WORKSPACES-EXIT0: run as a user whose home is not the
    daemon's, the default discovery finds nothing; that must not read as
    "no problems" (the next step starts an older daemon)."""
    root = tmp_path / "home" / ".cubicle" / "workspaces"
    if existing:
        root.mkdir(parents=True)
    monkeypatch.setattr(
        workstream_dirs_rollback, "_default_workspaces_root", lambda: root
    )
    monkeypatch.setattr(workstream_dirs_rollback, "daemon_is_running", lambda: False)

    result = CliRunner().invoke(prepare_rollback, ["--apply"] if apply_changes else [])

    assert result.exit_code != 0
    assert str(root) in result.output
    assert "--workspace" in result.output and "uid" in result.output


def _write_marker(directory: Path, workstream_id: str, name: str = "X") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "CLAUDE.md").write_text(
        f"# Workstream: {name}\n<!-- workstream-id: {workstream_id} -->\n\n"
        "**Short code:** `XX` · **Priority:** `medium`\n"
    )


def test_a_directory_whose_claude_md_names_another_workstream_is_not_moved(
    workspace: Path,
) -> None:
    """A stale map entry (the map says ws-pr is Продажі's, its CLAUDE.md
    names Beta) never moves the directory back to Продажі's legacy name."""
    ClaudeMdWriter(str(workspace)).sync_workstream_directories(
        [_ws(ALPHA, "Продажі", "PR"), _ws(BETA, "Beta", "BE")]
    )
    root = workspace / "workstreams"
    _write_marker(root / "ws-pr", BETA, "Beta")

    plan = plan_rollback(workspace)

    assert not [s for s in plan.steps if s.source == "ws-pr"]
    assert any("ws-pr" in warning and BETA in warning for warning in plan.warnings)


def test_a_deleted_claim_whose_claude_md_names_a_current_workstream_is_left(
    workspace: Path,
) -> None:
    """R4-IDENTITY-INODE-REUSE (helper): the recorded identity matches but
    the directory's CLAUDE.md names a current workstream."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(BETA, "Beta", "BE")])
    root = workspace / "workstreams"
    (root / "beta" / "tasks").mkdir()
    (root / "beta" / "tasks" / "g.txt").write_text("gamma work")
    map_path = workspace / MAP_DIRNAME / MAP_FILENAME
    stale = json.loads(map_path.read_text())
    info = os.stat(root / "beta")
    stale["deleted"] = {ALPHA: "beta"}
    stale["deleted_identity"] = {ALPHA: [info.st_dev, info.st_ino]}
    map_path.write_text(json.dumps(stale))

    plan = plan_rollback(workspace)

    assert not [s for s in plan.steps if s.source == "beta" and s.action == "archive"]
    assert any("beta" in warning and BETA in warning for warning in plan.warnings)


def test_an_unrecorded_directory_naming_a_current_workstream_is_left(
    workspace: Path,
) -> None:
    ClaudeMdWriter(str(workspace)).sync_workstream_directories(
        [_ws(ALPHA, "Alpha", "AL")]
    )
    root = workspace / "workstreams"
    _write_marker(root / "stray", ALPHA, "Alpha")
    (root / "stray" / "work.txt").write_text("alpha's")

    plan = plan_rollback(workspace)

    assert not [s for s in plan.steps if s.source == "stray"]
    assert any("stray" in warning for warning in plan.warnings)


def test_a_legacy_target_a_marker_veto_leaves_in_place_takes_nothing_in(
    workspace: Path,
) -> None:
    """ws-pr is left in place (its CLAUDE.md names Gamma, not Продажі); Beta,
    whose legacy directory the map records as ws-pr, is not merged into it
    either: its owner is uncertain."""
    ClaudeMdWriter(str(workspace)).sync_workstream_directories(
        [_ws(ALPHA, "Продажі", "PR"), _ws(BETA, "Beta", "BE")]
    )
    root = workspace / "workstreams"
    _write_marker(root / "ws-pr", GAMMA, "Gamma")
    (root / "beta" / "b.txt").write_text("beta work")
    map_path = workspace / MAP_DIRNAME / MAP_FILENAME
    recorded = json.loads(map_path.read_text())
    recorded["legacy"][BETA] = "ws-pr"
    map_path.write_text(json.dumps(recorded))

    plan = plan_rollback(workspace)

    assert not [s for s in plan.steps if s.source == "beta" or s.target == "ws-pr"]
    assert any(
        "workstreams/beta" in warning and "ws-pr" in warning
        for warning in plan.warnings
    )


def test_an_interrupted_sync_record_is_refused_until_the_daemon_completes_it(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A map with a write-ahead record does not say where directories are:
    the helper plans nothing, fails, and says how to complete the record."""
    from src.config_sync.workstream_dirs import WorkstreamDirectoryMap

    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories([_ws(ALPHA, "Alpha", "AB")])
    (workspace / "workstreams" / "alpha" / "a.txt").write_text("a")

    class Crash(BaseException):
        pass

    with monkeypatch.context() as patched:

        def crash(self, *, strict: bool = False) -> None:
            raise Crash

        patched.setattr(WorkstreamDirectoryMap, "save", crash)
        with pytest.raises(Crash):
            writer.sync_workstream_directories([_ws(ALPHA, "Beta", "AB")])

    plan = plan_rollback(workspace)
    assert plan.steps == []
    assert any("interrupted sync" in warning for warning in plan.warnings)
    result = CliRunner().invoke(
        prepare_rollback, ["--workspace", str(workspace), "--apply"]
    )
    assert result.exit_code != 0
    assert (workspace / "workstreams" / "beta" / "a.txt").exists()

    # One sync of the workstream_dirs_v1 daemon completes it.
    writer.sync_workstream_directories([_ws(ALPHA, "Beta", "AB")])
    assert plan_rollback(workspace).steps


def test_a_deleted_directory_moved_aside_is_archived_before_an_older_daemon_runs(
    workspace: Path,
) -> None:
    """An older daemon deletes every unknown directory without spec.md (it
    does not skip dot-names): the helper archives ``.deleted-<id>``."""
    writer = ClaudeMdWriter(str(workspace))
    writer.sync_workstream_directories(
        [_ws(ALPHA, "Alpha", "AL"), _ws(BETA, "Beta", "BE")]
    )
    root = workspace / "workstreams"
    (root / "beta" / "tasks").mkdir()
    (root / "beta" / "tasks" / "b.txt").write_text("deleted beta work")
    (root / ".archived").write_text("not a directory")
    writer.sync_workstream_directories(
        [_ws(ALPHA, "Alpha", "AL"), _ws(GAMMA, "Beta", "CE")]
    )
    aside = root / f"{DELETED_PREFIX}{BETA}"
    assert (aside / "tasks" / "b.txt").exists()
    (root / ".archived").unlink()

    plan = plan_rollback(workspace)
    assert ("archive", aside.name) in [(s.action, s.source) for s in plan.steps]
    assert apply_rollback(plan).failed == []
    _old_daemon_sync(workspace, ["Alpha", "Beta"])

    [archived] = sorted((root / ".archived").iterdir())
    assert (archived / "tasks" / "b.txt").read_text() == "deleted beta work"
    assert not aside.exists()


def test_writer_and_both_parsers_share_one_workstream_heading(
    workspace: Path,
) -> None:
    """B4-hygiene-03: the rollback helper reads the heading the daemon
    writes with the same constant as ``directory_attribution``, so a change
    to the heading cannot leave one parser behind."""
    from src.config_sync import workstream_dirs
    from src.config_sync._descriptor_io import open_dir_nofollow
    from src.config_sync.claude_md_content import generate_workstream_claude_md

    assert workstream_dirs_rollback._HEADING is workstream_dirs._HEADING
    row = _ws(ALPHA, "Sales Team", "ST")
    assert generate_workstream_claude_md(row).startswith(workstream_dirs._HEADING)
    ClaudeMdWriter(str(workspace)).sync_workstream_directories([row])

    with open_dir_nofollow(workspace / "workstreams") as root_fd:
        assert workstream_dirs.directory_attribution(root_fd, "sales-team") == (
            ALPHA,
            "ST",
        )
        assert (
            workstream_dirs_rollback._legacy_from_claude_md(root_fd, "sales-team")
            == "sales-team"
        )
