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

from src.config import OfficeConfig, resolve_office_resource_limits
from src.paths import CUBICLE_HOME, get_secrets_path

logger = logging.getLogger(__name__)

# Docker image used for office containers
IMAGE_TAG = "cbcl-agent:latest"

# CFS scheduler accounting period for the office container's CPU cap.
# The quota is derived per create: ``cpu_quota = int(cpus * period)``,
# where ``cpus`` comes from the user-configurable office resource
# limits (``office_cpus`` in ~/.cubicle/config.yaml, or the
# CBCL_OFFICE_CPUS env override — see
# ``src.config.get_office_resource_limits``).
CPU_PERIOD_US = 100_000

# Label stamped on every office container at creation, and the legacy
# name prefix used as a fallback for containers created before the label
# shipped. Both are used by the teardown sweep so `cbcl stop` reclaims
# EVERY office container — including orphans the in-memory dict lost.
MANAGED_LABEL = "cbcl.managed"
OFFICE_NAME_PREFIX = "cbcl-office-"


def stop_and_remove_managed_containers() -> int:
    """Stop + remove EVERY office container this install manages.

    Synchronous + self-contained (own docker client) so the ``cbcl stop``
    CLI — a SEPARATE process from the daemon, with no in-memory tracking —
    can guarantee teardown even if the daemon was SIGKILLed before its own
    cleanup ran, or crashed. Matches by the ``cbcl.managed`` label first,
    then falls back to the ``cbcl-office-`` name prefix for containers
    created before the label shipped. Returns the number removed.
    """
    try:
        import docker
    except Exception:
        return 0
    try:
        client = docker.from_env()
    except Exception as exc:
        logger.warning("stop_and_remove_managed_containers: no docker: %s", exc)
        return 0

    seen: dict[str, Any] = {}
    try:
        for c in client.containers.list(
            all=True, filters={"label": f"{MANAGED_LABEL}=true"}
        ):
            seen[c.id] = c
    except Exception as exc:
        logger.warning("label listing failed: %s", exc)
    try:
        for c in client.containers.list(all=True):
            if c.id not in seen and (c.name or "").startswith(OFFICE_NAME_PREFIX):
                seen[c.id] = c
    except Exception as exc:
        logger.warning("name listing failed: %s", exc)

    removed = 0
    for c in seen.values():
        try:
            c.remove(force=True)  # force = stop (SIGKILL after grace) + remove
            removed += 1
            logger.info("Removed office container %s", c.name)
        except Exception as exc:
            logger.warning("Failed to remove container %s: %s", c.name, exc)
    return removed

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


