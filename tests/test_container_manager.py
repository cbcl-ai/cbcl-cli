"""Tests for the ContainerManager and Docker integration."""

from __future__ import annotations

import pytest

from src.config import OfficeConfig
from src.docker.container_manager import ContainerManager, IMAGE_TAG
from src.paths import slugify


class TestSlugify:
    """Tests for the slugify helper."""

    def test_simple_name(self):
        assert slugify("My Office") == "my-office"

    def test_special_characters(self):
        assert slugify("Recruitment & Hiring!") == "recruitment-hiring"

    def test_trailing_leading_hyphens(self):
        assert slugify("  --My Office--  ") == "my-office"

    def test_empty_name(self):
        assert slugify("") == "office"

    def test_unicode(self):
        # Non-ascii chars get stripped, fallback to "office"
        result = slugify("café")
        assert result  # Should not be empty

    def test_numbers(self):
        assert slugify("Office 42") == "office-42"


class TestContainerManagerDirectMode:
    """Tests for ContainerManager when use_docker=False (direct mode)."""

    def test_init_direct_mode(self):
        cm = ContainerManager(use_docker=False)
        assert cm.use_docker is False
        assert cm._containers == {}

    @pytest.mark.asyncio
    async def test_ensure_container_noop_in_direct_mode(self):
        cm = ContainerManager(use_docker=False)
        office = OfficeConfig(id="test-id", name="Test Office")
        await cm.ensure_container(office)
        assert cm._containers == {}

    @pytest.mark.asyncio
    async def test_stop_all_noop_in_direct_mode(self):
        cm = ContainerManager(use_docker=False)
        await cm.stop_all()  # Should not raise

    def test_get_container_name_returns_none_in_direct_mode(self):
        cm = ContainerManager(use_docker=False)
        assert cm.get_container_name("any-office-id") is None

    def test_get_container_name_returns_name_when_tracked(self):
        from unittest.mock import MagicMock
        cm = ContainerManager(use_docker=False)
        mock_container = MagicMock()
        mock_container.name = "cbcl-office-test"
        cm._containers["office-1"] = mock_container
        assert cm.get_container_name("office-1") == "cbcl-office-test"

    def test_get_container_name_returns_none_when_not_tracked(self):
        cm = ContainerManager(use_docker=False)
        assert cm.get_container_name("nonexistent") is None

    @pytest.mark.asyncio
    async def test_get_status_not_running(self):
        cm = ContainerManager(use_docker=False)
        status = await cm.get_status("nonexistent")
        assert status == {"status": "not_running"}


class TestContainerManagerDockerMode:
    """Tests for ContainerManager Docker mode (mocked docker client)."""

    def test_init_docker_mode(self):
        cm = ContainerManager(use_docker=True)
        assert cm.use_docker is True
        assert cm._client is None  # Lazy init

    def test_image_tag_constant(self):
        assert IMAGE_TAG == "cbcl-agent:latest"

    @pytest.mark.asyncio
    async def test_ensure_image_noop_when_direct(self):
        cm = ContainerManager(use_docker=False)
        await cm.ensure_image()  # Should not raise

    @pytest.mark.asyncio
    async def test_stop_office_removes_from_tracking(self):
        cm = ContainerManager(use_docker=False)
        # Manually inject a mock container
        cm._containers["office-1"] = None
        await cm.stop_office("office-1")
        assert "office-1" not in cm._containers

    @pytest.mark.asyncio
    async def test_stop_office_nonexistent_noop(self):
        cm = ContainerManager(use_docker=False)
        await cm.stop_office("nonexistent")  # Should not raise


class TestSessionBridge:
    """Tests for the session_bridge module."""

    def test_session_message_dataclass(self):
        from src.docker.session_bridge import SessionMessage
        msg = SessionMessage(type="result", data={"session_id": "abc"})
        assert msg.type == "result"
        assert msg.data["session_id"] == "abc"


class TestManagerControllerConstruction:
    """Tests that ManagerController accepts new constructor signature."""

    def test_controller_accepts_new_params(self):
        from unittest.mock import MagicMock
        from src.orchestrator.manager_controller import ManagerController

        mgr = ManagerController(
            supervisor=MagicMock(),
            router=MagicMock(),
            session_manager=MagicMock(),
            config_store=MagicMock(),
            office_id="test-office",
            workspace_path="/tmp/workspace",
        )
        assert mgr._office_id == "test-office"
        assert mgr._workspace_path == "/tmp/workspace"

    def test_controller_defaults(self):
        from unittest.mock import MagicMock
        from src.orchestrator.manager_controller import ManagerController

        mgr = ManagerController(
            supervisor=MagicMock(),
            router=MagicMock(),
            session_manager=MagicMock(),
            config_store=MagicMock(),
        )
        assert mgr._office_id == ""
        assert mgr._workspace_path == ""


# ─── extra_mounts plumbing (Security → Extra mounts office setting) ──


