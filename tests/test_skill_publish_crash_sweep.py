"""Crash/failure injection at every filesystem step of a skill commit (F03).

Each run publishes a skill, lets the user edit it, then publishes a new
version while ONE filesystem step of the helper's ``fs_skill_commit`` fails:
the helper process dies there (``crash``), the call raises ``EIO``
(``error``), or the helper deadline expires (``timeout``). Every step is
tried, on each swap path — ``RENAME_EXCHANGE``, the journaled two-rename
fallback, a first install with ``RENAME_NOREPLACE`` and its plain-rename
fallback.

Two families of runs:

* **backend** — the real backend publisher drives the real helper (the
  daemon transport is replaced by a direct call; a dead helper answers 503
  like the daemon's relay). Its result is compared with what is live.
* **raw** — the backend is gone after sending the commit: recovery is left
  to whichever helper action comes next (status, discovery, abort, or a
  Files write under the skills root), tried first in turn.

After each run the invariants must hold:

(a) a complete skill is live — exactly the old one or the new one;
(b) the user's edit is never removed without being reported: it is live,
    or the whole old version sits in ``retired/`` and a backend success
    says ``replaced_local_changes``;
(c) the backend's answer matches what is live (success ⇔ the new version);
(d) no journal outlives recovery, and no staging outlives the backend's
    flow (raw runs: recovery plus the stale-staging prune).

Linux lane only (``renameat2``, ``/proc`` mount ids). The backend package
must be importable (``PYTHONPATH`` includes ``backend``).
"""

from __future__ import annotations

import asyncio
import base64
import errno
import functools
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from src._agent_image import secure_files as files
from tests.backend_boundary import import_backend

publisher = import_backend("app.skills.publisher")
bundles = import_backend("app.skills.bundles")
office_fs = import_backend("app.transport.office_fs")

SKILL = "demo"
OLD = {
    "SKILL.md": b"---\nname: demo\ndescription: Demo. Use when testing.\n---\nv1\n",
    "old.md": b"old resource\n",
}
EDIT = b"USER EDIT\n"
NEW = {
    "SKILL.md": b"---\nname: demo\ndescription: Demo. Use when testing.\n---\nv2\n",
    "new/deep.md": b"new resource\n",
}
PATHS = ("exchange", "journaled", "noreplace", "rename")
KINDS = ("crash", "error", "timeout")
TRIGGERS = ("status", "discovery", "abort", "write")
_SOURCE = {"kind": "catalog_github", "revision": "git:" + "c" * 40}


class SimulatedCrash(BaseException):
    """The helper process dying at an injection point."""


class Injector:
    """Counts the commit's filesystem steps and fails the chosen one."""

    def __init__(self) -> None:
        self.active = False
        self.count = 0
        self.target: int | None = None
        self.kind = "crash"
        self.fired: str | None = None

    def step(self, operation: str) -> None:
        if not self.active:
            return
        self.count += 1
        if self.fired is None and self.count == self.target:
            self.fired = operation
            if self.kind == "crash":
                raise SimulatedCrash(operation)
            if self.kind == "timeout":
                raise TimeoutError(f"injected at {operation}")
            raise OSError(errno.EIO, f"injected at {operation}")

    @contextmanager
    def armed(self):
        self.active = True
        try:
            yield
        finally:
            self.active = False


def _unsupported(*_args):
    raise files.UnsupportedFilesystemError("flags unsupported")


def _skip_fsync(descriptor: int) -> None:
    """A counted injection point without the disk wait (B7b-tests-01): the
    sweep simulates process death and I/O errors, never power loss, so real
    durability is not needed. A bad descriptor still raises (EBADF)."""
    os.fstat(descriptor)


def _install_injector(monkeypatch, injector: Injector, path: str) -> None:
    def counted(name, real, *, create_only=False):
        def wrapper(*args, **kwargs):
            if create_only:
                flags = args[1] if len(args) > 1 else kwargs.get("flags", 0)
                if flags & os.O_CREAT:
                    injector.step(name)
            else:
                injector.step(name)
            return real(*args, **kwargs)

        return wrapper

    for name in ("rename", "replace", "mkdir", "unlink", "rmdir"):
        monkeypatch.setattr(os, name, counted(name, getattr(os, name)))
    # Still a counted step (the step targets are unchanged), but no real fsync.
    monkeypatch.setattr(os, "fsync", counted("fsync", _skip_fsync))
    monkeypatch.setattr(os, "fchmod", counted("fchmod", os.fchmod))
    monkeypatch.setattr(os, "write", counted("write", os.write))
    monkeypatch.setattr(os, "open", counted("open", os.open, create_only=True))
    if path in ("exchange", "noreplace"):
        monkeypatch.setattr(files, "_renameat2", counted("renameat2", files._renameat2))
    else:
        monkeypatch.setattr(files, "_renameat2", _unsupported)


