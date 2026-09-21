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
OFFICE_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def private_runtime(tmp_path, monkeypatch):
    from src import office_runtime, paths

    monkeypatch.setattr(paths, "CUBICLE_HOME", tmp_path / "home")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with office_runtime.runtime_lock(OFFICE_ID):
        office_runtime.prepare_runtime(OFFICE_ID, workspace)


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
    from src.office_runtime import claude_auth_dir

    auth_dir = claude_auth_dir(OFFICE_ID)
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
        office_id=OFFICE_ID,
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
async def test_policy_error_does_not_mark_existing_credentials_invalid(tmp_path):
    from src._setup_cli import GenerationPolicyError

    states = []
    credentials = _write_creds(tmp_path, NOW + 10)
    before = credentials.read_text()

    async def unavailable(container_id):
        raise GenerationPolicyError("upgrade required")

    keepalive = AuthKeepalive(
        office_id=OFFICE_ID, container_name="container", probe=unavailable,
        clock=Clock(), on_auth_state=states.append,
    )
    with pytest.raises(GenerationPolicyError, match="upgrade"):
        await keepalive.tick()
    assert keepalive._consecutive_failures == 0
    assert states == []
    assert credentials.read_text() == before


@pytest.mark.asyncio
async def test_future_expiry_does_not_clear_known_auth_failure(tmp_path):
    clock = Clock()
    _write_creds(tmp_path, clock.now + 2 * REFRESH_LEAD_SECONDS)
    probe, states = FakeProbe(), []
    ka = _keepalive(tmp_path, clock, probe, states)
    ka._consecutive_failures = AUTH_DOWN_AFTER_FAILURES

    assert await ka.tick() == "fresh"
    assert probe.calls == 0
    assert states == []
    assert ka._consecutive_failures == AUTH_DOWN_AFTER_FAILURES
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
async def test_successful_probe_does_not_claim_unobserved_refresh(tmp_path, caplog):
    clock = Clock()
    _write_creds(tmp_path, clock.now + REFRESH_LEAD_SECONDS / 2)
    ka = _keepalive(tmp_path, clock, FakeProbe([True]), [])
    with caplog.at_level("INFO"):
        assert await ka.tick() == "probe_ok"
    assert "expiry unchanged (refresh not confirmed)" in caplog.text
    assert "expiry advanced" not in caplog.text
    assert "the CLI refreshed" not in caplog.text


@pytest.mark.asyncio
async def test_refresh_is_reported_only_when_saved_expiry_advances(tmp_path, caplog):
    clock = Clock()
    _write_creds(tmp_path, clock.now + 60)

    async def rotating_probe(_container):
        _write_creds(tmp_path, clock.now + 8 * 60 * 60)
        return True

    ka = _keepalive(tmp_path, clock, rotating_probe, [])
    with caplog.at_level("INFO"):
        assert await ka.tick() == "probe_ok"
    assert "saved OAuth expiry advanced" in caplog.text
    assert "refresh not confirmed" not in caplog.text


@pytest.mark.asyncio
async def test_probe_gap_rate_limits_repeat_probes(tmp_path):
    clock = Clock()
    _write_creds(tmp_path, clock.now + 20 * 60)
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
async def test_unchanged_expiry_gets_another_probe_before_normal_gap(tmp_path):
    clock = Clock()
    _write_creds(tmp_path, clock.now + 6 * 60)
    probe = FakeProbe([True, True])
    ka = _keepalive(tmp_path, clock, probe, [])
    assert await ka.tick() == "probe_ok"
    clock.now += 2 * 60
    assert await ka.tick() == "probe_ok"
    assert probe.calls == 2


@pytest.mark.asyncio
async def test_quota_pause_does_not_suppress_oauth_maintenance(tmp_path, monkeypatch):
    from unittest.mock import Mock

    runtime = Mock()
    runtime.quota_status.return_value = {"state": "quota_paused"}
    monkeypatch.setattr("src.runtime_state.generation_runtime", lambda _name: runtime)
    clock = Clock()
    _write_creds(tmp_path, clock.now - 60)
    states = []

    async def refresh_before_capped_model(_container):
        _write_creds(tmp_path, clock.now + 8 * 60 * 60)
        return True  # warm probe verifies the new token through the profile API

    ka = _keepalive(tmp_path, clock, refresh_before_capped_model, states)
    assert await ka.tick() == "probe_ok"
    assert states == [True]
    assert ka.credentials_path.read_text() == ka.backup_path.read_text()
    runtime.resume.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("oauth", [None, [], "invalid", {"expiresAt": True},
    {"expiresAt": float("nan")}, {"expiresAt": float("inf")}])
async def test_malformed_expiry_does_not_probe_or_clear_auth(tmp_path, oauth):
    path = _write_creds(tmp_path, NOW)
    path.write_text(json.dumps({"claudeAiOauth": oauth}))
    probe, states = FakeProbe(), []
    ka = _keepalive(tmp_path, Clock(), probe, states)
    assert await ka.tick() == "no_expiry"
    assert probe.calls == 0
    assert states == []


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


@pytest.mark.parametrize("invalid", ["{ interrupted", "[]", "null", '"not a bundle"'])
async def test_corruption_between_initial_read_and_backup_preserves_good_backup(tmp_path, monkeypatch, invalid):
    clock = Clock()
    path = _write_creds(tmp_path, clock.now + 2 * REFRESH_LEAD_SECONDS)
    ka = _keepalive(tmp_path, clock, FakeProbe(), [])
    assert await ka.tick() == "fresh"
    good = ka.backup_path.read_text()
    original_read = ka._read_credentials

    def read_then_corrupt():
        parsed = original_read()
        # Ordinary CLI sessions do not take the host lifecycle lock. Inject a
        # broken/interrupted write after the tick validated its initial read.
        path.write_text(invalid)
        return parsed

    with monkeypatch.context() as patch:
        patch.setattr(ka, "_read_credentials", read_then_corrupt)
        assert await ka.tick() == "fresh"
    assert ka.backup_path.read_text() == good
    assert path.read_text() == invalid
    assert await ka.tick() == "restored_backup"
    assert path.read_text() == good


@pytest.mark.parametrize("invalid_backup", ["[]", "null", '"not a bundle"'])
async def test_nonobject_backup_is_not_restored_over_corrupt_live_file(tmp_path, invalid_backup):
    path = _write_creds(tmp_path, NOW + 2 * REFRESH_LEAD_SECONDS)
    ka = _keepalive(tmp_path, Clock(), FakeProbe(), [])
    path.write_text("{ original corruption")
    ka.backup_path.write_text(invalid_backup)
    assert await ka.tick() == "corrupt_credentials"
    assert path.read_text() == "{ original corruption"


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
    from src.office_runtime import claude_auth_dir

    auth_dir = claude_auth_dir(OFFICE_ID)
    auth_dir.mkdir(parents=True, exist_ok=True)
    (auth_dir / ".credentials.json").write_text(json.dumps({"weird": 1}))
    probe, states = FakeProbe(), []
    ka = _keepalive(tmp_path, Clock(), probe, states)
    assert await ka.tick() == "no_expiry"
    assert probe.calls == 0
