"""Unit tests for ``filter_script_author_tools`` in the MCP tool server.

The filter strips script-authoring tools (``register_script``) from
every worker EXCEPT the ``automation-script-developer``. This used
to live inline in ``main()`` and was therefore untested; refactored
out so the contract is explicit.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

# The MCP tool server lives inside ``src/_agent_image/`` (the bundled
# agent-image asset dir) so it ships with the wheel AND copies into
# the agent Docker image as a standalone file. Load it directly off
# disk to avoid polluting test sys.path with the asset dir.
_MCP_PATH = (
    Path(__file__).resolve().parent.parent
    / "src" / "_agent_image" / "mcp_tool_server.py"
)
_spec = importlib.util.spec_from_file_location("mcp_tool_server", _MCP_PATH)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
filter_script_author_tools = _mod.filter_script_author_tools
compute_output_dir = _mod.compute_output_dir
_get_manager_tools = _mod._get_manager_tools
_get_worker_tools = _mod._get_worker_tools


def _sample_tools() -> list[dict]:
    return [
        {"name": "register_script", "description": "create/update script"},
        {"name": "execute_script", "description": "run script"},
        {"name": "create_task", "description": "manager-only"},
        {"name": "add_activity", "description": "post a comment"},
    ]


def test_script_developer_keeps_register_script():
    tools = _sample_tools()
    out = filter_script_author_tools(tools, "automation-script-developer")
    names = [t["name"] for t in out]
    assert "register_script" in names
    assert len(out) == len(tools)


def test_other_agent_loses_register_script():
    tools = _sample_tools()
    out = filter_script_author_tools(tools, "research-agent")
    names = [t["name"] for t in out]
    assert "register_script" not in names
    # Non-authoring tools are preserved.
    assert "execute_script" in names
    assert "create_task" in names
    assert "add_activity" in names


def test_empty_agent_name_loses_register_script():
    """Empty AGENT_NAME is a spawn-time bug — fail closed (strip the tool).

    The caller (``main()``) logs CRITICAL when this happens so the bug
    surfaces in production logs. Behaviour-wise we still strip rather
    than allow, since allowing would silently grant authoring rights
    to the wrong agent.
    """
    tools = _sample_tools()
    out = filter_script_author_tools(tools, "")
    names = [t["name"] for t in out]
    assert "register_script" not in names


def test_filter_is_pure_does_not_mutate_input():
    tools = _sample_tools()
    out = filter_script_author_tools(tools, "research-agent")
    assert len(tools) == 4  # original list untouched
    assert out is not tools  # new list returned


def test_no_register_script_in_input_is_noop():
    tools = [{"name": "create_task"}, {"name": "add_activity"}]
    out = filter_script_author_tools(tools, "research-agent")
    assert out == tools


# ─── compute_output_dir — QA round 5 M1 backfill ────────────────────
# Locks the in-container twin of the host-side ScriptRunner's
# per-workstream output dir computation. Without this coverage, a
# regression in mcp_tool_server's _execute_script would silently
# break agent-triggered runs (they'd dump output into the flat
# /workspace/outputs/ root) while the host-triggered path stayed
# correct — a partial regression nearly impossible to spot in QA.


def test_output_dir_with_workstream_only():
    assert compute_output_dir("WR", "") == "/workspace/outputs/WR"


def test_output_dir_with_workstream_and_scope():
    assert (
        compute_output_dir("WR", "WR-003.S01")
        == "/workspace/outputs/WR/WR-003.S01"
    )


def test_output_dir_falls_back_to_flat_when_no_workstream():
    """Manual UI Run on a workstream-less script still produces a
    valid path — the legacy flat /workspace/outputs/ root."""
    assert compute_output_dir("", "") == "/workspace/outputs"


def test_output_dir_ignores_scope_when_workstream_missing():
    """Scope without workstream is meaningless (a scope is always
    inside a workstream). Falling back to the flat root rather
    than synthesising ``/workspace/outputs/<scope>/`` keeps the
    invariant ``ws-segment-then-scope-segment`` clean."""
    assert compute_output_dir("", "WR-003.S01") == "/workspace/outputs"


def test_output_dir_matches_host_runner_for_same_inputs():
    """Cross-check: the in-container path computation MUST match
    what the host-side ``ScriptRunner._build_launch_command``
    produces for the same workstream+scope inputs (modulo the
    workspace prefix). The host runner builds
    ``{workspace}/outputs/{ws}/{scope}/``; this in-container
    helper produces ``/workspace/outputs/{ws}/{scope}/``. Inside
    the container, ``{workspace}`` IS ``/workspace``, so the two
    paths are identical for the same inputs."""
    # Spot-check the contract with a representative input.
    out = compute_output_dir("RC", "RC-001.S02")
    assert out.startswith("/workspace/outputs/")
    assert out.endswith("/RC/RC-001.S02")


def test_output_dir_strips_whitespace_to_match_host_runner():
    """QA round 8 B1: the host-side runner does ``.strip()`` on both
    inputs before composing the path; the in-container helper must
    do the same so agent-triggered runs land in the same directory
    as UI-triggered runs even when an env var arrives with
    trailing/leading whitespace. The empirical divergence the QA
    agent flagged was ``compute_output_dir("  WR  ", "")`` →
    ``/workspace/outputs/  WR  ``, which doesn't match the host's
    ``/workspace/outputs/WR``."""
    assert compute_output_dir("  WR  ", "") == "/workspace/outputs/WR"
    assert compute_output_dir("\tWR\n", "") == "/workspace/outputs/WR"
    assert (
        compute_output_dir("  WR  ", "  WR-003.S01  ")
        == "/workspace/outputs/WR/WR-003.S01"
    )
    # Whitespace-only inputs collapse to "no workstream" rather
    # than leaving literal spaces in a path segment.
    assert compute_output_dir("   ", "") == "/workspace/outputs"
    assert compute_output_dir("WR", "   ") == "/workspace/outputs/WR"


# ─── Manager tool list — script-execution exclusion (#14 defense) ───
# The Manager is a pure orchestrator; it must NEVER execute scripts.
# Architectural defence is the per-role tool list — script-execution
# tools (execute_script, register_script, schedule_script,
# update_script_cron, delete_script_cron) appear in
# ``_get_worker_tools`` only. Pin that contract so a future
# contributor accidentally adding them to ``_get_manager_tools``
# (e.g. for "convenience") fails at test time, not at runtime when
# the Manager dumps output into the wrong directory.


_SCRIPT_EXECUTION_TOOLS = frozenset({
    "execute_script",
    "register_script",
    "schedule_script",
    "update_script_cron",
    "delete_script_cron",
})


def _tool_names(tools: list[dict]) -> set[str]:
    return {t["name"] for t in tools}


def test_manager_tool_list_excludes_script_execution_tools():
    names = _tool_names(_get_manager_tools())
    leaked = names & _SCRIPT_EXECUTION_TOOLS
    assert not leaked, (
        f"Manager tool list MUST NOT include script-execution tools. "
        f"Found leaked tools: {sorted(leaked)}. The Manager is a pure "
        f"orchestrator — script execution is a worker responsibility. "
        f"See ``_execute_script`` runtime guard for the second-line "
        f"defense."
    )


def test_manager_keeps_read_only_script_tools():
    """Manager DOES retain script LIST/READ tools so it can audit
    what scripts exist when planning tasks. The exclusion above is
    specifically about EXECUTION/AUTHORING — read-only inspection
    is fine."""
    names = _tool_names(_get_manager_tools())
    # These are read-only and should remain.
    assert "list_scripts" in names
    assert "get_script" in names
    assert "list_script_executions" in names


def test_worker_tool_list_includes_execute_script():
    """Workers DO get execute_script (subject to the per-role
    SCRIPT_AUTHOR_ONLY filter for the authoring tool). Verify the
    happy-path inclusion stays intact."""
    names = _tool_names(_get_worker_tools())
    assert "execute_script" in names


def test_execute_script_runtime_guard_blocks_manager_mode():
    """Defence in depth (#14): even if a future change leaks
    ``execute_script`` into the Manager tool list, the runtime
    handler refuses to execute when ``TASK_MODE == 'manager'``.
    Returns a clear error rather than silently dumping output
    into the flat /workspace/outputs/ root (Manager has no
    workstream short_code env var)."""
    import asyncio

    # Patch the module's TASK_MODE to manager and call the handler.
    original = _mod.TASK_MODE
    _mod.TASK_MODE = "manager"
    try:
        result = asyncio.run(
            _mod._execute_script({"script_name": "anything"}),
        )
    finally:
        _mod.TASK_MODE = original
    assert result.get("error") is True
    assert "manager" in result["message"].lower()
    # Worker-mode call still tries to read the script dir (and
    # returns a not-found error in the test's ephemeral env) —
    # confirms the guard ONLY fires in manager mode.
    _mod.TASK_MODE = "execute"
    try:
        result = asyncio.run(
            _mod._execute_script({"script_name": "nonexistent"}),
        )
    finally:
        _mod.TASK_MODE = original
    assert result.get("error") is True
    # Different error path — script dir not found (manager guard
    # was bypassed because TASK_MODE='execute').
    assert "not found" in result["message"].lower()


# ─── Triage-mode guards (C3 fix) ─────────────────────────────────────
# When the dispatcher hands a blocked task to the Manager Assistant,
# the MA's MCP server runs with TASK_MODE='triage'. The playbook says
# 'NEVER call move_task(blocked → ready) yourself' — these tests are
# the runtime defence-in-depth that backs the playbook rule. They run
# the actual ``MCPServer._execute_tool`` path with the relevant module
# globals monkey-patched.


def _run_triage(
    tool_name: str, arguments: dict, *, task_id_env: str = "current-task",
) -> dict:
    """Helper: spin up an MCPServer in triage mode and call one tool.

    Patches TASK_MODE+TASK_ID, builds a server with both worker tools
    AND a synthetic 'move_task'/'update_status' entry so the guard
    has something to dispatch against."""
    import asyncio

    original_mode = _mod.TASK_MODE
    original_task = _mod.TASK_ID
    _mod.TASK_MODE = "triage"
    _mod.TASK_ID = task_id_env

    # Seed the server with the bare-name forms the guard checks.
    tools = [
        {"name": "update_status", "action": "task_status_update"},
        {"name": "move_task", "action": "move_task"},
        {"name": "archive_task", "action": "move_task"},
        {"name": "add_activity", "action": "add_activity"},
        {"name": "create_task", "action": "create_task"},
        {"name": "update_task", "action": "update_task"},
    ]
    server = _mod.MCPServer(tools)
    try:
        return asyncio.run(server._execute_tool(tool_name, arguments))
    finally:
        _mod.TASK_MODE = original_mode
        _mod.TASK_ID = original_task


def test_triage_blocks_update_status_on_current_task():
    """In triage mode, update_status is disabled regardless of the
    target task id — the MA must never call it. Without this guard
    the playbook's 'NEVER call move_task(blocked → ready)' rule
    relied on prompt compliance alone."""
    result = _run_triage(
        "update_status", {"task_id": "current-task", "new_status": "review"},
    )
    assert result.get("isError") is True
    text = result["content"][0]["text"]
    assert "update_status is disabled" in text


def test_triage_blocks_move_task_on_current_task():
    """move_task on the CURRENT blocked task is refused — pre-flipping
    the task into ready is exactly the loop the C3 fix breaks."""
    result = _run_triage(
        "move_task", {"task_id": "current-task", "new_status": "ready"},
    )
    assert result.get("isError") is True
    assert "disabled in triage mode" in result["content"][0]["text"]


def test_triage_allows_create_task_for_helper():
    """The MA's path C (helper-task with depends_on) requires
    create_task on OTHER tasks — the triage guard must NOT block it.
    Test passes if the call goes through the guard and hits the
    backend call path (which fails on the mock env, but that's
    downstream of the guard)."""
    # The guard returns None when it lets the call through; the
    # call then proceeds to the backend (which will error in a
    # bare test env). We assert specifically that the error text
    # is NOT the guard's text, i.e. the guard let the call pass.
    result = _run_triage(
        "create_task", {"workstream_id": "ws-1", "title": "Refresh API key"},
    )
    # Either the call succeeded (unlikely in test env) or it failed
    # downstream of the guard. Either way, the guard's specific
    # rejection text must NOT appear.
    if result.get("isError"):
        text = result["content"][0]["text"]
        assert "disabled in triage mode" not in text
        assert "update_status is disabled" not in text


def test_triage_allows_move_task_on_different_task():
    """If the MA needs to move a DIFFERENT task (e.g. archive a
    stale helper), the guard must permit it. Only the CURRENT
    blocked task is protected."""
    result = _run_triage(
        "move_task",
        {"task_id": "different-task-id", "new_status": "archived"},
        task_id_env="current-task",
    )
    if result.get("isError"):
        text = result["content"][0]["text"]
        # Must NOT match the triage-block message.
        assert "disabled in triage mode" not in text


def test_output_dir_rejects_path_traversal_in_segments():
    """QA round 8 B2: defence in depth. Today's backend generates
    deterministic, safe segment values (``WR``, ``WR-003.S01``), but
    a future producer that bypasses that contract must NOT be able
    to escape ``/workspace/outputs/`` via ``..`` or path separators
    in the env var. Falls back to the safe parent (workstream-only
    when scope is unsafe, or the flat root when workstream is
    unsafe)."""
    # ``..`` in workstream → fall back to flat root.
    assert (
        compute_output_dir("..", "WR-003.S01")
        == "/workspace/outputs"
    )
    assert (
        compute_output_dir("etc/passwd", "")
        == "/workspace/outputs"
    )
    # ``..`` in scope → fall back to workstream-only.
    assert (
        compute_output_dir("WR", "../escape")
        == "/workspace/outputs/WR"
    )
    assert (
        compute_output_dir("WR", "WR/../etc")
        == "/workspace/outputs/WR"
    )
    # Backslash (Windows-style) is also rejected.
    assert (
        compute_output_dir("WR", "evil\\path")
        == "/workspace/outputs/WR"
    )
