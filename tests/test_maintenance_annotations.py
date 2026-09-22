import time
import os
import pytest
from click.testing import CliRunner
from src.cli_commands import cli
from src.operations.maintenance import annotate, read_annotation
from src.runtime_state import RuntimeState


def test_annotation_does_not_change_admission_and_expiry_is_fixed(
    tmp_path, monkeypatch
):
    database = tmp_path / "control.sqlite3"
    runtime = RuntimeState(database, "office")
    annotations = database.with_name("maintenance-annotations.sqlite3")
    now = time.time()
    monkeypatch.setattr("src.operations.maintenance.time.time", lambda: now)
    saved = annotate(
        annotations,
        scope="*",
        owner="release-a",
        reason="Staged upgrade",
        duration=7200,
    )
    assert runtime.admission_open()
    runtime.set_maintenance(True)
    assert (
        runtime.maintenance_status()["annotation"]["expires_at"] == saved["expires_at"]
    )
    now += 7201
    assert read_annotation(annotations, "office") is None
    assert not runtime.admission_open()


def test_annotation_keeps_scope_owner_and_bounded_duration(tmp_path):
    path = tmp_path / "annotations.sqlite3"
    annotate(path, scope="a", owner="release-a", reason="Upgrade", duration=10)
    assert read_annotation(path, "b") is None
    with pytest.raises(ValueError, match="different operator"):
        annotate(path, scope="a", owner="release-b", reason="Upgrade", duration=10)
    with pytest.raises(ValueError, match="duration"):
        annotate(path, scope="a", owner="release-a", reason="Upgrade", duration=21601)


def test_cli_annotation_is_not_a_pause(tmp_path, monkeypatch):
    database = tmp_path / "control.sqlite3"
    monkeypatch.setattr("src.paths.get_runtime_state_path", lambda: database)
    runtime = RuntimeState(database, "office")
    result = CliRunner().invoke(
        cli,
        [
            "maintenance",
            "annotate",
            "--owner",
            "release-a",
            "--reason",
            "Backup and upgrade",
        ],
    )
    assert result.exit_code == 0, result.output
    assert runtime.admission_open()


def test_annotation_rejects_aliased_store_without_mutating_target(tmp_path):
    original = tmp_path / "original"
    original.write_text("unrelated contents")
    original.chmod(0o644)
    alias = tmp_path / "annotations.sqlite3"
    os.link(original, alias)
    with pytest.raises(ValueError, match="ownership"):
        annotate(alias, scope="*", owner="operator", reason="Upgrade", duration=10)
    assert original.read_text() == "unrelated contents"
    assert original.stat().st_mode & 0o777 == 0o644
    alias.unlink()
    folder = tmp_path / "alias-folder"
    folder.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        annotate(
            folder / "annotations.sqlite3",
            scope="*",
            owner="operator",
            reason="Upgrade",
            duration=10,
        )
    assert not alias.exists()
