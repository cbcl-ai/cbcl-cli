"""Workspace directory setup — creates the base directory structure
and manages per-agent skill symlinks for skill isolation.

Called during office initialization and after every config sync to
ensure the workspace reflects the current agent/skill assignments.
"""

from __future__ import annotations

import logging
import os
import stat
from contextlib import ExitStack
from pathlib import Path

from src.config_sync._descriptor_io import (
    MaterializationFailures,
    ensure_owned_directory,
    ensure_subdirectory,
    fchown_to_agent,
    is_environmental_error,
    is_real_directory,
    list_real_directories,
    open_dir_nofollow,
    open_owned_subdirectory,
    open_workspace_root,
    remove_directory_tree,
)
from src.paths import is_safe_agent_name

AGENTS_DIRNAME = "agents"
OUTPUTS_DIRNAME = "outputs"

# Base directories created (and owned by the agent uid) on office setup,
# each as the chain of single path components walked from the workspace.
_BASE_DIRECTORIES: tuple[tuple[str, ...], ...] = (
    (AGENTS_DIRNAME,),
    (AGENTS_DIRNAME, "manager"),
    ("workstreams",),
    (".claude", "skills"),
    (".scripts",),
    (".cubicle",),
    (OUTPUTS_DIRNAME,),
)

logger = logging.getLogger(__name__)


