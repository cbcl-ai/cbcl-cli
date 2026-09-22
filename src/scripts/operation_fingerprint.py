"""Bounded mechanism digests; not an atomic snapshot of a mutable workspace."""

import hashlib
import json
import os
from pathlib import Path
import stat


MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_SOURCE_ENTRIES = 10000
MAX_SOURCE_DEPTH = 32
_EXCLUDED_DIRECTORIES = {"executions", ".deps", ".outbox", "__pycache__", ".git"}


def fingerprint(script_dir: Path, spec: dict, overrides: dict | None) -> str:
    if script_dir.is_symlink():
        raise ValueError("Tracked operation source must not use symlinks")
    digest = hashlib.sha256(b"cubicle-operation-mechanism-v2\0")

    def frame(data: bytes) -> None:
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)

    frame(
        json.dumps(
            {"inputs": spec, "overrides": overrides or {}}, sort_keys=True
        ).encode()
    )
    pending = [(script_dir, 0)]
    count = total = 0
    while pending:
        root, depth = pending.pop()
        files, directories = [], []
        # Streaming enumeration bounds metadata even for millions of empty files.
        with os.scandir(root) as entries:
            for entry in entries:
                count += 1
                if count > MAX_SOURCE_ENTRIES:
                    raise ValueError("Operation source exceeds its entry budget")
                if entry.name in _EXCLUDED_DIRECTORIES and entry.is_dir(
                    follow_symlinks=False
                ):
                    continue
                if entry.name == ".progress.json":
                    continue
                if entry.is_symlink():
                    raise ValueError("Tracked operation source must not use symlinks")
                if entry.is_dir(follow_symlinks=False):
                    if depth >= MAX_SOURCE_DEPTH:
                        raise ValueError(
                            "Operation source exceeds its directory depth budget"
                        )
                    directories.append(Path(entry.path))
                else:
                    files.append(Path(entry.path))
        pending.extend((path, depth + 1) for path in sorted(directories, reverse=True))
        for path in sorted(files):
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as source:
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError("Operation source must contain regular files")
                if total + before.st_size > MAX_SOURCE_BYTES:
                    raise ValueError(
                        "Operation script source exceeds the 20 MiB fingerprint budget"
                    )
                frame(str(path.relative_to(script_dir)).encode())
                digest.update(before.st_size.to_bytes(8, "big"))
                read = 0
                while block := source.read(
                    min(64 * 1024, MAX_SOURCE_BYTES - total + 1)
                ):
                    read += len(block)
                    total += len(block)
                    if total > MAX_SOURCE_BYTES:
                        raise ValueError(
                            "Operation script source exceeds the 20 MiB fingerprint budget"
                        )
                    digest.update(block)
                after = os.fstat(source.fileno())
                if (
                    read != before.st_size
                    or before.st_mtime_ns != after.st_mtime_ns
                    or before.st_ctime_ns != after.st_ctime_ns
                ):
                    raise ValueError("Operation source changed during fingerprinting")
    return digest.hexdigest()
