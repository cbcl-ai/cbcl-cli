"""Read-only release gates separate installation from native initialization."""

from __future__ import annotations

import hashlib
import importlib.metadata
import math
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from src.operations._files import regular_reader
from src.operations._release_capacity_waits import (
    capacity_wait_inventory,
    require_wait_office_inventory,
)


PHASES = ("installed", "initialized", "ready", "reopened")
BASELINE_TABLES = {
    "maintenance",
    "admissions",
    "worker_completions",
    "worker_completion_cleanup",
    "script_handoffs",
    "task_script_waits",
    "recovery_cycles",
}


def _quiescent(
    database_path: Path, actual: dict, pause_token: float | None
) -> tuple[dict, dict]:
    """Package install never waives existing ownership or maintenance checks."""
    if (
        type(pause_token) not in (int, float)
        or not math.isfinite(pause_token)
        or pause_token <= 0
    ):
        raise ValueError("Exact release-owned global maintenance token is required")
    if not BASELINE_TABLES <= actual.keys():
        raise ValueError("Supported older runtime ownership tables are missing")
    counts = {}
    with sqlite3.connect(
        f"{database_path.absolute().as_uri()}?mode=ro", uri=True
    ) as connection:
        connection.execute("BEGIN")
        if connection.execute(
            "SELECT enabled,changed_at FROM maintenance WHERE scope='*'"
        ).fetchone() != (1, pause_token):
            raise ValueError("Release-owned global maintenance changed")
        for name in (
            "admissions",
            "worker_completions",
            "worker_completion_cleanup",
            "worker_executions",
        ):
            if name in actual:
                counts[name] = connection.execute(
                    f"SELECT COUNT(*) FROM {name}"
                ).fetchone()[0]
        if "script_resource_leases" in actual:
            counts["script_resources"] = connection.execute(
                "SELECT COUNT(*) FROM script_resource_leases WHERE state IS NULL OR state!='released'"
            ).fetchone()[0]
        if "managed_operations" in actual:
            counts["managed_operations"] = connection.execute(
                "SELECT COUNT(*) FROM managed_operations WHERE cleanup_confirmed=0 OR state IN ('preparing','running') OR (state='unknown' AND mechanism='external')"
            ).fetchone()[0]
        counts["script_handoffs"] = connection.execute(
            "SELECT COUNT(*) FROM script_handoffs WHERE state IS NULL OR state NOT IN ('completed','failed','killed','cancelled','timeout','timed_out')"
        ).fetchone()[0]
        counts["script_waits"] = connection.execute(
            "SELECT COUNT(*) FROM task_script_waits w LEFT JOIN recovery_cycles r ON r.office_id=w.office_id AND r.task_id=w.task_id WHERE w.cycle=COALESCE(r.cycle,0)"
        ).fetchone()[0]
        deferred_waits = capacity_wait_inventory(connection, actual)
        # Never-started waiting intents survive a drained restart. An unfinished
        # resume claim must reconcile even if its worker journal was not written.
        counts["capacity_resume_claims"] = deferred_waits["in_flight_resume_count"]
    from src.docker.execution_ledger import read_execution_inventory

    counts["isolated_executions"] = len(
        read_execution_inventory(database_path.with_name("execution-containers.sqlite"))
    )
    capacity = database_path.with_name("host-capacity.sqlite3")
    if capacity.exists():
        with sqlite3.connect(
            f"{capacity.absolute().as_uri()}?mode=ro", uri=True
        ) as connection:
            counts["host_capacity"] = connection.execute(
                "SELECT COUNT(*) FROM capacity_leases WHERE state='reserved'"
            ).fetchone()[0]
    if any(counts.values()):
        raise ValueError(
            "Unsettled runtime ownership: "
            + ", ".join(name for name, count in counts.items() if count)
        )
    return counts, deferred_waits


def _schema(path: Path) -> dict[str, set[str]]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("An existing regular runtime database is required")
    with sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True) as connection:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Runtime database integrity check failed")
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            name: {
                row[1]
                for row in connection.execute(
                    'PRAGMA table_info("' + name.replace('"', '""') + '")'
                )
            }
            for name in tables
        }


