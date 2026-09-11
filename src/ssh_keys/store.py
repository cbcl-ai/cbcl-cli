"""Private office-ID-keyed SSH storage, bind-mounted into the same office only."""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from src._chown import chown_to_agent
from src.office_runtime import (
    RuntimeStorageError,
    require_ready,
    runtime_lock,
    ssh_keys_dir,
)

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_CONTAINER_SSH_DIR = "/home/agent/.ssh"


class SshKeyStoreError(Exception):
    """User-actionable failure without secret material."""


def _sanitize_name(name: str) -> str:
    cleaned = name.strip()
    if (
        not cleaned
        or len(cleaned) > 255
        or cleaned.startswith(".")
        or not _NAME_RE.fullmatch(cleaned)
    ):
        raise SshKeyStoreError("Invalid SSH key name")
    return cleaned


def host_ssh_dir_for_office(office_id: str) -> Path:
    require_ready(office_id)
    directory = ssh_keys_dir(office_id)
    directory.chmod(0o700)
    chown_to_agent(directory)
    return directory


def container_path_for(name: str) -> str:
    return f"{_CONTAINER_SSH_DIR}/{_sanitize_name(name)}"


def write_key(
    office_id: str,
    name: str,
    private_key: str,
    *,
    container_name: str | None,
) -> str:
    """Persist atomically; the verified office bind mount provides live visibility."""
    safe_name = _sanitize_name(name)
    try:
        with runtime_lock(office_id):
            directory = host_ssh_dir_for_office(office_id)
            directory_fd = os.open(
                directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            temporary = f".key-{uuid.uuid4().hex}"
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    output.write(
                        private_key
                        if private_key.endswith("\n")
                        else private_key + "\n"
                    )
                    output.flush()
                    os.fsync(output.fileno())
                chown_to_agent(directory / temporary)
                os.replace(
                    temporary,
                    safe_name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
                os.fsync(directory_fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                os.close(directory_fd)
    except (OSError, RuntimeStorageError) as exc:
        raise SshKeyStoreError(
            "Private SSH storage is unavailable; reconnect or recover migration"
        ) from exc
    return container_path_for(safe_name)


def remove_key(office_id: str, name: str, *, container_name: str | None) -> None:
    safe_name = _sanitize_name(name)
    try:
        with runtime_lock(office_id):
            directory = host_ssh_dir_for_office(office_id)
            descriptor = os.open(
                directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                try:
                    os.unlink(safe_name, dir_fd=descriptor)
                except FileNotFoundError:
                    pass
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except (OSError, RuntimeStorageError) as exc:
        raise SshKeyStoreError(
            "Private SSH storage is unavailable; deletion was not confirmed"
        ) from exc


def list_host_keys(office_id: str) -> list[str]:
    """Return names only; private key readback is never exposed."""
    with runtime_lock(office_id):
        directory = host_ssh_dir_for_office(office_id)
        return sorted(
            entry.name
            for entry in directory.iterdir()
            if not entry.name.startswith(".")
        )
