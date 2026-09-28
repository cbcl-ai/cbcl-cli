"""Put workstream directories where a pre-``workstream_dirs_v1`` daemon looks.

A daemon from before ``workstream_dirs_v1`` names every workstream directory
``slugify(name)`` (the shared ``office`` directory for a name without ASCII
letters or digits) and, on every sync, DELETES any other directory under
``workstreams/`` unless it holds ``spec.md`` / ``learnings.md`` at its top
level; those it moves to ``.archived/<name>``, deleting an older archive of
the same name first. Directories this daemon created in the new layout
(``ws-<code>`` for such names), a staged ``.relocating-<id>`` move, or a
slug-named archive entry are therefore lost when a daemon is rolled back
past ``workstream_dirs_v1``.

This helper runs while cbcl is STOPPED, before the older daemon starts. It is
driven by the directory map (``.cubicle/workstream_dirs.json``: workstream
id -> directory, and each workstream's legacy directory) and:

* moves each workstream's directory to its legacy directory, merging into an
  existing one when that workstream is its only owner (never into a legacy
  directory the map marks ``shared_legacy``: that is left, with a warning);
* archives (never merges) a directory whose legacy directory is shared with
  another workstream: mixed content cannot be split;
* places a staged ``.relocating-<id>`` move the same way (that directory is
  the workstream's location; the name the map records for it is only the
  target it waited for, never moved or archived as its directory);
* archives the directory of a deleted workstream the daemon could not archive
  yet (a frozen claim), before any other step targets that name, while the
  name still holds the directory the claim recorded (``deleted_identity``);
* archives any other directory the older daemon would delete, unless it holds
  only the regenerable ``CLAUDE.md``;
* renames slug-named ``.archived`` entries to time-stamped names the older
  daemon never produces, so it cannot delete them;
* sets the map aside, so a later ``workstream_dirs_v1`` daemon re-seeds it
  from the legacy layout and moves the directories forward again. The map is
  set aside only when every other step succeeded: after a failure it stays,
  and a re-run (once the cause is fixed) finishes the remaining steps. The
  map records each completed move, merge and archive as it happens, so a
  ``workstream_dirs_v1`` daemon started instead of the re-run moves those
  directories forward again rather than archiving them as orphans.

Whatever the helper cannot resolve with certainty (a deleted claim it cannot
attribute, a shared legacy directory, a directory whose legacy name is
unknown, an unreadable map) is left untouched with a WARNING; the command
then exits non-zero and the map is not set aside.

Without a live map, the newest map an earlier run set aside is used, for
directories an older daemon never creates only: ``.relocating-<id>`` moves,
and recorded ``ws-<code>`` locations whose CLAUDE.md heading does not name
them. The older daemon may already have run and reused other names, so any
other recorded location is reported, not moved. Any ``ws-<code>`` or
``.relocating-<id>`` directory left for the older daemon to delete is
reported as a problem.

Nothing is deleted: merges keep conflicting entries in the source, which is
archived (an empty source directory is removed). Byte-identical duplicate
files are kept once. Dry run by default; ``--apply`` refuses while a cbcl
daemon runs. Every operation is relative to directory descriptors opened
without following links.

Registered file paths the backend rewrote to ``ws-<code>`` are not rewritten
back. For a directory moved or merged back they resolve again once a
``workstream_dirs_v1`` daemon moves it forward (the backend retargets on that
connect). An archived directory (shared legacy directory, or a legacy name
that is not a directory) does not come back on its own, and its paths stay
unresolved: to restore it, stop cbcl after the older daemon has run and before
starting the ``workstream_dirs_v1`` daemon, and rename
``workstreams/.archived/<ws-code>-<stamp>`` back to ``workstreams/<ws-code>``.
"""

from __future__ import annotations

import os
import re
import stat
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import click

from src.config_sync._descriptor_io import (
    is_real_directory,
    lstat_nofollow,
    open_dir_nofollow,
    open_existing_workspace_root,
)
from src.config_sync.workstream_dirs import (
    _HEADING,
    _READ_FLAGS,
    ARCHIVE_DIRNAME,
    CLAUDE_MD,
    MAP_DIRNAME,
    MAP_FILENAME,
    RELOCATING_PREFIX,
    WORKSTREAMS_DIRNAME,
    WorkstreamDirectoryMap,
    _merge_tree,
    canonical_workstream_id,
    directory_attribution,
    directory_identity,
    only_regenerable,
    valid_directory_name,
)
from src.paths import legacy_workstream_dir_slug

# The heading line is at most ~1 KB (names are capped at 255 characters);
# only the first line is read here.
_HEAD_BYTES = 4096
_V1_DIR_PREFIX = "ws-"
_SET_ASIDE_PREFIX = f"{MAP_FILENAME}.pre-rollback-"
_SET_ASIDE_SUFFIX_RE = re.compile(r"(\d{8}T\d{6}Z)(?:-(\d+))?")

MOVE = "move"
MERGE = "merge"
ARCHIVE = "archive"
PROTECT = "protect"
SET_ASIDE = "set-aside"


