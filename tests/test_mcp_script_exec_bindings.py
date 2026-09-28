"""NEW-1 regression: the in-container ``execute_script`` env build MUST
resolve ``variables.json`` BINDINGS to their literal values before
injecting them as the script subprocess env.

Under Phase 1.5 the Variables UI writes bindings, not bare values, into
``variables.json``:

    {"COUNT": {"kind": "literal", "value": 100},
     "API_KEY": {"kind": "office_secret", "ref": "UNIPILE_API_KEY"}}

The old code stringified the raw binding object, so an agent-triggered run
saw ``os.environ["COUNT"] == "{'kind': 'literal', 'value': 100}"`` instead
of ``"100"`` — silently corrupting every literal-bound variable on the
primary agent path. ``_binding_literal_value`` is the inlined mirror of the
host-side ``variable_manager.normalise_binding`` resolution (``src.scripts``
is not importable inside the agent image), so it is unit-tested here.

The module imports ``_mcp_backend`` as a sibling (only resolvable in the
agent image's ``/opt/cubicle/`` flat layout), so we stub that module and
put ``_agent_image`` on ``sys.path`` to import the real helper.
"""
from __future__ import annotations

import pytest

from tests.agent_image_stub import stubbed_mcp_script_exec


@pytest.fixture(scope="module")
def mcp_script_exec():
    """Import the real ``_mcp_script_exec`` module with a stubbed
    ``_mcp_backend`` sibling so the top-level import succeeds outside the
    container."""
    with stubbed_mcp_script_exec() as module:
        yield module


def test_literal_binding_resolves_to_value(mcp_script_exec) -> None:
    """The new binding shape → its embedded value (NEW-1 core)."""
    assert mcp_script_exec._binding_literal_value(
        {"kind": "literal", "value": 100}
    ) == 100
    assert mcp_script_exec._binding_literal_value(
        {"kind": "literal", "value": "Python dev"}
    ) == "Python dev"
    assert mcp_script_exec._binding_literal_value(
        {"kind": "literal", "value": True}
    ) is True


def test_office_secret_binding_yields_no_local_value(mcp_script_exec) -> None:
    """Office-secret bindings carry only a NAME — no local literal; the
    value is injected host-side via docker exec -e. Must be skipped."""
    result = mcp_script_exec._binding_literal_value(
        {"kind": "office_secret", "ref": "UNIPILE_API_KEY"}
    )
    assert result is mcp_script_exec._NO_LOCAL_VALUE


def test_bare_scalar_is_accepted_legacy_shape(mcp_script_exec) -> None:
    """Legacy / hand-edited variables.json uses bare values; still read."""
    assert mcp_script_exec._binding_literal_value(100) == 100
    assert mcp_script_exec._binding_literal_value("hello") == "hello"
    assert mcp_script_exec._binding_literal_value(False) is False
    assert mcp_script_exec._binding_literal_value(3.5) == 3.5


def test_malformed_entries_yield_no_local_value(mcp_script_exec) -> None:
    """Unknown kinds, missing value, lists, and null all skip cleanly."""
    sentinel = mcp_script_exec._NO_LOCAL_VALUE
    assert mcp_script_exec._binding_literal_value({"kind": "literal"}) is sentinel
    assert mcp_script_exec._binding_literal_value({"kind": "bogus"}) is sentinel
    assert mcp_script_exec._binding_literal_value({}) is sentinel
    assert mcp_script_exec._binding_literal_value([1, 2, 3]) is sentinel
    assert mcp_script_exec._binding_literal_value(None) is sentinel


def test_stringify_of_resolved_literal_matches_host(mcp_script_exec) -> None:
    """End-to-end: resolve THEN stringify gives the wire value the child
    sees — not the stringified binding dict (the actual NEW-1 bug)."""
    raw = {"kind": "literal", "value": 100}
    resolved = mcp_script_exec._binding_literal_value(raw)
    assert mcp_script_exec._stringify_env_value(resolved) == "100"
    # The bug produced this instead:
    assert mcp_script_exec._stringify_env_value(raw) != "100"