def _bundle(content: dict[str, bytes]):
    return bundles.build_bundle(
        SKILL, [(path, data, 0o644) for path, data in content.items()], _SOURCE
    )


def _manifest(content: dict[str, bytes]) -> dict:
    return bundles.manifest_payload(_bundle(content))


class Office:
    """One workspace, reached like the backend reaches the daemon."""

    def __init__(self, root: Path, injector: Injector) -> None:
        self.root = root
        self.injector = injector
        self.calls: list[tuple[str, dict]] = []
        self.commit_armed = False

    def execute(self, action: str, **params) -> dict:
        return files.execute({"action": action, "params": params}, self.root)

    async def fs_call(
        self, office_id, action, params, *, timeout=None, busy_delays=None
    ):
        del office_id, timeout, busy_delays
        self.calls.append((action, dict(params)))
        arm = action == "fs_skill_commit" and self.commit_armed
        self.commit_armed = self.commit_armed and not arm
        try:
            if arm:
                with self.injector.armed():
                    return self.execute(action, **params)
            return self.execute(action, **params)
        except SimulatedCrash:
            # The daemon relays a dead helper as 503 (fs_handler).
            return {"error": "Secure Files unavailable", "status": 503}

    # -- inspection -------------------------------------------------------

    @property
    def area(self) -> Path:
        return self.root / ".claude" / ".cubicle-skill-bundles"

    def folder_files(self, folder: Path) -> dict[str, bytes] | None:
        if not folder.is_dir():
            return None
        found = {}
        for path in folder.rglob("*"):
            if path.is_file() and not path.is_symlink():
                relative = str(path.relative_to(folder))
                if relative in (".cubicle-bundle.json", "params.json"):
                    continue
                found[relative] = path.read_bytes()
        return found

    def live(self) -> dict[str, bytes] | None:
        return self.folder_files(self.root / ".claude" / "skills" / SKILL)

    def retired_versions(self) -> list[dict[str, bytes]]:
        retired = self.area / "retired" / SKILL
        if not retired.is_dir():
            return []
        return [self.folder_files(entry) or {} for entry in retired.iterdir()]

    def leftovers(self, kind: str) -> list[str]:
        folder = self.area / kind
        return sorted(os.listdir(folder)) if folder.is_dir() else []


def _publish_raw(office: Office, content: dict[str, bytes], expected: str) -> tuple:
    publication_id = uuid.uuid4().hex
    begun = office.execute(
        "fs_skill_stage_begin", publication_id=publication_id, skill_name=SKILL
    )
    assert begun.get("staged") is True, begun
    put = office.execute(
        "fs_skill_stage_put",
        publication_id=publication_id,
        files=[
            {
                "path": path,
                "offset": 0,
                "data_base64": base64.b64encode(data).decode(),
            }
            for path, data in content.items()
        ],
    )
    assert "error" not in put, put
    params = {
        "publication_id": publication_id,
        "skill_name": SKILL,
        "manifest": _manifest(content),
        "mode": "update",
        "expected_live_digest": expected,
    }
    return publication_id, params


def _prepare(office: Office, path: str) -> dict[str, bytes] | None:
    """Publish and edit the old version; returns what is live before."""
    (office.root / ".claude" / "skills").mkdir(parents=True)
    if path in ("noreplace", "rename"):
        return None
    publication_id, params = _publish_raw(office, OLD, "none")
    committed = office.execute("fs_skill_commit", **params)
    assert committed["status"] == "published", committed
    (office.root / ".claude" / "skills" / SKILL / "notes.md").write_bytes(EDIT)
    return {**OLD, "notes.md": EDIT}


def _trigger(office: Office, trigger: str, publication_id: str) -> None:
    if trigger == "status":
        office.execute("fs_skill_status", skill_name=SKILL)
    elif trigger == "discovery":
        office.execute("fs_list_skills")
    elif trigger == "abort":
        office.execute(
            "fs_skill_abort", publication_id=publication_id, skill_name=SKILL
        )
    else:
        office.execute(
            "fs_write", path=".claude/skills/other/SKILL.md", content="# other\n"
        )


def _check_live(office: Office, before: dict | None, label: str) -> str:
    """(a) and (b); returns "old" or "new"."""
    live = office.live()
    if live == NEW:
        if before is not None:
            assert (
                before in office.retired_versions()
            ), f"{label}: the replaced version (with the user's edit) is gone"
        return "new"
    assert live == before, f"{label}: live is neither version: {live!r}"
    return "old"


def _check_clean(office: Office, label: str, *, staging: bool) -> None:
    """(d)."""
    assert office.leftovers("journal") == [], f"{label}: a journal outlived recovery"
    if staging:
        assert (
            office.leftovers("staging") == []
        ), f"{label}: staging outlived recovery: {office.leftovers('staging')}"