@dataclass(frozen=True)
class Step:
    """One planned change. ``source`` / ``target`` are entry names in
    ``workstreams/`` (``.archived/`` for ``protect``; ``.cubicle/`` for
    ``set-aside``). An archive target is chosen when the step runs."""

    action: str
    source: str
    target: str = ""
    reason: str = ""
    # The workstream a move/merge/archive places (or the deleted workstream
    # whose directory is archived): the live map is updated as it succeeds.
    owner: str = ""

    def describe(self) -> str:
        if self.action == SET_ASIDE:
            where = f"{MAP_DIRNAME}/{self.source} -> {MAP_DIRNAME}/{self.target}"
        elif self.action == PROTECT:
            archive = f"{WORKSTREAMS_DIRNAME}/{ARCHIVE_DIRNAME}"
            where = f"{archive}/{self.source} -> {archive}/{self.target}"
        elif self.action == ARCHIVE:
            where = (
                f"{WORKSTREAMS_DIRNAME}/{self.source} -> "
                f"{WORKSTREAMS_DIRNAME}/{ARCHIVE_DIRNAME}/{self.target}"
            )
        else:
            where = (
                f"{WORKSTREAMS_DIRNAME}/{self.source} -> "
                f"{WORKSTREAMS_DIRNAME}/{self.target}"
            )
        return f"{self.action:<9} {where}" + (
            f"  ({self.reason})" if self.reason else ""
        )


@dataclass
class RollbackPlan:
    workspace: Path
    stamp: str
    steps: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Directories an older daemon would delete that no step takes care of:
    # each needs the operator before the older daemon starts.
    warnings: list[str] = field(default_factory=list)


@dataclass
class RollbackOutcome:
    done: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    # The kept map could not record the completed steps: a daemon started
    # now would see the pre-rollback locations.
    map_update_failed: bool = False


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")


def _stamped_label(label: str, stamp: str) -> str:
    """An archive name no older daemon produces (it only archives under a
    slug-alphabet name; the stamp carries upper-case ``T``/``Z``)."""
    return f"{label.lstrip('.') or 'orphan'}-{stamp}"


def _unique_stamped_name(parent_fd: int, label: str, stamp: str) -> str:
    base = _stamped_label(label, stamp)
    candidate, suffix = base, 0
    while lstat_nofollow(candidate, parent_fd) is not None:
        suffix += 1
        candidate = f"{base}-{suffix}"
    return candidate


def _legacy_from_claude_md(root_fd: int, directory: str) -> str | None:
    """The legacy directory from the daemon-written ``# Workstream: <name>``
    heading (fallback for a map written before it recorded ``legacy``)."""
    try:
        with open_dir_nofollow(directory, root_fd) as directory_fd:
            descriptor = os.open(CLAUDE_MD, _READ_FLAGS, dir_fd=directory_fd)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    return None
                head = os.read(descriptor, _HEAD_BYTES)
            finally:
                os.close(descriptor)
    except OSError:
        return None
    lines = head.decode("utf-8", "replace").splitlines()
    if not lines or not lines[0].startswith(_HEADING):
        return None
    name = lines[0][len(_HEADING) :].strip()
    if not name:
        return None
    legacy = legacy_workstream_dir_slug(name)
    return legacy if valid_directory_name(legacy) else None


def _newest_set_aside_map(workspace: Path) -> str | None:
    """The newest regular-file map an earlier run set aside, if any."""
    newest: tuple[tuple[str, int], str] | None = None
    try:
        with open_existing_workspace_root(workspace) as workspace_fd, open_dir_nofollow(
            MAP_DIRNAME, workspace_fd
        ) as map_fd:
            for entry in os.listdir(map_fd):
                if not entry.startswith(_SET_ASIDE_PREFIX):
                    continue
                match = _SET_ASIDE_SUFFIX_RE.fullmatch(entry[len(_SET_ASIDE_PREFIX) :])
                info = lstat_nofollow(entry, map_fd)
                if match is None or info is None or not stat.S_ISREG(info.st_mode):
                    continue
                key = (match.group(1), int(match.group(2) or 0))
                if newest is None or key > newest[0]:
                    newest = (key, entry)
    except OSError:
        return None
    return newest[1] if newest else None


def _is_v1_leftover(root_fd: int, name: str) -> bool:
    """A directory only a ``workstream_dirs_v1`` daemon creates, with content
    an older daemon would delete (``ws-<code>`` unless it is the legacy name
    its CLAUDE.md heading gives, or a staged ``.relocating-<id>`` move)."""
    if not is_real_directory(root_fd, name) or only_regenerable(root_fd, name):
        return False
    if name.startswith(RELOCATING_PREFIX):
        return True
    return (
        name.startswith(_V1_DIR_PREFIX)
        and _legacy_from_claude_md(root_fd, name) != name
    )


def _warn_leftovers(plan: RollbackPlan, root_fd: int, handled: set[str]) -> None:
    for name in sorted(os.listdir(root_fd)):
        if name in handled or not _is_v1_leftover(root_fd, name):
            continue
        plan.warnings.append(
            f"workstreams/{name} is not handled and an older daemon deletes it "
            "(no spec.md at its top level). Move it to the workstream's legacy "
            f"directory or into workstreams/{ARCHIVE_DIRNAME}/ by hand before "
            "starting the older daemon."
        )


