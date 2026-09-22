"""Release, archive and retention guarantees use disposable local artifacts."""

import gzip
import hashlib
import io
import json
import os
import sqlite3
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from src.operations.archive import ArchiveVerificationError, verify_archive
from src.operations.cli import operations
from src.operations.release import preflight, verify_release_phase
from src.operations.retention import inventory
from src.runtime_state import RuntimeState


def _archive(path, names=("runtime/control.sqlite3", "workspace/source.py")):
    with tarfile.open(path, "w:gz") as archive:
        for name in names:
            entry = tarfile.TarInfo(name)
            entry.size = 4
            archive.addfile(entry, io.BytesIO(b"data"))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_installed_old_database_defers_native_tables_without_mutating(tmp_path):
    database = tmp_path / "runtime.sqlite3"
    RuntimeState(database, "office")
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE script_resource_leases")
        connection.execute("DROP TABLE worker_executions")
        connection.execute("INSERT INTO maintenance VALUES ('*',1,123)")
    original = database.read_bytes()
    with patch(
        "src.operations.release.importlib.metadata.version", return_value="candidate"
    ):
        assert verify_release_phase(
            database, "installed", expected_version="candidate", pause_token=123
        )["verified"]
        with pytest.raises(ValueError, match="incomplete for initialized"):
            verify_release_phase(
                database, "initialized", expected_version="candidate", pause_token=123
            )
    assert database.read_bytes() == original
    RuntimeState(database, "office")
    with patch(
        "src.operations.release.importlib.metadata.version", return_value="candidate"
    ):
        assert verify_release_phase(
            database, "initialized", expected_version="candidate", pause_token=123
        )["verified"]


def test_release_ready_requires_every_office_fresh_and_paused(tmp_path):
    database = tmp_path / "runtime.sqlite3"
    RuntimeState(database, "a")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO maintenance VALUES ('*',1,90)")
        for office in ("a", "b"):
            connection.execute(
                "INSERT INTO runtime_snapshots VALUES (?,100,0,0)", (office,)
            )
    reports = [
        {
            "office_id": office,
            "observed_at": 100,
            "ready": True,
            "communicator_version": "candidate",
            "maintenance": {
                "office_id": office,
                "enabled": True,
                "state": "drained",
                "fresh_acknowledgment": True,
            },
        }
        for office in ("a", "b")
    ]
    with patch(
        "src.operations.release.importlib.metadata.version", return_value="candidate"
    ):
        with pytest.raises(ValueError, match="every expected office"):
            verify_release_phase(
                database,
                "ready",
                expected_version="candidate",
                expected_offices=["a", "b"],
                health_reports=reports[:1],
                now=110,
                pause_token=90,
            )
        assert verify_release_phase(
            database,
            "ready",
            expected_version="candidate",
            expected_offices=["a", "b"],
            health_reports=reports,
            now=110,
            pause_token=90,
        )["verified"]
        with pytest.raises(ValueError, match="fresh"):
            verify_release_phase(
                database,
                "ready",
                expected_version="candidate",
                expected_offices=["a", "b"],
                health_reports=reports,
                now=200,
                pause_token=90,
            )
        with pytest.raises(ValueError, match="maintenance"):
            verify_release_phase(
                database,
                "reopened",
                expected_version="candidate",
                expected_offices=["a", "b"],
                health_reports=reports,
                now=110,
                pause_token=90,
            )


def test_archive_checks_digest_expected_inventory_and_crc(tmp_path):
    archive = tmp_path / "backup.tar.gz"
    digest = _archive(archive)
    assert (
        verify_archive(
            archive, expected_sha256=digest, required_names=("runtime/control.sqlite3",)
        )["members"]
        == 2
    )
    with pytest.raises(ArchiveVerificationError, match="SHA-256"):
        verify_archive(archive, expected_sha256="0" * 64)
    with pytest.raises(ArchiveVerificationError, match="absent"):
        verify_archive(archive, required_names=("credentials",))
    damaged = bytearray(archive.read_bytes())
    damaged[-8] ^= 0xFF
    archive.write_bytes(damaged)
    with pytest.raises(ArchiveVerificationError, match="integrity"):
        verify_archive(archive)


@pytest.mark.parametrize("names", [("../escape",), ("same", "same")])
def test_archive_rejects_unsafe_and_duplicate_members(tmp_path, names):
    archive = tmp_path / "backup.tar.gz"
    _archive(archive, names)
    with pytest.raises(ArchiveVerificationError):
        verify_archive(archive)


