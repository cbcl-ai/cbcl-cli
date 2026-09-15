#!/usr/bin/env python3
"""One-shot elapsed-time reminders for worker sessions, not a timeout.

Runs as a PreToolUse hook. The host supplies a unique run ID and UTC epoch;
no browser/provider request, task mutation, or transcript scan is needed.
At most two small markers per assignment live in the container's temporary
filesystem. Atomic creation avoids duplicate reminders for parallel tools.
Failure to emit advice never prevents a legitimate tool call.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from collections.abc import Mapping

_THRESHOLDS = (15 * 60, 25 * 60)
_RUN_ID = re.compile(r"[0-9a-f]{32}")


def guidance(env: Mapping[str, str], now: float, state_dir: Path) -> str | None:
    """Return the newest due reminder once, scoped to this assignment run."""
    run_id = env.get("CBCL_TASK_RUN_ID", "")
    if not _RUN_ID.fullmatch(run_id):
        return None
    try:
        started = float(env.get("CBCL_TASK_RUN_STARTED_AT", ""))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(started) or started <= 0 or not math.isfinite(now):
        return None
    elapsed = now - started
    due = [threshold for threshold in _THRESHOLDS if elapsed >= threshold]
    if not due:
        return None
    threshold = max(due)
    # The ID is host-generated and validated; no task/source paths enter here.
    run_dir = state_dir / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        marker = run_dir / str(threshold)
        with marker.open("x"):
            pass
        # Skip the earlier reminder if the first tool call comes after 25m.
        for earlier in due:
            if earlier < threshold:
                (run_dir / str(earlier)).touch(exist_ok=True)
    except OSError:
        return None
    return (
        f"This worker session has been open for at least {threshold // 60} minutes. "
        "For straightforward work the execution target is 15–25 minutes, not a "
        "hard deadline. Check the actual remaining acceptance criteria now. "
        "Reuse evidence for the unchanged revision, fix only concrete failures, "
        "and finish through your role's normal completion tool once required "
        "checks pass. Do not start optional reviewer committees or repeat a "
        "successful full audit. If necessary work remains, continue it and "
        "record the specific remaining requirement or blocker in a concise "
        "checkpoint. Never skip required checks or claim an unverified result."
    )


def main() -> None:
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict) or not isinstance(data.get("tool_name"), str):
            return
        text = guidance(
            os.environ, time.time(), Path(tempfile.gettempdir()) / "cubicle-execution-pace",
        )
        if text:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "additionalContext": text,
            }}))
    except (ValueError, TypeError, OSError):
        return


if __name__ == "__main__":
    main()
