"""Maintain office OAuth credentials without conflating login and model quota.

Near saved expiry, a protected CLI diagnostic gives the CLI a chance to refresh.
If the model is capped, an independent profile check can still verify login.
A successful probe does not prove a refresh occurred; only an observed increase
in saved expiry is reported as such. A future timestamp alone cannot clear a
known authentication failure, because providers can revoke unexpired tokens.

The office lifecycle lock serializes this loop with login and migration, not
with ordinary worker CLI sessions. The CLI owns its refresh concurrency.
The corruption backup mirrors parse-valid credentials; it cannot recover a
revoked token. Provider rejection can still require a new sign-in.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from pathlib import Path
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

# Loop cadence (jittered ±20% so a multi-office daemon doesn't probe
# every office in the same instant).
KEEPALIVE_INTERVAL_SECONDS = 300.0
# Probe when the access token is within this lead of ``expiresAt``.
REFRESH_LEAD_SECONDS = 30 * 60.0
# Normal probe spacing; near expiry, retry sooner if it has not advanced.
MIN_PROBE_GAP_SECONDS = 10 * 60.0
EXPIRY_PROBE_GAP_SECONDS = 60.0
EXPIRY_PROBE_WINDOW_SECONDS = 5 * 60.0
# After a failed probe, hold off this long before probing again — a
# dead refresh token doesn't heal on its own, and each probe costs an
# API round-trip that will just fail again.
FAILED_PROBE_BACKOFF_SECONDS = 30 * 60.0
# Consecutive probe failures before auth is declared DOWN (the latch
# then fronts even unclassifiable Manager-turn errors with auth copy).
AUTH_DOWN_AFTER_FAILURES = 2


def _credential_expiry(credentials: dict | str) -> float | None:
    """Read finite millisecond expiry without trusting arbitrary JSON shapes."""
    oauth = credentials.get("claudeAiOauth") if isinstance(credentials, dict) else None
    value = oauth.get("expiresAt") if isinstance(oauth, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        expiry = float(value)
    except OverflowError:
        return None
    return expiry if math.isfinite(expiry) else None


def _default_probe(container_name: str, office_id: str) -> "Coroutine[Any, Any, bool]":
    """Run the proven warm probe off-loop (it is blocking subprocess IO)."""
    from src.auth_helpers import warm_claude_in_container
    from src.office_runtime import validated_container_id

    def probe() -> bool:
        return warm_claude_in_container(validated_container_id(office_id, container_name))

    return asyncio.to_thread(probe)


class AuthKeepalive:
    """Per-office OAuth keepalive loop. Create once per connected office
    and run :meth:`run` under the daemon's task supervisor."""

    def __init__(
        self,
        *,
        office_id: str,
        container_name: str,
        office_name: str = "",
        on_auth_state: Callable[[bool], None] | None = None,
        probe: Callable[[str], "Coroutine[Any, Any, bool]"] | None = None,
        clock: Callable[[], float] | None = None,
        interval_seconds: float = KEEPALIVE_INTERVAL_SECONDS,
    ) -> None:
        from src.office_runtime import claude_auth_dir

        self._office_id = office_id
        self._auth_dir: Path = claude_auth_dir(office_id)
        self._container_name = container_name
        self._office_name = office_name or container_name
        self._on_auth_state = on_auth_state
        self._probe = probe or (lambda name: _default_probe(name, office_id))
        self._clock = clock or time.time
        self._interval = interval_seconds
        self._lock = asyncio.Lock()
        self._last_probe_at: float = 0.0
        self._next_allowed_probe_at: float = 0.0
        self._consecutive_failures: int = 0

    # ── paths ──────────────────────────────────────────────────────────

    @property
    def credentials_path(self) -> Path:
        return self._auth_dir / ".credentials.json"

    @property
    def backup_path(self) -> Path:
        return self._auth_dir / ".credentials.json.backup"

    # ── the loop ───────────────────────────────────────────────────────

    async def run(self) -> None:
        """Tick forever. Every tick is individually best-effort; only
        cancellation (office teardown / daemon shutdown) ends the loop."""
        import random

        while True:
            try:
                outcome = await self.tick()
                logger.debug(
                    "auth-keepalive[%s]: %s", self._office_name, outcome,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "auth-keepalive[%s]: tick failed (loop continues)",
                    self._office_name,
                )
            await asyncio.sleep(self._interval * random.uniform(0.8, 1.2))

    # ── one tick (unit-test surface) ───────────────────────────────────

    async def tick(self) -> str:
        """Serialize host credential writes with migration and authentication."""
        from src.office_runtime import async_runtime_lock, require_ready

        async with async_runtime_lock(self._office_id):
            require_ready(self._office_id)
            operation = asyncio.create_task(self._tick_locked())
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                await operation
                raise

    async def _tick_locked(self) -> str:
        """Run one keepalive decision. Returns a string outcome:

        ``no_credentials`` · ``restored_backup`` · ``corrupt_credentials``
        · ``no_expiry`` · ``fresh`` · ``skip_recent_probe`` ·
        ``skip_backoff`` · ``probe_ok`` · ``probe_failed``
        """
        now = self._clock()
        creds = self._read_credentials()
        if creds == "missing":
            # Never-authenticated (or signed-out) office — nothing to
            # keep alive, and not the expiry incident: leave the latch
            # alone (the setup wizard / Settings own this state).
            return "no_credentials"
        if creds == "corrupt":
            return self._restore_backup_if_valid()

        expires_at_ms = _credential_expiry(creds)
        if expires_at_ms is None:
            return "no_expiry"

        expires_at = float(expires_at_ms) / 1000.0
        if now < expires_at - REFRESH_LEAD_SECONDS:
            # A future local expiry is not proof the provider accepts the
            # token: revoked tokens can retain hours of nominal validity.
            # Keep the latest structurally valid bundle for corruption
            # recovery, but only an actual probe may clear an auth-down latch.
            self._refresh_backup()
            return "fresh"

        # OAuth maintenance must continue when business/model work is quota
        # paused. The CLI can persist refreshed credentials before a model 429;
        # warm_claude_in_container then checks login through the profile endpoint.
        probe_gap = (
            EXPIRY_PROBE_GAP_SECONDS
            if expires_at - now <= EXPIRY_PROBE_WINDOW_SECONDS
            else MIN_PROBE_GAP_SECONDS
        )

        # Within the refresh lead (or already past expiry) — time for
        # ONE warm probe, rate-limited and lock-serialized.
        if now - self._last_probe_at < probe_gap:
            return "skip_recent_probe"
        if now < self._next_allowed_probe_at:
            return "skip_backoff"

        async with self._lock:
            # Re-check under the lock — a rival caller may have probed
            # while we waited.
            now = self._clock()
            if now - self._last_probe_at < probe_gap:
                return "skip_recent_probe"
            self._last_probe_at = now
            ok = bool(await self._probe(self._container_name))

        if ok:
            self._consecutive_failures = 0
            self._next_allowed_probe_at = 0.0
            self._notify(True)
            self._refresh_backup()
            current_expiry = _credential_expiry(self._read_credentials())
            if current_expiry is not None and current_expiry > expires_at_ms:
                logger.info(
                    "auth-keepalive[%s]: sign-in verified; saved OAuth expiry advanced",
                    self._office_name,
                )
            else:
                logger.info(
                    "auth-keepalive[%s]: sign-in verified; saved OAuth expiry unchanged "
                    "(refresh not confirmed)",
                    self._office_name,
                )
            return "probe_ok"

        self._consecutive_failures += 1
        self._next_allowed_probe_at = (
            self._clock() + FAILED_PROBE_BACKOFF_SECONDS
        )
        if self._consecutive_failures >= AUTH_DOWN_AFTER_FAILURES:
            self._notify(False)
            logger.error(
                "auth-keepalive[%s]: warm probe failed %d× — Claude "
                "auth is DOWN (OAuth token expired and could not be "
                "refreshed?). The user must re-run the Claude sign-in "
                "from Office Settings (or `cbcl auth --force`).",
                self._office_name, self._consecutive_failures,
            )
        else:
            logger.warning(
                "auth-keepalive[%s]: warm probe failed (%d/%d before "
                "auth is declared down); next probe in ~%d min",
                self._office_name, self._consecutive_failures,
                AUTH_DOWN_AFTER_FAILURES,
                int(FAILED_PROBE_BACKOFF_SECONDS / 60),
            )
        return "probe_failed"

    # ── helpers ────────────────────────────────────────────────────────

    def _notify(self, ok: bool) -> None:
        if self._on_auth_state is None:
            return
        try:
            self._on_auth_state(ok)
        except Exception:
            logger.debug(
                "auth-keepalive[%s]: on_auth_state callback failed",
                self._office_name, exc_info=True,
            )

    def _read_credentials(self) -> dict | str:
        """Host-side read. Returns the parsed dict, ``"missing"``, or
        ``"corrupt"`` (exists but is not valid JSON)."""
        try:
            from src.office_runtime import read_auth_file

            raw = read_auth_file(self._office_id, ".credentials.json")
        except FileNotFoundError:
            return "missing"
        except OSError:
            logger.warning(
                "auth-keepalive[%s]: cannot read %s",
                self._office_name, self.credentials_path, exc_info=True,
            )
            return "missing"
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return "corrupt"
        return parsed if isinstance(parsed, dict) else "corrupt"

    def _restore_backup_if_valid(self) -> str:
        """The live file failed JSON-parse — restore the backup IFF the
        backup itself parses. NEVER triggered by token invalidity."""
        try:
            from src.office_runtime import read_auth_file, write_auth_file

            backup_raw = read_auth_file(self._office_id, ".credentials.json.backup")
            json.loads(backup_raw)
        except (OSError, json.JSONDecodeError, ValueError):
            logger.error(
                "auth-keepalive[%s]: %s is corrupt and no valid backup "
                "exists — re-run the Claude sign-in from Office Settings.",
                self._office_name, self.credentials_path,
            )
            return "corrupt_credentials"
        try:
            write_auth_file(self._office_id, ".credentials.json", backup_raw)
        except OSError:
            logger.exception(
                "auth-keepalive[%s]: failed restoring %s from backup",
                self._office_name, self.credentials_path,
            )
            return "corrupt_credentials"
        logger.warning(
            "auth-keepalive[%s]: %s failed JSON-parse — restored the "
            "last known-good backup (next tick re-evaluates expiry).",
            self._office_name, self.credentials_path,
        )
        return "restored_backup"

    def _refresh_backup(self) -> None:
        """Mirror the latest live bundle for parse-corruption recovery.

        This does not verify tokens or make a rejected refresh token reusable.
        """
        try:
            from src.office_runtime import read_auth_file, write_auth_file

            raw = read_auth_file(self._office_id, ".credentials.json")
            if (
                self.backup_path.exists()
                and read_auth_file(self._office_id, ".credentials.json.backup") == raw
            ):
                return
            write_auth_file(self._office_id, ".credentials.json.backup", raw)
        except OSError:
            logger.debug(
                "auth-keepalive[%s]: backup refresh failed",
                self._office_name, exc_info=True,
            )
