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