class TestApplyExtraMounts:
    """``_apply_extra_mounts`` is the defence-in-depth boundary
    between the backend's validated extra_mounts payload and the
    Docker SDK call that creates the office container. The backend
    already validates absolute paths / reserved-prefix rules; these
    tests pin the helper's behaviour on hostile or malformed input
    so a contract drift can't silently mount over the workspace."""

    def _base_volumes(self):
        return {
            "/host/workspace": {"bind": "/workspace", "mode": "rw"},
        }

    def test_none_input_is_noop(self):
        from src.docker.container_manager import _apply_extra_mounts
        vols = self._base_volumes()
        _apply_extra_mounts(vols, None, "cbcl-office-x")
        assert vols == self._base_volumes()

    def test_empty_list_is_noop(self):
        from src.docker.container_manager import _apply_extra_mounts
        vols = self._base_volumes()
        _apply_extra_mounts(vols, [], "cbcl-office-x")
        assert vols == self._base_volumes()

    def test_valid_mount_added_with_ro_default(self):
        """A path inside /home/agent that's NOT under the platform-
        reserved /home/agent/.ssh or /home/agent/.claude subtrees
        is permitted. SSH keys are now managed via the dedicated
        SSH Keys section, not Extra Mounts."""
        from src.docker.container_manager import _apply_extra_mounts
        vols = self._base_volumes()
        _apply_extra_mounts(vols, [
            {"host_path": "/host/creds", "container_path": "/home/agent/credentials.json"},
        ], "cbcl-office-x")
        assert vols["/host/creds"] == {
            "bind": "/home/agent/credentials.json",
            "mode": "ro",
        }

    def test_ssh_subtree_refused(self):
        """/home/agent/.ssh is owned by the SSH Keys feature; Extra
        Mounts pointing there would collide with the platform's
        bind mount and silently shadow user-managed keys."""
        from src.docker.container_manager import _apply_extra_mounts
        for bad in (
            "/home/agent/.ssh",
            "/home/agent/.ssh/id_ed25519",
            "/home/agent/.ssh/config",
        ):
            vols = self._base_volumes()
            _apply_extra_mounts(vols, [
                {"host_path": "/host/x", "container_path": bad},
            ], "cbcl-office-x")
            assert "/host/x" not in vols, (
                f"ssh subtree {bad!r} must be refused"
            )

    def test_claude_subtree_refused(self):
        """/home/agent/.claude is the Claude auth volume. Same
        reasoning as /home/agent/.ssh."""
        from src.docker.container_manager import _apply_extra_mounts
        for bad in (
            "/home/agent/.claude",
            "/home/agent/.claude/.credentials.json",
        ):
            vols = self._base_volumes()
            _apply_extra_mounts(vols, [
                {"host_path": "/host/x", "container_path": bad},
            ], "cbcl-office-x")
            assert "/host/x" not in vols

    def test_read_only_false_yields_rw(self):
        from src.docker.container_manager import _apply_extra_mounts
        vols = self._base_volumes()
        _apply_extra_mounts(vols, [
            {"host_path": "/host/data", "container_path": "/data",
             "read_only": False},
        ], "cbcl-office-x")
        assert vols["/host/data"]["mode"] == "rw"

    def test_reserved_container_path_refused(self):
        """Even if the backend payload is malformed, the container
        manager must not let an extra_mount clobber /workspace,
        /opt/cubicle, /etc, etc."""
        from src.docker.container_manager import _apply_extra_mounts
        for bad in (
            "/workspace",
            "/workspace/.scripts/foo",
            "/opt/cubicle/x",
            "/etc/something",
            "/var/run/x",
            "/home/agent",
        ):
            vols = self._base_volumes()
            _apply_extra_mounts(vols, [
                {"host_path": "/host/x", "container_path": bad},
            ], "cbcl-office-x")
            assert "/host/x" not in vols, (
                f"reserved container_path {bad!r} must be refused"
            )

    def test_relative_paths_refused(self):
        from src.docker.container_manager import _apply_extra_mounts
        vols = self._base_volumes()
        _apply_extra_mounts(vols, [
            {"host_path": "relative/host", "container_path": "/data"},
            {"host_path": "/host", "container_path": "relative/container"},
        ], "cbcl-office-x")
        assert "relative/host" not in vols
        assert vols == self._base_volumes()

    def test_path_traversal_refused(self):
        from src.docker.container_manager import _apply_extra_mounts
        vols = self._base_volumes()
        _apply_extra_mounts(vols, [
            {"host_path": "/host/../secret", "container_path": "/data"},
        ], "cbcl-office-x")
        assert "/host/../secret" not in vols

    def test_duplicate_container_path_refused(self):
        """If an extra mount targets an already-bound container path
        (e.g. /workspace via the platform mount), it must not silently
        shadow the platform's mount."""
        from src.docker.container_manager import _apply_extra_mounts
        vols = self._base_volumes()  # /workspace already bound
        _apply_extra_mounts(vols, [
            {"host_path": "/host/foo", "container_path": "/workspace"},
        ], "cbcl-office-x")
        # /workspace stays mapped to its original host path
        assert vols["/host/workspace"]["bind"] == "/workspace"
        assert "/host/foo" not in vols


