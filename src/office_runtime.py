"""Private, immutable-office-owned credential storage and migration."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import stat
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from src import paths

RUNTIME_VERSION = 1
_KINDS = {"claude-auth": ".claude-auth", "ssh-keys": "ssh-keys"}
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class RuntimeStorageError(RuntimeError):
    """Credential storage needs operator attention before an office can start."""


def canonical_office_id(office_id: str) -> str:
    try:
        return str(uuid.UUID(str(office_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeStorageError("A valid immutable office UUID is required") from exc


def runtime_base() -> Path:
    return paths.CUBICLE_HOME / "private-runtime"


def office_runtime_dir(office_id: str) -> Path:
    return runtime_base() / "offices" / canonical_office_id(office_id)


def claude_auth_dir(office_id: str) -> Path:
    return office_runtime_dir(office_id) / "claude-auth"


def ssh_keys_dir(office_id: str) -> Path:
    return office_runtime_dir(office_id) / "ssh-keys"


def _private_directory(directory: Path) -> None:
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    metadata = directory.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise RuntimeStorageError("Private runtime parent has unsafe ownership or type")
    directory.chmod(0o700)


def _initialize_parent() -> None:
    paths.CUBICLE_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    if paths.CUBICLE_HOME.is_symlink():
        raise RuntimeStorageError("Cubicle home must not be a symbolic link")
    _private_directory(runtime_base())
    _private_directory(runtime_base() / "offices")
    _private_directory(runtime_base() / "locks")


def _lock_descriptor(office_id: str) -> int:
    _initialize_parent()
    lock_path = runtime_base() / "locks" / f"{canonical_office_id(office_id)}.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
        os.close(descriptor)
        raise RuntimeStorageError("Private runtime lock has unsafe ownership or type")
    return descriptor


@contextmanager
def runtime_lock(office_id: str):
    descriptor = _lock_descriptor(office_id)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


@asynccontextmanager
async def async_runtime_lock(office_id: str):
    descriptor = _lock_descriptor(office_id)
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        yield
    finally:
        os.close(descriptor)


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, _DIRECTORY_FLAGS)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_record(path: Path, record: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(record, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read_record(path: Path) -> dict | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "r", encoding="utf-8") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 * 1024:
            raise RuntimeStorageError("Invalid private runtime metadata")
        try:
            record = json.load(source)
        except (ValueError, UnicodeError) as exc:
            raise RuntimeStorageError(
                "Private runtime metadata needs recovery"
            ) from exc
    if not isinstance(record, dict):
        raise RuntimeStorageError("Invalid private runtime metadata")
    return record


def read_auth_file(office_id: str, name: str) -> str:
    if name not in (".credentials.json", ".credentials.json.backup"):
        raise RuntimeStorageError("Unsupported credential filename")
    directory_fd = os.open(claude_auth_dir(office_id), _DIRECTORY_FLAGS)
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
        )
        with os.fdopen(descriptor, "r", encoding="utf-8") as source:
            metadata = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size > 1024 * 1024
            ):
                raise OSError("Unsafe credential file")
            return source.read(1024 * 1024 + 1)
    finally:
        os.close(directory_fd)


def write_auth_file(office_id: str, name: str, content: str) -> None:
    if name not in (".credentials.json", ".credentials.json.backup"):
        raise RuntimeStorageError("Unsupported credential filename")
    from src._chown import AGENT_GID, AGENT_UID

    directory_fd = os.open(claude_auth_dir(office_id), _DIRECTORY_FLAGS)
    temporary = f".credentials-{uuid.uuid4().hex}"
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            try:
                os.fchown(output.fileno(), AGENT_UID, AGENT_GID)
            except PermissionError:
                pass
            os.fsync(output.fileno())
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _workspace_path(workspace_path: str | Path) -> Path:
    workspace = Path(workspace_path).absolute()
    if workspace != workspace.resolve() or not workspace.is_dir():
        raise RuntimeStorageError(
            "Legacy workspace must be an existing non-symlink directory"
        )
    private = runtime_base().resolve()
    if (
        workspace == private
        or workspace in private.parents
        or private in workspace.parents
    ):
        raise RuntimeStorageError(
            "Public workspace and private runtime must not overlap"
        )
    return workspace


def _identity(path: Path) -> list[int] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeStorageError(
            "Credential backing must be a real directory, not a link"
        )
    return [metadata.st_dev, metadata.st_ino]


def _walk_directory(source_fd: int, destination_fd: int | None = None) -> str:
    digest = hashlib.sha256()
    for name in sorted(os.listdir(source_fd)):
        metadata = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        digest.update(name.encode("utf-8", "surrogateescape") + b"\0")
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=source_fd)
            target_fd = None
            try:
                if destination_fd is not None:
                    os.mkdir(name, mode=0o700, dir_fd=destination_fd)
                    target_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=destination_fd)
                digest.update(
                    b"directory\0" + _walk_directory(child_fd, target_fd).encode()
                )
                if target_fd is not None:
                    os.fsync(target_fd)
            finally:
                os.close(child_fd)
                if target_fd is not None:
                    os.close(target_fd)
        elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_fd
            )
            target = None
            try:
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                    raise RuntimeStorageError(
                        "Credential file changed during migration"
                    )
                if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                    raise RuntimeStorageError(
                        "Credential file is not an unlinked regular file"
                    )
                if destination_fd is not None:
                    target = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=destination_fd,
                    )
                    os.fchmod(target, 0o600 | (opened.st_mode & 0o100))
                file_digest = hashlib.sha256()
                while chunk := os.read(descriptor, 1024 * 1024):
                    file_digest.update(chunk)
                    if target is not None:
                        remaining = memoryview(chunk)
                        while remaining:
                            remaining = remaining[os.write(target, remaining) :]
                final = os.fstat(descriptor)
                if (opened.st_size, opened.st_mtime_ns) != (
                    final.st_size,
                    final.st_mtime_ns,
                ):
                    raise RuntimeStorageError(
                        "Credential file changed during migration"
                    )
                digest.update(
                    b"file\0"
                    + bytes([bool(opened.st_mode & 0o100)])
                    + file_digest.digest()
                )
                if target is not None:
                    os.fsync(target)
            finally:
                os.close(descriptor)
                if target is not None:
                    os.close(target)
        else:
            raise RuntimeStorageError(
                "Credential migration refuses links and special files"
            )
    return digest.hexdigest()


def _tree_digest(source: Path, destination: Path | None = None) -> str:
    source_fd = os.open(source, _DIRECTORY_FLAGS)
    target_fd = None
    try:
        if destination is not None:
            destination.mkdir(mode=0o700)
            target_fd = os.open(destination, _DIRECTORY_FLAGS)
        result = _walk_directory(source_fd, target_fd)
        if target_fd is not None:
            os.fsync(target_fd)
        return result
    finally:
        os.close(source_fd)
        if target_fd is not None:
            os.close(target_fd)


def approve_legacy_ownership(office_id: str, workspace_path: str | Path) -> None:
    """Record an explicit operator mapping; the caller must quiesce the daemon."""
    office_id = canonical_office_id(office_id)
    with runtime_lock(office_id):
        workspace = _workspace_path(workspace_path)
        root = office_runtime_dir(office_id)
        _private_directory(root)
        approval = {
            "office_id": office_id,
            "workspace": str(workspace),
            "workspace_identity": _identity(workspace),
            "legacy": {
                kind: _identity(workspace / name) for kind, name in _KINDS.items()
            },
        }
        previous = _read_record(root / "approval.json")
        if previous is not None and previous != approval:
            raise RuntimeStorageError(
                "A different legacy ownership mapping already exists"
            )
        if _read_record(root / "state.json") is not None:
            raise RuntimeStorageError(
                "Migration already started; preserve its journal for recovery"
            )
        _write_record(root / "approval.json", approval)


def prepare_runtime(
    office_id: str,
    workspace_path: str | Path,
    *,
    legacy_authorized: bool = False,
) -> None:
    """Migrate while the caller holds the runtime lock and all containers are stopped."""
    office_id = canonical_office_id(office_id)
    workspace = _workspace_path(workspace_path)
    root = office_runtime_dir(office_id)
    _private_directory(root)
    state_path = root / "state.json"
    state = _read_record(state_path)
    if state is not None:
        if state.get("office_id") != office_id or state.get("workspace") != str(
            workspace
        ):
            raise RuntimeStorageError(
                "Private credential storage belongs to a different mapping"
            )
        if state.get("phase") == "ready":
            require_ready(office_id)
            return
        if state.get("version") != RUNTIME_VERSION or state.get("phase") != "migrating":
            raise RuntimeStorageError("Unsupported private credential migration state")
    else:
        legacy = {kind: _identity(workspace / name) for kind, name in _KINDS.items()}
        if any(legacy.values()) and not legacy_authorized:
            approval = _read_record(root / "approval.json")
            expected = {
                "office_id": office_id,
                "workspace": str(workspace),
                "workspace_identity": _identity(workspace),
                "legacy": legacy,
            }
            if approval != expected:
                raise RuntimeStorageError(
                    "Legacy credential ownership is unverified. Stop cbcl and run "
                    "cbcl migrate-credentials --office-id UUID --workspace PATH "
                    "--approve-legacy-owner after verifying this office's ownership."
                )
        for kind, identity in legacy.items():
            if (root / kind).exists():
                raise RuntimeStorageError(
                    "Unjournaled private credentials exist; refusing overwrite"
                )
            if identity is not None and identity[0] != root.stat().st_dev:
                raise RuntimeStorageError(
                    "Cross-filesystem legacy migration requires operator recovery"
                )
        fingerprints = {
            kind: _tree_digest(workspace / _KINDS[kind]) if identity else None
            for kind, identity in legacy.items()
        }
        state = {
            "version": RUNTIME_VERSION,
            "office_id": office_id,
            "workspace": str(workspace),
            "phase": "migrating",
            "legacy": legacy,
            "fingerprints": fingerprints,
        }
        _write_record(state_path, state)
    _private_directory(root / "rollback")
    for kind, source_name in _KINDS.items():
        source = workspace / source_name
        destination = root / kind
        original = state["legacy"][kind]
        fingerprint = state["fingerprints"][kind]
        if original is None:
            if _identity(source) is not None:
                raise RuntimeStorageError(
                    "Unexpected legacy credentials appeared during migration"
                )
            if not destination.exists():
                destination.mkdir(mode=0o700)
            elif _tree_digest(destination) != hashlib.sha256().hexdigest():
                raise RuntimeStorageError(
                    "Unverified private destination contains data"
                )
            continue
        rollback = root / "rollback" / kind
        if rollback.exists():
            if source.exists() or _identity(rollback) != original:
                raise RuntimeStorageError(
                    "Conflicting legacy rollback; refusing to select credentials"
                )
            if (
                _tree_digest(rollback) != fingerprint
                or _tree_digest(destination) != fingerprint
            ):
                raise RuntimeStorageError(
                    "Credential verification failed during migration recovery"
                )
            continue
        if _identity(source) != original or _tree_digest(source) != fingerprint:
            raise RuntimeStorageError(
                "Legacy credentials changed; operator recovery is required"
            )
        if not destination.exists():
            staging = root / f"staging-{kind}"
            if not staging.exists():
                copied = _tree_digest(source, staging)
            else:
                copied = _tree_digest(staging)
            if copied != fingerprint:
                raise RuntimeStorageError("Credential staging verification failed")
            _sync_directory(root)
            os.rename(staging, destination)
            _sync_directory(root)
        if (
            _tree_digest(destination) != fingerprint
            or _tree_digest(source) != fingerprint
        ):
            raise RuntimeStorageError(
                "Credential copy differs; refusing to overwrite either copy"
            )
        if _identity(source) != original:
            raise RuntimeStorageError(
                "Legacy credential directory changed during migration"
            )
        os.rename(source, rollback)
        _sync_directory(workspace)
        _sync_directory(root / "rollback")
    state["phase"] = "ready"
    _write_record(state_path, state)
    require_ready(office_id)


def require_ready(office_id: str) -> Path:
    root = office_runtime_dir(office_id)
    state = _read_record(root / "state.json")
    if (
        state is None
        or state.get("version") != RUNTIME_VERSION
        or state.get("phase") != "ready"
        or state.get("office_id") != canonical_office_id(office_id)
    ):
        raise RuntimeStorageError(
            "Office credential migration is incomplete; reconnect after recovery"
        )
    for kind, legacy_name in _KINDS.items():
        if _identity(root / kind) is None:
            raise RuntimeStorageError("Private credential directory is missing")
        if os.path.lexists(Path(state["workspace"]) / legacy_name):
            raise RuntimeStorageError(
                "A legacy credential alias reappeared in the public workspace"
            )
    return root


def private_mounts_match(container, office_id: str) -> bool:
    state = _read_record(office_runtime_dir(office_id) / "state.json")
    if state is None or state.get("phase") != "ready" or not state.get("workspace"):
        return False
    expected = {
        "/workspace": state["workspace"],
        "/home/agent/.claude": str(claude_auth_dir(office_id)),
        "/home/agent/.ssh": str(ssh_keys_dir(office_id)),
    }
    actual = {
        mount.get("Destination"): mount.get("Source")
        for mount in container.attrs.get("Mounts", [])
        if mount.get("Type") == "bind"
    }
    writable = {
        mount.get("Destination")
        for mount in container.attrs.get("Mounts", [])
        if mount.get("Type") == "bind" and mount.get("RW") is True
    }
    environment = container.attrs.get("Config", {}).get("Env", []) or []
    return (
        container.labels.get("cbcl.office_id") == canonical_office_id(office_id)
        and all(actual.get(target) == source for target, source in expected.items())
        and set(expected).issubset(writable)
        and not any(
            item.startswith(("ANTHROPIC_API_KEY=", "CLAUDE_CODE_OAUTH_TOKEN="))
            for item in environment
        )
    )


def validated_container_id(office_id: str, container_name: str) -> str:
    """Resolve a running, correctly mounted container without trusting its name."""
    import docker

    require_ready(office_id)
    client = docker.from_env()
    try:
        container = client.containers.get(container_name)
        if container.status != "running" or not private_mounts_match(
            container, office_id
        ):
            raise RuntimeStorageError(
                "Office container requires credential migration or restart"
            )
        return container.id
    except docker.errors.DockerException as exc:
        raise RuntimeStorageError(
            "The selected office container is unavailable"
        ) from exc
    finally:
        client.close()


async def resolve_office_container_id(office_id: str, container_name: str) -> str:
    """Snapshot the verified container identity under the office lifecycle lock."""
    async with async_runtime_lock(office_id):
        return await asyncio.to_thread(
            validated_container_id, office_id, container_name
        )


def assert_no_running_credential_users(
    client, office_id: str, workspace_path: str
) -> None:
    """Refuse migration if any container still has access to its backing paths."""
    workspace = _workspace_path(workspace_path)
    protected = [workspace, office_runtime_dir(office_id).resolve()]
    for container in client.containers.list(filters={"status": "running"}):
        for mount in container.attrs.get("Mounts", []):
            source = mount.get("Source")
            if mount.get("Type") != "bind" or not source:
                continue
            source_path = Path(source).resolve()
            if any(
                source_path == target
                or source_path in target.parents
                or target in source_path.parents
                for target in protected
            ):
                raise RuntimeStorageError(
                    "Stop every container using this office's storage before migration"
                )


def assert_no_other_daemon() -> None:
    try:
        daemon_pid = int(paths.get_pid_path().read_text().strip())
    except FileNotFoundError:
        return
    except ValueError as exc:
        raise RuntimeStorageError(
            "Resolve the invalid daemon PID record before migration"
        ) from exc
    if daemon_pid == os.getpid():
        return
    try:
        os.kill(daemon_pid, 0)
    except ProcessLookupError:
        return
    raise RuntimeStorageError(
        "Stop the running communicator before migrating credential mounts"
    )


def legacy_mounts_authorize(container, office_id: str, workspace_path: str) -> bool:
    if (container.labels or {}).get("cbcl.office_id") != canonical_office_id(office_id):
        return False
    actual = {
        mount.get("Destination"): mount.get("Source")
        for mount in container.attrs.get("Mounts", [])
        if mount.get("Type") == "bind"
    }
    workspace = _workspace_path(workspace_path)
    if actual.get("/workspace") != str(workspace):
        return False
    for kind, name in _KINDS.items():
        target = "/home/agent/.claude" if kind == "claude-auth" else "/home/agent/.ssh"
        if _identity(workspace / name) is not None and actual.get(target) != str(
            workspace / name
        ):
            return False
    return True


def remove_private_runtime(office_id: str, *, client) -> None:
    """Delete only a journal-owned office after its container has been removed."""
    import shutil

    with runtime_lock(office_id):
        root = office_runtime_dir(office_id)
        if not root.exists():
            return
        for container in client.containers.list(all=True):
            if (container.labels or {}).get("cbcl.office_id") == canonical_office_id(
                office_id
            ):
                raise RuntimeStorageError(
                    "Remove the office container before deleting its credentials"
                )
            for mount in container.attrs.get("Mounts", []):
                source = Path(mount.get("Source") or "/").resolve()
                if mount.get("Type") == "bind" and (
                    source == root or source in root.parents or root in source.parents
                ):
                    raise RuntimeStorageError(
                        "A container still mounts this office's private storage"
                    )
        state = _read_record(root / "state.json")
        if state is None or state.get("office_id") != canonical_office_id(office_id):
            raise RuntimeStorageError(
                "Cannot prove private runtime ownership for deletion"
            )
        if root.is_symlink() or not shutil.rmtree.avoids_symlink_attacks:
            raise RuntimeStorageError("Safe private runtime deletion is unavailable")
        shutil.rmtree(root)
        _sync_directory(root.parent)
