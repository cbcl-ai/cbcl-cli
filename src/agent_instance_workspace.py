"""Materialize a task agent's retained instructions independently of its profile.

The private archive is outside the worker-mounted workspace. Rework restores
the same Profile-owned instructions and skill files; normal profile sync never
edits it. Credential/configuration files are excluded using the shared Files
policy. Declared non-secret parameter values refresh per attempt and are never
archived. The office's parent instructions and catalog remain visible; the
retained playbook paths are explicit guidance, not a filesystem isolation
boundary.

Archive formats (X51):

* Current archives keep only the Profile-owned parts (``profile.json``: the
  custom system prompt, office notes, role, advisory tool list, assigned skill
  metadata and the pinned Office work policy) plus the skill files. The
  task-Agent ``CLAUDE.md`` is rendered FRESH from the running daemon's
  platform templates on every attempt, so a daemon upgrade that changes
  lifecycle, blocker or reviewer rules reaches task Agents already in rework.
  Connectors are rendered from the attempt's live-filtered configuration: a
  revoked credential never reappears.
* Archives written before this change stored the fully rendered CLAUDE.md.
  They keep resuming byte-for-byte; their identity keys are unchanged.
* A current archive also keeps the CLAUDE.md rendered when it was created,
  listed in ``files`` like a legacy archive. The running daemon never
  restores that copy; it exists only so a daemon ROLLED BACK to the legacy
  format still restores the Agent's playbook instead of none.

Skill copies (F03 snapshot coordination, X15/X52):

* Each assigned skill is copied from one opened directory and then checked
  against the live one: the live ``.claude/skills/<name>`` must still be the
  same inode and the opened directory must still be linked. A failed check
  retries a bounded number of times; no lock is taken (``flock`` does not
  cross the Docker Desktop VM boundary). A passing check proves one complete
  old or new version ONLY for folders published whole by the
  ``skill_bundles_v1`` publisher (``backend/app/skills/publisher.py``, which
  catalog/GitHub installs, reinstalls and generated bundles use): it swaps
  whole directories (``renameat2`` exchange or a journaled two-rename
  fallback), never edits a live folder in place, and keeps a retired version
  for at least 30 minutes. Writers that keep the directory inode are NOT
  detected and can be captured mid-change and recorded as ``copied``:
  backends that predate whole-folder publication (their file-by-file
  install and reinstall), Skills-page and skill file-route saves, and direct
  agent edits.
* Only a skill's ROOT ``params.json`` (mutable parameter values) is left out;
  a nested ``params.json`` is a skill resource and is kept.
* A NEW snapshot continues without an assigned skill that is not installed
  or has no ``SKILL.md``. The archive manifest records it under ``skills``
  (with the ``bundle_sha256`` of every copied skill that carries a
  ``.cubicle-bundle.json``), the task-Agent CLAUDE.md names it, and the
  caller reports it on the task. Unsafe content (links, special files) still
  refuses the snapshot. Resuming an existing archive stays strict: every
  retained skill must be restored exactly as archived.

Restore (SEC-1, SEC-2): the task Agent directory is writable by every
session in the office while the daemon may run as root. The restore opens
``agents/.instances/<id>`` component by component with ``O_NOFOLLOW`` and
does everything else relative to that descriptor: descriptor-based removal,
``mkdir(dir_fd=)``, ``O_EXCL|O_NOFOLLOW`` file creation with ``fchmod`` and
``fchown`` on the new file, and rename-based ``params.json`` and
``settings.json`` replacement. ``.claude`` is recreated root-owned and handed
to the agent uid only after the daemon has finished writing into it. A link
swapped in anywhere on the way refuses the restore.

Unlike config sync, the restore does not use ``MaterializationFailures``:
config sync writes many independent entries and must not let one planted
entry pause the office, so it skips wrong-kind entries and reports the rest
at the end. A retained workspace is restored exactly or not at all, so any
error (a planted entry or an environmental failure) refuses this attempt at
once; the supervisor reports the cause and the task is retried.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import time
import uuid

from src._agent_image.secure_files import (
    _PROTECTED_FILE_PREFIXES,
    _PROTECTED_NAMES,
    _SKILL_SHA256,
    SKILL_BUNDLE_MANIFEST,
    SKILL_BUNDLE_MANIFEST_MAX_BYTES,
    protected_path,
)
from src.config_sync._descriptor_io import (
    DIR_FLAGS,
    atomic_replace_file,
    ensure_owned_directory,
    ensure_subdirectory,
    fchown_to_agent,
    open_dir_nofollow,
)
from src.config_sync.claude_md_writer import (
    ClaudeMdWriter,
    write_agent_hook_settings_at,
)
from src.config_sync.office_work_policy import OFFICE_WORK_POLICY_KEY
from src.orchestrator.worker_prompt import task_output_dir


# Archive-only file of the current format: the Profile-owned render inputs.
PROFILE_PARTS_FILE = "profile.json"
# The rendered instructions a legacy-format archive restores.
LEGACY_INSTRUCTIONS_FILE = "CLAUDE.md"
# Manifest marker of the current format (absent on legacy archives, which
# restore their archived CLAUDE.md byte-for-byte).
INSTRUCTIONS_FORMAT = "profile-parts-v1"
_PROFILE_OWNED_KEYS = (
    "name",
    "display_name",
    "agent_type",
    "role_description",
    "system_prompt",
    "claude_md_content",
    "allowed_tools",
)
_SKILL_KEYS = ("name", "display_name", "description", "parameter_schema")

logger = logging.getLogger(__name__)

# Bounded retry while a publisher swaps a whole skill directory (module
# docstring). The backoff doubles: 0.05 + 0.1 + 0.2 + 0.4 s at most.
SKILL_COPY_ATTEMPTS = 5
SKILL_COPY_BACKOFF_SECONDS = 0.05
_sleep = time.sleep
# Written into the archive for a skill a new snapshot continued without, so
# its directory exists after a restore (a rolled-back daemon refreshes
# params.json for every Profile skill). Never a playbook.
UNAVAILABLE_SKILL_FILE = "UNAVAILABLE.md"


class AgentWorkspaceError(RuntimeError):
    """A task Agent's retained workspace could not be prepared.

    The message is safe to show on the task: it names the cause without host
    paths.
    """


class WorkspaceRefusal(ValueError):
    """A refusal this module authored: its text never carries a host path.

    ``workspace_failure_text`` shows only these messages verbatim (SEC-4);
    any other exception text (pathlib, the OS, JSON) is replaced.
    """


def _resolved(path: Path) -> Path:
    """``Path.resolve()`` whose failure (a symlink loop) carries no path."""
    try:
        return path.resolve()
    except (RuntimeError, OSError):
        raise WorkspaceRefusal(
            "Agent workspace path has a symlink loop or cannot be resolved"
        ) from None


class _SkillUnavailable(Exception):
    """An assigned skill is not installed or has no SKILL.md."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def display_text(text: object, limit: int = 200) -> str:
    """One printable line for task activity and CLAUDE.md text."""
    printable = "".join(
        character if character.isprintable() and character != "`" else "?"
        for character in str(text)
    )
    return printable if len(printable) <= limit else printable[: limit - 1] + "…"


