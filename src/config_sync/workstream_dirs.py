"""Workstream workspace directories: identity map, renames, safe orphans.

Every workstream owns ``/workspace/workstreams/<dir>/`` where ``<dir>`` is the
``workspace_dir`` the backend declares (``paths.workstream_dir_slug(name,
short_code)``), or, when an older backend declares none, the legacy
``paths.slugify(name)`` layout that backend writes to, unless the map records
that the backend already declared the new layout for that workstream
(``WorkstreamLayout.directory_for``; a backend rollback). It holds the
daemon-written ``CLAUDE.md`` plus content nothing here can regenerate:
the backend's approved ``spec.md`` / ``plan.md`` / ``intake/`` projections,
legacy ``learnings*.md``, and (in offices with dynamic execution) the
task-owned ``tasks/`` and ``scopes/*/tasks/`` outputs.

Because the directory name follows the display name, a rename changes it.
The daemon keeps a small identity map (``.cubicle/workstream_dirs.json`` in
the workspace: workstream id -> directory) so a rename MOVES the directory
instead of orphaning it:

* Renames move in two phases through hidden ``.relocating-<id>`` names, so
  swaps and chains of renames cannot clobber each other and an interrupted
  move is resumed on the next sync.
* When the target already exists (the backend re-materialised a projection
  there first, or a leftover), the old directory is MERGED into it: entries
  the target lacks move over, byte-identical duplicates are dropped, and
  conflicting entries stay behind and are archived. A target owned by
  another current workstream is never merged into.
* A late write into a renamed workstream's old directory (a session that
  started before the rename) is merged forward on the next sync.
* An orphan is deleted only when it holds nothing but the regenerable
  ``CLAUDE.md``; anything else is moved to ``workstreams/.archived/`` under
  a unique name. An existing archive is never overwritten.
* Without a map (the first sync after an upgrade) each workstream's
  previous directory is taken to be the legacy ``paths.slugify(name)``
  layout, so a non-Latin workstream moves out of the old shared ``office``
  directory when it was its only owner.
* A move never lands on something it cannot merge into: a regular file or
  link at the target name, or the directory of a workstream whose own move
  could not be staged this pass, keeps the moved directory staged and the
  next sync retries. A directory the map still assigns to a DELETED
  workstream is archived before a renamed workstream takes that name, so
  the renamed one never adopts the deleted one's spec or outputs. The map
  keeps that claim until the directory is actually archived, so a failed
  archive blocks the name on every later sync too, not only this one.
* The map authorizes every move, merge and archive; a directory's
  ``CLAUDE.md`` (which names its workstream id in an HTML comment under the
  heading) can only VETO one, so a forged or stale marker makes the daemon
  do less, never mix content. A recorded directory naming another current or
  a deleted workstream is not moved (a stale entry replaying a swap or chain
  after a lost map update); a deleted claim on a directory whose id marker
  names a current workstream is dropped. Adoption check: an existing
  directory the map does not record for its claimant (or a missing map's
  legacy guess) is never taken when its ``CLAUDE.md`` names a deleted
  workstream (archived first) or another current one (frozen, not
  archived), and late writes at a retired name are merged forward first.
* A workstream whose move is still staged is recorded at its TARGET name,
  with the directory it came from kept apart (``staged``): it never counts
  as occupying its old name, so a workstream that takes that name later
  keeps (and moves) its own directory.
* The map records the directory the backend last DECLARED for each
  workstream. When a rolled-back backend declares none, a workstream the
  backend already moved to the declared layout keeps it (no move back, no
  archive; registered paths stay valid) and the legacy directory that
  backend writes projections to is kept out of the orphan sweep; with a
  single owner it merges forward once the layout is declared again. The map
  also records each workstream's legacy directory for the rollback helper
  (``workstream_dirs_rollback``), which puts directories back where a daemon
  older than ``workstream_dirs_v1`` looks for them. The kept legacy
  directory stays associated with its workstream until it is merged forward
  or archived: another workstream at that name (one created while the
  backend was rolled back) never moves it away with its own rename.

The map sits in the agent-writable workspace, so it is DATA: every entry is
validated (UUID ids, slug-alphabet directory names) before it can steer a
move, and every filesystem operation, including the CLAUDE.md write and its
ownership change, runs relative to directory descriptors opened without
following symlinks. A planted symlink is never followed, merged into,
written through or archived through.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import secrets
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from src.config_sync._descriptor_io import (
    MaterializationFailures,
    atomic_replace_file,
    ensure_subdirectory,
    fchown_to_agent,
    is_real_directory,
    list_real_directories,
    lstat_nofollow,
    open_dir_nofollow,
    open_existing_workspace_root,
    open_owned_subdirectory,
    open_workspace_root,
)
from src.paths import (
    WORKSPACE_DIR_RE,
    legacy_workstream_dir_slug,
    workstream_dir_slug,
)

logger = logging.getLogger(__name__)

MAP_DIRNAME = ".cubicle"
MAP_FILENAME = "workstream_dirs.json"
WORKSTREAMS_DIRNAME = "workstreams"
ARCHIVE_DIRNAME = ".archived"
RELOCATING_PREFIX = ".relocating-"
# A deleted workstream's directory that could not be archived while a
# current workstream claims its name is moved aside to ``.deleted-<id>``
# (recorded in the map's ``deleted`` section), freeing the name.
DELETED_PREFIX = ".deleted-"
CLAUDE_MD = "CLAUDE.md"
# Version 2 adds ``declared``, ``legacy``, ``deleted``, ``deleted_identity``,
# ``staged``, ``kept_legacy`` and ``shared_legacy`` (additive: a version-1
# reader ignores them, and a version-1 map loads with them empty). The
# write-ahead ``pending`` plan and its ``seeded`` list are additive too: a
# reader that predates them ignores both and sees the map as it was before
# the interrupted sync (what a lost save left before), and its own save
# drops them. So is ``unverified``: an older reader sees those workstreams
# without an entry (new ones), as before.
_MAP_VERSION = 2
_MAP_MAX_BYTES = 1024 * 1024
_COMPARE_MAX_BYTES = 64 * 1024 * 1024
# Shared-subdirectory nesting a merge descends (see ``_merge_tree``).
_MERGE_MAX_DEPTH = 32
# Files the daemon rewrites on every sync; nothing else in a workstream
# directory is safe to delete.
_REGENERABLE_FILES = frozenset({"CLAUDE.md"})
# The owner a daemon-written workstream CLAUDE.md names (see
# ``claude_md_templates/_workstream.py``): the heading, an id marker on the
# next line (written since the adoption check; older files have none) and
# the short-code line.
_HEADING = "# Workstream: "
_ID_MARKER_RE = re.compile(r"<!-- workstream-id: ([0-9A-Fa-f-]{36}) -->")
_SHORT_CODE_RE = re.compile(r"\*\*Short code:\*\* `([^`]+)`")
_ATTRIBUTION_BYTES = 8192
# ``open(O_NOFOLLOW | O_NONBLOCK)`` errors meaning "not a regular file": a
# link, a socket or a device. Nothing a daemon wrote.
_NOT_A_FILE_ERRNOS = frozenset(
    {errno.ELOOP, errno.EMLINK, errno.ENXIO, errno.ENODEV, errno.EOPNOTSUPP}
)
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def valid_directory_name(name: object) -> bool:
    """A workstream directory name the daemon itself could have produced."""
    return isinstance(name, str) and bool(WORKSPACE_DIR_RE.fullmatch(name))


def canonical_workstream_id(value: object) -> str | None:
    """The canonical UUID string of a workstream id, or None."""
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def _is_regenerable(name: str) -> bool:
    return name in _REGENERABLE_FILES or (
        name.startswith(".CLAUDE.md.") and name.endswith(".tmp")
    )


def _move_staged(workstream_id: str, root_fd: int) -> bool:
    """True when the workstream's directory sits staged under
    ``.relocating-<id>``: it then occupies no name under ``workstreams/``."""
    return is_real_directory(root_fd, f"{RELOCATING_PREFIX}{workstream_id}")


@contextmanager
def open_workstreams_root(workspace: Path) -> Iterator[int]:
    """Create (if needed) and open ``<workspace>/workstreams``.

    The directory is created and opened relative to the workspace
    descriptor without following a link; a link or file at that name
    raises ``OSError`` (the caller refuses to write through it).
    """
    with open_workspace_root(workspace) as workspace_fd, open_owned_subdirectory(
        workspace_fd, WORKSTREAMS_DIRNAME
    ) as root_fd:
        yield root_fd


def has_real_directories(root_fd: int) -> bool:
    """True when the workstreams directory holds any real subdirectory, or
    an entry whose type cannot be read (a session removed search
    permission), so an empty-sync guard refuses cleanup. An environmental
    error raises."""
    failures = MaterializationFailures()
    real, unknown = list_real_directories(root_fd, failures, WORKSTREAMS_DIRNAME)
    failures.raise_if_any()
    return bool(real or unknown)


def directory_attribution(
    root_fd: int, name: str
) -> tuple[str | None, str | None] | None:
    """The owner ``<name>/CLAUDE.md`` names when a daemon wrote it: its
    workstream id marker and short code (either may be None).

    None when there is no daemon-written ``CLAUDE.md`` (none, a link or other
    non-regular file, or no ``# Workstream:`` heading). Reads a bounded head
    relative to descriptors, never through a link. Raises ``OSError`` when
    the directory or file exists but cannot be read.
    """
    with open_dir_nofollow(name, root_fd) as directory:
        try:
            descriptor = os.open(CLAUDE_MD, _READ_FLAGS, dir_fd=directory)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in _NOT_A_FILE_ERRNOS:
                return None
            raise
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return None
            head = os.read(descriptor, _ATTRIBUTION_BYTES)
        finally:
            os.close(descriptor)
    lines = head.decode("utf-8", "replace").split("\n")
    if not lines[0].startswith(_HEADING):
        return None
    rest = lines[1:]
    marked_id = None
    if rest:
        match = _ID_MARKER_RE.fullmatch(rest[0].strip())
        if match:
            marked_id = canonical_workstream_id(match.group(1))
            rest = rest[1:]
    code = None
    for line in rest:
        if not line.strip():
            continue
        # Only the line right after the heading: instructions further down
        # are user text.
        match = _SHORT_CODE_RE.match(line)
        if match:
            code = match.group(1).strip() or None
        break
    if marked_id is None and code is None:
        return None
    return marked_id, code


def write_workstream_claude_md(root_fd: int, name: str, content: str) -> None:
    """Create ``<name>/`` and atomically replace its ``CLAUDE.md``.

    Every step runs relative to descriptors: the directory is created with
    ``mkdir(dir_fd=)`` and opened without following a link (a link, file or
    other non-directory at ``name`` raises ``OSError``); the temporary file
    is created ``O_EXCL | O_NOFOLLOW``; ownership changes go through
    ``fchown`` on the open descriptors; ``rename`` replaces a planted link at
    ``CLAUDE.md`` instead of writing through it. A concurrent reader sees
    the old or the new complete file, never a partial one.
    """
    ensure_subdirectory(root_fd, name)
    with open_dir_nofollow(name, root_fd) as directory:
        fchown_to_agent(directory)
        atomic_replace_file(directory, CLAUDE_MD, content)


def remove_workstream_claude_md(root_fd: int, name: str) -> bool:
    """Remove ``<name>/CLAUDE.md`` when it is a regular file; True if removed.

    Relative to descriptors, never through a link: a directory, link or
    other entry at either name is left alone.
    """
    with open_dir_nofollow(name, root_fd) as directory:
        info = lstat_nofollow(CLAUDE_MD, directory)
        if info is None or not stat.S_ISREG(info.st_mode):
            return False
        os.unlink(CLAUDE_MD, dir_fd=directory)
    return True


def only_regenerable(parent_fd: int, name: str) -> bool:
    """True when the directory holds nothing but daemon-rewritten files."""
    try:
        with open_dir_nofollow(name, parent_fd) as directory:
            for child in os.listdir(directory):
                info = lstat_nofollow(child, directory)
                if (
                    info is None
                    or not stat.S_ISREG(info.st_mode)
                    or not _is_regenerable(child)
                ):
                    return False
        return True
    except OSError:
        return False


def _same_regular_file(name: str, source_fd: int, target_fd: int) -> bool:
    """Byte-identical regular files (bounded; links never followed)."""
    try:
        first = os.open(name, _READ_FLAGS, dir_fd=source_fd)
    except OSError:
        return False
    try:
        try:
            second = os.open(name, _READ_FLAGS, dir_fd=target_fd)
        except OSError:
            return False
        try:
            first_info, second_info = os.fstat(first), os.fstat(second)
            if not (
                stat.S_ISREG(first_info.st_mode)
                and stat.S_ISREG(second_info.st_mode)
                and first_info.st_size == second_info.st_size
                and first_info.st_size <= _COMPARE_MAX_BYTES
            ):
                return False
            remaining = first_info.st_size
            while remaining > 0:
                chunk = min(remaining, 1024 * 1024)
                if os.read(first, chunk) != os.read(second, chunk):
                    return False
                remaining -= chunk
            return True
        finally:
            os.close(second)
    finally:
        os.close(first)


def _merge_tree(source_fd: int, target_fd: int, depth: int = 0) -> None:
    """Move every entry of ``source`` that ``target`` lacks; recurse into
    shared real subdirectories; drop byte-identical duplicate files.
    Conflicting entries and links stay in ``source``.

    Only directories present on BOTH sides recurse (anything else moves in
    one rename), and never deeper than ``_MERGE_MAX_DEPTH``: the tree is
    agent-writable, and a planted deep nest must neither exhaust the stack
    nor hold one descriptor per level. A deeper shared subdirectory stays in
    ``source`` like any other conflict (it is archived, never deleted).
    """
    for name in sorted(os.listdir(source_fd)):
        info = lstat_nofollow(name, source_fd)
        if info is None or stat.S_ISLNK(info.st_mode):
            continue
        existing = lstat_nofollow(name, target_fd)
        if existing is None:
            os.rename(name, name, src_dir_fd=source_fd, dst_dir_fd=target_fd)
            continue
        if stat.S_ISDIR(info.st_mode) and stat.S_ISDIR(existing.st_mode):
            if depth >= _MERGE_MAX_DEPTH:
                continue
            try:
                with open_dir_nofollow(name, source_fd) as child, open_dir_nofollow(
                    name, target_fd
                ) as destination:
                    _merge_tree(child, destination, depth + 1)
            except OSError:
                continue
            try:
                os.rmdir(name, dir_fd=source_fd)
            except OSError:
                pass
        elif stat.S_ISREG(info.st_mode) and _same_regular_file(
            name, source_fd, target_fd
        ):
            os.unlink(name, dir_fd=source_fd)


def _unique_archive_name(archive_fd: int, label: str) -> str:
    if lstat_nofollow(label, archive_fd) is None:
        return label
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = 0
    while True:
        candidate = f"{label}-{stamp}" if suffix == 0 else f"{label}-{stamp}-{suffix}"
        if lstat_nofollow(candidate, archive_fd) is None:
            return candidate
        suffix += 1


def archive_directory(root_fd: int, name: str, label: str) -> str:
    """Move ``name`` under ``.archived/`` without overwriting anything.

    Returns the archive entry name. A ``.archived`` that is not a real
    directory (a planted symlink) raises instead of being followed.
    """
    try:
        os.mkdir(ARCHIVE_DIRNAME, dir_fd=root_fd)
    except FileExistsError:
        pass
    with open_dir_nofollow(ARCHIVE_DIRNAME, root_fd) as archive_fd:
        destination = _unique_archive_name(archive_fd, label.lstrip(".") or "orphan")
        os.rename(name, destination, src_dir_fd=root_fd, dst_dir_fd=archive_fd)
    return destination


def _remove_regenerable(root_fd: int, name: str) -> bool:
    """Unlink the regenerable files of ``name`` and remove it; False when
    anything else is there now (the caller archives instead).

    Only a regular file with a regenerable name is ever unlinked, each one
    re-checked at unlink time: an entry written after ``only_regenerable``
    looked stops the removal, and the final ``rmdir`` fails on a file
    created after this listing. Nothing but a regenerable file is deleted.
    """
    with open_dir_nofollow(name, root_fd) as directory:
        for child in os.listdir(directory):
            info = lstat_nofollow(child, directory)
            if info is None:
                continue
            if not stat.S_ISREG(info.st_mode) or not _is_regenerable(child):
                return False
            os.unlink(child, dir_fd=directory)
    try:
        os.rmdir(name, dir_fd=root_fd)
    except OSError:
        return False
    return True


def retire_directory(root_fd: int, name: str, label: str) -> bool:
    """Remove an emptied/regenerable directory or archive what remains.

    True when the directory was removed (its inode is freed and a new
    directory may reuse it), False when it was archived.
    """
    try:
        os.rmdir(name, dir_fd=root_fd)
        return True
    except OSError:
        pass
    if only_regenerable(root_fd, name) and _remove_regenerable(root_fd, name):
        logger.info("Removed orphan workstream directory: %s", label)
        return True
    destination = archive_directory(root_fd, name, label)
    logger.warning(
        "Archived workstream directory %s to .archived/%s (task outputs, "
        "specs, intake and learnings are never deleted).",
        label,
        destination,
    )
    return False


def directory_identity(name: str, dir_fd: int) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of the real directory ``name``, else None."""
    info = lstat_nofollow(name, dir_fd)
    if info is None or not stat.S_ISDIR(info.st_mode):
        return None
    return (info.st_dev, info.st_ino)


def _id_to_identity(raw: object) -> dict[str, tuple[int, int]]:
    """Validated ``{workstream id: (st_dev, st_ino)}`` from the map."""
    identities: dict[str, tuple[int, int]] = {}
    if not isinstance(raw, dict):
        return identities
    for key, value in raw.items():
        workstream_id = canonical_workstream_id(key)
        if (
            workstream_id
            and isinstance(value, list)
            and len(value) == 2
            and all(
                isinstance(part, int) and not isinstance(part, bool) for part in value
            )
        ):
            identities[workstream_id] = (value[0], value[1])
    return identities


def _aside_name(key: str) -> str:
    return f"{DELETED_PREFIX}{key}"


def _is_aside_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and name.startswith(DELETED_PREFIX)
        and canonical_workstream_id(name[len(DELETED_PREFIX) :])
        == name[len(DELETED_PREFIX) :]
    )


