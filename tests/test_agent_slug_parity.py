"""The daemon and the backend slugify a generated agent name identically.

The setup wizard's ``agent_slug`` (communicator) and apply-config's
``setup_content.generated_agent_slug`` (backend) both turn a generated
name into the agent-name rule. Apply writes the row directly (it never
passes through ``AgentCreate``), so the backend slugifier is the only gate
on that path: every non-empty result must also pass the REST rule
(``app.agents.schemas.is_valid_agent_name``, which refuses ``..``). In a
standalone CLI checkout (no ``backend/``) this module skips.
"""

from __future__ import annotations

import pytest

from src._setup_config_normalize import agent_slug
from tests.backend_boundary import import_backend

setup_content = import_backend("app.offices.setup_content")
agent_schemas = import_backend("app.agents.schemas")

CASES = [
    "Quote Builder!",
    "  --Data.Curator_2--  ",
    "Data..Curator",
    "a / .. / b",
    "Ops.. Lead",
    "...lead...",
    "a" * 300,
    "x." + "b" * 120,
    "!!!",
    "",
    "Research Specialist (EU)",
    "ÜberAgent",
    None,
    42,
]


@pytest.mark.parametrize("raw", CASES, ids=repr)
def test_daemon_and_backend_slug_the_same(raw: object) -> None:
    assert agent_slug(raw) == setup_content.generated_agent_slug(raw)


@pytest.mark.parametrize("raw", CASES, ids=repr)
def test_every_slug_passes_the_rest_rule(raw: object) -> None:
    slug = setup_content.generated_agent_slug(raw)
    if slug:
        assert agent_schemas.is_valid_agent_name(slug)
        assert agent_schemas._validate_agent_name(slug) == slug


def test_dot_runs_collapse_instead_of_producing_a_refused_name() -> None:
    assert agent_slug("Data..Curator") == "data.curator"
    assert agent_slug("a / .. / b") == "a-.-b"
