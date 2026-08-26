"""Unit tests for the proactive Claude OAuth keepalive.

Everything is injected — clock, probe, workspace paths — so no docker
and no real time pass. Contract (``src/auth_keepalive.py``):

* host-side ``expiresAt`` read; no probe while the token is fresh;
* within the 30-min lead: ONE warm probe, rate-limited (10-min probe
  gap) and backed off ~30 min after a failure;
* probe verdicts feed ``on_auth_state`` — True on success (clears the
  Manager's auth latch), False only after 2 consecutive failures;
* corruption guard: the backup is restored ONLY when the live file
  fails JSON-parse, never on token invalidity; healthy ticks refresh
  the backup so it carries the newest rotated refresh token.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.auth_keepalive import (
    AUTH_DOWN_AFTER_FAILURES,
    FAILED_PROBE_BACKOFF_SECONDS,
    MIN_PROBE_GAP_SECONDS,
    REFRESH_LEAD_SECONDS,
    AuthKeepalive,
)

NOW = 1_700_000_000.0  # arbitrary fixed epoch


class FakeProbe:
    def __init__(self, results=None):
        self.calls = 0
        self.results = list(results or [])

    def __call__(self, container_name: str):
        self.calls += 1
        ok = self.results.pop(0) if self.results else True

        async def _run():
            return ok

        return _run()


class Clock:
    def __init__(self, now: float = NOW):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _write_creds(workspace: Path, expires_at_s: float) -> Path:
    auth_dir = workspace / ".claude-auth"
    auth_dir.mkdir(parents=True, exist_ok=True)
    path = auth_dir / ".credentials.json"
    path.write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": "tok",
            "refreshToken": "ref",
            "expiresAt": int(expires_at_s * 1000),
        },
    }))
    return path


def _keepalive(tmp_path: Path, clock: Clock, probe: FakeProbe, states: list):
    return AuthKeepalive(
        workspace_path=str(tmp_path),
        container_name="cbcl-office-test",
        office_name="test-office",
        on_auth_state=states.append,
        probe=probe,
        clock=clock,
    )


@pytest.mark.asyncio
async def test_missing_credentials_is_a_quiet_noop(tmp_path):
    probe, states = FakeProbe(), []
    ka = _keepalive(tmp_path, Clock(), probe, states)
    assert await ka.tick() == "no_credentials"
    assert probe.calls == 0
    assert states == []  # never-authenticated ≠ the expiry incident


@pytest.mark.asyncio
async def test_fresh_token_skips_probe_and_clears_latch(tmp_path):
    clock = Clock()
    _write_creds(tmp_path, clock.now + 2 * REFRESH_LEAD_SECONDS)
    probe, states = FakeProbe(), []
    ka = _keepalive(tmp_path, clock, probe, states)

    assert await ka.tick() == "fresh"
    assert probe.calls == 0
    assert states == [True]
    # Healthy tick keeps the corruption backup current.
    assert ka.backup_path.exists()
    assert ka.backup_path.read_text() == ka.credentials_path.read_text()


@pytest.mark.asyncio
async def test_near_expiry_runs_one_probe_and_reports_ok(tmp_path):
    clock = Clock()
    _write_creds(tmp_path, clock.now + REFRESH_LEAD_SECONDS / 2)
    probe, states = FakeProbe([True]), []
    ka = _keepalive(tmp_path, clock, probe, states)

    assert await ka.tick() == "probe_ok"
    assert probe.calls == 1
    assert states == [True]


@pytest.mark.asyncio
async def test_probe_gap_rate_limits_repeat_probes(tmp_path):
    clock = Clock()
    _write_creds(tmp_path, clock.now + 60)  # nearly expired
    probe, states = FakeProbe([True, True]), []
    ka = _keepalive(tmp_path, clock, probe, states)

    assert await ka.tick() == "probe_ok"
    clock.now += MIN_PROBE_GAP_SECONDS / 2
    assert await ka.tick() == "skip_recent_probe"
    assert probe.calls == 1
    clock.now += MIN_PROBE_GAP_SECONDS
    assert await ka.tick() == "probe_ok"
    assert probe.calls == 2


@pytest.mark.asyncio
async def test_failed_probe_backs_off_then_marks_auth_down(tmp_path):
    clock = Clock()
    _write_creds(tmp_path, clock.now - 60)  # already expired
    probe, states = FakeProbe([False, False]), []
    ka = _keepalive(tmp_path, clock, probe, states)

    # First failure: backoff armed, auth NOT yet declared down.
    assert await ka.tick() == "probe_failed"
    assert states == []
    clock.now += MIN_PROBE_GAP_SECONDS + 1
    assert await ka.tick() == "skip_backoff"  # 30-min failure backoff holds
    assert probe.calls == 1

    # Past the backoff: second failure crosses the auth-down threshold.
    clock.now += FAILED_PROBE_BACKOFF_SECONDS
    assert await ka.tick() == "probe_failed"
    assert probe.calls == AUTH_DOWN_AFTER_FAILURES
    assert states == [False]


@pytest.mark.asyncio
async def test_recovery_after_failures_clears_auth_down(tmp_path):
    clock = Clock()
    _write_creds(tmp_path, clock.now - 60)
    probe, states = FakeProbe([False, False, True]), []
    ka = _keepalive(tmp_path, clock, probe, states)

    await ka.tick()
    clock.now += FAILED_PROBE_BACKOFF_SECONDS + MIN_PROBE_GAP_SECONDS
    await ka.tick()
    assert states == [False]

    # User re-signed in (fresh file would normally appear, but even a
    # working probe on the old path must clear the latch).
    clock.now += FAILED_PROBE_BACKOFF_SECONDS + MIN_PROBE_GAP_SECONDS
    assert await ka.tick() == "probe_ok"
    assert states == [False, True]


@pytest.mark.asyncio
async def test_corrupt_live_file_restores_valid_backup(tmp_path):
    clock = Clock()
    path = _write_creds(tmp_path, clock.now + 2 * REFRESH_LEAD_SECONDS)
    probe, states = FakeProbe(), []
    ka = _keepalive(tmp_path, clock, probe, states)
    await ka.tick()  # writes the backup

    good = path.read_text()
    path.write_text("{ this is not json")
    assert await ka.tick() == "restored_backup"
    assert path.read_text() == good
    # Next tick proceeds normally off the restored file.
    assert await ka.tick() == "fresh"


@pytest.mark.asyncio
async def test_corrupt_live_file_without_backup_does_not_invent_one(
    tmp_path,
):
    clock = Clock()
    path = _write_creds(tmp_path, clock.now + 2 * REFRESH_LEAD_SECONDS)
    probe, states = FakeProbe(), []
    ka = _keepalive(tmp_path, clock, probe, states)
    path.write_text("{ nope")  # corrupt BEFORE any backup exists

    assert await ka.tick() == "corrupt_credentials"
    assert not ka.backup_path.exists()
    assert probe.calls == 0


@pytest.mark.asyncio
async def test_token_invalidity_never_triggers_restore(tmp_path):
    """The guard is for PARSE corruption only. A parse-valid file whose
    tokens are dead goes down the probe path — restoring an old backup
    over it would just mask the needed re-login."""
    clock = Clock()
    path = _write_creds(tmp_path, clock.now + 2 * REFRESH_LEAD_SECONDS)
    probe, states = FakeProbe([False]), []
    ka = _keepalive(tmp_path, clock, probe, states)
    await ka.tick()  # backup written (fresh branch)

    # Token now "expired" (invalid) but the file still parses.
    _write_creds(tmp_path, clock.now - 60)
    expired_raw = path.read_text()
    assert await ka.tick() == "probe_failed"
    # Live file untouched — no restore happened.
    assert path.read_text() == expired_raw


@pytest.mark.asyncio
async def test_missing_expiry_shape_is_left_alone(tmp_path):
    auth_dir = tmp_path / ".claude-auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / ".credentials.json").write_text(json.dumps({"weird": 1}))
    probe, states = FakeProbe(), []
    ka = _keepalive(tmp_path, Clock(), probe, states)
    assert await ka.tick() == "no_expiry"
    assert probe.calls == 0
