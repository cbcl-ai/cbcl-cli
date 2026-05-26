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
    """The exact ``claude mcp add --scope user perplexity -e KEY=VAL -- npx -y server`` shape.

    Arg order is LOAD-BEARING — ``-e / --env`` is a Commander
    variadic that consumes positional args until the next flag. If
    env flags came BEFORE the name, claude would parse the name
    itself as an env-var entry and exit 1. The 0.2.18 fix moved
    env flags to AFTER the name; the v0.2.16/0.2.17 chase happened
    because we were debugging the SYMPTOMS (UI missing entries)
    instead of the actual root cause (every stdio add was silently
    failing).
    """
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
        "perplexity",
        "-e", "PERPLEXITY_API_KEY=sk-xxx",
        "--",
        "npx",
        "-y",
        "@perplexity-ai/mcp-server",
    ]


def test_env_flags_come_after_name_not_before():
    """Regression guard for the 0.2.18 fix.

    If a future refactor moves the env-flag loop BEFORE the
    ``name`` token (the pre-0.2.18 shape), every stdio add will
    silently fail with claude's ``Invalid environment variable
    format: <name>`` error and the UI will go quiet again. Lock
    the relative order in place.
    """
    argv = _build_stdio_argv(
        container_name="ctr",
        name="perplexity",
        command="npx",
        args=["pkg"],
        env_vars=[{"name": "API_KEY", "value": "v"}],
    )
    name_idx = argv.index("perplexity")
    first_env_flag_idx = argv.index("-e")
    assert name_idx < first_env_flag_idx, (
        "env flags MUST come after the name; otherwise claude's "
        "variadic -e consumes the name and add fails silently"
    )


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
    """Two ``-e K=V`` pairs both make it through, in input order.

    Anchor on the first ``-e`` rather than slice indices so a
    future flag-order tweak doesn't silently break the assertion.
    """
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
    first_env = argv.index("-e")
    assert argv[first_env:first_env + 4] == [
        "-e", "A_KEY=v1", "-e", "B_KEY=v2",
    ]


def test_env_value_can_contain_shell_metacharacters_safely():
    """Env VALUES can be anything — they're passed via argv to
    subprocess.run (no shell), so shell metacharacters become
    literal bytes of the ``-e KEY=VAL`` arg."""
    argv = _build_stdio_argv(
        container_name="ctr",
        name="x",
        command="npx",
        args=[],
        env_vars=[{"name": "DANGEROUS", "value": "; rm -rf /"}],
    )
    # The metacharacters survive AS LITERAL CHARS in argv. subprocess
    # won't reinterpret them because shell=False is the default.
    assert "-e" in argv
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
    backend's Pydantic-layer constants. Previously this test tried
    to ``import app.connectors.router`` which only works in the
    monorepo backend container — so the test silently SKIPPED in
    the standalone cbcl-cli test environment AND in the daemon's
    own dev container. Useless safety net.

    Round-5 fix: text-level constant extraction. Read both source
    files (daemon at ``src/_handlers/_mcp.py``, backend at
    ``../../backend/app/connectors/router.py``) and parse the
    constant definitions via regex. Works in ANY Python env that
    has the daemon source on disk — and the daemon source always
    has the backend tree as a sibling when this is checked-out as
    a monorepo. In the public cbcl-cli standalone repo there's no
    backend tree, so the test skips (real skip — not a silent
    import failure).
    """
    import os
    import re

    daemon_src_path = os.path.join(
        os.path.dirname(__file__), "..", "src", "_handlers", "_mcp.py",
    )
    backend_src_path = os.path.join(
        os.path.dirname(__file__),
        "..", "..", "backend", "app", "connectors", "router.py",
    )
    if not os.path.isfile(backend_src_path):
        pytest.skip("backend tree not present (cbcl-cli standalone)")

    daemon_src = open(daemon_src_path).read()
    backend_src = open(backend_src_path).read()

    def grep_first(pattern: str, src: str) -> str | None:
        m = re.search(pattern, src)
        return m.group(1) if m else None

    # Allowlist — set literal on a single line.
    dm_allow_raw = grep_first(
        r"_STDIO_COMMAND_ALLOWLIST: set\[str\] = \{([^}]+)\}", daemon_src,
    )
    be_allow_raw = grep_first(
        r"STDIO_COMMAND_ALLOWLIST: set\[str\] = \{([^}]+)\}", backend_src,
    )
    assert dm_allow_raw is not None, "daemon allowlist not found"
    assert be_allow_raw is not None, "backend allowlist not found"
    norm = lambda s: {  # noqa: E731
        t.strip().strip('"').strip("'")
        for t in s.split(",") if t.strip()
    }
    assert norm(dm_allow_raw) == norm(be_allow_raw), "command allowlist drift"

    # Regex constants — ``re.compile(r"...")`` form.
    def regex_pattern(name: str, src: str) -> str | None:
        return grep_first(rf"{name} = re\.compile\(r\"([^\"]+)\"\)", src)

    pairs = [
        ("_SAFE_STDIO_ARG_RE", "SAFE_STDIO_ARG_RE"),
        ("_ENV_VAR_NAME_RE", "ENV_VAR_NAME_RE"),
        ("_MCP_NAME_RE", "MCP_NAME_RE"),
    ]
    for daemon_name, backend_name in pairs:
        dm = regex_pattern(daemon_name, daemon_src)
        be = regex_pattern(backend_name, backend_src)
        assert dm is not None, f"daemon {daemon_name} not found"
        assert be is not None, f"backend {backend_name} not found"
        assert dm == be, (
            f"regex drift: daemon {daemon_name}={dm!r} vs "
            f"backend {backend_name}={be!r}"
        )

    # Integer constants — ``name = 512`` form.
    def int_const(name: str, src: str) -> int | None:
        v = grep_first(rf"{name} = (\d+)", src)
        return int(v) if v else None

    assert int_const("_SAFE_STDIO_ARG_MAX_LEN", daemon_src) == int_const(
        "SAFE_STDIO_ARG_MAX_LEN", backend_src,
    ), "arg max len drift"

    # Daemon-only caps — backend uses Pydantic Field(max_length=...),
    # daemon uses named constants. Hard-code the expected values so
    # a one-sided change to either fails the test.
    assert int_const("_STDIO_ARGS_MAX", daemon_src) == 64, (
        "daemon _STDIO_ARGS_MAX must match backend "
        "McpAddRequest.args max_length=64"
    )
    assert int_const("_STDIO_ENV_VARS_MAX", daemon_src) == 32, (
        "daemon _STDIO_ENV_VARS_MAX must match backend "
        "McpAddRequest.env_vars max_length=32"
    )
    # Spot-check the backend literals are what we assume.
    assert "max_length=64" in backend_src
    assert "max_length=32" in backend_src


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
    # Sanity: the name appears as a positional arg (between
    # ``--scope user`` and any ``-e`` flag — see the 0.2.18 arg-
    # order regression test for why).
    assert name in argv
    name_idx = argv.index(name)
    assert argv[name_idx - 2:name_idx] == ["--scope", "user"]
    # Sanity: the command is right after --.
    assert argv[argv.index("--") + 1] == command