def plan_rollback(workspace: Path, *, now: datetime | None = None) -> RollbackPlan:
    """Compute the changes; reads only."""
    workspace = Path(workspace)
    plan = RollbackPlan(workspace=workspace, stamp=_stamp(now))
    dir_map = WorkstreamDirectoryMap(workspace)
    dir_map.load()
    set_aside: str | None = None
    if not dir_map.found:
        set_aside = _newest_set_aside_map(workspace)
        if set_aside is not None:
            dir_map.load(set_aside)
    if not dir_map.found:
        plan.notes.append(
            "No readable workstream directory map (.cubicle/workstream_dirs.json): "
            "either no workstream_dirs_v1 daemon synced this workspace (nothing "
            "to reverse) or the map is damaged (inspect workstreams/ by hand "
            "before starting an older daemon)."
        )
        if _map_entry_exists(workspace):
            plan.warnings.append(
                f"{MAP_DIRNAME}/{MAP_FILENAME} exists but cannot be read; nothing "
                "is planned. Restore or repair it (or move the workstream "
                "directories by hand) before starting an older daemon."
            )
        _warn_without_map(plan, workspace)
        return plan
    if dir_map.pending:
        # A workstream_dirs_v1 sync stopped between recording its moves and
        # saving the result: the map's sections do not say where directories
        # are until that daemon completes the record.
        plan.warnings.append(
            f"{MAP_DIRNAME}/{MAP_FILENAME} records an interrupted sync "
            f"({len(dir_map.pending)} planned directory step(s)); nothing is "
            "planned. Start the workstream_dirs_v1 daemon and let it sync once "
            "(it completes the record before anything else; free disk space "
            "first if it reports a full disk), stop it, then re-run this helper."
        )
        return plan
    if set_aside is not None:
        plan.notes.append(
            f"Planning from {MAP_DIRNAME}/{set_aside}: the live map is missing "
            "(an earlier run set it aside). Only staged moves and ws-<code> "
            "directories whose CLAUDE.md does not name them are changed; other "
            "recorded directories are reported. Nothing is set aside again."
        )
    with ExitStack() as stack:
        workspace_fd = stack.enter_context(open_existing_workspace_root(workspace))
        try:
            root_fd = stack.enter_context(
                open_dir_nofollow(WORKSTREAMS_DIRNAME, workspace_fd)
            )
        except FileNotFoundError:
            root_fd = None
        except OSError as exc:
            plan.notes.append(
                f"workstreams/ is not a real directory ({exc}); nothing is planned."
            )
            return plan
        if root_fd is not None:
            _plan_directories(plan, root_fd, dir_map, from_set_aside=set_aside)
        if set_aside is not None:
            if plan.steps and all(step.action == PROTECT for step in plan.steps):
                plan.steps.clear()
                plan.notes.append(
                    "The set-aside map leaves no directory to place; archives "
                    "are left as they are."
                )
            return plan
        if plan.warnings:
            # Something is left for the operator: keep the map a re-run
            # plans from once it is resolved.
            plan.notes.append(
                "The map is kept (not set aside) until every warning is resolved "
                "and a re-run of --apply reports none."
            )
            return plan
        map_dir = lstat_nofollow(MAP_DIRNAME, workspace_fd)
        if map_dir is not None and stat.S_ISDIR(map_dir.st_mode):
            with open_dir_nofollow(MAP_DIRNAME, workspace_fd) as map_fd:
                plan.steps.append(
                    Step(
                        SET_ASIDE,
                        MAP_FILENAME,
                        _unique_stamped_name(
                            map_fd, f"{MAP_FILENAME}.pre-rollback", plan.stamp
                        ),
                        "a later workstream_dirs_v1 daemon re-seeds the map "
                        "from the legacy layout and moves directories forward",
                    )
                )
    return plan


def _map_entry_exists(workspace: Path) -> bool:
    try:
        with open_existing_workspace_root(workspace) as workspace_fd, open_dir_nofollow(
            MAP_DIRNAME, workspace_fd
        ) as map_fd:
            return lstat_nofollow(MAP_FILENAME, map_fd) is not None
    except OSError:
        return False


def _warn_without_map(plan: RollbackPlan, workspace: Path) -> None:
    try:
        with open_existing_workspace_root(workspace) as workspace_fd, open_dir_nofollow(
            WORKSTREAMS_DIRNAME, workspace_fd
        ) as root_fd:
            _warn_leftovers(plan, root_fd, set())
    except OSError:
        return


