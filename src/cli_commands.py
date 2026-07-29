"""CLI command implementations for the Cubicle Communicator.

Auth-flow helpers (OAuth URL capture, browser interception, container-local
callback forwarding) live in ``src.cli_auth`` — imported below.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

import click

from src.cli_auth import (
    _authenticate_office_container,
    _find_offices_for_auth,
)
from src.config import (
    Config,
    config_exists,
    ensure_config_dir,
    ensure_credentials_file,
    fetch_offices_sync,
    load_config,
    save_config,
)
from src.daemon import (
    _format_uptime,
    _is_process_running,
    _read_pid,
    _setup_logging_foreground,
    _start_daemon,
    _start_foreground,
    find_running_daemon_pid,
)
from src.docker.container_manager import ContainerManager
from src.main import cli
from src.paths import CUBICLE_HOME, get_logs_path, get_pid_path, slugify
from src.utils import get_daemon_version

logger = logging.getLogger(__name__)

# Short pause between office auth attempts to let ports leave TIME_WAIT.
_INTER_OFFICE_DELAY = 3



# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--office", "-o", default=None,
    help="Authenticate a specific office by name (default: all offices).",
)
@click.option(
    "--force", "-f", is_flag=True,
    help="Force re-authentication even if already authenticated.",
)
def auth(office: str | None, force: bool) -> None:
    """Authenticate Claude CLI in office containers.

    Runs the OAuth login flow for each office container.  By default,
    offices that are already authenticated are skipped (use --force to
    re-authenticate, e.g. to switch to a different Claude account).

    Examples::

        cbcl auth                        # auth all unauthenticated offices
        cbcl auth --office Recruitment   # auth a specific office
        cbcl auth --force                # force re-auth for all offices
    """
    if not config_exists():
        click.echo("Not configured. Run 'cbcl setup' first.")
        sys.exit(1)

    config = load_config()

    click.echo("")
    try:
        offices = _find_offices_for_auth(config.platform_url, office, config.security_token)
    except Exception as exc:
        click.echo(f"  Cannot reach platform: {exc}")
        sys.exit(1)

    if not offices:
        sys.exit(1)

    cm = ContainerManager(use_docker=True)

    # Ensure the Docker image exists (setup builds it, but auth may run standalone)
    click.echo("  Ensuring agent Docker image...")
    try:
        asyncio.run(cm.ensure_image())
    except Exception as exc:
        click.echo(f"  ERROR: Could not build agent image: {exc}")
        click.echo("  Ensure Docker is running. You can also run 'cbcl build'.")
        sys.exit(1)

    results: dict[str, bool] = {}
    for i, ofc in enumerate(offices):
        office_slug = slugify(ofc.name)
        container_name = f"cbcl-office-{office_slug}"

        click.echo(f"\n{'─' * 60}")
        click.echo(f"  Office: {ofc.name}  ({i + 1}/{len(offices)})")
        click.echo(f"  Container: {container_name}")
        click.echo(f"{'─' * 60}")

        # Ensure container is running
        try:
            asyncio.run(
                cm.start_office(office_slug, ofc.id, ofc.workspace_path),
            )
        except Exception as exc:
            click.echo(f"  ERROR: Could not start container: {exc}")
            results[ofc.name] = False
            continue

        success = _authenticate_office_container(container_name, force=force)
        results[ofc.name] = success

        # Small delay between offices to let ports leave TIME_WAIT
        if i < len(offices) - 1:
            time.sleep(_INTER_OFFICE_DELAY)

    # --- Summary ---
    _print_auth_summary(results)


@cli.command()
@click.option(
    "--office", "-o", default=None,
    help="Logout a specific office by name (default: all offices).",
)
def logout(office: str | None) -> None:
    """Remove Claude authentication from office containers.

    Deletes the stored credentials so the office can be re-authenticated
    with a different account.

    Examples::

        cbcl logout                        # logout all offices
        cbcl logout --office Recruitment   # logout a specific office
    """
    if not config_exists():
        click.echo("Not configured. Run 'cbcl setup' first.")
        sys.exit(1)

    config = load_config()

    try:
        offices = _find_offices_for_auth(config.platform_url, office, config.security_token)
    except Exception as exc:
        click.echo(f"  Cannot reach platform: {exc}")
        sys.exit(1)

    if not offices:
        sys.exit(1)

    for ofc in offices:
        office_slug = slugify(ofc.name)
        container_name = f"cbcl-office-{office_slug}"
        click.echo(f"\n  Office: {ofc.name}")

        # Check if container is running
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0 or "true" not in result.stdout.lower():
                click.echo("    Container not running. Skipping.")
                continue
        except Exception:
            click.echo("    Container not found. Skipping.")
            continue

        # Run claude auth logout
        try:
            result = subprocess.run(
                ["docker", "exec", container_name, "claude", "auth", "logout"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                click.echo("    Logged out successfully.")
            else:
                # Fallback: delete credentials file directly
                subprocess.run(
                    ["docker", "exec", container_name, "rm", "-f",
                     "/home/agent/.claude/.credentials.json",
                     "/home/agent/.claude.json"],
                    capture_output=True, timeout=10,
                )
                click.echo("    Credentials removed.")
        except Exception as exc:
            click.echo(f"    Error: {exc}")

    click.echo("\nDone. Run 'cbcl auth' to re-authenticate.")


@cli.command()
@click.option(
    "--company-token",
    envvar="CBCL_COMPANY_TOKEN",
    default=None,
    help="Company Token from the platform UI (cbcl_co_...). Defaults to "
         "the existing config value or ``CBCL_COMPANY_TOKEN`` env var.",
)
@click.option(
    "--anthropic-api-key",
    envvar="CBCL_ANTHROPIC_API_KEY",
    default=None,
    help="Optional API key fallback. Subscription auth via 'cbcl auth' "
         "is the recommended path; this slot exists for CI / batch use.",
)
@click.option(
    "--non-interactive", "--yes", "-y",
    is_flag=True,
    envvar="CBCL_NON_INTERACTIVE",
    help="Refuse to prompt for any missing values. Fail with a clear "
         "error if a required value is absent. Use this in CI, cloud-init, "
         "ansible, or any 'no TTY' install flow.",
)
def setup(
    company_token: str | None,
    anthropic_api_key: str | None,
    non_interactive: bool,
) -> None:
    """Configure the Communicator: security token, containers, auth.

    The platform URL defaults to ``https://app.cbcl.ai`` (the public
    Cubicle platform). Developers running a local backend override
    with the ``CBCL_PLATFORM_URL`` env var.

    Interactive when run on a TTY; headless when given flags / env vars.
    Examples:

        # Interactive (laptop, fresh install)
        cbcl setup

        # Fully headless (cloud-init, ansible, CI):
        cbcl setup \\
            --company-token cbcl_co_xxx \\
            --non-interactive

        # Same effect via env vars:
        CBCL_COMPANY_TOKEN=cbcl_co_xxx \\
        CBCL_NON_INTERACTIVE=1 \\
            cbcl setup

        # Dev: point at a local backend
        CBCL_PLATFORM_URL=http://localhost:8000 cbcl setup
    """
    ensure_config_dir()
    ensure_credentials_file()

    if config_exists():
        config = load_config()
        click.echo("Existing config found. Updating...\n")
    else:
        config = Config()
        click.echo("Setting up Cubicle Communicator...\n")

    # Helper: resolve value with flag > env > existing config > prompt
    # ordering; in non-interactive mode, fall through directly to the
    # existing-config fallback and refuse to prompt.
    def _resolve(
        flag_value: str | None,
        existing: str,
        prompt_text: str,
        *,
        prompt_kwargs: dict | None = None,
        required: bool = True,
        field_label: str | None = None,
    ) -> str:
        # Flag wins.
        if flag_value is not None:
            value = flag_value.strip() if isinstance(flag_value, str) else flag_value
            if value or not required:
                return value
            # Empty string for a required field → error.
        if non_interactive:
            if existing:
                return existing
            if not required:
                return ""
            raise click.UsageError(
                f"--non-interactive set but {field_label or prompt_text!r} "
                f"is missing. Pass --{(field_label or '').lower().replace(' ', '-') or 'value'} "
                f"on the command line or set the matching CBCL_* env var."
            )
        return click.prompt(prompt_text, **(prompt_kwargs or {}))

    # Platform URL defaults to https://app.cbcl.ai. The env var
    # ``CBCL_PLATFORM_URL`` lets developers swap it for a local
    # backend; ``Config()`` resolves it on construction. Surface
    # whichever URL we resolved so the user can spot a stale env
    # var or a stored override in ``~/.cubicle/config.yaml``.
    click.echo(f"  Platform: {config.platform_url}")

    # --- Step 1: Company Token ---  (numbered comments mirror the
    # user-visible sequence; renumber if you reorder steps.)
    if not non_interactive:
        click.echo("")
        click.echo("  A Company Token authenticates this daemon with the platform.")
        click.echo("  Generate one in the Platform UI: Company Settings > Tokens.")
        click.echo("  One token = one daemon machine; assign offices to this")
        click.echo("  token in each office's Settings > Connection tab.")
        click.echo("")
    token_input = _resolve(
        company_token,
        config.security_token,
        "Company Token (cbcl_co_...)",
        prompt_kwargs={
            "default": config.security_token or "",
            "show_default": bool(config.security_token),
        },
        field_label="company-token",
    )
    if token_input:
        token_input = token_input.strip()
        # Soft warning only — accept the input either way so a custom
        # token format used by future versions doesn't lock users out.
        # A wrong prefix surfaces as a 401 on the next discovery call
        # which we handle with a friendly message below.
        if not token_input.startswith("cbcl_co_"):
            click.echo(
                "  Warning: token does not start with 'cbcl_co_' — "
                "this is the current Company Token format. If the "
                "platform rejects it, mint a fresh token in Company "
                "Settings > Tokens and re-run 'cbcl setup'."
            )
        config.security_token = token_input

    # --- Step 2: Optional API key (CI / batch use) ---
    if anthropic_api_key:
        config.anthropic_api_key = anthropic_api_key.strip()

    # --- Step 3: Discover offices ---
    click.echo("")
    try:
        offices = fetch_offices_sync(config.platform_url, config.security_token)
        click.echo("  Connected to platform")
        if offices:
            names = ", ".join(f'"{o.name}"' for o in offices)
            click.echo(f"  Found {len(offices)} office(s): {names}")
        else:
            # Two reasons this can happen: (a) no offices exist in
            # the Company yet, or (b) offices exist but none are
            # bound to THIS token (token-affinity filter). Both end
            # up here with an empty list and the same fix from the
            # user's POV — assign offices to this token in Office
            # Settings → Connection, or create the first office.
            click.echo("  No offices visible to this token.")
            click.echo("  Either create an office in the UI, or open")
            click.echo("  Office Settings > Connection and bind an")
            click.echo("  existing office to this token. Re-run 'cbcl")
            click.echo("  setup' after.")
            save_config(config)
            click.echo(f"\n  Config saved to {CUBICLE_HOME / 'config.yaml'}")
            return
    except Exception as exc:
        # 401 = revoked / wrong-Company token; 403 = role gate (won't
        # happen on this endpoint today but defensive). Detect via
        # the httpx.HTTPStatusError message shape — fetch_offices_sync
        # wraps the raw error string. Everything else falls through
        # to the generic "fix the URL" path.
        msg = str(exc)
        if "401" in msg or "Unauthorized" in msg:
            click.echo(
                "  Token rejected by platform (401). The token may "
                "be revoked or belong to a different Company."
            )
            click.echo(
                "  Mint a fresh Company Token in Company Settings > "
                "Tokens and re-run 'cbcl setup'."
            )
        else:
            click.echo(f"  Cannot reach platform: {exc}")
        save_config(config)
        click.echo(f"\n  Config saved to {CUBICLE_HOME / 'config.yaml'}")
        click.echo("  Fix the issue above and re-run 'cbcl setup'.")
        return

    save_config(config)

    # --- Step 4: Build image and start containers ---
    click.echo("\n" + "=" * 60)
    click.echo("  Setting up office containers")
    click.echo("=" * 60)

    cm = ContainerManager(use_docker=True)

    # Redis: cbcl runs Redis IN-PROCESS using fakeredis. NO external
    # container or system service — the daemon host stays untouched
    # outside the office containers. See ``src/local_redis.py`` for
    # the rationale.
    click.echo("\n  Building agent Docker image...")
    try:
        asyncio.run(cm.ensure_image())
        click.echo("  Image ready.")
    except Exception as exc:
        click.echo(f"  ERROR: Could not build agent image: {exc}")
        click.echo("  Ensure Docker is running and re-run 'cbcl setup'.")
        return

    # --- Step 5: For each office — start container + authenticate ---
    results: dict[str, bool] = {}

    for i, ofc in enumerate(offices):
        office_slug = slugify(ofc.name)
        container_name = f"cbcl-office-{office_slug}"
        click.echo(f"\n{'─' * 60}")
        click.echo(f"  Office: {ofc.name}  ({i + 1}/{len(offices)})")
        click.echo(f"  Container: {container_name}")
        click.echo(f"{'─' * 60}")

        # Start container
        try:
            asyncio.run(
                cm.start_office(office_slug, ofc.id, ofc.workspace_path),
            )
            click.echo("  Container started.")
        except Exception as exc:
            click.echo(f"  ERROR: Could not start container: {exc}")
            results[ofc.name] = False
            continue

        # Authenticate Claude in this container
        success = _authenticate_office_container(container_name)
        results[ofc.name] = success

        # Small delay between offices to let ports leave TIME_WAIT
        if i < len(offices) - 1:
            time.sleep(_INTER_OFFICE_DELAY)

    # --- Summary ---
    _print_auth_summary(results)

    if all(results.values()):
        click.echo("\nAll offices ready. Run 'cbcl start' to begin.")
    else:
        click.echo("\nSome offices need authentication.")
        click.echo("Run 'cbcl auth --force' to retry, or for a specific office:")
        for ofc in offices:
            if not results.get(ofc.name, False):
                click.echo(f"  cbcl auth --office \"{ofc.name}\" --force")


def _print_auth_summary(results: dict[str, bool]) -> None:
    """Print a summary table of auth results for all offices."""
    click.echo(f"\n{'=' * 60}")
    click.echo("  Authentication Summary")
    click.echo(f"{'=' * 60}")
    for office_name, success in results.items():
        icon = "+" if success else "x"
        status = "Authenticated" if success else "NOT AUTHENTICATED"
        click.echo(f"  [{icon}] {office_name}: {status}")


@cli.command()
@click.option("--daemon", "-d", is_flag=True, help="Run in background")
def start(daemon: bool) -> None:
    """Start the Communicator and connect to all offices."""
    try:
        config = load_config()
    except FileNotFoundError as exc:
        click.echo(str(exc), err=True)
        click.echo("Run 'cbcl setup' first.", err=True)
        sys.exit(1)

    # Verify we can reach the platform and there are offices
    try:
        offices = fetch_offices_sync(config.platform_url, config.security_token)
    except Exception as exc:
        click.echo(f"Cannot reach platform at {config.platform_url}: {exc}", err=True)
        click.echo("Check the URL and run 'cbcl setup' if needed.", err=True)
        sys.exit(1)

    if not offices:
        click.echo("No offices found on the platform. Create one in the UI first.", err=True)
        sys.exit(1)

    # Linux + UFW preflight. The tool_proxy binds 0.0.0.0 so docker
    # containers can reach it via host.docker.internal:host-gateway,
    # but a default-DROP UFW INPUT chain silently blocks the docker
    # bridge. Containers hit ConnectionTimeoutError on every
    # ``execute_script`` and the failure mode is invisible to the
    # operator (the daemon logs say nothing, /health responds fine
    # from localhost). Detect + warn so the user fixes it once
    # instead of debugging ghost outages.
    _ufw_preflight()

    if daemon:
        _start_daemon(config)
    else:
        _start_foreground(config)


def _ufw_preflight() -> None:
    """Warn if UFW is active and the docker bridge isn't allowed in.

    No-op on macOS / Windows / no-ufw Linux. Pure diagnostic: never
    fails the startup, never modifies firewall state (operator
    decides what to do).
    """
    if platform.system() != "Linux":
        return
    try:
        result = subprocess.run(
            ["ufw", "status"],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        # No UFW installed → no preflight. Log at debug so operators
        # on slow hosts hitting the 3s timeout can correlate the
        # silent skip with their environment.
        logger.debug("UFW preflight skipped: %s", exc)
        return
    if result.returncode != 0:
        return
    status = result.stdout
    if "Status: active" not in status:
        return  # UFW present but disabled → ok
    # Match "docker0" as a whole interface token, not as a substring,
    # so "docker0-backup" or similar doesn't false-positive.
    if any(
        tok.lower() == "docker0" for tok in status.replace(",", " ").split()
    ):
        return  # Already allowed → ok
    click.echo(
        "\n⚠  UFW is active and the docker bridge isn't allowed in.\n"
        "   In-container script execution will fail with "
        "ConnectionTimeoutError\n"
        "   when the agent tries to reach the host-side script "
        "runner. Fix:\n\n"
        "       sudo ufw allow in on docker0\n"
        "       sudo ufw reload\n\n"
        "   This opens the docker bridge interface (containers "
        "→ host) only,\n"
        "   not the public network. See docs/06-operations/"
        "deployment.md.\n",
        err=True,
    )


@cli.command()
def stop() -> None:
    """Stop the Communicator AND tear down all office containers.

    Signals the daemon to shut down gracefully, then sweeps every office
    container (by ``cbcl.managed`` label / ``cbcl-office-`` name) and
    stop+removes it. The sweep runs UNCONDITIONALLY — even if the daemon
    was already gone or had to be force-killed — so no office container
    is ever left running after ``cbcl stop`` (the previously-reported
    bug: a SIGKILLed daemon never reached its own container teardown, and
    ``unless-stopped`` kept the containers alive).
    """
    from src.docker.container_manager import stop_and_remove_managed_containers

    pid_path = get_pid_path()
    pid: int | None = None

    if pid_path.exists():
        pid = _read_pid(pid_path)
        if pid is None:
            click.echo("Stale PID file (unreadable) — falling back to /proc scan")
            pid_path.unlink(missing_ok=True)
        elif not _is_process_running(pid):
            click.echo("Stale PID file (process gone) — falling back to /proc scan")
            pid_path.unlink(missing_ok=True)
            pid = None

    # Fallback: PID file is missing or stale, but a daemon started
    # by an older cbcl (pre-0.2.8, foreground path didn't write a
    # PID file) may still be running. Scan /proc to find it.
    if pid is None:
        pid = find_running_daemon_pid()
        if pid is not None:
            click.echo(
                f"Discovered cbcl daemon at PID {pid} via /proc scan "
                f"(no PID file). Stopping it."
            )

    if pid is None:
        click.echo("Communicator daemon is not running.")
    else:
        try:
            os.kill(pid, signal.SIGTERM)
            click.echo(f"Stopping Communicator (PID {pid})...")
            # Give the daemon a graceful window to flush state + stop its
            # own containers. Generous enough to exceed the daemon's
            # per-office agent-shutdown budget; the sweep below is the
            # backstop regardless.
            stopped = False
            for _ in range(25):
                time.sleep(1)
                if not _is_process_running(pid):
                    stopped = True
                    break
            if stopped:
                click.echo("Communicator stopped.")
            else:
                os.kill(pid, signal.SIGKILL)
                click.echo("Communicator force-stopped (timed out).")
        except OSError:
            click.echo("Communicator process already gone.")
        finally:
            pid_path.unlink(missing_ok=True)

    # ALWAYS tear down office containers — this is what guarantees
    # `cbcl stop` leaves nothing running, independent of how (or whether)
    # the daemon exited.
    click.echo("Tearing down office containers...")
    removed = stop_and_remove_managed_containers()
    if removed:
        click.echo(f"Removed {removed} office container(s).")
    else:
        click.echo("No office containers to remove.")


@cli.command()
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
@click.option("--lines", "-n", default=50, help="Number of lines to show")
def logs(follow: bool, lines: int) -> None:
    """View the Communicator log file."""
    log_path = get_logs_path() / "communicator.log"
    if not log_path.exists():
        click.echo("No log file found. Start the Communicator with --daemon first.")
        sys.exit(1)

    if follow:
        subprocess.run(["tail", "-f", "-n", str(lines), str(log_path)])
    else:
        with open(log_path) as f:
            all_lines = f.readlines()
            for line in all_lines[-lines:]:
                click.echo(line, nl=False)


@cli.command()
def status() -> None:
    """Show current Communicator status."""
    if not config_exists():
        click.echo("Not configured. Run 'cbcl setup' first.")
        return

    config = load_config()
    pid_path = get_pid_path()

    click.echo("")
    click.echo("Cubicle Communicator")
    click.echo(f"  Version:  {get_daemon_version()}")

    pid = _read_pid(pid_path) if pid_path.exists() else None
    if pid and _is_process_running(pid):
        uptime_str = _format_uptime(pid_path)
        click.echo(f"  Status:   Running (PID {pid})")
        click.echo(f"  Uptime:   {uptime_str}")
    else:
        # Fallback: a daemon started by pre-0.2.8 cbcl (foreground
        # path didn't write a PID file) is still findable via
        # /proc. Surface it so operators don't think the daemon is
        # dead when it's actively serving traffic.
        proc_pid = find_running_daemon_pid()
        if proc_pid is not None:
            click.echo(f"  Status:   Running (PID {proc_pid}, discovered via /proc)")
            click.echo(
                "  Hint:     Started by older cbcl without PID file — "
                "next 'cbcl start' will write one"
            )
        else:
            click.echo("  Status:   Not running")
        if pid:
            pid_path.unlink(missing_ok=True)

    click.echo(f"  Platform: {config.platform_url}")

    if config.security_token:
        click.echo(f"  Token:    {config.security_token[:10]}...{config.security_token[-4:]}")
        # The daemon writes ~/.cubicle/.token_revoked when discovery
        # comes back 401. Surface that in `cbcl status` so the user
        # sees "your token was revoked" without grepping logs.
        try:
            from src.paths import CUBICLE_HOME

            revoked_marker = Path(CUBICLE_HOME) / ".token_revoked"
            if revoked_marker.exists():
                click.echo(
                    "            ⚠ Token rejected by platform (401). "
                    "Mint a fresh one in Company Settings > Tokens "
                    "and re-run 'cbcl setup'."
                )
        except Exception:
            # Marker is observability-only; never block status output.
            pass
    else:
        click.echo("  Token:    not set")

    key = config.anthropic_api_key
    if key:
        click.echo(f"  API key:  {key[:8]}...{key[-4:]}")
    else:
        click.echo("  API key:  not set (using subscription auth)")

    # Discover offices
    click.echo("")
    click.echo("Offices:")
    try:
        offices = fetch_offices_sync(config.platform_url, config.security_token)
        if not offices:
            click.echo("  (none found on platform)")
        else:
            # One ContainerManager (= one Docker SDK client = one
            # daemon socket dial) for the whole status read, then
            # gather the per-office lookups in a single event loop.
            # Pre-fix posture instantiated a fresh CM + ran
            # ``asyncio.run`` for every office, paying TLS/socket +
            # event-loop bootstrap costs ~N times on a multi-office
            # status call. ``cbcl status`` is a separate process
            # from the daemon so cache visibility isn't a concern
            # either way — we report the actual container state.
            cm = ContainerManager(use_docker=True)
            container_names = [
                f"cbcl-office-{slugify(o.name)}" for o in offices
            ]

            async def _gather_statuses() -> list[dict | Exception]:
                return await asyncio.gather(
                    *(cm.get_status_by_name(n) for n in container_names),
                    return_exceptions=True,
                )

            statuses = asyncio.run(_gather_statuses())
            for office, info in zip(offices, statuses):
                click.echo(f"  {office.name}")
                click.echo(f"    ID:        {office.id}")
                click.echo(f"    Workspace: {office.workspace_path}")
                if isinstance(info, Exception):
                    click.echo(f"    Container: error ({info})")
                else:
                    click.echo(
                        f"    Container: {info.get('status', 'unknown')}"
                    )
    except Exception as exc:
        click.echo(f"  (cannot reach platform: {exc})")

    click.echo("")
    click.echo(f"Log: {get_logs_path() / 'communicator.log'}")
    click.echo("")


@cli.command()
def build() -> None:
    """Build the agent Docker image."""
    _setup_logging_foreground()
    click.echo("Building agent Docker image...")
    try:
        cm = ContainerManager(use_docker=True)
        asyncio.run(cm.ensure_image())
        click.echo("Image built successfully.")
    except Exception as exc:
        click.echo(f"Build failed: {exc}", err=True)
        sys.exit(1)
