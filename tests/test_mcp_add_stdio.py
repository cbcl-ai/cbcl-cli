"""Tests for the stdio branch of ``_handlers._mcp.run_mcp_add``.

The backend's Pydantic ``McpAddRequest`` is the primary security
gate (refuses shell injection, command outside allowlist, etc.).
The ``_build_stdio_argv`` helper here is defence-in-depth — it
re-validates so a buggy / bypassed backend can't push an unsafe
command into the daemon's ``subprocess.run``.

These tests lock the argv-construction contract: the EXACT command
shape ``claude mcp add --scope user [--env K=V ...] <name> --
<command> [<args>...]`` so a refactor that reorders flags doesn't
silently break the Perplexity-style install.
"""
from __future__ import annotations

import pytest

from src._handlers._mcp import _build_stdio_argv


# ── Happy paths ────────────────────────────────────────────────────


def test_perplexity_shape():
    """The exact ``claude mcp add perplexity --env PERPLEXITY_API_KEY=... -- npx -y @perplexity-ai/mcp-server`` shape."""
    argv = _build_stdio_argv(
        container_name="cbcl-office-dev",
        name="perplexity",
        command="npx",
        args=["-y", "@perplexity-ai/mcp-server"],
        env_vars=[{"name": "PERPLEXITY_API_KEY", "value": "sk-xxx"}],
    )
    assert argv == [
        "docker", "exec", "cbcl-office-dev",
        "claude", "mcp", "add", "--scope", "user",
        "--env", "PERPLEXITY_API_KEY=sk-xxx",
        "perplexity",
        "--",
        "npx",
        "-y",
        "@perplexity-ai/mcp-server",
    ]


def test_no_args_no_env():
    """Plain ``deno run server.ts`` — no args means just ``--`` then
    the bare command. (Args list is empty but the ``--`` MUST still
    appear so a later refactor that adds args doesn't have to
    remember to inject the separator.)"""
    argv = _build_stdio_argv(
        container_name="ctr",
        name="bare",
        command="deno",
        args=[],
        env_vars=[],
    )
    assert argv == [
        "docker", "exec", "ctr",
        "claude", "mcp", "add", "--scope", "user",
        "bare",
        "--",
        "deno",
    ]


def test_multiple_env_vars_preserve_order():
    """Two ``--env K=V`` pairs both make it through, in input order.

    Locate the env section by name-anchor rather than hard-coded
    slice indices — keeps the test stable if the preamble changes
    (e.g. an extra ``--scope`` flag is added before --env)."""
    argv = _build_stdio_argv(
        container_name="ctr",
        name="x",
        command="npx",
        args=["pkg"],
        env_vars=[
            {"name": "A_KEY", "value": "v1"},
            {"name": "B_KEY", "value": "v2"},
        ],
    )
    # First --env appears once; locate it, then check the next 4
    # tokens are the two env pairs in order.
    first_env = argv.index("--env")
    assert argv[first_env:first_env + 4] == [
        "--env", "A_KEY=v1", "--env", "B_KEY=v2",
    ]


def test_env_value_can_contain_shell_metacharacters_safely():
    """Env VALUES can be anything — they're passed via argv to
    subprocess.run (no shell), so shell metacharacters become
    literal bytes of the ``--env KEY=VAL`` arg."""
    argv = _build_stdio_argv(
        container_name="ctr",
        name="x",
        command="npx",
        args=[],
        env_vars=[{"name": "DANGEROUS", "value": "; rm -rf /"}],
    )
    # The metacharacters survive AS LITERAL CHARS in argv. subprocess
    # won't reinterpret them because shell=False is the default.
    assert "--env" in argv
    assert "DANGEROUS=; rm -rf /" in argv


# ── Defence-in-depth refusals ─────────────────────────────────────


