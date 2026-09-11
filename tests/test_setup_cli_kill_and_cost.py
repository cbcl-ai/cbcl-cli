"""``_run_claude_cli`` hardening (Flow Studio fix lane).

Two behaviors of ``src/_setup_cli.py``:

* **Timeout ownership.** The trusted in-container helper owns the CLI
  subprocess and its shorter execution deadline; the host docker-exec
  timeout includes an additional shutdown/transport allowance.
* **Cost capture (spec §11).** Passing ``cost_sink`` opts the call into
  the ``--output-format json`` envelope; ``total_cost_usd`` is appended
  to the sink and the envelope's ``result`` text is returned. Envelope
  drift falls back to raw stdout (never converts a good generation
  into a failure).
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock

import pytest

import src._setup_cli as cli


def _ok_result(stdout: str = "hello") -> MagicMock:
    result = MagicMock()
    result.returncode = 0
    result.stdout = stdout
    result.stderr = ""
    return result


def _install_fake_run(monkeypatch, claude_behavior):
    """Fake ``subprocess.run`` for the protected generation helper.
    Returns the recorded command list."""
    runs: list[tuple[list[str], dict]] = []

    def fake_run(cmd, **kwargs):
        runs.append((cmd, kwargs))
        if cli._GENERATION_RUNNER in cmd:
            return claude_behavior(cmd)
        return _ok_result("")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    return runs


@pytest.mark.asyncio
async def test_timeout_delegates_inner_deadline_before_host_timeout(monkeypatch):
    """The container helper enforces the CLI deadline before the outer timeout."""

    def timeout(cmd):
        raise subprocess.TimeoutExpired(cmd, 240)

    runs = _install_fake_run(monkeypatch, timeout)

    with pytest.raises(subprocess.TimeoutExpired):
        await cli._run_claude_cli("a" * 64, "sys", "usr", timeout=240)

    assert len(runs) == 1
    command, kwargs = runs[0]
    assert command[-1] == cli._GENERATION_RUNNER
    assert json.loads(kwargs["input"])["timeout"] == 240
    assert kwargs["timeout"] > 240


@pytest.mark.asyncio
async def test_happy_path_issues_no_kill(monkeypatch):
    runs = _install_fake_run(monkeypatch, lambda cmd: _ok_result("fine"))
    out = await cli._run_claude_cli("a" * 64, "sys", "usr")
    assert out == "fine"
    assert len(runs) == 1


@pytest.mark.asyncio
async def test_cost_sink_opts_into_json_envelope(monkeypatch):
    envelope = (
        '{"result": "the text", "total_cost_usd": 0.042, "is_error": false}'
    )
    runs = _install_fake_run(monkeypatch, lambda cmd: _ok_result(envelope))
    sink: list[float] = []
    out = await cli._run_claude_cli(
        "a" * 64, "sys", "usr", cost_sink=sink
    )
    assert out == "the text"
    assert sink == [0.042]
    assert json.loads(runs[0][1]["input"])["output_format"] == "json"


@pytest.mark.asyncio
async def test_no_cost_sink_keeps_text_output(monkeypatch):
    runs = _install_fake_run(monkeypatch, lambda cmd: _ok_result("plain"))
    out = await cli._run_claude_cli("a" * 64, "sys", "usr")
    assert out == "plain"
    assert json.loads(runs[0][1]["input"])["output_format"] == "text"


def test_extract_json_envelope_fallbacks():
    sink: list[float] = []
    # Envelope drift → raw stdout back, no cost, no crash.
    assert cli._extract_json_envelope("not json at all", sink) == (
        "not json at all"
    )
    assert cli._extract_json_envelope('["a", "list"]', sink) == '["a", "list"]'
    assert sink == []
    # result missing → raw stdout, but cost still captured.
    raw = '{"total_cost_usd": 0.01}'
    assert cli._extract_json_envelope(raw, sink) == raw
    assert sink == [0.01]
    # A bool cost is shape drift, not a number.
    sink.clear()
    cli._extract_json_envelope('{"result": "x", "total_cost_usd": true}', sink)
    assert sink == []
