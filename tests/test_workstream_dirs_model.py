"""Model-based check of the workstream directory layout and rollback helper.

Seeded random sequences (``random.Random(seed)``, no extra dependency) of
what an office goes through:

* backend changes that may pile up before the daemon syncs (the daemon
  offline, or revisions coalesced): create, rename and delete workstreams,
  including swaps (two workstreams exchange names) and chains (one takes
  another's name, which takes a new one). A new workstream may get a
  deleted one's name and short code (the backend only avoids live codes);
* syncs that succeed, or lose their map save: the daemon dies after its
  moves and before the final save, or the disk fills up at the final save
  or after a few write-ahead records (every later map write failing too);
* writes addressed the way the backend and workers address them: through
  the declared ``workspace_dir`` (whatever CLAUDE.md is there, except a
  directory the daemon keeps frozen for another workstream: the documented
  limitation), and late writes of a session that started before a rename,
  into the previous directory;
* session-caused conditions: ``workstreams/.archived`` replaced by a file,
  a link planted at a directory name, a CLAUDE.md the daemon cannot read;
* the operator restoring an older copy of the map, and rolling the daemon
  back with ``prepare-rollback --apply``, an older-daemon pass and a
  re-upgrade sync.

After every step:

1. no directory whose CLAUDE.md names a workstream holds a file written for
   another workstream (live or deleted);
2. no file is ever deleted;
3. no file of a LIVE workstream is ever archived (unless the model predicts
   it: content the older daemon's layout mixed, or files an operator's map
   restore left unattributed);
4. a sync raises only for the injected full disk (``OSError(ENOSPC)``, so
   worker admission stays closed), never for anything a session causes;
5. the rollback helper never exits 0 while it leaves something for the
   older daemon to destroy or mix (checked by running that daemon).

Once every cause is repaired and syncs run (``settle``): every live
workstream's files are in its current directory, every deleted one's are
archived, and the map keeps no claim, staged move or write-ahead record.

The test also counts the branches the sequences reach (frozen names,
identity checks, marker vetoes, completed write-ahead records, helper
refusals, ...) and fails when one was never reached across the seeds.

``CUBICLE_WSDIRS_MODEL_SEEDS`` sets the number of seeds (default 300),
``CUBICLE_WSDIRS_MODEL_FIRST`` the first one (for a long run in chunks);
``CUBICLE_WSDIRS_MODEL_SEED`` runs a single one (a reproduction; no branch
check).
"""

from __future__ import annotations

import errno
import json
import logging
import os
import random
import shutil
import stat
import tempfile
import uuid
from collections import Counter
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from src.config_sync import workstream_dirs, workstream_dirs_rollback
from src.config_sync.claude_md_writer import ClaudeMdWriter
from src.config_sync.workstream_dirs import (
    DELETED_PREFIX,
    MAP_DIRNAME,
    MAP_FILENAME,
    RELOCATING_PREFIX,
)
from src.config_sync.workstream_dirs_rollback import apply_rollback, plan_rollback
from src.paths import slugify, workstream_dir_slug

NAMES = (
    "Alpha",
    "Beta",
    "Gamma",
    "Delta",
    "Office",
    "Продажі",
    "Маркетинг",
    "Фінанси",
)
STEPS_PER_SEED = 40
SEEDS = int(os.environ.get("CUBICLE_WSDIRS_MODEL_SEEDS", "300"))
FIRST = int(os.environ.get("CUBICLE_WSDIRS_MODEL_FIRST", "0"))
ONE_SEED = os.environ.get("CUBICLE_WSDIRS_MODEL_SEED")
# B7b-tests-01: a per-test budget that grows with the seed count, so the
# model is never cut short by the suite-wide 30 s pytest-timeout on a slow
# runner (about 0.05 s per seed here; 0.5 s per seed leaves a wide margin).
MODEL_TIMEOUT_SECONDS = 60 + (1 if ONE_SEED else SEEDS) // 2
ARCHIVE_HOLD = ".archived-hold"
UNREADABLE_TAG = "\n<!-- model: unreadable to the daemon -->\n"
MARKER = "<!-- workstream-id: {} -->"

