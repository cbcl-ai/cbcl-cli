"""Tests for the v2 script manifest parser.

Covers YAML loading, schema validation, edge cases around the
variable->env-dict builder, and the happy-path defaults so a minimal
``script.yaml`` with only an entry point still validates.
"""
from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.scripts.manifest import (  # noqa: E402
    ManifestError,
    ScriptManifest,
    load_manifest,
)


# ---------------------------------------------------------------------------
# Validation — schema-level rejections
# ---------------------------------------------------------------------------


class TestValidation:

    def test_minimal_manifest_accepts_defaults(self, tmp_path):
        # Everything is optional except that validation should lock
        # the defaults: entry_point=main.py, runtime=python3.12,
        # callback_manager=True. Locking these here so a refactor
        # can't silently change the contract the Auto Script Dev
        # agent is prompted with.
        (tmp_path / "script.yaml").write_text("description: minimal\n")
        manifest = load_manifest(tmp_path)
        assert manifest.entry_point == "main.py"
        assert manifest.runtime == "python3.12"
        assert manifest.callback_manager is True
        assert manifest.variables == []
        assert manifest.dependencies == []
        assert manifest.entry_module == "main"

    def test_entry_point_must_end_in_py(self, tmp_path):
        (tmp_path / "script.yaml").write_text(
            "entry_point: main.txt\n",
        )
        with pytest.raises(ManifestError, match="entry_point"):
            load_manifest(tmp_path)

    def test_entry_point_rejects_traversal(self, tmp_path):
        (tmp_path / "script.yaml").write_text(
            "entry_point: ../escape.py\n",
        )
        with pytest.raises(ManifestError, match="entry_point"):
            load_manifest(tmp_path)

    def test_entry_point_rejects_absolute_path(self, tmp_path):
        (tmp_path / "script.yaml").write_text(
            "entry_point: /etc/passwd.py\n",
        )
        with pytest.raises(ManifestError, match="entry_point"):
            load_manifest(tmp_path)

    def test_entry_point_accepts_nested_module(self, tmp_path):
        # Scripts should be able to live in lib/ subpackages.
        (tmp_path / "script.yaml").write_text(
            "entry_point: lib/cli/run.py\n",
        )
        manifest = load_manifest(tmp_path)
        assert manifest.entry_point == "lib/cli/run.py"
        assert manifest.entry_module == "lib.cli.run"

    def test_unknown_runtime_rejected(self, tmp_path):
        # Rubber-stamping unknown runtimes would silently run python
        # against a node script. Reject at parse time.
        (tmp_path / "script.yaml").write_text(
            "runtime: node18\n",
        )
        with pytest.raises(ManifestError, match="runtime"):
            load_manifest(tmp_path)

    def test_variable_name_must_be_env_safe(self, tmp_path):
        # docker exec -e KEY=VALUE silently drops keys that are not
        # valid env-var identifiers on some platforms. Reject early.
        (tmp_path / "script.yaml").write_text(dedent("""\
            variables:
              - name: bad-name
                type: string
        """))
        with pytest.raises(ManifestError, match="env-var identifier"):
            load_manifest(tmp_path)

    def test_duplicate_variable_names_rejected(self, tmp_path):
        (tmp_path / "script.yaml").write_text(dedent("""\
            variables:
              - name: COUNT
                type: number
              - name: COUNT
                type: string
        """))
        with pytest.raises(ManifestError, match="duplicate"):
            load_manifest(tmp_path)

    @pytest.mark.parametrize(
        "reserved",
        [
            "PYTHONPATH",
            "CUBICLE_SCRIPT_DIR",
            "CUBICLE_SCRIPT_NAME",
            "CUBICLE_EXECUTION_ID",
            "CUBICLE_TASK_ID",
        ],
    )
    def test_reserved_variable_names_rejected(self, tmp_path, reserved):
        # Reject at parse time — if a manifest declared PYTHONPATH
        # as a variable and the user set its value via variables.json,
        # the Runner's metadata injection would be shadowed and the
        # script's imports (or its `cubicle` Phase-4 helper) would
        # break in a confusing way. Surfacing this at parse time
        # saves the scriptmaker from a debugging session.
        (tmp_path / "script.yaml").write_text(
            f"variables:\n  - name: {reserved}\n    type: string\n"
        )
        with pytest.raises(ManifestError, match="reserved"):
            load_manifest(tmp_path)

    def test_extra_keys_forbidden(self, tmp_path):
        # Strict schema — a typo in a field name shouldn't silently
        # ship ("entrypoint" vs "entry_point" is exactly the kind of
        # bug that wastes an afternoon).
        (tmp_path / "script.yaml").write_text(
            "entrypoint: main.py\n",   # note missing underscore
        )
        with pytest.raises(ManifestError):
            load_manifest(tmp_path)

    def test_dependency_with_shell_metachars_rejected(self, tmp_path):
        # A dep spec with `;` or backticks is almost certainly a
        # failed attempt at shell injection. pip wouldn't run them
        # (--target isolates exec) but refusing up front is cheap.
        (tmp_path / "script.yaml").write_text(dedent("""\
            dependencies:
              - "requests; rm -rf /"
        """))
        with pytest.raises(ManifestError, match="metacharacters"):
            load_manifest(tmp_path)

    def test_dependency_blank_entries_dropped(self, tmp_path):
        # Trailing-whitespace entries are common after YAML list
        # edits — tolerate them quietly.
        (tmp_path / "script.yaml").write_text(dedent("""\
            dependencies:
              - requests>=2.31
              - ""
              - "   "
        """))
        manifest = load_manifest(tmp_path)
        assert manifest.dependencies == ["requests>=2.31"]


