"""Tests for the Script Runner, SecretsStore, and ScriptSyncer."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.scripts.manifest import ManifestError
from src.scripts.secrets_store import SecretsStore
from src.scripts.variable_manager import VariableManager
from src.scripts.script_runner import ScriptRunner
from src.config_sync.script_sync import ScriptSyncer


# -----------------------------------------------------------------------
# Secrets Store
# -----------------------------------------------------------------------


class TestSecretsStore:
    def test_script_secret_roundtrip(self, tmp_path):
        store = SecretsStore(str(tmp_path))

        store.set_script_secret("test-script", "API_KEY", "secret123")
        secrets = store.get_script_secrets("test-script")
        assert secrets["API_KEY"] == "secret123"

    def test_script_secret_file_location(self, tmp_path):
        store = SecretsStore(str(tmp_path))

        store.set_script_secret("my-script", "TOKEN", "abc")
        expected = tmp_path / ".scripts" / "my-script" / ".secrets.json"
        assert expected.exists()

    def test_script_secret_update_existing(self, tmp_path):
        store = SecretsStore(str(tmp_path))

        store.set_script_secret("s", "K1", "v1")
        store.set_script_secret("s", "K2", "v2")
        secrets = store.get_script_secrets("s")
        assert secrets == {"K1": "v1", "K2": "v2"}

    def test_skill_secret_roundtrip(self, tmp_path):
        store = SecretsStore(
            workspace_path=str(tmp_path / "workspace"),
            config_dir=str(tmp_path / "config"),
        )

        store.set_skill_secret("slack", "BOT_TOKEN", "xoxb-123")
        secrets = store.get_skill_secrets("slack")
        assert secrets["BOT_TOKEN"] == "xoxb-123"

    def test_skill_secret_file_location(self, tmp_path):
        config_dir = tmp_path / "config"
        store = SecretsStore(
            workspace_path=str(tmp_path / "workspace"),
            config_dir=str(config_dir),
        )

        store.set_skill_secret("gmail", "TOKEN", "ya29")
        expected = config_dir / "secrets" / "skills" / "gmail" / "secrets.json"
        assert expected.exists()

    def test_missing_secrets_returns_empty(self, tmp_path):
        store = SecretsStore(str(tmp_path))
        assert store.get_script_secrets("nonexistent") == {}
        assert store.get_skill_secrets("nonexistent") == {}


# -----------------------------------------------------------------------
# Script Syncer
# -----------------------------------------------------------------------


class TestScriptSyncer:
    @pytest.mark.asyncio
    async def test_sync_creates_script_directory(self, tmp_path):
        """ScriptSyncer creates directory structure but does NOT write project files.
        Mini-project files (main.py, script.yaml, lib/, requirements.txt)
        live on disk; they're laid down by the backend bootstrap on create
        and then edited by agents or users via the Files tree."""
        syncer = ScriptSyncer(str(tmp_path))
        scripts = [
            {
                "name": "test-script",
                "variable_schema": [],
            }
        ]
        await syncer.sync_scripts(scripts)

        script_dir = tmp_path / ".scripts" / "test-script"
        assert script_dir.is_dir()
        assert (script_dir / "variables.json").exists()
        assert (script_dir / ".secrets.json").exists()
        assert (script_dir / "executions").is_dir()

    @pytest.mark.asyncio
    async def test_sync_creates_empty_variables_json_when_missing(
        self, tmp_path
    ):
        # Sync should create an empty ``variables.json`` on first
        # run so the mini-project has a valid file to read. The
        # manifest's ``default:`` fields on each variable are the
        # authoritative runtime defaults — variables.json holds
        # only user overrides (set via the UI).
        syncer = ScriptSyncer(str(tmp_path))
        scripts = [
            {
                "name": "test-script",
                "variable_schema": [
                    {
                        "name": "COUNT", "type": "number",
                        "is_secret": False,
                    },
                ],
            }
        ]
        await syncer.sync_scripts(scripts)

        var_file = tmp_path / ".scripts" / "test-script" / "variables.json"
        assert var_file.exists()
        assert json.loads(var_file.read_text()) == {}

    @pytest.mark.asyncio
    async def test_sync_does_not_overwrite_existing_variables(
        self, tmp_path
    ):
        # Regression guard: the user's edits to variables.json must
        # NOT be clobbered by a sync that happens later.
        script_dir = tmp_path / ".scripts" / "my-script"
        script_dir.mkdir(parents=True)
        var_file = script_dir / "variables.json"
        var_file.write_text('{"USER_SET": "value"}')

        syncer = ScriptSyncer(str(tmp_path))
        await syncer.sync_scripts([
            {"name": "my-script", "variable_schema": []},
        ])

        assert json.loads(var_file.read_text()) == {"USER_SET": "value"}

    @pytest.mark.asyncio
    async def test_sync_creates_empty_secrets_json(self, tmp_path):
        syncer = ScriptSyncer(str(tmp_path))
        scripts = [{"name": "new-script", "variable_schema": []}]
        await syncer.sync_scripts(scripts)

        secrets_file = (
            tmp_path / ".scripts" / "new-script" / ".secrets.json"
        )
        assert secrets_file.exists()
        assert json.loads(secrets_file.read_text()) == {}

    @pytest.mark.asyncio
    async def test_sync_does_not_overwrite_existing_secrets(self, tmp_path):
        # Pre-create .secrets.json with existing secrets
        script_dir = tmp_path / ".scripts" / "my-script"
        script_dir.mkdir(parents=True)
        secrets_file = script_dir / ".secrets.json"
        secrets_file.write_text('{"EXISTING": "value"}')

        syncer = ScriptSyncer(str(tmp_path))
        scripts = [{"name": "my-script", "variable_schema": []}]
        await syncer.sync_scripts(scripts)

        data = json.loads(secrets_file.read_text())
        assert data == {"EXISTING": "value"}

    @pytest.mark.asyncio
    async def test_sync_creates_executions_dir(self, tmp_path):
        syncer = ScriptSyncer(str(tmp_path))
        scripts = [{"name": "s1", "variable_schema": []}]
        await syncer.sync_scripts(scripts)

        assert (tmp_path / ".scripts" / "s1" / "executions").is_dir()

    @pytest.mark.asyncio
    async def test_sync_removes_stale_directories(self, tmp_path):
        # Create a stale script directory — something left on disk
        # that's no longer in the current config payload. Syncer
        # should GC it.
        stale_dir = tmp_path / ".scripts" / "old-script"
        stale_dir.mkdir(parents=True)
        (stale_dir / "main.py").write_text("print('old')")
        (stale_dir / "script.yaml").write_text("description: old\n")

        syncer = ScriptSyncer(str(tmp_path))
        scripts = [{"name": "new-script", "variable_schema": []}]
        await syncer.sync_scripts(scripts)

        assert not stale_dir.exists()
        # Directory created; mini-project files come from backend
        # bootstrap, not from sync.
        assert (tmp_path / ".scripts" / "new-script").is_dir()

    def test_sync_from_config_message(self, tmp_path):
        syncer = ScriptSyncer(str(tmp_path))
        message = {
            "config": {
                "scripts": [
                    {"name": "s1", "variable_schema": []}
                ]
            }
        }
        asyncio.run(syncer.sync_from_config(message))
        # Directory created; mini-project files are laid down by the
        # backend bootstrap, not by sync.
        assert (tmp_path / ".scripts" / "s1").is_dir()
        assert (tmp_path / ".scripts" / "s1" / "variables.json").exists()

class TestMiniProjectExecution:
    """Locks the contract of the mini-project launch pathway:

    - Entry point resolves from ``manifest.entry_module``
    - Manifest-declared variables flow into the child as env vars
    - PYTHONPATH points at ``lib/``, ``.deps/``, and the script root
    - A bad manifest surfaces with a clear error rather than
      producing a mystery runtime failure
    """

    def _runner(
        self,
        workspace: Path,
        container_name: str | None = None,
    ) -> ScriptRunner:
        secrets = SecretsStore(str(workspace))
        variables = VariableManager(str(workspace))
        return ScriptRunner(
            workspace_path=str(workspace),
            secrets_store=secrets,
            variable_manager=variables,
            ws_client=None,
            container_name=container_name,
        )

    def _make_v2_project(
        self,
        workspace: Path,
        name: str,
        *,
        manifest_yaml: str,
        main_py: str = "print('ok')\n",
        extras: dict[str, str] | None = None,
    ) -> Path:
        script_dir = workspace / ".scripts" / name
        script_dir.mkdir(parents=True, exist_ok=True)
        (script_dir / "script.yaml").write_text(manifest_yaml)
        (script_dir / "main.py").write_text(main_py)
        (script_dir / "variables.json").write_text("{}")
        (script_dir / ".secrets.json").write_text("{}")
        (script_dir / "executions").mkdir(exist_ok=True)
        for rel, content in (extras or {}).items():
            path = script_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return script_dir

    @pytest.mark.asyncio
    async def test_runs_with_only_manifest_and_entry(self, tmp_path):
        # Sanity: the runner launches the manifest's entry module
        # directly — no ``script.py`` shim, no materialised
        # ``_run.py``. Only ``script.yaml`` + ``main.py`` are
        # required on disk (plus whatever ``lib/`` modules the
        # script imports).
        self._make_v2_project(
            tmp_path,
            "v2-only",
            manifest_yaml="description: minimal mini-project\n",
        )
        runner = self._runner(tmp_path)

        captured: dict = {}

        async def _fake_spawn(*args, **kwargs):
            captured["argv"] = args
            captured["env"] = kwargs.get("env")

            class _Stub:
                returncode = None
                pid = 1
            return _Stub()

        with patch(
            "src.scripts.script_runner.asyncio.create_subprocess_exec",
            side_effect=_fake_spawn,
        ):
            exec_id = await runner.execute("v2-only", triggered_by="test")

        assert exec_id.startswith("exec-")
        assert "python" in captured["argv"]
        assert "-m" in captured["argv"]
        assert "main" in captured["argv"]

    @pytest.mark.asyncio
    async def test_entry_point_nested_becomes_module(self, tmp_path):
        self._make_v2_project(
            tmp_path,
            "nested-entry",
            manifest_yaml="entry_point: lib/cli/run.py\n",
            main_py="# unused — entry is lib/cli/run.py\n",
            extras={
                "lib/__init__.py": "",
                "lib/cli/__init__.py": "",
                "lib/cli/run.py": "print('from nested')\n",
            },
        )
        runner = self._runner(tmp_path)

        captured_argv: list = []

        async def _fake_spawn(*args, **kwargs):
            captured_argv.extend(args)

            class _Stub:
                returncode = None
            return _Stub()

        with patch(
            "src.scripts.script_runner.asyncio.create_subprocess_exec",
            side_effect=_fake_spawn,
        ):
            await runner.execute("nested-entry", triggered_by="test")

        # python -m lib.cli.run — dotted module, not a path.
        assert captured_argv[-3:] == ["python", "-m", "lib.cli.run"]

    @pytest.mark.asyncio
    async def test_declared_variables_injected_as_env(self, tmp_path):
        # A manifest-declared variable flows to the child process
        # via env, NOT via Jinja injection into source. Locking this
        # because the whole v2 pathway's ergonomic advantage is
        # "author writes os.environ[X] instead of {{X}}".
        self._make_v2_project(
            tmp_path,
            "env-injected",
            manifest_yaml=dedent("""\
                variables:
                  - name: SEARCH_QUERY
                    type: string
                    default: "python devs"
                  - name: COUNT
                    type: number
                    default: 100
            """),
        )
        runner = self._runner(tmp_path)

        captured_env: dict = {}

        async def _fake_spawn(*args, **kwargs):
            captured_env.update(kwargs.get("env") or {})

            class _Stub:
                returncode = None
            return _Stub()

        with patch(
            "src.scripts.script_runner.asyncio.create_subprocess_exec",
            side_effect=_fake_spawn,
        ):
            await runner.execute("env-injected", triggered_by="test")

        assert captured_env["SEARCH_QUERY"] == "python devs"
        assert captured_env["COUNT"] == "100"

    @pytest.mark.asyncio
    async def test_variable_overrides_win(self, tmp_path):
        self._make_v2_project(
            tmp_path,
            "override-test",
            manifest_yaml=dedent("""\
                variables:
                  - name: MODE
                    type: string
                    default: "default"
            """),
        )
        runner = self._runner(tmp_path)

        captured_env: dict = {}

        async def _fake_spawn(*args, **kwargs):
            captured_env.update(kwargs.get("env") or {})

            class _Stub:
                returncode = None
            return _Stub()

        with patch(
            "src.scripts.script_runner.asyncio.create_subprocess_exec",
            side_effect=_fake_spawn,
        ):
            await runner.execute(
                "override-test",
                variable_overrides={"MODE": "from-caller"},
                triggered_by="test",
            )

        assert captured_env["MODE"] == "from-caller"

    @pytest.mark.asyncio
    async def test_pythonpath_points_at_lib_deps_and_script_root(
        self, tmp_path
    ):
        self._make_v2_project(
            tmp_path,
            "pythonpath-test",
            manifest_yaml="description: x\n",
        )
        runner = self._runner(tmp_path)

        captured_env: dict = {}

        async def _fake_spawn(*args, **kwargs):
            captured_env.update(kwargs.get("env") or {})

            class _Stub:
                returncode = None
            return _Stub()

        with patch(
            "src.scripts.script_runner.asyncio.create_subprocess_exec",
            side_effect=_fake_spawn,
        ):
            await runner.execute("pythonpath-test", triggered_by="test")

        pp = captured_env["PYTHONPATH"]
        script_root = str(tmp_path / ".scripts" / "pythonpath-test")
        assert f"{script_root}/lib" in pp
        assert f"{script_root}/.deps" in pp
        assert script_root in pp

    @pytest.mark.asyncio
    async def test_bad_manifest_raises_before_spawn(self, tmp_path):
        # A malformed manifest should fail early with a clear error,
        # not run the script against stale state.
        script_dir = tmp_path / ".scripts" / "broken"
        script_dir.mkdir(parents=True)
        (script_dir / "script.yaml").write_text(
            "entrypoint: not_a_field\n",  # typo — strict schema
        )
        (script_dir / "main.py").write_text("print('never runs')\n")
        (script_dir / "variables.json").write_text("{}")
        (script_dir / ".secrets.json").write_text("{}")
        (script_dir / "executions").mkdir(exist_ok=True)

        runner = self._runner(tmp_path)
        with patch(
            "src.scripts.script_runner.asyncio.create_subprocess_exec",
        ) as spawn:
            with pytest.raises(ManifestError):
                await runner.execute("broken", triggered_by="test")
            # We must NOT have spawned a process before parsing the
            # manifest — otherwise a bad manifest would produce an
            # orphan "exec-*" dir + log with no script actually run.
            spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_undeclared_overrides_are_dropped(self, tmp_path):
        # Overrides are a narrow escape hatch — we accept values
        # for DECLARED variables only. Anything else is a caller
        # bug (stale tooling, typo) and should be quietly dropped
        # with a warning log; letting it through would leak
        # arbitrary env keys into the child and violate the
        # declared-only contract.
        self._make_v2_project(
            tmp_path,
            "override-drop-test",
            manifest_yaml=dedent("""\
                variables:
                  - name: MODE
                    type: string
                    default: "default"
            """),
        )
        runner = self._runner(tmp_path)

        captured_env: dict = {}

        async def _fake_spawn(*args, **kwargs):
            captured_env.update(kwargs.get("env") or {})

            class _Stub:
                returncode = None
            return _Stub()

        with patch(
            "src.scripts.script_runner.asyncio.create_subprocess_exec",
            side_effect=_fake_spawn,
        ):
            await runner.execute(
                "override-drop-test",
                variable_overrides={
                    "MODE": "declared-ok",
                    "ROGUE_KEY": "should-be-dropped",
                    # A reserved-name attempt via overrides should
                    # also drop since it's undeclared.
                    "PYTHONPATH": "attacker-controlled",
                },
                triggered_by="test",
            )

        assert captured_env["MODE"] == "declared-ok"
        assert "ROGUE_KEY" not in captured_env
        # PYTHONPATH should still be the Runner-injected value,
        # NOT the attacker's — verify by checking it contains the
        # script's lib dir.
        script_lib = str(tmp_path / ".scripts" / "override-drop-test" / "lib")
        assert script_lib in captured_env["PYTHONPATH"]

    @pytest.mark.asyncio
    async def test_cwd_is_script_dir_not_workspace(self, tmp_path):
        # Host fallback must match docker's `-w script_dir` so a
        # script that opens "config.json" (relative path) works the
        # same in tests and in production. Regression test for the
        # M3 finding.
        self._make_v2_project(
            tmp_path,
            "cwd-test",
            manifest_yaml="description: x\n",
        )
        runner = self._runner(tmp_path)

        captured_kwargs: dict = {}

        async def _fake_spawn(*args, **kwargs):
            captured_kwargs.update(kwargs)

            class _Stub:
                returncode = None
            return _Stub()

        with patch(
            "src.scripts.script_runner.asyncio.create_subprocess_exec",
            side_effect=_fake_spawn,
        ):
            await runner.execute("cwd-test", triggered_by="test")

        assert captured_kwargs["cwd"] == str(
            tmp_path / ".scripts" / "cwd-test"
        )

    @pytest.mark.asyncio
    async def test_spawn_failure_marks_status_failed(self, tmp_path):
        # If create_subprocess_exec raises (permission denied,
        # binary missing, whatever), the exec_dir stays on disk
        # (user can inspect it) but status.json must be updated to
        # "failed" — otherwise the UI shows a ghost "running"
        # execution forever.
        self._make_v2_project(
            tmp_path,
            "spawn-fail-test",
            manifest_yaml="description: x\n",
        )
        runner = self._runner(tmp_path)

        async def _exploding_spawn(*args, **kwargs):
            raise OSError("simulated spawn failure")

        with patch(
            "src.scripts.script_runner.asyncio.create_subprocess_exec",
            side_effect=_exploding_spawn,
        ):
            with pytest.raises(OSError):
                await runner.execute(
                    "spawn-fail-test", triggered_by="test",
                )

        # Exactly one exec dir under executions/ — find it by
        # walking rather than reconstructing the exec_id.
        exec_root = tmp_path / ".scripts" / "spawn-fail-test" / "executions"
        exec_dirs = [p for p in exec_root.iterdir() if p.is_dir()]
        assert len(exec_dirs) == 1
        status_path = exec_dirs[0] / "status.json"
        assert status_path.exists()
        status = json.loads(status_path.read_text())
        assert status["status"] == "failed"
        assert "spawn failed" in (status["error_message"] or "")

    @pytest.mark.asyncio
    async def test_deps_install_error_propagates_before_exec_dir(
        self, tmp_path
    ):
        # DepsInstallError must NOT leave a ghost exec-* dir on
        # disk — deps are installed BEFORE the exec_dir is
        # created (script_runner.py: step 3 before step 4).
        self._make_v2_project(
            tmp_path,
            "deps-fail-test",
            manifest_yaml=dedent("""\
                dependencies:
                  - rich
            """),
            extras={"requirements.txt": "rich\n"},
        )
        runner = self._runner(tmp_path)

        from src.scripts.deps_installer import DepsInstallError

        async def _boom(**kwargs):
            raise DepsInstallError("simulated pip failure", stderr_tail="nope")

        with patch(
            "src.scripts.script_runner.ensure_deps_installed",
            side_effect=_boom,
        ):
            with pytest.raises(DepsInstallError):
                await runner.execute(
                    "deps-fail-test", triggered_by="test",
                )

        # No exec dir should have been allocated since the deps
        # install raised BEFORE exec_dir.mkdir in _execute_v2.
        exec_root = tmp_path / ".scripts" / "deps-fail-test" / "executions"
        assert not any(p.is_dir() for p in exec_root.iterdir())

    @pytest.mark.asyncio
    async def test_docker_mode_injects_manifest_vars_via_minus_e(
        self, tmp_path
    ):
        # When docker mode is on, manifest vars MUST flow through
        # -e (not env=) so the container sees them. Regression test
        # for the host-env-leak audit concern.
        self._make_v2_project(
            tmp_path,
            "docker-env-test",
            manifest_yaml=dedent("""\
                variables:
                  - name: API_KEY
                    type: string
                    is_secret: true
                    default: "placeholder"
            """),
        )
        runner = self._runner(
            tmp_path, container_name="cbcl-office-foo",
        )

        captured_argv: list = []
        captured_kwargs: dict = {}

        async def _fake_spawn(*args, **kwargs):
            captured_argv.extend(args)
            captured_kwargs.update(kwargs)

            class _Stub:
                returncode = None
            return _Stub()

        with patch(
            "src.scripts.script_runner.asyncio.create_subprocess_exec",
            side_effect=_fake_spawn,
        ):
            await runner.execute("docker-env-test", triggered_by="test")

        # Docker branch MUST NOT pass env= — all vars go via -e.
        assert "env" not in captured_kwargs
        # Find the -e pair carrying API_KEY.
        env_pairs = [
            captured_argv[i + 1]
            for i, flag in enumerate(captured_argv)
            if flag == "-e" and i + 1 < len(captured_argv)
        ]
        assert any(p.startswith("API_KEY=placeholder") for p in env_pairs)


class TestPerWorkstreamOutputDir:
    """Locks the contract that scripts get a per-task CUBICLE_OUTPUT_DIR
    injected by the Runner. Without this, all script outputs collide in
    the flat /workspace/outputs/ root and the user can't find anything
    when multiple workstreams run scripts."""

    def _runner(self, workspace: Path) -> ScriptRunner:
        return ScriptRunner(
            workspace_path=str(workspace),
            secrets_store=SecretsStore(str(workspace)),
            variable_manager=VariableManager(str(workspace)),
            ws_client=None,
        )

    def _make_project(self, workspace: Path, name: str) -> Path:
        script_dir = workspace / ".scripts" / name
        script_dir.mkdir(parents=True, exist_ok=True)
        (script_dir / "script.yaml").write_text(
            "description: test\n",
        )
        (script_dir / "main.py").write_text("print('ok')\n")
        (script_dir / "variables.json").write_text("{}")
        (script_dir / ".secrets.json").write_text("{}")
        (script_dir / "executions").mkdir(exist_ok=True)
        return script_dir

    @pytest.mark.asyncio
    async def test_output_dir_with_workstream_only(self, tmp_path):
        self._make_project(tmp_path, "ws-only")
        runner = self._runner(tmp_path)
        captured: dict = {}

        async def _fake_spawn(*args, **kwargs):
            captured["env"] = kwargs.get("env")

            class _Stub:
                returncode = None
            return _Stub()

        with patch(
            "src.scripts.script_runner.asyncio.create_subprocess_exec",
            side_effect=_fake_spawn,
        ):
            await runner.execute(
                "ws-only",
                triggered_by="test",
                workstream_short_code="WR",
            )

        env = captured["env"]
        assert env["CUBICLE_OUTPUT_DIR"] == str(tmp_path / "outputs" / "WR")
        # Pre-created on disk so the script's first write doesn't race.
        assert (tmp_path / "outputs" / "WR").is_dir()

    @pytest.mark.asyncio
    async def test_output_dir_with_scope(self, tmp_path):
        self._make_project(tmp_path, "ws-scope")
        runner = self._runner(tmp_path)
        captured: dict = {}

        async def _fake_spawn(*args, **kwargs):
            captured["env"] = kwargs.get("env")

            class _Stub:
                returncode = None
            return _Stub()

        with patch(
            "src.scripts.script_runner.asyncio.create_subprocess_exec",
            side_effect=_fake_spawn,
        ):
            await runner.execute(
                "ws-scope",
                triggered_by="test",
                workstream_short_code="WR",
                scope_readable_id="WR-003.S01",
            )

        env = captured["env"]
        assert env["CUBICLE_OUTPUT_DIR"] == str(
            tmp_path / "outputs" / "WR" / "WR-003.S01"
        )
        assert (tmp_path / "outputs" / "WR" / "WR-003.S01").is_dir()

    @pytest.mark.asyncio
    async def test_output_dir_falls_back_to_flat_when_no_workstream(
        self, tmp_path,
    ):
        """Manual UI Run on a workstream-less script gets the legacy
        flat /workspace/outputs/ — preserves the historical contract."""
        self._make_project(tmp_path, "no-ws")
        runner = self._runner(tmp_path)
        captured: dict = {}

        async def _fake_spawn(*args, **kwargs):
            captured["env"] = kwargs.get("env")

            class _Stub:
                returncode = None
            return _Stub()

        with patch(
            "src.scripts.script_runner.asyncio.create_subprocess_exec",
            side_effect=_fake_spawn,
        ):
            await runner.execute("no-ws", triggered_by="test")

        env = captured["env"]
        assert env["CUBICLE_OUTPUT_DIR"] == str(tmp_path / "outputs")


# ── from_office_secret integration ───────────────────────────────────


class TestOfficeSecretsResolution:
    """Script Runner resolves ``from_office_secret`` references at
    launch time. Missing references refuse the run and raise
    :class:`MissingOfficeSecretError` so the caller can build a
    setup_office_secret action_request."""

    def _make_project(
        self,
        workspace: Path,
        name: str,
        *,
        manifest_yaml: str,
        main_py: str = "print('ok')\n",
    ) -> Path:
        script_dir = workspace / ".scripts" / name
        script_dir.mkdir(parents=True, exist_ok=True)
        (script_dir / "script.yaml").write_text(manifest_yaml)
        (script_dir / "main.py").write_text(main_py)
        (script_dir / "variables.json").write_text("{}")
        (script_dir / ".secrets.json").write_text("{}")
        (script_dir / "executions").mkdir(exist_ok=True)
        return script_dir

    def _runner(self, workspace: Path, *, office_name: str = "Office"):
        secrets = SecretsStore(str(workspace))
        variables = VariableManager(str(workspace))
        return ScriptRunner(
            workspace_path=str(workspace),
            secrets_store=secrets,
            variable_manager=variables,
            ws_client=None,
            office_name=office_name,
        )

    @pytest.mark.asyncio
    async def test_missing_office_secret_refuses_run(
        self, tmp_path, monkeypatch,
    ):
        """Script declares ``from_office_secret: OPENAI_API_KEY`` but
        the office store doesn't have it → runner raises
        MissingOfficeSecretError BEFORE spawning the subprocess.
        Crucially: no execution directory, no log file, no status
        row are created."""
        from src.scripts.script_runner import MissingOfficeSecretError

        self._make_project(
            tmp_path,
            "needs-key",
            manifest_yaml=(
                "variables:\n"
                "  - name: OPENAI_API_KEY\n"
                "    type: string\n"
                "    from_office_secret: OPENAI_API_KEY\n"
            ),
        )
        # Empty office secrets store.
        from src.office_secrets import store as os_store
        monkeypatch.setattr(
            os_store, "read_office_secrets", lambda _: {},
        )
        # Defensive: ensure the runner's own import is the one
        # patched (it imported the function name, not the module).
        import src.scripts.script_runner as runner_mod
        monkeypatch.setattr(
            runner_mod, "read_office_secrets", lambda _: {},
        )

        runner = self._runner(tmp_path)

        spawn_calls: list = []

        async def _no_spawn(*args, **kwargs):
            spawn_calls.append((args, kwargs))

            class _Stub:
                returncode = None
            return _Stub()

        with patch(
            "src.scripts.script_runner.asyncio.create_subprocess_exec",
            side_effect=_no_spawn,
        ):
            with pytest.raises(MissingOfficeSecretError) as exc_info:
                await runner.execute("needs-key", triggered_by="test")

        assert exc_info.value.missing == ["OPENAI_API_KEY"]
        assert exc_info.value.script_name == "needs-key"
        # Subprocess MUST NOT have been launched.
        assert spawn_calls == []

    @pytest.mark.asyncio
    async def test_present_office_secret_flows_to_env(
        self, tmp_path, monkeypatch,
    ):
        """When the office store has the referenced secret, the
        value flows into the child process env as the manifest's
        declared variable name."""
        self._make_project(
            tmp_path,
            "uses-key",
            manifest_yaml=(
                "variables:\n"
                "  - name: OPENAI_KEY\n"
                "    type: string\n"
                "    from_office_secret: OPENAI_API_KEY\n"
            ),
        )

        import src.scripts.script_runner as runner_mod
        # Note: the variable name (OPENAI_KEY) intentionally differs
        # from the office secret name (OPENAI_API_KEY) to confirm
        # the runner uses the reference, not the variable name.
        monkeypatch.setattr(
            runner_mod, "read_office_secrets",
            lambda _: {"OPENAI_API_KEY": "sk-zzz-marker"},
        )

        runner = self._runner(tmp_path)
        captured: dict = {}

        async def _fake_spawn(*args, **kwargs):
            captured["env"] = kwargs.get("env")

            class _Stub:
                returncode = None
            return _Stub()

        with patch(
            "src.scripts.script_runner.asyncio.create_subprocess_exec",
            side_effect=_fake_spawn,
        ):
            await runner.execute("uses-key", triggered_by="test")

        env = captured["env"]
        assert env.get("OPENAI_KEY") == "sk-zzz-marker", (
            f"office secret not propagated to env: {env}"
        )

    @pytest.mark.asyncio
    async def test_office_secret_overrides_per_script_value(
        self, tmp_path, monkeypatch,
    ):
        """The office store wins over a stale per-script value when
        both happen to share the variable name. Documents the
        precedence specified in ScriptManifest.env_from."""
        self._make_project(
            tmp_path,
            "wins",
            manifest_yaml=(
                "variables:\n"
                "  - name: API_KEY\n"
                "    type: string\n"
                "    from_office_secret: API_KEY\n"
            ),
        )
        # Pre-existing per-script secret with a STALE value.
        (tmp_path / ".scripts" / "wins" / ".secrets.json").write_text(
            '{"API_KEY": "stale_per_script_value"}'
        )

        import src.scripts.script_runner as runner_mod
        monkeypatch.setattr(
            runner_mod, "read_office_secrets",
            lambda _: {"API_KEY": "live_office_value"},
        )

        runner = self._runner(tmp_path)
        captured: dict = {}

        async def _fake_spawn(*args, **kwargs):
            captured["env"] = kwargs.get("env")

            class _Stub:
                returncode = None
            return _Stub()

        with patch(
            "src.scripts.script_runner.asyncio.create_subprocess_exec",
            side_effect=_fake_spawn,
        ):
            await runner.execute("wins", triggered_by="test")

        env = captured["env"]
        assert env["API_KEY"] == "live_office_value"

    @pytest.mark.asyncio
    async def test_corrupt_secrets_file_raises_dedicated_error(
        self, tmp_path, monkeypatch,
    ):
        """A corrupt office secrets file should produce
        OfficeSecretsCorruptError, NOT a MissingOfficeSecretError
        per declared reference. Without this distinction, a corrupt
        file looks like every secret was deleted and the user is
        flooded with setup_office_secret cards."""
        from src.office_secrets.store import CorruptOfficeSecretsError
        from src.scripts.script_runner import OfficeSecretsCorruptError

        self._make_project(
            tmp_path,
            "corrupt-deps",
            manifest_yaml=(
                "variables:\n"
                "  - name: A_KEY\n"
                "    from_office_secret: A_KEY\n"
                "  - name: B_KEY\n"
                "    from_office_secret: B_KEY\n"
            ),
        )

        import src.scripts.script_runner as runner_mod

        def _raise_corrupt(_office_name):
            raise CorruptOfficeSecretsError(
                "office secrets file is unreadable: JSONDecodeError",
            )

        monkeypatch.setattr(
            runner_mod, "read_office_secrets", _raise_corrupt,
        )
        runner = self._runner(tmp_path)

        async def _no_spawn(*args, **kwargs):
            class _Stub:
                returncode = None
            return _Stub()

        with patch(
            "src.scripts.script_runner.asyncio.create_subprocess_exec",
            side_effect=_no_spawn,
        ):
            with pytest.raises(OfficeSecretsCorruptError) as exc_info:
                await runner.execute(
                    "corrupt-deps", triggered_by="test",
                )

        assert exc_info.value.script_name == "corrupt-deps"
        assert "JSONDecodeError" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_no_office_secret_refs_skips_disk_read(
        self, tmp_path, monkeypatch,
    ):
        """A script that doesn't reference any office secret must
        NOT trigger a read of the office secrets file — performance
        guarantee for the common case."""
        self._make_project(
            tmp_path,
            "no-refs",
            manifest_yaml=(
                "variables:\n"
                "  - name: COUNT\n"
                "    type: number\n"
                "    default: 100\n"
            ),
        )

        import src.scripts.script_runner as runner_mod
        read_calls: list = []

        def _track(office_name):
            read_calls.append(office_name)
            return {}

        monkeypatch.setattr(runner_mod, "read_office_secrets", _track)

        runner = self._runner(tmp_path)
        async def _fake_spawn(*args, **kwargs):
            class _Stub:
                returncode = None
            return _Stub()

        with patch(
            "src.scripts.script_runner.asyncio.create_subprocess_exec",
            side_effect=_fake_spawn,
        ):
            await runner.execute("no-refs", triggered_by="test")

        assert read_calls == []
