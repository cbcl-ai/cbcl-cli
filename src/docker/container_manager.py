"""Docker container management for agent execution environments.

Manages one Docker container per office.  When ``use_docker`` is False
(the default, for development) the Communicator runs Agent SDK sessions
in-process.  When True, each office gets an isolated container running
the ``cbcl-agent:latest`` image.  The communicator invokes Claude CLI
directly via ``docker exec`` — no HTTP server inside the container.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from src.config import OfficeConfig
from src.paths import get_secrets_path, slugify

logger = logging.getLogger(__name__)

# Docker image used for office containers
IMAGE_TAG = "cbcl-agent:latest"

# Agent-image asset directory. Lives at ``src/_agent_image/`` so it
# ships INSIDE the installed wheel (pip / pipx). Pre-v0.1.x it lived
# at the repo root as ``communicator/docker/`` and was resolved with
# ``parent.parent.parent``, which only worked for editable installs
# from a source checkout — a real ``pip install`` would land on
# ``site-packages/docker/`` (the Docker Python SDK's directory) and
# fail with a confusing "Dockerfile.agent not found" inside the SDK.
# The leading underscore signals "bundled asset, not user-facing API".
_DOCKER_DIR = Path(__file__).resolve().parent.parent / "_agent_image"

# Container paths the platform owns. Mounting on top of these would
# break the office runtime. Mirrors the backend's
# ``_RESERVED_CONTAINER_PATH_PREFIXES`` in
# ``backend/app/offices/schemas.py`` — duplicated here so a malformed
# payload from a misconfigured backend can't silently break the
# container.
_RESERVED_CONTAINER_PATH_PREFIXES = (
    "/workspace",
    "/opt/cubicle",
    "/usr/local",
    "/var",
    "/etc",
    "/proc",
    "/sys",
    "/dev",
    "/root",
    # ``/home/agent/.ssh`` is reserved for the per-office SSH-keys
    # volume the platform manages (Settings → Security → SSH Keys).
    # Letting Extra Mounts target a path inside it would shadow
    # user-managed keys with whatever the user put in the mount,
    # confusingly silently. Mounts targeting a single specific
    # key file under it (``/home/agent/.ssh/id_legacy``) would also
    # collide with the volume mount and Docker would refuse the
    # container, so refuse early with a clear message.
    "/home/agent/.ssh",
    # The Claude auth volume.
    "/home/agent/.claude",
)


def _is_reserved_container_path(container_path: str) -> bool:
    """Return True if ``container_path`` overlaps a reserved system
    path. Bare ``/home/agent`` would shadow the agent user's home,
    so it's refused too — but paths INSIDE it (other than the
    reserved subtrees) are allowed."""
    cp = container_path.rstrip("/") or "/"
    if cp == "/home/agent":
        return True
    for prefix in _RESERVED_CONTAINER_PATH_PREFIXES:
        if cp == prefix or cp.startswith(prefix + "/"):
            return True
    return False


def _apply_extra_mounts(
    volumes: dict[str, dict],
    extra_mounts: list[dict] | None,
    container_name: str,
) -> None:
    """Merge per-office extra mounts into the Docker volumes dict.

    Defensively skips entries that fail any of: absolute paths,
    no '..' in host_path, container_path not in the reserved set,
    no overlap with an already-bound container path (which would
    silently shadow the workspace mount). Each skip logs a clear
    warning so the user can diagnose without spelunking Docker
    errors.
    """
    if not extra_mounts:
        return
    bound_containers = {entry["bind"] for entry in volumes.values()}
    for m in extra_mounts:
        host_path = str(m.get("host_path") or "").strip()
        container_path = str(m.get("container_path") or "").strip()
        read_only = bool(m.get("read_only", True))
        if not host_path.startswith("/") or not container_path.startswith("/"):
            logger.warning(
                "Skipping extra_mount with non-absolute path "
                "(container=%s, host=%r, container=%r)",
                container_name, host_path, container_path,
            )
            continue
        if ".." in host_path.split("/") or ".." in container_path.split("/"):
            logger.warning(
                "Skipping extra_mount with '..' segment "
                "(container=%s, host=%r)",
                container_name, host_path,
            )
            continue
        if _is_reserved_container_path(container_path):
            logger.warning(
                "Refusing extra_mount on reserved container_path %r "
                "(container=%s)", container_path, container_name,
            )
            continue
        if container_path in bound_containers:
            logger.warning(
                "Skipping extra_mount — container_path %r is already "
                "bound by the platform (container=%s)",
                container_path, container_name,
            )
            continue
        if not Path(host_path).exists():
            # Docker will fail-fast if the host path doesn't exist;
            # surface a clear log entry so the user knows which
            # mount caused the failure.
            logger.warning(
                "extra_mount host_path %r does not exist on host — "
                "the container will fail to start (container=%s). "
                "Either create the path or remove the mount.",
                host_path, container_name,
            )
        volumes[host_path] = {
            "bind": container_path,
            "mode": "ro" if read_only else "rw",
        }
        bound_containers.add(container_path)
        logger.info(
            "Applied extra_mount %s → %s (%s) for container %s",
            host_path, container_path,
            "ro" if read_only else "rw", container_name,
        )


def _compute_mcp_server_hash() -> str:
    """Hash the agent image's build inputs for image-cache invalidation.

    Original P3-F design hashed only the MCP server source files
    (``mcp_tool_server.py`` + ``_mcp/*.py``). That left a gap: a
    ``Dockerfile.agent`` change — e.g. adding a pip dependency that
    the MCP server actually needs — would NOT invalidate the cached
    image, so the next ``cbcl start`` would happily reuse the stale
    image and every ``execute_script`` call would explode with
    ``No module named 'X'`` at runtime.

    We hit exactly that failure mode when a transitive PyYAML
    dependency disappeared from one of the listed packages. To
    prevent recurrence, hash the Dockerfile too — any change to
    the build recipe forces a rebuild.

    Returns the first 12 hex chars of an MD5 over the concatenated
    files. MD5 because we're invalidating a cache, not authenticating.
    """
    import hashlib

    h = hashlib.md5()
    # Dockerfile FIRST so a pip-deps change is immediately visible
    # in the hash without depending on any other file changing.
    dockerfile = _DOCKER_DIR / "Dockerfile.agent"
    if dockerfile.exists():
        h.update(dockerfile.read_bytes())
    entrypoint = _DOCKER_DIR / "mcp_tool_server.py"
    if entrypoint.exists():
        h.update(entrypoint.read_bytes())
    # Wave 11 sibling modules: each must invalidate the image cache
    # on its own. They're separate top-level files (NOT inside the
    # ``_mcp`` package below) so the ``_mcp`` loop doesn't catch
    # them. Without these lines, editing ``_mcp_script_exec`` would
    # ship a stale agent image that imports the OLD copy at runtime.
    # Names must match the ``COPY`` lines in ``Dockerfile.agent``.
    for sibling in ("_mcp_backend.py", "_mcp_script_exec.py"):
        sibling_path = _DOCKER_DIR / sibling
        if sibling_path.exists():
            h.update(sibling_path.read_bytes())
    mcp_pkg = _DOCKER_DIR / "_mcp"
    if mcp_pkg.is_dir():
        for path in sorted(mcp_pkg.glob("*.py")):
            h.update(path.read_bytes())
    return h.hexdigest()[:12]


def _ensure_bind_mount_ownership(container, container_name: str) -> None:
    """Chown the auth + ssh + workspace bind-mount dirs to agent.

    Three host directories get bind-mounted into the container:
    ``/home/agent/.claude``, ``/home/agent/.ssh``, and
    ``/workspace``. All three are created by cbcl on the host as
    root (the daemon's effective UID), and Docker bind mounts
    preserve host UIDs — so inside the container all three land
    as ``root:root``. But the container runs as ``USER agent``
    (Claude CLI refuses to run as root), so the agent user can't
    write to any of them.

    Three concrete symptoms motivated this fix, all the same bug:

    1. ``cbcl auth`` died with ``bash: line 1:
       /home/agent/.claude/.credentials.json: Permission denied``
       because ``_write_credentials`` runs ``cat > .credentials.json``
       as the agent user.
    2. Manager chat first turn died with "Failed to write system
       prompt file to container" because
       ``session_bridge.stream_cli_session`` runs ``tee
       /workspace/.cubicle/.prompt-XXXX`` as agent.
    3. Same class of writes into ``/home/agent/.ssh`` for SSH key
       management.

    Strategy per path:

    * ``/home/agent/.claude``, ``/home/agent/.ssh`` — chown -R is
      safe: the user never directly populates these dirs from the
      host, so flipping every file inside has no surprises.
    * ``/workspace`` — chown ONLY the top-level dir + the
      platform-managed ``/workspace/.cubicle/`` subdir. We avoid
      ``-R`` because the user can drop files into the workspace
      via the office's bind mount (think pre-existing source
      trees with their own UID expectations) and a recursive
      chown would silently rewrite those.

    Runs as ``user="0"`` (root) because the agent user can't
    chown root-owned files. Idempotent — chown-to-same-user on a
    subsequent start is a no-op.
    """
    try:
        container.exec_run(
            ["bash", "-c",
             # Workspace bind-mount lid + the two platform-managed
             # subdirs we know we'll write into. ``.claude/`` covers
             # the SKILL.md write path the AI-skill-gen handler uses
             # (the daemon writes /workspace/.claude/skills/<name>/
             # SKILL.md inline as of cbcl 0.2.10) — without this
             # chown a sudo-cbcl deployment lands the directory as
             # root-owned and ``mkdir -p`` from the agent user fails.
             # ``.cubicle/`` is the per-turn prompt scratch dir the
             # session_bridge writes the system prompt into. Both
             # ``mkdir -p`` are no-ops on rebuilds.
             #
             # ``outputs/`` and ``.scripts/`` are recursive on
             # purpose: they're platform-managed dirs that historical
             # sudo-cbcl runs (or a container restart after a
             # sudo-spawned script wrote there) can leave root-owned,
             # wedging future agent writes with confusing EACCES
             # errors deep into a 30-minute task. Recursive chown is
             # safe on these two paths because every legitimate writer
             # is the agent user — but explicitly NOT recursive on
             # /workspace itself (which holds user project files
             # whose ownership we must not silently rewrite).
             "chown agent:agent /workspace && "
             "mkdir -p /workspace/.cubicle && "
             "chown agent:agent /workspace/.cubicle && "
             "mkdir -p /workspace/.claude/skills && "
             "chown -R agent:agent /workspace/.claude && "
             "mkdir -p /workspace/outputs /workspace/.scripts && "
             "chown -R agent:agent /workspace/outputs /workspace/.scripts && "
             "chown -R agent:agent /home/agent/.claude /home/agent/.ssh && "
             "chmod 700 /home/agent/.ssh"],
            user="0",
        )
    except Exception as exc:
        logger.warning(
            "Container %s: failed to chown bind-mount dirs: %s",
            container_name, exc,
        )


class ContainerManager:
    """Manages Docker containers for office execution environments."""

    def __init__(self, use_docker: bool = False) -> None:
        self.use_docker = use_docker
        self._client: Any | None = None  # docker.DockerClient (lazy)
        self._containers: dict[str, Any] = {}  # office_id -> Container

    # -- Docker client (lazy init) ------------------------------------------

    def _get_client(self) -> Any:
        """Return a ``docker.DockerClient``, creating it on first use."""
        if self._client is None:
            try:
                import docker
                self._client = docker.from_env()
            except ImportError:
                raise RuntimeError(
                    "docker package not installed. "
                    "Install with: pip install 'cubicle-communicator[docker]'"
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot connect to Docker daemon: {exc}"
                ) from exc
        return self._client

    # -- Image management ---------------------------------------------------

    async def ensure_image(self) -> None:
        """Build the agent image if missing or if source files changed."""
        if not self.use_docker:
            return
        client = self._get_client()

        need_build = False
        try:
            image = client.images.get(IMAGE_TAG)
            # Check if source files are newer than the image.
            # P3-F: the MCP tool server is now split across the
            # entrypoint + the ``_mcp`` sibling package, so the hash
            # has to cover all of them or a worker_tools.py edit
            # would silently ship the stale image.
            stored_hash = image.labels.get("mcp_server_hash", "")
            current_hash = _compute_mcp_server_hash()
            if stored_hash and stored_hash == current_hash:
                logger.debug("Image %s is up to date (hash match)", IMAGE_TAG)
            else:
                logger.info(
                    "Image %s is stale (hash %s != %s) — rebuilding",
                    IMAGE_TAG, stored_hash or "none", current_hash,
                )
                need_build = True
        except Exception as exc:
            import docker.errors
            if isinstance(exc, docker.errors.ImageNotFound):
                logger.info("Image %s not found — building...", IMAGE_TAG)
                need_build = True
            else:
                raise

        if need_build:
            await self._build_image()

    async def _build_image(self) -> None:
        """Build the cbcl-agent image from the Dockerfile."""
        content_hash = _compute_mcp_server_hash()

        dockerfile_path = _DOCKER_DIR / "Dockerfile.agent"
        if not dockerfile_path.exists():
            raise FileNotFoundError(f"Dockerfile not found: {dockerfile_path}")
        logger.info("Building %s (hash=%s)...", IMAGE_TAG, content_hash)
        # Use docker CLI directly — faster than docker-py for builds
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "docker", "build",
                "-t", IMAGE_TAG,
                "-f", str(dockerfile_path),
                "--label", f"mcp_server_hash={content_hash}",
                str(_DOCKER_DIR),
            ],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.error("Image build failed:\n%s", result.stderr)
            raise RuntimeError(f"Failed to build {IMAGE_TAG}: {result.stderr}")
        logger.info("Image %s built successfully", IMAGE_TAG)

    # -- Container lifecycle ------------------------------------------------

    async def ensure_container(self, office: OfficeConfig) -> None:
        """Ensure a Docker container is running for the office."""
        if not self.use_docker:
            logger.debug(
                "Docker disabled — agent SDK runs in-process for %s",
                office.name,
            )
            return
        await self.start_office(
            office_slug=slugify(office.name),
            office_id=office.id,
            workspace_path=office.workspace_path,
            extra_mounts=office.extra_mounts,
        )

    async def start_office(
        self, office_slug: str, office_id: str, workspace_path: str,
        extra_mounts: list[dict] | None = None,
    ) -> str:
        """Start a Docker container for an office. Returns container ID.

        ``extra_mounts`` is the per-office "Mounts" tab list
        (host→container bind mounts). Applied to the volumes dict
        on first create; ignored when the container is already running
        (Docker doesn't allow adding mounts to a running container —
        the user must restart the office to apply new mounts).
        """
        from src.config import get_api_key

        client = self._get_client()
        container_name = f"cbcl-office-{office_slug}"

        # Check if already running
        try:
            existing = client.containers.get(container_name)
            if existing.status == "running":
                logger.info(
                    "Container %s already running for office %s",
                    container_name, office_id,
                )
                self._containers[office_id] = existing
                # Re-apply the auth-dir chown on the existing
                # container too — operators who started their
                # container with an older cbcl (before the chown
                # fix shipped) need it applied on next start to
                # unblock ``cbcl auth``. Idempotent.
                await asyncio.to_thread(
                    _ensure_bind_mount_ownership, existing, container_name,
                )
                return existing.id
            existing.remove(force=True)
        except Exception as exc:
            import docker.errors
            if not isinstance(exc, docker.errors.NotFound):
                raise

        volumes: dict[str, dict] = {
            workspace_path: {"bind": "/workspace", "mode": "rw"},
        }
        secrets_dir = get_secrets_path()
        if secrets_dir.exists():
            volumes[str(secrets_dir)] = {"bind": "/secrets", "mode": "ro"}

        # Persistent Claude auth volume — survives container restarts/rebuilds.
        # `claude auth login` stores credentials in ~/.claude/ inside the
        # container. We mount a host directory so the token persists.
        auth_dir = Path(workspace_path) / ".claude-auth"
        auth_dir.mkdir(parents=True, exist_ok=True)
        volumes[str(auth_dir)] = {"bind": "/home/agent/.claude", "mode": "rw"}

        # Persistent SSH-keys volume — bound to /home/agent/.ssh so
        # the UI-added keys survive container teardown and apply at
        # next start. The Communicator writes new keys here on the
        # fly and also uses ``docker exec`` to write them into the
        # live container; on a fresh start the mount makes them
        # available without any exec at all. The chmod-mkdir-0700
        # contract lives in ``src/ssh_keys/store.py`` so the bind
        # mount and runtime writes use exactly the same
        # configuration.
        from src.ssh_keys.store import ensure_ssh_dir_for_workspace
        ssh_keys_dir = ensure_ssh_dir_for_workspace(workspace_path)
        volumes[str(ssh_keys_dir)] = {"bind": "/home/agent/.ssh", "mode": "rw"}

        # Apply per-office extra mounts. Backend already validates
        # absolute paths + reserved-prefix rules; we apply defence-
        # in-depth here so a malformed payload (or future contract
        # drift) can't silently break the container.
        _apply_extra_mounts(volumes, extra_mounts, container_name)

        logger.info(
            "Starting container %s for office %s (workspace=%s)",
            container_name, office_id, workspace_path,
        )

        # Build environment variables
        env: dict[str, str] = {
            "OFFICE_ID": office_id,
        }
        # If user has an API key configured, pass it as fallback.
        # Primary auth is via `claude auth login` (subscription token
        # stored in the persistent auth volume). The API key env var
        # is a fallback for cases where login wasn't done.
        api_key = get_api_key()
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key

        container = await asyncio.to_thread(
            client.containers.run,
            IMAGE_TAG,
            name=container_name,
            detach=True,
            volumes=volumes,
            environment=env,
            # ``host.docker.internal`` is auto-resolved on Docker Desktop
            # (Mac / Windows) but NOT on Linux daemons. The tool-proxy
            # server runs on the host at ``http://host.docker.internal:
            # <port>`` and the in-container MCP server's
            # ``execute_script`` path POSTs to it for scripts that
            # reference Office Secrets (the host-only secret store).
            # Without this ``extra_hosts`` mapping, the in-container DNS
            # lookup for ``host.docker.internal`` fails and every
            # office-secret-using script errors with
            # ``ClientConnectorDNSError: Could not reach the host-side
            # script runner via the tool proxy. Is cbcl running?`` —
            # which it was. ``host-gateway`` is the Docker 20.10+
            # special value that resolves to the host's default
            # gateway IP from the container's perspective.
            extra_hosts={"host.docker.internal": "host-gateway"},
            # No port mapping — no HTTP server inside the container.
            # Communication is via docker exec (subprocess streaming).
            restart_policy={"Name": "unless-stopped"},
            mem_limit="8g",
            cpu_period=100000,
            cpu_quota=400000,  # 4 CPUs
        )

        self._containers[office_id] = container
        await asyncio.to_thread(container.reload)

        # Fix bind-mount ownership (see ``_ensure_bind_mount_ownership``).
        await asyncio.to_thread(
            _ensure_bind_mount_ownership, container, container_name,
        )

        # Symlink ~/.claude.json -> ~/.claude/.claude.json inside the container.
        # The Claude CLI stores auth credentials in ~/.claude/.credentials.json
        # but also requires ~/.claude.json (at home root) for config metadata.
        # Our persistent auth volume mounts as ~/.claude/, so .claude.json
        # written there needs a symlink at the home root.
        try:
            await asyncio.to_thread(
                container.exec_run,
                ["bash", "-c",
                 "ln -sf /home/agent/.claude/.claude.json /home/agent/.claude.json 2>/dev/null; "
                 "touch /home/agent/.claude/.claude.json"],
            )
        except Exception:
            pass  # Non-critical — CLI may create it on first run

        logger.info(
            "Container %s started: id=%s",
            container_name, container.short_id,
        )
        return container.id

    async def stop_office(self, office_id: str) -> None:
        """Stop and remove the container."""
        container = self._containers.pop(office_id, None)
        if container:
            try:
                await asyncio.to_thread(container.stop, timeout=30)
                await asyncio.to_thread(container.remove)
                logger.info("Container for office %s stopped and removed", office_id)
            except Exception as exc:
                logger.warning(
                    "Error stopping container for office %s: %s", office_id, exc,
                )

    async def restart_office(
        self, office_id: str, office_slug: str, workspace_path: str,
    ) -> None:
        """Stop then start."""
        await self.stop_office(office_id)
        await self.start_office(office_slug, office_id, workspace_path)

    async def stop_all(self) -> None:
        """Stop all running containers."""
        if not self.use_docker:
            return
        for office_id in list(self._containers):
            await self.stop_office(office_id)

    # -- Status -------------------------------------------------------------

    async def get_status(self, office_id: str) -> dict:
        """Get container status, tracked office first then docker fallback.

        Tracked path (daemon process): look up the office in the
        in-memory ``self._containers`` dict. This is the daemon's own
        view of containers it spawned + manages.

        Docker fallback (``cbcl status`` CLI process): the dict is
        empty because we're a SEPARATE process from the running
        daemon — the daemon's in-memory state isn't visible here.
        Fall back to asking the docker daemon directly by name, which
        is what the user actually wants to know ("is the container
        I see in ``docker ps`` running?").
        """
        container = self._containers.get(office_id)
        if container is not None:
            try:
                await asyncio.to_thread(container.reload)
                started_at = container.attrs.get("State", {}).get("StartedAt", "")
                return {
                    "status": container.status,
                    "container_id": container.short_id,
                    "started_at": started_at,
                }
            except Exception as exc:
                logger.debug("Error getting container status for %s: %s", office_id, exc)
                return {"status": "unknown", "error": str(exc)}
        return {"status": "not_running"}

    async def get_status_by_name(self, container_name: str) -> dict:
        """Read-only docker lookup by container name.

        Used by ``cbcl status`` (a separate CLI process from the
        running daemon — can't see the daemon's in-memory
        ``_containers`` dict). Previously the CLI reported every
        container as ``not_running`` because of that visibility gap;
        this method bypasses the in-memory cache and queries the
        Docker daemon directly.

        Returns the same shape as :meth:`get_status`. Distinguishes
        three states the operator cares about:
          * ``not_running`` — no container with that name exists.
          * ``unknown`` — container exists but docker reload raised.
          * Any docker status string (``running`` / ``exited`` / …).
        """
        try:
            client = self._get_client()
            container = await asyncio.to_thread(
                client.containers.get, container_name,
            )
            await asyncio.to_thread(container.reload)
            started_at = container.attrs.get("State", {}).get("StartedAt", "")
            return {
                "status": container.status,
                "container_id": container.short_id,
                "started_at": started_at,
            }
        except Exception as exc:
            # docker-py raises NotFound when the name doesn't exist,
            # APIError on daemon issues. Treat NotFound as the
            # "container isn't running" answer, everything else as
            # opaque error so the operator sees the actual cause.
            import docker.errors
            if isinstance(exc, docker.errors.NotFound):
                return {"status": "not_running"}
            logger.debug(
                "Error querying container %s: %s", container_name, exc,
            )
            return {"status": "unknown", "error": str(exc)}

    async def force_restart_office(self, office_id: str) -> None:
        """Force-restart an office container in place.

        Used by the health-check loop's ``on_restart`` escalation
        path: when a container has been ``exited`` for three
        consecutive checks (90 seconds), the loop calls this method
        to bring it back. Without this method wired in, the escalation
        was a no-op — the log said "Attempting forced restart" every
        90 s but nothing actually happened, leaving the office offline
        until an operator manually intervened (user-reported on
        cbcl-stg 2026-05-28).

        Why ``container.start()`` over ``restart_office()``: the
        existing container object retains Docker's full launch config
        (image, mounts, env, network, restart policy). Starting it in
        place avoids needing the office_slug + workspace_path that
        ``restart_office`` requires — which the health-check loop
        doesn't have. If ``start()`` fails (image gone, port conflict,
        OOM) the error surfaces so the operator sees the real cause
        instead of a silent infinite retry.
        """
        container = self._containers.get(office_id)
        if container is None:
            logger.warning(
                "force_restart_office: no tracked container for %s — "
                "skipping (was the office removed mid-loop?)",
                office_id,
            )
            return
        try:
            await asyncio.to_thread(container.reload)
            if container.status == "running":
                # Container is already up — nothing to do. Health
                # loop should have reset the counter via the "healthy"
                # branch; this is a defensive no-op for races.
                logger.info(
                    "force_restart_office: container for %s is already "
                    "running — no restart needed", office_id,
                )
                return
            logger.info(
                "force_restart_office: starting container for %s "
                "(current status=%s)", office_id, container.status,
            )
            await asyncio.to_thread(container.start)
            await asyncio.to_thread(container.reload)
            logger.info(
                "force_restart_office: container for %s now %s",
                office_id, container.status,
            )
        except Exception as exc:
            logger.exception(
                "force_restart_office: FAILED to restart container for "
                "office %s: %s. Office is offline until an operator "
                "intervenes (cbcl restart, docker logs <container>).",
                office_id, exc,
            )

    async def health_check_all(
        self,
        on_crash: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        """Background loop: check all containers every 30 seconds."""
        from src.docker.container_health import health_check_all
        # Wire on_restart so the escalation path actually restarts
        # the container instead of just logging. Previously
        # ``on_restart`` was None and the "ESCALATION: Attempting
        # forced restart" log message was a lie — the loop kept
        # cycling 1/3 → 2/3 → 3/3 → reset forever while the office
        # stayed offline.
        await health_check_all(
            self._containers,
            on_crash=on_crash,
            on_restart=self.force_restart_office,
        )

    # -- Container name -----------------------------------------------------

    def get_container_name(self, office_id: str) -> str | None:
        """Return the Docker container name for an office, or None if not running."""
        container = self._containers.get(office_id)
        if container:
            return container.name
        return None