class WorkspaceSetup:
    """Creates and maintains the workspace directory structure."""

    def __init__(self, workspace_path: str) -> None:
        self._workspace = Path(workspace_path)

    def ensure_structure(self) -> None:
        """Create the base directory structure for the workspace.

        Every directory we touch is owned by the agent uid because the
        daemon runs as root on the host and the bind-mounted dirs end up
        root-owned otherwise — blocking the in-container ``agent`` user
        (uid 1000) from writing anything beneath them.

        Everything below the workspace is agent-writable, so each directory
        is created and opened component by component relative to a
        workspace descriptor without following a link, and owned through
        ``fchown`` (SEC-2): a link a session planted at ``workstreams``,
        ``agents``, ``.cubicle`` or any other base name is never chowned or
        created through. Such an entry, or one a session made unreadable, is
        logged and left alone, so office setup never stops on it; an
        environmental failure (no space, I/O, read-only file system,
        exhausted descriptors) raises after the remaining directories.

        ``inbox`` and ``source`` are created by the container startup upload
        directory helper. Keep their privileged legacy ownership repair out of
        this host-side loop: it must reject symlinks and nested workspace mounts.
        """
        failures = MaterializationFailures()
        with open_workspace_root(self._workspace) as workspace_fd:
            fchown_to_agent(workspace_fd)
            for parts in _BASE_DIRECTORIES:
                try:
                    ensure_owned_directory(workspace_fd, *parts)
                except OSError as exc:
                    failures.handle(exc, str(self._workspace.joinpath(*parts)))
        failures.raise_if_any()

        logger.info("Workspace structure ensured at %s", self._workspace)

    def sync_workstream_outputs(self, workstreams: list[dict]) -> None:
        """Pre-create per-workstream output directories.

        Files written by agents and scripts live under
        ``/workspace/outputs/{workstream_short_code}/[{scope_readable_id}/]``
        so that work from different workstreams stays separated and
        discoverable. The flat ``/workspace/outputs/`` root is reserved
        for legacy artifacts (kept readable to preserve existing files).

        Per-scope subdirectories are NOT pre-created here — the worker
        creates them on first write — because scope sets churn faster
        than workstreams and we only need the parent directories to
        exist before any worker starts.

        Notes
        -----
        * ``short_code`` is immutable per task-spec.md once the
          workstream is created; renaming a workstream does NOT change
          its directory name. Existing files keep working.
        * Archived workstreams are still included so their files
          remain reachable; the backend's ``_serialize_workstream``
          ships them in ``sync_config`` regardless of status.
        * This is idempotent, so it's safe to call on every config sync.
        * Directories are created without following a link (SEC-2): a
          link at ``outputs`` or at a workstream's output directory is
          never created or chowned through. Only an environmental error (no
          space, I/O, read-only file system, exhausted descriptors or
          memory) raises, after the remaining directories; any other error
          is logged and that directory skipped.
        """
        outputs_root = self._workspace / OUTPUTS_DIRNAME
        created = 0
        failures = MaterializationFailures()
        with ExitStack() as stack:
            workspace_fd = stack.enter_context(open_workspace_root(self._workspace))
            try:
                outputs_fd = stack.enter_context(
                    open_owned_subdirectory(workspace_fd, OUTPUTS_DIRNAME)
                )
            except OSError as exc:
                if is_environmental_error(exc):
                    raise
                logger.error(
                    "%s could not be opened as a real directory (%s); workstream "
                    "output directories are not created through it.",
                    outputs_root,
                    exc,
                )
                return
            for ws in workstreams or []:
                short_code = (ws.get("short_code") or "").strip()
                if not short_code:
                    continue
                try:
                    ensure_owned_directory(outputs_fd, short_code)
                except OSError as exc:
                    failures.handle(
                        exc, f"output directory {outputs_root}/{short_code}"
                    )
                    continue
                created += 1
        logger.info(
            "Synced %d workstream output directories under %s",
            created,
            outputs_root,
        )
        failures.raise_if_any()

    def ensure_task_output_dir(
        self,
        workstream_short_code: str,
        scope_readable_id: str | None = None,
    ) -> str:
        """Idempotently create the per-task output directory and return its path.

        Closes the race between "new workstream is created on the
        backend" and "first task in that workstream is dispatched
        before the next ``sync_config`` arrives". The dispatcher (or
        the task-ready handler) calls this just before the worker
        spawn so the directory is always there when the worker reads
        its prompt and writes its first chunk.

        Falls back to the flat ``/workspace/outputs/`` root when
        ``workstream_short_code`` is empty (older orchestrator
        versions, manually-triggered scripts without a workstream).

        Every directory in the chain is created and owned without
        following a link (SEC-2); a link or file in the chain, or a
        component that is not a single path entry, raises ``OSError``.
        """
        outputs_root = self._workspace / OUTPUTS_DIRNAME
        parts = [OUTPUTS_DIRNAME]
        short = (workstream_short_code or "").strip()
        if short:
            parts.append(short)
            if scope_readable_id and scope_readable_id.strip():
                parts.append(scope_readable_id.strip())
        with open_workspace_root(self._workspace) as workspace_fd:
            ensure_owned_directory(workspace_fd, *parts)
        return str(outputs_root.joinpath(*parts[1:]))

    def sync_agent_workspaces(self, agents: list[dict]) -> None:
        """Create per-agent workspace directories with skill symlinks.

        Each agent gets ``/workspace/agents/{name}/.claude/skills/`` with
        symlinks to only their assigned skills.  The Manager gets symlinks
        to ALL skills.

        Parameters
        ----------
        agents:
            List of agent dicts from sync_config, each with ``name``,
            ``agent_type``, and ``skills`` (list of skill dicts with ``name``).
        """
        agents_dir = self._workspace / AGENTS_DIRNAME
        seen_names: set[str] = {"manager", ".instances"}
        # Only an environmental error (no space, I/O, read-only file system,
        # exhausted descriptors or memory) raises, after the remaining
        # agents, so config sync keeps admission closed; any other error (a
        # planted link or file, a changed permission) is logged and the entry
        # skipped.
        failures = MaterializationFailures()

        with ExitStack() as stack:
            # SEC-1: everything below runs relative to descriptors anchored at
            # the workspace root and opened without following a link. The
            # tree is agent-writable and the daemon may run as root: a link
            # a session swaps in for ``agents``, an agent directory or its
            # ``.claude/skills`` must never redirect a root-owned mkdir,
            # chown, unlink or ``rmtree`` outside the workspace.
            workspace_fd = stack.enter_context(open_workspace_root(self._workspace))
            try:
                agents_fd = stack.enter_context(
                    open_owned_subdirectory(workspace_fd, AGENTS_DIRNAME)
                )
            except OSError as exc:
                if is_environmental_error(exc):
                    raise
                logger.error(
                    "%s could not be opened as a real directory (%s); refusing "
                    "to sync agent workspaces through it.",
                    agents_dir,
                    exc,
                )
                return

            # Installed skill names from the master directory (None when it
            # is not a real directory: links are then left untouched).
            try:
                all_skill_names = self._installed_skill_names(workspace_fd)
            except OSError as exc:
                failures.handle(exc, "the master skills directory")
                all_skill_names = None

            # Manager gets ALL skills
            self._sync_agent_dir_skills(
                agents_fd, "manager", all_skill_names, all_skill_names, failures
            )

            # Workers get only assigned skills
            for agent in agents:
                name = agent.get("name", "")
                if not name:
                    continue
                seen_names.add(name)

                assigned_skills = {
                    s.get("name", "") for s in agent.get("skills", []) if s.get("name")
                }

                # 07/H-13: a name must be one plain directory entry.
                if not is_safe_agent_name(name):
                    logger.warning(
                        "Skipping agent with unsafe name %r — it would resolve "
                        "outside the workspace agents directory",
                        name,
                    )
                    continue
                self._sync_agent_dir_skills(
                    agents_fd, name, assigned_skills, all_skill_names, failures
                )

            # Clean up orphan agent workspace dirs (real directories only;
            # an entry whose type cannot be read is left alone).
            orphans, _unknown = list_real_directories(
                agents_fd, failures, str(agents_dir)
            )
            for child in orphans:
                if child in seen_names:
                    continue
                try:
                    remove_directory_tree(agents_fd, child)
                except OSError as exc:
                    # A session can make removal fail forever; the orphan
                    # is harmless where it is.
                    failures.skip(exc, f"orphan agent workspace {child} (removal)")
                    continue
                logger.info("Removed orphan agent workspace: %s", child)

        logger.info(
            "Synced skill symlinks for %d agents (%d master skills)",
            len(seen_names),
            len(all_skill_names or ()),
        )
        failures.raise_if_any()

    @staticmethod
    def _installed_skill_names(workspace_fd: int) -> set[str] | None:
        """Skill directory names under ``.claude/skills``.

        Returns an empty set when the master directory is absent and None
        when ``.claude`` or ``skills`` is a link or not a directory.
        """
        try:
            with (
                open_dir_nofollow(".claude", workspace_fd) as claude_fd,
                open_dir_nofollow("skills", claude_fd) as skills_fd,
            ):
                return {
                    entry
                    for entry in os.listdir(skills_fd)
                    if is_real_directory(skills_fd, entry)
                }
        except FileNotFoundError:
            return set()
        except OSError as exc:
            if is_environmental_error(exc):
                raise
            logger.warning(
                "The master skills directory could not be read (%s); agent "
                "skill links are left untouched this sync.",
                exc,
            )
            return None

    def _sync_agent_dir_skills(
        self,
        agents_fd: int,
        name: str,
        skill_names: set[str] | None,
        installed: set[str] | None,
        failures: MaterializationFailures,
    ) -> None:
        """Create/own ``agents/<name>`` and sync its skill links; a failure
        is handed to ``failures`` so the other agents still sync."""
        try:
            with open_owned_subdirectory(agents_fd, name) as agent_fd:
                if skill_names is None or installed is None:
                    return
                self._sync_agent_skills(agent_fd, name, skill_names & installed)
        except OSError as exc:
            failures.handle(exc, f"agent workspace {name}")

    def _sync_agent_skills(
        self,
        agent_fd: int,
        agent_name: str,
        skill_names: set[str],
    ) -> None:
        """Create/update/remove skill symlinks in an agent's .claude/skills/.

        Uses relative symlinks so paths resolve correctly both on the host
        and inside the Docker container. Every step is relative to the
        agent directory descriptor; ``.claude`` and ``skills`` are opened
        without following a link.
        """
        agent_dir = self._workspace / AGENTS_DIRNAME / agent_name
        skills_dir = agent_dir / ".claude" / "skills"
        master_skills_dir = self._workspace / ".claude" / "skills"
        with ExitStack() as stack:
            for part in (".claude", "skills"):
                ensure_subdirectory(agent_fd, part)
                agent_fd = stack.enter_context(open_dir_nofollow(part, agent_fd))
            skills_fd = agent_fd

            # Create or update symlinks for assigned skills
            for skill_name in skill_names:
                # Compute relative path from link location to target
                rel_target = os.path.relpath(master_skills_dir / skill_name, skills_dir)
                try:
                    info = os.stat(skill_name, dir_fd=skills_fd, follow_symlinks=False)
                except FileNotFoundError:
                    info = None
                if info is not None and stat.S_ISLNK(info.st_mode):
                    # Update if target changed
                    if os.readlink(skill_name, dir_fd=skills_fd) != rel_target:
                        os.unlink(skill_name, dir_fd=skills_fd)
                        os.symlink(rel_target, skill_name, dir_fd=skills_fd)
                    continue
                if info is not None:
                    # Non-symlink file/dir exists — remove and replace
                    try:
                        if is_real_directory(skills_fd, skill_name):
                            remove_directory_tree(skills_fd, skill_name)
                        else:
                            os.unlink(skill_name, dir_fd=skills_fd)
                    except OSError as exc:
                        # Session-planted content: never a sync failure.
                        MaterializationFailures.skip(
                            exc, f"skill entry {agent_name}/{skill_name} (removal)"
                        )
                        continue
                os.symlink(rel_target, skill_name, dir_fd=skills_fd)

            # Remove stale symlinks (skills no longer assigned)
            for entry in os.listdir(skills_fd):
                if entry in skill_names:
                    continue
                info = os.stat(entry, dir_fd=skills_fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    os.unlink(entry, dir_fd=skills_fd)
                    logger.debug(
                        "Removed stale skill symlink: %s/%s", agent_name, entry
                    )
