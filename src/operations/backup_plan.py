"""Verify the audited stopped-backup format using the archive's disk index."""

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from src.operations._files import bounded_bytes


EVIDENCE = (
    "stopped-venv.tar.gz",
    "installed-package.tar.gz",
    "office-recovery-images.json",
    "office-containers.json",
    "daemon-config.tar.gz",
    "manager-sessions.tar.gz",
    "stopped-backup-plan.json",
)
CRITICAL = (
    "cubicle/config.yaml",
    "cubicle/private-runtime",
    "cubicle/runtime/control.sqlite3",
    "cubicle/workspaces",
    "cubicle/secrets",
)


class StoppedBackupPlan:
    metadata_names = {
        "release-evidence/stopped-backup-plan.json",
        "release-evidence/state-snapshots/sqlite-manifest.json",
    }

    def __init__(self, path: Path, expected_sha256: str | None):
        self.raw = bounded_bytes(path, 1024 * 1024)
        self.sha256 = hashlib.sha256(self.raw).hexdigest()
        if expected_sha256 is None or self.sha256 != expected_sha256:
            raise ValueError(
                "Independently pinned stopped backup plan identity is required"
            )
        self.plan = json.loads(self.raw)
        if not isinstance(self.plan, dict):
            raise ValueError("Stopped backup plan must be an object")
        roots = self.plan.get("archive_roots", [])
        if (
            not isinstance(roots, list)
            or not 1 <= len(roots) <= 100
            or any(not isinstance(root, dict) for root in roots)
            or roots[0].get("prefix") != "cubicle"
        ):
            raise ValueError("Backup plan requires the current daemon root")
        self.prefixes = set()
        for root in roots:
            prefix, source = root.get("prefix"), root.get("source")
            if (
                not isinstance(prefix, str)
                or not re.fullmatch(r"cubicle|external-approved-mount-[0-9]+", prefix)
                or prefix in self.prefixes
            ):
                raise ValueError("Invalid or duplicated approved archive prefix")
            if (
                not isinstance(source, str)
                or not PurePosixPath(source).is_absolute()
                or ".." in PurePosixPath(source).parts
            ):
                raise ValueError("Invalid source root in stopped backup plan")
            self.prefixes.add(prefix)
        if self.plan.get("excluded_historical_roots") != [
            "recovery-tools",
            "recovery-backups",
        ]:
            raise ValueError("Unreviewed stopped-backup exclusions")
        if (
            type(self.plan.get("sqlite_snapshots")) is not int
            or not 0 <= self.plan["sqlite_snapshots"] <= 100000
        ):
            raise ValueError("Invalid SQLite snapshot count")
        if not re.fullmatch(
            r"[0-9a-f]{64}", str(self.plan.get("sqlite_manifest_sha256", ""))
        ):
            raise ValueError("Invalid SQLite snapshot manifest identity")

    def check_name(self, name: str) -> None:
        if PurePosixPath(name).parts[0] not in self.prefixes | {"release-evidence"}:
            raise ValueError("Unapproved archive root")
        if any(
            name == f"cubicle/{root}" or name.startswith(f"cubicle/{root}/")
            for root in self.plan["excluded_historical_roots"]
        ):
            raise ValueError("Historical backup appeared in current-state archive")

    def verify(self, index, metadata: dict) -> dict:
        for name in self.prefixes | set(CRITICAL):
            expected_kind = (
                "file"
                if name in {"cubicle/config.yaml", "cubicle/runtime/control.sqlite3"}
                else "directory"
            )
            row = index.execute(
                "SELECT kind FROM members WHERE name=?", (name,)
            ).fetchone()
            if row is None or row[0] != expected_kind:
                raise ValueError(
                    "Required current-state archive root/member is missing or has the wrong type"
                )
        for name in EVIDENCE:
            row = index.execute(
                "SELECT kind FROM members WHERE name=?", ("release-evidence/" + name,)
            ).fetchone()
            if row is None or row[0] != "file":
                raise ValueError("Required release evidence is missing or nonregular")
        if metadata.get("release-evidence/stopped-backup-plan.json") != self.raw:
            raise ValueError("Included stopped backup plan differs byte-for-byte")
        snapshot_raw = metadata.get(
            "release-evidence/state-snapshots/sqlite-manifest.json", b""
        )
        if (
            hashlib.sha256(snapshot_raw).hexdigest()
            != self.plan["sqlite_manifest_sha256"]
        ):
            raise ValueError("SQLite snapshot manifest identity differs")
        snapshots = json.loads(snapshot_raw)
        if (
            not isinstance(snapshots, list)
            or len(snapshots) != self.plan["sqlite_snapshots"]
        ):
            raise ValueError("SQLite snapshot inventory differs")
        index.execute("CREATE TABLE expected_snapshots (name TEXT PRIMARY KEY)")
        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                raise ValueError("Malformed SQLite snapshot entry")
            name = snapshot.get("snapshot")
            if not isinstance(name, str) or not re.fullmatch(
                r"sqlite-[0-9]{4,}\.sqlite3", name
            ):
                raise ValueError("Unsafe SQLite snapshot identity")
            member = "release-evidence/state-snapshots/" + name
            try:
                index.execute("INSERT INTO expected_snapshots VALUES (?)", (member,))
            except Exception as error:
                raise ValueError("Duplicate SQLite snapshot identity") from error
            row = index.execute(
                "SELECT kind,sha256 FROM members WHERE name=?", (member,)
            ).fetchone()
            if row is None or row[0] != "file" or row[1] != snapshot.get("sha256"):
                raise ValueError("SQLite snapshot content differs")
        extra = index.execute(
            "SELECT 1 FROM members LEFT JOIN expected_snapshots USING(name) WHERE members.name LIKE 'release-evidence/state-snapshots/%.sqlite3' AND expected_snapshots.name IS NULL LIMIT 1"
        ).fetchone()
        if extra:
            raise ValueError("Unrecorded SQLite snapshot present")
        return {
            "stopped_backup_content_verified": True,
            "backup_plan_sha256": self.sha256,
            "sqlite_snapshot_content_verified": True,
            "sqlite_snapshots": len(snapshots),
        }