def test_archive_budgets_fail_explicitly_and_memory_does_not_cache_members(
    tmp_path, monkeypatch
):
    archive = tmp_path / "backup.tar.gz"
    _archive(archive, tuple(f"files/{number}" for number in range(4000)))
    with pytest.raises(ArchiveVerificationError, match="member budget"):
        verify_archive(archive, max_members=10)
    original = tarfile.TarFile.next

    def next_member(self):
        # Initial creation may cache one member; each parsed member is released.
        assert len(self.members) <= 1
        return original(self)

    monkeypatch.setattr(tarfile.TarFile, "next", next_member)
    assert verify_archive(archive)["members"] == 4000


def test_preflight_checks_real_staged_asset_without_pause(tmp_path):
    asset = tmp_path / "candidate.whl"
    asset.write_bytes(b"candidate")
    result = preflight(
        tmp_path,
        required_bytes=1,
        minimum_free_bytes=1,
        assets={asset: hashlib.sha256(b"candidate").hexdigest()},
    )
    assert result["verified"] and result["final_stopped_backup_required"]
    with pytest.raises(ValueError, match="space"):
        preflight(tmp_path, required_bytes=10**30, minimum_free_bytes=1)
    with pytest.raises(ValueError, match="identity"):
        preflight(
            tmp_path, required_bytes=1, minimum_free_bytes=1, assets={asset: "wrong"}
        )


