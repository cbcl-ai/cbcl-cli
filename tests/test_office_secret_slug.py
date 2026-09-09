"""Office-secret slug pinning — the 2026-09-09 production incident.

The office-secret WRITE handler used to key the host store file by
``slugify(office.name)`` while worker sessions READ it by the PINNED
workspace slug (07/H-16). After an office rename the two diverged:
newly saved secrets landed in a stray file agents never read, while
pre-rename secrets kept arriving — "the August secret works, today's
doesn't".

Pins:

* the set/delete handlers write to the PINNED-slug file even when the
  office was renamed;
* :func:`reconcile_stray_secret_file` heals a pre-fix stray file
  (merge policy, `.migrated` rename, the sibling-office guard);
* the ScriptRunner and ssh-key wiring pass the pinned slug (source
  pins — the same bug lived in three call sites).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import OfficeConfig
from src.office_secrets.store import (
    reconcile_stray_secret_file,
    set_office_secret,
)

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture
def secrets_isolated(tmp_path, monkeypatch):
    """Redirect the office-secrets store to a tmp dir (the
    ``test_office_secrets.py`` fixture, trimmed to this file's needs)."""
    office_secrets_dir = tmp_path / "office-secrets"
    office_secrets_dir.mkdir()

    def fake_office_secrets_path(slug: str):
        return office_secrets_dir / f"{slug}.json"

    from src import paths as paths_mod
    from src.office_secrets import store as store_mod

    monkeypatch.setattr(
        paths_mod, "get_office_secrets_path", fake_office_secrets_path
    )
    monkeypatch.setattr(
        store_mod, "get_office_secrets_path", fake_office_secrets_path
    )
    return office_secrets_dir


@pytest.fixture
def renamed_office():
    """An office whose display name diverged from its pinned slug —
    the exact production shape (renamed after creation)."""
    return OfficeConfig(
        id="e27c9410-7bc4-44ac-a12e-8faf092e6561",
        name="Landings Website Development Office",
        workspace_slug="landings-development-office",
    )


def _sent(messages):
    async def send(msg):
        messages.append(msg)

    return send


class TestHandlersWriteThePinnedSlugFile:
    @pytest.mark.asyncio
    async def test_set_writes_slug_file_not_name_file(
        self, secrets_isolated, renamed_office
    ):
        from src.office_secrets.handlers import handle_office_secret_set

        sent: list[dict] = []
        await handle_office_secret_set(
            {"name": "GITLAB_WEBSITE_ACCESS_TOKEN", "value": "tok"},
            renamed_office,
            _sent(sent),
        )
        assert sent and sent[0]["type"] == "office_secret_added"
        slug_file = secrets_isolated / "landings-development-office.json"
        stray_file = (
            secrets_isolated / "landings-website-development-office.json"
        )
        assert slug_file.is_file(), "secret must land in the PINNED slug file"
        assert not stray_file.exists(), (
            "writing by the current display name is the incident regression"
        )
        assert "GITLAB_WEBSITE_ACCESS_TOKEN" in json.loads(
            slug_file.read_text()
        )

    @pytest.mark.asyncio
    async def test_delete_targets_slug_file(
        self, secrets_isolated, renamed_office
    ):
        from src.office_secrets.handlers import handle_office_secret_delete

        set_office_secret(
            renamed_office.slug, "GITLAB_WEBSITE_ACCESS_TOKEN", "tok"
        )
        sent: list[dict] = []
        await handle_office_secret_delete(
            {"name": "GITLAB_WEBSITE_ACCESS_TOKEN"},
            renamed_office,
            _sent(sent),
        )
        assert sent and sent[0]["type"] == "office_secret_deleted"
        slug_file = secrets_isolated / "landings-development-office.json"
        assert json.loads(slug_file.read_text()) == {}


class TestReconcileStrayFile:
    def _write(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data))

    def test_merges_missing_keys_and_renames_stray(
        self, secrets_isolated, renamed_office
    ):
        slug_file = secrets_isolated / "landings-development-office.json"
        stray_file = (
            secrets_isolated / "landings-website-development-office.json"
        )
        self._write(slug_file, {"OLD_AUGUST_TOKEN": "aug"})
        self._write(stray_file, {"GITLAB_WEBSITE_ACCESS_TOKEN": "new"})

        merged = reconcile_stray_secret_file(
            renamed_office.name, renamed_office.slug
        )

        assert merged == ["GITLAB_WEBSITE_ACCESS_TOKEN"]
        data = json.loads(slug_file.read_text())
        assert data == {
            "OLD_AUGUST_TOKEN": "aug",
            "GITLAB_WEBSITE_ACCESS_TOKEN": "new",
        }
        assert not stray_file.exists()
        backups = list(secrets_isolated.glob("*.migrated-*.bak"))
        assert len(backups) == 1, "stray file is kept aside, never deleted"

    def test_conflict_newer_stray_wins(self, secrets_isolated, renamed_office):
        import os
        import time

        slug_file = secrets_isolated / "landings-development-office.json"
        stray_file = (
            secrets_isolated / "landings-website-development-office.json"
        )
        self._write(slug_file, {"SHARED": "old-value"})
        self._write(stray_file, {"SHARED": "resaved-after-rename"})
        # Stray strictly newer — every post-rename save went there.
        now = time.time()
        os.utime(slug_file, (now - 100, now - 100))
        os.utime(stray_file, (now, now))

        merged = reconcile_stray_secret_file(
            renamed_office.name, renamed_office.slug
        )
        assert merged == ["SHARED"]
        assert json.loads(slug_file.read_text())["SHARED"] == (
            "resaved-after-rename"
        )

    def test_sibling_office_canonical_file_is_never_touched(
        self, secrets_isolated
    ):
        """If the rename collides with ANOTHER office's slug, the
        "stray" is that office's live store — hands off."""
        office = OfficeConfig(
            id="1" * 32,
            name="Development 01",  # slugifies onto the sibling's slug
            workspace_slug="development",
        )
        sibling_file = secrets_isolated / "development-01.json"
        self._write(sibling_file, {"SIBLING_SECRET": "theirs"})

        merged = reconcile_stray_secret_file(
            office.name,
            office.slug,
            protected_slugs={"development-01"},
        )
        assert merged == []
        assert json.loads(sibling_file.read_text()) == {
            "SIBLING_SECRET": "theirs"
        }

    def test_noop_when_name_matches_slug(self, secrets_isolated):
        office = OfficeConfig(
            id="2" * 32,
            name="Presale Engagement",
            workspace_slug="presale-engagement",
        )
        assert (
            reconcile_stray_secret_file(office.name, office.slug) == []
        )

    def test_noop_when_no_stray_file(self, secrets_isolated, renamed_office):
        assert (
            reconcile_stray_secret_file(
                renamed_office.name, renamed_office.slug
            )
            == []
        )


