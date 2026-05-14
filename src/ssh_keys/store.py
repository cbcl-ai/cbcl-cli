"""Host-side + container-side storage for office SSH keys.

Files live in two places at once:

1. ``~/.cubicle/workspaces/<slug>/ssh-keys/<name>`` on the host —
   the persistent store. Survives container teardown; bind-mounted
   into the container at ``/home/agent/.ssh/`` on next start.
2. ``/home/agent/.ssh/<name>`` inside the office container (via
   the bind mount; or, for already-running legacy containers that
   don't have the mount, via ``docker exec``). This is what
   ``ssh user@host`` from inside the container actually reads.

Reading back the private key from disk is intentionally NOT
exposed — the UI shows the fingerprint only.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

from src.paths import get_workspace_path, slugify

logger = logging.getLogger(__name__)


_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_CONTAINER_SSH_DIR = "/home/agent/.ssh"


class SshKeyStoreError(Exception):
    """User-actionable failure (bad name, file write, docker exec)."""


def _sanitize_name(name: str) -> str:
    """Refuse anything the backend's validator wouldn't have let
    through. Defence-in-depth so a contract drift can't write a
    file outside ``ssh-keys/``."""
    n = name.strip()
    if not n or len(n) > 255 or n in (".", ".."):
        raise SshKeyStoreError(f"invalid ssh key name: {name!r}")
    if not _NAME_RE.match(n):
        raise SshKeyStoreError(
            f"invalid ssh key name {name!r}: may only contain "
            "letters, digits, dots, dashes, underscores",
        )
    if "/" in n or n.startswith("."):
        # Defensive: _NAME_RE already excludes /, and starting-dot
        # would be a hidden file ("." is excluded by the literal
        # check above but ".env" etc would pass — we refuse to
        # keep the listing predictable).
        if n.startswith("."):
            raise SshKeyStoreError(
                f"ssh key name cannot start with '.': {name!r}",
            )
    return n


def _office_ssh_dir(office_name: str) -> Path:
    """The host directory bind-mounted into the office container's
    ``/home/agent/.ssh/``. Mirrors the path the container_manager
    binds — keep both in sync."""
    return Path(get_workspace_path(slugify(office_name))) / "ssh-keys"


def host_ssh_dir_for_office(office_name: str) -> Path:
    """Ensure the host ssh-keys dir exists with 0700 perms and
    return its path. Single source of truth for the ssh-keys
    directory contract."""
    return _ensure_ssh_dir(_office_ssh_dir(office_name))


def ensure_ssh_dir_for_workspace(workspace_path: str | Path) -> Path:
    """Same contract as ``host_ssh_dir_for_office`` but takes the
    workspace path directly. Used by ``container_manager.start_office``
    so the chmod-mkdir-0700 logic lives in one place. Mounting the
    returned path as ``/home/agent/.ssh`` is the bind-mount the
    Communicator wires on container create."""
    return _ensure_ssh_dir(Path(workspace_path) / "ssh-keys")


def _ensure_ssh_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        # macOS host with restrictive umask sometimes blocks chmod
        # on bind-mounted dirs. Non-fatal — SSH inside the container
        # cares about the in-container perms, not the host dir's.
        logger.debug(
            "Could not chmod 0700 %s (non-fatal)", path, exc_info=True,
        )
    return path


def container_path_for(name: str) -> str:
    """The in-container path for a key with the given name. Backend
    persists this so agents can read it back without round-tripping
    through the Communicator."""
    return f"{_CONTAINER_SSH_DIR}/{_sanitize_name(name)}"


def write_key(
    office_name: str,
    name: str,
    private_key: str,
    *,
    container_name: str | None,
) -> str:
    """Write a private key to the host store AND to the live
    container (if one is running). Returns the in-container path.

    The private_key value is written to disk with mode 0600 and is
    NEVER returned or logged. The caller already ran fingerprint
    extraction; this layer just persists.
    """
    safe_name = _sanitize_name(name)
    host_dir = host_ssh_dir_for_office(office_name)
    host_path = host_dir / safe_name

    # Atomic write: write to a tempfile in the same directory then
    # rename. Guarantees the file either has the full content or
    # doesn't appear in the listing.
    tmp_path = host_dir / f".{safe_name}.tmp"
    try:
        # Honour the SSH convention: trailing newline preserved.
        body = private_key if private_key.endswith("\n") else private_key + "\n"
        # Open with restrictive mode from the start so an interrupted
        # write can't leave a world-readable file.
        fd = os.open(
            str(tmp_path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            os.write(fd, body.encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp_path, host_path)
        os.chmod(host_path, 0o600)
    except Exception as exc:
        # Best-effort cleanup of the tmp file.
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise SshKeyStoreError(
            f"failed to write key to host: {exc}",
        ) from exc

    # If the office container is running, also write the file
    # directly via ``docker exec``. On NEW containers the host dir
    # is bind-mounted at ``/home/agent/.ssh/`` so this is redundant
    # (the file is already visible). On OLD containers without the
    # mount, this is the fallback that makes the key immediately
    # usable without a restart.
    if container_name:
        _docker_write_inside(container_name, safe_name, body)

    return container_path_for(safe_name)


def remove_key(
    office_name: str,
    name: str,
    *,
    container_name: str | None,
) -> None:
    """Delete a key from BOTH the host store and the live container."""
    safe_name = _sanitize_name(name)
    host_path = _office_ssh_dir(office_name) / safe_name
    try:
        host_path.unlink()
    except FileNotFoundError:
        # Already gone — harmless. Carry on to the container clean-up.
        logger.debug(
            "ssh key %s already removed from host (%s)",
            safe_name, host_path,
        )
    except OSError as exc:
        logger.warning(
            "Failed to remove ssh key %s from host (%s): %s",
            safe_name, host_path, exc,
        )

    if container_name:
        _docker_remove_inside(container_name, safe_name)


def list_host_keys(office_name: str) -> list[str]:
    """Filenames of every key currently on the host. Used by
    reconcile / drift checks; doesn't read or expose the key
    bytes."""
    d = _office_ssh_dir(office_name)
    if not d.exists():
        return []
    return sorted(
        p.name for p in d.iterdir()
        if p.is_file() and not p.name.startswith(".")
    )


# ── docker exec helpers ──────────────────────────────────────────────


def _docker_write_inside(
    container_name: str, safe_name: str, body: str,
) -> None:
    """Stream the private key bytes into the container via ``docker
    exec ... tee``, then chmod 600. Bytes pass over stdin; they are
    NOT visible to ``ps`` (no command-line args carry them) or to
    ``docker inspect`` (no env vars). Best-effort: if the container
    isn't reachable, the host file is still in place and the key
    becomes usable on the next container restart."""
    container_path = f"{_CONTAINER_SSH_DIR}/{safe_name}"
    try:
        # 1. Ensure the .ssh dir exists with 0700 and is owned by agent.
        subprocess.run(
            [
                "docker", "exec", container_name,
                "sh", "-c",
                "mkdir -p /home/agent/.ssh && "
                "chmod 700 /home/agent/.ssh && "
                "chown agent:agent /home/agent/.ssh 2>/dev/null || true",
            ],
            capture_output=True, timeout=10, check=False,
        )

        # 2. Tee the key in via stdin to keep the bytes out of any
        #    process listing. ``--`` defends against keys whose
        #    sanitised filename looks like a flag.
        proc = subprocess.run(
            [
                "docker", "exec", "-i", container_name,
                "tee", "--", container_path,
            ],
            input=body, capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            logger.warning(
                "docker exec tee for %s returned %d: %s",
                container_name, proc.returncode,
                (proc.stderr or "").strip()[:300],
            )
            return

        # 3. chmod 600 + agent ownership so SSH actually accepts it.
        subprocess.run(
            [
                "docker", "exec", container_name,
                "sh", "-c",
                f"chmod 600 {container_path} && "
                f"chown agent:agent {container_path} 2>/dev/null || true",
            ],
            capture_output=True, timeout=10, check=False,
        )
        logger.info(
            "Live-installed ssh key %s into container %s",
            safe_name, container_name,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "docker exec timed out while installing ssh key %s "
            "into %s (host file is in place; restart office to apply)",
            safe_name, container_name,
        )
    except FileNotFoundError:
        # docker CLI not installed — Communicator may be running in
        # direct mode without Docker. The host file is still useful
        # if Docker mode is later enabled.
        logger.debug(
            "docker CLI not found; skipping live-install of ssh key %s",
            safe_name,
        )
    except Exception:
        logger.exception(
            "Unexpected error while live-installing ssh key %s into %s",
            safe_name, container_name,
        )


def _docker_remove_inside(
    container_name: str, safe_name: str,
) -> None:
    container_path = f"{_CONTAINER_SSH_DIR}/{safe_name}"
    try:
        proc = subprocess.run(
            [
                "docker", "exec", container_name,
                "rm", "-f", "--", container_path,
            ],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            logger.warning(
                "docker exec rm for %s returned %d: %s",
                container_name, proc.returncode,
                (proc.stderr or "").strip()[:300],
            )
    except subprocess.TimeoutExpired:
        logger.warning(
            "docker exec rm timed out for %s/%s",
            container_name, safe_name,
        )
    except FileNotFoundError:
        pass  # see write helper
    except Exception:
        logger.exception(
            "Unexpected error removing ssh key %s from %s",
            safe_name, container_name,
        )