def _plan_directories(
    plan: RollbackPlan,
    root_fd: int,
    dir_map: WorkstreamDirectoryMap,
    *,
    from_set_aside: str | None = None,
) -> None:
    # Protect existing archives first, so the time-stamped names chosen for
    # them never collide with directories archived by the steps below.
    _plan_archive_protection(plan, root_fd)
    names = sorted(os.listdir(root_fd))
    occupied = set(names)
    real = {name for name in names if is_real_directory(root_fd, name)}

    # A workstream whose move is staged lives in ``.relocating-<id>``; the
    # name the map records for it is the target it waits for, which may be
    # another workstream's directory.
    staged_dirs: dict[str, str] = {}
    for name in names:
        if name.startswith(RELOCATING_PREFIX) and name in real:
            staged_id = canonical_workstream_id(name[len(RELOCATING_PREFIX) :])
            if staged_id:
                staged_dirs[staged_id] = name

    legacy: dict[str, str] = {}
    for workstream_id, recorded in sorted(dir_map.current.items()):
        location = staged_dirs.get(workstream_id, recorded)
        known = dir_map.legacy.get(workstream_id)
        if not known and location in real:
            known = _legacy_from_claude_md(root_fd, location)
        if known:
            legacy[workstream_id] = known
        elif location in real and not only_regenerable(root_fd, location):
            plan.warnings.append(
                f"Workstream {workstream_id}: its legacy directory is unknown (no "
                f"map record, no readable CLAUDE.md heading); workstreams/"
                f"{location} is left in place. An older daemon deletes it unless "
                "its name is slugify(the workstream name) or it holds spec.md: "
                "move it there by hand."
            )
    # Where each directory must end up for the older daemon: the legacy
    # directory of every known workstream, the current one of the rest.
    final_names = set(legacy.values()) | {
        location
        for workstream_id, location in dir_map.current.items()
        if workstream_id not in legacy
    }

    def others_at(target: str, workstream_id: str) -> bool:
        return any(
            other != workstream_id
            and (
                legacy.get(other) == target
                or (other not in legacy and dir_map.current.get(other) == target)
            )
            for other in dir_map.current
        )

    def marked_id(name: str) -> str | None:
        """The workstream id ``name``'s CLAUDE.md marker names; None without
        one, "" when it cannot be read. A marker can only veto a step."""
        try:
            attribution = directory_attribution(root_fd, name)
        except OSError:
            return ""
        return attribution[0] if attribution else None

    def vetoed(name: str, workstream_id: str | None, what: str) -> bool:
        """True when ``name``'s CLAUDE.md leaves it in place (it cannot be
        read, or names another workstream than ``workstream_id``; None:
        a directory the map records for nobody)."""
        marked = marked_id(name)
        if marked is None or marked == workstream_id:
            return False
        consumed.add(name)
        left_in_place.add(name)
        plan.warnings.append(
            f"workstreams/{name} ({what}) is left in place: its CLAUDE.md "
            + (
                "cannot be read"
                if marked == ""
                else f"names workstream {marked}, so the map entry is stale"
            )
            + ". Check whose content it holds and place it by hand, then "
            "re-run --apply."
        )
        return True

    consumed: set[str] = set()
    # Directories a marker veto leaves in place: nothing is moved or merged
    # into them either (their owner is uncertain).
    left_in_place: set[str] = set()
    # Deleted workstreams' directories a set-aside map records: an older
    # daemon may have reused the name since, so they are neither archived
    # nor merged into.
    held_back: set[str] = set()

    # A deleted workstream's directory the daemon could not archive yet is
    # frozen (the daemon never records a workstream there): it is archived
    # before any step targets the name, while it is still the directory the
    # claim recorded. One a map from before identities were recorded shares
    # with a recorded workstream cannot be attributed with certainty.
    locations = {
        location
        for workstream_id, location in dir_map.current.items()
        if workstream_id not in staged_dirs
    }
    for gone_id, name in sorted(dir_map.deleted.items()):
        if gone_id in dir_map.current or name in consumed or name not in real:
            continue
        recorded_identity = dir_map.deleted_identity.get(gone_id)
        if recorded_identity is not None:
            if recorded_identity != directory_identity(name, root_fd):
                plan.notes.append(
                    f"The claim of deleted workstream {gone_id} on workstreams/"
                    f"{name} is stale (another directory is there now); it is "
                    "ignored, as the daemon drops it."
                )
                continue
        elif name in locations:
            consumed.add(name)
            held_back.add(name)
            plan.warnings.append(
                f"workstreams/{name}: the map records it both as the directory of "
                f"deleted workstream {gone_id} and as a current workstream's "
                "(a map from before directory identities were recorded); it is "
                "left in place. Check whose content it holds: move a deleted "
                f"workstream's directory into workstreams/{ARCHIVE_DIRNAME}/ by "
                "hand, then re-run --apply."
            )
            continue
        if vetoed(name, gone_id, f"directory of deleted workstream {gone_id}"):
            held_back.add(name)
            continue
        consumed.add(name)
        if from_set_aside is not None:
            held_back.add(name)
            plan.warnings.append(
                f"workstreams/{name}: the set-aside map records it as the "
                f"directory of deleted workstream {gone_id}; it is left in place "
                "because an older daemon may have reused the name. If it is the "
                f"deleted workstream's, move it into workstreams/{ARCHIVE_DIRNAME}/ "
                "by hand before starting the older daemon."
            )
            continue
        occupied.discard(name)
        real.discard(name)
        plan.steps.append(
            Step(
                ARCHIVE,
                name,
                _stamped_label(name, plan.stamp),
                f"directory of deleted workstream {gone_id}; no other "
                "workstream may take it over",
                owner=gone_id,
            )
        )

    def foreign_target(target: str, workstream_id: str) -> str | None:
        """Whose content the existing legacy directory ``target`` holds when
        its CLAUDE.md id marker names neither this workstream nor one the
        map places there (a deleted workstream's orphan the daemon could
        not archive); "" when it cannot be read; None otherwise. A file
        without the marker (an older daemon's) is taken as before."""
        marked = marked_id(target)
        if marked is None or marked == workstream_id:
            return None
        if marked and dir_map.current.get(marked) == target:
            return None
        return marked

    def place(source: str, target: str, workstream_id: str, label: str) -> None:
        if vetoed(source, workstream_id, label):
            return
        consumed.add(source)
        if target in dir_map.shared_legacy and target in real:
            plan.warnings.append(
                f"workstreams/{source} ({label}) is left in place: its legacy "
                f"directory {target} also holds another workstream's content "
                "from a backend rollback (never merged into one workstream). "
                f"Move workstreams/{target} into workstreams/{ARCHIVE_DIRNAME}/ "
                "by hand (a re-upgrade archives it too), then re-run --apply."
            )
            return
        if target in left_in_place:
            plan.warnings.append(
                f"workstreams/{source} ({label}) is left in place: its legacy "
                f"directory {target} is left in place too (its CLAUDE.md does "
                "not name its owner reliably). Place both by hand, then re-run "
                "--apply."
            )
            consumed.add(source)
            return
        if target in held_back:
            plan.warnings.append(
                f"workstreams/{source} ({label}) is left in place: its legacy "
                f"directory {target} is a deleted workstream's directory the "
                "set-aside map records. Place it by hand before starting the "
                "older daemon."
            )
            return
        if (
            target in real
            and target not in consumed
            and not others_at(target, workstream_id)
        ):
            foreign = foreign_target(target, workstream_id)
            if foreign is not None:
                # A marker only vetoes: nothing is merged into, or archived
                # out of, a directory it attributes to someone else.
                consumed.add(target)
                plan.warnings.append(
                    f"workstreams/{source} ({label}) is left in place: its legacy "
                    f"directory {target} "
                    + (
                        "has a CLAUDE.md that cannot be read"
                        if foreign == ""
                        else f"holds workstream {foreign}'s content"
                    )
                    + ". Check it and move it into workstreams/"
                    f"{ARCHIVE_DIRNAME}/ by hand if it is not this workstream's, "
                    "then re-run --apply."
                )
                return
        target_free = target not in occupied
        target_is_dir = target in real
        occupied.discard(source)
        real.discard(source)
        if others_at(target, workstream_id):
            plan.steps.append(
                Step(
                    ARCHIVE,
                    source,
                    _stamped_label(source, plan.stamp),
                    f"{label}: its legacy directory {target} is shared with "
                    "another workstream; mixed content cannot be split",
                    owner=workstream_id,
                )
            )
        elif target_free:
            plan.steps.append(Step(MOVE, source, target, label, owner=workstream_id))
            occupied.add(target)
            real.add(target)
        elif target_is_dir:
            plan.steps.append(
                Step(
                    MERGE,
                    source,
                    target,
                    f"{label}: entries {target} lacks move in; conflicting "
                    "entries stay behind and are archived",
                    owner=workstream_id,
                )
            )
        else:
            plan.steps.append(
                Step(
                    ARCHIVE,
                    source,
                    _stamped_label(source, plan.stamp),
                    f"{label}: {target} exists and is not a directory",
                    owner=workstream_id,
                )
            )

    # A legacy guess the daemon could not check (``unverified``: its CLAUDE.md
    # could not be read) is not trusted where no move checks it: an older
    # daemon would take the directory as it is. It is left with a warning
    # unless its CLAUDE.md can now be read and does not name another
    # workstream; the daemon checks the guess again on its next sync.
    for workstream_id in sorted(dir_map.seeded):
        location = dir_map.current.get(workstream_id)
        if (
            location
            and location in real
            and location not in consumed
            and legacy.get(workstream_id, location) == location
        ):
            vetoed(
                location,
                workstream_id,
                f"workstream {workstream_id}'s directory, a guess the daemon "
                "could not check",
            )

    for workstream_id, location in sorted(dir_map.current.items()):
        if workstream_id in staged_dirs:
            continue  # placed from its staged directory below
        target = legacy.get(workstream_id)
        if not (target and location != target and location in real):
            continue
        if from_set_aside is not None and not (
            location.startswith(_V1_DIR_PREFIX) and _is_v1_leftover(root_fd, location)
        ):
            # An older daemon may have created this name since for a
            # workstream of its own: only a ws-<code> directory whose
            # CLAUDE.md heading does not name it is surely this map's.
            consumed.add(location)
            if not only_regenerable(root_fd, location):
                plan.warnings.append(
                    f"workstreams/{location}: the set-aside map records it as "
                    f"workstream {workstream_id}'s directory (legacy {target}), "
                    "but it may now belong to a workstream an older daemon "
                    "created. It is left in place; check it by hand."
                )
            continue
        place(location, target, workstream_id, f"workstream {workstream_id}")

    for name in names:
        if not name.startswith(RELOCATING_PREFIX) or name not in real:
            continue
        workstream_id = canonical_workstream_id(name[len(RELOCATING_PREFIX) :])
        target = None
        if workstream_id:
            target = legacy.get(workstream_id) or dir_map.current.get(workstream_id)
        if workstream_id and target:
            place(
                name,
                target,
                workstream_id,
                f"staged move of workstream {workstream_id}",
            )
            continue
        consumed.add(name)
        plan.steps.append(
            Step(
                ARCHIVE,
                name,
                _stamped_label(name, plan.stamp),
                "staged move of a workstream the map does not record",
            )
        )

    if from_set_aside is not None:
        # Other directories may be the older daemon's own by now.
        _warn_leftovers(plan, root_fd, consumed | set(legacy.values()))
        return

    for name in names:
        if (
            name in consumed
            or name in final_names
            or name == ARCHIVE_DIRNAME
            or name not in real
        ):
            continue
        if only_regenerable(root_fd, name):
            plan.notes.append(
                f"workstreams/{name} holds only CLAUDE.md; left for the older "
                "daemon to remove."
            )
            continue
        marked = marked_id(name)
        if marked == "" or (marked and marked in dir_map.current):
            vetoed(name, None, "not recorded for any workstream")
            continue
        plan.steps.append(
            Step(
                ARCHIVE,
                name,
                _stamped_label(name, plan.stamp),
                "not the directory of any recorded workstream; an older daemon "
                "deletes it unless it holds spec.md",
            )
        )


