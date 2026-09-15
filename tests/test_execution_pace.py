"""Elapsed guidance is bounded, phase-neutral and never permission to skip QA."""
import io
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from src._agent_image.execution_pace import guidance, main

ENV = {"CBCL_TASK_RUN_ID": "a" * 32, "CBCL_TASK_RUN_STARTED_AT": "1000"}


def test_reminders_fire_once_at_each_threshold(tmp_path):
    assert guidance(ENV, 1899, tmp_path) is None
    first = guidance(ENV, 1900, tmp_path)
    assert "15 minutes" in first
    assert "Never skip required checks" in first
    assert guidance(ENV, 1901, tmp_path) is None
    assert "25 minutes" in guidance(ENV, 2500, tmp_path)
    assert guidance(ENV, 5000, tmp_path) is None


def test_first_late_call_skips_old_reminder_and_new_run_is_independent(tmp_path):
    assert "25 minutes" in guidance(ENV, 2600, tmp_path)
    assert guidance(ENV, 2000, tmp_path) is None  # wall clock moved backward
    new_run = {**ENV, "CBCL_TASK_RUN_ID": "b" * 32}
    assert guidance(new_run, 2600, tmp_path)


def test_parallel_tools_emit_one_reminder(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: guidance(ENV, 1900, tmp_path), range(16)))
    assert sum(value is not None for value in results) == 1


@pytest.mark.parametrize("env", [
    {}, {**ENV, "CBCL_TASK_RUN_ID": "../../escape"},
    {**ENV, "CBCL_TASK_RUN_STARTED_AT": "nan"},
    {**ENV, "CBCL_TASK_RUN_STARTED_AT": "inf"},
    {**ENV, "CBCL_TASK_RUN_STARTED_AT": "bad"},
    {**ENV, "CBCL_TASK_RUN_STARTED_AT": "-1"},
    {**ENV, "CBCL_TASK_RUN_STARTED_AT": "3000"},
])
def test_missing_or_invalid_context_is_silent(env, tmp_path):
    assert guidance(env, 2500, tmp_path) is None
    assert not list(tmp_path.iterdir())


def test_unwritable_state_cannot_block_execution(tmp_path):
    occupied = tmp_path / "file"
    occupied.write_text("not a directory")
    assert guidance(ENV, 2500, occupied) is None


def test_hook_outputs_advice_without_allowing_or_denying_tools(monkeypatch, tmp_path, capsys):
    from src._agent_image import execution_pace
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(execution_pace.time, "time", lambda: 2500)
    monkeypatch.setattr(execution_pace.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Write"})))
    main()
    result = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert result["hookEventName"] == "PreToolUse"
    assert "additionalContext" in result
    assert "permissionDecision" not in result
    assert "continue it" in result["additionalContext"]


@pytest.mark.parametrize("raw", ["broken json", "null", "[]", '{}', '{"tool_name":42}'])
def test_malformed_hook_input_is_silent(monkeypatch, capsys, raw):
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    main()
    assert capsys.readouterr().out == ""
