"""Stdlib-only SDK shipped to every mini-project's ``lib/cubicle/``.

This file is the CONTENTS of what will land at
``{script_dir}/lib/cubicle/__init__.py`` when the bootstrap creates
a new script. The outbox watcher reads the JSON files this module
writes.

Scripts import it like:

    import cubicle
    cubicle.notify_manager(
        workstream="Recruitment",
        message="Sourced 87 profiles, 13 flagged — please review.",
        attachments=["outputs/sourced_profiles.json"],
    )

Design constraints:
  - Zero third-party imports. A script with ``requirements.txt: []``
    must be able to use ``cubicle.notify_manager`` as soon as it
    starts, no pip install needed.
  - Atomic drop via ``os.replace``. A crash mid-write can't produce
    a half-read payload the watcher would either parse partially or
    skip forever.
  - Stateless — no globals, no background threads. Each call is
    self-contained so unit tests can exercise it without setup.

This file is also imported by ``tests/test_outbox_watcher.py`` as a
sanity check: the helper's output must round-trip cleanly through
the Pydantic ``OutboxNotifyPayload`` schema the watcher enforces.
"""

from __future__ import annotations

import json
import os
import time
import uuid


def output_dir() -> str:
    """Return the per-task output directory injected by the Runner.

    The Runner sets ``CUBICLE_OUTPUT_DIR`` based on the task's
    workstream + (optional) scope so output from different
    workstreams stays separated and discoverable. The directory is
    pre-created — scripts can write to it directly without a mkdir.

    Path shape:
        * ``/workspace/outputs/{workstream_short_code}/{scope_readable_id}/``
          when the script was triggered from a scoped task.
        * ``/workspace/outputs/{workstream_short_code}/`` when the
          task has no scope (legacy one-off path).
        * ``/workspace/outputs/`` when the script was triggered
          manually with no task context (UI's manual Run button on a
          script not bound to a workstream).

    Use this instead of hardcoding ``/workspace/outputs/`` so the
    same script works correctly across workstreams.

    Example::

        import cubicle
        with open(f"{cubicle.output_dir()}/profiles.json", "w") as f:
            json.dump(results, f)

    Returns:
        Absolute path string. Always set when the script is launched
        via the Runner; falls back to ``/workspace/outputs`` if the
        env var is somehow missing (legacy or test environments).
    """
    return os.environ.get("CUBICLE_OUTPUT_DIR", "/workspace/outputs")


def notify_manager(
    message: str,
    workstream: str | None = None,
    attachments: list[str] | None = None,
) -> str:
    """Send a notification to the office Manager.

    Args:
        message: Free-text message. Caps at 8 K characters — longer
            content should be dropped in a file and referenced via
            ``attachments``.
        workstream: Target chat context. Optional — when omitted,
            the helper auto-resolves to the workstream of the task
            that triggered the script. Resolution order:
              1. Caller-supplied value (UUID, display name, or
                 ``"general_chat"``).
              2. ``CUBICLE_WORKSTREAM_SHORT_CODE`` env var the
                 Runner injects for every task-linked execution —
                 covers the common case ("the script ran as part
                 of task X; route the response to X's workstream
                 chat") without forcing scriptmakers to thread the
                 value through their own code.
              3. ``"general_chat"`` fallback for manual UI Runs
                 with no task context.
            If you explicitly want general chat, pass
            ``workstream="general_chat"``; the env-derived value
            won't override an explicit caller argument.
        attachments: Optional list of workspace-relative paths the
            Manager can read. Absolute paths and ``..`` traversal
            attempts are dropped before the payload reaches the
            Manager; the message still goes through with the
            sanitised list so a scriptmaker can see which
            attachments were skipped.

    Returns:
        The filename of the dropped notification (useful for logs
        + debugging — the user can ``ls .outbox/.processed/`` to
        find it after the watcher picks it up).

    Raises:
        RuntimeError: if ``CUBICLE_SCRIPT_DIR`` isn't set. The
            Runner always injects this; running the script outside
            the Runner is a scriptmaker mistake and the exception
            makes the fix obvious.
    """
    script_dir = os.environ.get("CUBICLE_SCRIPT_DIR")
    if not script_dir:
        raise RuntimeError(
            "cubicle.notify_manager: CUBICLE_SCRIPT_DIR env var is not "
            "set. This helper must run inside a mini-project launched "
            "by the Cubicle Runner — it won't work when invoked manually."
        )

    # Auto-derive the target workstream from the task context the
    # Runner injects. Caller-supplied value wins (lets scriptmakers
    # route to general_chat or a different workstream explicitly).
    if workstream is None or (
        isinstance(workstream, str) and not workstream.strip()
    ):
        env_ws = (
            os.environ.get("CUBICLE_WORKSTREAM_SHORT_CODE")
            or ""
        ).strip()
        workstream = env_ws or "general_chat"

    outbox = os.path.join(script_dir, ".outbox")
    os.makedirs(outbox, exist_ok=True)

    payload = {
        "v": 1,
        "action": "notify_manager",
        "workstream": workstream,
        "message": message,
        "attachments": list(attachments or []),
        "execution_id": os.environ.get("CUBICLE_EXECUTION_ID"),
        "task_id": os.environ.get("CUBICLE_TASK_ID"),
        "script_name": os.environ.get("CUBICLE_SCRIPT_NAME"),
        "emitted_at": time.time(),
    }

    fname = f"notify-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.json"
    tmp = os.path.join(outbox, fname + ".tmp")
    final = os.path.join(outbox, fname)

    # Write to tempfile + rename so the watcher never sees a
    # half-written file. json.dumps in one shot keeps this small.
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, final)
    return fname
