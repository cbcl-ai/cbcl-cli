"""D2 / X17 / X55 — skill secret parameters are office-scoped and host-only.

The pre-D2 store keyed skill secrets by skill name alone under the
daemon-wide ``~/.cubicle/secrets/skills/`` — a directory older daemons
also bind-mounted read-only into every office container. Two offices with the
same skill slug overwrote each other's value, and invalid names were dropped
after the API had already reported success. These tests pin the new
contract: per-office private-runtime files (0600 in 0700 directories), the
legacy file neither read nor written nor deleted, and failures that are
raised by the store and logged (never the value) by the daemon handler.
"""

from __future__ import annotations

import json
import logging
import os
import stat

import pytest

from src.dispatch import handle_skill_secret_update
from src.scripts.secrets_store import SecretsStore

OFFICE_A = "aaaaaaaa-1111-4222-8333-444444444444"
OFFICE_B = "bbbbbbbb-1111-4222-8333-444444444444"


def _store(tmp_path, office_id: str | None) -> SecretsStore:
    return SecretsStore(
        str(tmp_path / "workspace"),
        config_dir=str(tmp_path / "home"),
        office_id=office_id,
    )


def test_offices_sharing_a_skill_slug_do_not_overwrite_each_other(tmp_path):
    first = _store(tmp_path, OFFICE_A)
    second = _store(tmp_path, OFFICE_B)

    first.set_skill_secret("slack", "WEBHOOK_URL", "value-for-a")
    second.set_skill_secret("slack", "WEBHOOK_URL", "value-for-b")

    assert first.get_skill_secrets("slack") == {"WEBHOOK_URL": "value-for-a"}
    assert second.get_skill_secrets("slack") == {"WEBHOOK_URL": "value-for-b"}


def test_store_is_private_and_outside_the_legacy_secrets_tree(tmp_path):
    # T7: start from a store left world-readable (an older daemon, a manual
    # copy, a permissive umask) so the test fails unless the write itself
    # tightens the directory and the file.
    store = _store(tmp_path, OFFICE_A)
    directory = store.skill_secrets_dir()
    home = tmp_path / "home"
    assert directory == (
        home / "private-runtime" / "offices" / OFFICE_A / "skill-secrets"
    )
    directory.mkdir(parents=True)
    os.chmod(directory, 0o755)
    path = directory / "gmail.json"
    path.write_text(json.dumps({"REFRESH": "kept"}))
    os.chmod(path, 0o644)

    previous = os.umask(0)
    try:
        store.set_skill_secret("gmail", "TOKEN", "ya29")
    finally:
        os.umask(previous)

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"REFRESH": "kept", "TOKEN": "ya29"}
    assert sorted(p.name for p in directory.iterdir()) == ["gmail.json"]
    # Nothing lands in the retired daemon-wide tree.
    assert not (home / "secrets").exists()


def test_new_private_directories_ignore_a_permissive_umask(tmp_path):
    store = _store(tmp_path, OFFICE_A)
    previous = os.umask(0)
    try:
        store.set_skill_secret("gmail", "TOKEN", "ya29")
    finally:
        os.umask(previous)
    home = tmp_path / "home"
    current = store.skill_secrets_dir()
    while current != home:
        assert stat.S_IMODE(current.stat().st_mode) == 0o700, current
        current = current.parent
    path = store.skill_secrets_dir() / "gmail.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_legacy_daemon_wide_file_is_neither_read_nor_deleted(tmp_path):
    store = _store(tmp_path, OFFICE_A)
    legacy = store.legacy_skill_secrets_path("slack")
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"BOT_TOKEN": "from-another-office"}))

    assert store.get_skill_secrets("slack") == {}
    store.set_skill_secret("slack", "BOT_TOKEN", "mine")

    assert store.get_skill_secrets("slack") == {"BOT_TOKEN": "mine"}
    assert json.loads(legacy.read_text()) == {"BOT_TOKEN": "from-another-office"}


def test_store_without_office_id_refuses_skill_secrets(tmp_path):
    store = _store(tmp_path, None)
    with pytest.raises(ValueError, match="office-scoped"):
        store.set_skill_secret("slack", "TOKEN", "x")
    # Script secrets do not need an office id.
    store.set_script_secret("report", "API_KEY", "k")
    assert store.get_script_secrets("report") == {"API_KEY": "k"}