def workspace_failure_text(exc: BaseException) -> str:
    """The precise, path-free reason a task Agent workspace failed.

    Only messages this module (or the supervisor) authored are shown
    verbatim. OSError text carries host paths, so only its error description
    is kept; any other exception becomes a fixed phrase (SEC-4).
    """
    if isinstance(exc, (WorkspaceRefusal, AgentWorkspaceError)):
        detail = str(exc) or type(exc).__name__
    elif isinstance(exc, OSError):
        detail = exc.strerror or type(exc).__name__
    else:
        detail = f"unexpected {type(exc).__name__}; see the daemon log"
    return (
        "Task Agent workspace could not be prepared: "
        f"{display_text(detail, limit=480)}"
    )


# Restored files are created exclusively and never through a link (SEC-1).
_CREATE_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
)


def _create_file(parent_fd: int, name: str, data: bytes, mode: int) -> None:
    """Create ``name`` in ``parent_fd``; mode and owner change on the new fd."""
    descriptor = os.open(name, _CREATE_FLAGS, 0o600, dir_fd=parent_fd)
    try:
        remaining = memoryview(data)
        while remaining:
            remaining = remaining[os.write(descriptor, remaining) :]
        os.fchmod(descriptor, mode)
        fchown_to_agent(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _open_relative_dir(base_fd: int, parts: tuple[str, ...], *, create: bool = False):
    """Walk ``parts`` below ``base_fd`` with no-follow descriptors."""
    with ExitStack() as stack:
        descriptor = base_fd
        for part in parts:
            if create:
                ensure_subdirectory(descriptor, part)
            descriptor = stack.enter_context(open_dir_nofollow(part, descriptor))
        yield descriptor


def _remove_entry(parent_fd: int, name: str, *, directory_allowed: bool) -> None:
    """Remove ``name`` from ``parent_fd`` without following a link.

    A directory is removed with the descriptor-based ``shutil.rmtree`` (it
    refuses a link swapped in mid-removal); a file or link is unlinked.
    """
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=parent_fd)
    elif directory_allowed:
        shutil.rmtree(name, dir_fd=parent_fd)
    else:
        raise WorkspaceRefusal("Task agent instruction path must be a file")