def test_refuses_command_outside_allowlist():
    """Even ``bash`` is refused — backend Pydantic refuses too, but
    the daemon doesn't trust the backend's validation."""
    argv = _build_stdio_argv(
        container_name="ctr",
        name="x",
        command="bash",
        args=["-c", "whoami"],
        env_vars=[],
    )
    assert argv is None


def test_refuses_shell_metacharacters_in_args():
    """Backend rejected these via Pydantic; we re-validate here so a
    bypassed backend or a future code change that drops the Pydantic
    guard can't reach subprocess.run with unsafe args."""
    for bad in [
        "foo; rm -rf /",
        "$(whoami)",
        "`whoami`",
        "foo|bar",
        "foo&bar",
        "foo>out.txt",
        "foo*",
        "with space",
    ]:
        argv = _build_stdio_argv(
            container_name="ctr",
            name="x",
            command="npx",
            args=[bad],
            env_vars=[],
        )
        assert argv is None, (
            f"injection arg {bad!r} should be refused by daemon-side "
            "validation"
        )


def test_refuses_invalid_env_var_name():
    """Lowercase / hyphenated / starts-with-digit / shell-metachar
    env-var names all rejected."""
    for bad_name in ["lowercase", "Mixed", "1STARTS", "HAS-DASH", ""]:
        argv = _build_stdio_argv(
            container_name="ctr",
            name="x",
            command="npx",
            args=[],
            env_vars=[{"name": bad_name, "value": "v"}],
        )
        assert argv is None, f"env name {bad_name!r} should refuse"


def test_refuses_duplicate_env_var_names():
    """Same name twice could silently overwrite — refuse."""
    argv = _build_stdio_argv(
        container_name="ctr",
        name="x",
        command="npx",
        args=[],
        env_vars=[
            {"name": "API_KEY", "value": "v1"},
            {"name": "API_KEY", "value": "v2"},
        ],
    )
    assert argv is None


def test_refuses_oversized_arg():
    """An arg over the 512-char cap is refused. Caps the per-arg
    payload so a malicious payload can't bloat the ``docker exec``
    argv past kernel ARG_MAX."""
    argv = _build_stdio_argv(
        container_name="ctr",
        name="x",
        command="npx",
        args=["A" * 513],
        env_vars=[],
    )
    assert argv is None


def test_refuses_non_string_arg():
    """Type guard for buggy producers that pass ints / dicts."""
    argv = _build_stdio_argv(
        container_name="ctr",
        name="x",
        command="npx",
        args=[42, "ok"],  # type: ignore[list-item]
        env_vars=[],
    )
    assert argv is None


# ── Real-world MCP packages ───────────────────────────────────────


@pytest.mark.parametrize(
    "name,command,args,env_vars",
    [
        (
            "perplexity",
            "npx",
            ["-y", "@perplexity-ai/mcp-server"],
            [{"name": "PERPLEXITY_API_KEY", "value": "sk-p"}],
        ),
        (
            "brave-search",
            "npx",
            ["-y", "@modelcontextprotocol/server-brave-search"],
            [{"name": "BRAVE_API_KEY", "value": "sk-b"}],
        ),
        (
            "github",
            "npx",
            ["-y", "@modelcontextprotocol/server-github"],
            [{"name": "GITHUB_PERSONAL_ACCESS_TOKEN", "value": "ghp_x"}],
        ),
        (
            "python-mcp",
            "uvx",
            ["mcp-server-time"],
            [],
        ),
    ],
)
def test_canonical_npm_pip_mcps_build_cleanly(
    name, command, args, env_vars,
):
    """The four most-common MCP-server-install shapes all build a
    valid argv. Locking these in catches a future validator change
    that would accidentally refuse a real package name."""
    argv = _build_stdio_argv(
        container_name="ctr",
        name=name,
        command=command,
        args=args,
        env_vars=env_vars,
    )
    assert argv is not None, f"{name} should build cleanly"
    # Sanity: the name appears as a positional arg before --.
    assert argv[argv.index("--") - 1] == name
    # Sanity: the command is right after --.
    assert argv[argv.index("--") + 1] == command
