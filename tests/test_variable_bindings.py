"""Tests for the Phase 1.5 variable binding helpers.

Covers ``normalise_binding``, ``resolve_binding``, and
``VariableManager.{get_bindings, set_binding}``. The manifest-side
resolution chain (precedence rules) is exercised in
``test_manifest.TestBindingResolution``; this module focuses on the
binding store itself.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.scripts.variable_manager import (
    VariableManager,
    normalise_binding,
    resolve_binding,
)


# ── normalise_binding ──────────────────────────────────────────────


class TestNormaliseBinding:

    def test_bare_string_becomes_literal(self):
        assert normalise_binding("hello") == {"kind": "literal", "value": "hello"}

    def test_bare_number_becomes_literal(self):
        assert normalise_binding(42) == {"kind": "literal", "value": 42}

    def test_bare_bool_becomes_literal(self):
        assert normalise_binding(True) == {"kind": "literal", "value": True}

    def test_none_returns_none(self):
        assert normalise_binding(None) is None

    def test_explicit_literal_passes_through(self):
        assert normalise_binding({"kind": "literal", "value": "x"}) == {
            "kind": "literal",
            "value": "x",
        }

    def test_literal_missing_value_rejected(self):
        # A literal binding with no ``value`` is malformed.
        # Rejecting it (rather than defaulting to None) makes the
        # configuration mistake visible to the user.
        assert normalise_binding({"kind": "literal"}) is None

    def test_explicit_office_secret_passes_through(self):
        assert normalise_binding({"kind": "office_secret", "ref": "OPENAI_API_KEY"}) == {
            "kind": "office_secret",
            "ref": "OPENAI_API_KEY",
        }

    def test_office_secret_lowercase_ref_rejected(self):
        # Same shape as the backend office_secrets router enforces.
        assert (
            normalise_binding({"kind": "office_secret", "ref": "lowercase_ref"})
            is None
        )

    def test_office_secret_empty_ref_rejected(self):
        assert (
            normalise_binding({"kind": "office_secret", "ref": ""}) is None
        )

    def test_unknown_kind_rejected(self):
        assert (
            normalise_binding({"kind": "random_thing", "value": "x"}) is None
        )

    def test_list_value_rejected(self):
        # A list as the binding (not as the wrapped value) is
        # malformed — would have been a copy-paste mistake.
        assert normalise_binding(["wrong", "shape"]) is None


# ── resolve_binding ────────────────────────────────────────────────


class TestResolveBinding:

    def test_literal_resolves_to_value(self):
        assert resolve_binding({"kind": "literal", "value": "hello"}) == (True, "hello")

    def test_office_secret_resolves_when_present(self):
        result = resolve_binding(
            {"kind": "office_secret", "ref": "OPENAI_API_KEY"},
            {"OPENAI_API_KEY": "sk-live-xxx"},
        )
        assert result == (True, "sk-live-xxx")

    def test_office_secret_missing_returns_not_found(self):
        # The (False, None) signal lets the caller fall through to
        # the next env source instead of silently using None.
        assert resolve_binding(
            {"kind": "office_secret", "ref": "MISSING"},
            {"OTHER_KEY": "v"},
        ) == (False, None)

    def test_office_secret_no_store_returns_not_found(self):
        assert resolve_binding(
            {"kind": "office_secret", "ref": "ANY"},
            None,
        ) == (False, None)

    def test_none_binding_returns_not_found(self):
        assert resolve_binding(None) == (False, None)


# ── VariableManager I/O ────────────────────────────────────────────


class TestVariableManager:

    def _setup(self, tmp_path: Path) -> tuple[VariableManager, Path]:
        """Build a VariableManager rooted at tmp_path + the script
        dir path it'll read/write."""
        script_dir = tmp_path / ".scripts" / "demo"
        script_dir.mkdir(parents=True)
        return VariableManager(str(tmp_path)), script_dir

    def test_get_bindings_empty_when_no_file(self, tmp_path):
        vm, _ = self._setup(tmp_path)
        assert vm.get_bindings("demo") == {}

    def test_get_bindings_normalises_legacy_bare_values(self, tmp_path):
        vm, script_dir = self._setup(tmp_path)
        (script_dir / "variables.json").write_text(json.dumps({
            "COUNT": 100,
            "QUERY": "python devs",
        }))
        bindings = vm.get_bindings("demo")
        assert bindings == {
            "COUNT": {"kind": "literal", "value": 100},
            "QUERY": {"kind": "literal", "value": "python devs"},
        }

    def test_get_bindings_drops_malformed_entries(self, tmp_path):
        vm, script_dir = self._setup(tmp_path)
        # A mix: one valid, one malformed. The malformed one should
        # be dropped and the valid one returned.
        (script_dir / "variables.json").write_text(json.dumps({
            "GOOD": {"kind": "literal", "value": "yes"},
            "BAD": {"kind": "wat", "value": "no"},
        }))
        bindings = vm.get_bindings("demo")
        assert bindings == {"GOOD": {"kind": "literal", "value": "yes"}}

    def test_get_bindings_handles_non_dict_root(self, tmp_path):
        vm, script_dir = self._setup(tmp_path)
        # A list at the root is invalid; we shouldn't crash.
        (script_dir / "variables.json").write_text("[1, 2, 3]")
        assert vm.get_bindings("demo") == {}

    def test_get_bindings_handles_invalid_json(self, tmp_path):
        vm, script_dir = self._setup(tmp_path)
        (script_dir / "variables.json").write_text("{not json")
        assert vm.get_bindings("demo") == {}

    def test_set_binding_writes_atomically(self, tmp_path):
        vm, script_dir = self._setup(tmp_path)
        vm.set_binding("demo", "API_KEY", {
            "kind": "office_secret", "ref": "OPENAI_API_KEY",
        })
        on_disk = json.loads((script_dir / "variables.json").read_text())
        assert on_disk == {
            "API_KEY": {"kind": "office_secret", "ref": "OPENAI_API_KEY"},
        }

    def test_set_binding_preserves_other_entries(self, tmp_path):
        vm, script_dir = self._setup(tmp_path)
        # Pre-populate with two existing bindings.
        (script_dir / "variables.json").write_text(json.dumps({
            "COUNT": {"kind": "literal", "value": 100},
            "QUERY": {"kind": "literal", "value": "stale"},
        }))
        # Overwrite only one of them.
        vm.set_binding("demo", "QUERY", {"kind": "literal", "value": "fresh"})
        on_disk = json.loads((script_dir / "variables.json").read_text())
        assert on_disk == {
            "COUNT": {"kind": "literal", "value": 100},
            "QUERY": {"kind": "literal", "value": "fresh"},
        }

    def test_set_binding_none_deletes_entry(self, tmp_path):
        vm, script_dir = self._setup(tmp_path)
        (script_dir / "variables.json").write_text(json.dumps({
            "DOOMED": {"kind": "literal", "value": "x"},
            "KEEPER": {"kind": "literal", "value": "y"},
        }))
        vm.set_binding("demo", "DOOMED", None)
        on_disk = json.loads((script_dir / "variables.json").read_text())
        assert on_disk == {"KEEPER": {"kind": "literal", "value": "y"}}

    def test_set_binding_upgrades_bare_values_on_write(self, tmp_path):
        """When the writer touches the file, every entry gets
        re-serialised as an explicit binding shape — so a legacy
        ``{NAME: VALUE}`` file gets upgraded in place the first
        time the user edits any binding through the UI."""
        vm, script_dir = self._setup(tmp_path)
        (script_dir / "variables.json").write_text(json.dumps({
            "OLD_COUNT": 7,            # bare
            "OLD_TEXT": "raw value",   # bare
        }))
        vm.set_binding("demo", "NEW_BIND", {"kind": "literal", "value": "new"})
        on_disk = json.loads((script_dir / "variables.json").read_text())
        # All three entries now use the explicit binding shape.
        assert on_disk == {
            "OLD_COUNT": {"kind": "literal", "value": 7},
            "OLD_TEXT": {"kind": "literal", "value": "raw value"},
            "NEW_BIND": {"kind": "literal", "value": "new"},
        }

    def test_set_binding_creates_parent_dirs(self, tmp_path):
        # A fresh workspace where no script dir exists yet: the
        # writer must mkdir before the file write so a bootstrap
        # that didn't run yet doesn't crash the binding set.
        vm = VariableManager(str(tmp_path))
        vm.set_binding("brand-new", "VAR", {"kind": "literal", "value": "x"})
        assert (tmp_path / ".scripts" / "brand-new" / "variables.json").is_file()
