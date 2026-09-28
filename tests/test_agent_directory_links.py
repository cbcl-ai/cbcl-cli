"""SEC-1 / WSD-3 / SEC-2: agent and base directories never follow a link.

``/workspace`` is writable by the session uid while the daemon may run as
root. A session can swap ``agents``, an agent directory, ``.claude``,
``workstreams`` or a ``CLAUDE.md`` for a link; config sync must never write,
chown or delete through it. Run as root (the Linux test lane) the ownership
assertions also prove that no ``chown`` reached the link target.
"""

from __future__ import annotations

import errno
import os
import resource
import shutil
import tempfile
from pathlib import Path

import pytest

from src.config_sync import claude_md_writer, workspace_setup
from src.config_sync.claude_md_writer import ClaudeMdWriter
from src.config_sync.workspace_setup import WorkspaceSetup

ANALYST = {"name": "analyst", "agent_type": "system", "display_name": "Analyst"}
AUDITOR = {"name": "auditor", "agent_type": "system", "display_name": "Auditor"}
SYNC_CONFIG = {
    "office_name": "O",
    "agents": [ANALYST],
    "workstreams": [{"id": "11111111-1111-1111-1111-111111111111", "name": "Alpha"}],
}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A host directory outside the workspace holding precious content."""
    host = tmp_path / "host"
    (host / "precious").mkdir(parents=True)
    (host / "precious" / "data.txt").write_text("host data")
    return host


def _snapshot(root: Path) -> dict[str, tuple[int, int, str | None]]:
    """Every entry under ``root`` with its owner and file content."""
    entries = {".": (root.stat().st_uid, root.stat().st_gid, None)}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        content = path.read_text() if path.is_file() and not path.is_symlink() else None
        entries[str(path.relative_to(root))] = (info.st_uid, info.st_gid, content)
    return entries


@pytest.fixture
def no_path_chown(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every path-based chown (all agent ownership must use fchown)."""
    calls: list[str] = []
    real_chown = os.chown

    def recording_chown(path, *args, **kwargs):
        calls.append(str(path))
        return real_chown(path, *args, **kwargs)

    monkeypatch.setattr(os, "chown", recording_chown)
    return calls