def test_reference_retention_protects_mixed_source_hardlinks_and_unknown(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    cache = root / "cache"
    cache.mkdir()
    (cache / "derived").write_text("data")
    os.link(cache / "derived", cache / "second")
    (root / "source.py").write_text("source")
    (root / "build").mkdir()
    (root / "build" / "task.py").write_text("task source")
    references = {
        "office_id": "office",
        "reference_inventory_complete": True,
        "source_receipt": "ref-v1",
        "references": [{"path": "build/task.py", "reason": "active task source"}],
        "candidates": [
            {
                "path": path,
                "classification": "rebuildable_cache",
                "rebuild_proof": "recipe-v1",
                "recovery_receipt": "restore-v1",
            }
            for path in ("cache", "build")
        ],
    }
    output = tmp_path / "manifest.jsonl"
    result = inventory(root, output, references)
    rows = {
        row["path"]: row
        for row in map(json.loads, output.read_text().splitlines())
        if row["type"] == "entry"
    }
    assert result["complete"] and not result["removal_authorized"]
    assert rows["source.py"]["classification"] == "protected"
    assert rows["build/task.py"]["reason"] == "active task source"
    assert rows["cache/derived"]["classification"] == "protected"
    assert (
        rows["cache/derived"]["allocated_bytes"]
        + rows["cache/second"]["allocated_bytes"]
        == (cache / "derived").stat().st_blocks * 512
    )
    assert output.stat().st_mode & 0o777 == 0o600
    assert (root / "source.py").read_text() == "source"


def test_retention_budget_and_missing_reference_evidence_never_authorize(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    for number in range(4):
        (root / str(number)).touch()
    output = tmp_path / "manifest.jsonl"
    result = inventory(root, output, {"office_id": "office"}, max_entries=2)
    assert result["complete"] is False and result["entries"] == 2
    assert result["review_candidate_allocated_bytes"] == 0
    assert not result["removal_authorized"]
    with pytest.raises(ValueError, match="outside"):
        inventory(root, root / "manifest.jsonl", {"office_id": "office"})


def test_operations_cli_exposes_runnable_read_only_gates(tmp_path):
    assert CliRunner().invoke(operations, ["--help"]).exit_code == 0
    archive = tmp_path / "backup.tar.gz"
    digest = _archive(archive)
    result = CliRunner().invoke(
        operations, ["archive-verify", str(archive), "--expected-sha256", digest]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["provenance_verified"]


def test_archive_rejects_nonpadding_trailing_data_even_in_read_buffer(tmp_path):
    archive = tmp_path / "backup.tar"
    with tarfile.open(archive, "w") as stream:
        entry = tarfile.TarInfo("file")
        entry.size = 4
        stream.addfile(entry, io.BytesIO(b"data"))
    payload = bytearray(archive.read_bytes())
    payload[-100] = 1
    archive.write_bytes(payload)
    with pytest.raises(ArchiveVerificationError, match="Non-padding"):
        verify_archive(archive)


@pytest.mark.parametrize("padding", [0, 512])
@pytest.mark.parametrize("compressed", [False, True])
def test_archive_rejects_missing_tar_end_markers_with_intact_member_and_gzip(
    tmp_path, padding, compressed
):
    entry = tarfile.TarInfo("file")
    entry.size = 4
    payload = entry.tobuf() + b"data" + bytes(508 + padding)
    archive = tmp_path / "truncated.tar"
    archive.write_bytes(gzip.compress(payload) if compressed else payload)
    with pytest.raises(ArchiveVerificationError, match="end-of-archive"):
        verify_archive(archive)


def test_host_snapshot_is_bounded_and_reports_missing_platform_data(tmp_path):
    from src.operations.host_snapshot import snapshot

    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text("x" * 70000)
    result = snapshot(tmp_path, proc=proc, cgroup=tmp_path / "absent")
    assert result["host"]["stat"] is None
    assert result["storage"]["available_bytes"] > 0
    assert result["capacity_changed"] is False


def _stopped_archive(
    tmp_path, *, snapshot=b"sqlite snapshot", recorded=None, wrong_directory=False
):
    from src.operations.backup_plan import CRITICAL, EVIDENCE

    snapshot_manifest = json.dumps(
        [
            {
                "snapshot": "sqlite-0001.sqlite3",
                "sha256": hashlib.sha256(
                    recorded if recorded is not None else snapshot
                ).hexdigest(),
            }
        ]
    ).encode()
    plan = {
        "archive_roots": [{"source": "/operator/.cubicle", "prefix": "cubicle"}],
        "excluded_historical_roots": ["recovery-tools", "recovery-backups"],
        "sqlite_snapshots": 1,
        "sqlite_manifest_sha256": hashlib.sha256(snapshot_manifest).hexdigest(),
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    files = {
        "cubicle": b"",
        **{name: b"data" for name in CRITICAL},
        **{"release-evidence/" + name: b"evidence" for name in EVIDENCE},
        "release-evidence/stopped-backup-plan.json": plan_path.read_bytes(),
        "release-evidence/state-snapshots/sqlite-manifest.json": snapshot_manifest,
        "release-evidence/state-snapshots/sqlite-0001.sqlite3": snapshot,
    }
    archive = tmp_path / "stopped.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            if (
                (name == "cubicle" or name in CRITICAL)
                and name
                not in {"cubicle/config.yaml", "cubicle/runtime/control.sqlite3"}
                and not wrong_directory
            ):
                member.type = tarfile.DIRTYPE
                stream.addfile(member)
            else:
                member.size = len(data)
                stream.addfile(member, io.BytesIO(data))
    return archive, plan_path, hashlib.sha256(plan_path.read_bytes()).hexdigest()


def test_stopped_archive_preserves_plan_and_sqlite_content_guards(tmp_path):
    archive, plan, plan_digest = _stopped_archive(tmp_path)
    result = verify_archive(
        archive, stopped_plan=plan, expected_plan_sha256=plan_digest
    )
    assert result["stopped_backup_content_verified"]
    assert result["sqlite_snapshots"] == 1
    archive, plan, plan_digest = _stopped_archive(
        tmp_path, snapshot=b"wrong", recorded=b"expected"
    )
    with pytest.raises(ValueError, match="SQLite snapshot content"):
        verify_archive(archive, stopped_plan=plan, expected_plan_sha256=plan_digest)
    with pytest.raises(ValueError, match="pinned"):
        verify_archive(archive, stopped_plan=plan, expected_plan_sha256="0" * 64)


def test_stopped_archive_rejects_file_masquerading_as_directory_root(tmp_path):
    archive, plan, plan_digest = _stopped_archive(tmp_path, wrong_directory=True)
    with pytest.raises(ValueError, match="wrong type"):
        verify_archive(archive, stopped_plan=plan, expected_plan_sha256=plan_digest)


def test_retention_nested_candidate_cannot_override_protected_parent(tmp_path):
    root = tmp_path / "workspace"
    (root / "build/cache").mkdir(parents=True)
    (root / "build/source.py").write_text("source")
    (root / "build/cache/derived").write_text("generated")
    output = tmp_path / "inventory.jsonl"
    result = inventory(
        root,
        output,
        {
            "office_id": "office",
            "reference_inventory_complete": True,
            "source_receipt": "refs-v1",
            "references": [{"path": "build/source.py", "reason": "active source"}],
            "candidates": [
                {
                    "path": name,
                    "classification": "rebuildable_cache",
                    "rebuild_proof": "recipe",
                    "recovery_receipt": "copy",
                }
                for name in ("build", "build/cache")
            ],
        },
    )
    assert result["complete"]
    entries = [json.loads(line) for line in output.read_text().splitlines()]
    derived = next(row for row in entries if row.get("path") == "build/cache/derived")
    assert derived["classification"] == "protected"
    assert derived["reason"] == "active source"


def test_retention_never_follows_directory_swapped_to_symlink(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    (root / "child").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private").write_text("must not inventory")
    original = os.open

    def swap(path, flags, *args, **kwargs):
        if path == "child" and kwargs.get("dir_fd") is not None:
            (root / "child").rename(root / "old-child")
            (root / "child").symlink_to(outside, target_is_directory=True)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap)
    output = tmp_path / "inventory.jsonl"
    result = inventory(root, output, {"office_id": "office"})
    assert result["complete"] is False
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert not any(row.get("path") == "child/private" for row in rows)
    assert (outside / "private").read_text() == "must not inventory"


def test_operator_inputs_reject_fifo_symlink_and_replacement(tmp_path):
    from src.operations._files import bounded_bytes, regular_reader

    path = tmp_path / "input.json"
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular"):
        bounded_bytes(path, 100)
    path.unlink()
    target = tmp_path / "target"
    target.write_text("data")
    path.symlink_to(target)
    with pytest.raises(OSError):
        bounded_bytes(path, 100)
    path.unlink()
    path.write_text("data")
    with pytest.raises(ValueError, match="replaced"):
        with regular_reader(path) as stream:
            assert stream.read() == b"data"
            replacement = tmp_path / "replacement"
            replacement.write_text("data")
            replacement.replace(path)


def test_release_local_state_and_post_pause_snapshot_bind_health_claims(tmp_path):
    database = tmp_path / "runtime.sqlite3"
    RuntimeState(database, "a")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO maintenance VALUES ('*',1,100)")
        connection.execute("INSERT INTO runtime_snapshots VALUES ('a',101,0,0)")
    report = {
        "office_id": "a",
        "observed_at": 99,
        "ready": True,
        "communicator_version": "candidate",
        "maintenance": {
            "office_id": "a",
            "enabled": True,
            "state": "drained",
            "fresh_acknowledgment": True,
        },
    }
    arguments = dict(
        expected_version="candidate",
        expected_offices=["a"],
        health_reports=[report],
        now=110,
        pause_token=100,
    )
    with patch(
        "src.operations.release.importlib.metadata.version", return_value="candidate"
    ):
        with pytest.raises(ValueError, match="fresh"):
            verify_release_phase(database, "ready", **arguments)
        report["observed_at"] = 102
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE runtime_snapshots SET observed_at=99")
        with pytest.raises(ValueError, match="fresh local"):
            verify_release_phase(database, "ready", **arguments)
        report["maintenance"].update(enabled=False, state="open")
        with pytest.raises(ValueError, match="Local global maintenance"):
            verify_release_phase(database, "reopened", **arguments)
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE maintenance SET enabled=0,changed_at=103")
            connection.execute("UPDATE runtime_snapshots SET observed_at=104")
        report["observed_at"] = 105
        assert verify_release_phase(database, "reopened", **arguments)["verified"]


def test_operations_cli_rejects_wrong_json_shape_without_traceback(tmp_path):
    malformed = tmp_path / "input.json"
    malformed.write_text("[]")
    result = CliRunner().invoke(
        operations,
        [
            "retention-manifest",
            "--root",
            str(tmp_path),
            "--output",
            str(tmp_path.parent / "unused.jsonl"),
            "--references",
            str(malformed),
        ],
    )
    assert result.exit_code != 0
    assert "must be a dict" in result.output


def test_installed_phase_still_rejects_unsettled_ownership_and_other_pause(tmp_path):
    database = tmp_path / "runtime.sqlite3"
    runtime = RuntimeState(database, "office")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO maintenance VALUES ('*',1,123)")
        connection.execute(
            "INSERT INTO admissions VALUES ('token','office','worker','task',100)"
        )
    with patch(
        "src.operations.release.importlib.metadata.version", return_value="candidate"
    ):
        with pytest.raises(ValueError, match="Unsettled runtime ownership"):
            verify_release_phase(
                database, "installed", expected_version="candidate", pause_token=123
            )
        with pytest.raises(ValueError, match="maintenance changed"):
            verify_release_phase(
                database, "installed", expected_version="candidate", pause_token=124
            )


def _capacity_release_fixture(tmp_path):
    database = tmp_path / "runtime.sqlite3"
    runtime = RuntimeState(database, "a")
    task = {
        "id": "task",
        "status": "in_progress",
        "assigned_agent": "worker",
        "active_execution_attempt_id": "attempt",
        "execution_cycle": 1,
        "execution_generation": 2,
        "review_retry_epoch": 0,
    }
    caller = {
        "role": "worker",
        "agent_name": "worker",
        "task_mode": "execute",
        "attempt_id": "attempt",
        "task_id": "task",
        "execution_cycle": 1,
        "execution_generation": 2,
        "review_retry_epoch": 0,
    }
    record = {
        "task_id": "task",
        "cycle": 1,
        "phase": "execute",
        "cleanup_confirmed": True,
        "operation_id": "operation",
        "operation_key": "intent",
        "script_name": "check",
        "input_fingerprint": "a" * 64,
    }
    assert runtime.register_capacity_wait(
        record, task, caller, action="start", had_variable_overrides=True
    )
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO maintenance VALUES ('*',1,100)")
        connection.execute("INSERT INTO runtime_snapshots VALUES ('a',105,0,0)")
    report = {
        "office_id": "a",
        "observed_at": 106,
        "ready": True,
        "communicator_version": "candidate",
        "maintenance": {
            "office_id": "a",
            "enabled": True,
            "state": "drained",
            "fresh_acknowledgment": True,
        },
    }
    return database, report


@pytest.mark.parametrize("phase", ["installed", "initialized", "ready"])
def test_release_preserves_and_reports_deferred_capacity_waits(tmp_path, phase):
    database, report = _capacity_release_fixture(tmp_path)
    before = database.read_bytes()
    with patch(
        "src.operations.release.importlib.metadata.version", return_value="candidate"
    ):
        result = verify_release_phase(
            database,
            phase,
            expected_version="candidate",
            pause_token=100,
            expected_offices=["a"],
            health_reports=[report],
            now=110,
        )
    assert result["ownership_counts"]["capacity_resume_claims"] == 0
    inventory = result["deferred_capacity_waits"]
    assert inventory["counts"] == {"waiting": 1}
    assert inventory["active_offices"] == ["a"]
    assert inventory["active_waits"][0]["operation_id"] == "operation"
    assert "resume_context" not in inventory["active_waits"][0]
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    "state,pending", [("resuming", None), ("waiting", "pending-claim")]
)
@pytest.mark.parametrize("phase", ["installed", "initialized", "ready"])
def test_release_rejects_in_flight_resume_even_before_worker_journal(
    tmp_path, state, pending, phase
):
    database, report = _capacity_release_fixture(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE capacity_waits SET state=?,pending_resume_attempt_id=?",
            (state, pending),
        )
    with patch(
        "src.operations.release.importlib.metadata.version", return_value="candidate"
    ):
        with pytest.raises(ValueError, match="capacity_resume_claims"):
            verify_release_phase(
                database,
                phase,
                expected_version="candidate",
                pause_token=100,
                expected_offices=["a"],
                health_reports=[report],
                now=110,
            )


@pytest.mark.parametrize("phase", ["installed", "initialized", "ready", "reopened"])
def test_release_rejects_unknown_capacity_wait_state(tmp_path, phase):
    database, report = _capacity_release_fixture(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE capacity_waits SET state='unexpected'")
    with patch(
        "src.operations.release.importlib.metadata.version", return_value="candidate"
    ):
        with pytest.raises(ValueError, match="Unknown capacity wait"):
            verify_release_phase(
                database,
                phase,
                expected_version="candidate",
                pause_token=100,
                expected_offices=["a"],
                health_reports=[report],
                now=110,
            )


def test_reopened_reports_live_resume_but_requires_owning_office_health(tmp_path):
    database, report = _capacity_release_fixture(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE capacity_waits SET state='resuming',pending_resume_attempt_id='claim'"
        )
        connection.execute("UPDATE maintenance SET enabled=0,changed_at=104")
    report["maintenance"].update(enabled=False, state="open")
    with patch(
        "src.operations.release.importlib.metadata.version", return_value="candidate"
    ):
        with pytest.raises(ValueError, match="omits active capacity-wait offices"):
            verify_release_phase(
                database,
                "reopened",
                expected_version="candidate",
                expected_offices=["other"],
                health_reports=[report],
                now=110,
            )
        result = verify_release_phase(
            database,
            "reopened",
            expected_version="candidate",
            expected_offices=["a"],
            health_reports=[report],
            now=110,
        )
    assert result["ownership_counts"] is None
    assert result["deferred_capacity_waits"]["in_flight_resume_count"] == 1


def test_old_installed_schema_can_omit_capacity_waits_until_native_initialization(
    tmp_path,
):
    database, _ = _capacity_release_fixture(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE capacity_waits")
    with patch(
        "src.operations.release.importlib.metadata.version", return_value="candidate"
    ):
        result = verify_release_phase(
            database, "installed", expected_version="candidate", pause_token=100
        )
        assert result["deferred_capacity_waits"]["schema_present"] is False
        with pytest.raises(ValueError, match="capacity_waits"):
            verify_release_phase(
                database, "initialized", expected_version="candidate", pause_token=100
            )
