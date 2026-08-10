"""``_run_claude_cli`` hardening (Flow Studio fix lane).

Two behaviors of ``src/_setup_cli.py``:

* **Timeout kill (the ScriptRunner NEW-2 pattern).** ``subprocess.run``'s
  ``TimeoutExpired`` kills only the HOST-side docker-exec client — with
  no TTY there is no signal forwarding, so the in-container ``claude``
  keeps burning tokens/CPU to natural completion. Flow blocks invoke
  this helper automatically and retry on failure, so without the
  pidfile + in-container kill a consistently-slow block stacks
  abandoned Opus sessions in the CPU-capped office container.
* **Cost capture (spec §11).** Passing ``cost_sink`` opts the call into
  the ``--output-format json`` envelope; ``total_cost_usd`` is appended
  to the sink and the envelope's ``result`` text is returned. Envelope
  drift falls back to raw stdout (never converts a good generation
  into a failure).
"""

from __future__ import annotations

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
    """Fake ``subprocess.run``: prompt-file writes and cleanup succeed;
    the ``claude --print`` invocation runs ``claude_behavior(cmd)``.
    Returns the recorded command list."""
    runs: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        runs.append(cmd)
        joined = " ".join(cmd)
        if "claude --print" in joined:
            return claude_behavior(cmd)
        return _ok_result("")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    return runs


@pytest.mark.asyncio
async def test_timeout_kills_the_in_container_pid(monkeypatch):
    """On TimeoutExpired the helper must issue a ``docker exec … kill``
    of the pidfile-recorded in-container PID before re-raising — the
    old behavior killed only the host client and orphaned the session."""

    def timeout(cmd):
        raise subprocess.TimeoutExpired(cmd, 240)

    runs = _install_fake_run(monkeypatch, timeout)

    with pytest.raises(subprocess.TimeoutExpired):
        await cli._run_claude_cli("cbcl-office-test", "sys", "usr", timeout=240)

    claude_cmds = [c for c in runs if "claude --print" in " ".join(c)]
    assert len(claude_cmds) == 1
    claude_cmd = " ".join(claude_cmds[0])
    # The invocation records its own in-container PID (echo $$ + exec)…
    assert "echo $$ >" in claude_cmd
    assert "exec claude --print" in claude_cmd
    pid_file = claude_cmd.split('echo $$ > "')[1].split('"')[0]
    assert pid_file.startswith("/tmp/cubicle_pid_")
    # …and the kill leg targets exactly that pidfile, in-container.
    kill_cmds = [c for c in runs if "kill" in " ".join(c)]
    assert len(kill_cmds) == 1
    kill_cmd = " ".join(kill_cmds[0])
    assert kill_cmd.startswith("docker exec cbcl-office-test")
    assert pid_file in kill_cmd


@pytest.mark.asyncio
async def test_happy_path_issues_no_kill(monkeypatch):
    runs = _install_fake_run(monkeypatch, lambda cmd: _ok_result("fine"))
    out = await cli._run_claude_cli("cbcl-office-test", "sys", "usr")
    assert out == "fine"
    assert not [c for c in runs if "kill" in " ".join(c)]


@pytest.mark.asyncio
async def test_cost_sink_opts_into_json_envelope(monkeypatch):
    envelope = (
        '{"result": "the text", "total_cost_usd": 0.042, "is_error": false}'
    )
    runs = _install_fake_run(monkeypatch, lambda cmd: _ok_result(envelope))
    sink: list[float] = []
    out = await cli._run_claude_cli(
        "cbcl-office-test", "sys", "usr", cost_sink=sink
    )
    assert out == "the text"
    assert sink == [0.042]
    claude_cmd = " ".join(
        next(c for c in runs if "claude --print" in " ".join(c))
    )
    assert "--output-format json" in claude_cmd


@pytest.mark.asyncio
async def test_no_cost_sink_keeps_text_output(monkeypatch):
    runs = _install_fake_run(monkeypatch, lambda cmd: _ok_result("plain"))
    out = await cli._run_claude_cli("cbcl-office-test", "sys", "usr")
    assert out == "plain"
    claude_cmd = " ".join(
        next(c for c in runs if "claude --print" in " ".join(c))
    )
    assert "--output-format text" in claude_cmd


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