# ---------------------------------------------------------------------------
# Loader — filesystem edge cases
# ---------------------------------------------------------------------------


class TestLoader:

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ManifestError, match="not found"):
            load_manifest(tmp_path)

    def test_empty_file_raises(self, tmp_path):
        (tmp_path / "script.yaml").write_text("")
        with pytest.raises(ManifestError, match="empty"):
            load_manifest(tmp_path)

    def test_non_dict_root_raises(self, tmp_path):
        # A YAML list at the top level is a class of authoring
        # mistake to catch clearly.
        (tmp_path / "script.yaml").write_text("- just a list\n")
        with pytest.raises(ManifestError, match="mapping"):
            load_manifest(tmp_path)

    def test_malformed_yaml_raises(self, tmp_path):
        (tmp_path / "script.yaml").write_text(
            "entry_point: [unclosed list\n",
        )
        with pytest.raises(ManifestError, match="valid YAML"):
            load_manifest(tmp_path)

    def test_oversized_file_raises(self, tmp_path):
        # Sanity cap — a multi-MB manifest is always a bug.
        payload = "description: " + ("x" * 200_000) + "\n"
        (tmp_path / "script.yaml").write_text(payload)
        with pytest.raises(ManifestError, match="larger than"):
            load_manifest(tmp_path)


# ---------------------------------------------------------------------------
# env_from — the runner's injection builder
# ---------------------------------------------------------------------------


