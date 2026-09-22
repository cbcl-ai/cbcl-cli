"""Stream full gzip/tar integrity without retaining millions of TarInfo objects."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import tarfile
import tempfile
import time
from src.operations._files import regular_reader


class ArchiveVerificationError(ValueError):
    pass


class _BoundedReader:
    def __init__(self, source, deadline: float):
        self.source = source
        self.deadline = deadline
        self.bytes_read = 0

    def read(self, size=-1):
        if time.monotonic() > self.deadline:
            raise ArchiveVerificationError("Archive verification time budget exhausted")
        if size < 0 or size > 32 * 1024 * 1024:
            raise ArchiveVerificationError(
                "Archive metadata read exceeds bounded buffer"
            )
        content = self.source.read(size)
        self.bytes_read += len(content)
        return content


def _safe_name(name: str) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts or len(name) > 16384:
        raise ArchiveVerificationError("Unsafe or unbounded archive member name")
    return str(path)


def verify_archive(
    path: Path,
    *,
    expected_sha256: str | None = None,
    required_names: tuple[str, ...] = (),
    max_members: int = 10000000,
    max_bytes: int = 1024**4,
    max_seconds: float = 3600,
    stopped_plan: Path | None = None,
    expected_plan_sha256: str | None = None,
) -> dict:
    """Verify all regular content and gzip trailers, never extract any member.

    Disk-backed names validate duplicate/hardlink references in bounded memory.
    A digest must come from independent retained evidence to prove provenance.
    This is archive integrity, not a claim that a restore rehearsal succeeded.
    """
    if max_members < 1 or max_bytes < 1 or not 0 < max_seconds <= 86400:
        raise ValueError("Positive bounded archive budgets are required")
    deadline = time.monotonic() + max_seconds
    digest = hashlib.sha256()
    members = content_bytes = 0
    from src.operations.backup_plan import StoppedBackupPlan

    plan = (
        StoppedBackupPlan(stopped_plan, expected_plan_sha256) if stopped_plan else None
    )
    metadata = {}
    with regular_reader(path) as raw:
        initial = os.fstat(raw.fileno())
        # Hashing a distinct sequential pass avoids buffering compressed data and
        # binds trailing bytes as well as the tar payload to the receipt.
        while chunk := raw.read(1024 * 1024):
            if time.monotonic() > deadline:
                raise ArchiveVerificationError(
                    "Archive verification time budget exhausted"
                )
            digest.update(chunk)
        observed = digest.hexdigest()
        if expected_sha256 is not None and observed != expected_sha256.lower():
            raise ArchiveVerificationError(
                "Archive SHA-256 does not match expected identity"
            )
        raw.seek(0)
        compressed = raw.read(2) == b"\x1f\x8b"
        raw.seek(0)
        source = gzip.GzipFile(fileobj=raw) if compressed else raw
        try:
            with tempfile.TemporaryDirectory(prefix="cbcl-archive-index-") as index_dir:
                with sqlite3.connect(str(Path(index_dir) / "members.sqlite3")) as index:
                    index.execute("PRAGMA cache_size=-2048")
                    index.execute(
                        "CREATE TABLE members (name TEXT PRIMARY KEY, kind TEXT, target TEXT, sha256 TEXT, size INTEGER)"
                    )
                    reader = _BoundedReader(source, deadline)
                    with tarfile.open(fileobj=reader, mode="r|") as archive:
                        for member in archive:
                            members += 1
                            if members > max_members:
                                raise ArchiveVerificationError(
                                    "Archive member budget exhausted"
                                )
                            name = _safe_name(member.name)
                            if plan:
                                plan.check_name(name)
                            kind = (
                                "file"
                                if member.isfile()
                                else (
                                    "hardlink"
                                    if member.islnk()
                                    else (
                                        "symlink"
                                        if member.issym()
                                        else (
                                            "directory"
                                            if member.isdir()
                                            else (
                                                "fifo"
                                                if member.isfifo()
                                                else "unsupported"
                                            )
                                        )
                                    )
                                )
                            )
                            if kind == "unsupported":
                                raise ArchiveVerificationError(
                                    "Archive contains an unsupported special member"
                                )
                            target = (
                                _safe_name(member.linkname)
                                if member.islnk()
                                else member.linkname if member.issym() else None
                            )
                            if target is not None and len(target) > 16384:
                                raise ArchiveVerificationError(
                                    "Archive link target exceeds metadata budget"
                                )
                            try:
                                index.execute(
                                    "INSERT INTO members VALUES (?,?,?,NULL,?)",
                                    (name, kind, target, member.size),
                                )
                            except sqlite3.IntegrityError as error:
                                raise ArchiveVerificationError(
                                    "Duplicate archive member identity"
                                ) from error
                            if member.isfile():
                                content_bytes += member.size
                                if content_bytes > max_bytes:
                                    raise ArchiveVerificationError(
                                        "Archive content budget exhausted"
                                    )
                                stream = archive.extractfile(member)
                                if stream is None:
                                    raise ArchiveVerificationError(
                                        "Archive regular content is unreadable"
                                    )
                                read_bytes = 0
                                file_digest = hashlib.sha256()
                                small = (
                                    bytearray()
                                    if plan and name in plan.metadata_names
                                    else None
                                )
                                while chunk := stream.read(1024 * 1024):
                                    if time.monotonic() > deadline:
                                        raise ArchiveVerificationError(
                                            "Archive verification time budget exhausted"
                                        )
                                    read_bytes += len(chunk)
                                    file_digest.update(chunk)
                                    if small is not None:
                                        if read_bytes > 32 * 1024 * 1024:
                                            raise ArchiveVerificationError(
                                                "Release metadata exceeds bounded buffer"
                                            )
                                        small.extend(chunk)
                                if read_bytes != member.size:
                                    raise ArchiveVerificationError(
                                        "Truncated archive member"
                                    )
                                index.execute(
                                    "UPDATE members SET sha256=? WHERE name=?",
                                    (file_digest.hexdigest(), name),
                                )
                                if small is not None:
                                    metadata[name] = bytes(small)
                            elif member.islnk():
                                target_row = index.execute(
                                    "SELECT sha256,size FROM members WHERE name=? AND kind IN ('file','hardlink')",
                                    (target,),
                                ).fetchone()
                                if not target_row or not target_row[0]:
                                    raise ArchiveVerificationError(
                                        "Archive hardlink lacks a preceding regular-file target"
                                    )
                                index.execute(
                                    "UPDATE members SET sha256=?,size=? WHERE name=?",
                                    (*target_row, name),
                                )
                            # Python <=3.12 stream mode still caches TarInfo.
                            archive.members.clear()
                        if any(archive.fileobj.buf):
                            raise ArchiveVerificationError(
                                "Non-padding bytes follow tar archive"
                            )
                        end_of_members = archive.offset
                    # Consume all compressed frames and trailers, detecting CRC
                    # damage/truncation even after the tar end-of-archive marker.
                    while chunk := reader.read(1024 * 1024):
                        if any(chunk):
                            raise ArchiveVerificationError(
                                "Non-padding bytes follow tar archive"
                            )
                    if reader.bytes_read < end_of_members + 2 * tarfile.BLOCKSIZE:
                        raise ArchiveVerificationError(
                            "Tar end-of-archive markers are missing or truncated"
                        )
                    missing = [
                        name
                        for name in required_names
                        if index.execute(
                            "SELECT 1 FROM members WHERE name=?", (_safe_name(name),)
                        ).fetchone()
                        is None
                    ]
                    if missing:
                        raise ArchiveVerificationError(
                            "Required archive members are absent"
                        )
                    broken = index.execute(
                        "SELECT 1 FROM members AS link LEFT JOIN members AS target ON target.name=link.target WHERE link.kind='hardlink' AND (target.name IS NULL OR target.sha256 IS NULL) LIMIT 1"
                    ).fetchone()
                    if broken:
                        raise ArchiveVerificationError(
                            "Archive hardlink lacks a regular-file target"
                        )
                    plan_result = (
                        plan.verify(index, metadata)
                        if plan
                        else {"stopped_backup_content_verified": False}
                    )
        except (tarfile.TarError, EOFError, OSError) as error:
            raise ArchiveVerificationError("Archive stream integrity failed") from error
        finally:
            if compressed:
                source.close()
        final = os.fstat(raw.fileno())
        if (initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) != (
            final.st_size,
            final.st_mtime_ns,
            final.st_ctime_ns,
        ):
            raise ArchiveVerificationError("Archive changed during verification")
    if not members:
        raise ArchiveVerificationError("Empty archive is not a backup")
    return {
        "sha256": observed,
        "members": members,
        "regular_content_bytes": content_bytes,
        "integrity": "verified",
        "provenance_verified": expected_sha256 is not None,
        "restore_verified": False,
        **plan_result,
        "duration_seconds": round(max_seconds - (deadline - time.monotonic()), 3),
    }