class TestSlugWiringSourcePins:
    """The same name-vs-slug bug lived in three call sites; pin all
    three so a revert of any one fails loudly."""

    def test_script_runner_gets_the_pinned_slug(self):
        src = (SRC_ROOT / "handlers.py").read_text()
        assert "office_name=office.slug," in src, (
            "ScriptRunner must key read_office_secrets by the pinned "
            "workspace slug, not the rename-able display name"
        )

    def test_ssh_key_handlers_use_the_pinned_slug(self):
        src = (SRC_ROOT / "ssh_keys" / "handlers.py").read_text()
        assert "office.slug, name, private_key," in src
        assert "remove_key(office.slug, name" in src
        assert "write_key(\n            office.name" not in src

    def test_secret_handlers_use_the_pinned_slug(self):
        src = (SRC_ROOT / "office_secrets" / "handlers.py").read_text()
        assert "set_office_secret(office.slug, name, value)" in src
        assert "delete_office_secret(office.slug, name)" in src
        assert "set_office_secret(office.name" not in src

    def test_daemon_startup_runs_the_reconcile(self):
        src = (SRC_ROOT / "daemon.py").read_text()
        assert "reconcile_stray_secret_file(" in src

    def test_teardown_candidates_lead_with_the_pinned_slug(self):
        """A renamed office's DELETE cleanup must target the pinned
        slug's workspace + secrets file, not only the name-derived
        slug (dead-workspace accumulation)."""
        src = (SRC_ROOT / "daemon.py").read_text()
        assert 'getattr(oc, "office_slug", "")' in src
        assert "office_slug=office.slug," in src