@contextmanager
def _open_agent_directory(root: Path, instance_id: str):
    """Yield ``(instances_fd, target_fd)`` for ``agents/.instances/<id>``.

    Every component is opened ``O_DIRECTORY|O_NOFOLLOW`` from the workspace
    root, so a link a worker swapped in anywhere on the way refuses the
    restore instead of redirecting it (SEC-1).
    """
    with ExitStack() as stack:
        descriptor = stack.enter_context(open_dir_nofollow(_resolved(root)))
        opened = []
        for part in ("agents", ".instances", instance_id):
            ensure_subdirectory(descriptor, part)
            descriptor = stack.enter_context(open_dir_nofollow(part, descriptor))
            opened.append(descriptor)
        yield opened[1], opened[2]


def _prepare_output_directory(root: Path, task: dict) -> None:
    """Create the task's output directory chain without following links."""
    try:
        relative = PurePosixPath(task_output_dir(task)).relative_to("/workspace")
    except ValueError as exc:
        # worker_prompt's refusals are fixed text; relative_to's names paths.
        detail = str(exc) if str(exc).startswith("Task") else "not below /workspace"
        raise WorkspaceRefusal(f"Task output directory is invalid: {detail}") from None
    parts = relative.parts
    if any(part in ("", ".", "..") for part in parts):
        raise WorkspaceRefusal("Agent workspace path escapes its workspace")
    if not parts:
        return
    with open_dir_nofollow(_resolved(root)) as root_fd:
        ensure_owned_directory(root_fd, *parts)


def profile_owned_parts(profile: dict) -> dict:
    """The Profile-owned inputs of a task-Agent CLAUDE.md (X51).

    Platform template text is never archived; it is rendered by the running
    daemon on each attempt. Connectors are omitted on purpose: they are live
    access, filtered per attempt by the supervisor.
    """
    parts: dict = {key: profile.get(key) for key in _PROFILE_OWNED_KEYS}
    parts["skills"] = [
        {key: skill.get(key) for key in _SKILL_KEYS if key in skill}
        for skill in profile.get("skills") or []
        if isinstance(skill, dict)
    ]
    if OFFICE_WORK_POLICY_KEY in profile:
        # Presence matters: a snapshot without the key predates F09.
        parts[OFFICE_WORK_POLICY_KEY] = profile[OFFICE_WORK_POLICY_KEY]
    return parts


def _parts_digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _unavailable_skills_note(unavailable: dict[str, str]) -> str:
    if not unavailable:
        return ""
    lines = "\n".join(
        f"- `{display_text(name, 120)}`: {display_text(reason)}"
        for name, reason in sorted(unavailable.items())
    )
    return (
        "\n\n## Unavailable assigned skills\n\n"
        "These skills are assigned to this Profile but could not be copied when "
        "this task Agent was created, so they are not in this Agent's "
        "`.claude/skills/` and are not listed under Skills:\n"
        f"{lines}\n"
        "Do not substitute another playbook from the office catalog. If the work "
        "cannot be done correctly without one of them, say so and name the skill "
        "instead of guessing its contents.\n"
    )


