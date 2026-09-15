"""Separate bounded local verification from human and script handoffs."""


EXTERNAL_WAIT_POLICY = """
## External waits require an explicit handoff

A live process is not progress. Do not repeatedly replace a short sleep/poll
with another short sleep/poll while waiting for a person, credential, approval,
sign-in callback, or external dependency. The total allowance for checking an
imminent external condition is two minutes, not two minutes per Bash call.

If only the user can proceed, call request_user_action with the exact one
action needed, then end the session after the request succeeds. The platform
records the blocked task and routes the answer; no sleeping worker is needed.
Never put credentials or one-time codes into chat, checkpoints, or logs.

For long autonomous work, use a managed script and hand off to its tracked
execution. Do not launch a duplicate script merely because your session ended.
On verification-resume, inspect the recorded execution and outputs first.
If the run failed, diagnose or escalate; do not automatically repeat side
effects. Local workflow subagents still need their bounded in-turn completion;
that rule does not override an explicit human or managed-script handoff.
"""