class TestAgentDirectories:
    def test_linked_agents_root_is_never_written_chowned_or_pruned(
        self, workspace: Path, outside: Path, no_path_chown: list[str]
    ) -> None:
        (workspace / "agents").symlink_to(outside)
        before = _snapshot(outside)

        ClaudeMdWriter(str(workspace)).sync_agent_directories([ANALYST])

        assert _snapshot(outside) == before
        assert (workspace / "agents").is_symlink()
        assert no_path_chown == []

    def test_linked_agent_directory_is_skipped_and_others_are_written(
        self, workspace: Path, outside: Path, no_path_chown: list[str]
    ) -> None:
        (workspace / "agents").mkdir()
        (workspace / "agents" / "analyst").symlink_to(outside)
        before = _snapshot(outside)

        ClaudeMdWriter(str(workspace)).sync_agent_directories([ANALYST, AUDITOR])

        assert _snapshot(outside) == before
        assert (workspace / "agents" / "auditor" / "CLAUDE.md").is_file()
        assert (
            workspace / "agents" / "auditor" / ".claude" / "settings.json"
        ).is_file()
        assert no_path_chown == []

    @pytest.mark.parametrize(
        "unsafe", [".hidden", "-x", "~x", " ", "a\\b", "../escape"]
    )
    def test_unsafe_agent_names_are_skipped_by_both_syncs(
        self, workspace: Path, unsafe: str
    ) -> None:
        """07/H-13 (B7b-tests-03): an agent name that is not one plain entry
        gets no directory in either sync, and the valid agents are still
        written. Multi-component names are also refused by the descriptor
        helpers; the rest are the naming rule ``is_safe_agent_name`` adds."""
        agents = [ANALYST, {**ANALYST, "name": unsafe}]

        ClaudeMdWriter(str(workspace)).sync_all({"office_name": "O", "agents": agents})
        WorkspaceSetup(str(workspace)).sync_agent_workspaces(agents)

        assert sorted(os.listdir(workspace / "agents")) == ["analyst", "manager"]
        assert (workspace / "agents" / "analyst" / "CLAUDE.md").is_file()
        assert not (workspace / "escape").exists()

    def test_orphan_cleanup_never_follows_a_linked_entry(
        self, workspace: Path, outside: Path
    ) -> None:
        writer = ClaudeMdWriter(str(workspace))
        writer.sync_agent_directories([ANALYST, AUDITOR])
        (workspace / "agents" / "evil").symlink_to(outside)
        before = _snapshot(outside)

        writer.sync_agent_directories([ANALYST])

        assert _snapshot(outside) == before
        assert (workspace / "agents" / "evil").is_symlink()
        assert not (workspace / "agents" / "auditor").exists()

    def test_linked_claude_md_is_replaced_even_when_existing_files_are_kept(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        host_file = tmp_path / "host.md"
        host_file.write_text("host data")
        agent_dir = workspace / "agents" / "analyst"
        agent_dir.mkdir(parents=True)
        (agent_dir / "CLAUDE.md").symlink_to(host_file)

        ClaudeMdWriter(str(workspace)).sync_agent_directories(
            [ANALYST], keep_existing=True
        )

        assert host_file.read_text() == "host data"
        assert not (agent_dir / "CLAUDE.md").is_symlink()
        assert "Analyst" in (agent_dir / "CLAUDE.md").read_text()

    def test_manager_claude_md_never_follows_a_linked_agents_root(
        self, workspace: Path, outside: Path, no_path_chown: list[str]
    ) -> None:
        (workspace / "agents").symlink_to(outside)
        before = _snapshot(outside)

        ClaudeMdWriter(str(workspace)).write_manager_claude_md({"office_name": "O"})

        assert _snapshot(outside) == before
        assert no_path_chown == []

    def test_manager_claude_md_never_follows_a_linked_manager_directory(
        self, workspace: Path, outside: Path
    ) -> None:
        (workspace / "agents").mkdir()
        (workspace / "agents" / "manager").symlink_to(outside)
        before = _snapshot(outside)

        ClaudeMdWriter(str(workspace)).write_manager_claude_md({"office_name": "O"})

        assert _snapshot(outside) == before

    def test_office_claude_md_replaces_planted_links(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        host_file = tmp_path / "host.md"
        host_file.write_text("host data")
        (workspace / "CLAUDE.md").symlink_to(host_file)
        (workspace / f".CLAUDE.md.{os.getpid()}.tmp").symlink_to(host_file)

        ClaudeMdWriter(str(workspace)).write_office_claude_md({"office_name": "O"})

        assert host_file.read_text() == "host data"
        assert not (workspace / "CLAUDE.md").is_symlink()
        assert "# Office: O" in (workspace / "CLAUDE.md").read_text()


class TestSyncAllBaseDirectories:
    """WSD-3: sync_all creates its base directories without following a link
    and a file or link at one of their names never fails the sync."""

    def test_file_at_workstreams_does_not_fail_sync_all(self, workspace: Path) -> None:
        (workspace / "workstreams").write_text("not a directory")

        ClaudeMdWriter(str(workspace)).sync_all(SYNC_CONFIG)

        assert (workspace / "workstreams").read_text() == "not a directory"
        assert (workspace / "agents" / "analyst" / "CLAUDE.md").is_file()
        assert (workspace / "agents" / "manager" / "CLAUDE.md").is_file()

    @pytest.mark.parametrize("name", ["workstreams", "agents"])
    def test_linked_base_directory_is_never_chowned_or_written(
        self,
        workspace: Path,
        outside: Path,
        no_path_chown: list[str],
        name: str,
    ) -> None:
        (workspace / name).symlink_to(outside)
        before = _snapshot(outside)

        ClaudeMdWriter(str(workspace)).sync_all(SYNC_CONFIG)

        assert _snapshot(outside) == before
        assert (workspace / name).is_symlink()
        assert no_path_chown == []
        assert (workspace / "CLAUDE.md").is_file()


class TestAgentWorkspaces:
    """SEC-1: the per-agent skill-link sync never follows a planted link."""

    @staticmethod
    def _install_skill(workspace: Path, name: str) -> None:
        skill = workspace / ".claude" / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"# {name}")

    def test_links_assigned_skills(self, workspace: Path) -> None:
        self._install_skill(workspace, "design")
        self._install_skill(workspace, "review")
        agent = {**ANALYST, "skills": [{"name": "design"}, {"name": "missing"}]}

        WorkspaceSetup(str(workspace)).sync_agent_workspaces([agent])

        skills = workspace / "agents" / "analyst" / ".claude" / "skills"
        assert sorted(p.name for p in skills.iterdir()) == ["design"]
        assert (skills / "design").is_symlink()
        assert (skills / "design" / "SKILL.md").read_text() == "# design"
        manager_skills = workspace / "agents" / "manager" / ".claude" / "skills"
        assert sorted(p.name for p in manager_skills.iterdir()) == ["design", "review"]

    def test_linked_agents_root_is_never_touched(
        self, workspace: Path, outside: Path, no_path_chown: list[str]
    ) -> None:
        self._install_skill(workspace, "design")
        (workspace / "agents").symlink_to(outside)
        before = _snapshot(outside)

        WorkspaceSetup(str(workspace)).sync_agent_workspaces(
            [{**ANALYST, "skills": [{"name": "design"}]}]
        )

        assert _snapshot(outside) == before
        assert no_path_chown == []

    def test_linked_skills_directory_is_never_written_or_pruned(
        self, workspace: Path, outside: Path
    ) -> None:
        self._install_skill(workspace, "precious")
        (workspace / "agents" / "analyst" / ".claude").mkdir(parents=True)
        (workspace / "agents" / "analyst" / ".claude" / "skills").symlink_to(outside)
        before = _snapshot(outside)

        # "precious" is installed and assigned: the old path-based sync would
        # have rmtree'd outside/precious to put a skill link in its place.
        WorkspaceSetup(str(workspace)).sync_agent_workspaces(
            [{**ANALYST, "skills": [{"name": "precious"}]}]
        )

        assert _snapshot(outside) == before

    def test_orphan_cleanup_never_follows_a_linked_entry(
        self, workspace: Path, outside: Path
    ) -> None:
        (workspace / "agents").mkdir()
        (workspace / "agents" / "evil").symlink_to(outside)
        before = _snapshot(outside)

        WorkspaceSetup(str(workspace)).sync_agent_workspaces([ANALYST])

        assert _snapshot(outside) == before
        assert (workspace / "agents" / "evil").is_symlink()

    # B7b-tests-02: the steady-state branches of ``_sync_agent_skills`` (every
    # sync after the first) had no test.
    @staticmethod
    def _skills(workspace: Path) -> Path:
        return workspace / "agents" / "analyst" / ".claude" / "skills"

    def test_resync_restores_a_repointed_link(self, workspace: Path) -> None:
        self._install_skill(workspace, "design")
        agent = {**ANALYST, "skills": [{"name": "design"}]}
        setup = WorkspaceSetup(str(workspace))
        setup.sync_agent_workspaces([agent])
        link = self._skills(workspace) / "design"
        good = os.readlink(link)
        link.unlink()
        os.symlink("../elsewhere", link)

        setup.sync_agent_workspaces([agent])

        assert link.is_symlink()
        assert os.readlink(link) == good
        assert (link / "SKILL.md").read_text() == "# design"

    @pytest.mark.parametrize("planted", ["directory", "file"])
    def test_planted_entry_at_an_assigned_skill_is_replaced(
        self, workspace: Path, outside: Path, planted: str
    ) -> None:
        self._install_skill(workspace, "design")
        agent = {**ANALYST, "skills": [{"name": "design"}]}
        setup = WorkspaceSetup(str(workspace))
        setup.sync_agent_workspaces([agent])
        link = self._skills(workspace) / "design"
        link.unlink()
        if planted == "directory":
            link.mkdir()
            (link / "notes.txt").write_text("planted")
            os.symlink(outside, link / "esc")
        else:
            link.write_text("planted")
        before = _snapshot(outside)

        setup.sync_agent_workspaces([agent])

        assert link.is_symlink()
        assert (link / "SKILL.md").read_text() == "# design"
        assert _snapshot(outside) == before

    def test_a_planted_directory_that_cannot_be_removed_is_skipped(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._install_skill(workspace, "design")
        agent = {**ANALYST, "skills": [{"name": "design"}]}
        setup = WorkspaceSetup(str(workspace))
        setup.sync_agent_workspaces([agent])
        link = self._skills(workspace) / "design"
        link.unlink()
        link.mkdir()
        (link / "notes.txt").write_text("planted")

        def refuse(parent_fd: int, name: str) -> None:
            raise PermissionError(errno.EACCES, "Permission denied", name)

        monkeypatch.setattr(workspace_setup, "remove_directory_tree", refuse)

        setup.sync_agent_workspaces([agent])

        assert link.is_dir() and not link.is_symlink()
        assert (link / "notes.txt").read_text() == "planted"

    def test_an_unassigned_skill_link_is_pruned_and_non_links_are_kept(
        self, workspace: Path
    ) -> None:
        self._install_skill(workspace, "design")
        self._install_skill(workspace, "review")
        setup = WorkspaceSetup(str(workspace))
        setup.sync_agent_workspaces(
            [{**ANALYST, "skills": [{"name": "design"}, {"name": "review"}]}]
        )
        skills = self._skills(workspace)
        (skills / "notes").mkdir()

        setup.sync_agent_workspaces([{**ANALYST, "skills": [{"name": "design"}]}])

        assert not os.path.lexists(skills / "review")
        assert (skills / "notes").is_dir() and not (skills / "notes").is_symlink()
        assert (skills / "design").is_symlink()


class TestWorkspaceBaseStructure:
    """SEC-2: office setup creates its base directories without following a
    link, so a planted link is never chowned (the daemon may run as root)."""

    @pytest.mark.parametrize(
        "name", ["workstreams", "agents", ".cubicle", ".claude", "outputs"]
    )
    def test_linked_base_directory_is_never_chowned_or_written(
        self,
        workspace: Path,
        outside: Path,
        no_path_chown: list[str],
        name: str,
    ) -> None:
        (workspace / name).symlink_to(outside)
        before = _snapshot(outside)

        WorkspaceSetup(str(workspace)).ensure_structure()

        assert _snapshot(outside) == before
        assert (workspace / name).is_symlink()
        assert no_path_chown == []
        # The other base directories are still created.
        assert (workspace / ".scripts").is_dir()

    def test_file_at_base_name_does_not_fail_setup(self, workspace: Path) -> None:
        (workspace / "agents").write_text("not a directory")

        WorkspaceSetup(str(workspace)).ensure_structure()

        assert (workspace / "agents").read_text() == "not a directory"
        assert (workspace / "workstreams").is_dir()
        assert (workspace / ".claude" / "skills").is_dir()

    def test_linked_outputs_is_never_created_through(
        self, workspace: Path, outside: Path, no_path_chown: list[str]
    ) -> None:
        (workspace / "outputs").symlink_to(outside)
        before = _snapshot(outside)
        setup = WorkspaceSetup(str(workspace))

        setup.sync_workstream_outputs([{"short_code": "AB"}])
        with pytest.raises(OSError):
            setup.ensure_task_output_dir("AB", "AB-001.S01")

        assert _snapshot(outside) == before
        assert no_path_chown == []

    def test_task_output_dir_is_created(self, workspace: Path) -> None:
        setup = WorkspaceSetup(str(workspace))

        path = setup.ensure_task_output_dir("AB", "AB-001.S01")

        assert path == str(workspace / "outputs" / "AB" / "AB-001.S01")
        assert (workspace / "outputs" / "AB" / "AB-001.S01").is_dir()
        assert setup.ensure_task_output_dir("") == str(workspace / "outputs")

    def test_task_output_dir_refuses_a_multi_component_name(
        self, workspace: Path
    ) -> None:
        with pytest.raises(OSError):
            WorkspaceSetup(str(workspace)).ensure_task_output_dir("AB", "../x")
        assert not (workspace / "x").exists()


class TestEnvironmentalFailures:
    """SEC1-SWALLOW: only a planted entry is skipped. Any other write failure
    (no space, I/O) still fails config sync, after the remaining files are
    written, so admission stays closed and the sync is retried."""

    @staticmethod
    def _fail_writes_in(
        monkeypatch: pytest.MonkeyPatch, directory: Path, code: int
    ) -> None:
        real = claude_md_writer.atomic_replace_file

        def failing(directory_fd, filename, content, mode=0o644):
            if directory.exists() and os.fstat(directory_fd).st_ino == (
                directory.stat().st_ino
            ):
                raise OSError(code, os.strerror(code))
            return real(directory_fd, filename, content, mode)

        monkeypatch.setattr(claude_md_writer, "atomic_replace_file", failing)

    def test_no_space_for_the_manager_file_fails_sync_all_after_the_rest(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = workspace / "agents" / "manager"
        manager.mkdir(parents=True)
        self._fail_writes_in(monkeypatch, manager, errno.ENOSPC)

        with pytest.raises(OSError) as raised:
            ClaudeMdWriter(str(workspace)).sync_all(SYNC_CONFIG)

        assert raised.value.errno == errno.ENOSPC
        assert not (manager / "CLAUDE.md").exists()
        assert (workspace / "CLAUDE.md").is_file()
        assert (workspace / "agents" / "analyst" / "CLAUDE.md").is_file()
        assert (workspace / "workstreams" / "alpha" / "CLAUDE.md").is_file()

    def test_an_io_error_in_one_agent_fails_after_the_other_agents(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        analyst = workspace / "agents" / "analyst"
        analyst.mkdir(parents=True)
        self._fail_writes_in(monkeypatch, analyst, errno.EIO)

        with pytest.raises(OSError) as raised:
            ClaudeMdWriter(str(workspace)).sync_agent_directories([ANALYST, AUDITOR])

        assert raised.value.errno == errno.EIO
        assert "analyst" in str(raised.value)
        assert (workspace / "agents" / "auditor" / "CLAUDE.md").is_file()

    def test_no_space_for_the_office_file_fails_its_write(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._fail_writes_in(monkeypatch, workspace, errno.ENOSPC)

        with pytest.raises(OSError):
            ClaudeMdWriter(str(workspace)).write_office_claude_md({})

    def test_planted_entries_still_do_not_fail_sync_all(
        self, workspace: Path, outside: Path
    ) -> None:
        (workspace / "agents").mkdir()
        (workspace / "agents" / "analyst").symlink_to(outside)
        (workspace / "CLAUDE.md").mkdir()

        ClaudeMdWriter(str(workspace)).sync_all(SYNC_CONFIG)

        assert (workspace / "agents" / "manager" / "CLAUDE.md").is_file()

    def test_a_skill_link_failure_fails_agent_workspaces_after_the_rest(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = WorkspaceSetup._sync_agent_skills

        def failing(self, agent_fd, agent_name, skill_names):
            if agent_name == "analyst":
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
            return real(self, agent_fd, agent_name, skill_names)

        monkeypatch.setattr(WorkspaceSetup, "_sync_agent_skills", failing)

        with pytest.raises(OSError) as raised:
            WorkspaceSetup(str(workspace)).sync_agent_workspaces([ANALYST, AUDITOR])

        assert raised.value.errno == errno.ENOSPC
        assert (workspace / "agents" / "auditor" / ".claude" / "skills").is_dir()

    def test_a_base_directory_failure_fails_setup_after_the_rest(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = workspace_setup.ensure_owned_directory

        def failing(parent_fd, *parts):
            if parts == (".scripts",):
                raise OSError(errno.EROFS, os.strerror(errno.EROFS))
            return real(parent_fd, *parts)

        monkeypatch.setattr(workspace_setup, "ensure_owned_directory", failing)

        with pytest.raises(OSError) as raised:
            WorkspaceSetup(str(workspace)).ensure_structure()

        assert raised.value.errno == errno.EROFS
        assert (workspace / "outputs").is_dir()


def _run_as_session_uid(scenario, workspace: Path) -> str:
    """Run ``scenario(workspace)`` with the permissions of a non-root daemon
    that shares the agent uid (a dev host, macOS Docker Desktop).

    Run as root (the Linux lane), it is forked and ``setuid(1000)`` with the
    workspace owned by 1000; otherwise it runs in-process with the current
    uid. The scenario returns "ok" or a description of what went wrong.
    """
    if os.geteuid() != 0:
        return scenario(workspace)
    for path in [workspace, *workspace.rglob("*")]:
        os.lchown(path, 1000, 1000)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child process
        os.close(read_fd)
        try:
            os.setgid(1000)
            os.setuid(1000)
            result = scenario(workspace)
        except BaseException as exc:  # noqa: BLE001 - reported to the parent
            result = f"{type(exc).__name__}: {exc}"
        os.write(write_fd, result.encode())
        os._exit(0)
    os.close(write_fd)
    chunks = []
    while chunk := os.read(read_fd, 65536):
        chunks.append(chunk)
    os.close(read_fd)
    os.waitpid(pid, 0)
    return b"".join(chunks).decode()


@pytest.fixture
def session_workspace():
    """A workspace outside pytest's root-only tmp tree (the session uid must
    reach it); every mode is restored before removal."""
    base = Path(tempfile.mkdtemp(prefix="cubicle-perm-"))
    os.chmod(base, 0o755)
    workspace = base / "workspace"
    workspace.mkdir()
    yield workspace
    for path in [base, *base.rglob("*")]:
        if not path.is_symlink():
            os.chmod(path, 0o755)
    shutil.rmtree(base)


PERMISSION_CONFIG = {
    "office_name": "O",
    "agents": [ANALYST, AUDITOR],
    "workstreams": [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "name": "Alpha",
            "short_code": "AL",
        },
        {
            "id": "22222222-2222-2222-2222-222222222222",
            "name": "Beta",
            "short_code": "BE",
        },
    ],
}


def _full_sync(workspace: Path) -> None:
    ClaudeMdWriter(str(workspace)).sync_all(PERMISSION_CONFIG)
    setup = WorkspaceSetup(str(workspace))
    setup.ensure_structure()
    setup.sync_agent_workspaces(PERMISSION_CONFIG["agents"])
    setup.sync_workstream_outputs(PERMISSION_CONFIG["workstreams"])


class TestSessionPermissionChanges:
    """R3-SEC1: a session (same uid as a non-root daemon) that removes
    permissions on an entry it owns never fails config sync or office
    setup; the other entries are still written."""

    @pytest.mark.parametrize(
        ("entry", "mode"),
        [
            ("agents/analyst", 0o555),
            ("agents/analyst", 0o000),
            ("agents/analyst/.claude", 0o000),
            ("outputs/AL", 0o000),
            (".cubicle", 0o000),
        ],
    )
    def test_a_permission_a_session_removed_is_skipped(
        self, session_workspace: Path, entry: str, mode: int
    ) -> None:
        def scenario(workspace: Path) -> str:
            _full_sync(workspace)
            (workspace / "agents" / "auditor" / "CLAUDE.md").unlink()
            os.rmdir(workspace / "outputs" / "BE")
            os.chmod(workspace / entry, mode)
            for _ in range(2):
                _full_sync(workspace)
            os.chmod(workspace / entry, 0o755)
            if not (workspace / "agents" / "auditor" / "CLAUDE.md").is_file():
                return "the other agent's CLAUDE.md was not written"
            if not (workspace / "outputs" / "BE").is_dir():
                return "the other output directory was not created"
            return "ok"

        assert _run_as_session_uid(scenario, session_workspace) == "ok"

    def test_an_orphan_that_cannot_be_removed_is_left(
        self, session_workspace: Path
    ) -> None:
        def scenario(workspace: Path) -> str:
            _full_sync(workspace)
            locked = workspace / "agents" / "gone" / "locked"
            locked.mkdir(parents=True)
            (locked / "keep.txt").write_text("x")
            os.chmod(locked, 0o555)
            (workspace / "agents" / "auditor" / "CLAUDE.md").unlink()
            _full_sync(workspace)
            os.chmod(locked, 0o755)
            if not (locked / "keep.txt").exists():
                return "the orphan was removed"
            if not (workspace / "agents" / "auditor" / "CLAUDE.md").is_file():
                return "the other agent's CLAUDE.md was not written"
            return "ok"

        assert _run_as_session_uid(scenario, session_workspace) == "ok"

    def test_a_deep_orphan_that_exhausts_descriptors_is_left(
        self, session_workspace: Path
    ) -> None:
        """EMFILE from removing a session-built deep tree is cleanup, not a
        materialization failure."""

        def scenario(workspace: Path) -> str:
            _full_sync(workspace)
            deep = workspace / "agents" / "zzz"
            deep.joinpath(*["a"] * 150).mkdir(parents=True)
            open_now = len(os.listdir("/dev/fd"))
            resource.setrlimit(resource.RLIMIT_NOFILE, (open_now + 40, open_now + 40))
            _full_sync(workspace)
            if not deep.exists():
                return "the orphan was removed"
            return "ok"

        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child process (keeps the limit)
            os.close(read_fd)
            try:
                result = scenario(session_workspace)
            except BaseException as exc:  # noqa: BLE001 - reported to the parent
                result = f"{type(exc).__name__}: {exc}"
            os.write(write_fd, result.encode())
            os._exit(0)
        os.close(write_fd)
        result = os.read(read_fd, 65536).decode()
        os.close(read_fd)
        os.waitpid(pid, 0)
        assert result == "ok"

    @pytest.mark.parametrize("root", ["agents", "workstreams"])
    @pytest.mark.parametrize("mode", [0o444, 0o000])
    def test_a_permission_a_session_removed_on_a_root_directory_never_raises(
        self, session_workspace: Path, root: str, mode: int
    ) -> None:
        """R4-SEC-1: without search permission on ``agents/`` or
        ``workstreams/`` the listing's lstat fails; the entry is kept (never
        removed as an orphan) and no sync raises."""

        def scenario(workspace: Path) -> str:
            _full_sync(workspace)
            (workspace / root / "session-made").mkdir()
            os.chmod(workspace / root, mode)
            writer = ClaudeMdWriter(str(workspace))
            writer.sync_all(PERMISSION_CONFIG)
            writer.sync_all({**PERMISSION_CONFIG, "workstreams": []})
            setup = WorkspaceSetup(str(workspace))
            setup.ensure_structure()
            setup.sync_agent_workspaces(PERMISSION_CONFIG["agents"])
            setup.sync_workstream_outputs(PERMISSION_CONFIG["workstreams"])
            os.chmod(workspace / root, 0o755)
            if not (workspace / root / "session-made").is_dir():
                return "the session-made directory was removed"
            _full_sync(workspace)
            if not (workspace / "agents" / "auditor" / "CLAUDE.md").is_file():
                return "a later sync did not write the agent CLAUDE.md"
            return "ok"

        assert _run_as_session_uid(scenario, session_workspace) == "ok"

    def test_a_workspace_root_a_session_locked_is_restored(
        self, session_workspace: Path
    ) -> None:
        """R4-SEC-2: the session owns the root (office setup gives it to the
        agent uid) and can remove its permissions; a daemon that owns it
        restores the owner's access instead of stopping office setup and
        every config sync."""

        def scenario(workspace: Path) -> str:
            _full_sync(workspace)
            (workspace / "agents" / "auditor" / "CLAUDE.md").unlink()
            os.chmod(workspace, 0o000)
            WorkspaceSetup(str(workspace)).ensure_structure()
            if os.stat(workspace).st_mode & 0o700 != 0o700:
                return f"mode not restored: {os.stat(workspace).st_mode:o}"
            os.chmod(workspace, 0o000)
            _full_sync(workspace)
            if not (workspace / "agents" / "auditor" / "CLAUDE.md").is_file():
                return "the sync after restoring did not write the agent CLAUDE.md"
            return "ok"

        assert _run_as_session_uid(scenario, session_workspace) == "ok"

    def test_a_workspace_root_the_daemon_cannot_open_names_the_repair(
        self, session_workspace: Path
    ) -> None:
        """A root the daemon neither owns nor can open stays fatal, with one
        clear error naming the path and the repair."""
        if os.geteuid() != 0:
            pytest.skip("needs root to hand the root to another owner")
        workspace = session_workspace
        os.chmod(workspace, 0o000)  # still owned by root

        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child process
            os.close(read_fd)
            try:
                os.setgid(1000)
                os.setuid(1000)
                WorkspaceSetup(str(workspace)).ensure_structure()
                result = "no error"
            except OSError as exc:
                result = f"{exc.errno}|{exc}"
            os.write(write_fd, result.encode())
            os._exit(0)
        os.close(write_fd)
        result = os.read(read_fd, 65536).decode()
        os.close(read_fd)
        os.waitpid(pid, 0)
        code, _, message = result.partition("|")
        assert code == str(errno.EACCES)
        assert str(workspace) in message and "chmod u+rwx" in message

    def test_a_directory_planted_at_the_temporary_name_is_skipped(
        self, workspace: Path
    ) -> None:
        """unlink() of a directory is EISDIR on Linux and EPERM on macOS: both
        are skipped."""
        (workspace / "agents" / "analyst").mkdir(parents=True)
        (workspace / "agents" / "analyst" / f".CLAUDE.md.{os.getpid()}.tmp").mkdir()
        (workspace / f".CLAUDE.md.{os.getpid()}.tmp").mkdir()

        ClaudeMdWriter(str(workspace)).sync_all(SYNC_CONFIG)

        assert (workspace / "agents" / "manager" / "CLAUDE.md").is_file()