def _render_claude_md(
    parts: dict, profile: dict, unavailable: dict[str, str] | None = None
) -> str:
    """Current platform playbook + retained Profile parts + live connectors.

    A skill the snapshot continued without is left out of the skill index and
    named in a closing note instead (X52).
    """
    unavailable = unavailable or {}
    skills = [
        skill
        for skill in parts.get("skills") or []
        if not (isinstance(skill, dict) and skill.get("name") in unavailable)
    ]
    return ClaudeMdWriter.compose_task_agent_claude_md(
        {
            **parts,
            "skills": skills,
            "connectors": list(profile.get("connectors") or []),
        }
    ) + _unavailable_skills_note(unavailable)


def _contained(root: Path, relative: Path) -> Path:
    path = root / relative
    if not _resolved(path).is_relative_to(_resolved(root)):
        raise WorkspaceRefusal("Agent workspace path escapes its workspace")
    return path


_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def _require_skill_name(name: object) -> str:
    if (
        not isinstance(name, str)
        or not name
        or Path(name).name != name
        or name in (".", "..")
    ):
        raise WorkspaceRefusal("Invalid assigned skill name")
    return name


@contextmanager
def _open_skill(root: Path, name: str):
    """Walk from a trusted workspace using descriptors, never following links."""
    _require_skill_name(name)
    descriptors = []
    try:
        try:
            current = os.open(_resolved(root), DIR_FLAGS)
            descriptors.append(current)
            for part in (".claude", "skills", name):
                current = os.open(part, DIR_FLAGS, dir_fd=current)
                descriptors.append(current)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise WorkspaceRefusal(
                "Skill folder is unavailable or reached through an unsafe path"
            ) from exc
        yield current
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@contextmanager
def _open_regular(parent: int, name: str):
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise WorkspaceRefusal(
                "Assigned skills require regular files without links"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            yield stream
    finally:
        os.close(descriptor)


def _shown_path(relative: tuple[str, ...]) -> str:
    return display_text("/".join(relative), 160)


def _copy_skill(source: int, target: Path, parts: tuple[str, ...] = ()) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for name in sorted(os.listdir(source)):
        relative = (*parts, name)
        # The ROOT params.json holds mutable runtime values, never part of an
        # instruction archive; a nested params.json is a skill resource. The
        # shared Files policy also excludes credentials and CLI configuration.
        if relative == ("params.json",) or protected_path(relative):
            continue
        info = os.stat(name, dir_fd=source, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, DIR_FLAGS, dir_fd=source)
            try:
                _copy_skill(child, target / name, relative)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode):
            try:
                with _open_regular(source, name) as stream, (target / name).open(
                    "wb"
                ) as output:
                    shutil.copyfileobj(stream, output)
            except ValueError as exc:
                raise WorkspaceRefusal(
                    f"Skill file {_shown_path(relative)!r} must be a regular file "
                    "without links"
                ) from exc
            # Preserve script executability without setuid/setgid/sticky bits.
            (target / name).chmod(stat.S_IMODE(info.st_mode) & 0o777)
        elif stat.S_ISLNK(info.st_mode):
            raise WorkspaceRefusal(
                f"Skill file {_shown_path(relative)!r} is an external symlink or "
                "unsupported internal link"
            )
        else:
            raise WorkspaceRefusal(
                f"Skill file {_shown_path(relative)!r} is a special file"
            )


def _is_live_version(root: Path, name: str, source: int) -> bool:
    """Whether the opened skill directory is still the live, linked one."""
    opened = os.fstat(source)
    if opened.st_nlink == 0:
        return False
    try:
        with _open_skill(root, name) as live:
            current = os.fstat(live)
    except (FileNotFoundError, ValueError):
        return False
    return (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino)


def _skill_is_missing(root: Path, name: str) -> bool:
    try:
        with _open_skill(root, name):
            return False
    except FileNotFoundError:
        return True
    except ValueError:
        return False


def _bundle_identity(copied: Path) -> str | None:
    """The copied version's ``bundle_sha256``, or None when unmanaged/unknown.

    Reads the publisher's manifest with the helper's own name and size cap,
    so a manifest the helper treats as unmanaged is unmanaged here too.
    """
    try:
        with (copied / SKILL_BUNDLE_MANIFEST).open("rb") as stream:
            raw = stream.read(SKILL_BUNDLE_MANIFEST_MAX_BYTES + 1)
    except OSError:
        return None
    if len(raw) > SKILL_BUNDLE_MANIFEST_MAX_BYTES:
        return None
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    digest = manifest.get("bundle_sha256") if isinstance(manifest, dict) else None
    if isinstance(digest, str) and _SKILL_SHA256.fullmatch(digest):
        return digest
    return None


