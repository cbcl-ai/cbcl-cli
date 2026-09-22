"""Runnable operator gates. No command deploys, restores, deletes or reopens."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
import click


def _json(path: Path, *, container=dict):
    from src.operations._files import bounded_bytes

    result = json.loads(bounded_bytes(path, 1024 * 1024))
    if not isinstance(result, container):
        raise ValueError(f"Operator JSON must be a {container.__name__}")
    return result


def _report(operation):
    started = time.monotonic()
    try:
        result = operation()
    except (ValueError, OSError, sqlite3.Error) as error:
        raise click.ClickException(str(error)) from error
    result["command_duration_seconds"] = round(time.monotonic() - started, 3)
    result["reported_at"] = time.time()
    click.echo(json.dumps(result, indent=2))
    return result


@click.group()
def operations():
    """Preflight, release-phase and storage verification with explicit budgets."""


@operations.command("host-snapshot")
@click.option(
    "--storage",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
)
def host_snapshot(storage):
    """Read one bounded CPU/memory/pressure/storage sample; no scanning or tuning."""
    from src.operations.host_snapshot import snapshot

    _report(lambda: snapshot(storage))


@operations.command("release-check")
@click.option(
    "--phase",
    type=click.Choice(["installed", "initialized", "ready", "reopened"]),
    required=True,
)
@click.option(
    "--runtime-db",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option("--expected-version", required=True)
@click.option(
    "--pause-token",
    type=float,
    help="Exact owned global maintenance changed_at; required before reopening.",
)
@click.option(
    "--health-file", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option("--office-id", multiple=True)
def release_check(
    phase, runtime_db, expected_version, pause_token, health_file, office_id
):
    """Check installed code, initialized schema, then fresh all-office readiness."""
    from src.operations.release import verify_release_phase

    _report(
        lambda: verify_release_phase(
            runtime_db,
            phase,
            expected_version=expected_version,
            health_reports=_json(health_file, container=list) if health_file else None,
            expected_offices=list(office_id),
            pause_token=pause_token,
        )
    )


@operations.command("preflight")
@click.option(
    "--destination",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
)
@click.option("--required-bytes", type=click.IntRange(0), required=True)
@click.option("--headroom-bytes", type=click.IntRange(0), required=True)
@click.option("--required-inodes", type=click.IntRange(0), default=1)
@click.option("--assets", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def preflight_command(
    destination, required_bytes, headroom_bytes, required_inodes, assets
):
    """Check measured space and exact staged asset hashes before maintenance."""
    from src.operations.release import preflight

    _report(
        lambda: preflight(
            destination,
            required_bytes=required_bytes,
            minimum_free_bytes=headroom_bytes,
            required_inodes=required_inodes,
            assets=(
                {Path(path): digest for path, digest in _json(assets).items()}
                if assets
                else {}
            ),
        )
    )


@operations.command("archive-verify")
@click.argument("archive", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--expected-sha256", required=True)
@click.option(
    "--stopped-plan", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option("--expected-plan-sha256")
@click.option("--require-name", multiple=True)
@click.option(
    "--max-members", type=click.IntRange(1), default=10000000, show_default=True
)
@click.option("--max-bytes", type=click.IntRange(1), default=1024**4, show_default=True)
@click.option(
    "--max-seconds", type=click.FloatRange(1, 86400), default=3600, show_default=True
)
def archive_verify(
    archive,
    expected_sha256,
    stopped_plan,
    expected_plan_sha256,
    require_name,
    max_members,
    max_bytes,
    max_seconds,
):
    """Read every tar payload/gzip trailer with bounded metadata memory."""
    from src.operations.archive import verify_archive

    _report(
        lambda: verify_archive(
            archive,
            expected_sha256=expected_sha256,
            required_names=require_name,
            max_members=max_members,
            max_bytes=max_bytes,
            max_seconds=max_seconds,
            stopped_plan=stopped_plan,
            expected_plan_sha256=expected_plan_sha256,
        )
    )


@operations.command("retention-manifest")
@click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
)
@click.option("--output", type=click.Path(path_type=Path), required=True)
@click.option(
    "--references",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--max-entries", type=click.IntRange(1), default=100000, show_default=True
)
@click.option(
    "--max-seconds", type=click.FloatRange(1, 3600), default=60, show_default=True
)
def retention_manifest(root, output, references, max_entries, max_seconds):
    """Write a private dry-run inventory; no path is authorized for removal."""
    from src.operations.retention import inventory

    result = _report(
        lambda: inventory(
            root,
            output,
            _json(references),
            max_entries=max_entries,
            max_seconds=max_seconds,
        )
    )
    if not result["complete"]:
        raise click.ClickException(
            "Inventory budget/read limit reached; manifest is explicitly incomplete"
        )


@operations.command("capacity-status")
@click.option(
    "--ledger",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
def capacity_status(ledger):
    """Read bounded shared-budget inventory without creating/migrating a ledger."""

    def read():
        if ledger.is_symlink():
            raise ValueError("Capacity ledger cannot be a symlink")
        with sqlite3.connect(
            f"{ledger.absolute().as_uri()}?mode=ro", uri=True
        ) as connection:
            connection.row_factory = sqlite3.Row
            counts = {
                row["state"]: row["count"]
                for row in connection.execute(
                    "SELECT state,COUNT(*) AS count FROM capacity_leases GROUP BY state"
                )
            }
            rows = connection.execute(
                "SELECT operation_id,office_id,state,created_at FROM capacity_leases WHERE state!='released' ORDER BY sequence LIMIT 100"
            ).fetchall()
        return {
            "counts": counts,
            "operations": [dict(row) for row in rows],
            "operations_limit": 100,
        }

    _report(read)
