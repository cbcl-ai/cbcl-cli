"""Office-container resource limits — config parsing + container-create wiring.

Covers ``src.config.get_office_resource_limits`` (the ``office_cpus`` /
``office_memory`` keys in ``~/.cubicle/config.yaml`` and their
``CBCL_OFFICE_CPUS`` / ``CBCL_OFFICE_MEMORY`` env overrides) and the
``ContainerManager.start_office`` create path that applies them as the
Docker ``mem_limit`` / ``cpu_period`` / ``cpu_quota`` kwargs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from src import config as config_mod
from src.config import (
    DEFAULT_OFFICE_CPUS,
    DEFAULT_OFFICE_MEMORY,
    Config,
    OfficeConfig,
    OfficeResourceLimits,
    _office_from_payload,
    get_office_resource_limits,
    resolve_office_resource_limits,
    save_config,
)
from src.docker.container_manager import CPU_PERIOD_US, ContainerManager


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    """Point the config module at a per-test config.yaml (absent by
    default) and guarantee no resource-limit env vars leak in."""
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config_mod, "get_config_path", lambda: path)
    monkeypatch.delenv("CBCL_OFFICE_CPUS", raising=False)
    monkeypatch.delenv("CBCL_OFFICE_MEMORY", raising=False)
    return path


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.dump(data))


# ─── config parsing ─────────────────────────────────────────────────


class TestDefaults:
    def test_no_file_no_env_yields_defaults(self, config_path):
        limits = get_office_resource_limits()
        assert limits.cpus == DEFAULT_OFFICE_CPUS == 4.0
        assert limits.memory == DEFAULT_OFFICE_MEMORY == "8g"

    def test_file_without_keys_yields_defaults(self, config_path):
        _write_yaml(config_path, {"platform_url": "https://app.cbcl.ai"})
        limits = get_office_resource_limits()
        assert limits.cpus == 4.0
        assert limits.memory == "8g"

    def test_unparseable_file_yields_defaults_not_crash(self, config_path):
        config_path.write_text(":: not yaml ::\n\t{{{{")
        limits = get_office_resource_limits()
        assert limits.cpus == 4.0
        assert limits.memory == "8g"

    def test_non_dict_yaml_yields_defaults(self, config_path):
        config_path.write_text("- just\n- a\n- list\n")
        limits = get_office_resource_limits()
        assert limits.cpus == 4.0
        assert limits.memory == "8g"


class TestYamlValues:
    def test_yaml_values_read(self, config_path):
        _write_yaml(config_path, {"office_cpus": 8, "office_memory": "16g"})
        limits = get_office_resource_limits()
        assert limits.cpus == 8.0
        assert limits.memory == "16g"

    def test_yaml_float_cpus(self, config_path):
        _write_yaml(config_path, {"office_cpus": 2.5})
        assert get_office_resource_limits().cpus == 2.5

    def test_yaml_string_cpus_accepted(self, config_path):
        _write_yaml(config_path, {"office_cpus": "6"})
        assert get_office_resource_limits().cpus == 6.0

    def test_memory_megabytes(self, config_path):
        _write_yaml(config_path, {"office_memory": "512m"})
        assert get_office_resource_limits().memory == "512m"

    def test_memory_uppercase_normalised(self, config_path):
        _write_yaml(config_path, {"office_memory": "16G"})
        assert get_office_resource_limits().memory == "16g"


class TestEnvOverrides:
    def test_env_beats_yaml(self, config_path, monkeypatch):
        _write_yaml(config_path, {"office_cpus": 2, "office_memory": "4g"})
        monkeypatch.setenv("CBCL_OFFICE_CPUS", "12")
        monkeypatch.setenv("CBCL_OFFICE_MEMORY", "24g")
        limits = get_office_resource_limits()
        assert limits.cpus == 12.0
        assert limits.memory == "24g"

    def test_env_only_no_file(self, config_path, monkeypatch):
        monkeypatch.setenv("CBCL_OFFICE_CPUS", "1.5")
        monkeypatch.setenv("CBCL_OFFICE_MEMORY", "2g")
        limits = get_office_resource_limits()
        assert limits.cpus == 1.5
        assert limits.memory == "2g"

    def test_invalid_env_falls_back_to_default_not_yaml(
        self, config_path, monkeypatch, caplog,
    ):
        """An operator who set the env var meant to override the file
        — a broken env value falls to the DEFAULT (spec'd behaviour),
        never silently to the YAML value."""
        _write_yaml(config_path, {"office_cpus": 2, "office_memory": "4g"})
        monkeypatch.setenv("CBCL_OFFICE_CPUS", "lots")
        monkeypatch.setenv("CBCL_OFFICE_MEMORY", "8gb")
        with caplog.at_level(logging.WARNING, logger="src.config"):
            limits = get_office_resource_limits()
        assert limits.cpus == DEFAULT_OFFICE_CPUS
        assert limits.memory == DEFAULT_OFFICE_MEMORY
        assert "CBCL_OFFICE_CPUS" in caplog.text
        assert "CBCL_OFFICE_MEMORY" in caplog.text


class TestInvalidFallbacks:
    @pytest.mark.parametrize("bad", ["abc", "", None, True, [4]])
    def test_invalid_cpus_warn_and_default(self, config_path, caplog, bad):
        _write_yaml(config_path, {"office_cpus": bad})
        with caplog.at_level(logging.WARNING, logger="src.config"):
            limits = get_office_resource_limits()
        assert limits.cpus == DEFAULT_OFFICE_CPUS
        assert "office_cpus" in caplog.text

    @pytest.mark.parametrize("bad", [0, 0.5, 65, 1000, -4])
    def test_out_of_range_cpus_warn_and_default(self, config_path, caplog, bad):
        _write_yaml(config_path, {"office_cpus": bad})
        with caplog.at_level(logging.WARNING, logger="src.config"):
            limits = get_office_resource_limits()
        assert limits.cpus == DEFAULT_OFFICE_CPUS
        assert "out of range" in caplog.text

    def test_cpus_bounds_are_inclusive(self, config_path):
        _write_yaml(config_path, {"office_cpus": 1})
        assert get_office_resource_limits().cpus == 1.0
        _write_yaml(config_path, {"office_cpus": 64})
        assert get_office_resource_limits().cpus == 64.0

    @pytest.mark.parametrize(
        "bad", ["8gb", "8", "g8", "abc", "", None, True, "1.5g", "8 g"],
    )
    def test_invalid_memory_warn_and_default(self, config_path, caplog, bad):
        _write_yaml(config_path, {"office_memory": bad})
        with caplog.at_level(logging.WARNING, logger="src.config"):
            limits = get_office_resource_limits()
        assert limits.memory == DEFAULT_OFFICE_MEMORY
        assert "office_memory" in caplog.text

    def test_one_invalid_key_does_not_poison_the_other(
        self, config_path, caplog,
    ):
        _write_yaml(config_path, {"office_cpus": "junk", "office_memory": "32g"})
        with caplog.at_level(logging.WARNING, logger="src.config"):
            limits = get_office_resource_limits()
        assert limits.cpus == DEFAULT_OFFICE_CPUS
        assert limits.memory == "32g"


class TestSaveConfigPreservesResourceKeys:
    def test_save_config_round_trips_unmanaged_keys(self, config_path):
        """``cbcl setup`` re-runs (and the legacy-URL auto-heal) call
        ``save_config`` — hand-edited office_cpus/office_memory (and
        redis_url) must survive the rewrite."""
        _write_yaml(config_path, {
            "platform_url": "https://old.example",
            "office_cpus": 8,
            "office_memory": "16g",
            "redis_url": "redis://localhost:6379/0",
        })
        save_config(Config(
            platform_url="https://app.cbcl.ai",
            anthropic_api_key="",
            security_token="cbcl_co_x",
        ))
        data = yaml.safe_load(config_path.read_text())
        assert data["platform_url"] == "https://app.cbcl.ai"
        assert data["security_token"] == "cbcl_co_x"
        assert data["office_cpus"] == 8
        assert data["office_memory"] == "16g"
        assert data["redis_url"] == "redis://localhost:6379/0"


# ─── per-office override precedence ─────────────────────────────────


class TestResolvePerOfficePrecedence:
    """``resolve_office_resource_limits`` — per-office backend value
    beats the whole host-global chain (env → yaml → defaults),
    per key, independently."""

    def test_no_overrides_equals_host_chain(self, config_path):
        _write_yaml(config_path, {"office_cpus": 6, "office_memory": "12g"})
        limits = resolve_office_resource_limits(None, None)
        assert limits == OfficeResourceLimits(cpus=6.0, memory="12g")

    def test_office_values_beat_yaml(self, config_path):
        _write_yaml(config_path, {"office_cpus": 6, "office_memory": "12g"})
        limits = resolve_office_resource_limits(2, "2g")
        assert limits == OfficeResourceLimits(cpus=2.0, memory="2g")

    def test_office_values_beat_env(self, config_path, monkeypatch):
        monkeypatch.setenv("CBCL_OFFICE_CPUS", "12")
        monkeypatch.setenv("CBCL_OFFICE_MEMORY", "24g")
        limits = resolve_office_resource_limits(8, "16g")
        assert limits == OfficeResourceLimits(cpus=8.0, memory="16g")

    def test_per_key_independence(self, config_path):
        """Only one key overridden per office — the other falls to
        the host chain."""
        _write_yaml(config_path, {"office_cpus": 6, "office_memory": "12g"})
        limits = resolve_office_resource_limits(2, None)
        assert limits == OfficeResourceLimits(cpus=2.0, memory="12g")
        limits = resolve_office_resource_limits(None, "2g")
        assert limits == OfficeResourceLimits(cpus=6.0, memory="2g")

    def test_no_overrides_no_config_yields_defaults(self, config_path):
        limits = resolve_office_resource_limits()
        assert limits == OfficeResourceLimits(
            cpus=DEFAULT_OFFICE_CPUS, memory=DEFAULT_OFFICE_MEMORY,
        )

    @pytest.mark.parametrize("bad_cpus", ["abc", 0, 65, True, [4]])
    def test_invalid_office_cpus_falls_to_host_chain(
        self, config_path, caplog, bad_cpus,
    ):
        _write_yaml(config_path, {"office_cpus": 6})
        with caplog.at_level(logging.WARNING, logger="src.config"):
            limits = resolve_office_resource_limits(bad_cpus, None)
        assert limits.cpus == 6.0  # host chain, NOT the built-in default
        assert "container_cpus" in caplog.text

    @pytest.mark.parametrize("bad_mem", ["8gb", "8", "1.5g", True])
    def test_invalid_office_memory_falls_to_host_chain(
        self, config_path, caplog, bad_mem,
    ):
        _write_yaml(config_path, {"office_memory": "12g"})
        with caplog.at_level(logging.WARNING, logger="src.config"):
            limits = resolve_office_resource_limits(None, bad_mem)
        assert limits.memory == "12g"
        assert "container_memory" in caplog.text

    def test_string_and_uppercase_office_values_coerced(self, config_path):
        limits = resolve_office_resource_limits("2.5", "16G")
        assert limits == OfficeResourceLimits(cpus=2.5, memory="16g")


class TestDiscoveryPayloadParsing:
    """``_office_from_payload`` — per-office limits ride the discovery
    payload; absent/invalid values degrade to ``None`` (no override)."""

    def test_fields_absent_yield_none(self):
        office = _office_from_payload({"id": "oid-1", "name": "Test"})
        assert office.container_cpus is None
        assert office.container_memory is None

    def test_valid_fields_parsed(self):
        office = _office_from_payload({
            "id": "oid-1", "name": "Test",
            "container_cpus": 8, "container_memory": "16g",
        })
        assert office.container_cpus == 8.0
        assert office.container_memory == "16g"

    def test_null_fields_yield_none_without_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="src.config"):
            office = _office_from_payload({
                "id": "oid-1", "name": "Test",
                "container_cpus": None, "container_memory": None,
            })
        assert office.container_cpus is None
        assert office.container_memory is None
        assert "container_cpus" not in caplog.text

    def test_invalid_fields_warn_and_yield_none(self, caplog):
        with caplog.at_level(logging.WARNING, logger="src.config"):
            office = _office_from_payload({
                "id": "oid-1", "name": "Test",
                "container_cpus": "lots", "container_memory": "8gb",
            })
        assert office.container_cpus is None
        assert office.container_memory is None
        assert "office_cpus" in caplog.text
        assert "office_memory" in caplog.text


# ─── container-create wiring ────────────────────────────────────────


class TestStartOfficeAppliesLimits:
    """``start_office`` must pass the resolved limits to
    ``containers.run`` as mem_limit / cpu_period / cpu_quota, with
    ``cpu_quota = int(cpus * CPU_PERIOD_US)``."""

    def _make_cm(self, monkeypatch):
        import docker.errors

        cm = ContainerManager(use_docker=True)
        run_kwargs: dict = {}

        fake_container = MagicMock()
        fake_container.id = "cid-full"
        fake_container.short_id = "cid"

        class _FakeContainers:
            def get(self, name):
                raise docker.errors.NotFound("no such container")

            def run(self, image, **kwargs):
                run_kwargs.update(kwargs)
                return fake_container

        class _FakeClient:
            containers = _FakeContainers()

        monkeypatch.setattr(cm, "_get_client", lambda: _FakeClient())
        return cm, run_kwargs

    @pytest.mark.asyncio
    async def test_default_limits_applied(
        self, config_path, monkeypatch, tmp_path,
    ):
        cm, run_kwargs = self._make_cm(monkeypatch)
        await cm.start_office(
            office_slug="test", office_id="oid-1",
            workspace_path=str(tmp_path / "ws"),
        )
        assert run_kwargs["mem_limit"] == "8g"
        assert run_kwargs["cpu_period"] == CPU_PERIOD_US == 100_000
        assert run_kwargs["cpu_quota"] == 400_000  # 4 CPUs

    @pytest.mark.asyncio
    async def test_configured_limits_applied(
        self, config_path, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("CBCL_OFFICE_CPUS", "8")
        monkeypatch.setenv("CBCL_OFFICE_MEMORY", "16g")
        cm, run_kwargs = self._make_cm(monkeypatch)
        await cm.start_office(
            office_slug="test", office_id="oid-1",
            workspace_path=str(tmp_path / "ws"),
        )
        assert run_kwargs["mem_limit"] == "16g"
        assert run_kwargs["cpu_quota"] == int(8 * CPU_PERIOD_US) == 800_000
        assert run_kwargs["cpu_period"] == CPU_PERIOD_US

    @pytest.mark.asyncio
    async def test_fractional_cpus_quota_truncated_to_int(
        self, config_path, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("CBCL_OFFICE_CPUS", "2.5")
        cm, run_kwargs = self._make_cm(monkeypatch)
        await cm.start_office(
            office_slug="test", office_id="oid-1",
            workspace_path=str(tmp_path / "ws"),
        )
        assert run_kwargs["cpu_quota"] == 250_000
        assert isinstance(run_kwargs["cpu_quota"], int)

    @pytest.mark.asyncio
    async def test_yaml_limits_applied_from_config_file(
        self, config_path, monkeypatch, tmp_path,
    ):
        _write_yaml(config_path, {"office_cpus": 6, "office_memory": "12g"})
        cm, run_kwargs = self._make_cm(monkeypatch)
        await cm.start_office(
            office_slug="test", office_id="oid-1",
            workspace_path=str(tmp_path / "ws"),
        )
        assert run_kwargs["mem_limit"] == "12g"
        assert run_kwargs["cpu_quota"] == 600_000

    @pytest.mark.asyncio
    async def test_per_office_overrides_beat_env_and_yaml(
        self, config_path, monkeypatch, tmp_path,
    ):
        _write_yaml(config_path, {"office_cpus": 6, "office_memory": "12g"})
        monkeypatch.setenv("CBCL_OFFICE_CPUS", "10")
        monkeypatch.setenv("CBCL_OFFICE_MEMORY", "20g")
        cm, run_kwargs = self._make_cm(monkeypatch)
        await cm.start_office(
            office_slug="test", office_id="oid-1",
            workspace_path=str(tmp_path / "ws"),
            container_cpus=2.0, container_memory="2g",
        )
        assert run_kwargs["mem_limit"] == "2g"
        assert run_kwargs["cpu_quota"] == 200_000

    @pytest.mark.asyncio
    async def test_ensure_container_threads_per_office_limits(
        self, config_path, monkeypatch, tmp_path,
    ):
        cm, run_kwargs = self._make_cm(monkeypatch)
        office = OfficeConfig(
            id="oid-1", name="test",
            container_cpus=3.0, container_memory="6g",
        )
        monkeypatch.setattr(
            OfficeConfig, "workspace_path",
            property(lambda self: str(tmp_path / "ws")),
        )
        await cm.ensure_container(office)
        assert run_kwargs["mem_limit"] == "6g"
        assert run_kwargs["cpu_quota"] == 300_000


class TestRecreateOffice:
    """``recreate_office`` — force-remove by NAME, then a full
    ``start_office`` with the office's complete config (mounts +
    per-office limits) so a recreate can never drop settings."""

    @pytest.mark.asyncio
    async def test_removes_existing_and_recreates_with_full_config(
        self, config_path, monkeypatch, tmp_path,
    ):
        from unittest.mock import AsyncMock

        cm = ContainerManager(use_docker=True)
        removed: list[bool] = []

        fake_existing = MagicMock()
        fake_existing.remove = lambda force=False: removed.append(force)

        class _FakeContainers:
            def get(self, name):
                assert name == "cbcl-office-test"
                return fake_existing

        class _FakeClient:
            containers = _FakeContainers()

        monkeypatch.setattr(cm, "_get_client", lambda: _FakeClient())
        monkeypatch.setattr(
            OfficeConfig, "workspace_path",
            property(lambda self: str(tmp_path / "ws")),
        )
        start_mock = AsyncMock(return_value="new-cid")
        monkeypatch.setattr(cm, "start_office", start_mock)
        cm._containers["oid-1"] = fake_existing

        office = OfficeConfig(
            id="oid-1", name="test",
            extra_mounts=[{
                "host_path": "/h", "container_path": "/c",
                "read_only": True,
            }],
            container_cpus=8.0, container_memory="16g",
        )
        result = await cm.recreate_office(office)

        assert result == "new-cid"
        assert removed == [True]  # force-removed
        assert "oid-1" not in cm._containers
        start_mock.assert_awaited_once_with(
            office_slug="test",
            office_id="oid-1",
            workspace_path=str(tmp_path / "ws"),
            extra_mounts=office.extra_mounts,
            container_cpus=8.0,
            container_memory="16g",
        )

    @pytest.mark.asyncio
    async def test_missing_container_still_recreates(
        self, config_path, monkeypatch, tmp_path,
    ):
        import docker.errors
        from unittest.mock import AsyncMock

        cm = ContainerManager(use_docker=True)

        class _FakeContainers:
            def get(self, name):
                raise docker.errors.NotFound("gone")

        class _FakeClient:
            containers = _FakeContainers()

        monkeypatch.setattr(cm, "_get_client", lambda: _FakeClient())
        monkeypatch.setattr(
            OfficeConfig, "workspace_path",
            property(lambda self: str(tmp_path / "ws")),
        )
        start_mock = AsyncMock(return_value="new-cid")
        monkeypatch.setattr(cm, "start_office", start_mock)

        office = OfficeConfig(id="oid-1", name="test")
        result = await cm.recreate_office(office)
        assert result == "new-cid"
        start_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_docker_disabled_is_noop(self, config_path):
        cm = ContainerManager(use_docker=False)
        office = OfficeConfig(id="oid-1", name="test")
        assert await cm.recreate_office(office) is None
