"""Pin the Manager's ``--disallowed-tools`` list.

The AI Manager is a pure orchestrator: it MUST NEVER spawn subagents or
run shell work directly (the sole-orchestrator invariant). ``--disallowed-tools``
is the only enforcement that Claude CLI actually honours — the system
prompt + manager-spec.md merely reinforce it. Claude CLI v2.1.63 renamed
the subagent-spawn tool ``Task`` → ``Agent``; a list that only blocks
``Task`` would silently let the Manager spawn subagents on a newer CLI.
This test fails CI if either name (or ``Bash``) is dropped from the list.
"""
from __future__ import annotations

from src._agent_worker_manager import (
    MANAGER_DISALLOWED_TOOLS,
    MANAGER_ENV_OVERRIDES,
)
from src._session_policy import _SUBAGENT_TOOLS


def test_manager_blocks_bash_and_both_subagent_tool_names() -> None:
    # Shell access — the Manager never executes work directly.
    assert "Bash" in MANAGER_DISALLOWED_TOOLS
    # Subagent spawn — legacy ``Task`` AND the renamed ``Agent``.
    assert "Task" in MANAGER_DISALLOWED_TOOLS
    assert "Agent" in MANAGER_DISALLOWED_TOOLS


def test_manager_disallow_covers_every_subagent_tool() -> None:
    # If the CLI renames the tool again, the shared tuple is the single
    # source of truth — the Manager list must stay a superset of it.
    for tool in _SUBAGENT_TOOLS:
        assert tool in MANAGER_DISALLOWED_TOOLS


def test_manager_disallow_is_a_plain_string_list() -> None:
    # ``stream_cli_session`` forwards this straight to the CLI argv, so it
    # must be a flat list of strings (no tuples / None leaking through).
    assert isinstance(MANAGER_DISALLOWED_TOOLS, list)
    assert all(isinstance(t, str) and t for t in MANAGER_DISALLOWED_TOOLS)


def test_manager_env_hard_disables_dynamic_workflows() -> None:
    # Second-layer I1 guard: the Manager session always runs with dynamic
    # workflows hard-disabled via env, regardless of any agent's effort.
    assert MANAGER_ENV_OVERRIDES == {"CLAUDE_CODE_DISABLE_WORKFLOWS": "1"}


def test_manager_runner_passes_disallow_and_env_to_cli() -> None:
    # Pin that the Manager runner actually wires BOTH guards into the CLI
    # call (not just defines the constants). The source must pass
    # disallowed_tools=MANAGER_DISALLOWED_TOOLS and
    # env_overrides=MANAGER_ENV_OVERRIDES to stream_cli_session.
    import inspect
    import src._agent_worker_manager as mgr

    src = inspect.getsource(mgr)
    assert "disallowed_tools=MANAGER_DISALLOWED_TOOLS" in src
    assert "env_overrides=MANAGER_ENV_OVERRIDES" in src