def _id_to_directory(raw: object, *, aside: bool = False) -> dict[str, str]:
    """Validated ``{workstream id: directory}`` entries of one map section
    (``aside``: also ``.deleted-<the same id>``)."""
    entries: dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            workstream_id = canonical_workstream_id(key)
            if workstream_id and (
                valid_directory_name(value)
                or (aside and value == _aside_name(workstream_id))
            ):
                entries[workstream_id] = value
    return entries


class WorkstreamDirectoryMap:
    """Persisted workstream-id -> directory map plus retired directory names.

    * ``current``: where each workstream's directory is (for a move still
      staged under ``.relocating-<id>``, the name it waits for).
    * ``staged``: for a move still staged, the directory it came from (the
      archive label; never a claim on that name).
    * ``kept_legacy``: the legacy directory a rolled-back backend writes a
      workstream's projections to while the workstream keeps the declared
      layout; kept until that directory is merged forward or archived.
    * ``shared_legacy``: kept legacy directories that also hold another
      workstream's content (one created there during the rollback, then
      renamed away or deleted); never merged into a single workstream,
      archived once the backend declares the layout again.
    * ``retired``: old names of renamed workstreams, merged forward when a
      late write recreates them.
    * ``declared``: the directory the backend last declared (``workspace_dir``)
      for each workstream; kept while a rolled-back backend declares none.
    * ``legacy``: each workstream's pre-``workstream_dirs_v1`` directory
      (``slugify(name)``), for the rollback helper.
    * ``deleted``: the directory of a deleted workstream that could not be
      archived yet. The name is FROZEN until it is: nothing moves it, merges
      into it or writes there, and no workstream is recorded at it. When a
      current workstream claims the name, the directory is moved aside to
      ``.deleted-<id>`` first (the claim follows it), so only a directory
      that cannot even be renamed keeps the name frozen.
    * ``deleted_identity``: the ``(st_dev, st_ino)`` of each such directory
      when the claim was recorded; a claim whose name now holds another
      directory is stale (the archive already happened) and is dropped.
    * ``unverified``: a legacy guess (``seeded``) that could not be checked
      because its CLAUDE.md is unreadable (at the workstream's own name, or
      the previous directory of a held move). Kept as a guess (loaded back
      into ``seeded``), never as a trusted entry, so the next sync checks it
      again instead of forgetting or trusting it.

    * ``pending``: the write-ahead record of a sync in progress. Before a
      sync moves, merges, archives or lays out a directory, it saves the map
      as it was at the start of the sync plus the planned step (``stage``,
      ``place``, ``retire`` or ``merge``: owner id, source, target and the
      source's identity). The final save clears it. A sync that finds one
      (the process died, or the final save failed) first applies the steps
      the disk shows completed (``reconcile_pending``), so a lost save never
      replays a move against a stale map.

    ``found`` is False when no readable map exists (first sync after an
    upgrade, or a damaged file); the caller then seeds the legacy layout.
    ``seeded`` names the workstreams whose ``current`` entry is that guess:
    it is checked against the directory's CLAUDE.md before it steers
    anything. It is saved only with a write-ahead record (the final save
    records where each workstream is), so an interrupted first sync does not
    turn a guess into a trusted entry.
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = Path(workspace)
        self.current: dict[str, str] = {}
        self.retired: dict[str, str] = {}
        self.declared: dict[str, str] = {}
        self.legacy: dict[str, str] = {}
        self.deleted: dict[str, str] = {}
        self.deleted_identity: dict[str, tuple[int, int]] = {}
        self.staged: dict[str, str] = {}
        self.kept_legacy: dict[str, str] = {}
        self.shared_legacy: set[str] = set()
        self.found = False
        self.seeded: set[str] = set()
        self.unverified: dict[str, str] = {}
        self.pending: list[dict] = []

    def load(self, filename: str = MAP_FILENAME) -> None:
        """Read the map (``filename`` in ``.cubicle/``: the rollback helper
        also reads a map an earlier run set aside)."""
        try:
            with open_existing_workspace_root(
                self._workspace
            ) as workspace_fd, open_dir_nofollow(MAP_DIRNAME, workspace_fd) as map_dir:
                descriptor = os.open(filename, _READ_FLAGS, dir_fd=map_dir)
                try:
                    info = os.fstat(descriptor)
                    if not stat.S_ISREG(info.st_mode) or info.st_size > _MAP_MAX_BYTES:
                        raise ValueError("not a bounded regular file")
                    raw = json.loads(os.read(descriptor, _MAP_MAX_BYTES + 1))
                finally:
                    os.close(descriptor)
        except FileNotFoundError:
            return
        except (OSError, ValueError, RecursionError) as exc:
            logger.warning(
                "Workstream directory map is unreadable (%s); the legacy "
                "directory layout is assumed for this sync.",
                exc,
            )
            return
        if not isinstance(raw, dict):
            return
        self.found = True
        self.current = _id_to_directory(raw.get("workstreams"))
        self.declared = _id_to_directory(raw.get("declared"))
        self.legacy = _id_to_directory(raw.get("legacy"))
        self.deleted = _id_to_directory(raw.get("deleted"), aside=True)
        self.deleted_identity = _id_to_identity(raw.get("deleted_identity"))
        self.staged = _id_to_directory(raw.get("staged"))
        self.kept_legacy = _id_to_directory(raw.get("kept_legacy"))
        shared = raw.get("shared_legacy")
        if isinstance(shared, list):
            self.shared_legacy = {name for name in shared if valid_directory_name(name)}
        retired = raw.get("retired")
        if isinstance(retired, dict):
            for key, value in retired.items():
                owner = canonical_workstream_id(value)
                if owner and valid_directory_name(key):
                    self.retired[key] = owner
        seeded = raw.get("seeded")
        if isinstance(seeded, list):
            self.seeded = {
                workstream_id
                for workstream_id in map(canonical_workstream_id, seeded)
                if workstream_id and workstream_id in self.current
            }
        # An unverified guess is a guess again (``finish`` re-records it
        # while it still cannot be checked).
        for workstream_id, name in _id_to_directory(raw.get("unverified")).items():
            if workstream_id not in self.current:
                self.current[workstream_id] = name
                self.seeded.add(workstream_id)
        pending = raw.get("pending")
        if isinstance(pending, list):
            self.pending = [
                step for step in map(_valid_step, pending) if step is not None
            ]

    def payload(self) -> dict:
        """The map as saved (no write-ahead record)."""
        payload = {
            "version": _MAP_VERSION,
            "workstreams": dict(sorted(self.current.items())),
            "retired": dict(sorted(self.retired.items())),
            "declared": dict(sorted(self.declared.items())),
            "legacy": dict(sorted(self.legacy.items())),
            "deleted": dict(sorted(self.deleted.items())),
            "deleted_identity": {
                gone_id: list(identity)
                for gone_id, identity in sorted(self.deleted_identity.items())
            },
            "staged": dict(sorted(self.staged.items())),
            "kept_legacy": dict(sorted(self.kept_legacy.items())),
            "shared_legacy": sorted(self.shared_legacy),
        }
        if self.unverified:
            payload["unverified"] = dict(sorted(self.unverified.items()))
        return payload

    def seed_legacy(self, workstreams: list[tuple[str, str]]) -> None:
        """Assume each workstream's previous directory is the legacy
        ``paths.slugify(name)`` layout (``(id, name)`` pairs)."""
        for workstream_id, name in workstreams:
            legacy = legacy_workstream_dir_slug(name)
            if valid_directory_name(legacy) and workstream_id not in self.current:
                self.current[workstream_id] = legacy
                self.seeded.add(workstream_id)

    def save(self, *, strict: bool = False) -> None:
        """Persist the map (clearing any write-ahead record); a failure is
        logged (``strict``: raised)."""
        self.write_payload(self.payload(), strict=strict)

    def write_payload(self, payload: dict, *, strict: bool = False) -> None:
        """Atomically replace the map file with ``payload``.

        The temporary file has a random name created ``O_EXCL |
        O_NOFOLLOW``, so nothing a session plants in ``.cubicle`` can make
        the write fail or redirect it.
        """
        data = json.dumps(payload, indent=2).encode() + b"\n"
        try:
            with open_existing_workspace_root(self._workspace) as workspace_fd:
                try:
                    os.mkdir(MAP_DIRNAME, 0o755, dir_fd=workspace_fd)
                except FileExistsError:
                    pass
                with open_dir_nofollow(MAP_DIRNAME, workspace_fd) as map_dir:
                    temporary = (
                        f".{MAP_FILENAME}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
                    )
                    descriptor = os.open(
                        temporary,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o644,
                        dir_fd=map_dir,
                    )
                    try:
                        try:
                            view = memoryview(data)
                            while view:
                                view = view[os.write(descriptor, view) :]
                            os.fsync(descriptor)
                        finally:
                            os.close(descriptor)
                        os.replace(
                            temporary,
                            MAP_FILENAME,
                            src_dir_fd=map_dir,
                            dst_dir_fd=map_dir,
                        )
                    except BaseException:
                        try:
                            os.unlink(temporary, dir_fd=map_dir)
                        except OSError:
                            pass
                        raise
        except OSError as exc:
            if strict:
                raise
            logger.warning("Could not persist the workstream directory map: %s", exc)


_STEP_OPS = frozenset(
    {"stage", "place", "alias", "retire", "merge", "aside", "removed"}
)


def _valid_name(name: object) -> bool:
    """A name a planned step may carry: a slug, or a staged move."""
    if not isinstance(name, str):
        return False
    if _is_aside_name(name):
        return True
    if name.startswith(RELOCATING_PREFIX):
        return canonical_workstream_id(name[len(RELOCATING_PREFIX) :]) is not None
    return valid_directory_name(name)


def _valid_step(raw: object) -> dict | None:
    """A validated write-ahead step read from the (agent-writable) map."""
    if not isinstance(raw, dict) or raw.get("op") not in _STEP_OPS:
        return None
    owner = raw.get("owner")
    if owner is not None:
        owner = canonical_workstream_id(owner)
        if owner is None:
            return None
    step: dict = {"op": raw["op"], "owner": owner}
    for key in ("source", "target"):
        value = raw.get(key)
        if value is not None and not _valid_name(value):
            return None
        step[key] = value
    identity = raw.get("identity")
    if identity is not None:
        if not (
            isinstance(identity, list)
            and len(identity) == 2
            and all(
                isinstance(part, int) and not isinstance(part, bool)
                for part in identity
            )
        ):
            return None
        identity = [identity[0], identity[1]]
    step["identity"] = identity
    op = step["op"]
    if op in ("stage", "place", "alias", "merge", "aside") and not owner:
        return None
    if op in ("stage", "retire", "merge", "aside") and not step["source"]:
        return None
    if op in ("place", "alias", "aside") and not step["target"]:
        return None
    if op in ("aside", "removed") and not identity:
        return None
    if op == "aside" and step["target"] != _aside_name(owner):
        return None
    return step


class WriteAheadJournal:
    """The write-ahead record of one sync (see ``WorkstreamDirectoryMap``).

    ``record`` saves the map as it was when the sync started plus every step
    planned so far, strictly, BEFORE the caller changes anything. When that
    save fails, nothing more is moved, merged, archived or laid out this
    sync (``ok`` turns False) and the failure is reported to ``failures``
    (an environmental error keeps worker admission closed; one a session
    can cause is logged); the next sync retries.
    """

    def __init__(
        self,
        dir_map: WorkstreamDirectoryMap,
        failures: MaterializationFailures | None = None,
    ) -> None:
        self._map = dir_map
        self._failures = failures
        base = dir_map.payload()
        if dir_map.seeded:
            base["seeded"] = sorted(dir_map.seeded)
        self._base = base
        self.steps: list[dict] = []
        self.ok = True

    def _fail(self, exc: OSError) -> None:
        self.ok = False
        what = "the workstream directory map (write-ahead record)"
        if self._failures is not None:
            self._failures.handle(exc, what)
        else:
            logger.warning("Could not save %s: %s", what, exc)
        logger.warning(
            "No workstream directory is moved, merged or archived for the rest "
            "of this sync (the map cannot record it first); retried on the "
            "next sync."
        )

    def persist_base(self) -> bool:
        """Save the reconciled map (no plan) so an interrupted sync's plan is
        never applied twice; False (and nothing moves) when that fails."""
        try:
            self._map.write_payload({**self._base, "pending": []}, strict=True)
        except OSError as exc:
            self._fail(exc)
        return self.ok

    def record(
        self,
        op: str,
        *,
        owner: str | None = None,
        source: str | None = None,
        target: str | None = None,
        identity: tuple[int, int] | None = None,
    ) -> bool:
        if not self.ok:
            return False
        step = {
            "op": op,
            "owner": owner,
            "source": source,
            "target": target,
            "identity": list(identity) if identity else None,
        }
        if step in self.steps:
            return True
        try:
            self._map.write_payload(
                {**self._base, "pending": [*self.steps, step]}, strict=True
            )
        except OSError as exc:
            self._fail(exc)
            return False
        self.steps.append(step)
        return True


def _record(
    journal: WriteAheadJournal | None,
    op: str,
    **fields: object,
) -> bool:
    """Record a planned step before making it; True when it may run (always
    without a journal: the unit-level entry points)."""
    return journal is None or journal.record(op, **fields)  # type: ignore[arg-type]


def _retire_recorded(
    root_fd: int, name: str, label: str, journal: WriteAheadJournal | None
) -> bool:
    """``retire_directory``, recording a removal (a freed inode) so that a
    directory created later at a planned step's source with the reused inode
    is never taken for the one the step recorded."""
    identity = directory_identity(name, root_fd)
    freed = retire_directory(root_fd, name, label)
    if freed and identity is not None and journal is not None:
        journal.record("removed", source=name, identity=identity)
    return freed


def _moved_away(name: str, identity: list[int] | None, root_fd: int) -> bool:
    """Whether the directory a step recorded is no longer at ``name``."""
    if identity is None:
        return not is_real_directory(root_fd, name)
    return directory_identity(name, root_fd) != tuple(identity)


def reconcile_pending(root_fd: int, dir_map: WorkstreamDirectoryMap) -> int:
    """Apply the write-ahead steps of an interrupted sync that the disk shows
    completed, then drop the record; returns how many were applied.

    Each step is checked against the actual source and target, so this is
    idempotent: a step that did not complete changes nothing (the sync plans
    it again from the map), and one that did is recorded as if that sync's
    final save had succeeded.
    """
    applied = 0
    # Inodes a step removed: a directory with one of them now is a new one.
    removed = {
        tuple(step["identity"]) for step in dir_map.pending if step["op"] == "removed"
    }

    def moved(step: dict) -> bool:
        identity = step["identity"]
        if identity is not None and tuple(identity) in removed:
            return True
        return _moved_away(step["source"], identity, root_fd)

    for step in dir_map.pending:
        op, owner = step["op"], step["owner"]
        source, target = step["source"], step["target"]
        if op == "stage" and moved(step):
            # The source is not the recorded directory any more.
            if is_real_directory(root_fd, f"{RELOCATING_PREFIX}{owner}"):
                dir_map.staged[owner] = source
            dir_map.retired[source] = owner
            applied += 1
        elif op == "place" and (
            moved(step) if source else is_real_directory(root_fd, target)
        ):
            dir_map.current[owner] = target
            dir_map.staged.pop(owner, None)
            dir_map.seeded.discard(owner)
            dir_map.retired.pop(target, None)
            applied += 1
        elif op == "alias" and is_real_directory(root_fd, target):
            # A held workstream's new name, laid out while its content
            # stays at the previous directory: its late writes merge forward.
            dir_map.retired[target] = owner
            applied += 1
        elif op == "retire" and moved(step):
            if owner:
                for section in (
                    dir_map.current,
                    dir_map.deleted,
                    dir_map.deleted_identity,
                    dir_map.staged,
                ):
                    section.pop(owner, None)
            dir_map.retired.pop(source, None)
            applied += 1
        elif op == "merge" and moved(step):
            # The late writes joined their owner. The name stays retired to
            # it while unclaimed (newer late writes there merge forward
            # too); ``finish`` drops the entry once a workstream takes it.
            applied += 1
        elif (
            op == "aside"
            and moved(step)
            and directory_identity(target, root_fd) == tuple(step["identity"])
        ):
            dir_map.deleted[owner] = target
            dir_map.deleted_identity[owner] = tuple(step["identity"])
            applied += 1
    if dir_map.pending:
        logger.warning(
            "An interrupted sync left %d planned workstream directory step(s); "
            "%d completed and are recorded now.",
            len(dir_map.pending),
            applied,
        )
    dir_map.pending = []
    return applied


def _relocate(
    root_fd: int,
    staged: str,
    slug: str,
    *,
    target_owned_by_other: bool,
    target_in_use: bool,
    label: str,
    journal: WriteAheadJournal | None = None,
) -> bool:
    """Place a staged directory at ``slug``; True once it is placed.

    A target that is not a real directory (a regular file or a link), or a
    directory still in use by another workstream whose own move could not be
    staged, is never merged into or archived over: the staged directory is
    KEPT and the next sync retries (False). A target that another current
    workstream claims as its own directory (a legacy duplicate) never
    receives this workstream's files: the staged copy is archived.
    """
    target = lstat_nofollow(slug, root_fd)
    if target is None:
        os.rename(staged, slug, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        logger.info("Moved renamed workstream directory %s -> %s", label, slug)
        return True
    if not stat.S_ISDIR(target.st_mode) or target_in_use:
        logger.warning(
            "Workstream directory %s cannot be placed at %s (%s); it stays "
            "staged as %s and the move is retried on the next sync.",
            label,
            slug,
            (
                "a directory still in use by another workstream"
                if stat.S_ISDIR(target.st_mode)
                else "not a directory"
            ),
            staged,
        )
        return False
    if target_owned_by_other:
        # Never mix one workstream's files into another workstream's
        # directory.
        _retire_recorded(root_fd, staged, label, journal)
        return True
    with open_dir_nofollow(staged, root_fd) as source, open_dir_nofollow(
        slug, root_fd
    ) as destination:
        _merge_tree(source, destination)
    _retire_recorded(root_fd, staged, label, journal)
    logger.info("Merged renamed workstream directory %s into %s", label, slug)
    return True


@dataclass
class ReconcileResult:
    """What one directory pass could not finish.

    * ``held``: workstream id -> previous directory whose move could not even
      be staged; it stays in place, out of the orphan sweep and in the map.
    * ``pending``: workstream id -> the directory a move still staged under
      ``.relocating-<id>`` came from (the map's ``staged`` section; the
      workstream itself is recorded at the name it waits for).
    * ``blocked``: names no staged directory, late write or CLAUDE.md write
      may use this pass.
    * ``deleted``: deleted workstream id -> its directory, for the claims
      still to be resolved (an archive that failed, or an unclaimed
      directory the orphan sweep handles); the map keeps each one while
      the directory remains.
    """

    held: dict[str, str] = field(default_factory=dict)
    pending: dict[str, str] = field(default_factory=dict)
    blocked: set[str] = field(default_factory=set)
    deleted: dict[str, str] = field(default_factory=dict)
    # ``(st_dev, st_ino)`` of each directory in ``deleted``.
    deleted_identity: dict[str, tuple[int, int]] = field(default_factory=dict)
    # Kept legacy directories another workstream's move was refused from:
    # their content is mixed (see ``WorkstreamDirectoryMap.shared_legacy``).
    shared_legacy: set[str] = field(default_factory=set)
    # Claimed names whose directory holds another workstream's content (its
    # CLAUDE.md names a deleted or a different workstream, or cannot be read)
    # and could not be archived: frozen like a deleted claim, retried on the
    # next sync (the CLAUDE.md there is not rewritten meanwhile).
    foreign: set[str] = field(default_factory=set)


_UNATTRIBUTED = "unattributed"
_UNREADABLE = "unreadable"
_GONE = "gone"
_WORKSTREAM = "workstream"


def _directory_owner(
    root_fd: int,
    name: str,
    current_ids: set[str],
    code_owners: dict[str, str | None] | None,
) -> tuple[str, str | None]:
    """Whose content ``<name>`` holds, by its daemon-written CLAUDE.md.

    ``(_WORKSTREAM, id)`` for a current workstream (its id marker, or a
    current workstream's short code); ``(_GONE, label)`` for an id or short
    code no current workstream has; ``(_UNATTRIBUTED, None)`` when there is
    no daemon-written CLAUDE.md (or only a short code and no codes were
    given); ``(_UNREADABLE, None)`` when it cannot be read.
    """
    try:
        attribution = directory_attribution(root_fd, name)
    except OSError as exc:
        logger.warning("Cannot read %s/%s: %s", name, CLAUDE_MD, exc)
        return _UNREADABLE, None
    if attribution is None:
        return _UNATTRIBUTED, None
    marked_id, code = attribution
    if marked_id is not None:
        if marked_id in current_ids:
            return _WORKSTREAM, marked_id
        return _GONE, f"workstream {marked_id}"
    if code_owners is None or code is None:
        return _UNATTRIBUTED, None
    if code not in code_owners:
        return _GONE, f"short code {code}"
    owner = code_owners[code]
    if owner is None:
        return _UNATTRIBUTED, None
    return _WORKSTREAM, owner


def _foreign_reason(
    root_fd: int,
    name: str,
    workstream_id: str,
    current_ids: set[str],
    code_owners: dict[str, str | None] | None,
    dir_map: WorkstreamDirectoryMap,
) -> tuple[str, bool] | None:
    """Why ``workstream_id`` must not take the existing directory ``name``,
    and whether it may be archived first; None when it may take it.

    Its CLAUDE.md can only VETO: one naming a deleted workstream (archived
    first, it is nobody's), another current workstream or unreadable (frozen)
    stops the adoption. A directory with no daemon-written CLAUDE.md (a
    projection the backend wrote first) may be taken, as may one the map
    places the named current workstream at, or keeps as its legacy directory
    during a backend rollback: the rename and shared-legacy rules decide those
    (a shared ``office`` directory stays with whoever keeps the name).

    Another current workstream's directory is never adopted. It is archived
    first only when the map records it for nobody and its id marker names
    that workstream: a stale copy (an operator restored an older map), which
    the orphan sweep archives too when no workstream claims the name. The map
    authorizes that archive; waiting would keep the name, and the claimant's
    backend writes, on it for ever. One the map still records elsewhere
    (another workstream's entry, a kept legacy directory), or attributed only
    by an older daemon's short code, waits this pass.
    """
    # Late writes of a workstream deleted since are a ``deleted`` claim by
    # now (``_claim_late_writes_of_deleted``), never a retired entry.
    kind, owner = _directory_owner(root_fd, name, current_ids, code_owners)
    if kind == _UNATTRIBUTED or (kind == _WORKSTREAM and owner == workstream_id):
        return None
    if kind == _UNREADABLE:
        return "its CLAUDE.md cannot be read", False
    if kind == _GONE:
        return f"its CLAUDE.md names {owner}, which no longer exists", True
    if name in (dir_map.current.get(owner or ""), dir_map.kept_legacy.get(owner or "")):
        return None
    # A stale layout that holds nothing but its CLAUDE.md (regenerated
    # wherever that workstream is) displaces no content: it is removed
    # (``retire_directory`` archives it after all if a file appears).
    recorded = name in dir_map.kept_legacy.values() or any(
        other != workstream_id and recorded_name == name
        for other, recorded_name in dir_map.current.items()
    )
    marked_by_id = _directory_owner(root_fd, name, current_ids, None)[0] == (
        _WORKSTREAM
    )
    return (
        f"its CLAUDE.md names workstream {owner}",
        (marked_by_id and not recorded) or only_regenerable(root_fd, name),
    )


def _deleted_claims(
    dir_map: WorkstreamDirectoryMap, current_ids: set[str]
) -> dict[str, str]:
    """Directories the map still assigns to workstreams that no longer exist:
    ``current`` entries of ids the backend no longer sends, plus the
    ``deleted`` claims kept from earlier syncs."""
    claims = {
        gone_id: name
        for gone_id, name in dir_map.deleted.items()
        if gone_id not in current_ids
    }
    # A deleted workstream whose move was still staged occupies no name (the
    # orphan sweep archives its ``.relocating-<id>``); its recorded name is
    # only the target it waited for.
    claims.update(
        (gone_id, name)
        for gone_id, name in dir_map.current.items()
        if gone_id not in current_ids and gone_id not in dir_map.staged
    )
    return claims


def _set_aside(
    root_fd: int,
    name: str,
    key: str,
    identity: tuple[int, int],
    journal: WriteAheadJournal | None,
) -> str | None:
    """Move a deleted workstream's directory whose archive failed to
    ``.deleted-<key>`` (inside ``workstreams/``, so it does not depend on
    ``.archived``), freeing ``name`` for the workstream that claims it: its
    CLAUDE.md, backend projections and worker outputs then land in a
    directory of its own. None when it cannot be moved (the name stays
    frozen)."""
    aside = _aside_name(key)
    if name == aside or lstat_nofollow(aside, root_fd) is not None:
        return None
    if not _record(
        journal, "aside", owner=key, source=name, target=aside, identity=identity
    ):
        return None
    try:
        os.rename(name, aside, src_dir_fd=root_fd, dst_dir_fd=root_fd)
    except OSError as exc:
        logger.warning("Could not move %s aside to %s: %s", name, aside, exc)
        return None
    logger.warning(
        "Moved %s aside to %s (it could not be archived); the workstream that "
        "claims the name starts in a directory of its own, and the archive is "
        "retried on the next sync.",
        name,
        aside,
    )
    return aside


def _retire_deleted_workstream_directories(
    root_fd: int,
    owners: dict[str, set[str]],
    current_ids: set[str],
    dir_map: WorkstreamDirectoryMap,
    result: ReconcileResult,
    journal: WriteAheadJournal | None = None,
) -> None:
    """Archive a deleted workstream's directory before another takes its name.

    When the map assigns a directory to a workstream that no longer exists
    and a current workstream now claims that name, the directory holds the
    DELETED workstream's spec, intake and outputs; merging or writing the
    new owner into it would hand that content to the wrong workstream. It is
    archived (or removed when it holds only ``CLAUDE.md``) first.

    A claimant without a map entry is a NEW workstream (created after the
    deleted one, possibly with the same name): it never inherits the
    deleted workstream's directory either. Deleted ids only come from this
    daemon's own map (a seeded map records current workstreams only).

    Left alone: a directory a current workstream is recorded at (a shared
    legacy directory: whoever keeps the name keeps it). A name that could
    not be archived goes into ``result.blocked`` (nothing may use it this
    pass) and its claim into ``result.deleted``, so the map keeps it and
    the next sync retries the archive before anything is merged into it.
    An unclaimed directory of a deleted workstream is kept in
    ``result.deleted`` too: the orphan sweep archives it, and the claim
    survives if that fails.

    The shared-directory exception applies only to a claim read from the
    map's ``current`` section. A ``deleted`` claim was kept because no
    current workstream was recorded at the name, and ``finish`` never
    records a waiting claimant there: the claim is kept until the archive
    succeeds (a current record at the name can only come from a map written
    before that rule).
    """
    for gone_id, name in sorted(_deleted_claims(dir_map, current_ids).items()):
        identity = directory_identity(name, root_fd)
        if identity is None:
            continue
        recorded_here = any(
            dir_map.current.get(other) == name and not _move_staged(other, root_fd)
            for other in current_ids
        )
        if gone_id not in dir_map.deleted:
            if recorded_here:
                continue
        elif gone_id in dir_map.deleted_identity:
            if dir_map.deleted_identity[gone_id] != identity:
                # The directory was archived and the name reused (a sync or
                # rollback whose map update was lost): not the deleted
                # workstream's any more.
                logger.warning(
                    "The claim of deleted workstream %s on %s is stale (another "
                    "directory is there now); it is dropped, nothing archived.",
                    gone_id,
                    name,
                )
                continue
        elif recorded_here:
            # A map from before identities were recorded: a workstream that
            # is recorded there (not a staged move) may own it by now.
            logger.warning(
                "The claim of deleted workstream %s on %s has no recorded "
                "identity and a workstream is recorded there; it is dropped.",
                gone_id,
                name,
            )
            continue
        # A marker can only veto (R4-STALE-CURRENT-CLAIM,
        # R4-IDENTITY-INODE-REUSE): a directory whose CLAUDE.md id marker
        # names a CURRENT workstream is that workstream's by now (the map
        # update after an archive was lost, or a reused inode), so the claim
        # is stale. Only the id marker counts: a short code may have been
        # reused by the new workstream.
        kind, marked = _directory_owner(root_fd, name, current_ids, None)
        if kind == _WORKSTREAM:
            logger.warning(
                "The claim of deleted workstream %s on %s is stale (its "
                "CLAUDE.md names current workstream %s); it is dropped, "
                "nothing archived.",
                gone_id,
                name,
                marked,
            )
            continue
        # A directory already moved aside is archived without a claimant.
        claimed = bool(owners.get(name)) or _is_aside_name(name)
        if (
            not claimed
            or kind == _UNREADABLE
            or not _record(
                journal, "retire", owner=gone_id, source=name, identity=identity
            )
        ):
            if kind == _UNREADABLE or claimed:
                result.blocked.add(name)
            result.deleted[gone_id] = name
            result.deleted_identity[gone_id] = identity
            continue
        try:
            if _retire_recorded(root_fd, name, name, journal):
                # The inode is free and a directory created now may reuse it:
                # nothing takes the name before the map records the claim
                # resolved (a claimant gets its directory next sync).
                result.blocked.add(name)
        except OSError:
            logger.exception(
                "Could not archive the directory %s of deleted workstream %s.",
                name,
                gone_id,
            )
            aside = None
            if not _is_aside_name(name):
                aside = _set_aside(root_fd, name, gone_id, identity, journal)
            if aside is None:
                # Frozen: the workstream now named for it waits (retried).
                result.blocked.add(name)
            result.deleted[gone_id] = aside or name
            result.deleted_identity[gone_id] = identity


def _drop_foreign_seeded_entries(
    root_fd: int,
    slugs: dict[str, str],
    current_ids: set[str],
    code_owners: dict[str, str | None] | None,
    dir_map: WorkstreamDirectoryMap,
) -> None:
    """Without a map, each workstream's previous directory is only a guess
    (its legacy name). A guess whose CLAUDE.md names a deleted or another
    workstream (or cannot be read) is dropped: that directory is not moved
    into this workstream's."""
    dropped: list[tuple[str, str, str]] = []
    for workstream_id in sorted(dir_map.seeded & current_ids):
        previous = dir_map.current.get(workstream_id)
        if (
            not previous
            or previous == slugs.get(workstream_id)
            or not is_real_directory(root_fd, previous)
        ):
            continue
        verdict = _foreign_reason(
            root_fd, previous, workstream_id, current_ids, code_owners, dir_map
        )
        if verdict and _directory_owner(root_fd, previous, current_ids, None)[0] != (
            _UNREADABLE
        ):
            # An unreadable CLAUDE.md keeps the guess: the move is held
            # (phase 1 re-reads it) until it can be read.
            dropped.append((workstream_id, previous, verdict[0]))
    for workstream_id, previous, reason in dropped:
        logger.warning(
            "Workstream %s: the legacy directory %s is not taken as its "
            "previous directory (%s).",
            workstream_id,
            previous,
            reason,
        )
        dir_map.current.pop(workstream_id, None)
        dir_map.seeded.discard(workstream_id)


def _merge_retired_forward(
    root_fd: int,
    slug: str,
    workstream_id: str,
    slugs: dict[str, str],
    current_ids: set[str],
    code_owners: dict[str, str | None] | None,
    dir_map: WorkstreamDirectoryMap,
    result: ReconcileResult,
    journal: WriteAheadJournal | None = None,
) -> bool:
    """A late write that recreated a renamed workstream's old name (the map
    retires the name to it) is that workstream's: merge it forward before
    another takes the name (R4-RETIRED-LATE-WRITE-ADOPTED). True when the
    name was handled (merged away, or frozen because the merge cannot run
    now)."""
    owner = dir_map.retired.get(slug)
    if not owner or owner == workstream_id or owner not in current_ids:
        return False
    if slug in dir_map.shared_legacy or slug in set(dir_map.kept_legacy.values()):
        return False  # a rolled-back backend's legacy directory: its rules decide
    kind, marked = _directory_owner(root_fd, slug, current_ids, code_owners)
    if kind == _UNREADABLE:
        result.blocked.add(slug)
        result.foreign.add(slug)
        return True
    if kind == _GONE or (kind == _WORKSTREAM and marked != owner):
        # Its CLAUDE.md vetoes the merge (the retired entry is stale); the
        # adoption check decides.
        return False
    target = slugs.get(owner)
    if _move_staged(owner, root_fd):
        # The owner's own directory is being moved this sync: the late
        # writes join it before it is placed.
        target = f"{RELOCATING_PREFIX}{owner}"
    try:
        if not target or target in result.blocked:
            raise OSError(errno.EBUSY, f"{target} is not ready")
        if dir_map.retired.get(target) not in (None, owner):
            # Another workstream's late writes are there: merging would mix
            # them (they are merged forward first, retried next sync).
            raise OSError(errno.EBUSY, f"{target} holds another's late writes")
        info = lstat_nofollow(target, root_fd)
        if info is not None and not stat.S_ISDIR(info.st_mode):
            raise OSError(errno.ENOTDIR, f"{target} is not a directory")
        if not _record(
            journal,
            "merge",
            owner=owner,
            source=slug,
            target=target,
            identity=directory_identity(slug, root_fd),
        ):
            raise OSError(errno.EAGAIN, "the map cannot record the merge first")
        if info is None:
            os.rename(slug, target, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        else:
            with open_dir_nofollow(slug, root_fd) as source, open_dir_nofollow(
                target, root_fd
            ) as destination:
                _merge_tree(source, destination)
            _retire_recorded(root_fd, slug, slug, journal)
    except OSError as exc:
        logger.warning(
            "Workstream %s does not take %s yet: it holds workstream %s's late "
            "writes, which could not be merged forward (%s); retried on the "
            "next sync.",
            workstream_id,
            slug,
            owner,
            exc,
        )
        result.blocked.add(slug)
        result.foreign.add(slug)
        return True
    dir_map.retired.pop(slug, None)
    logger.info(
        "Merged late writes in %s forward to workstream %s before workstream "
        "%s takes the name.",
        slug,
        owner,
        workstream_id,
    )
    return True


# Namespace of the keys new ``deleted`` claims get when no deleted
# workstream's id names the directory (a fixed value: the same state always
# yields the same keys, and so the same order of claims).
_CLAIM_KEY_NAMESPACE = uuid.UUID("5d1c6f2a-9b3e-4f57-8a0c-2e6b1d7f4c90")


def _new_claim_key(name: str, taken: set[str] | dict[str, str]) -> str:
    """A ``deleted`` claim key for ``name`` that no claim or workstream
    uses: derived from the name, so it never depends on chance."""
    attempt = 0
    while True:
        key = str(uuid.uuid5(_CLAIM_KEY_NAMESPACE, f"{name}/{attempt}"))
        if key not in taken:
            return key
        attempt += 1


def _set_foreign_aside(
    root_fd: int,
    name: str,
    current_ids: set[str],
    dir_map: WorkstreamDirectoryMap,
    result: ReconcileResult,
    journal: WriteAheadJournal | None,
) -> bool:
    """Move a directory whose CLAUDE.md names a deleted workstream, and whose
    archive failed, aside (``_set_aside``) under a new ``deleted`` claim: the
    id its marker names, or a fresh one for a pre-marker CLAUDE.md. True when
    it was moved."""
    identity = directory_identity(name, root_fd)
    try:
        attribution = directory_attribution(root_fd, name)
    except OSError:
        return False
    marked = attribution[0] if attribution else None
    key = marked or _new_claim_key(
        name, set(current_ids) | set(dir_map.deleted) | set(result.deleted)
    )
    if (
        identity is None
        or key in current_ids
        or key in dir_map.deleted
        or key in result.deleted
    ):
        return False
    aside = _set_aside(root_fd, name, key, identity, journal)
    if aside is None:
        return False
    result.deleted[key] = aside
    result.deleted_identity[key] = identity
    return True


def _freeze_foreign_claims(
    root_fd: int,
    entries: list[tuple[str, str]],
    current_ids: set[str],
    code_owners: dict[str, str | None] | None,
    dir_map: WorkstreamDirectoryMap,
    result: ReconcileResult,
    journal: WriteAheadJournal | None = None,
) -> None:
    """Adoption check: a workstream takes an existing directory at its name
    that the map does not record for it only when that directory is not
    another's. A renamed workstream's late writes there are merged forward
    first (``_merge_retired_forward``); then ``_foreign_reason`` decides: a
    deleted workstream's directory is archived first; another current
    workstream's, or one whose CLAUDE.md cannot be read, freezes the name
    for this pass (``result.foreign``), retried on the next sync. A failed
    archive freezes it too."""
    slugs = dict(entries)
    for workstream_id, slug in entries:
        if slug in result.blocked or slug in result.foreign:
            continue
        if not is_real_directory(root_fd, slug):
            continue
        if (
            dir_map.current.get(workstream_id) == slug
            and workstream_id not in dir_map.seeded
            and workstream_id not in dir_map.staged
        ):
            # The map records it here; the directory's CLAUDE.md can still
            # veto a stale entry (an operator's restored map): an id marker
            # naming a deleted workstream makes it that workstream's
            # directory, archived first below. Another current workstream's
            # marker does not veto it (that workstream's own entry decides
            # where it is; freezing here would wait forever).
            kind, marked = _directory_owner(root_fd, slug, current_ids, None)
            if kind != _GONE:
                continue
            logger.warning(
                "Workstream %s: its recorded directory %s is not adopted (its "
                "CLAUDE.md names %s; the map entry is stale).",
                workstream_id,
                slug,
                marked,
            )
        if _merge_retired_forward(
            root_fd,
            slug,
            workstream_id,
            slugs,
            current_ids,
            code_owners,
            dir_map,
            result,
            journal,
        ) or not is_real_directory(root_fd, slug):
            continue
        verdict = _foreign_reason(
            root_fd, slug, workstream_id, current_ids, code_owners, dir_map
        )
        if verdict is None:
            continue
        reason, archivable = verdict
        if archivable and not _record(
            journal,
            "retire",
            source=slug,
            identity=directory_identity(slug, root_fd),
        ):
            archivable = False
            reason += "; the map cannot record the archive first"
        if archivable:
            try:
                if _retire_recorded(root_fd, slug, slug, journal):
                    result.blocked.add(slug)  # an inode is free: next sync
            except OSError:
                logger.exception("Could not archive %s (%s).", slug, reason)
                if _set_foreign_aside(
                    root_fd, slug, current_ids, dir_map, result, journal
                ):
                    continue
                logger.warning(
                    "Workstream %s waits for %s; it is retried on the next sync.",
                    workstream_id,
                    slug,
                )
            else:
                logger.warning(
                    "Archived %s before workstream %s takes the name (%s).",
                    slug,
                    workstream_id,
                    reason,
                )
                continue
        else:
            logger.warning(
                "Workstream %s does not take %s (%s); it is retried on the "
                "next sync.",
                workstream_id,
                slug,
                reason,
            )
        result.blocked.add(slug)
        result.foreign.add(slug)


def reconcile_workstream_directories(
    root_fd: int,
    entries: list[tuple[str, str]],
    dir_map: WorkstreamDirectoryMap,
    short_codes: dict[str, str] | None = None,
    journal: WriteAheadJournal | None = None,
) -> ReconcileResult:
    """Move directories of renamed workstreams (``entries`` = (id, slug)).

    Phase 1 stages every renamed directory under ``.relocating-<id>``; phase
    2 places each staged directory at its new name. A crash between the
    phases leaves the staged name, which the next sync picks up again.
    Between the phases an existing directory the map does not record for its
    claimant is checked against its CLAUDE.md (``_freeze_foreign_claims``);
    ``short_codes`` (id -> short code) resolves a CLAUDE.md written before
    it carried the workstream id. See ``ReconcileResult`` for what the
    caller keeps for the next sync.

    With a ``journal`` every move, merge and archive is recorded before it is
    made; once a record cannot be saved nothing more is changed (see
    ``WriteAheadJournal``): the moves not made are held, staged or frozen
    exactly as if they had failed, and retried on the next sync.
    """
    owners: dict[str, set[str]] = {}
    for workstream_id, slug in entries:
        owners.setdefault(slug, set()).add(workstream_id)
    current_ids = {workstream_id for workstream_id, _slug in entries}
    code_owners: dict[str, str | None] | None = None
    if short_codes is not None:
        code_owners = {}
        for workstream_id, code in sorted(short_codes.items()):
            if workstream_id not in current_ids or not code:
                continue
            # A code several current workstreams share names none of them.
            shared = code_owners.get(code, workstream_id) != workstream_id
            code_owners[code] = None if shared else workstream_id
    result = ReconcileResult()
    _drop_foreign_seeded_entries(
        root_fd, dict(entries), current_ids, code_owners, dir_map
    )
    _retire_deleted_workstream_directories(
        root_fd, owners, current_ids, dir_map, result, journal
    )
    # Invariant F: a deleted workstream's directory that is not archived yet
    # is FROZEN. Nothing moves it, merges into it or writes a CLAUDE.md into
    # it, and it is nobody's previous directory, until the archive succeeds.
    frozen = set(result.deleted.values())
    result.blocked |= frozen
    for workstream_id, slug in entries:
        previous = dir_map.current.get(workstream_id)
        staged = f"{RELOCATING_PREFIX}{workstream_id}"
        if (
            not previous
            or previous == slug
            or lstat_nofollow(staged, root_fd) is not None
            or not is_real_directory(root_fd, previous)
        ):
            continue
        if previous in frozen:
            logger.warning(
                "Workstream %s: %s still holds a deleted workstream's content; "
                "it is not moved (the workstream starts at its new name).",
                workstream_id,
                previous,
            )
            continue
        # A legacy directory SHARED by several current workstreams
        # (duplicate names, or non-Latin names in the old ``office``
        # directory) holds mixed content that cannot be split: it moves with
        # none of them. Whoever keeps the name keeps it; otherwise the
        # orphan sweep archives it.
        # A workstream whose own move is staged occupies no name: it is
        # recorded at the target it waits for.
        if any(
            other != workstream_id
            and dir_map.current.get(other) == previous
            and not _move_staged(other, root_fd)
            for other in current_ids
        ):
            continue
        # Likewise the legacy directory a rolled-back backend wrote another
        # workstream's projections to (that workstream kept the declared
        # layout): its content is mixed, so it moves with nobody.
        if any(
            other != workstream_id and legacy == previous
            for other, legacy in dir_map.kept_legacy.items()
        ):
            result.shared_legacy.add(previous)
            logger.warning(
                "Workstream %s: its directory %s also holds another "
                "workstream's projections from a backend rollback; it is not "
                "moved (mixed content cannot be split).",
                workstream_id,
                previous,
            )
            continue
        # The map authorizes the move; the directory's CLAUDE.md can only
        # veto it. A stale map entry (a lost map update replaying a swap or
        # chain, R4-STALE-CURRENT-SWAP, or an older daemon's era) never
        # moves a directory whose CLAUDE.md names another current or a
        # deleted workstream; an unreadable one keeps it in place.
        kind, marked = _directory_owner(root_fd, previous, current_ids, code_owners)
        if kind == _UNREADABLE:
            result.held[workstream_id] = previous
            continue
        if kind == _GONE or (kind == _WORKSTREAM and marked != workstream_id):
            logger.warning(
                "Workstream %s: its recorded directory %s is not moved (its "
                "CLAUDE.md names %s; the map entry is stale).",
                workstream_id,
                previous,
                marked,
            )
            continue
        if not _record(
            journal,
            "stage",
            owner=workstream_id,
            source=previous,
            identity=directory_identity(previous, root_fd),
        ):
            result.held[workstream_id] = previous
            continue
        try:
            os.rename(previous, staged, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        except OSError:
            logger.exception(
                "Could not stage renamed workstream directory %s; it is kept "
                "and retried on the next sync.",
                previous,
            )
            result.held[workstream_id] = previous
            continue
        dir_map.retired[previous] = workstream_id
        dir_map.staged[workstream_id] = previous
    # A directory whose own move failed to stage is still in use by its
    # workstream: another workstream renamed onto that name waits (a chain
    # rename must not merge into it). A held workstream has no staged copy
    # of its own, so this never blocks the held workstream itself.
    result.blocked |= set(result.held.values())
    _freeze_foreign_claims(
        root_fd, entries, current_ids, code_owners, dir_map, result, journal
    )
    for workstream_id, slug in entries:
        staged = f"{RELOCATING_PREFIX}{workstream_id}"
        if not is_real_directory(root_fd, staged):
            continue
        placed = False
        target = lstat_nofollow(slug, root_fd)
        # A placement ``_relocate`` refuses anyway (the target is in use or
        # not a directory) changes nothing, so it needs no record.
        can_place = slug not in result.blocked and (
            target is None or stat.S_ISDIR(target.st_mode)
        )
        try:
            if can_place and not _record(
                journal,
                "place",
                owner=workstream_id,
                source=staged,
                target=slug,
                identity=directory_identity(staged, root_fd),
            ):
                raise OSError(errno.EAGAIN, "the map cannot record the move first")
            placed = _relocate(
                root_fd,
                staged,
                slug,
                target_owned_by_other=bool(owners.get(slug, set()) - {workstream_id}),
                target_in_use=slug in result.blocked,
                label=dir_map.staged.get(workstream_id)
                or dir_map.current.get(workstream_id)
                or staged,
                journal=journal,
            )
        except OSError:
            logger.exception(
                "Could not move the renamed workstream directory into %s; the "
                "staged copy %s is kept and retried on the next sync.",
                slug,
                staged,
            )
        if not placed and is_real_directory(root_fd, staged):
            result.pending[workstream_id] = (
                dir_map.staged.get(workstream_id)
                or dir_map.current.get(workstream_id)
                or slug
            )
    return result


def sweep_orphan_directories(
    root_fd: int,
    claimed: set[str],
    current_ids: dict[str, str],
    dir_map: WorkstreamDirectoryMap,
    blocked: set[str] | frozenset[str] = frozenset(),
    journal: WriteAheadJournal | None = None,
) -> dict[str, tuple[tuple[int, int], str | None]]:
    """Handle directories no current workstream claims.

    A retired name of a current workstream (a late write after its rename)
    is merged forward; everything else is deleted only when it holds nothing
    but ``CLAUDE.md`` and is archived otherwise. Links are left alone. A
    late write whose target is ``blocked`` (a directory still in use by
    another workstream) is kept for the next sync. With a ``journal`` each
    merge and archive is recorded first; once a record fails nothing more is
    swept this pass.

    Returns the directories it meant to archive as nobody's but could not,
    with their identities and the deleted workstream their CLAUDE.md names
    (if any), so the caller keeps a claim on each: a workstream that takes
    the name later must not adopt their content.
    """
    unarchived: dict[str, tuple[tuple[int, int], str | None]] = {}
    # The deleted workstream a swept directory belonged to: its claim is
    # recorded with the archive, so a sync that stops before its save never
    # revives the claim against a directory created at the name since.
    gone_owner = {
        name: gone_id
        for gone_id, name in _deleted_claims(dir_map, set(current_ids)).items()
    }
    for name in sorted(os.listdir(root_fd)):
        if (
            name in claimed
            or name == ARCHIVE_DIRNAME
            or not is_real_directory(root_fd, name)
        ):
            continue
        if _is_aside_name(name):
            # A deleted workstream's directory moved aside whose claim the
            # map lost: archived unless its CLAUDE.md names a current
            # workstream (a marker vetoes; it is left for the operator).
            kind, marked = _directory_owner(root_fd, name, set(current_ids), None)
            if kind in (_WORKSTREAM, _UNREADABLE) or not _record(
                journal,
                "retire",
                source=name,
                identity=directory_identity(name, root_fd),
            ):
                continue
            try:
                _retire_recorded(root_fd, name, name, journal)
            except OSError:
                logger.exception("Could not archive %s; it is kept.", name)
            continue
        if name.startswith(RELOCATING_PREFIX):
            # Staged by a workstream that no longer exists.
            if name[len(RELOCATING_PREFIX) :] not in current_ids:
                if not _record(
                    journal,
                    "retire",
                    source=name,
                    identity=directory_identity(name, root_fd),
                ):
                    continue
                try:
                    _retire_recorded(root_fd, name, name, journal)
                except OSError:
                    logger.exception("Could not archive %s; it is kept.", name)
            continue
        if name.startswith("."):
            continue
        if _directory_owner(root_fd, name, set(current_ids), None)[0] == _UNREADABLE:
            # Whose content it is cannot be told: it is left until its
            # CLAUDE.md can be read (retried on the next sync).
            continue
        try:
            owner = dir_map.retired.get(name)
            target_slug = current_ids.get(owner or "")
            if target_slug:
                # The map authorizes the merge forward; the directory's
                # CLAUDE.md can veto it (a stale retired entry after a lost
                # map update: the name now holds another workstream's
                # content). A vetoed or unreadable one is archived instead.
                kind, marked = _directory_owner(root_fd, name, set(current_ids), None)
                if kind in (_GONE, _UNREADABLE) or (
                    kind == _WORKSTREAM and marked != owner
                ):
                    logger.warning(
                        "%s is not merged into workstream %s (its CLAUDE.md "
                        "names %s); it is archived.",
                        name,
                        owner,
                        marked or "an unreadable owner",
                    )
                    target_slug = None
            identity = directory_identity(name, root_fd)
            if target_slug:
                target = lstat_nofollow(target_slug, root_fd)
                if target_slug in blocked or (
                    target is not None and not stat.S_ISDIR(target.st_mode)
                ):
                    # The target is in use, or its name holds a link or a
                    # file (a session's): the late writes wait, never
                    # archived for it (retried on the next sync).
                    continue
                if not _record(
                    journal,
                    "merge",
                    owner=owner,
                    source=name,
                    target=target_slug,
                    identity=identity,
                ):
                    continue
                target = lstat_nofollow(target_slug, root_fd)
                if target is not None and not stat.S_ISDIR(target.st_mode):
                    continue
                if target is None:
                    os.rename(name, target_slug, src_dir_fd=root_fd, dst_dir_fd=root_fd)
                    continue
                if stat.S_ISDIR(target.st_mode):
                    with open_dir_nofollow(name, root_fd) as source, open_dir_nofollow(
                        target_slug, root_fd
                    ) as destination:
                        _merge_tree(source, destination)
            elif not _record(
                journal,
                "retire",
                owner=gone_owner.get(name),
                source=name,
                identity=identity,
            ):
                continue
            _retire_recorded(root_fd, name, name, journal)
        except OSError:
            logger.exception(
                "Could not clean up orphan workstream directory %s; it is kept.",
                name,
            )
            identity = directory_identity(name, root_fd)
            if not target_slug and name not in gone_owner and identity is not None:
                try:
                    attribution = directory_attribution(root_fd, name)
                except OSError:
                    attribution = None
                marked = attribution[0] if attribution else None
                unarchived[name] = (
                    identity,
                    marked if marked and marked not in current_ids else None,
                )
    return unarchived


class WorkstreamLayout:
    """One sync pass over ``/workspace/workstreams``.

    ``load`` reads the map; ``directory_for`` names each workstream's
    directory; ``prepare`` completes an interrupted sync's plan
    (``reconcile_pending``) and moves renamed directories; the caller then
    writes every CLAUDE.md (``write_workstream_claude_md``), calling
    ``record_placement`` first; ``finish`` sweeps orphans and saves the map.
    ``prepare`` and ``finish`` run relative to a descriptor of the
    workstreams directory (``open_workstreams_root``), opened without
    following links.

    Every move, merge, archive and new placement is recorded in the map
    before it is made (``WriteAheadJournal``). A record or final save that
    fails is reported to ``failures`` (an environmental error keeps worker
    admission closed; the caller raises it) and nothing more is moved.
    """

    def __init__(
        self, workspace: Path, failures: MaterializationFailures | None = None
    ) -> None:
        self._workspace = Path(workspace)
        self._failures = failures
        self._map = WorkstreamDirectoryMap(self._workspace)
        self._journal: WriteAheadJournal | None = None
        self._identified: list[tuple[str, str]] = []
        self._result = ReconcileResult()
        self._declared_now: dict[str, str] = {}
        self._legacy_now: dict[str, str] = {}
        # Workstream id -> the legacy directory a backend that declares no
        # ``workspace_dir`` writes projections to, while this daemon keeps
        # the workstream in the layout that backend's successor declared.
        self._kept_legacy: dict[str, str] = {}
        # Legacy directories several kept workstreams resolve to this pass.
        self._shared_kept_targets: set[str] = set()

    def load(self) -> None:
        self._map.load()

    @property
    def blocked(self) -> set[str]:
        """Names nothing may use this pass (after ``prepare``): the directory
        of a deleted workstream that could not be archived, one whose own
        move could not be staged, or one holding another workstream's
        content (the adoption check). The caller skips a CLAUDE.md write
        there; it is retried on the next sync."""
        return set(self._result.blocked)

    @property
    def freed(self) -> set[str]:
        """Blocked names that are blocked only because this pass freed them
        (an archive or removal just released the inode): ``finish`` records
        their claimant there, and its CLAUDE.md is written next sync."""
        return self._result.blocked - (
            set(self._result.deleted.values())
            | self._result.foreign
            | set(self._result.held.values())
        )

    def kept_legacy_targets(self, claimed: set[str]) -> dict[str, str]:
        """Legacy directory -> workstream id, for this pass's kept workstreams.

        A rolled-back backend points the workers and Planner of a workstream
        kept in the declared layout at its legacy directory (no
        ``workspace_dir`` in task payloads), so that workstream's CLAUDE.md
        is written there too. Only a legacy directory with exactly one such
        workstream and no current workstream claiming it as its own: a
        shared one would give each workstream's workers another's
        instructions, so it gets none (a warning is logged, and
        ``shared_kept_targets`` names it so a CLAUDE.md written there while
        it had one owner is removed).
        """
        self._shared_kept_targets = set()
        owners: dict[str, list[str]] = {}
        for workstream_id, legacy in self._kept_legacy.items():
            if legacy not in claimed:
                owners.setdefault(legacy, []).append(workstream_id)
        targets: dict[str, str] = {}
        for legacy, ids in sorted(owners.items()):
            if len(ids) == 1:
                targets[legacy] = ids[0]
            else:
                self._shared_kept_targets.add(legacy)
                logger.warning(
                    "Workstreams %s all resolve to the legacy directory %s "
                    "while the backend is rolled back; no CLAUDE.md is written "
                    "there (each would carry another's instructions).",
                    ", ".join(sorted(ids)),
                    legacy,
                )
        return targets

    @property
    def shared_kept_targets(self) -> set[str]:
        """Legacy directories ``kept_legacy_targets`` gave no CLAUDE.md
        because several kept workstreams resolve to them."""
        return set(self._shared_kept_targets)

    def directory_for(self, ws: dict) -> str:
        """The directory of one synced workstream row.

        The backend's declared ``workspace_dir``; without one (a rolled-back
        backend), the declared layout again when the map records that the
        backend declared it for this workstream (``workstream_dir_slug`` is
        exactly what that backend declares, and it follows renames); for a
        workstream the backend never declared, the legacy
        ``slugify(name)`` layout an older backend writes to.
        """
        name = ws.get("name")
        workstream_id = canonical_workstream_id(ws.get("id"))
        legacy = legacy_workstream_dir_slug(name)
        declared = ws.get("workspace_dir")
        if valid_directory_name(declared):
            directory = declared
            if workstream_id:
                self._declared_now[workstream_id] = directory
        elif workstream_id and workstream_id in self._map.declared:
            code = str(ws.get("short_code") or "").strip()
            directory = workstream_dir_slug(name, code)
            if not code and directory == "ws":
                # No short code to derive the fallback from: keep the
                # directory last declared.
                directory = self._map.declared[workstream_id]
            self._declared_now[workstream_id] = directory
            if valid_directory_name(legacy) and legacy != directory:
                self._kept_legacy[workstream_id] = legacy
                self._map.kept_legacy[workstream_id] = legacy
        else:
            directory = legacy
        if workstream_id and valid_directory_name(legacy):
            self._legacy_now[workstream_id] = legacy
        return directory

    def prepare(
        self, root_fd: int, entries: list[tuple[dict, str | None, str]]
    ) -> None:
        self._identified = [
            (workstream_id, slug)
            for _ws, workstream_id, slug in entries
            if workstream_id
        ]
        interrupted = bool(self._map.pending)
        if interrupted:
            reconcile_pending(root_fd, self._map)
        if not self._map.found:
            self._map.seed_legacy(
                [
                    (workstream_id, str(ws.get("name") or ""))
                    for ws, workstream_id, _slug in entries
                    if workstream_id
                ]
            )
        self._claim_late_writes_of_deleted(root_fd)
        self._retire_previous_declared(root_fd)
        # Where the backend declares each workstream and its legacy name are
        # facts of this pass's rows, not of the disk. They are saved first
        # (with the write-ahead base) whenever they change, so after a lost
        # final save the next sync still knows where workers wrote, and the
        # rollback helper moves each directory to the legacy name of the
        # workstream's CURRENT name (an older daemon deletes any other).
        identified = {workstream_id for workstream_id, _slug in self._identified}
        declared = {
            workstream_id: directory
            for workstream_id, directory in self._declared_now.items()
            if workstream_id in identified
        }
        legacy = {
            workstream_id: directory
            for workstream_id, directory in self._legacy_now.items()
            if workstream_id in identified
        }
        renamed = declared != self._map.declared or legacy != self._map.legacy
        self._map.declared = declared
        self._map.legacy = legacy
        self._journal = WriteAheadJournal(self._map, self._failures)
        if interrupted or (renamed and self._map.found):
            # The completed steps are recorded before a new plan is made, so
            # the old plan is never applied again.
            self._journal.persist_base()
        # Normalised as ``generate_workstream_claude_md`` writes it.
        short_codes = {
            workstream_id: " ".join(str(ws.get("short_code") or "WS").split())
            for ws, workstream_id, _slug in entries
            if workstream_id
        }
        self._result = reconcile_workstream_directories(
            root_fd, self._identified, self._map, short_codes, self._journal
        )

    def _claim_late_writes_of_deleted(self, root_fd: int) -> None:
        """Late writes at the old name of a workstream deleted since are that
        deleted workstream's content: a ``deleted`` claim (under a fresh key,
        with the directory's identity) replaces the retired entry, so the
        name is archived, moved aside or frozen like the deleted workstream's
        own directory, and never adopted, also when the first archive fails.
        A name the map records as a current workstream's directory is left to
        the rules for that."""
        current = {workstream_id for workstream_id, _slug in self._identified}
        recorded = {
            name
            for workstream_id, name in self._map.current.items()
            if workstream_id in current
        }
        claimed_names = set(self._map.deleted.values())
        for name, owner in sorted(self._map.retired.items()):
            if owner in current:
                continue
            del self._map.retired[name]
            identity = directory_identity(name, root_fd)
            if identity is None or name in recorded or name in claimed_names:
                continue
            key = _new_claim_key(name, current | set(self._map.deleted))
            self._map.deleted[key] = name
            self._map.deleted_identity[key] = identity
            claimed_names.add(name)

    def _retire_previous_declared(self, root_fd: int) -> None:
        """Workers and the backend write to a workstream's declared directory
        even while its own directory cannot be placed there (its move is
        staged or held). When the declared name changes, what was written at
        the old one is that workstream's: its late writes, merged forward like
        those at a renamed workstream's old name. A name the map records for
        another workstream (or as a deleted or kept legacy directory) is left
        to the rules for those."""
        others: dict[str, set[str]] = {}
        for owner, name in self._map.current.items():
            others.setdefault(name, set()).add(owner)
        for owner, name in (*self._map.staged.items(), *self._map.kept_legacy.items()):
            others.setdefault(name, set()).add(owner)
        # A deleted workstream's claim protects its name while the directory
        # there is still the one it recorded (a stale claim is dropped later).
        protected = {
            name
            for gone_id, name in self._map.deleted.items()
            if self._map.deleted_identity.get(gone_id)
            in (None, directory_identity(name, root_fd))
        }
        for workstream_id, slug in self._identified:
            previous = self._map.declared.get(workstream_id)
            if (
                not previous
                or previous == slug
                or previous in self._map.retired
                or previous in protected
                or not is_real_directory(root_fd, previous)
            ):
                continue
            recorded = others.get(previous, set())
            if recorded - {workstream_id}:
                continue
            if workstream_id in recorded and workstream_id not in self._map.staged:
                # Its own directory: the rename moves it.
                continue
            self._map.retired[previous] = workstream_id

    def record_placement(self, workstream_id: str | None, slug: str) -> bool:
        """Record, before its CLAUDE.md is written, that ``workstream_id`` is
        laid out at ``slug`` when the map does not say so yet (a new
        workstream, or one that starts at a new name): a sync whose final
        save is lost then still knows whose directory it is, and a rename
        before the next save moves it instead of archiving it. A workstream
        whose move is held keeps its content at the previous directory, so
        its new name is recorded as an alias whose writes merge forward
        (``finish``); one whose move is still staged needs no record.

        False when the record could not be saved: the caller then writes no
        CLAUDE.md there this sync, so no directory is laid out that the map
        cannot attribute (retried on the next sync).
        """
        if (
            not workstream_id
            or self._journal is None
            or workstream_id in self._result.pending
            or (
                self._map.current.get(workstream_id) == slug
                and workstream_id not in self._map.seeded
                and workstream_id not in self._map.staged
            )
        ):
            return True
        op = "alias" if workstream_id in self._result.held else "place"
        return self._journal.record(op, owner=workstream_id, target=slug)

    def _shared_legacy_names(self, current_ids: dict[str, str]) -> set[str]:
        """Kept legacy directories that also hold another workstream's
        content: the ones already marked, the ones another workstream's move
        was refused from this pass, and any another (current or deleted)
        workstream is recorded at, now claims, or was deleted from."""
        kept_owners: dict[str, set[str]] = {}
        for workstream_id, legacy in (
            *self._map.kept_legacy.items(),
            *self._kept_legacy.items(),
        ):
            kept_owners.setdefault(legacy, set()).add(workstream_id)
        shared = set(self._map.shared_legacy)
        shared |= self._result.shared_legacy & kept_owners.keys()
        deleted_names = set(self._result.deleted.values())
        deleted_names |= set(self._map.deleted.values())
        for legacy, owners in kept_owners.items():
            others = {
                other
                for other, name in (*self._map.current.items(), *current_ids.items())
                if name == legacy and other not in owners
            }
            if others or legacy in deleted_names:
                shared.add(legacy)
        return shared

    def finish(self, root_fd: int, claimed: set[str]) -> None:
        current_ids = dict(self._identified)
        held = self._result.held
        frozen = set(self._result.deleted.values()) | self._result.foreign
        shared_legacy = self._shared_legacy_names(current_ids)
        # A retired name stays mergeable only while its workstream exists
        # and no current workstream has taken the name back. A claimant that
        # waits (the adoption check froze the name, e.g. because the late
        # writes could not be merged forward yet) has not taken it: the
        # entry is kept, so the next sync still merges them forward first.
        # A kept legacy directory that also holds another workstream's
        # content is never merged into one workstream: it is archived once
        # unprotected.
        deleted_names = set(self._result.deleted.values())
        retired = {
            name: owner
            for name, owner in self._map.retired.items()
            if owner in current_ids
            and (name not in claimed or name in self._result.foreign)
            and name not in shared_legacy
            and name not in deleted_names
        }
        # The legacy directory a rolled-back backend writes projections to
        # is kept out of the sweep; a single owner's merges forward once the
        # backend declares the layout again (several owners' mixed content
        # is archived then, like any shared legacy directory).
        legacy_owners: dict[str, set[str]] = {}
        for workstream_id, legacy in self._kept_legacy.items():
            if workstream_id in current_ids and legacy not in claimed:
                legacy_owners.setdefault(legacy, set()).add(workstream_id)
        for legacy, owners in legacy_owners.items():
            if len(owners) == 1 and legacy not in shared_legacy:
                retired[legacy] = next(iter(owners))
            else:
                retired.pop(legacy, None)
        # A workstream whose move is held keeps its content at the previous
        # directory, while its CLAUDE.md, projections and outputs go to the
        # new name: that directory is its too, and merges forward into
        # wherever it is once the name is no longer its own (a rename back or
        # onward, or another workstream claiming it).
        for workstream_id, previous in held.items():
            slug = current_ids.get(workstream_id)
            if (
                slug
                and slug != previous
                and slug not in frozen
                and is_real_directory(root_fd, slug)
            ):
                retired[slug] = workstream_id
        self._map.retired = retired
        # A move that could not be staged keeps its old directory out of the
        # sweep and in the map, so the next sync retries it.
        # A directory moved aside keeps its claim (its archive was attempted
        # in ``prepare``); the sweep archives only one whose claim was lost.
        aside = {name for name in self._result.deleted.values() if _is_aside_name(name)}
        unarchived = sweep_orphan_directories(
            root_fd,
            claimed | set(held.values()) | set(legacy_owners) | aside,
            current_ids,
            self._map,
            blocked=self._result.blocked,
            journal=self._journal,
        )
        # An orphan whose archive failed is claimed like a deleted
        # workstream's directory (under the id its CLAUDE.md names, else a
        # new key): it is archived, moved aside or frozen on later syncs,
        # never adopted by a workstream that takes its name.
        for name, (identity, gone_id) in unarchived.items():
            key = gone_id
            taken = set(current_ids) | set(self._result.deleted)
            if not key or key in taken:
                key = _new_claim_key(name, taken)
            self._result.deleted[key] = name
            self._result.deleted_identity[key] = identity
        # A move still staged records its target; the name it came from is
        # kept apart (``staged``) and never claimed. A workstream waiting at
        # a frozen name (a deleted workstream's directory not archived yet)
        # is not recorded there: it keeps its previous entry, or none, so it
        # is never mistaken for that directory's owner.
        # Likewise a workstream waiting for another's held directory (one
        # whose move could not be staged) is never recorded there: that
        # directory is still the held workstream's.
        # A legacy guess at its own name that waits only because its CLAUDE.md
        # cannot be read stays a guess (``unverified``), checked again next
        # sync; dropping it would leave that directory an orphan.
        occupied = frozen | set(held.values())
        recorded: dict[str, str] = {}
        unverified: dict[str, str] = {}
        for workstream_id, slug in current_ids.items():
            if (
                slug in occupied
                and held.get(workstream_id) != slug
                and workstream_id not in self._result.pending
            ):
                previous = self._map.current.get(workstream_id)
                if previous and previous not in occupied:
                    recorded[workstream_id] = previous
                elif (
                    previous == slug
                    and workstream_id in self._map.seeded
                    and _directory_owner(root_fd, slug, set(current_ids), None)[0]
                    == _UNREADABLE
                ):
                    unverified[workstream_id] = slug
                continue
            recorded[workstream_id] = slug
        # A held move of a legacy guess (its CLAUDE.md cannot be read) keeps
        # the guess unverified too: it is checked again, never trusted.
        guessed = {
            workstream_id: previous
            for workstream_id, previous in held.items()
            if workstream_id in self._map.seeded
        }
        self._map.current = {
            **recorded,
            **{
                workstream_id: previous
                for workstream_id, previous in held.items()
                if workstream_id not in guessed
            },
        }
        for workstream_id in guessed:
            self._map.current.pop(workstream_id, None)
        self._map.unverified = {**unverified, **guessed}
        self._map.staged = {
            workstream_id: origin
            for workstream_id, origin in self._result.pending.items()
            if workstream_id in current_ids
        }
        # A deleted workstream's directory stays claimed while it remains:
        # the next sync retries the archive before anything takes the name.
        self._map.deleted = {
            gone_id: name
            for gone_id, name in self._result.deleted.items()
            if gone_id not in current_ids
            and directory_identity(name, root_fd)
            == self._result.deleted_identity.get(gone_id)
        }
        self._map.deleted_identity = {
            gone_id: self._result.deleted_identity[gone_id]
            for gone_id in self._map.deleted
        }
        self._map.declared = {
            workstream_id: directory
            for workstream_id, directory in self._declared_now.items()
            if workstream_id in current_ids
        }
        self._map.legacy = {
            workstream_id: directory
            for workstream_id, directory in self._legacy_now.items()
            if workstream_id in current_ids
        }
        # A kept legacy directory stays associated with its workstream (so
        # no other workstream moves it away) until it has been merged
        # forward or archived, or a workstream keeping that name owns it.
        self._map.kept_legacy = {
            workstream_id: legacy
            for workstream_id, legacy in self._map.kept_legacy.items()
            if workstream_id in self._kept_legacy
            or (legacy not in claimed and is_real_directory(root_fd, legacy))
        }
        kept_names = set(self._map.kept_legacy.values())
        self._map.shared_legacy = {
            name
            for name in shared_legacy
            if name in kept_names and is_real_directory(root_fd, name)
        }
        # The final save clears the write-ahead record. When it fails, the
        # record stays and the next sync completes it (``reconcile_pending``).
        try:
            self._map.save(strict=True)
        except OSError as exc:
            what = "the workstream directory map"
            if self._failures is not None:
                self._failures.handle(exc, what)
            else:
                logger.warning("Could not persist %s: %s", what, exc)