# Branches every run of the default seeds must reach (see ``Office.counters``).
REQUIRED_BRANCHES = (
    "frozen_name",
    "identity_check",
    "marker_veto",
    "pending_replay",
    "helper_refusal",
    "helper_pending_refusal",
    "moved_aside",
    "record_failure",
    "coalesced_sync",
    "swap",
    "chain",
    "late_write",
    "unreadable_claude_md",
    "workspace_dir_write",
)
# Log messages of ``workstream_dirs`` that mark a branch.
LOGGED_BRANCHES = {
    "An interrupted sync left": "pending_replay",
    "is stale (another directory is there now)": "identity_check",
    "the map entry is stale": "marker_veto",
    "is not merged into workstream": "marker_veto",
    "is stale (its CLAUDE.md names current workstream": "marker_veto",
    "is not taken as its previous directory": "marker_veto",
    "Moved %s aside": "moved_aside",
    "cannot record it first": "record_failure",
    "Cannot read %s/%s": "unreadable_claude_md",
}


def model_clock(seed: int) -> type[datetime]:
    """A deterministic clock for the archive and set-aside stamps, so a seed
    replays exactly: even seeds stay within one second (stamps collide),
    odd ones advance a second per reading."""
    step = timedelta(seconds=seed % 2)
    readings = {"count": 0}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            readings["count"] += 1
            base = datetime(2026, 9, 24, 12, 0, 0, tzinfo=tz or UTC)
            return base + step * readings["count"]

    return Clock


class Crash(BaseException):
    """The daemon process dying (not an ``Exception`` a sync catches)."""


class BranchCounter(logging.Handler):
    def __init__(self, counters: Counter) -> None:
        super().__init__(logging.DEBUG)
        self.counters = counters

    def emit(self, record: logging.LogRecord) -> None:
        for fragment, branch in LOGGED_BRANCHES.items():
            if fragment in str(record.msg):
                self.counters[branch] += 1