class TestEnvFrom:

    def _manifest(self, yaml_text: str, tmp_path: Path) -> ScriptManifest:
        (tmp_path / "script.yaml").write_text(yaml_text)
        return load_manifest(tmp_path)

    def test_only_declared_vars_are_injected(self, tmp_path):
        # Anything in variables.json that the manifest DOESN'T
        # declare is a leftover from a previous version — don't
        # leak it into env. Scripts should rely only on declared
        # vars.
        manifest = self._manifest(
            dedent("""\
                variables:
                  - name: SEARCH_QUERY
                    type: string
            """),
            tmp_path,
        )
        env = manifest.env_from(
            variable_values={
                "SEARCH_QUERY": "python devs",
                "STALE_VAR": "should not be injected",
            },
            secrets={},
        )
        assert env == {"SEARCH_QUERY": "python devs"}

    def test_secrets_override_variables(self, tmp_path):
        # Secrets file is authoritative if both carry the same key —
        # protects against a user accidentally putting an API key
        # into variables.json.
        manifest = self._manifest(
            dedent("""\
                variables:
                  - name: API_KEY
                    type: string
                    is_secret: true
            """),
            tmp_path,
        )
        env = manifest.env_from(
            variable_values={"API_KEY": "exposed-in-json"},
            secrets={"API_KEY": "real-secret"},
        )
        assert env == {"API_KEY": "real-secret"}

    def test_defaults_populate_missing_values(self, tmp_path):
        manifest = self._manifest(
            dedent("""\
                variables:
                  - name: COUNT
                    type: number
                    default: 100
                  - name: DELAY
                    type: number
                    default: 90
            """),
            tmp_path,
        )
        env = manifest.env_from(
            variable_values={"COUNT": 50},  # overrides default
            secrets={},
        )
        # COUNT comes from variables.json, DELAY from manifest default
        assert env == {"COUNT": "50", "DELAY": "90"}

    def test_undeclared_vars_are_dropped(self, tmp_path):
        manifest = self._manifest("variables: []\n", tmp_path)
        env = manifest.env_from(
            variable_values={"ANYTHING": "nope"},
            secrets={"API_KEY": "nope"},
        )
        assert env == {}

    def test_booleans_stringify_to_lowercase(self, tmp_path):
        # Idiomatic for shell-style checks the script may layer on.
        # Also the convention PyYAML round-trips cleanly.
        manifest = self._manifest(
            dedent("""\
                variables:
                  - name: DRY_RUN
                    type: boolean
                    default: true
                  - name: VERBOSE
                    type: boolean
                    default: false
            """),
            tmp_path,
        )
        env = manifest.env_from(variable_values={}, secrets={})
        assert env["DRY_RUN"] == "true"
        assert env["VERBOSE"] == "false"

    def test_missing_var_with_no_default_omitted(self, tmp_path):
        # Scripts can decide whether that's fatal via
        # os.environ.get("X") vs os.environ["X"]. We don't force a
        # choice here.
        manifest = self._manifest(
            dedent("""\
                variables:
                  - name: REQUIRED_KEY
                    type: string
            """),
            tmp_path,
        )
        env = manifest.env_from(variable_values={}, secrets={})
        assert "REQUIRED_KEY" not in env

    def test_entry_module_for_nested_path(self, tmp_path):
        manifest = self._manifest(
            "entry_point: lib/cli/run.py\n",
            tmp_path,
        )
        assert manifest.entry_module == "lib.cli.run"


# ── from_office_secret schema + env resolution ───────────────────────


