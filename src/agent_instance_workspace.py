"""Materialize a task agent's retained instructions independently of its profile.

The private archive is outside the worker-mounted workspace. Rework restores
the same profile playbook and skill files; normal profile sync never edits it.
Current platform/task contracts continue to arrive in each execution prompt.
Credential/configuration files are excluded using the shared Files policy.
Declared non-secret parameter values refresh per attempt and are never archived.
The office's parent instructions and catalog remain visible; the retained
playbook paths are explicit guidance, not a filesystem isolation boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
import shutil
import stat
import tempfile
import uuid

from src._chown import chown_to_agent
from src._agent_image.secure_files import protected_path
from src.config_sync.claude_md_writer import ClaudeMdWriter, _write_agent_hook_settings
from src.orchestrator.worker_prompt import task_output_dir


def _contained(root: Path, relative: Path) -> Path:
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Agent workspace path escapes its workspace")
    return path


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


@contextmanager
def _open_skill(root: Path, name: str):
    """Walk from a trusted workspace using descriptors, never following links."""
    if not name or Path(name).name != name or name in (".", ".."):
        raise ValueError("Invalid assigned skill name")
    descriptors = []
    try:
        try:
            current = os.open(root.resolve(), _DIRECTORY_FLAGS)
            descriptors.append(current)
            for part in (".claude", "skills", name):
                current = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
                descriptors.append(current)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ValueError(
                "Assigned skill is unavailable or contains an unsafe path"
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
            raise ValueError("Assigned skills require regular files without links")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            yield stream
    finally:
        os.close(descriptor)


def _copy_skill(source: int, target: Path, parts: tuple[str, ...] = ()) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for name in sorted(os.listdir(source)):
        relative = (*parts, name)
        # Params are mutable runtime data, never part of an instruction archive.
        # The shared Files policy also excludes credentials and CLI configuration.
        if name == "params.json" or protected_path(relative):
            continue
        info = os.stat(name, dir_fd=source, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, _DIRECTORY_FLAGS, dir_fd=source)
            try:
                _copy_skill(child, target / name, relative)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode):
            with _open_regular(source, name) as stream, (target / name).open(
                "wb"
            ) as output:
                shutil.copyfileobj(stream, output)
            # Preserve script executability without setuid/setgid/sticky bits.
            (target / name).chmod(stat.S_IMODE(info.st_mode) & 0o777)
        elif stat.S_ISLNK(info.st_mode):
            raise ValueError(
                "Assigned skill contains an external symlink or unsupported internal link"
            )
        else:
            raise ValueError("Assigned skill contains a special file")


def _refresh_skill_params(
    root: Path,
    target: Path,
    skills: list[dict],
    current_skills: list[dict] | None = None,
) -> None:
    """Refresh only declared non-secret values; retained playbooks stay pinned."""
    current_by_name = {skill.get("name"): skill for skill in current_skills or []}
    for skill in skills:
        name = skill.get("name", "")
        if not name or Path(name).name != name or name in (".", ".."):
            raise ValueError("Invalid assigned skill name")
        allowed = {
            parameter["name"]
            for parameter in skill.get("parameter_schema", [])
            if isinstance(parameter, dict)
            and isinstance(parameter.get("name"), str)
            and parameter.get("is_secret", False) is False
        }
        if current_skills is not None:
            # Classification and access remain live even while instructions are
            # retained. Missing metadata (including a removed skill) grants none.
            allowed.intersection_update(
                parameter["name"]
                for parameter in current_by_name.get(name, {}).get(
                    "parameter_schema", []
                )
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
        destination = _contained(target, Path(".claude/skills") / name / "params.json")
        destination.write_text(json.dumps(values, sort_keys=True))
        destination.chmod(0o600)
        chown_to_agent(destination)


def prepare_instance_workspace(
    workspace: str,
    archive_root: Path,
    profile: dict,
    task: dict,
    *,
    current_skills: list[dict] | None = None,
) -> str:
    instance_id = str(uuid.UUID(str(task["agent_instance_id"])))
    profile_id = str(uuid.UUID(str(task["profile_id"])))
    revision = str(task.get("profile_revision") or "")
    if not revision:
        raise ValueError("Agent profile revision is required")
    root = Path(workspace)
    if archive_root.resolve().is_relative_to(root.resolve()):
        raise ValueError("Agent instruction archives must be outside the workspace")
    archive_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive = _contained(archive_root, Path(instance_id))
    if archive.is_symlink():
        raise ValueError("Agent instruction archive cannot be a symlink")
    target = _contained(root, Path("agents/.instances") / instance_id)
    if any(path.is_symlink() for path in (root / "agents", target.parent, target)):
        raise ValueError("Task agent directory cannot be a symlink")
    manifest = {
        "agent_instance_id": instance_id,
        "profile_id": profile_id,
        "profile_revision": revision,
    }
    if not archive.exists():
        if task.get("prior_session_id") or target.exists():
            raise ValueError(
                f"Retained instruction archive for Agent {instance_id} is missing. "
                "Restore its private instruction archive or explicitly create a new "
                "Agent assignment before retrying; existing work cannot resume "
                "with newly copied playbooks."
            )
        staging = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=archive_root))
        try:
            (staging / "CLAUDE.md").write_text(
                ClaudeMdWriter._get_agent_claude_md(profile)
                + "\n\n## Retained task Agent playbooks\n"
                "Use the assigned playbooks at the `.claude/skills/` paths above, "
                "relative to this Agent directory. Read those local files directly; "
                "the inherited office skill catalog can contain newer or unassigned "
                "playbooks and is not this Agent's retained configuration. "
                "Non-secret params.json values refresh when an attempt starts.\n"
            )
            for skill in profile.get("skills", []):
                name = skill.get("name", "")
                destination = staging / ".claude" / "skills" / name
                with _open_skill(root, name) as source:
                    _copy_skill(source, destination)
                if not (destination / "SKILL.md").is_file():
                    raise ValueError(f"Assigned skill {name!r} is not materialized")
            manifest["files"] = {
                str(file.relative_to(staging)): hashlib.sha256(
                    file.read_bytes()
                ).hexdigest()
                for file in sorted(staging.rglob("*"))
                if file.is_file()
            }
            (staging / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
            os.rename(staging, archive)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    saved = json.loads((archive / "manifest.json").read_text())
    if any(saved.get(key) != manifest[key] for key in manifest if key != "files"):
        raise ValueError("Retained agent snapshot does not match its backend identity")
    target.mkdir(parents=True, exist_ok=True)
    # Restore the selected catalog exactly; a stale file must not silently
    # become another skill or instruction source during a resumed phase.
    claude_config = target / ".claude"
    if claude_config.is_symlink():
        claude_config.unlink()
    elif claude_config.exists():
        shutil.rmtree(claude_config)
    for local_instruction in (
        target / "CLAUDE.md",
        target / "CLAUDE.local.md",
        target / ".mcp.json",
    ):
        if local_instruction.is_symlink() or local_instruction.is_file():
            local_instruction.unlink()
        elif local_instruction.exists():
            raise ValueError("Task agent instruction path must be a file")
    restored_directories = {target.parent, target}
    for relative, digest in saved["files"].items():
        parts = Path(relative).parts
        if protected_path(parts) or "params.json" in parts:
            raise ValueError("Retained agent snapshot contains protected runtime data")
        source = _contained(archive, Path(relative))
        if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
            raise ValueError(
                "Retained agent instruction snapshot failed integrity verification"
            )
        destination = _contained(target, Path(relative))
        destination.parent.mkdir(parents=True, exist_ok=True)
        parent = destination.parent
        while parent != target:
            restored_directories.add(parent)
            parent = parent.parent
        shutil.copy2(source, destination)
        chown_to_agent(destination)
    _refresh_skill_params(root, target, profile.get("skills", []), current_skills)
    # Never recursively chown the worker-writable retained directory: an Agent
    # may have left a symlink to data outside it during an earlier attempt.
    for directory in restored_directories:
        chown_to_agent(directory)
    _write_agent_hook_settings(target)
    output = task_output_dir(task)
    relative_output = Path(output).relative_to("/workspace")
    output_path = _contained(root, relative_output)
    output_path.mkdir(parents=True, exist_ok=True)
    for directory in (output_path, *output_path.parents):
        if directory == root:
            break
        chown_to_agent(directory)
    return f"/workspace/agents/.instances/{instance_id}"
