"""Environment variable names that change how the Claude CLI signs in.

Cubicle is subscription-only: every Claude session runs on the office's own
Claude subscription login (``/home/agent/.claude/.credentials.json``). The
Claude CLI reads these names before that login. An API key or bearer token
bills the Anthropic API instead, a base URL or a Bedrock/Vertex/Foundry
switch sends requests to another provider, an OAuth token signs in as
another account, and ``CLAUDE_CONFIG_DIR`` reads another credential
directory. The ``ANTHROPIC_`` and ``CLAUDE_CODE_`` prefixes also cover names
later CLI releases add.

One definition on both sides of the container boundary, in this pure module:
the daemon imports it through ``src/claude_auth_env.py``, and the in-container
script executor (``_mcp_script_exec``) refuses a script that declares or binds
such a name. The backend refuses office-secret and skill-secret names in this
set (``backend/app/office_secrets/schemas.py`` holds the matching rule;
``tests/test_reserved_claude_env_parity.py`` pins the two together).
"""

from __future__ import annotations

RESERVED_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_")
RESERVED_NAMES = frozenset({"CLAUDE_CONFIG_DIR"})

# Why such a name is refused, for the teaching errors that refuse one.
RESERVED_REASON = (
    "names starting with ANTHROPIC_ or CLAUDE_CODE_, and CLAUDE_CONFIG_DIR, "
    "change how Claude signs in or which account is billed, and Cubicle runs "
    "on the office's Claude subscription login only"
)


def is_reserved_claude_env_name(name: str) -> bool:
    """True when ``name`` (any case) would change Claude's sign-in or provider."""
    upper = name.upper()
    return upper in RESERVED_NAMES or upper.startswith(RESERVED_PREFIXES)