class TestFromOfficeSecret:
    """Manifest variables can opt into the office's shared secrets
    store via ``from_office_secret: NAME``. Tests cover schema
    validation, mutual exclusion with ``default``, env-dict
    precedence, and the ``office_secret_refs`` helper."""

    def _manifest(self, yaml_body: str, tmp_path):
        from textwrap import dedent
        from src.scripts.manifest import load_manifest

        (tmp_path / "script.yaml").write_text(dedent(yaml_body))
        return load_manifest(tmp_path)

    def test_accepts_well_formed_reference(self, tmp_path):
        manifest = self._manifest(
            """
            variables:
              - name: OPENAI_KEY
                type: string
                from_office_secret: OPENAI_API_KEY
            """,
            tmp_path,
        )
        assert manifest.variables[0].from_office_secret == "OPENAI_API_KEY"

    def test_rejects_lowercase_reference(self, tmp_path):
        import pytest
        from src.scripts.manifest import ManifestError

        with pytest.raises(ManifestError, match="from_office_secret"):
            self._manifest(
                """
                variables:
                  - name: K
                    from_office_secret: openai_key
                """,
                tmp_path,
            )

    def test_rejects_default_and_reference_together(self, tmp_path):
        import pytest
        from src.scripts.manifest import ManifestError

        with pytest.raises(
            ManifestError, match="from_office_secret and default",
        ):
            self._manifest(
                """
                variables:
                  - name: K
                    default: "fallback"
                    from_office_secret: K
                """,
                tmp_path,
            )

    def test_office_secret_refs_returns_var_to_secret_mapping(
        self, tmp_path,
    ):
        manifest = self._manifest(
            """
            variables:
              - name: OPENAI_KEY
                from_office_secret: OPENAI_API_KEY
              - name: PLAIN_VAR
                default: "x"
              - name: ANTHROPIC_KEY
                from_office_secret: ANTHROPIC_API_KEY
            """,
            tmp_path,
        )
        assert manifest.office_secret_refs() == {
            "OPENAI_KEY": "OPENAI_API_KEY",
            "ANTHROPIC_KEY": "ANTHROPIC_API_KEY",
        }

    def test_env_from_resolves_office_secret(self, tmp_path):
        manifest = self._manifest(
            """
            variables:
              - name: OPENAI_KEY
                from_office_secret: OPENAI_API_KEY
              - name: COUNT
                type: number
                default: 50
            """,
            tmp_path,
        )
        env = manifest.env_from(
            variable_values={},
            secrets={},
            office_secrets={"OPENAI_API_KEY": "sk-live"},
        )
        assert env["OPENAI_KEY"] == "sk-live"
        assert env["COUNT"] == "50"

    def test_env_from_omits_missing_office_secret(self, tmp_path):
        """When the referenced office secret is absent, env_from
        omits it (the runner's preflight is responsible for
        refusing; env_from is pure)."""
        manifest = self._manifest(
            """
            variables:
              - name: OPENAI_KEY
                from_office_secret: OPENAI_API_KEY
            """,
            tmp_path,
        )
        env = manifest.env_from(
            variable_values={},
            secrets={},
            office_secrets={},
        )
        assert "OPENAI_KEY" not in env

    def test_office_secret_overrides_per_script_secret(self, tmp_path):
        """Office store wins over .secrets.json for the same
        variable name when from_office_secret is set."""
        manifest = self._manifest(
            """
            variables:
              - name: API_KEY
                from_office_secret: API_KEY
            """,
            tmp_path,
        )
        env = manifest.env_from(
            variable_values={},
            secrets={"API_KEY": "stale_per_script"},
            office_secrets={"API_KEY": "live_office"},
        )
        assert env["API_KEY"] == "live_office"


# ── Phase 1.5: variable BINDING resolution ─────────────────────────────────


