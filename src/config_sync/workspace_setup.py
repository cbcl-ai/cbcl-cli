"""Workspace directory setup — creates the base directory structure
and manages per-agent skill symlinks for skill isolation.

Called during office initialization and after every config sync to
ensure the workspace reflects the current agent/skill assignments.
"""

from __future__ import annotations

import logging

from src.paths import safe_agent_dir
import os
import shutil
from pathlib import Path

from src._chown import chown_to_agent

logger = logging.getLogger(__name__)


class WorkspaceSetup:
    """Creates and maintains the workspace directory structure."""

    def __init__(self, workspace_path: str) -> None:
        self._workspace = Path(workspace_path)

    def ensure_structure(self) -> None:
        """Create the base directory structure for the workspace.

        Every directory we touch needs ``chown_to_agent`` because the
        daemon runs as root on the host and the bind-mounted dirs
        end up root-owned otherwise — blocking the in-container
        ``agent`` user (uid 1000) from writing anything beneath them.
        """
        dirs = [
            self._workspace,
            self._workspace / "agents",
            self._workspace / "agents" / "manager",
            self._workspace / "workstreams",
            self._workspace / ".claude" / "skills",
            self._workspace / ".scripts",
            self._workspace / ".cubicle",
            self._workspace / "outputs",
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
            chown_to_agent(d)

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
        * This is idempotent (mkdir exist_ok=True) so it's safe to
          call on every config sync.
        """
        outputs_root = self._workspace / "outputs"
        outputs_root.mkdir(parents=True, exist_ok=True)
        chown_to_agent(outputs_root)
        created = 0
        for ws in workstreams or []:
            short_code = (ws.get("short_code") or "").strip()
            if not short_code:
                continue
            ws_dir = outputs_root / short_code
            ws_dir.mkdir(parents=True, exist_ok=True)
            chown_to_agent(ws_dir)
            created += 1
        logger.info(
            "Synced %d workstream output directories under %s",
            created,
            outputs_root,
        )

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
        """
        outputs_root = self._workspace / "outputs"
        outputs_root.mkdir(parents=True, exist_ok=True)
        chown_to_agent(outputs_root)
        short = (workstream_short_code or "").strip()
        if not short:
            return str(outputs_root)
        # Build the chain incrementally so we chown each intermediate
        # directory the agent will need to traverse + write into.
        ws_dir = outputs_root / short
        ws_dir.mkdir(parents=True, exist_ok=True)
        chown_to_agent(ws_dir)
        target = ws_dir
        if scope_readable_id and scope_readable_id.strip():
            target = ws_dir / scope_readable_id.strip()
            target.mkdir(parents=True, exist_ok=True)
            chown_to_agent(target)
        return str(target)

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
        agents_dir = self._workspace / "agents"
        master_skills_dir = self._workspace / ".claude" / "skills"
        seen_names: set[str] = {"manager"}

        # Collect all installed skill names from the master directory
        all_skill_names: set[str] = set()
        if master_skills_dir.is_dir():
            for entry in master_skills_dir.iterdir():
                if entry.is_dir():
                    all_skill_names.add(entry.name)

        # Manager gets ALL skills
        self._sync_agent_skills(
            agents_dir / "manager",
            master_skills_dir,
            all_skill_names,
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

            # 07/H-13: jail the name before it becomes a host path.
            agent_dir = safe_agent_dir(agents_dir, name)
            if agent_dir is None:
                logger.warning(
                    "Skipping agent with unsafe name %r — it would resolve "
                    "outside the workspace agents directory", name,
                )
                continue
            agent_dir.mkdir(parents=True, exist_ok=True)
            chown_to_agent(agent_dir)

            self._sync_agent_skills(agent_dir, master_skills_dir, assigned_skills)

        # Clean up orphan agent workspace dirs
        for child in agents_dir.iterdir():
            if child.is_dir() and child.name not in seen_names:
                shutil.rmtree(child)
                logger.info("Removed orphan agent workspace: %s", child.name)

        logger.info(
            "Synced skill symlinks for %d agents (%d master skills)",
            len(seen_names),
            len(all_skill_names),
        )

    @staticmethod
    def _sync_agent_skills(
        agent_dir: Path,
        master_skills_dir: Path,
        skill_names: set[str],
    ) -> None:
        """Create/update/remove skill symlinks in an agent's .claude/skills/.

        Uses relative symlinks so paths resolve correctly both on the host
        and inside the Docker container.
        """
        skills_dir = agent_dir / ".claude" / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        # Create or update symlinks for assigned skills
        for skill_name in skill_names:
            link_path = skills_dir / skill_name
            target_path = master_skills_dir / skill_name

            if not target_path.exists():
                continue  # Skill not installed on disk — skip

            # Compute relative path from link location to target
            rel_target = os.path.relpath(target_path, skills_dir)

            if link_path.is_symlink():
                # Update if target changed
                if os.readlink(str(link_path)) != rel_target:
                    link_path.unlink()
                    link_path.symlink_to(rel_target)
                continue

            if link_path.exists():
                # Non-symlink file/dir exists — remove and replace
                if link_path.is_dir():
                    shutil.rmtree(link_path)
                else:
                    link_path.unlink()

            link_path.symlink_to(rel_target)

        # Remove stale symlinks (skills no longer assigned)
        for entry in skills_dir.iterdir():
            if entry.is_symlink() and entry.name not in skill_names:
                entry.unlink()
                logger.debug("Removed stale skill symlink: %s/%s", agent_dir.name, entry.name)