def _snapshot_skill(root: Path, name: str, destination: Path) -> dict:
    """Copy one complete version of an assigned skill into ``destination``.

    Returns the archive manifest record. Raises ``_SkillUnavailable`` when the
    skill is not installed or has no SKILL.md, and ``ValueError`` for unsafe
    content (never retried) or a skill still being replaced after the
    bounded retries.
    """
    _require_skill_name(name)
    shown = display_text(name, 120)
    outcome = "missing"
    for attempt in range(SKILL_COPY_ATTEMPTS):
        if attempt:
            _sleep(SKILL_COPY_BACKOFF_SECONDS * 2 ** (attempt - 1))
        if destination.exists():
            shutil.rmtree(destination)
        try:
            with _open_skill(root, name) as source:
                _copy_skill(source, destination)
                if not _is_live_version(root, name, source):
                    # Swapped or removed while being copied: copy again.
                    outcome = "changed"
                    continue
        except FileNotFoundError:
            # Missing (never installed), or a file/directory vanished mid-copy.
            outcome = "missing" if _skill_is_missing(root, name) else "changed"
            continue
        except ValueError as exc:
            # Unsafe content is never retried; name the skill for the task.
            detail = str(exc) if isinstance(exc, WorkspaceRefusal) else (
                type(exc).__name__
            )
            raise WorkspaceRefusal(f"Assigned skill {shown!r}: {detail}") from exc
        # Exact-case check (SNAP-1): on a case-insensitive host a ``skill.md``
        # would satisfy ``is_file()`` but not the archive's exact file key.
        if "SKILL.md" not in os.listdir(destination) or not (
            destination / "SKILL.md"
        ).is_file():
            raise _SkillUnavailable("its folder has no SKILL.md (incomplete install)")
        return {"status": "copied", "bundle_sha256": _bundle_identity(destination)}
    if outcome == "missing":
        raise _SkillUnavailable("not installed in the office skills folder")
    raise WorkspaceRefusal(
        f"Assigned skill {shown!r} changed during each of {SKILL_COPY_ATTEMPTS} "
        "copy attempts; retry once its publication settles"
    )


def _refresh_skill_params(
    root: Path,
    target_fd: int,
    skills: list[dict],
    current_skills: list[dict] | None = None,
    unavailable: frozenset[str] | set[str] = frozenset(),
) -> None:
    """Refresh only declared non-secret values; retained playbooks stay pinned.

    ``target_fd`` is an open descriptor of the task Agent directory, opened
    no-follow from the workspace root; ``params.json`` is replaced by a
    no-follow rename below it (SEC-1). Skills a new snapshot continued
    without get no parameters (X52).
    """
    current_by_name = {skill.get("name"): skill for skill in current_skills or []}
    for skill in skills:
        name = skill.get("name", "")
        _require_skill_name(name)
        if name in unavailable:
            continue
        allowed = {
            parameter["name"]
            for parameter in skill.get("parameter_schema", [])
            if isinstance(parameter, dict)
            and isinstance(parameter.get("name"), str)
            and parameter.get("is_secret", False) is False
        }
        if current_skills is not None:
            # Classification and access remain live even while instructions are
            # retained. A removed skill grants none. A live entry WITHOUT a
            # parameter_schema list (the REST roster summary shape) is unknown,
            # not revocation: the snapshot's allowed set stands (X53).
            live = current_by_name.get(name)
            live_schema = (
                []
                if live is None
                else live.get("parameter_schema")
                if isinstance(live.get("parameter_schema"), list)
                else None
            )
            if live_schema is not None:
                allowed.intersection_update(
                    parameter["name"]
                    for parameter in live_schema
                    if isinstance(parameter, dict)
                    and isinstance(parameter.get("name"), str)
                    and parameter.get("is_secret", False) is False
                )
        values = {}
        if allowed:
            try:
                with _open_skill(root, name) as source, _open_regular(
                    source, "params.json"
                ) as stream:
                    loaded = json.load(stream)
                if isinstance(loaded, dict):
                    values = {
                        key: value
                        for key, value in loaded.items()
                        if key in allowed and isinstance(value, str)
                    }
            except FileNotFoundError:
                pass
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                # RecursionError: nesting too deep for the JSON parser.
                raise WorkspaceRefusal(
                    f"Assigned skill {display_text(name, 120)!r} has a "
                    "params.json that is not valid JSON"
                ) from None
        with _open_relative_dir(target_fd, (".claude", "skills", name)) as skill_fd:
            atomic_replace_file(
                skill_fd, "params.json", json.dumps(values, sort_keys=True), 0o600
            )