class TestBindingResolution:
    """Phase 1.5 of the Scripts marketplace work: variables.json
    carries a per-variable BINDING that resolves at run time to
    either a literal value or an office-secret reference. Bindings
    take precedence over both ``.secrets.json`` and the legacy
    manifest ``from_office_secret`` fallback.
    """

    def _manifest(self, yaml_body: str, tmp_path):
        from textwrap import dedent
        from src.scripts.manifest import load_manifest

        (tmp_path / "script.yaml").write_text(dedent(yaml_body))
        return load_manifest(tmp_path)

    def test_literal_binding_resolves_value(self, tmp_path):
        manifest = self._manifest(
            """
            variables:
              - name: COUNT
                type: number
            """,
            tmp_path,
        )
        env = manifest.env_from(
            variable_values={},
            secrets={},
            bindings={"COUNT": {"kind": "literal", "value": 42}},
        )
        assert env == {"COUNT": "42"}

    def test_bare_value_binding_legacy_compat(self, tmp_path):
        """A bare value in variables.json (legacy + hand-edited
        files) is still accepted on read and treated as a literal."""
        manifest = self._manifest(
            """
            variables:
              - name: SEARCH_QUERY
                type: string
            """,
            tmp_path,
        )
        env = manifest.env_from(
            variable_values={},
            secrets={},
            bindings={"SEARCH_QUERY": "python developer"},
        )
        assert env == {"SEARCH_QUERY": "python developer"}

    def test_office_secret_binding_resolves_from_store(self, tmp_path):
        manifest = self._manifest(
            """
            variables:
              - name: API_KEY
                type: string
                is_secret: true
            """,
            tmp_path,
        )
        env = manifest.env_from(
            variable_values={},
            secrets={},
            office_secrets={"OPENAI_API_KEY": "sk-live-xxx"},
            bindings={
                "API_KEY": {"kind": "office_secret", "ref": "OPENAI_API_KEY"},
            },
        )
        assert env == {"API_KEY": "sk-live-xxx"}

    def test_office_secret_binding_with_missing_ref_omits_and_does_not_fall_back_to_manifest(
        self, tmp_path,
    ):
        """When a binding explicitly references an office secret and
        the store doesn't have it, the variable is OMITTED — we do
        NOT silently fall back to the manifest's legacy
        ``from_office_secret`` (the user opted into a specific ref
        via the UI and a different resolution would mask the
        misconfiguration). The Runner's preflight will refuse the
        run before reaching this method in production."""
        manifest = self._manifest(
            """
            variables:
              - name: API_KEY
                type: string
                is_secret: true
                from_office_secret: LEGACY_API_KEY
            """,
            tmp_path,
        )
        env = manifest.env_from(
            variable_values={},
            secrets={},
            office_secrets={"LEGACY_API_KEY": "should-not-leak"},
            bindings={
                "API_KEY": {"kind": "office_secret", "ref": "MISSING_REF"},
            },
        )
        assert "API_KEY" not in env

    def test_binding_overrides_legacy_from_office_secret(self, tmp_path):
        """A binding for the same variable name beats the manifest's
        legacy ``from_office_secret`` field — the UI's choice wins."""
        manifest = self._manifest(
            """
            variables:
              - name: API_KEY
                type: string
                is_secret: true
                from_office_secret: LEGACY_API_KEY
            """,
            tmp_path,
        )
        env = manifest.env_from(
            variable_values={},
            secrets={},
            office_secrets={
                "LEGACY_API_KEY": "from-manifest",
                "NEW_API_KEY": "from-binding",
            },
            bindings={
                "API_KEY": {"kind": "office_secret", "ref": "NEW_API_KEY"},
            },
        )
        assert env == {"API_KEY": "from-binding"}

    def test_binding_overrides_secrets_json(self, tmp_path):
        """A literal binding for a secret variable beats a literal
        value in .secrets.json — the UI write through the binding
        path is the more recent intent. (In practice the dispatch
        layer also drops the .secrets.json entry when rebinding to
        office_secret, so this only matters when both paths wrote
        independently.)"""
        manifest = self._manifest(
            """
            variables:
              - name: TOKEN
                type: string
                is_secret: true
            """,
            tmp_path,
        )
        env = manifest.env_from(
            variable_values={},
            secrets={"TOKEN": "stale_secrets_json"},
            bindings={"TOKEN": {"kind": "literal", "value": "fresh_binding"}},
        )
        assert env == {"TOKEN": "fresh_binding"}

    def test_legacy_from_office_secret_still_works_without_binding(
        self, tmp_path,
    ):
        """Scripts created before Phase 1.5 that have a manifest
        ``from_office_secret`` and no binding still resolve via the
        legacy fallback."""
        manifest = self._manifest(
            """
            variables:
              - name: API_KEY
                type: string
                is_secret: true
                from_office_secret: LEGACY_API_KEY
            """,
            tmp_path,
        )
        env = manifest.env_from(
            variable_values={},
            secrets={},
            office_secrets={"LEGACY_API_KEY": "legacy-value"},
            bindings={},  # no binding configured
        )
        assert env == {"API_KEY": "legacy-value"}

    def test_malformed_binding_is_ignored_and_falls_through(self, tmp_path):
        """A binding with an unknown ``kind`` (e.g. user-edited
        variables.json) is dropped at normalise time; the env-build
        then falls through to the next source (secrets.json /
        manifest default / etc.)."""
        manifest = self._manifest(
            """
            variables:
              - name: COUNT
                type: number
                default: 7
            """,
            tmp_path,
        )
        env = manifest.env_from(
            variable_values={},
            secrets={},
            bindings={"COUNT": {"kind": "weird", "value": 99}},
        )
        # Malformed binding → fall through to manifest default.
        assert env == {"COUNT": "7"}
