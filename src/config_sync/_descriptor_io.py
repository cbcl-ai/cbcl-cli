"""Descriptor-relative writes into the agent-writable workspace.

The daemon may run as root while everything under ``/workspace`` is writable
by the session uid (1000). A check such as ``is_symlink()`` followed by a
path-based ``mkdir`` / ``write_text`` / ``chown`` can be raced: a session that
swaps in a link between the check and the write redirects a root-owned write
or ownership change outside the workspace. These helpers never follow a link:
directories are opened ``O_NOFOLLOW | O_DIRECTORY``, files are created
``O_EXCL | O_NOFOLLOW`` next to their destination and renamed over it (a
planted link at the destination is replaced, not written through), and
ownership changes go through ``fchown`` on the open descriptor.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from src._chown import AGENT_GID, AGENT_UID

logger = logging.getLogger(__name__)

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_CREATE_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
)


@contextmanager
def open_dir_nofollow(name: str | Path, dir_fd: int | None = None) -> Iterator[int]:
    """Open a directory without following a link (ELOOP/ENOTDIR raise)."""
    descriptor = os.open(name, DIR_FLAGS, dir_fd=dir_fd)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


_ROOT_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
_OWNER_ACCESS = stat.S_IRWXU


def _may_restore_owner_access(info: os.stat_result) -> bool:
    """The daemon owns the directory (or runs as root)."""
    return stat.S_ISDIR(info.st_mode) and (
        os.geteuid() == 0 or info.st_uid == os.geteuid()
    )


@contextmanager
def open_workspace_root(workspace: str | Path) -> Iterator[int]:
    """Create (if needed) and open the office workspace itself.

    The workspace path is chosen by the daemon on the host (the bind-mount
    source); a session cannot replace it, only what is inside it. Its own
    components may therefore be followed (an operator may relocate the
    workspace with a link); everything below it must be opened relative to
    this descriptor with ``open_dir_nofollow``.

    A session can still remove the owner's permissions on the root (it owns
    it: office setup gives it to the agent uid). When the daemon owns it or
    runs as root, the owner's read, write and search permission is restored
    (R4-SEC-2); a root that still cannot be opened raises one ``OSError``
    naming the path and the repair, which stays fatal for the caller.
    """
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(workspace, _ROOT_FLAGS)
    except PermissionError as exc:
        descriptor = _reopen_after_restoring_access(workspace, exc)
    try:
        _restore_owner_access(descriptor, workspace)
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def open_existing_workspace_root(workspace: str | Path) -> Iterator[int]:
    """Open the office workspace itself, without creating or repairing it.

    Same trust rule as ``open_workspace_root`` (the root's own components
    may be followed; everything below it is opened relative to this
    descriptor with ``open_dir_nofollow``), but a missing workspace raises
    ``FileNotFoundError`` and no permission is restored. The workstream
    directory map treats a missing workspace as "no map", and the rollback
    helper is a dry run by default: neither may create directories or
    change permissions just by reading.
    """
    descriptor = os.open(workspace, _ROOT_FLAGS)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _reopen_after_restoring_access(workspace: Path, exc: PermissionError) -> int:
    try:
        info = os.stat(workspace)
        if _may_restore_owner_access(info):
            os.chmod(workspace, stat.S_IMODE(info.st_mode) | _OWNER_ACCESS)
            logger.warning(
                "The office workspace %s had no owner access (mode %o); it was "
                "restored.",
                workspace,
                stat.S_IMODE(info.st_mode),
            )
            return os.open(workspace, _ROOT_FLAGS)
    except OSError as retry_exc:
        exc = retry_exc  # type: ignore[assignment]
    raise OSError(
        exc.errno or errno.EACCES,
        f"The office workspace {workspace} cannot be opened ({exc.strerror or exc}). "
        "It must be a directory the user running cbcl can read and search: "
        f"check its owner and mode (for example `chmod u+rwx {workspace}`); "
        "the office connects on the next retry.",
    ) from exc


def _restore_owner_access(descriptor: int, workspace: Path) -> None:
    """Give the owner (the agent uid after office setup) back full access
    to an open root a session removed it from; best-effort."""
    try:
        info = os.fstat(descriptor)
        mode = stat.S_IMODE(info.st_mode)
        if mode & _OWNER_ACCESS != _OWNER_ACCESS and _may_restore_owner_access(info):
            os.fchmod(descriptor, mode | _OWNER_ACCESS)
            logger.warning(
                "The office workspace %s had mode %o; the owner's access was "
                "restored.",
                workspace,
                mode,
            )
    except OSError as exc:
        logger.warning("Could not check the mode of %s: %s", workspace, exc)


def _require_single_component(name: str) -> None:
    """Refuse a name that would resolve through more than one directory entry.

    ``mkdir``/``open`` relative to a descriptor still follow links in every
    component but the last, so a multi-component name would reopen the
    link-following hole these helpers close.
    """
    if not name or name in (".", "..") or "/" in name or "\x00" in name:
        raise OSError(errno.EINVAL, f"not a single path component: {name!r}")


# Errors that mean the host cannot store what the daemon writes: no space or
# quota, an I/O or read-only file system error, or exhausted descriptors or
# memory. Only these fail a config sync; every other error on an entry below
# the workspace root (a planted link, file or directory, a permission a
# session changed, a busy or non-empty entry) is logged and the entry skipped.
ENVIRONMENTAL_ERRNOS = frozenset(
    code
    for code in (
        errno.ENOSPC,
        getattr(errno, "EDQUOT", None),
        errno.EIO,
        errno.EROFS,
        errno.EMFILE,
        errno.ENFILE,
        errno.ENOMEM,
    )
    if code is not None
)


def is_environmental_error(exc: OSError) -> bool:
    """True when ``exc`` means the host cannot store the write (see
    ``ENVIRONMENTAL_ERRNOS``); such a failure is reported so config sync
    keeps admission closed and retries. Every other error on an entry below
    the workspace root is one a session can cause: the entry is logged and
    skipped, so a session can never pause config sync."""
    return exc.errno in ENVIRONMENTAL_ERRNOS


class MaterializationFailures:
    """Collects write failures so the remaining entries are still written.

    ``handle`` records an environmental error and logs-and-skips any other
    (session-caused) one; ``skip`` only logs (cleanup of session-writable
    content is never a failure). ``raise_if_any`` then raises one
    ``OSError`` naming every recorded entry, so config sync keeps admission
    closed and retries.
    """

    def __init__(self) -> None:
        self._failed: list[str] = []
        self._errno: int | None = None

    def handle(self, exc: OSError, what: str) -> None:
        if not is_environmental_error(exc):
            self.skip(exc, what)
            return
        logger.error("Could not write %s: %s", what, exc)
        self._failed.append(f"{what}: {exc}")
        if self._errno is None:
            self._errno = exc.errno

    @staticmethod
    def skip(exc: OSError, what: str) -> None:
        logger.warning("%s is skipped (%s).", what, exc)

    def merge(self, exc: OSError) -> None:
        """Record an ``OSError`` a step raised (after its own skips)."""
        self._failed.append(str(exc))
        if self._errno is None:
            self._errno = exc.errno

    def raise_if_any(self) -> None:
        if self._failed:
            raise OSError(
                self._errno or errno.EIO,
                "workspace materialization failed: " + "; ".join(self._failed),
            )


def fchown_to_agent(descriptor: int) -> None:
    """Best-effort ownership change of an OPEN file or directory to the
    in-container agent uid/gid (no-op without CAP_CHOWN, e.g. macOS dev)."""
    try:
        os.fchown(descriptor, AGENT_UID, AGENT_GID)
    except OSError as exc:
        logger.debug("fchown to the agent uid failed: %s", exc)


def ensure_subdirectory(parent_fd: int, name: str, mode: int = 0o755) -> None:
    """Create ``name`` under ``parent_fd`` if it is absent (never through a
    link; an existing link or file is left for the no-follow open to refuse)."""
    _require_single_component(name)
    try:
        os.mkdir(name, mode, dir_fd=parent_fd)
    except FileExistsError:
        pass


def atomic_replace_file(
    directory_fd: int, filename: str, content: str | bytes, mode: int = 0o644
) -> None:
    """Atomically replace ``filename`` in the directory ``directory_fd``.

    A concurrent reader sees the old or the new complete file. The temporary
    file is created exclusively without following a link and owned by the
    agent uid before the rename; a leftover temporary (or a link planted at
    its name) is removed first, never written through.
    """
    data = content.encode() if isinstance(content, str) else content
    temporary = f".{filename}.{os.getpid()}.tmp"
    try:
        os.unlink(temporary, dir_fd=directory_fd)
    except FileNotFoundError:
        pass
    descriptor = os.open(temporary, _CREATE_FLAGS, mode, dir_fd=directory_fd)
    try:
        try:
            remaining = memoryview(data)
            while remaining:
                remaining = remaining[os.write(descriptor, remaining) :]
            fchown_to_agent(descriptor)
        finally:
            os.close(descriptor)
        os.replace(
            temporary, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd
        )
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except OSError:
            pass
        raise


@contextmanager
def open_owned_subdirectory(parent_fd: int, name: str) -> Iterator[int]:
    """Create (if needed), open without following a link, and own ``name``.

    A link, file or other non-directory at ``name`` raises ``OSError``; the
    ownership change goes through ``fchown`` on the opened directory.
    """
    ensure_subdirectory(parent_fd, name)
    with open_dir_nofollow(name, parent_fd) as descriptor:
        fchown_to_agent(descriptor)
        yield descriptor


@contextmanager
def open_owned_path(parent_fd: int, *parts: str) -> Iterator[int]:
    """``open_owned_subdirectory`` for each component of ``parts`` in turn.

    Every intermediate directory is created, opened without following a link
    and owned by the agent uid; the descriptor of the last one is yielded.
    """
    if not parts:
        raise OSError(errno.EINVAL, "no path components")
    with ExitStack() as stack:
        descriptor = parent_fd
        for part in parts:
            descriptor = stack.enter_context(open_owned_subdirectory(descriptor, part))
        yield descriptor


def ensure_owned_directory(parent_fd: int, *parts: str) -> None:
    """Create and own ``parts`` under ``parent_fd`` without following a link."""
    with open_owned_path(parent_fd, *parts):
        pass


def lstat_nofollow(name: str, dir_fd: int) -> os.stat_result | None:
    """``lstat`` of ``name`` under ``dir_fd`` (a link is not followed);
    None when it does not exist."""
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def is_real_directory(dir_fd: int, name: str) -> bool:
    """True when ``name`` is a directory itself, not a link to one."""
    info = lstat_nofollow(name, dir_fd)
    return info is not None and stat.S_ISDIR(info.st_mode)


def list_real_directories(
    dir_fd: int, failures: MaterializationFailures, what: str
) -> tuple[list[str], list[str]]:
    """``(real directories, entries of unknown type)`` under ``dir_fd``.

    A listing or ``lstat`` a session can make fail (it removed read or
    search permission) is logged: an entry whose type cannot be read is
    returned as unknown, never as removable, and an unreadable listing
    returns ``([], ["?"])`` so callers treat the directory as not empty.
    An environmental error is recorded in ``failures``.
    """
    try:
        names = os.listdir(dir_fd)
    except OSError as exc:
        failures.handle(exc, f"the listing of {what}")
        return [], ["?"]
    real: list[str] = []
    unknown: list[str] = []
    for name in names:
        try:
            if is_real_directory(dir_fd, name):
                real.append(name)
        except OSError as exc:
            failures.handle(exc, f"{what}/{name}")
            unknown.append(name)
    return real, unknown


def is_regular_file(dir_fd: int, name: str) -> bool:
    """True when ``name`` is a regular file itself, not a link to one."""
    info = lstat_nofollow(name, dir_fd)
    return info is not None and stat.S_ISREG(info.st_mode)


def remove_directory_tree(parent_fd: int, name: str) -> None:
    """Delete the real directory ``name`` under ``parent_fd`` and its content.

    Refuses anything that is not a directory itself (a link is never
    followed, at the top or below: ``shutil.rmtree`` with ``dir_fd`` walks
    by descriptor and only unlinks nested links).
    """
    _require_single_component(name)
    if not is_real_directory(parent_fd, name):
        raise OSError(errno.ENOTDIR, f"not a real directory: {name!r}")
    try:
        shutil.rmtree(name, dir_fd=parent_fd)
    except NotImplementedError as exc:  # no descriptor-relative rmtree here
        raise OSError(errno.ENOSYS, str(exc)) from exc
