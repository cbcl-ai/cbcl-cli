"""B2b regression: the in-container MCP script-exec path MUST inject
``CUBICLE_WORKSTREAM_SHORT_CODE`` + ``CUBICLE_SCOPE_READABLE_ID``
into the script subprocess env so the SDK's
``cubicle.notify_manager(message)`` (no explicit workstream) routes
to the task's workstream chat instead of silently defaulting to
``general_chat``.

The host-side ``ScriptRunner._build_env`` already injects both env
vars (``script_runner.py:407-410``). The in-container MCP path had
divergent env-building that omitted them — so agent-triggered runs
landed in general_chat while host-triggered runs (UI / cron) went
to the workstream. User report 2026-05-29 surfaced the asymmetry.

The in-container module uses sibling-import semantics (works only
in the agent image's ``/opt/cubicle/`` layout) so direct import in a
test environment fails. Source-level pin tests are sufficient — they
catch any future "tidy up the env-building block" pass that drops
the injection again.
"""
from __future__ import annotations

import pathlib


_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_MCP_SCRIPT_EXEC = _REPO_ROOT / "src" / "_agent_image" / "_mcp_script_exec.py"
_HOST_RUNNER = _REPO_ROOT / "src" / "scripts" / "script_runner.py"


def test_in_container_path_injects_workstream_short_code() -> None:
    """The agent-triggered run MUST set the env var the SDK reads."""
    text = _MCP_SCRIPT_EXEC.read_text()
    assert 'env_values["CUBICLE_WORKSTREAM_SHORT_CODE"]' in text, (
        "B2b regression — CUBICLE_WORKSTREAM_SHORT_CODE injection "
        "was removed from _mcp_script_exec.py's env-building block. "
        "Agent-triggered ``cubicle.notify_manager(message)`` will "
        "silently route to general_chat. Re-add the injection block "
        "alongside the CUBICLE_SCRIPT_DIR / CUBICLE_TASK_ID writes."
    )


def test_in_container_path_injects_scope_readable_id() -> None:
    """The companion scope env var the SDK reads for output_dir."""
    text = _MCP_SCRIPT_EXEC.read_text()
    assert 'env_values["CUBICLE_SCOPE_READABLE_ID"]' in text, (
        "B2b regression — CUBICLE_SCOPE_READABLE_ID injection "
        "was removed from _mcp_script_exec.py. Scope-scoped output "
        "directories won't be available to agent-triggered scripts."
    )


def test_in_container_injection_is_conditional() -> None:
    """No empty values — only inject when the constants have content,
    same shape as the host-side runner. An empty env var still
    overrides the SDK's fallback path; the conditional injection
    keeps the manual-UI-run fallback (no workstream context →
    general_chat) intact."""
    text = _MCP_SCRIPT_EXEC.read_text()
    assert "if WORKSTREAM_SHORT_CODE:" in text, (
        "B2b injection must be conditional on the module constant — "
        "unconditional injection would clobber the manual-UI-run "
        "fallback that legitimately resolves to general_chat."
    )
    assert "if SCOPE_READABLE_ID:" in text


def test_in_container_path_records_pid_for_reconcile() -> None:
    """F3 / ADD-C1 symmetry: the agent in-container path must record its
    ``in_container.pid`` so the host's startup reconcile can kill an
    agent-triggered orphan after a hard MCP-process kill, exactly as it
    does for host-launched runs."""
    text = _MCP_SCRIPT_EXEC.read_text()
    assert '"in_container.pid"' in text and "proc.pid" in text, (
        "F3 regression — _mcp_script_exec.py no longer writes "
        "in_container.pid after spawning the script. Agent-triggered "
        "in-container orphans become unkillable by "
        "reconcile_orphaned_executions (no pidfile → marked failed "
        "without a kill)."
    )
    # Spawn must be session-detached so the kill can reap the tree.
    assert "start_new_session=True" in text


def test_host_and_in_container_inject_same_env_vars() -> None:
    """Cross-pin: the two paths MUST stay in sync. If the host
    runner gains or drops an injected var, the in-container path
    must mirror the change so agent-triggered runs and UI-triggered
    runs behave identically. User report 2026-05-29 was triggered
    by exactly this kind of divergence — host had it, container
    didn't, and the SDK silently downgraded to general_chat."""
    host = _HOST_RUNNER.read_text()
    in_container = _MCP_SCRIPT_EXEC.read_text()

    for var in (
        "CUBICLE_SCRIPT_DIR",
        "CUBICLE_SCRIPT_NAME",
        "CUBICLE_EXECUTION_ID",
        "CUBICLE_TASK_ID",
        "CUBICLE_WORKSTREAM_SHORT_CODE",
        "CUBICLE_SCOPE_READABLE_ID",
        "CUBICLE_OUTPUT_DIR",
    ):
        # The host path uses different brackets (meta_env["..."]),
        # the in-container path uses env_values["..."]. Both must
        # mention the var by name.
        assert var in host, (
            f"host runner missing env var {var!r} — drift from "
            "in-container path"
        )
        assert var in in_container, (
            f"in-container path missing env var {var!r} — drift from "
            "host runner (B2b regression class)"
        )