def claude_auth_dir(workspace_path) -> Path:
    """Host-side backing dir of the container's ``/home/agent/.claude``
    bind mount — where ``.credentials.json`` actually lives. Shared by
    the mount setup below and the auth keepalive's host-side reads
    (``src.auth_keepalive``) so the two paths can never drift."""
    return Path(workspace_path) / ".claude-auth"


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
        # 07/H-14: resolve symlinks — this is the check the BACKEND has
        # been deferring to.
        #
        # The backend's validator is lexical: it never touches this host's
        # filesystem, so it cannot see that /srv/data is a symlink into
        # ~/.cubicle. Its docstring said the daemon caught that "at mount
        # time"; no such check existed, so BOTH validators missed it and
        # the office-secrets tree could be mounted into an agent container.
        #
        # realpath() collapses symlinks, ".." and "." to the true location,
        # so this catches the whole family in one comparison.
        try:
            real_host = Path(host_path).resolve()
            cubicle_root = CUBICLE_HOME.resolve()
            if real_host == cubicle_root or cubicle_root in real_host.parents:
                logger.warning(
                    "Refusing extra_mount %r — it resolves to %s, inside "
                    "the Cubicle config/secrets tree (%s). Mounting it "
                    "would expose office secrets to every agent in "
                    "container=%s.",
                    host_path, real_host, cubicle_root, container_name,
                )
                continue
            # Also refuse a mount that CONTAINS the secrets tree.
            if real_host in cubicle_root.parents:
                logger.warning(
                    "Refusing extra_mount %r — it resolves to %s, which "
                    "contains the Cubicle config/secrets tree (%s). "
                    "(container=%s)",
                    host_path, real_host, cubicle_root, container_name,
                )
                continue
        except OSError:
            # A path we cannot resolve is one we cannot vouch for.
            logger.warning(
                "Skipping extra_mount %r — could not resolve it to a real "
                "path (container=%s)", host_path, container_name,
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


def _mcp_server_source_files() -> list[Path]:
    """The MCP-server source files COPYed into ``/opt/cubicle`` that the
    image-cache hash must cover, in hash order.

    SINGLE SOURCE OF TRUTH for both ``_compute_mcp_server_hash`` (below)
    and the COPY lines in ``_agent_image/Dockerfile.agent``. If the two
    drift, the image can silently ship stale MCP code (symptom:
    "cubicle-tools MCP server disconnected" in the container after an
    edit that didn't trigger a rebuild). ``tests/test_agent_image_copy_sync.py``
    asserts this list stays in lockstep with the Dockerfile.

    Excludes ``Dockerfile.agent`` itself — it's the build recipe, hashed
    separately, not a COPYed artifact.
    """
    files: list[Path] = [
        _DOCKER_DIR / "mcp_tool_server.py",
        _DOCKER_DIR / "_mcp_backend.py",
        _DOCKER_DIR / "_mcp_script_exec.py",
        _DOCKER_DIR / "bash_guard.py",
    ]
    mcp_pkg = _DOCKER_DIR / "_mcp"
    if mcp_pkg.is_dir():
        files.extend(sorted(mcp_pkg.glob("*.py")))
    return files


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
    # Then every COPYed MCP source file (entrypoint, the Wave 11 sibling
    # modules, and the _mcp package). The file list lives in
    # ``_mcp_server_source_files`` so it stays in lockstep with the
    # Dockerfile COPY set — order preserved here so the hash is stable.
    for path in _mcp_server_source_files():
        if path.exists():
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
            # MODULE RULE: every docker-py call is a synchronous dockerd API
            # request — wrap in to_thread so it never blocks the event loop
            # (and the per-office connect path that awaits ensure_image).
            image = await asyncio.to_thread(client.images.get, IMAGE_TAG)
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
            office_slug=office.slug,
            office_id=office.id,
            workspace_path=office.workspace_path,
            extra_mounts=office.extra_mounts,
            container_cpus=office.container_cpus,
            container_memory=office.container_memory,
        )

    async def start_office(
        self, office_slug: str, office_id: str, workspace_path: str,
        extra_mounts: list[dict] | None = None,
        container_cpus: float | None = None,
        container_memory: str | None = None,
    ) -> str:
        """Start a Docker container for an office. Returns container ID.

        ``extra_mounts`` is the per-office "Mounts" tab list
        (host→container bind mounts). Applied to the volumes dict
        on first create; ignored when the container is already running
        (Docker doesn't allow adding mounts to a running container —
        the user must restart the office to apply new mounts).

        **Resource limits** (CPU + memory) are resolved per create via
        :func:`src.config.resolve_office_resource_limits` — the
        PER-OFFICE overrides (``container_cpus`` / ``container_memory``
        from Office Settings → Resources, passed in here) beat the
        host-global chain: ``office_cpus`` / ``office_memory`` in
        ``~/.cubicle/config.yaml`` or the ``CBCL_OFFICE_CPUS`` /
        ``CBCL_OFFICE_MEMORY`` env overrides (defaults: 4 CPUs / 8g).
        Why they matter: Claude Code dynamic workflows (agents with
        ``effort == "ultracode"``, e.g. the Planner) size their
        parallel-subagent pool from the cores visible inside the
        container — roughly **cores − 2** — so the historical 4-CPU
        hardcode capped workflow concurrency at ~2 subagents and
        starved tool execution on busy offices. Raise ``office_cpus``
        to raise the cap. macOS caveat: on Docker Desktop every
        container runs inside the Docker Desktop Linux VM, so the VM's
        own allocation (Docker Desktop → Settings → Resources) is a
        hard ceiling — an ``office_cpus`` above it buys nothing until
        the VM is resized.

        Like ``extra_mounts``, the limits apply at container CREATE
        time only: Docker can't retrofit them onto a running container,
        and this method reuses a running container (keeping its OLD
        limits) whenever the image is unchanged. Changed PER-OFFICE
        limits are applied by the sync-driven reconciler
        (``src.docker.limits_reconciler`` — recreate when idle, defer
        while busy). Changed HOST-GLOBAL limits still need
        ``cbcl stop && cbcl start`` (there is no ``cbcl restart``;
        ``cbcl stop`` removes every office container unconditionally,
        so the next start recreates them with the new values).
        """
        from src.config import get_api_key

        client = self._get_client()
        container_name = f"cbcl-office-{office_slug}"

        # Check if already running. T8.2.3 (03/#12): every docker-py call here
        # issues a synchronous dockerd API request — wrap in to_thread so a
        # wedged/slow daemon can't block the event loop (every office's WS,
        # heartbeats, dispatchers). MODULE RULE: all docker-py calls go through
        # asyncio.to_thread.
        try:
            existing = await asyncio.to_thread(
                client.containers.get, container_name,
            )
            if existing.status == "running":
                # Reuse ONLY if the running container is on the CURRENT
                # image. A container left running from a previous cbcl
                # version runs a STALE baked image (old MCP tool server,
                # missing tools like consult_planner). Reusing it silently
                # ships old in-container code. Compare image ids and
                # recreate on mismatch so `cbcl start` always lands the
                # latest agent image.
                try:
                    current_image = await asyncio.to_thread(
                        client.images.get, IMAGE_TAG,
                    )
                    current_image_id = current_image.id
                    # ``.image`` is a lazy docker-py attribute that issues an
                    # API call — wrap it too.
                    running_image_id = (
                        await asyncio.to_thread(lambda: existing.image.id)
                    )
                except Exception:
                    current_image_id = running_image_id = None
                if current_image_id and running_image_id != current_image_id:
                    logger.info(
                        "Container %s runs a stale image (%s != %s) — "
                        "recreating from %s",
                        container_name,
                        (running_image_id or "?")[:19],
                        current_image_id[:19],
                        IMAGE_TAG,
                    )
                    await asyncio.to_thread(existing.remove, force=True)
                    # fall through to (re)create below
                else:
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
            else:
                await asyncio.to_thread(existing.remove, force=True)
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
        auth_dir = claude_auth_dir(workspace_path)
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

        # Resolve the user-configurable resource limits (see the
        # method docblock). Resolved fresh per create so a config
        # edit takes effect on the next (re)create without a daemon
        # code change. Per-office overrides (when set) beat the
        # host-global env/config.yaml chain.
        limits = resolve_office_resource_limits(
            container_cpus, container_memory,
        )
        cpu_quota = int(limits.cpus * CPU_PERIOD_US)
        limits_source = (
            "per-office override"
            if container_cpus is not None or container_memory is not None
            else "host config (env/config.yaml/defaults)"
        )
        logger.info(
            "Container %s resource limits: cpus=%s (cpu_period=%d, "
            "cpu_quota=%d), memory=%s [source: %s]. Limits apply at "
            "container CREATE only — per-office changes are applied "
            "by the sync-driven reconciler (recreate when idle); "
            "after changing office_cpus/office_memory "
            "(~/.cubicle/config.yaml) or CBCL_OFFICE_CPUS/"
            "CBCL_OFFICE_MEMORY, recreate with `cbcl stop && cbcl "
            "start` (there is no `cbcl restart`; a reused running "
            "container keeps its old limits).",
            container_name, limits.cpus, CPU_PERIOD_US, cpu_quota,
            limits.memory, limits_source,
        )

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
            # Labels let `cbcl stop` + startup recovery find and tear
            # down EVERY office container by label, without depending on
            # the daemon's in-memory tracking dict (which is empty in a
            # separate CLI process and lost after a crash/SIGKILL).
            labels={
                "cbcl.managed": "true",
                "cbcl.office_id": office_id,
            },
            # No port mapping — no HTTP server inside the container.
            # Communication is via docker exec (subprocess streaming).
            restart_policy={"Name": "unless-stopped"},
            mem_limit=limits.memory,
            cpu_period=CPU_PERIOD_US,
            cpu_quota=cpu_quota,
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

    # T8.2.4 (03/#18): ``restart_office`` was DELETED — it called start_office
    # without ``extra_mounts``, silently dropping the user's Mounts config on
    # restart. It had zero live callers (only ``force_restart_office`` exists,
    # which preserves mounts via ``container.start()``). Removed as dead code
    # rather than threading mounts through an unused path.
    # ``recreate_office`` below is the safe successor: it takes the FULL
    # ``OfficeConfig`` so extra_mounts AND the per-office resource limits
    # are always threaded through the recreate.

    async def recreate_office(self, office: OfficeConfig) -> str | None:
        """Force-remove the office container and start a fresh one.

        Used by the resource-limit reconciler
        (``src.docker.limits_reconciler``) when the per-office
        ``container_cpus`` / ``container_memory`` values changed —
        Docker can only apply limits at CREATE time, so the container
        must be recreated. Unlike the deleted ``restart_office``
        (T8.2.4 note above), this path goes through ``start_office``
        with the office's FULL config — ``extra_mounts`` and the
        per-office resource limits included — so a recreate can never
        silently drop the user's Mounts or Resources config.

        Removes the container by NAME with ``force=True`` (not via
        ``stop_office``, whose in-memory-dict dependency would leave
        an untracked-but-running container in place, which
        ``start_office`` would then REUSE with its old limits).
        """
        if not self.use_docker:
            return None
        client = self._get_client()
        container_name = f"cbcl-office-{office.slug}"
        try:
            existing = await asyncio.to_thread(
                client.containers.get, container_name,
            )
            logger.info(
                "Removing container %s for recreate (office %s)",
                container_name, office.id,
            )
            await asyncio.to_thread(existing.remove, force=True)
        except Exception as exc:
            import docker.errors
            if not isinstance(exc, docker.errors.NotFound):
                raise
        self._containers.pop(office.id, None)
        return await self.start_office(
            office_slug=office.slug,
            office_id=office.id,
            workspace_path=office.workspace_path,
            extra_mounts=office.extra_mounts,
            container_cpus=office.container_cpus,
            container_memory=office.container_memory,
        )

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
        on_giveup: Callable[[str, str], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        """Background loop: check all containers every 30 seconds.

        ``on_giveup(office_id, message)`` is called once when the
        health loop gives up on an office (10 consecutive failed
        restart attempts). The daemon wires this to push the message
        as the office's sticky ``last_error`` so the UI shows
        actionable copy instead of falling back to a generic
        "disconnected" state.
        """
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
            on_giveup=on_giveup,
        )

    # -- Container name -----------------------------------------------------

    def get_container_name(self, office_id: str) -> str | None:
        """Return the Docker container name for an office, or None if not running."""
        container = self._containers.get(office_id)
        if container:
            return container.name
        return None
