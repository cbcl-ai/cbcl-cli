"""Proactive Claude OAuth keepalive — one background loop per office.

The recurring owner incident (every ~2-3 weeks): the container CLI's
OAuth login dies mid-work with "Failed to authenticate: OAuth session
expired and could not be refreshed", every Manager turn and worker
session fails until the user re-runs the sign-in, and Settings shows
"Not authenticated". Root cause shape: the CLI refreshes the OAuth
token ONLY when a session runs, refresh tokens are SINGLE-USE and
rotate on every refresh, and an idle office (or ~20 workers all waking
at once and racing the one-shot refresh token) eventually lands on an
access token past expiry with a refresh token that can no longer be
redeemed.

This loop removes both failure modes:

* **Idle expiry** — the host-side ``expiresAt`` is read every few
  minutes; when the access token is within ``REFRESH_LEAD_SECONDS`` of
  expiry, ONE cheap warm probe (``verify_claude_in_container`` — a
  ``--max-turns 1`` haiku round-trip) runs, which makes the CLI itself
  perform its internal refresh and rewrite ``.credentials.json`` with
  a fresh access token AND a fresh rotated refresh token. Because the
  refresh token renews on every refresh, a regular cadence means no
  fixed refresh-token TTL can ever bite.
* **Concurrent-refresh race** — the probe runs alone, under a
  per-office asyncio lock, while the office is otherwise quiet at the
  expiry boundary, instead of N worker sessions racing the single-use
  refresh token.

Credential reads are HOST-side only: ``.credentials.json`` lives in the
bind-mounted ``<workspace>/.claude-auth/`` dir (mapped to
``/home/agent/.claude`` — ``docker/container_manager.claude_auth_dir``),
so no ``docker exec`` is spent on the every-few-minutes read; only the
actual probe execs into the container.

Outcomes feed the ManagerController's auth-expired latch via
``on_auth_state`` (→ ``ManagerController.note_auth_probe``): a
successful probe clears it; ``AUTH_DOWN_AFTER_FAILURES`` consecutive
probe failures mark auth down so the next failing Manager turn surfaces
the auth explainer immediately — even when its own error text is the
useless synthetic exit line. There is deliberately NO chat post from
this loop (a system chat row needs a context + an FE-whitelisted
payload kind); the latch + loud logs + the Settings surface are the
honest signal.

Corruption guard: ``auth_service._write_credentials`` keeps a
``.credentials.json.backup`` beside the live file, and this loop
refreshes that backup whenever the live bundle shows evidence of
working (fresh expiry / successful probe). The backup is restored ONLY
when the live file fails JSON-parse — never on token invalidity (a
parse failure is disk/write corruption; invalid tokens need the user's
re-login, and "restoring" would just mask that).

Unit-testable by construction: the clock, the probe, and the RNG are
injectable; ``tick()`` returns a string outcome the tests assert on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

# Loop cadence (jittered ±20% so a multi-office daemon doesn't probe
# every office in the same instant).
KEEPALIVE_INTERVAL_SECONDS = 300.0
# Probe when the access token is within this lead of ``expiresAt``.
REFRESH_LEAD_SECONDS = 30 * 60.0
# Never probe twice within this window (a probe IS a Claude API call).
MIN_PROBE_GAP_SECONDS = 10 * 60.0
# After a failed probe, hold off this long before probing again — a
# dead refresh token doesn't heal on its own, and each probe costs an
# API round-trip that will just fail again.
FAILED_PROBE_BACKOFF_SECONDS = 30 * 60.0
# Consecutive probe failures before auth is declared DOWN (the latch
# then fronts even unclassifiable Manager-turn errors with auth copy).
AUTH_DOWN_AFTER_FAILURES = 2


def _default_probe(container_name: str) -> "Coroutine[Any, Any, bool]":
    """Run the proven warm probe off-loop (it is blocking subprocess IO)."""
    from src.auth_helpers import verify_claude_in_container

    return asyncio.to_thread(verify_claude_in_container, container_name)


class AuthKeepalive:
    """Per-office OAuth keepalive loop. Create once per connected office
    and run :meth:`run` under the daemon's task supervisor."""

    def __init__(
        self,
        *,
        workspace_path: str,
        container_name: str,
        office_name: str = "",
        on_auth_state: Callable[[bool], None] | None = None,
        probe: Callable[[str], "Coroutine[Any, Any, bool]"] | None = None,
        clock: Callable[[], float] | None = None,
        interval_seconds: float = KEEPALIVE_INTERVAL_SECONDS,
    ) -> None:
        from src.docker.container_manager import claude_auth_dir

        self._auth_dir: Path = claude_auth_dir(workspace_path)
        self._container_name = container_name
        self._office_name = office_name or container_name
        self._on_auth_state = on_auth_state
        self._probe = probe or _default_probe
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

        expires_at_ms = (
            creds.get("claudeAiOauth", {}).get("expiresAt")
            if isinstance(creds, dict)
            else None
        )
        if not isinstance(expires_at_ms, (int, float)):
            # Unknown shape (older CLI / manual edit) — don't guess.
            return "no_expiry"

        expires_at = float(expires_at_ms) / 1000.0
        if now < expires_at - REFRESH_LEAD_SECONDS:
            # Healthy: the CLI refreshed recently (any session does).
            # The valid access token is proof auth works — clear the
            # latch and keep the corruption backup current (it now
            # carries the newest ROTATED refresh token).
            self._consecutive_failures = 0
            self._notify(True)
            self._refresh_backup()
            return "fresh"

        # Within the refresh lead (or already past expiry) — time for
        # ONE warm probe, rate-limited and lock-serialized.
        if now - self._last_probe_at < MIN_PROBE_GAP_SECONDS:
            return "skip_recent_probe"
        if now < self._next_allowed_probe_at:
            return "skip_backoff"

        async with self._lock:
            # Re-check under the lock — a rival caller may have probed
            # while we waited.
            now = self._clock()
            if now - self._last_probe_at < MIN_PROBE_GAP_SECONDS:
                return "skip_recent_probe"
            self._last_probe_at = now
            ok = bool(await self._probe(self._container_name))

        if ok:
            self._consecutive_failures = 0
            self._next_allowed_probe_at = 0.0
            self._notify(True)
            self._refresh_backup()
            logger.info(
                "auth-keepalive[%s]: warm probe OK — the CLI refreshed "
                "the OAuth token (expiry was within %d min)",
                self._office_name, int(REFRESH_LEAD_SECONDS / 60),
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
            raw = self.credentials_path.read_text()
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
            backup_raw = self.backup_path.read_text()
            json.loads(backup_raw)
        except (OSError, json.JSONDecodeError, ValueError):
            logger.error(
                "auth-keepalive[%s]: %s is corrupt and no valid backup "
                "exists — re-run the Claude sign-in from Office Settings.",
                self._office_name, self.credentials_path,
            )
            return "corrupt_credentials"
        try:
            self.credentials_path.write_text(backup_raw)
            self.credentials_path.chmod(0o600)
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
        """Copy the (parse-valid, evidence-of-working) live file over the
        backup so the backup always carries the newest rotated refresh
        token. Best-effort."""
        try:
            raw = self.credentials_path.read_text()
            if (
                self.backup_path.exists()
                and self.backup_path.read_text() == raw
            ):
                return
            self.backup_path.write_text(raw)
            self.backup_path.chmod(0o600)
        except OSError:
            logger.debug(
                "auth-keepalive[%s]: backup refresh failed",
                self._office_name, exc_info=True,
            )