def _step_count(path: str) -> int:
    injector = Injector()
    root = Path(tempfile.mkdtemp()) / "workspace"
    try:
        with pytest.MonkeyPatch.context() as patch:
            _install_injector(patch, injector, path)
            office = Office(root, injector)
            before = _prepare(office, path)
            expected = "none" if before is None else _live_digest(office)
            _pid, params = _publish_raw(office, NEW, expected)
            with injector.armed():
                result = office.execute("fs_skill_commit", **params)
            assert result["status"] == "published", result
            assert result["swap"] == path, result
        return injector.count
    finally:
        shutil.rmtree(root.parent)


def _live_digest(office: Office) -> str:
    return office.execute("fs_skill_status", skill_name=SKILL)["live"]["digest"]


def _run_backend(path: str, kind: str, target: int) -> str | None:
    """Backend family; returns the injected operation (None: past the end)."""
    injector = Injector()
    injector.kind, injector.target = kind, target
    root = Path(tempfile.mkdtemp()) / "workspace"
    label = f"backend/{path}/{kind}@{target}"
    try:
        with pytest.MonkeyPatch.context() as patch:
            _install_injector(patch, injector, path)
            office = Office(root, injector)
            before = _prepare(office, path)
            patch.setattr(office_fs, "fs_call", office.fs_call)
            patch.setattr(publisher, "supports_bundles", lambda _office_id: True)
            office.commit_armed = True
            try:
                result = asyncio.run(
                    publisher.publish_bundle("office", _bundle(NEW), mode="update")
                )
                outcome = None
            except Exception as error:  # noqa: BLE001 - the refusal is checked
                result, outcome = None, error
            label = f"{label} ({injector.fired})"
            commits = [
                params for action, params in office.calls if action == "fs_skill_commit"
            ]
            publication_id = commits[0]["publication_id"]
            for trigger in TRIGGERS:
                _trigger(office, trigger, publication_id)
            state = _check_live(office, before, label)
            # (c) the backend's answer matches what is live.
            if outcome is None:
                assert state == "new", f"{label}: success reported, old live"
                assert result.bundle_sha256 == _bundle(NEW).bundle_sha256
                if before is not None:
                    assert (
                        result.replaced_local_changes
                    ), f"{label}: the user's edit was replaced silently"
            else:
                assert (
                    state == "old"
                ), f"{label}: {type(outcome).__name__} reported, new live"
            _check_clean(office, label, staging=True)
            return injector.fired
    finally:
        shutil.rmtree(root.parent)


def _run_raw(path: str, kind: str, target: int, first: str) -> str | None:
    """Raw family: no backend after the commit; ``first`` recovers."""
    injector = Injector()
    injector.kind, injector.target = kind, target
    root = Path(tempfile.mkdtemp()) / "workspace"
    label = f"raw/{path}/{kind}@{target}/{first}"
    clock = [1_000_000_000.0]
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(files, "_now", lambda: clock[0])
            _install_injector(patch, injector, path)
            office = Office(root, injector)
            before = _prepare(office, path)
            expected = "none" if before is None else _live_digest(office)
            publication_id, params = _publish_raw(office, NEW, expected)
            with injector.armed():
                try:
                    office.execute("fs_skill_commit", **params)
                except SimulatedCrash:
                    pass
            label = f"{label} ({injector.fired})"
            order = (first, *(name for name in TRIGGERS if name != first))
            for trigger in order:
                _trigger(office, trigger, publication_id)
                _check_live(office, before, f"{label} after {trigger}")
                _check_clean(office, f"{label} after {trigger}", staging=False)
            # Kept staging is for a re-commit; the prune removes it later —
            # never a displaced version (checked by _check_live above).
            clock[0] += files.SKILL_STAGING_STALE_SECONDS + 1
            office.execute("fs_skill_status", skill_name=SKILL)
            _check_live(office, before, f"{label} after the prune")
            _check_clean(office, f"{label} after the prune", staging=True)
            return injector.fired
    finally:
        shutil.rmtree(root.parent)


@functools.lru_cache(maxsize=None)
def _steps(path: str) -> int:
    return _step_count(path)


# One case loops over every commit step and trigger: well above the
# suite-wide 30 s default on a slow runner (B7b-tests-01).
@pytest.mark.timeout(180)
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("path", PATHS)
def test_every_commit_step_fails_safely(path: str, kind: str) -> None:
    steps = _steps(path)
    assert steps >= 10, (path, steps)  # the sweep really covers the commit
    for target in range(1, steps + 1):
        fired = _run_backend(path, kind, target)
        assert fired is not None, (path, kind, target)
        for first in TRIGGERS:
            _run_raw(path, kind, target, first)
