"""Per-script pip dependency installer.

Mini-projects declare their Python dependencies in
``requirements.txt`` next to ``script.yaml``. The Runner materialises
those deps into a per-script ``.deps/`` cache INSIDE the office
Docker container — the cache is on the bind-mounted workspace so it
survives container restarts, but pip itself runs in the container's
controlled Python environment.

We cache aggressively:
  - If ``requirements.txt`` mtime ≤ ``.deps/.installed_at`` mtime,
    skip pip entirely (cache hit).
  - If ``requirements.txt`` is absent, skip entirely (empty deps).
  - A ``.deps/.installing.lock`` file prevents concurrent installs
    of the same script from racing (two cron fires at the same
    minute, or a manual Run click during a cron install).

Installation runs via ``docker exec`` into the office container so
it hits the agent image's Python 3.12, not the host's. On the host
fallback path (tests / rollback), we use plain ``python -m pip``
against the host interpreter.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Pip install timeout — generous for large trees over slow links.
_INSTALL_TIMEOUT_SECONDS = 10 * 60

# How long a stale lock is considered dead. Kept to ~2× install
# timeout so a pip that's running right at its own deadline can't
# have its lock broken by a concurrent runner — that would spawn a
# second pip writing into the same ``--target`` and corrupt the
# cache.
_LOCK_STALE_SECONDS = 2 * _INSTALL_TIMEOUT_SECONDS + 60

# How long to wait for a concurrent install to finish before we
# give up. MUST be larger than ``_LOCK_STALE_SECONDS`` +
# ``_INSTALL_TIMEOUT_SECONDS`` — otherwise a healthy pip can run
# slightly past the wait window and the second runner returns a
# "timed out waiting" error for no good reason.
_LOCK_POLL_INTERVAL = 2.0
_LOCK_WAIT_TIMEOUT = _LOCK_STALE_SECONDS + _INSTALL_TIMEOUT_SECONDS + 60

# Module-load invariant: a future edit that bumps
# ``_INSTALL_TIMEOUT_SECONDS`` without also raising
# ``_LOCK_STALE_SECONDS`` would let a running pip (still under its
# own deadline) look "stale" to a second runner, which would break
# the lock and spawn a concurrent writer to the same ``--target``.
# Fail at import rather than silently corrupting a cache.
#
# Plain ``if ... raise`` instead of ``assert`` so the check
# survives ``python -O`` (which strips asserts). Factor of 1.5
# matches the intent of the "2× + 60s" formula above.
if _LOCK_STALE_SECONDS <= _INSTALL_TIMEOUT_SECONDS * 1.5:
    raise RuntimeError(
        "deps_installer: _LOCK_STALE_SECONDS must stay > 1.5 × "
        "_INSTALL_TIMEOUT_SECONDS or concurrent pip writers can race"
    )


class DepsInstallError(RuntimeError):
    """Raised when pip fails or the install times out. Callers should
    surface the ``stderr`` tail to the user so they can fix the
    underlying requirement (bad version spec, missing native dep,
    network issue)."""

    def __init__(self, message: str, stderr_tail: str = "") -> None:
        super().__init__(message)
        self.stderr_tail = stderr_tail


@dataclass(frozen=True)
class DepsInstallPlan:
    """The result of :func:`plan_install` — enough info for the caller
    to decide whether to await pip, and to know where the cache will
    be on disk so it can extend ``PYTHONPATH``."""

    needed: bool              # False = cache hit, skip pip
    deps_dir: Path            # {script_dir}/.deps/, absolute host path
    requirements_file: Path   # {script_dir}/requirements.txt, may or may not exist


def plan_install(script_dir: Path) -> DepsInstallPlan:
    """Decide whether an install is needed for this script.

    - No ``requirements.txt``                              → ``needed=False``.
    - No ``.deps/.installed_at`` stamp                     → ``needed=True``.
    - ``requirements.txt`` mtime ≤ stamp mtime             → ``needed=False``.
    - Stamp is newer than or equal to requirements.txt     → ``needed=False``.

    Deliberately does NOT inspect ``.deps/`` contents — a partial
    previous install leaves the stamp absent, which naturally falls
    into the "needed" branch.
    """
    reqs = script_dir / "requirements.txt"
    deps_dir = script_dir / ".deps"
    stamp = deps_dir / ".installed_at"

    if not reqs.is_file():
        return DepsInstallPlan(
            needed=False, deps_dir=deps_dir, requirements_file=reqs,
        )
    if not stamp.is_file():
        return DepsInstallPlan(
            needed=True, deps_dir=deps_dir, requirements_file=reqs,
        )
    # Cache hit iff the stamp is at least as new as requirements.txt.
    # Using >= (not >) handles the case where both files have the
    # same mtime (e.g. written in the same second on a low-res FS).
    if stamp.stat().st_mtime >= reqs.stat().st_mtime:
        return DepsInstallPlan(
            needed=False, deps_dir=deps_dir, requirements_file=reqs,
        )
    return DepsInstallPlan(
        needed=True, deps_dir=deps_dir, requirements_file=reqs,
    )


async def ensure_deps_installed(
    *,
    script_dir: Path,
    container_name: str | None,
    workspace_to_container: callable = lambda p: str(p),
) -> Path:
    """Ensure the script's ``.deps/`` cache is populated. Returns the
    absolute host path to the cache (caller uses it to extend the
    PYTHONPATH it passes to the script, translating to the
    container-side path via ``workspace_to_container`` when needed).

    Fast path is essentially free — one stat call on the stamp file
    decides whether pip runs at all.

    ``workspace_to_container`` translates host-side paths to their
    in-container equivalents. Defaults to identity (host-fallback
    path). The Runner injects the real translator so container
    installs reference ``/workspace/.scripts/{name}/.deps``.
    """
    plan = plan_install(script_dir)
    if not plan.needed:
        logger.debug(
            "Script deps cache hit for %s (requirements.txt unchanged)",
            script_dir.name,
        )
        return plan.deps_dir

    plan.deps_dir.mkdir(parents=True, exist_ok=True)
    lock = plan.deps_dir / ".installing.lock"

    # --- Acquire the install lock ---------------------------------
    # A concurrent install of the SAME script would be wasteful and
    # potentially corrupt the cache. We serialise with a single file
    # lock that carries a PID + mtime so stale locks (from a crashed
    # previous install) time out instead of wedging forever.
    acquired_at = await _acquire_install_lock(lock)
    try:
        # Re-plan AFTER the lock — the previous holder may have just
        # finished the install for us.
        plan = plan_install(script_dir)
        if not plan.needed:
            logger.info(
                "Script deps cache warmed by concurrent installer for %s",
                script_dir.name,
            )
            return plan.deps_dir

        # --- Run pip ----------------------------------------------
        await _run_pip_install(
            container_name=container_name,
            script_dir=script_dir,
            deps_dir=plan.deps_dir,
            requirements_file=plan.requirements_file,
            workspace_to_container=workspace_to_container,
        )

        # --- Write the stamp --------------------------------------
        # Re-touch AFTER pip succeeds so a failed install doesn't
        # look like a cache hit on the next run.
        stamp = plan.deps_dir / ".installed_at"
        stamp.write_text(f"ok {int(time.time())}\n")
        return plan.deps_dir
    finally:
        # Best-effort lock release. If we can't unlink it, a future
        # run will see a stale lock and time it out.
        try:
            if acquired_at is not None:
                lock.unlink(missing_ok=True)
        except OSError:
            logger.debug("Failed to remove install lock %s", lock)


async def _acquire_install_lock(lock: Path) -> float | None:
    """Block until we hold the install lock for this script.

    Returns the acquisition timestamp, or raises DepsInstallError if
    the wait times out. Detects stale locks (older than
    ``_LOCK_STALE_SECONDS``) and breaks them. Safe to call from
    multiple tasks — the OS ``O_EXCL`` open is the authoritative
    gate, the stale detection is a fallback for crashed holders.
    """
    deadline = time.monotonic() + _LOCK_WAIT_TIMEOUT
    while True:
        try:
            # O_EXCL | O_CREAT is the atomic "create if not exists"
            # primitive — no sleep-in-between-checks races.
            fd = os.open(
                str(lock),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
            with os.fdopen(fd, "w") as fh:
                fh.write(f"{os.getpid()} {int(time.time())}\n")
            return time.monotonic()
        except FileExistsError:
            pass

        # Lock exists — check if it's stale.
        try:
            age = time.time() - lock.stat().st_mtime
        except FileNotFoundError:
            # Raced with the holder releasing. Retry the acquire.
            continue
        if age > _LOCK_STALE_SECONDS:
            logger.warning(
                "Install lock %s is %.0fs old; breaking stale lock",
                lock, age,
            )
            try:
                lock.unlink()
            except FileNotFoundError:
                pass
            continue

        if time.monotonic() >= deadline:
            raise DepsInstallError(
                f"Timed out waiting for concurrent install of "
                f"{lock.parent.parent.name} to finish",
            )
        await asyncio.sleep(_LOCK_POLL_INTERVAL)


async def _run_pip_install(
    *,
    container_name: str | None,
    script_dir: Path,
    deps_dir: Path,
    requirements_file: Path,
    workspace_to_container,
) -> None:
    """Run pip install --target with --no-deps disabled (we want
    transitive deps)."""
    # `--upgrade` is load-bearing: without it, `pip install --target`
    # into a directory that already contains a previous version of a
    # package silently SKIPS the upgrade even though the new
    # requirements spec asks for it. Users edit requirements.txt,
    # the cache invalidates correctly, pip runs — and the bug is
    # that nothing actually changed on disk. --upgrade forces pip
    # to replace existing installs when specs move forward.
    _COMMON_FLAGS = [
        "--no-input",
        "--disable-pip-version-check",
        "--no-warn-script-location",
        "--upgrade",
    ]
    if container_name:
        # In-container: pip from the agent image's python3.12.
        # Translate all paths to their container-side form so pip
        # operates on the right files.
        container_deps = workspace_to_container(deps_dir)
        container_reqs = workspace_to_container(requirements_file)
        argv = [
            "docker", "exec",
            container_name,
            "python", "-m", "pip", "install",
            *_COMMON_FLAGS,
            "--target", container_deps,
            "-r", container_reqs,
        ]
        launch_mode = "docker"
    else:
        # Host fallback — rare, used only by unit tests (no
        # container_name). no rollback flag exists, so
        # production always takes the docker path above.
        argv = [
            "python", "-m", "pip", "install",
            *_COMMON_FLAGS,
            "--target", str(deps_dir),
            "-r", str(requirements_file),
        ]
        launch_mode = "host"

    logger.info(
        "Installing script deps (%s) for %s",
        launch_mode, script_dir.name,
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise DepsInstallError(
            f"pip launcher not available: {exc}",
        ) from exc

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_INSTALL_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        # Must await the kill + wait so the OS reaps the child and
        # asyncio doesn't warn about pending pipe readers. Without
        # this the zombie lingers until interpreter exit and leaks
        # FDs on long-running communicator sessions.
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            # Kernel hasn't reaped yet — let the OS handle it on
            # process exit. Nothing else we can do from here.
            pass
        raise DepsInstallError(
            f"pip install timed out after {_INSTALL_TIMEOUT_SECONDS}s "
            f"for {script_dir.name}",
        )

    if proc.returncode != 0:
        tail = (stderr.decode(errors="replace") or stdout.decode(errors="replace"))[-2000:]
        raise DepsInstallError(
            f"pip install failed (exit {proc.returncode}) for "
            f"{script_dir.name}",
            stderr_tail=tail,
        )
    logger.info(
        "Script deps installed for %s (%d bytes of output)",
        script_dir.name, len(stdout),
    )
