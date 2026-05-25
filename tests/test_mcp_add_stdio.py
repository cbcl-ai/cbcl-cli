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


def test_refuses_name_with_leading_dash():
    """``--scope`` as a name would be parsed as a flag by claude
    even though argv defeats shell injection. Daemon refuses too
    in case a payload bypassed the backend."""
    for bad in ["--scope", "-y", "--help"]:
        argv = _build_stdio_argv(
            container_name="ctr",
            name=bad,
            command="npx",
            args=[],
            env_vars=[],
        )
        assert argv is None, f"name {bad!r} (leading dash) should refuse"


def test_refuses_name_with_shell_meta_or_control():
    """Slashes / spaces / control chars in a name would corrupt
    ~/.claude.json (keyed by name)."""
    for bad in ["foo bar", "foo/bar", "foo;rm", "foo\n", "foo\x00"]:
        argv = _build_stdio_argv(
            container_name="ctr",
            name=bad,
            command="npx",
            args=[],
            env_vars=[],
        )
        assert argv is None


def test_refuses_too_many_args():
    """65 args refused; 64 accepted. The cap mirrors the backend."""
    argv = _build_stdio_argv(
        container_name="ctr",
        name="x",
        command="npx",
        args=["a"] * 65,
        env_vars=[],
    )
    assert argv is None
    # 64 should succeed
    argv64 = _build_stdio_argv(
        container_name="ctr",
        name="x",
        command="npx",
        args=["a"] * 64,
        env_vars=[],
    )
    assert argv64 is not None


def test_refuses_too_many_env_vars():
    """33 env vars refused; 32 accepted."""
    env33 = [{"name": f"K{i}", "value": "v"} for i in range(33)]
    assert _build_stdio_argv(
        container_name="ctr", name="x", command="npx",
        args=[], env_vars=env33,
    ) is None
    env32 = [{"name": f"K{i}", "value": "v"} for i in range(32)]
    assert _build_stdio_argv(
        container_name="ctr", name="x", command="npx",
        args=[], env_vars=env32,
    ) is not None


def test_refuses_env_value_with_forbidden_chars():
    """NUL crashes subprocess; CR/LF corrupts ~/.claude.json."""
    for bad in ["foo\x00", "foo\n", "foo\r"]:
        argv = _build_stdio_argv(
            container_name="ctr",
            name="x",
            command="npx",
            args=[],
            env_vars=[{"name": "API_KEY", "value": bad}],
        )
        assert argv is None, f"env value {bad!r} should refuse"


def test_scrub_env_values_redacts_flag_pairs():
    """The log-scrubber collapses ``--env KEY=SECRET`` to
    ``--env KEY=[REDACTED]`` so a future ``claude mcp add`` that
    echoes env flags doesn't leak secrets into the operator log."""
    from src._handlers._mcp import _scrub_env_values
    raw = "Added with --env API_KEY=sk-secret-xxx and other text"
    scrubbed = _scrub_env_values(raw)
    assert "sk-secret-xxx" not in scrubbed
    assert "API_KEY=[REDACTED]" in scrubbed
    # Multiple env flags all scrubbed
    raw2 = "--env A=1 --env BEE_KEY=hunter2"
    scrubbed2 = _scrub_env_values(raw2)
    assert "1" not in scrubbed2 or "[REDACTED]" in scrubbed2
    assert "hunter2" not in scrubbed2
    assert scrubbed2.count("[REDACTED]") == 2


def test_constants_lockstep_with_backend():
    """The daemon's defence-in-depth constants MUST match the
    backend's Pydantic-layer constants. Import both, assert
    equality. This catches a one-sided edit at CI time instead
    of in production.

    Skipped if the backend tree isn't reachable from this test
    environment (running the communicator test suite standalone,
    e.g. in the public cbcl-cli repo where the backend doesn't
    ship). The monorepo CI runs both side-by-side and the
    lockstep check fires there.
    """
    import sys
    import os
    backend_path = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__), "..", "..", "backend",
        ),
    )
    if not os.path.isdir(backend_path):
        pytest.skip("backend tree not present (cbcl-cli standalone)")
    sys.path.insert(0, backend_path)
    try:
        from app.connectors.router import (
            STDIO_COMMAND_ALLOWLIST as BE_ALLOW,
            SAFE_STDIO_ARG_RE as BE_ARG_RE,
            SAFE_STDIO_ARG_MAX_LEN as BE_ARG_MAX,
            ENV_VAR_NAME_RE as BE_ENV_RE,
            MCP_NAME_RE as BE_NAME_RE,
        )
    except ImportError:
        pytest.skip("backend imports not configured")
    from src._handlers._mcp import (
        _STDIO_COMMAND_ALLOWLIST as DM_ALLOW,
        _SAFE_STDIO_ARG_RE as DM_ARG_RE,
        _SAFE_STDIO_ARG_MAX_LEN as DM_ARG_MAX,
        _ENV_VAR_NAME_RE as DM_ENV_RE,
        _MCP_NAME_RE as DM_NAME_RE,
        _STDIO_ARGS_MAX as DM_ARGS_MAX,
        _STDIO_ENV_VARS_MAX as DM_ENVS_MAX,
    )
    assert BE_ALLOW == DM_ALLOW, "command allowlist drift"
    assert BE_ARG_RE.pattern == DM_ARG_RE.pattern, "arg regex drift"
    assert BE_ARG_MAX == DM_ARG_MAX, "arg max len drift"
    assert BE_ENV_RE.pattern == DM_ENV_RE.pattern, "env name regex drift"
    assert BE_NAME_RE.pattern == DM_NAME_RE.pattern, "mcp name regex drift"
    # Backend caps are on the Pydantic Field, daemon caps are
    # named constants — assert daemon matches the backend's
    # documented 64 / 32.
    assert DM_ARGS_MAX == 64
    assert DM_ENVS_MAX == 32


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