def native_schema() -> dict[str, set[str]]:
    """Let installed native initialization declare required tables in scratch."""
    from src.runtime_state import RuntimeState

    with tempfile.TemporaryDirectory(prefix="cbcl-schema-probe-") as directory:
        probe = Path(directory) / "runtime.sqlite3"
        RuntimeState(probe, "release-schema-probe")
        return _schema(probe)


def _local_readiness(
    database_path: Path,
    reports: dict[str, dict],
    *,
    phase: str,
    observed: float,
    pause_token: float | None,
) -> None:
    """Health files cannot override local admission state or reuse old snapshots."""
    with sqlite3.connect(
        f"{database_path.absolute().as_uri()}?mode=ro", uri=True
    ) as connection:
        connection.execute("BEGIN")
        global_state = connection.execute(
            "SELECT enabled,changed_at FROM maintenance WHERE scope='*'"
        ).fetchone()
        if not global_state or (
            phase == "ready"
            and global_state != (1, pause_token)
            or phase == "reopened"
            and global_state[0] != 0
        ):
            raise ValueError("Local global maintenance does not match release phase")
        for office, report in reports.items():
            states = connection.execute(
                "SELECT enabled,changed_at FROM maintenance WHERE scope IN ('*',?)",
                (office,),
            ).fetchall()
            changed_at = max(state[1] for state in states)
            if report["observed_at"] < changed_at:
                raise ValueError(
                    "Release health observation predates the local maintenance change"
                )
            if phase == "reopened" and any(state[0] != 0 for state in states):
                raise ValueError("Local office maintenance has not reopened")
            snapshot = connection.execute(
                "SELECT observed_at,active_workers,active_scripts FROM runtime_snapshots WHERE office_id=?",
                (office,),
            ).fetchone()
            if (
                not snapshot
                or snapshot[0] < changed_at
                or not 0 <= observed - snapshot[0] <= 90
            ):
                raise ValueError(
                    "Release readiness requires a fresh local runtime snapshot after the maintenance change"
                )
            if phase == "ready" and snapshot[1:] != (0, 0):
                raise ValueError("Local runtime is not drained under maintenance")


