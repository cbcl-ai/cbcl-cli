"""Bounded, reference-aware storage review manifests; deliberately no delete API."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time


def inventory(
    root: Path,
    output: Path,
    reference_manifest: dict,
    *,
    max_entries: int = 100000,
    max_seconds: float = 60,
) -> dict:
    """Classify explicit candidates against an operator-supplied reference map.

    Every unknown path is protected. A candidate is only reviewable, never
    authorized for removal. No age/path-name heuristic proves recoverability.
    Hardlinks are counted once and links are retained for ownership review.
    """
    root = root.resolve(strict=True)
    root_metadata = root.stat()
    root_device = root_metadata.st_dev
    if not root.is_dir() or max_entries < 1 or not 0 < max_seconds <= 3600:
        raise ValueError(
            "A directory and positive bounded inventory budgets are required"
        )
    output = output.parent.resolve(strict=True) / output.name
    if output == root or root in output.parents or output.is_symlink():
        raise ValueError("Write private manifests outside the inventoried tree")
    if not isinstance(reference_manifest, dict) or not reference_manifest.get(
        "office_id"
    ):
        raise ValueError("Reference manifest requires office ownership")
    complete_references = reference_manifest.get(
        "reference_inventory_complete"
    ) is True and bool(reference_manifest.get("source_receipt"))

    def scoped(value: str) -> Path:
        if (
            not isinstance(value, str)
            or not value
            or Path(value).is_absolute()
            or ".." in Path(value).parts
        ):
            raise ValueError("Reference paths must stay within the inventory root")
        return root / value

    for key, bound in (("references", 10000), ("candidates", 1000)):
        items = reference_manifest.get(key, [])
        if (
            not isinstance(items, list)
            or len(items) > bound
            or any(not isinstance(item, dict) or "path" not in item for item in items)
        ):
            raise ValueError(
                "Reference and candidate lists must be bounded path objects"
            )
    references = [
        (scoped(item["path"]), item.get("reason", "retained reference"))
        for item in reference_manifest.get("references", [])
    ]
    candidates = [
        (scoped(item["path"]), item)
        for item in reference_manifest.get("candidates", [])
    ]
    deadline = time.monotonic() + max_seconds
    counts = {
        "entries": 0,
        "allocated_bytes": 0,
        "logical_bytes": 0,
        "review_candidate_allocated_bytes": 0,
        "complete": True,
    }
    descriptor = os.open(
        output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as manifest, tempfile.TemporaryDirectory(
        prefix="cbcl-retention-index-"
    ) as temporary:
        with sqlite3.connect(str(Path(temporary) / "inodes.sqlite3")) as index:
            index.execute("PRAGMA cache_size=-1024")
            index.execute(
                "CREATE TABLE inodes (device INTEGER, inode INTEGER, PRIMARY KEY(device,inode))"
            )
            manifest.write(
                json.dumps(
                    {
                        "type": "inventory",
                        "root": str(root),
                        "office_id": reference_manifest["office_id"],
                        "source_receipt": reference_manifest.get("source_receipt"),
                        "removal_authorized": False,
                    }
                )
                + "\n"
            )

            def walk(directory: Path, directory_fd: int, depth=0):
                if depth > 64:
                    raise ValueError("Inventory directory depth budget exhausted")
                with os.scandir(directory_fd) as entries:
                    for entry in entries:
                        if (
                            counts["entries"] >= max_entries
                            or time.monotonic() >= deadline
                        ):
                            counts["complete"] = False
                            return
                        path = directory / entry.name
                        metadata = entry.stat(follow_symlinks=False)
                        counts["entries"] += 1
                        logical = (
                            metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0
                        )
                        inserted = index.execute(
                            "INSERT OR IGNORE INTO inodes VALUES (?,?)",
                            (metadata.st_dev, metadata.st_ino),
                        ).rowcount
                        allocated = (
                            getattr(metadata, "st_blocks", 0) * 512 if inserted else 0
                        )
                        counts["logical_bytes"] += logical
                        counts["allocated_bytes"] += allocated
                        classification, reason = (
                            "protected",
                            "unclassified ownership/recovery",
                        )
                        matched = [
                            (candidate, item)
                            for candidate, item in candidates
                            if path == candidate or candidate in path.parents
                        ]
                        if matched:
                            candidate, item = max(
                                matched, key=lambda pair: len(pair[0].parts)
                            )
                            # A retained child protects the candidate's entire
                            # tree; a nested candidate cannot override its parent.
                            referenced = [
                                (reference, note)
                                for reference, note in references
                                if any(
                                    reference == ancestor
                                    or ancestor in reference.parents
                                    or reference in ancestor.parents
                                    for ancestor, _ in matched
                                )
                            ]
                            if referenced:
                                reason = referenced[0][1]
                            elif not complete_references:
                                reason = "reference inventory is incomplete"
                            elif (
                                item.get("classification") != "rebuildable_cache"
                                or not item.get("rebuild_proof")
                                or not item.get("recovery_receipt")
                            ):
                                reason = "rebuild and recovery evidence are required"
                            elif (
                                stat.S_ISLNK(metadata.st_mode)
                                or metadata.st_nlink > 1
                                and not stat.S_ISDIR(metadata.st_mode)
                            ):
                                reason = "link ownership requires separate review"
                            elif stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(
                                metadata.st_mode
                            ):
                                classification, reason = (
                                    "review_candidate",
                                    "explicit cache with supplied recovery evidence; removal still requires review",
                                )
                                counts["review_candidate_allocated_bytes"] += allocated
                        manifest.write(
                            json.dumps(
                                {
                                    "type": "entry",
                                    "path": str(path.relative_to(root)),
                                    "device": metadata.st_dev,
                                    "inode": metadata.st_ino,
                                    "links": metadata.st_nlink,
                                    "mtime_ns": metadata.st_mtime_ns,
                                    "mode": metadata.st_mode,
                                    "logical_bytes": logical,
                                    "allocated_bytes": allocated,
                                    "classification": classification,
                                    "reason": reason,
                                }
                            )
                            + "\n"
                        )
                        if stat.S_ISDIR(metadata.st_mode):
                            if metadata.st_dev != root_device:
                                counts["complete"] = False
                                continue
                            child_fd = os.open(
                                entry.name,
                                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory_fd,
                            )
                            try:
                                current = os.fstat(child_fd)
                                if (current.st_dev, current.st_ino) != (
                                    metadata.st_dev,
                                    metadata.st_ino,
                                ):
                                    raise ValueError(
                                        "Inventory directory changed during traversal"
                                    )
                                yield from walk(path, child_fd, depth + 1)
                            finally:
                                os.close(child_fd)
                        yield None
                        if not counts["complete"]:
                            return

            try:
                root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    opened_root = os.fstat(root_fd)
                    if (opened_root.st_dev, opened_root.st_ino) != (
                        root_device,
                        root_metadata.st_ino,
                    ):
                        raise ValueError("Inventory root changed during traversal")
                    for _ in walk(root, root_fd):
                        pass
                finally:
                    os.close(root_fd)
            except (OSError, ValueError) as error:
                counts["complete"] = False
                counts["error"] = type(error).__name__
            # These byte counts are not a reclaimable-space promise, especially
            # for incomplete scans, snapshots, reflinks or external references.
            counts["removal_authorized"] = False
            counts["reference_inventory_complete"] = complete_references
            manifest.write(json.dumps({"type": "summary", **counts}) + "\n")
    return counts
