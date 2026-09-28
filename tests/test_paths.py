"""Tests for the centralized paths module."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.paths import (
    CUBICLE_HOME,
    get_config_path,
    get_credentials_path,
    get_logs_path,
    get_pid_path,
    get_workspace_path,
    is_safe_agent_name,
    slugify,
)


class TestSlugify:
    """Tests for the slugify helper."""

    def test_simple_name(self):
        assert slugify("My Office") == "my-office"

    def test_special_characters(self):
        assert slugify("Recruitment & Hiring!") == "recruitment-hiring"

    def test_trailing_leading_hyphens(self):
        assert slugify("  --My Office--  ") == "my-office"

    def test_empty_name(self):
        assert slugify("") == "office"

    def test_unicode(self):
        result = slugify("café")
        assert result  # Should not be empty

    def test_numbers(self):
        assert slugify("Office 42") == "office-42"

    def test_single_word(self):
        assert slugify("Recruitment") == "recruitment"


class TestPathResolution:
    """Tests for path resolution functions."""

    def test_cubicle_home_is_under_user_home(self):
        assert CUBICLE_HOME == Path.home() / ".cubicle"

    def test_config_path(self):
        assert get_config_path() == CUBICLE_HOME / "config.yaml"

    def test_credentials_path(self):
        assert get_credentials_path() == CUBICLE_HOME / "credentials.env"

    def test_pid_path(self):
        assert get_pid_path() == CUBICLE_HOME / "communicator.pid"

    def test_workspace_path_creates_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.paths.CUBICLE_HOME", tmp_path)
        path = get_workspace_path("my-office")
        assert path == tmp_path / "workspaces" / "my-office"
        assert path.is_dir()

    def test_startup_keeps_the_retired_secrets_root(self, tmp_path, monkeypatch):
        """The stopped-backup plan requires ~/.cubicle/secrets to exist
        (B4-hygiene-10: only the unused accessor was removed)."""
        from src.paths import ensure_cubicle_dirs

        monkeypatch.setattr("src.paths.CUBICLE_HOME", tmp_path)
        ensure_cubicle_dirs()
        assert (tmp_path / "secrets").is_dir()

    def test_logs_path_creates_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.paths.CUBICLE_HOME", tmp_path)
        path = get_logs_path()
        assert path == tmp_path / "logs"
        assert path.is_dir()


class TestOfficeConfigWorkspacePath:
    """Tests that OfficeConfig.workspace_path is correctly derived."""

    def test_workspace_path_computed_from_name(self):
        from src.config import OfficeConfig
        office = OfficeConfig(id="test-id", name="Recruitment Office")
        assert "recruitment-office" in office.workspace_path
        assert ".cubicle/workspaces/recruitment-office" in office.workspace_path


class TestSafeAgentName:
    """07/H-13: an agent name must be one plain directory entry (B7b-tests-03)."""

    @pytest.mark.parametrize(
        "name",
        ["", " ", ".", "..", ".hidden", "-x", "~x", "a/b", "a\\b", "a\x00b"],
    )
    def test_unsafe_names_are_refused(self, name: str) -> None:
        assert not is_safe_agent_name(name)

    @pytest.mark.parametrize("name", ["analyst", "python-developer", "a.b"])
    def test_plain_names_are_accepted(self, name: str) -> None:
        assert is_safe_agent_name(name)
