#!/usr/bin/env python3
"""PreToolUse Bash guard — blocks unbounded poll loops / monitors.

Tier 3 of the worker-session-churn fix (docs/archive/audits/2026-06-08-worker-session-churn-v4/worker-session-churn.md). Runs as
a Claude Code ``PreToolUse`` hook (matcher ``Bash``) inside the agent
container. Claude Code feeds the pending tool call to this script on
stdin as JSON; we inspect ``tool_input.command`` and, if it's an
open-ended monitor that would freeze the worker session inside one tool
call (``tail -f``, ``while true``, ``docker logs -f``, ``watch``, an
uncapped poll loop, …), we DENY it with a message that steers the agent
to a bounded wait or the Script system.

Contract (verified against the Claude Code hooks docs):
  * stdin  — ``{"tool_name": "Bash", "tool_input": {"command": "..."}, ...}``
  * deny   — exit 0 + stdout JSON:
      {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                              "permissionDecision": "deny",
                              "permissionDecisionReason": "..."}}
  * allow  — exit 0 with no/`allow` output (normal permission flow).

Hooks DO fire in ``claude --print`` headless mode, so this guard is
effective for the worker subprocesses the orchestrator spawns.

Fail-OPEN by construction: any parse error / unexpected input exits 0
without a deny, so a guard bug can never wedge an agent that was about
to run a legitimate command.
"""
from __future__ import annotations

import json
import re
import sys

# Quoted string spans are stripped before pattern matching so a command
# that merely MENTIONS a dangerous token in a string literal — e.g.
# ``grep 'while true' .`` or ``echo "tail -f"`` — isn't blocked. Only the
# executable "code" portion is inspected.
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


def _strip_quoted(command: str) -> str:
    return _QUOTED.sub(" ", command)


# A command that explicitly bounds its own runtime is fine — e.g.
# ``timeout 30 tail -f app.log`` or ``timeout 5m ./wait.sh``. If we see
# a ``timeout <duration>`` prefix anywhere we treat the whole command as
# bounded and allow it.
_TIMEOUT_OK = re.compile(r"\btimeout\s+-?-?\w*\s*\d", re.IGNORECASE)

# Each entry: (compiled pattern, human label). Patterns target the
# clearest open-ended forms; we accept rare false positives (the deny is
# advisory — the agent simply rephrases) in exchange for low complexity.
_DANGER: list[tuple[re.Pattern[str], str]] = [
    # tail -f / -F / --follow (the canonical "stream forever").
    (re.compile(r"\btail\b[^|;&\n]*?(?:\s-[A-Za-z]*[fF]\b|\s--follow\b)"),
     "tail -f / --follow (streams forever)"),
    # journalctl -f / --follow.
    (re.compile(r"\bjournalctl\b[^|;&\n]*?(?:\s-[A-Za-z]*f\b|\s--follow\b)"),
     "journalctl --follow (streams forever)"),
    # docker / kubectl / podman / nerdctl logs -f / --follow.
    (re.compile(
        r"\b(?:docker|kubectl|podman|nerdctl)\b[^|;&\n]*\blogs\b"
        r"[^|;&\n]*?(?:\s-[A-Za-z]*f\b|\s--follow\b)"),
     "container logs --follow (streams forever)"),
    # while true / while : / while 1 / while [ 1 ] — infinite loops.
    (re.compile(r"\bwhile\s+(?:true\b|:|1\b|\[\s*1\s*\]|\[\s+1\s+\])"),
     "while-true infinite loop"),
    # C-style infinite for: for (( ; ; )).
    (re.compile(r"\bfor\s*\(\(\s*;\s*;\s*\)\)"),
     "for (( ; ; )) infinite loop"),
    # watch — by definition a repeating monitor with no exit.
    (re.compile(r"(?:^|[|;&]|\s)watch\s"),
     "watch (repeats with no exit condition)"),
]

# An ``until``/``while`` poll loop that sleeps and re-checks with no cap.
# Only flagged when BOTH a loop keyword and a sleep are present, to avoid
# catching a one-shot ``until`` that's really a guard.
_POLL_LOOP = re.compile(r"\b(?:until|while)\b.*\bdo\b.*\bsleep\b", re.DOTALL)
# ``while read …`` is bounded by input EOF, not a timer — never a poll.
_WHILE_READ = re.compile(r"\bwhile\s+read\b")

_REMEDY = (
    "Unbounded in-Bash monitors freeze your session inside one tool call "
    "(no progress, looks like a hang). Use a BOUNDED wait instead — e.g. "
    "`for i in $(seq 1 24); do curl -sf URL && break; sleep 5; done` — or "
    "wrap it in `timeout N`. For genuinely long monitoring (a deploy, a "
    "log you must follow, a batch) use the Script system (background run + "
    ".progress.json) and poll `get_script_status` between short steps. "
    "Grab a snapshot, not a stream: `docker logs --tail 200` (no -f), "
    "`journalctl -n 200` (no -f), a single `curl`."
)


def classify_command(command: str) -> tuple[bool, str]:
    """Return ``(block, reason)`` for a Bash command string.

    ``block=True`` means the command is an open-ended monitor that should
    be denied. ``reason`` is the human/agent-facing explanation.
    """
    if not command or not command.strip():
        return False, ""
    # Inspect only the executable portion — string literals can mention a
    # dangerous token without running it.
    scrubbed = _strip_quoted(command)
    # Explicitly bounded → always allow.
    if _TIMEOUT_OK.search(scrubbed):
        return False, ""
    for pattern, label in _DANGER:
        if pattern.search(scrubbed):
            return True, label
    # Poll loop heuristic: a loop that sleeps and re-checks, uncapped.
    # ``while read`` is input-bounded, so it's exempt.
    if (
        _POLL_LOOP.search(scrubbed)
        and not _WHILE_READ.search(scrubbed)
        and "timeout" not in scrubbed
    ):
        return True, "uncapped poll loop (loop + sleep with no time bound)"
    return False, ""


def _deny(reason_label: str) -> None:
    """Emit the PreToolUse deny payload and exit 0."""
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Blocked: {reason_label}. {_REMEDY}"
            ),
        }
    }
    sys.stdout.write(json.dumps(out))
    sys.exit(0)


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        # Unparseable input — fail open (allow), never wedge the agent.
        sys.exit(0)

    if not isinstance(data, dict) or data.get("tool_name") != "Bash":
        sys.exit(0)

    tool_input = data.get("tool_input") or {}
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    block, label = classify_command(str(command))
    if block:
        _deny(label)
    sys.exit(0)


if __name__ == "__main__":
    main()