def _protected_exact_case(parts: tuple[str, ...]) -> bool:
    """The Files protection rule as it was before names compared
    case-insensitively.

    ``protected_path`` now casefolds, so a retained archive written by an
    older daemon can legitimately hold a name such as ``.ENV`` that only the
    new rule protects. A name the exact-case rule protects could never have
    been archived and still refuses the restore.
    """
    for index, name in enumerate(parts):
        if name in _PROTECTED_NAMES or name.startswith(_PROTECTED_FILE_PREFIXES):
            return True
        if name.startswith(".env.") and name not in {".env.example", ".env.sample"}:
            return True
        if name == ".claude" and (
            index + 1 == len(parts) or parts[index + 1] != "skills"
        ):
            return True
    return False


def _is_skill_runtime_params(parts: tuple[str, ...]) -> bool:
    """A skill's ROOT params.json: runtime values, never an archive file."""
    return (
        len(parts) == 4
        and parts[:2] == (".claude", "skills")
        and parts[3] == "params.json"
    )


def _require_recorded_skill_copies(saved: dict) -> None:
    """Resuming stays strict for archives that record their skill copies.

    Only archives carrying the ``skills`` map are checked, and only for skills
    recorded as copied: their exact-case ``SKILL.md`` must be restored as
    archived. Older archives were validated under the rule of the daemon
    that wrote them and keep resuming after an upgrade (SNAP-1).
    """
    records = saved.get("skills")
    if not isinstance(records, dict):
        return
    files = saved.get("files") or {}
    for name, record in records.items():
        if not (isinstance(record, dict) and record.get("status") == "copied"):
            continue
        if f".claude/skills/{name}/SKILL.md" not in files:
            raise WorkspaceRefusal(
                "Retained agent snapshot is missing assigned skill "
                f"{display_text(name, 120)!r}"
            )


def _recorded_unavailable(saved: dict) -> dict[str, str]:
    """Skills a snapshot was created without, from its manifest (X52)."""
    records = saved.get("skills")
    if not isinstance(records, dict):
        return {}
    return {
        str(name): str(record.get("reason") or "unavailable")
        for name, record in records.items()
        if isinstance(record, dict) and record.get("status") == "unavailable"
    }