class TestNormalizeMounts:
    def test_canonicalises_order(self):
        from src.config_sync.sync_service import _normalize_mounts
        a = [
            {"host_path": "/a", "container_path": "/data1", "read_only": True},
            {"host_path": "/b", "container_path": "/data2", "read_only": False},
        ]
        b = [
            {"host_path": "/b", "container_path": "/data2", "read_only": False},
            {"host_path": "/a", "container_path": "/data1", "read_only": True},
        ]
        assert _normalize_mounts(a) == _normalize_mounts(b)

    def test_missing_read_only_treated_as_true(self):
        from src.config_sync.sync_service import _normalize_mounts
        a = [{"host_path": "/a", "container_path": "/data"}]
        b = [{"host_path": "/a", "container_path": "/data", "read_only": True}]
        assert _normalize_mounts(a) == _normalize_mounts(b)

    def test_skips_malformed_entries(self):
        from src.config_sync.sync_service import _normalize_mounts
        out = _normalize_mounts([
            {"host_path": "/a", "container_path": "/data"},
            "not a dict",
            {"host_path": "", "container_path": "/data"},
            {"host_path": "/b"},  # missing container_path
        ])
        assert out == [("/a", "/data", True)]


class TestGetStatusByName:
    """Tests for ``ContainerManager.get_status_by_name`` — the read-
    only docker lookup used by ``cbcl status`` because it runs in a
    separate CLI process from the daemon and can't see the daemon's
    in-memory ``_containers`` dict.

    Regression: the previous ``cbcl status`` flow instantiated a
    fresh ``ContainerManager`` and called ``get_status(office_id)``
    which always returned ``not_running`` (the new dict has no
    entries for offices the DAEMON spawned in a different process).
    The user saw "Container: not_running" on every office despite
    the UI correctly showing every office as connected. This method
    bypasses the cache and asks docker directly.
    """

    @pytest.mark.asyncio
    async def test_running_container_returns_running_status(self, monkeypatch):
        cm = ContainerManager(use_docker=True)

        class _FakeContainer:
            status = "running"
            short_id = "abc123def456"
            attrs = {"State": {"StartedAt": "2026-05-25T18:00:00Z"}}

            def reload(self):
                pass

        class _FakeContainers:
            def get(self, name):
                assert name == "cbcl-office-dev"
                return _FakeContainer()

        class _FakeClient:
            containers = _FakeContainers()

        monkeypatch.setattr(cm, "_get_client", lambda: _FakeClient())

        result = await cm.get_status_by_name("cbcl-office-dev")
        assert result["status"] == "running"
        assert result["container_id"] == "abc123def456"
        assert result["started_at"] == "2026-05-25T18:00:00Z"

    @pytest.mark.asyncio
    async def test_missing_container_returns_not_running(self, monkeypatch):
        """NotFound from docker-py → user-facing ``not_running``,
        not a generic ``unknown`` (operator distinguishes 'docker
        is down' from 'this specific container doesn't exist')."""
        import docker.errors

        cm = ContainerManager(use_docker=True)

        class _FakeContainers:
            def get(self, name):
                raise docker.errors.NotFound("not found")

        class _FakeClient:
            containers = _FakeContainers()

        monkeypatch.setattr(cm, "_get_client", lambda: _FakeClient())
        result = await cm.get_status_by_name("cbcl-office-nope")
        assert result["status"] == "not_running"
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_other_error_returns_unknown(self, monkeypatch):
        """A non-NotFound docker error (daemon offline, permission
        denied) surfaces as ``unknown`` with the cause in ``error``
        so the operator sees the actual failure mode."""
        cm = ContainerManager(use_docker=True)

        class _FakeContainers:
            def get(self, name):
                raise RuntimeError("docker daemon offline")

        class _FakeClient:
            containers = _FakeContainers()

        monkeypatch.setattr(cm, "_get_client", lambda: _FakeClient())
        result = await cm.get_status_by_name("cbcl-office-x")
        assert result["status"] == "unknown"
        assert "docker daemon offline" in result["error"]

    @pytest.mark.asyncio
    async def test_get_status_unchanged_for_tracked_offices(self, monkeypatch):
        """The tracked-office path (daemon's own view) keeps
        working — running containers report ``running``, missing
        ones report ``not_running``. Locks in that the round-6
        refactor didn't break the daemon's own status query."""
        cm = ContainerManager(use_docker=True)

        # No tracked office → not_running (daemon's behaviour
        # before it spawns the container).
        result = await cm.get_status("office-untracked")
        assert result["status"] == "not_running"

        # Tracked office → reads from the in-memory container
        # object.
        class _FakeContainer:
            status = "running"
            short_id = "xyz"
            attrs = {"State": {"StartedAt": "2026-05-25T19:00:00Z"}}

            def reload(self):
                pass

        cm._containers["office-tracked"] = _FakeContainer()
        result = await cm.get_status("office-tracked")
        assert result["status"] == "running"
