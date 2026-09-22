"""Bounded maintenance explanations, separate from durable admission authority."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import stat
import time


MAX_ANNOTATION_SECONDS = 21600


def annotate(
    database_path: Path, *, scope: str, owner: str, reason: str, duration: int
) -> dict:
    if not all(
        isinstance(value, str) and 1 <= len(value) <= bound
        for value, bound in ((scope, 200), (owner, 200), (reason, 1000))
    ):
        raise ValueError("Bounded scope, owner and reason are required")
    if type(duration) is not int or not 1 <= duration <= MAX_ANNOTATION_SECONDS:
        raise ValueError("Maintenance annotation duration must be 1..21600 seconds")
    database_path = database_path.absolute()
    if any(path.is_symlink() for path in (database_path, *database_path.parents)):
        raise ValueError("Maintenance annotation store cannot traverse symbolic links")
    database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(database_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise ValueError("Unsafe maintenance annotation store ownership")
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    now = time.time()
    annotation = {
        "scope": scope,
        "owner": owner,
        "reason": reason,
        "issued_at": now,
        "expires_at": now + duration,
    }
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS annotations (scope TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL, payload TEXT NOT NULL)"
        )
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT owner,expires_at FROM annotations WHERE scope=?", (scope,)
        ).fetchone()
        if existing and existing[1] > now and existing[0] != owner:
            raise ValueError(
                "A different operator owns the unexpired maintenance annotation"
            )
        connection.execute(
            "INSERT OR REPLACE INTO annotations VALUES (?,?,?,?)",
            (scope, owner, annotation["expires_at"], json.dumps(annotation)),
        )
    return annotation


def read_annotation(database_path: Path, office_id: str) -> dict | None:
    if not database_path.exists():
        return None
    if any(
        path.is_symlink() for path in (database_path, *database_path.absolute().parents)
    ):
        return None
    try:
        with sqlite3.connect(
            f"{database_path.absolute().as_uri()}?mode=ro", uri=True, timeout=1
        ) as connection:
            row = connection.execute(
                "SELECT payload FROM annotations WHERE scope IN ('*',?) AND expires_at>? ORDER BY scope='*' LIMIT 1",
                (office_id, time.time()),
            ).fetchone()
            return json.loads(row[0]) if row else None
    except (sqlite3.Error, ValueError):
        return None
