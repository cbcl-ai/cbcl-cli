"""GEN-14: budget-permitted single retry for a CHEAP parse failure on an
otherwise no-retry (single-shot) generation flow. A TIMEOUT is never retried."""
from __future__ import annotations

import subprocess
from unittest.mock import AsyncMock

import pytest

import src._setup_cli as cli
from src._setup_cli import _run_chunk


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def fake_sleep(_s):
        return None

    monkeypatch.setattr(cli.asyncio, "sleep", fake_sleep)


@pytest.mark.asyncio
async def test_parse_failure_gets_one_budget_retry(monkeypatch):
    # First call returns malformed JSON (fast), second returns good JSON.
    mock = AsyncMock(side_effect=["not json at all", '{"ok": true}'])
    monkeypatch.setattr(cli, "_run_claude_cli", mock)

    out = await _run_chunk(
        "cbcl-office-test", "sys", "usr",
        timeout=150, max_retries=0, effort=None,
    )
    assert out == {"ok": True}
    assert mock.await_count == 2  # the budget retry fired


@pytest.mark.asyncio
async def test_timeout_is_never_retried(monkeypatch):
    mock = AsyncMock(side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=150))
    monkeypatch.setattr(cli, "_run_claude_cli", mock)

    with pytest.raises(subprocess.TimeoutExpired):
        await _run_chunk(
            "cbcl-office-test", "sys", "usr",
            timeout=150, max_retries=0, effort=None,
        )
    assert mock.await_count == 1  # no retry on a timeout


@pytest.mark.asyncio
async def test_deterministic_empty_output_is_not_retried(monkeypatch):
    # GEN-14: an empty-output/auth failure (EmptyGenerationOutputError) is
    # DETERMINISTIC — it would fail identically, so no budget retry is spent.
    from src._setup_json import EmptyGenerationOutputError

    mock = AsyncMock(side_effect=EmptyGenerationOutputError("auth broken"))
    monkeypatch.setattr(cli, "_run_claude_cli", mock)

    with pytest.raises(EmptyGenerationOutputError):
        await _run_chunk(
            "cbcl-office-test", "sys", "usr",
            timeout=150, max_retries=0, effort=None,
        )
    assert mock.await_count == 1  # no retry on a deterministic failure


@pytest.mark.asyncio
async def test_budget_retry_skipped_when_it_would_not_fit(monkeypatch):
    # A long per-call timeout means a retry can't fit under the ceiling → no retry.
    mock = AsyncMock(side_effect=["not json", '{"ok": true}'])
    monkeypatch.setattr(cli, "_run_claude_cli", mock)

    with pytest.raises(Exception):
        await _run_chunk(
            "cbcl-office-test", "sys", "usr",
            timeout=360, max_retries=0, effort=None,
        )
    assert mock.await_count == 1  # 360 + 30 > 240 budget → no budget retry
