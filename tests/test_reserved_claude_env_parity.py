"""The daemon and the backend reserve the same Claude sign-in names.

The backend refuses office-secret, skill-secret and script-binding names with
``app/office_secrets/schemas.reserved_claude_env_violation``; the daemon and
the in-container script executor refuse or drop them with
``_agent_image/_mcp/claude_auth_env.is_reserved_claude_env_name`` (re-exported
by ``src/claude_auth_env``). The two rules are separate copies on either side
of the platform boundary, so a prefix added to one side only would let a name
through the other. These tests pin them together (item 39). In a standalone
CLI checkout the backend comparisons skip.

The backend strips surrounding whitespace and the daemon does not. That is
harmless, because stored names match ``^[A-Z][A-Z0-9_]{0,63}$``, so the
corpus holds no padded names.
"""

from __future__ import annotations

import pytest

from src import claude_auth_env
from tests import backend_boundary

NAMES = [
    # Reserved: the known sign-in and provider switches, in any case.
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_",
    "anthropic_api_key",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "Claude_Code_Use_Vertex",
    "CLAUDE_CONFIG_DIR",
    "claude_config_dir",
    # Not reserved: other services' credentials and near misses.
    "OPENAI_API_KEY",
    "CLAUDE_API_TOKEN",
    "MY_ANTHROPIC_KEY",
    "ANTHROPIC",
    "ANTHROPICAPIKEY",
    "CLAUDE_CODE",
    "CLAUDE_CONFIG_DIRECTORY",
    "CLAUDE_CONFIG",
    "",
]


def _backend():
    return backend_boundary.import_backend("app.office_secrets.schemas")


def test_both_sides_reserve_the_same_prefixes_and_names():
    schemas = _backend()
    assert claude_auth_env.RESERVED_PREFIXES == schemas.RESERVED_CLAUDE_ENV_PREFIXES
    assert claude_auth_env.RESERVED_NAMES == schemas.RESERVED_CLAUDE_ENV_NAMES


@pytest.mark.parametrize("name", NAMES)
def test_both_sides_agree_on_each_name(name):
    schemas = _backend()
    daemon = claude_auth_env.is_reserved_claude_env_name(name)
    backend = schemas.reserved_claude_env_violation(name) is not None
    assert daemon == backend, name


def test_the_corpus_covers_both_outcomes():
    verdicts = {claude_auth_env.is_reserved_claude_env_name(name) for name in NAMES}
    assert verdicts == {True, False}


def test_the_daemon_rule_is_the_agent_image_rule():
    """One communicator definition: the daemon re-exports the image's module,
    which the in-container script executor imports."""
    from src._agent_image._mcp import claude_auth_env as image_rule

    assert claude_auth_env.is_reserved_claude_env_name is (
        image_rule.is_reserved_claude_env_name
    )
