"""Repair only the two managed upload directories inside an office container.

Legacy host-side uploads created root-owned directories. Public Files now runs
as uid 1000 and cannot write those directories. This maintenance program is
sent by the trusted daemon to isolated container Python at office startup; it
does not add a root execution path to public filesystem requests.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

AGENT_UID = 1000
AGENT_GID = 1000
MANAGED_UPLOAD_DIRECTORIES = ("inbox", "source")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _mount_id(descriptor: int) -> str:
    with open(f"/proc/self/fdinfo/{descriptor}", encoding="ascii") as details:
        for line in details:
            if line.startswith("mnt_id:"):
                return line.split(":", 1)[1].strip()
    raise RuntimeError("Upload directory setup requires Linux mount identity support")


def _open_workspace(root: str | Path) -> int:
    path = Path(root)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("A trusted absolute workspace path is required")
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def repair_upload_directories(root: str | Path = "/workspace") -> dict:
    """Create/repair top-level upload directories without traversing their contents.

    Only root-owned and agent-owned directories are eligible. Existing modes,
    user-owned projects and uploaded files remain untouched. Descriptor-relative
    opens reject symlinks, and mount identities reject nested/bind mounts even
    when their device IDs match. The public Files lock excludes active helpers.
    """
    results = {}
    root_descriptor = _open_workspace(root)
    try:
        fcntl.flock(root_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        root_mount = _mount_id(root_descriptor)
        for name in MANAGED_UPLOAD_DIRECTORIES:
            descriptor = None
            try:
                created = False
                try:
                    os.mkdir(name, mode=0o755, dir_fd=root_descriptor)
                    created = True
                except FileExistsError:
                    pass
                descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=root_descriptor)
                if _mount_id(descriptor) != root_mount:
                    raise ValueError("Nested mounts cannot be upload directories")
                metadata = os.fstat(descriptor)
                if metadata.st_uid not in {0, AGENT_UID}:
                    results[name] = {"status": "skipped", "reason": "custom owner"}
                    continue
                changed = (metadata.st_uid, metadata.st_gid) != (AGENT_UID, AGENT_GID)
                if changed:
                    os.fchown(descriptor, AGENT_UID, AGENT_GID)
                results[name] = {
                    "status": (
                        "created" if created else "repaired" if changed else "ready"
                    )
                }
            except (OSError, RuntimeError, ValueError) as error:
                results[name] = {"status": "error", "reason": type(error).__name__}
            finally:
                if descriptor is not None:
                    os.close(descriptor)
    finally:
        os.close(root_descriptor)
    return {
        "ok": all(
            item["status"] in {"created", "repaired", "ready"}
            for item in results.values()
        ),
        "directories": results,
    }


def container_setup_command() -> list[str]:
    """Use daemon-installed source; container imports cannot come from the workspace."""
    return [
        "/usr/local/bin/python3",
        "-I",
        "-S",
        "-c",
        Path(__file__).read_text(encoding="utf-8"),
    ]


if __name__ == "__main__":
    try:
        result = repair_upload_directories()
    except (OSError, RuntimeError, ValueError) as error:
        result = {"ok": False, "reason": type(error).__name__}
    print(json.dumps(result))
    raise SystemExit(0 if result["ok"] else 1)