def verify_release_phase(
    database_path: Path,
    phase: str,
    *,
    expected_version: str,
    health_reports: list[dict] | None = None,
    expected_offices: list[str] | None = None,
    now: float | None = None,
    pause_token: float | None = None,
) -> dict:
    if phase not in PHASES:
        raise ValueError("Unknown release phase")
    installed = importlib.metadata.version("cubicle-communicator")
    if installed != expected_version:
        raise ValueError("Installed communicator version differs from candidate")
    actual = _schema(database_path)
    # Installed new code is allowed to observe the supported older runtime DB.
    # No native initializer or new-table assertion touches that DB in this phase.
    baseline = {
        "maintenance": {"scope", "enabled", "changed_at"},
        "admissions": {"token", "office_id", "kind", "task_id", "created_at"},
    }
    required = baseline if phase == "installed" else native_schema()
    missing = {
        name: sorted(columns - actual.get(name, set()))
        for name, columns in required.items()
        if not columns <= actual.get(name, set())
    }
    if missing:
        raise ValueError(
            "Runtime schema is incomplete for "
            + phase
            + ": "
            + ", ".join(sorted(missing))
        )
    if phase == "reopened":
        ownership = None
        with sqlite3.connect(
            f"{database_path.absolute().as_uri()}?mode=ro", uri=True
        ) as connection:
            connection.execute("BEGIN")
            deferred_waits = capacity_wait_inventory(connection, actual)
    else:
        ownership, deferred_waits = _quiescent(database_path, actual, pause_token)
    observed = time.time() if now is None else now
    if phase in {"ready", "reopened"}:
        if (
            not isinstance(expected_offices, list)
            or not 1 <= len(expected_offices) <= 1000
            or any(
                not isinstance(office, str) or not 1 <= len(office) <= 200
                for office in expected_offices
            )
        ):
            raise ValueError("Exact bounded expected office inventory is required")
        offices = set(expected_offices)
        if not offices or len(offices) != len(expected_offices or []):
            raise ValueError("Exact nonempty expected office inventory is required")
        require_wait_office_inventory(deferred_waits, offices)
        reports = health_reports or []
        if (
            not isinstance(reports, list)
            or len(reports) > 1000
            or any(
                not isinstance(report, dict)
                or not isinstance(report.get("office_id"), str)
                for report in reports
            )
        ):
            raise ValueError("Health reports must be office observation objects")
        by_office = {report.get("office_id"): report for report in reports}
        if len(by_office) != len(reports) or set(by_office) != offices:
            raise ValueError("Health inventory does not match every expected office")
        for report in reports:
            timestamp = report.get("observed_at")
            if (
                type(timestamp) not in (int, float)
                or not 0 <= observed - timestamp <= 90
                or (phase == "ready" and timestamp < pause_token)
            ):
                raise ValueError("Release readiness requires fresh office observations")
            if (
                report.get("communicator_version") != expected_version
                or report.get("ready") is not True
            ):
                raise ValueError("Office runtime is not ready on candidate version")
            maintenance = report.get("maintenance", {})
            expected_enabled = phase == "ready"
            if (
                not isinstance(maintenance, dict)
                or maintenance.get("office_id") != report["office_id"]
                or maintenance.get("enabled") is not expected_enabled
            ):
                raise ValueError("Office maintenance does not match release phase")
            if phase == "ready" and (
                maintenance.get("fresh_acknowledgment") is not True
                or maintenance.get("state") != "drained"
            ):
                raise ValueError(
                    "Readiness under maintenance requires a fresh drained acknowledgment"
                )
            if phase == "reopened" and maintenance.get("state") != "open":
                raise ValueError("Office admissions have not reopened")
        _local_readiness(
            database_path,
            by_office,
            phase=phase,
            observed=observed,
            pause_token=pause_token,
        )
    return {
        "phase": phase,
        "version": installed,
        "verified": True,
        "runtime_database_mutated": False,
        "observed_at": observed,
        "expected_offices": expected_offices or [],
        "ownership_counts": ownership,
        "deferred_capacity_waits": deferred_waits,
        "process_and_image_acceptance_required": True,
    }


def preflight(
    destination: Path,
    *,
    required_bytes: int,
    minimum_free_bytes: int,
    required_inodes: int = 1,
    assets: dict[Path, str] | None = None,
    max_seconds: float = 600,
) -> dict:
    """Check known assets and measured backup space before pausing offices."""
    import os

    if any(
        type(value) is not int or value < 0
        for value in (required_bytes, minimum_free_bytes, required_inodes)
    ):
        raise ValueError("Preflight budgets must be nonnegative integer measurements")
    if not 0 < max_seconds <= 3600 or len(assets or {}) > 128:
        raise ValueError("Preflight asset/time budgets exceed limits")
    deadline = time.monotonic() + max_seconds
    space = shutil.disk_usage(destination)
    filesystem = os.statvfs(destination)
    if (
        space.free < required_bytes + minimum_free_bytes
        or filesystem.f_favail < required_inodes
    ):
        raise ValueError("Insufficient backup space/inodes with required headroom")
    verified = []
    for path, expected in (assets or {}).items():
        digest = hashlib.sha256()
        with regular_reader(path) as stream:
            while chunk := stream.read(1024 * 1024):
                if time.monotonic() > deadline:
                    raise ValueError(
                        "Preflight asset verification time budget exhausted"
                    )
                digest.update(chunk)
        if digest.hexdigest() != expected.lower():
            raise ValueError("Staged immutable asset identity mismatch")
        verified.append({"path": str(path), "sha256": digest.hexdigest()})
    return {
        "phase": "preflight",
        "verified": True,
        "free_bytes": space.free,
        "free_inodes": filesystem.f_favail,
        "required_bytes": required_bytes,
        "minimum_free_bytes": minimum_free_bytes,
        "assets": verified,
        "final_stopped_backup_required": True,
    }
