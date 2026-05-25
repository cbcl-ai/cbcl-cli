"""Phase 1.5: unit coverage for ``handle_script_variable_binding_set``.

The dispatch handler glues three components:

  * ``normalise_binding`` — shape vetting (already covered in
    test_variable_bindings.py).
  * ``VariableManager.set_binding`` — persistence.
  * ``SecretsStore.delete_script_secret`` — stale-entry cleanup
    when the new binding is ``office_secret``.

This test exercises the full handler path with a real (temp-dir)
VariableManager + SecretsStore so the side-effect contract on
disk is locked down: rebinding to office_secret drops the stale
``.secrets.json`` entry; rebinding to literal does NOT touch
``.secrets.json``; ``binding=None`` removes the entry from
``variables.json``; malformed payloads are dropped silently.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.dispatch import handle_script_variable_binding_set
from src.scripts.secrets_store import SecretsStore
from src.scripts.variable_manager import VariableManager


def _setup(tmp_path: Path) -> tuple[VariableManager, SecretsStore, Path]:
    """Build a VariableManager + SecretsStore rooted at tmp_path.

    Returns the (vm, secrets, script_dir) triple — script_dir is
    pre-created so the tests don't need to assert on directory
    creation behaviour (covered in test_variable_bindings.py).
    """
    script_dir = tmp_path / ".scripts" / "demo"
    script_dir.mkdir(parents=True)
    vm = VariableManager(str(tmp_path))
    # SecretsStore uses workspace (== tmp_path) for script secrets.
    secrets = SecretsStore(str(tmp_path))
    return vm, secrets, script_dir


# ── happy paths ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_literal_binding_persisted(tmp_path):
    vm, secrets, script_dir = _setup(tmp_path)
    await handle_script_variable_binding_set(
        {
            "type": "script_variable_binding_set",
            "script_name": "demo",
            "variable_name": "QUERY",
            "binding": {"kind": "literal", "value": "python devs"},
        },
        vm, secrets,
    )
    on_disk = json.loads((script_dir / "variables.json").read_text())
    assert on_disk == {
        "QUERY": {"kind": "literal", "value": "python devs"},
    }


@pytest.mark.asyncio
async def test_office_secret_binding_persisted(tmp_path):
    vm, secrets, script_dir = _setup(tmp_path)
    await handle_script_variable_binding_set(
        {
            "type": "script_variable_binding_set",
            "script_name": "demo",
            "variable_name": "API_KEY",
            "binding": {"kind": "office_secret", "ref": "OPENAI_API_KEY"},
        },
        vm, secrets,
    )
    on_disk = json.loads((script_dir / "variables.json").read_text())
    assert on_disk == {
        "API_KEY": {"kind": "office_secret", "ref": "OPENAI_API_KEY"},
    }


# ── stale-secret cleanup on office_secret rebind ───────────────


@pytest.mark.asyncio
async def test_rebind_to_office_secret_drops_stale_literal_secret(tmp_path):
    """The bug the handler exists to prevent: a user previously set
    a literal value for a secret variable (lands in .secrets.json),
    then switches to an Office Secret binding via the UI. Without
    cleanup, the .secrets.json entry would still be there and
    confuse a future toggle-back. The handler must drop it."""
    vm, secrets, script_dir = _setup(tmp_path)
    # Pre-populate .secrets.json with a literal secret value.
    secrets.set_script_secret("demo", "API_KEY", "literal-token-abc")
    secrets_path = script_dir / ".secrets.json"
    assert json.loads(secrets_path.read_text()) == {"API_KEY": "literal-token-abc"}

    # Now rebind to office_secret. Handler must drop the .secrets.json entry.
    await handle_script_variable_binding_set(
        {
            "type": "script_variable_binding_set",
            "script_name": "demo",
            "variable_name": "API_KEY",
            "binding": {"kind": "office_secret", "ref": "REAL_KEY"},
        },
        vm, secrets,
    )
    # Binding lands in variables.json.
    bindings_on_disk = json.loads((script_dir / "variables.json").read_text())
    assert bindings_on_disk == {
        "API_KEY": {"kind": "office_secret", "ref": "REAL_KEY"},
    }
    # And the stale entry is gone from .secrets.json.
    assert json.loads(secrets_path.read_text()) == {}


@pytest.mark.asyncio
async def test_rebind_to_literal_does_not_touch_secrets_json(tmp_path):
    """Inverse of the office_secret-rebind case: when the new
    binding is a literal, we should NOT clear .secrets.json. The
    literal binding lives in variables.json and ranks ahead of
    .secrets.json in env_from anyway, but the cleanup-on-rebind
    path is specific to the office_secret → ??? direction. Verify
    the literal direction leaves the secrets file alone."""
    vm, secrets, script_dir = _setup(tmp_path)
    # Different variable's secret should never be touched.
    secrets.set_script_secret("demo", "OTHER_TOKEN", "preserve-me")
    secrets_path = script_dir / ".secrets.json"

    await handle_script_variable_binding_set(
        {
            "type": "script_variable_binding_set",
            "script_name": "demo",
            "variable_name": "QUERY",
            "binding": {"kind": "literal", "value": "search-string"},
        },
        vm, secrets,
    )
    # OTHER_TOKEN preserved.
    assert json.loads(secrets_path.read_text()) == {
        "OTHER_TOKEN": "preserve-me",
    }


# ── binding=None clears the entry ─────────────────────────────


@pytest.mark.asyncio
async def test_null_binding_deletes_entry(tmp_path):
    vm, secrets, script_dir = _setup(tmp_path)
    # Pre-populate two bindings; the handler should drop only the
    # targeted one.
    (script_dir / "variables.json").write_text(json.dumps({
        "DOOMED": {"kind": "literal", "value": "x"},
        "KEEPER": {"kind": "literal", "value": "y"},
    }))
    await handle_script_variable_binding_set(
        {
            "type": "script_variable_binding_set",
            "script_name": "demo",
            "variable_name": "DOOMED",
            "binding": None,
        },
        vm, secrets,
    )
    on_disk = json.loads((script_dir / "variables.json").read_text())
    assert on_disk == {"KEEPER": {"kind": "literal", "value": "y"}}


@pytest.mark.asyncio
async def test_null_binding_clear_on_empty_store_is_noop(tmp_path):
    """A clear on a variable that doesn't have a binding yet is a
    no-op — the file may be created empty (which the writer does as
    a side effect of touching it) but no error is raised."""
    vm, secrets, _ = _setup(tmp_path)
    # Should not raise.
    await handle_script_variable_binding_set(
        {
            "type": "script_variable_binding_set",
            "script_name": "demo",
            "variable_name": "NEVER_SET",
            "binding": None,
        },
        vm, secrets,
    )


# ── malformed payloads dropped silently ────────────────────────


@pytest.mark.asyncio
async def test_missing_script_name_dropped(tmp_path):
    """Defensive: the sanitiser is supposed to catch this, but the
    handler also guards. A missing script_name must NOT raise — it
    must log and return so the consumer's reader loop keeps
    processing subsequent messages."""
    vm, secrets, script_dir = _setup(tmp_path)
    await handle_script_variable_binding_set(
        {
            "type": "script_variable_binding_set",
            "variable_name": "X",
            "binding": {"kind": "literal", "value": "v"},
        },
        vm, secrets,
    )
    # No file written.
    assert not (script_dir / "variables.json").exists()


@pytest.mark.asyncio
async def test_malformed_binding_dropped(tmp_path):
    vm, secrets, script_dir = _setup(tmp_path)
    await handle_script_variable_binding_set(
        {
            "type": "script_variable_binding_set",
            "script_name": "demo",
            "variable_name": "X",
            "binding": {"kind": "magic", "value": "x"},
        },
        vm, secrets,
    )
    assert not (script_dir / "variables.json").exists()


@pytest.mark.asyncio
async def test_unknown_kind_does_not_drop_existing_bindings(tmp_path):
    """Defense in depth: an unknown kind is dropped, but it must
    NOT also wipe other valid bindings in the same file. Verifies
    the handler short-circuits before any write."""
    vm, secrets, script_dir = _setup(tmp_path)
    (script_dir / "variables.json").write_text(json.dumps({
        "PRESERVE": {"kind": "literal", "value": "important"},
    }))
    await handle_script_variable_binding_set(
        {
            "type": "script_variable_binding_set",
            "script_name": "demo",
            "variable_name": "X",
            "binding": {"kind": "magic", "value": "x"},
        },
        vm, secrets,
    )
    on_disk = json.loads((script_dir / "variables.json").read_text())
    assert on_disk == {"PRESERVE": {"kind": "literal", "value": "important"}}