def _plan_archive_protection(plan: RollbackPlan, root_fd: int) -> None:
    archive = lstat_nofollow(ARCHIVE_DIRNAME, root_fd)
    if archive is None:
        return
    if not stat.S_ISDIR(archive.st_mode):
        plan.notes.append(
            f"workstreams/{ARCHIVE_DIRNAME} is not a real directory; nothing is "
            "archived through it (archive steps fail and keep their source)."
        )
        return
    with open_dir_nofollow(ARCHIVE_DIRNAME, root_fd) as archive_fd:
        taken: set[str] = set()
        for entry in sorted(os.listdir(archive_fd)):
            if not valid_directory_name(entry):
                continue
            base = _stamped_label(entry, plan.stamp)
            target, suffix = base, 0
            while lstat_nofollow(target, archive_fd) is not None or target in taken:
                suffix += 1
                target = f"{base}-{suffix}"
            taken.add(target)
            plan.steps.append(
                Step(
                    PROTECT,
                    entry,
                    target,
                    "an older daemon deletes a same-named archive before "
                    "archiving a directory of that name",
                )
            )


def _archive(root_fd: int, name: str, stamp: str) -> str:
    try:
        os.mkdir(ARCHIVE_DIRNAME, 0o755, dir_fd=root_fd)
    except FileExistsError:
        pass
    with open_dir_nofollow(ARCHIVE_DIRNAME, root_fd) as archive_fd:
        destination = _unique_stamped_name(archive_fd, name, stamp)
        os.rename(name, destination, src_dir_fd=root_fd, dst_dir_fd=archive_fd)
    return destination