def prepare_instance_workspace(
    workspace: str,
    archive_root: Path,
    profile: dict,
    task: dict,
    *,
    current_skills: list[dict] | None = None,
    unavailable_skills: list[dict] | None = None,
) -> str:
    """Create or restore a task Agent's retained workspace; return its cwd.

    ``unavailable_skills`` (an out-list) receives ``{"name", "reason"}`` for
    each assigned skill a NEW snapshot continued without. It is filled as
    soon as the new archive is committed, so it is complete even when a later
    restore step raises; the caller must report it in that case too (SNAP-2).
    A call that resumes an existing archive adds nothing.
    """
    try:
        instance_id = str(uuid.UUID(str(task["agent_instance_id"])))
        profile_id = str(uuid.UUID(str(task["profile_id"])))
    except (KeyError, TypeError, ValueError):
        raise WorkspaceRefusal("Task Agent identity is missing or invalid") from None
    revision = str(task.get("profile_revision") or "")
    if not revision:
        raise WorkspaceRefusal("Agent profile revision is required")
    root = Path(workspace)
    if _resolved(archive_root).is_relative_to(_resolved(root)):
        raise WorkspaceRefusal(
            "Agent instruction archives must be outside the workspace"
        )
    archive_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive = _contained(archive_root, Path(instance_id))
    if archive.is_symlink():
        raise WorkspaceRefusal("Agent instruction archive cannot be a symlink")
    # Fast refusal only: the restore below re-opens every component without
    # following links, which is what actually protects it (SEC-1).
    target = root / "agents" / ".instances" / instance_id
    if any(path.is_symlink() for path in (root / "agents", target.parent, target)):
        raise WorkspaceRefusal("Task agent directory cannot be a symlink")
    manifest = {
        "agent_instance_id": instance_id,
        "profile_id": profile_id,
        "profile_revision": revision,
    }
    if not archive.exists():
        if task.get("prior_session_id") or os.path.lexists(target):
            raise WorkspaceRefusal(
                f"Retained instruction archive for Agent {instance_id} is missing. "
                "Restore its private instruction archive or explicitly create a new "
                "Agent assignment before retrying; existing work cannot resume "
                "with newly copied playbooks."
            )
        staging = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=archive_root))
        try:
            skill_records: dict[str, dict] = {}
            for skill in profile.get("skills") or []:
                if not isinstance(skill, dict):
                    continue
                name = _require_skill_name(skill.get("name", ""))
                destination = staging / ".claude" / "skills" / name
                try:
                    skill_records[name] = _snapshot_skill(root, name, destination)
                except _SkillUnavailable as missing:
                    # X52: one unmaterialized skill must not block every new
                    # task of the Profile. Continue without it, visibly.
                    if destination.exists():
                        shutil.rmtree(destination)
                    destination.mkdir(parents=True)
                    (destination / UNAVAILABLE_SKILL_FILE).write_text(
                        "# Unavailable skill\n\nThis assigned skill could not be "
                        f"copied when this task Agent was created: {missing.reason}. "
                        "It is not part of this Agent's copy.\n"
                    )
                    skill_records[name] = {
                        "status": "unavailable",
                        "reason": missing.reason,
                    }
            unavailable = {
                name: record["reason"]
                for name, record in skill_records.items()
                if record["status"] == "unavailable"
            }
            parts_payload = json.dumps(
                profile_owned_parts(profile), sort_keys=True
            ).encode()
            (staging / PROFILE_PARTS_FILE).write_bytes(parts_payload)
            # Rollback copy for a legacy-format daemon (module docstring);
            # this daemon renders CLAUDE.md fresh on every attempt.
            (staging / LEGACY_INSTRUCTIONS_FILE).write_text(
                _render_claude_md(json.loads(parts_payload), profile, unavailable)
            )
            files = {
                str(file.relative_to(staging)): hashlib.sha256(
                    file.read_bytes()
                ).hexdigest()
                for file in sorted(staging.rglob("*"))
                if file.is_file() and file.relative_to(staging) != Path(
                    PROFILE_PARTS_FILE
                )
            }
            # Identity keys stay exactly the legacy three; the format marker
            # and the parts digest are additive (X51).
            # The skill copy records (bundle identity, skills continued
            # without) are additive, like the format marker (X51, X52).
            saved_manifest = {
                **manifest,
                "instructions": INSTRUCTIONS_FORMAT,
                "profile_parts_sha256": _parts_digest(parts_payload),
                "files": files,
                "skills": skill_records,
            }
            (staging / "manifest.json").write_text(
                json.dumps(saved_manifest, sort_keys=True)
            )
            os.rename(staging, archive)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        if unavailable_skills is not None:
            unavailable_skills.extend(
                {"name": name, "reason": reason}
                for name, reason in sorted(unavailable.items())
            )
    saved = json.loads((archive / "manifest.json").read_text())
    if any(saved.get(key) != manifest[key] for key in manifest if key != "files"):
        raise WorkspaceRefusal(
            "Retained agent snapshot does not match its backend identity"
        )
    retained_unavailable = _recorded_unavailable(saved)
    _require_recorded_skill_copies(saved)
    rendered_claude_md: str | None = None
    if saved.get("instructions") == INSTRUCTIONS_FORMAT:
        parts_path = _contained(archive, Path(PROFILE_PARTS_FILE))
        parts_payload = parts_path.read_bytes()
        if _parts_digest(parts_payload) != saved.get("profile_parts_sha256"):
            raise WorkspaceRefusal(
                "Retained agent instruction snapshot failed integrity verification"
            )
        parts = json.loads(parts_payload)
        rendered_claude_md = _render_claude_md(parts, profile, retained_unavailable)
    elif LEGACY_INSTRUCTIONS_FILE not in saved.get("files", {}):
        raise WorkspaceRefusal("Retained agent snapshot has no instructions")
    # Restore the selected catalog exactly; a stale file must not silently
    # become another skill or instruction source during a resumed phase.
    # Every step below is relative to one no-follow descriptor of the task
    # Agent directory (SEC-1): a worker that swaps a link in for that
    # directory, ``.claude`` or a file cannot redirect a removal, write or
    # ownership change. ``.claude`` is recreated root-owned and handed to the
    # agent uid only after the daemon has finished writing into it (SEC-2).
    with _open_agent_directory(root, instance_id) as (instances_fd, target_fd):
        _remove_entry(target_fd, ".claude", directory_allowed=True)
        for local_instruction in ("CLAUDE.md", "CLAUDE.local.md", ".mcp.json"):
            _remove_entry(target_fd, local_instruction, directory_allowed=False)
        try:
            os.mkdir(".claude", 0o755, dir_fd=target_fd)
        except FileExistsError:
            raise WorkspaceRefusal(
                "Task agent directory changed while its instructions were restored"
            ) from None
        created: set[tuple[str, ...]] = {(".claude",)}
        with open_dir_nofollow(".claude", target_fd) as claude_fd:
            for relative, digest in saved["files"].items():
                if (
                    rendered_claude_md is not None
                    and relative == LEGACY_INSTRUCTIONS_FILE
                ):
                    # The rollback copy: current platform rules are rendered
                    # below.
                    continue
                path_parts = Path(relative).parts
                if _protected_exact_case(path_parts) or _is_skill_runtime_params(
                    path_parts
                ):
                    raise WorkspaceRefusal(
                        "Retained agent snapshot contains protected runtime data"
                    )
                if protected_path(path_parts):
                    # Archived by a daemon whose exact-case rule did not
                    # protect this name (``.ENV``, ``.Git/config``); the
                    # case-insensitive rule does now. Leave it out rather
                    # than refuse the task Agent forever.
                    logger.warning(
                        "Retained snapshot of task Agent %s (task %s) holds %r, "
                        "now a protected name; it is not restored",
                        instance_id,
                        display_text(task.get("task_id") or "?", 36),
                        display_text(relative, 160),
                    )
                    continue
                source = _contained(archive, Path(relative))
                data = source.read_bytes()
                if hashlib.sha256(data).hexdigest() != digest:
                    raise WorkspaceRefusal(
                        "Retained agent instruction snapshot failed integrity "
                        "verification"
                    )
                mode = stat.S_IMODE(os.stat(source).st_mode) & 0o777
                if len(path_parts) == 1:
                    # A legacy archive's rendered CLAUDE.md: the stale file was
                    # removed above; O_EXCL refuses one planted since.
                    _create_file(target_fd, path_parts[0], data, mode)
                elif path_parts[0] == ".claude":
                    directories = path_parts[1:-1]
                    with _open_relative_dir(
                        claude_fd, directories, create=True
                    ) as parent_fd:
                        _create_file(parent_fd, path_parts[-1], data, mode)
                    created.update(
                        (".claude", *directories[: depth + 1])
                        for depth in range(len(directories))
                    )
                else:
                    raise WorkspaceRefusal(
                        "Retained agent snapshot contains an unexpected path"
                    )
            if rendered_claude_md is not None:
                # Current platform rules, retained Profile parts (X51). O_EXCL
                # |O_NOFOLLOW refuses a link a worker planted after the stale
                # CLAUDE.md was removed; ownership changes on the new file's
                # descriptor, never through a path (CM4).
                _create_file(
                    target_fd, "CLAUDE.md", rendered_claude_md.encode(), 0o644
                )
            _refresh_skill_params(
                root,
                target_fd,
                [
                    skill
                    for skill in profile.get("skills") or []
                    if isinstance(skill, dict)
                ],
                current_skills,
                unavailable=set(retained_unavailable),
            )
            write_agent_hook_settings_at(claude_fd)
        # Hand the recreated tree to the agent uid only now, deepest first.
        # Never recursively: an Agent may have left links elsewhere in its
        # directory during an earlier attempt.
        for directory in sorted(created, key=len, reverse=True):
            with _open_relative_dir(target_fd, directory) as descriptor:
                fchown_to_agent(descriptor)
        fchown_to_agent(target_fd)
        fchown_to_agent(instances_fd)
    _prepare_output_directory(root, task)
    return f"/workspace/agents/.instances/{instance_id}"