class Office:
    """The model: live workstreams, every tagged file ever written, and the
    session-caused conditions currently in place."""

    def __init__(self, root: Path, rng: random.Random, counters: Counter) -> None:
        self.base = root
        self.workspace = root / "workspace"
        self.workstreams = self.workspace / "workstreams"
        self.outside = root / "outside"
        self.outside.mkdir()
        (self.outside / "host.txt").write_text("host")
        self.rng = rng
        self.counters = counters
        self.live: dict[str, dict] = {}
        self.deleted: set[str] = set()
        # Deleted workstream id -> its last row (name, short code).
        self.gone: dict[str, dict] = {}
        self.files: dict[str, str] = {}  # file name -> owner id
        # Files the model predicts may be archived though their workstream
        # is live (mixed by the older daemon's layout, or unattributed after
        # a map restore): never lost, never mixed, but not held to liveness.
        self.excused: set[str] = set()
        self.codes = 0
        self.freed_codes: dict[str, str] = {}
        # Backend changes the daemon has not synced successfully yet.
        self.changes = 0
        # Workstream id -> its directory before the last synced rename (a
        # session started before the rename may still write there).
        self.previous: dict[str, str] = {}
        self.synced_slugs: dict[str, str] = {}
        self.archive_broken = False
        self.links: set[str] = set()
        # A CLAUDE.md made unreadable carries UNREADABLE_TAG (the condition
        # travels with the file, as a chmod does, and ends when the daemon
        # replaces the file).
        self.unreadable = False
        self.backups: list[bytes | None] = []
        self.trace: list[str] = []

    # -- helpers -------------------------------------------------------
    def slug(self, ws: dict) -> str:
        return workstream_dir_slug(ws["name"], ws["short_code"])

    def rows(self) -> list[dict]:
        return [
            {**ws, "workspace_dir": self.slug(ws)}
            for ws in sorted(self.live.values(), key=lambda ws: ws["id"])
        ]

    @property
    def map_path(self) -> Path:
        return self.workspace / MAP_DIRNAME / MAP_FILENAME

    def map(self) -> dict:
        path = self.map_path
        return json.loads(path.read_text()) if path.is_file() else {}

    def marker_of(self, directory: Path) -> str | None:
        """The workstream id ``directory``'s CLAUDE.md names; "" when it has
        a CLAUDE.md without one (an older daemon's), None without any."""
        claude = directory / "CLAUDE.md"
        if claude.is_symlink() or not claude.is_file():
            return None
        head = claude.read_text().split("\n")[:2]
        for owner in [*self.live, *self.deleted]:
            if len(head) > 1 and head[1] == MARKER.format(owner):
                return owner
        return ""

    def locate(self) -> dict[str, list[Path]]:
        found: dict[str, list[Path]] = {}
        if not self.workstreams.is_dir():
            return found
        for top, dirs, names in os.walk(self.workstreams):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(top, d))]
            for name in names:
                if name in self.files:
                    found.setdefault(name, []).append(Path(top) / name)
        return found

    def unique_slugs(self) -> bool:
        slugs = [self.slug(ws) for ws in self.live.values()]
        return len(slugs) == len(set(slugs))

    # -- backend changes (applied without a sync) ------------------------
    def create(self) -> None:
        if len(self.live) >= 5:
            return
        self.codes += 1
        code = "A" + chr(ord("A") + self.codes % 26) + str(self.codes)
        workstream_id = str(uuid.UUID(int=self.codes))
        used = {self.slug(ws) for ws in self.live.values()}
        names = [name for name in NAMES if workstream_dir_slug(name, code) not in used]
        name = self.rng.choice(names)
        freed = self.freed_codes.get(name)
        if (
            freed
            and freed not in {ws["short_code"] for ws in self.live.values()}
            and workstream_dir_slug(name, freed) not in used
            and self.rng.random() < 0.5
        ):
            code = freed
        self.live[workstream_id] = {
            "id": workstream_id,
            "name": name,
            "short_code": code,
        }
        self.changes += 1
        self.trace.append(f"create {workstream_id[-4:]} {name} {code}")

    def rename(self) -> None:
        if not self.live:
            return
        workstream_id = self.rng.choice(sorted(self.live))
        ws = self.live[workstream_id]
        used = {
            self.slug(other) for key, other in self.live.items() if key != workstream_id
        }
        names = [
            name
            for name in NAMES
            if name != ws["name"]
            and workstream_dir_slug(name, ws["short_code"]) not in used
        ]
        if not names:
            return
        ws["name"] = self.rng.choice(names)
        self.changes += 1
        self.trace.append(f"rename {workstream_id[-4:]} -> {ws['name']}")

    def swap(self) -> None:
        if len(self.live) < 2:
            return
        first, second = self.rng.sample(sorted(self.live), 2)
        a, b = self.live[first], self.live[second]
        a["name"], b["name"] = b["name"], a["name"]
        if not self.unique_slugs():
            a["name"], b["name"] = b["name"], a["name"]
            return
        self.changes += 2
        self.counters["swap"] += 1
        self.trace.append(f"swap {first[-4:]} <-> {second[-4:]}")

    def chain(self) -> None:
        """``first`` takes ``second``'s name, which takes a free one."""
        if len(self.live) < 2:
            return
        first, second = self.rng.sample(sorted(self.live), 2)
        a, b = self.live[first], self.live[second]
        old_a, old_b = a["name"], b["name"]
        a["name"] = old_b
        free = [name for name in NAMES if name not in (old_a, old_b)]
        self.rng.shuffle(free)
        for name in free:
            b["name"] = name
            if self.unique_slugs():
                break
        else:
            a["name"], b["name"] = old_a, old_b
            return
        self.changes += 2
        self.counters["chain"] += 1
        self.trace.append(
            f"chain {first[-4:]} -> {old_b}, {second[-4:]} -> {b['name']}"
        )

    def delete(self) -> None:
        if not self.live:
            return
        workstream_id = self.rng.choice(sorted(self.live))
        gone = self.live.pop(workstream_id)
        self.freed_codes[gone["name"]] = gone["short_code"]
        self.deleted.add(workstream_id)
        self.gone[workstream_id] = dict(gone)
        self.previous.pop(workstream_id, None)
        self.changes += 1
        self.trace.append(f"delete {workstream_id[-4:]}")

    def reclaim(self, name: str | None = None) -> None:
        """A live workstream takes a deleted one's directory name (``name``,
        or any): a new one created with its name and short code (the backend
        only avoids live codes), or a rename to it. The name may still hold
        the deleted workstream's content (its archive failed, or it has not
        synced)."""
        used = {self.slug(ws) for ws in self.live.values()}
        live_codes = {ws["short_code"] for ws in self.live.values()}
        candidates = sorted(
            key
            for key, gone in self.gone.items()
            if self.slug(gone) not in used and name in (None, self.slug(gone))
        )
        if not candidates:
            return
        gone = self.gone[self.rng.choice(candidates)]
        target = self.slug(gone)
        renames = [
            key
            for key, ws in sorted(self.live.items())
            if workstream_dir_slug(gone["name"], ws["short_code"]) == target
            and ws["name"] != gone["name"]
        ]
        if renames and (self.rng.random() < 0.5 or len(self.live) >= 5):
            workstream_id = self.rng.choice(renames)
            self.live[workstream_id]["name"] = gone["name"]
            self.changes += 1
            self.trace.append(
                f"rename {workstream_id[-4:]} -> {gone['name']} (reclaim)"
            )
            return
        if len(self.live) >= 5 or gone["short_code"] in live_codes:
            return
        self.codes += 1
        workstream_id = str(uuid.UUID(int=self.codes))
        self.live[workstream_id] = {
            "id": workstream_id,
            "name": gone["name"],
            "short_code": gone["short_code"],
        }
        self.changes += 1
        self.trace.append(
            f"create {workstream_id[-4:]} {gone['name']} {gone['short_code']} (reclaim)"
        )

    # -- syncs -----------------------------------------------------------
    def sync(self, mode: str = "ok") -> None:
        """One daemon sync; ``mode`` injects a lost map save."""
        Map = workstream_dirs.WorkstreamDirectoryMap
        original = Map.write_payload
        records_left = self.rng.randint(0, 3)

        def full_disk(self_, payload: dict, *, strict: bool = False) -> None:
            # The disk fills up at the final save (``enospc_final``), or
            # after 0-3 write-ahead records (``enospc_mid``): that write and
            # every later one fail.
            nonlocal records_left
            record = "pending" in payload
            if mode == "enospc_mid" and record and records_left > 0:
                records_left -= 1
                return original(self_, payload, strict=strict)
            if mode == "enospc_mid" or not record:
                raise OSError(errno.ENOSPC, "No space left on device")
            return original(self_, payload, strict=strict)

        def crash(self_, *, strict: bool = False) -> None:
            raise Crash

        coalesced = self.changes > 1
        with ExitStack() as stack:
            if mode == "crash":
                stack.enter_context(mock.patch.object(Map, "save", crash))
            elif mode != "ok":
                stack.enter_context(mock.patch.object(Map, "write_payload", full_disk))
            try:
                ClaudeMdWriter(str(self.workspace)).sync_workstream_directories(
                    self.rows()
                )
            except Crash:
                assert mode == "crash"
            except OSError as exc:
                # 4. Only the injected full disk may fail a sync.
                assert (
                    mode.startswith("enospc") and exc.errno == errno.ENOSPC
                ), f"sync raised {exc!r}"
            else:
                if mode == "ok" and coalesced:
                    self.counters["coalesced_sync"] += 1
            if mode in ("ok", "crash", "enospc_final"):
                # Every move was made (only the final save may be lost).
                self.synced()
        self.counters[f"sync_{mode}"] += 1
        self.trace.append(f"sync ({mode})")

    def synced(self) -> None:
        """Every backend change is synced: remember each renamed
        workstream's previous directory for late writes."""
        for workstream_id, ws in self.live.items():
            slug = self.slug(ws)
            before = self.synced_slugs.get(workstream_id)
            if before and before != slug:
                self.previous[workstream_id] = before
        self.synced_slugs = {key: self.slug(ws) for key, ws in self.live.items()}
        self.changes = 0

    # -- writes ----------------------------------------------------------
    def write(self) -> None:
        """Workers and backend projections write through the declared
        ``workspace_dir`` once the daemon synced it."""
        if self.changes:
            return
        for workstream_id, ws in sorted(self.live.items()):
            directory = self.workstreams / self.slug(ws)
            if directory.is_symlink() or (
                directory.exists() and not directory.is_dir()
            ):
                continue
            if directory.is_dir():
                marker = self.marker_of(directory)
                retired_to = self.map().get("retired", {}).get(directory.name)
                if (
                    self.is_unreadable(directory)
                    or marker not in (None, workstream_id)
                    or (marker is None and retired_to not in (None, workstream_id))
                ):
                    # A name the daemon keeps frozen for another workstream's
                    # content (its CLAUDE.md, or late writes not merged
                    # forward yet): the documented limitation (real workers
                    # would still write there).
                    self.counters["frozen_write_skipped"] += 1
                    continue
            name = f"t{len(self.files)}-{workstream_id[-4:]}.txt"
            target = directory / self.rng.choice(("tasks", "intake", "outputs"))
            target.mkdir(parents=True, exist_ok=True)
            (target / name).write_text(workstream_id)
            self.files[name] = workstream_id
            self.counters["workspace_dir_write"] += 1
        self.trace.append("write")

    def late_write(self) -> None:
        """A session started before a rename writes into the previous
        directory (the map retires that name to the workstream)."""
        if self.changes or not self.previous:
            return
        workstream_id = self.rng.choice(sorted(self.previous))
        previous = self.previous[workstream_id]
        directory = self.workstreams / previous
        taken = {self.slug(ws) for ws in self.live.values()}
        if (
            previous in taken
            or directory.exists()
            or directory.is_symlink()
            or self.map().get("retired", {}).get(previous) != workstream_id
        ):
            return
        name = f"late{len(self.files)}-{workstream_id[-4:]}.txt"
        (directory / "tasks").mkdir(parents=True)
        (directory / "tasks" / name).write_text(workstream_id)
        self.files[name] = workstream_id
        self.counters["late_write"] += 1
        self.trace.append(f"late write {workstream_id[-4:]} into {previous}")

    # -- session-caused conditions ---------------------------------------
    def break_archive(self) -> None:
        if self.archive_broken:
            return
        archive = self.workstreams / ".archived"
        self.workstreams.mkdir(parents=True, exist_ok=True)
        if archive.is_dir():
            archive.rename(self.workstreams / ARCHIVE_HOLD)
        archive.write_text("not a directory")
        self.archive_broken = True
        self.trace.append("break .archived")

    def repair_archive(self) -> None:
        if not self.archive_broken:
            return
        (self.workstreams / ".archived").unlink()
        hold = self.workstreams / ARCHIVE_HOLD
        if hold.is_dir():
            hold.rename(self.workstreams / ".archived")
        self.archive_broken = False
        self.trace.append("repair .archived")

    def plant_link(self) -> None:
        self.workstreams.mkdir(parents=True, exist_ok=True)
        candidates = [
            slug
            for name in NAMES
            for slug in {slugify(name), workstream_dir_slug(name, "AB")}
            if not (self.workstreams / slug).exists()
            and not (self.workstreams / slug).is_symlink()
        ]
        if not candidates:
            return
        name = self.rng.choice(candidates)
        (self.workstreams / name).symlink_to(self.outside)
        self.links.add(name)
        self.trace.append(f"plant link {name}")

    def remove_links(self) -> None:
        for name in sorted(self.links):
            link = self.workstreams / name
            if link.is_symlink():
                link.unlink()
        self.links.clear()
        self.trace.append("remove links")

    @staticmethod
    def is_unreadable(directory: Path) -> bool:
        claude = directory / "CLAUDE.md"
        if claude.is_symlink() or not claude.is_file():
            return False
        return UNREADABLE_TAG in claude.read_text()

    def make_unreadable(self) -> None:
        """A session makes a CLAUDE.md unreadable to the daemon (as when the
        daemon does not run as root and the file is chmod 000)."""
        if not self.workstreams.is_dir():
            return
        candidates = sorted(
            child.name
            for child in self.workstreams.iterdir()
            if not child.name.startswith(".")
            and child.is_dir()
            and not child.is_symlink()
            and (child / "CLAUDE.md").is_file()
            and not (child / "CLAUDE.md").is_symlink()
            and not self.is_unreadable(child)
        )
        if candidates:
            name = self.rng.choice(candidates)
            claude = self.workstreams / name / "CLAUDE.md"
            claude.write_text(claude.read_text() + UNREADABLE_TAG)
            self.unreadable = True
            self.trace.append(f"unreadable {name}/CLAUDE.md")

    def make_readable(self) -> None:
        if not self.unreadable:
            return
        for claude in self.workstreams.rglob("CLAUDE.md"):
            if claude.is_file() and not claude.is_symlink():
                text = claude.read_text()
                if UNREADABLE_TAG in text:
                    claude.write_text(text.replace(UNREADABLE_TAG, ""))
        self.unreadable = False
        self.trace.append("CLAUDE.md files readable again")

    def attribution(self, original):
        def read(root_fd: int, name: str):
            try:
                if not stat.S_ISLNK(
                    os.stat(name, dir_fd=root_fd, follow_symlinks=False).st_mode
                ):
                    fd = os.open(
                        f"{name}/CLAUDE.md", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd
                    )
                    try:
                        head = os.read(fd, 1 << 16)
                    finally:
                        os.close(fd)
                    if UNREADABLE_TAG.encode() in head:
                        raise PermissionError(errno.EACCES, "Permission denied", name)
            except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
                pass
            except OSError as exc:
                if isinstance(exc, PermissionError):
                    raise
            return original(root_fd, name)

        return read

    def legacy_heading(self, original):
        """The rollback helper's own CLAUDE.md heading read fails the same way
        (it then knows no legacy name)."""

        def read(root_fd: int, directory: str):
            try:
                self.attribution(lambda *_args: None)(root_fd, directory)
            except PermissionError:
                return None
            return original(root_fd, directory)

        return read

    def manual_archive(self) -> None:
        """A session (or the user) moves a deleted workstream's directory
        the daemon still claims into the archive by hand; the name may be
        reused before the daemon notices."""
        claims = [
            name
            for name in self.map().get("deleted", {}).values()
            if not name.startswith(".")
        ]
        candidates = [
            name
            for name in claims
            if (self.workstreams / name).is_dir()
            and not (self.workstreams / name).is_symlink()
            and not any(
                self.files.get(path.name) in self.live
                for path in (self.workstreams / name).rglob("*")
            )
        ]
        if not candidates:
            return
        name = self.rng.choice(candidates)
        archive = self.workstreams / (
            ARCHIVE_HOLD if self.archive_broken else ".archived"
        )
        archive.mkdir(exist_ok=True)
        (self.workstreams / name).rename(archive / f"manual-{len(self.trace)}")
        self.trace.append(f"archive {name} by hand")
        if self.rng.random() < 0.5:
            # The name is reused before the daemon notices.
            self.reclaim(name)

    def wrap_reconcile(self, original):
        """Counts the passes that froze a name (a claimant waits)."""

        def reconcile(*args, **kwargs):
            result = original(*args, **kwargs)
            deleted_frozen = {
                name
                for name in result.deleted.values()
                if not name.startswith(DELETED_PREFIX) and name in result.blocked
            }
            if result.foreign or deleted_frozen:
                self.counters["frozen_name"] += 1
            return result

        return reconcile

    # -- operator actions ------------------------------------------------
    def backup_map(self) -> None:
        self.backups = [
            *self.backups[-2:],
            self.map_path.read_bytes() if self.map_path.is_file() else None,
        ]
        self.trace.append("back up the map")

    def restore_map(self) -> None:
        """The operator restores an older copy of the map (or none)."""
        if not self.backups:
            return
        backup = self.rng.choice(self.backups)
        if backup is None:
            if self.map_path.is_file():
                self.map_path.unlink()
        else:
            self.map_path.parent.mkdir(parents=True, exist_ok=True)
            self.map_path.write_bytes(backup)
        # Every directory recorded since is unattributed now: its content is
        # never lost or mixed, but may be archived.
        self.excused |= set(self.files)
        self.previous.clear()
        self.changes += 1  # the daemon must sync before anyone writes
        self.trace.append("restore the map from a backup")

    def rollback(self) -> None:
        """prepare-rollback --apply; on exit 0 the older daemon runs, then a
        re-upgrade sync. On a non-zero exit the operator restarts the
        current daemon instead.

        The operator stops the daemon once it has synced the backend's
        current names: an older daemon deletes a renamed workstream's old
        directory by itself (the defect workstream_dirs_v1 fixed), so a
        rename it syncs is outside what the helper can protect."""
        if self.changes:
            self.sync()
        plan = plan_rollback(self.workspace)
        where = self.locate()
        for step in plan.steps:
            if step.action == "archive":
                self.excused |= {
                    name
                    for name, paths in where.items()
                    if any(
                        path.relative_to(self.workstreams).parts[0] == step.source
                        for path in paths
                    )
                }
        outcome = apply_rollback(plan)
        clean = not plan.warnings and not outcome.failed
        if not clean:
            self.counters["helper_refusal"] += 1
        if any("interrupted sync" in warning for warning in plan.warnings):
            self.counters["helper_pending_refusal"] += 1
        self.trace.append(
            f"rollback exit={'0' if clean else 'non-zero'} "
            f"steps={[(s.action, s.source, s.target) for s in plan.steps]} "
            f"warnings={len(plan.warnings)} failed={len(outcome.failed)}"
        )
        if not clean:
            self.sync()
            return
        # The older layout mixes the directories of workstreams whose
        # slugify(name) collides: their content cannot be split later.
        legacy: dict[str, list[str]] = {}
        for workstream_id, ws in self.live.items():
            legacy.setdefault(slugify(ws["name"]), []).append(workstream_id)
        mixed = {key for ids in legacy.values() if len(ids) > 1 for key in ids}
        self.excused |= {name for name, owner in self.files.items() if owner in mixed}
        self.old_daemon_sync()
        self.check(old_layout=True)
        self.previous.clear()
        self.changes += 1
        self.sync()

    def old_daemon_sync(self) -> None:
        """The workstream pass of a daemon older than workstream_dirs_v1
        (claude_md_writer at 1e5d05c4): slugify names, delete every other
        directory unless it holds spec.md/learnings.md."""
        root = self.workstreams
        root.mkdir(parents=True, exist_ok=True)
        seen: set[str] = set()
        for ws in self.live.values():
            slug = slugify(ws["name"])
            seen.add(slug)
            target = root / slug
            if target.is_symlink():
                continue  # the older daemon would write through it; not modelled
            target.mkdir(exist_ok=True)
            (target / "CLAUDE.md").write_text(
                f"# Workstream: {ws['name']}\n\n**Short code:** `{ws['short_code']}`\n"
            )
        if not seen and any(child.is_dir() for child in root.iterdir()):
            # The older daemon's own empty-sync guard (CTX-03).
            self.trace.append("older daemon sync (empty list: no cleanup)")
            return
        for child in root.iterdir():
            if child.is_symlink() or not child.is_dir():
                continue
            if child.name in seen or child.name == ".archived":
                continue
            # A dot-directory (a staged move, a deleted directory moved
            # aside) is swept too: it has no top-level spec.md.
            if any((child / name).exists() for name in ("spec.md", "learnings.md")):
                continue
            shutil.rmtree(child)
        self.trace.append("older daemon sync")

    # -- invariants ------------------------------------------------------
    def check(self, *, old_layout: bool = False) -> None:
        where = self.locate()
        # 2. Nothing is lost.
        lost = sorted(name for name in self.files if name not in where)
        assert not lost, f"files lost: {lost}"
        # 1. No directory attributed to a workstream holds another's file.
        for ws in self.live.values():
            if old_layout:
                slug = slugify(ws["name"])
                if sum(slugify(o["name"]) == slug for o in self.live.values()) > 1:
                    continue  # the older layout shares such a directory
                directory = self.workstreams / slug
                if not directory.is_dir() or directory.is_symlink():
                    continue
                self._assert_only(directory, ws["id"])
        if not old_layout and self.workstreams.is_dir():
            for directory in self.workstreams.iterdir():
                if directory.is_symlink() or not directory.is_dir():
                    continue
                owner = self.marker_of(directory)
                if owner:
                    self._assert_only(directory, owner)
        # 3. No live workstream's file is archived (unless predicted).
        for name, paths in where.items():
            owner = self.files[name]
            if owner not in self.live or name in self.excused:
                continue
            for path in paths:
                top = path.relative_to(self.workstreams).parts[0]
                assert top not in (".archived", ARCHIVE_HOLD), (
                    f"{path.relative_to(self.workstreams)} of live "
                    f"{owner[-4:]} was archived"
                )
        # The link target outside the workspace is never touched.
        assert sorted(p.name for p in self.outside.iterdir()) == ["host.txt"]

    def _assert_only(self, directory: Path, owner: str) -> None:
        for path in directory.rglob("*"):
            other = self.files.get(path.name)
            if other and other != owner and path.name not in self.excused:
                raise AssertionError(
                    f"{path.relative_to(self.workstreams)} of {other[-4:]} is in "
                    f"the directory of {owner[-4:]}"
                )

    def settle(self) -> None:
        """With every cause repaired, syncs put every live workstream's
        files in its directory and archive every deleted one's."""
        self.repair_archive()
        self.remove_links()
        self.make_readable()
        if not self.live:
            # A sync with no workstreams never acts (CTX-03), so the office
            # needs one live workstream to settle. Its name is one no
            # directory can already carry.
            self.codes += 1
            code = f"ZZ{self.codes}"
            workstream_id = str(uuid.UUID(int=self.codes))
            self.live[workstream_id] = {
                "id": workstream_id,
                "name": "Settle",
                "short_code": code,
            }
            self.trace.append(f"create {workstream_id[-4:]} Settle {code}")
        for _ in range(3):
            self.sync()
            self.check()
        recorded = self.map()
        assert (
            recorded.get("deleted", {}) == {}
        ), f"deleted claims never archived: {recorded['deleted']}"
        assert not recorded.get("staged") and not recorded.get(
            "pending"
        ), f"unfinished moves: {recorded.get('staged')} {recorded.get('pending')}"
        leftovers = sorted(
            child.name
            for child in self.workstreams.iterdir()
            if child.name.startswith((RELOCATING_PREFIX, DELETED_PREFIX))
        )
        assert not leftovers, f"left behind: {leftovers}"
        where = self.locate()
        for name, owner in self.files.items():
            for path in where[name]:
                top = path.relative_to(self.workstreams).parts[0]
                if owner in self.deleted:
                    assert top == ".archived", (
                        f"{path.relative_to(self.workstreams)} of deleted "
                        f"{owner[-4:]} is not archived"
                    )
                elif name not in self.excused:
                    assert top == self.slug(self.live[owner]), (
                        f"{path.relative_to(self.workstreams)} of live "
                        f"{owner[-4:]} is not in its directory "
                        f"{self.slug(self.live[owner])}"
                    )


