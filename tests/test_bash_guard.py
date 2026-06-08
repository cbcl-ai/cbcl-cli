"""Tests for the PreToolUse Bash guard (Tier 3 worker-session-churn fix).

Verifies the pattern classifier blocks open-ended monitors / poll loops
and allows ordinary (and explicitly-bounded) commands, plus the stdin →
stdout deny contract of ``main()``.
"""
from __future__ import annotations

import io
import json

import pytest

from src._agent_image.bash_guard import classify_command, main


# Commands that MUST be blocked (open-ended monitors / infinite loops).
BLOCK = [
    "tail -f /var/log/app.log",
    "tail -F app.log",
    "tail -n 100 -f app.log",
    "journalctl -f",
    "journalctl --follow -u nginx",
    "docker logs -f web",
    "docker logs --follow web",
    "kubectl logs --follow pod-xyz",
    "podman logs -f ctr",
    "while true; do echo x; sleep 1; done",
    "while :; do curl localhost; done",
    "while [ 1 ]; do echo hi; done",
    "watch -n 1 'curl -sf http://localhost:3000/health'",
    "for (( ; ; )); do :; done",
    "until curl -sf http://host:3000/health; do sleep 5; done",
    "sudo docker logs -f cbcl-office-dev",
    "echo start && tail -f log",
]

# Commands that MUST be allowed (bounded, one-shot, or snapshot reads).
ALLOW = [
    "",
    "   ",
    "ls -la",
    "git status",
    "git log --oneline -10",
    "curl -sf http://host:3000/health",
    "docker logs --tail 200 web",
    "journalctl -n 200 -u nginx",
    "tail -n 200 app.log",
    "grep -rn 'while true' .",  # searching for the text, not running it*
    "for i in $(seq 1 24); do curl -sf url && break; sleep 5; done",
    "while read line; do echo \"$line\"; sleep 1; done < input.txt",
    "sleep 5",
    "timeout 30 tail -f app.log",
    "timeout 5m ./wait-for-it.sh",
    "python3 manage.py migrate",
]


@pytest.mark.parametrize("cmd", BLOCK)
def test_classify_blocks_unbounded(cmd: str) -> None:
    block, reason = classify_command(cmd)
    assert block is True, f"expected BLOCK for: {cmd!r}"
    assert reason, "a blocked command must carry a reason label"


@pytest.mark.parametrize("cmd", ALLOW)
def test_classify_allows_safe(cmd: str) -> None:
    # NOTE on grep-for-text: substring matching means a command that
    # merely *mentions* "while true" in a quoted grep pattern is a known,
    # accepted false-negative-direction tradeoff — but `grep ... 'while
    # true'` does NOT contain a loop keyword followed by `do`, and has no
    # follow flag, so it classifies as allow. If that ever regresses,
    # revisit the anchoring.
    block, _ = classify_command(cmd)
    assert block is False, f"expected ALLOW for: {cmd!r}"


def test_timeout_wrapper_overrides_follow() -> None:
    # An explicit timeout bounds the runtime → allowed even with -f.
    assert classify_command("timeout 60 docker logs -f web")[0] is False


def test_main_emits_deny_json(monkeypatch, capsys) -> None:
    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "tail -f x.log"}}
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    out = json.loads(capsys.readouterr().out)
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert "tail -f" in hso["permissionDecisionReason"]


def test_main_allows_safe_no_output(monkeypatch, capsys) -> None:
    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert capsys.readouterr().out == ""


def test_main_ignores_non_bash_tools(monkeypatch, capsys) -> None:
    payload = json.dumps(
        {"tool_name": "Read", "tool_input": {"file_path": "/x"}}
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert capsys.readouterr().out == ""


def test_main_fails_open_on_garbage(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("not json{{"))
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert capsys.readouterr().out == ""