@pytest.mark.parametrize(
    ("skill", "param"),
    [("my.skill", "TOKEN"), ("slack", "API Key"), ("../x", "TOKEN"), ("", "T")],
)
def test_invalid_names_raise_and_write_nothing(tmp_path, skill, param):
    store = _store(tmp_path, OFFICE_A)
    with pytest.raises(ValueError):
        store.set_skill_secret(skill, param, "value")
    assert not (tmp_path / "home" / "private-runtime").exists()


def test_symlinked_skill_secret_directory_is_refused(tmp_path):
    store = _store(tmp_path, OFFICE_A)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    office_dir = store.skill_secrets_dir().parent
    office_dir.mkdir(parents=True)
    os.symlink(elsewhere, store.skill_secrets_dir())

    with pytest.raises(OSError, match="Unsafe skill-secret directory"):
        store.set_skill_secret("slack", "TOKEN", "value")
    assert list(elsewhere.iterdir()) == []


async def test_daemon_handler_logs_refusal_without_the_value(tmp_path, caplog):
    store = _store(tmp_path, OFFICE_A)
    caplog.set_level(logging.INFO)

    await handle_skill_secret_update(
        {"skill_name": "slack", "param_name": "API Key", "value": "s3cr3t"},
        store,
    )
    assert "NOT stored" in caplog.text
    assert "s3cr3t" not in caplog.text

    caplog.clear()
    await handle_skill_secret_update(
        {"skill_name": "slack", "param_name": "API_KEY", "value": "s3cr3t"},
        store,
    )
    assert store.get_skill_secrets("slack") == {"API_KEY": "s3cr3t"}
    assert "s3cr3t" not in caplog.text


def test_unreadable_store_is_set_aside_not_silently_overwritten(tmp_path):
    store = _store(tmp_path, OFFICE_A)
    store.set_skill_secret("slack", "TOKEN", "first")
    path = store.skill_secrets_dir() / "slack.json"
    path.write_text("{not json")

    store.set_skill_secret("slack", "OTHER", "second")

    assert store.get_skill_secrets("slack") == {"OTHER": "second"}
    backups = sorted(path.parent.glob("slack.json.corrupt-*"))
    assert [backup.read_text() for backup in backups] == ["{not json"]

    # A second corruption keeps the first backup instead of replacing it.
    path.write_text("[still not an object")
    store.set_skill_secret("slack", "THIRD", "third")

    assert store.get_skill_secrets("slack") == {"THIRD": "third"}
    backups = sorted(path.parent.glob("slack.json.corrupt-*"))
    assert sorted(backup.read_text() for backup in backups) == [
        "[still not an object",
        "{not json",
    ]
    assert all(backup.stat().st_mode & 0o777 == 0o600 for backup in backups)


def test_directory_follows_the_office_runtime_layout(tmp_path, monkeypatch):
    """B5-hygiene-10: office deletion removes ``office_runtime_dir``; the
    skill-secret directory must be derived from it, not rebuilt by hand."""
    from src import paths
    from src.office_runtime import office_runtime_dir

    monkeypatch.setattr(paths, "CUBICLE_HOME", tmp_path / "cubicle")
    store = SecretsStore(str(tmp_path / "workspace"), office_id=OFFICE_A)
    assert store.skill_secrets_dir() == office_runtime_dir(OFFICE_A) / "skill-secrets"


def test_invalid_office_id_is_a_value_error_the_handler_logs(tmp_path, caplog):
    """B5-hygiene-10: an invalid office id raised ``RuntimeStorageError``,
    which the documented ``ValueError`` contract and the dispatch handler's
    catch both missed, so it escaped into the connector read loop."""
    store = _store(tmp_path, "not-a-uuid")
    with pytest.raises(ValueError):
        store.skill_secrets_dir()
    with pytest.raises(ValueError):
        store.set_skill_secret("slack", "WEBHOOK_URL", "secret-value")

    import asyncio

    caplog.set_level(logging.ERROR)
    asyncio.run(
        handle_skill_secret_update(
            {"skill_name": "slack", "param_name": "WEBHOOK_URL", "value": "v1"},
            store,
        )
    )
    assert "NOT stored" in caplog.text
    assert "v1" not in caplog.text