OPERATIONS = (
    ("create", 4),
    ("rename", 4),
    ("swap", 2),
    ("chain", 2),
    ("delete", 3),
    ("reclaim", 2),
    ("write", 6),
    ("late_write", 2),
    ("break_archive", 2),
    ("repair_archive", 1),
    ("plant_link", 1),
    ("remove_links", 1),
    ("make_unreadable", 2),
    ("make_readable", 1),
    ("backup_map", 1),
    ("restore_map", 1),
    ("manual_archive", 4),
    ("sync", 9),
    ("rollback", 1),
)
SYNC_MODES = (
    ("ok", 6),
    ("crash", 2),
    ("enospc_final", 1),
    ("enospc_mid", 2),
)


def _skip_fsync(descriptor: int) -> None:
    """``os.fsync`` for the model: the invariants concern the injected
    crashes and full disks, never real power loss, so durability is not
    needed. The descriptor is still checked (EBADF raises as before), but
    the run no longer waits on the disk (B7b-tests-01: about half the time,
    and the part a slow CI disk stretched past the timeout)."""
    os.fstat(descriptor)


def run_sequence(seed: int, counters: Counter) -> Office:
    rng = random.Random(seed)
    base = Path(tempfile.mkdtemp(prefix=f"cubicle-model-{seed}-"))
    office = Office(base, rng, counters)
    handler = BranchCounter(counters)
    logger = logging.getLogger(workstream_dirs.__name__)
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    reads = office.attribution(workstream_dirs.directory_attribution)
    headings = office.legacy_heading(workstream_dirs_rollback._legacy_from_claude_md)
    reconcile = office.wrap_reconcile(workstream_dirs.reconcile_workstream_directories)
    try:
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(os, "fsync", _skip_fsync))
            clock = model_clock(seed)
            for module in (workstream_dirs, workstream_dirs_rollback):
                stack.enter_context(
                    mock.patch.object(module, "directory_attribution", reads)
                )
                stack.enter_context(mock.patch.object(module, "datetime", clock))
            stack.enter_context(
                mock.patch.object(
                    workstream_dirs_rollback, "_legacy_from_claude_md", headings
                )
            )
            stack.enter_context(
                mock.patch.object(
                    workstream_dirs, "reconcile_workstream_directories", reconcile
                )
            )
            office.workspace.mkdir()
            office.create()
            office.sync()
            names = [name for name, _weight in OPERATIONS]
            weights = [weight for _name, weight in OPERATIONS]
            modes = [mode for mode, _weight in SYNC_MODES]
            mode_weights = [weight for _mode, weight in SYNC_MODES]
            for _ in range(STEPS_PER_SEED):
                operation = rng.choices(names, weights)[0]
                if operation == "sync":
                    office.sync(rng.choices(modes, mode_weights)[0])
                else:
                    getattr(office, operation)()
                office.check()
            office.settle()
        return office
    except AssertionError as exc:
        raise AssertionError(
            f"seed {seed}: {exc}\n  " + "\n  ".join(office.trace)
        ) from None
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.timeout(MODEL_TIMEOUT_SECONDS)
def test_random_sequences_keep_every_invariant_and_reach_every_branch() -> None:
    counters: Counter = Counter()
    seeds = [int(ONE_SEED)] if ONE_SEED else range(FIRST, FIRST + SEEDS)
    for seed in seeds:
        run_sequence(seed, counters)
    print(f"\nworkstream directory model, {len(seeds)} seeds: {dict(counters)}")
    if ONE_SEED:
        return
    missing = [branch for branch in REQUIRED_BRANCHES if not counters[branch]]
    assert not missing, f"branches never reached: {missing} ({dict(counters)})"