def _run_step(
    step: Step, root_fd: int | None, workspace_fd: int, stamp: str
) -> tuple[str, bool]:
    """Run one step; the text for the report, and for a move or merge
    whether the directory is now at its target (False: it was archived)."""
    if step.action == SET_ASIDE:
        with open_dir_nofollow(MAP_DIRNAME, workspace_fd) as map_fd:
            if lstat_nofollow(step.target, map_fd) is not None:
                raise FileExistsError(step.target)
            os.rename(step.source, step.target, src_dir_fd=map_fd, dst_dir_fd=map_fd)
        return step.describe(), False
    if root_fd is None:
        raise FileNotFoundError(WORKSTREAMS_DIRNAME)
    if step.action == PROTECT:
        with open_dir_nofollow(ARCHIVE_DIRNAME, root_fd) as archive_fd:
            if lstat_nofollow(step.target, archive_fd) is not None:
                raise FileExistsError(step.target)
            os.rename(
                step.source, step.target, src_dir_fd=archive_fd, dst_dir_fd=archive_fd
            )
        return step.describe(), False
    if not is_real_directory(root_fd, step.source):
        raise FileNotFoundError(f"{step.source} is no longer a directory")
    if step.action == ARCHIVE:
        destination = _archive(root_fd, step.source, stamp)
        return (
            f"archive   workstreams/{step.source} -> "
            f"workstreams/{ARCHIVE_DIRNAME}/{destination}"
        ), False
    # MOVE or MERGE: re-check the target now; nothing is ever overwritten.
    if lstat_nofollow(step.target, root_fd) is None:
        os.rename(step.source, step.target, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        return f"move      workstreams/{step.source} -> workstreams/{step.target}", True
    if step.action == MOVE:
        # Planned as a move: the target was absent, or an earlier step was
        # to free it (archiving a deleted workstream's directory). It is
        # there, so that step failed: never merge into what it still holds.
        raise FileExistsError(
            f"{step.target} still exists (an earlier step that was to free "
            f"it failed); {step.source} is kept"
        )
    if not is_real_directory(root_fd, step.target):
        destination = _archive(root_fd, step.source, stamp)
        return (
            f"archive   workstreams/{step.source} -> workstreams/"
            f"{ARCHIVE_DIRNAME}/{destination}  ({step.target} is not a directory)"
        ), False
    with open_dir_nofollow(step.source, root_fd) as source, open_dir_nofollow(
        step.target, root_fd
    ) as destination_fd:
        _merge_tree(source, destination_fd)
    try:
        os.rmdir(step.source, dir_fd=root_fd)
        return f"merge     workstreams/{step.source} -> workstreams/{step.target}", True
    except OSError:
        pass
    try:
        remainder = _archive(root_fd, step.source, stamp)
    except OSError as exc:
        # The content is at the target now: the map must say so, although the
        # step failed (the conflicting entries are still at the source).
        raise _MergedWithLeftover(
            f"merged into {step.target}, but the conflicting entries left in "
            f"{step.source} could not be archived ({exc}); they are kept"
        ) from exc
    return (
        f"merge     workstreams/{step.source} -> workstreams/{step.target}; "
        f"conflicting entries archived to workstreams/{ARCHIVE_DIRNAME}/{remainder}"
    ), True


class _MergedWithLeftover(OSError):
    """A merge whose content reached its target, but whose leftover entries
    could not be archived: the step failed and is also recorded as placed."""


def _record_in_map(dir_map: WorkstreamDirectoryMap, step: Step, placed: bool) -> bool:
    """Record a completed step in the live map; True when it changed.

    A partly applied rollback keeps the map, and a daemon started before a
    re-run finishes reads it: it must name where each directory now is (a
    ``workstream_dirs_v1`` daemon then moves it forward again, as from the
    legacy layout), not where it was before the helper ran.
    """
    owner = step.owner
    if not owner or step.action not in (MOVE, MERGE, ARCHIVE):
        return False
    if step.action != ARCHIVE and placed:
        dir_map.current[owner] = step.target
        dir_map.staged.pop(owner, None)
        return True
    if owner in dir_map.deleted:
        dir_map.deleted.pop(owner)
        dir_map.deleted_identity.pop(owner, None)
        return True
    # An archived live workstream stays recorded (a daemon skips a recorded
    # directory that no longer exists): a re-run still sees that its legacy
    # directory is shared, and every other section (shared_legacy included)
    # is kept as loaded.
    return dir_map.staged.pop(owner, None) is not None


def _keep_unchecked_guesses(
    dir_map: WorkstreamDirectoryMap, guesses: dict[str, str]
) -> None:
    """Move each loaded ``unverified`` guess the helper did not place (it
    still names the guessed directory) back from ``current`` to
    ``unverified``, so the save keeps it a guess the daemon checks again."""
    for workstream_id, name in guesses.items():
        if dir_map.current.get(workstream_id) == name:
            dir_map.current.pop(workstream_id)
            dir_map.unverified[workstream_id] = name


def apply_rollback(plan: RollbackPlan) -> RollbackOutcome:
    """Run the planned steps; each failure is reported and leaves its source
    in place. Nothing is deleted. The map is set aside only when every other
    step succeeded; until then the live map records each completed move,
    merge and archive."""
    outcome = RollbackOutcome()
    if not plan.steps:
        return outcome
    dir_map = WorkstreamDirectoryMap(plan.workspace)
    dir_map.load()
    if dir_map.pending:
        # A daemon synced after the plan was made and was interrupted.
        outcome.failed.append(
            f"{MAP_DIRNAME}/{MAP_FILENAME} now records an interrupted sync; "
            "nothing was changed. Re-run the helper (it explains what to do)."
        )
        return outcome
    # Without a write-ahead record, ``seeded`` holds only the ``unverified``
    # legacy guesses ``load`` moved into ``current``. A save would record
    # them as trusted entries (B4-bugs-2), so each one the helper did not
    # place is written back as a guess before every save.
    guesses = {
        workstream_id: dir_map.current[workstream_id]
        for workstream_id in dir_map.seeded
        if workstream_id in dir_map.current
    }
    map_changed = False
    with ExitStack() as stack:
        workspace_fd = stack.enter_context(open_existing_workspace_root(plan.workspace))
        try:
            root_fd: int | None = stack.enter_context(
                open_dir_nofollow(WORKSTREAMS_DIRNAME, workspace_fd)
            )
        except FileNotFoundError:
            root_fd = None
        # Sources of failed steps are still where they were: a later move or
        # merge into one of them would mix content with what it still holds.
        kept_in_place: set[str] = set()
        for step in plan.steps:
            if step.action in (MOVE, MERGE) and step.target in kept_in_place:
                kept_in_place.add(step.source)
                outcome.failed.append(
                    f"{step.describe()}: skipped because {step.target} is still "
                    "occupied (an earlier step failed); the source is kept"
                )
                continue
            if step.action == SET_ASIDE and outcome.failed:
                # The map is what a re-run plans from: keep it until every
                # directory is where the older daemon looks.
                outcome.failed.append(
                    f"{step.describe()}: skipped because "
                    f"{len(outcome.failed)} earlier step(s) failed; the map is "
                    "kept so a re-run can finish"
                )
                continue
            if step.action == SET_ASIDE and map_changed:
                # The set-aside copy records where the directories are now.
                _keep_unchecked_guesses(dir_map, guesses)
                dir_map.save()
                map_changed = False
            try:
                text, placed = _run_step(step, root_fd, workspace_fd, plan.stamp)
            except OSError as exc:
                if step.action in (MOVE, MERGE, ARCHIVE):
                    kept_in_place.add(step.source)
                outcome.failed.append(f"{step.describe()}: {exc}")
                if isinstance(exc, _MergedWithLeftover) and dir_map.found:
                    map_changed |= _record_in_map(dir_map, step, True)
                continue
            outcome.done.append(text)
            if dir_map.found:
                map_changed |= _record_in_map(dir_map, step, placed)
    if map_changed:
        _keep_unchecked_guesses(dir_map, guesses)
        try:
            dir_map.save(strict=True)
        except OSError as exc:
            outcome.map_update_failed = True
            outcome.failed.append(
                f"update {MAP_DIRNAME}/{MAP_FILENAME} with the completed steps: {exc}"
            )
    return outcome


def daemon_is_running() -> bool:
    """True while a cbcl daemon runs on this host (PID file or /proc scan)."""
    from src.daemon import _is_process_running, _read_pid, find_running_daemon_pid
    from src.paths import get_pid_path

    pid_path = get_pid_path()
    if pid_path.exists():
        pid = _read_pid(pid_path)
        if pid is not None and _is_process_running(pid):
            return True
    return find_running_daemon_pid() is not None


def _default_workspaces_root() -> Path:
    from src.paths import CUBICLE_HOME

    return CUBICLE_HOME / "workspaces"


def _selected_workspaces(
    workspaces: tuple[Path, ...], offices: tuple[str, ...]
) -> list[Path]:
    from src.paths import get_workspace_path

    selected = [Path(path) for path in workspaces]
    selected += [get_workspace_path(slug, create=False) for slug in offices]
    if selected:
        return selected
    root = _default_workspaces_root()
    if not root.is_dir():
        return []
    return sorted(child for child in root.iterdir() if child.is_dir())


@click.command("prepare-rollback")
@click.option(
    "--workspace",
    "workspaces",
    multiple=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Office workspace directory (repeatable).",
)
@click.option(
    "--office",
    "offices",
    multiple=True,
    help="Office workspace slug under ~/.cubicle/workspaces (repeatable).",
)
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    help="Make the changes (default: print the plan only). cbcl must be stopped.",
)
def prepare_rollback(
    workspaces: tuple[Path, ...], offices: tuple[str, ...], apply_changes: bool
) -> None:
    """Put workstream directories where a daemon older than workstream_dirs_v1
    looks, before rolling the daemon back. Default: every workspace under
    ~/.cubicle/workspaces. Never deletes; archives on collisions."""
    if apply_changes and daemon_is_running():
        raise click.ClickException(
            "Stop cbcl (drained) before --apply: a running daemon moves the "
            "directories again on its next sync."
        )
    targets = _selected_workspaces(workspaces, offices)
    if not targets:
        # Nothing inspected is never "no problems": an operator running as a
        # user whose home is not the daemon's (sudo, a service account)
        # would otherwise start an older daemon that deletes new-layout
        # directories.
        import getpass

        raise click.ClickException(
            f"No office workspaces under {_default_workspaces_root()} (looked "
            f"there as user {getpass.getuser()}, uid {os.getuid()}). Run this "
            "as the user (with the same HOME) that runs cbcl, or name each "
            "workspace with --workspace <path> or --office <slug>."
        )
    failures = 0
    map_update_failed = False
    for workspace in targets:
        click.echo(f"Workspace {workspace}")
        if not workspace.is_dir():
            click.echo("  not a directory; skipped")
            failures += 1
            continue
        plan = plan_rollback(workspace)
        for note in plan.notes:
            click.echo(f"  note: {note}")
        for warning in plan.warnings:
            click.echo(f"  WARNING: {warning}")
        failures += len(plan.warnings)
        if not plan.steps:
            click.echo("  nothing to change")
            continue
        if not apply_changes:
            for step in plan.steps:
                click.echo(f"  {step.describe()}")
            continue
        outcome = apply_rollback(plan)
        for line in outcome.done:
            click.echo(f"  done: {line}")
        for line in outcome.failed:
            click.echo(f"  FAILED (source kept): {line}")
        failures += len(outcome.failed)
        map_update_failed |= outcome.map_update_failed
    if not apply_changes:
        click.echo(
            "Dry run: nothing changed. Stop cbcl, then re-run with --apply "
            "before starting the older daemon."
        )
    if failures:
        restart = (
            "Do not start any cbcl daemon before that: the map could not record "
            "the completed steps."
            if map_update_failed
            else "Do not start an older daemon before that; restarting the "
            "current daemon instead moves completed steps forward again."
        )
        raise click.ClickException(
            f"{failures} problem(s) need attention; see above. Fix the cause, "
            "then re-run with --apply (the map is kept after a failed step) "
            f"until none remain. {restart}"
        )


@click.group("workstream-dirs")
def workstream_dirs_command() -> None:
    """Workstream directory maintenance."""


workstream_dirs_command.add_command(prepare_rollback)


if __name__ == "__main__":  # python -m src.config_sync.workstream_dirs_rollback
    prepare_rollback()
