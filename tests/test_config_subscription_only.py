"""Subscription-only: the daemon keeps no Claude API key anywhere.

Every Claude session runs on the office's own Claude subscription login.
``cbcl setup`` accepts no API key, ``cbcl status`` reports none, and a key a
pre-subscription-only ``cbcl setup`` stored in ``~/.cubicle/config.yaml`` is
removed from the file the next time the config is loaded.
"""

from __future__ import annotations

import logging

import pytest
import yaml
from click.testing import CliRunner

from src import cli_commands
from src import config as config_mod
from src.config import Config, load_config, save_config


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config_mod, "get_config_path", lambda: path)
    monkeypatch.delenv("CBCL_PLATFORM_URL", raising=False)
    return path


def test_stored_api_key_is_removed_from_the_config_file(config_path, caplog):
    config_path.write_text(yaml.dump({
        "platform_url": "https://app.cbcl.ai",
        "anthropic_api_key": "sk-ant-api03-synthetic",
        "security_token": "cbcl_co_x",
        "office_cpus": 6,
    }))
    with caplog.at_level(logging.WARNING, logger="src.config"):
        config = load_config()
    assert not hasattr(config, "anthropic_api_key")
    assert config.security_token == "cbcl_co_x"
    stored = yaml.safe_load(config_path.read_text())
    assert "anthropic_api_key" not in stored
    assert stored["office_cpus"] == 6
    assert stored["security_token"] == "cbcl_co_x"
    assert "subscription" in caplog.text
    assert "sk-ant-api03-synthetic" not in caplog.text


def test_empty_legacy_key_is_dropped_quietly(config_path, caplog):
    config_path.write_text(yaml.dump({
        "platform_url": "https://app.cbcl.ai",
        "anthropic_api_key": "",
        "security_token": "cbcl_co_x",
    }))
    with caplog.at_level(logging.WARNING, logger="src.config"):
        load_config()
    assert "anthropic_api_key" not in yaml.safe_load(config_path.read_text())
    assert caplog.text == ""


def test_key_removal_keeps_the_stored_platform_url_under_an_env_override(
    config_path, monkeypatch,
):
    """``CBCL_PLATFORM_URL`` overrides this process only: stripping the
    legacy key must not persist the override into ``config.yaml``."""
    monkeypatch.setenv("CBCL_PLATFORM_URL", "http://localhost:8000")
    config_path.write_text(yaml.dump({
        "platform_url": "https://app.cbcl.ai",
        "anthropic_api_key": "",
        "security_token": "cbcl_co_x",
    }))
    config = load_config()
    assert config.platform_url == "http://localhost:8000"
    stored = yaml.safe_load(config_path.read_text())
    assert stored == {
        "platform_url": "https://app.cbcl.ai",
        "security_token": "cbcl_co_x",
    }


def test_legacy_url_heal_persists_the_default_under_an_env_override(
    config_path, monkeypatch,
):
    monkeypatch.setenv("CBCL_PLATFORM_URL", "http://localhost:8000")
    config_path.write_text(yaml.dump({
        "platform_url": "http://46.224.71.1:8000",
        "security_token": "cbcl_co_x",
    }))
    config = load_config()
    assert config.platform_url == "http://localhost:8000"
    stored = yaml.safe_load(config_path.read_text())
    assert stored["platform_url"] == "https://app.cbcl.ai"


def test_save_config_never_writes_an_api_key(config_path):
    config_path.write_text(yaml.dump({"anthropic_api_key": "sk-ant-stale"}))
    save_config(Config(platform_url="https://app.cbcl.ai", security_token="t"))
    assert "anthropic_api_key" not in yaml.safe_load(config_path.read_text())


def test_setup_has_no_api_key_option():
    result = CliRunner().invoke(
        cli_commands.setup,
        ["--anthropic-api-key", "sk-ant-x", "--non-interactive"],
    )
    assert result.exit_code != 0
    assert "No such option" in result.output


def test_status_reports_no_api_key(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_commands, "config_exists", lambda: True)
    monkeypatch.setattr(
        cli_commands, "load_config",
        lambda: Config(platform_url="https://app.cbcl.ai", security_token=""),
    )
    monkeypatch.setattr(cli_commands, "get_pid_path", lambda: tmp_path / "cbcl.pid")
    monkeypatch.setattr(cli_commands, "find_running_daemon_pid", lambda: None)
    monkeypatch.setattr(cli_commands, "fetch_offices_sync", lambda url, token: [])
    monkeypatch.setattr(cli_commands, "get_logs_path", lambda: tmp_path)
    result = CliRunner().invoke(cli_commands.status)
    assert result.exit_code == 0, result.output
    assert "API key" not in result.output
