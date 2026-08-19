"""The daemon refuses extra_mounts that RESOLVE into ~/.cubicle (07/H-14).

Two validators guard extra mounts and, until this change, both missed the
same thing.

The backend's is lexical — it never touches the operator's filesystem, so
it cannot see that ``/srv/data`` is a symlink into ``~/.cubicle``. Its
docstring said the daemon caught that "at mount time". No such check
existed. So a symlink planted anywhere the backend considered innocent
would mount the office-secrets tree straight into an agent container,
handing every agent in that office every office secret.

(The backend half was ALSO measuring the wrong machine: it built its
sensitive-prefix list from ``expanduser("~")``, which resolves inside the
BACKEND container, not on the host where the mount happens. Fixed
separately — see ``backend/app/offices/schemas.py``.)
"""
from __future__ import annotations

import logging

import pytest

from src.docker.container_manager import _apply_extra_mounts


def _mount(host: str, container: str = "/data") -> dict:
    return {"host_path": host, "container_path": container, "read_only": True}


@pytest.fixture
def fake_cubicle_home(tmp_path, monkeypatch):
    """A stand-in ~/.cubicle with a secret in it, plus a sibling data dir."""
    home = tmp_path / ".cubicle"
    (home / "office-secrets").mkdir(parents=True)
    (home / "office-secrets" / "acme.json").write_text('{"API_KEY":"s3cret"}')
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr("src.docker.container_manager.CUBICLE_HOME", home)
    return home, data


def test_symlink_into_secrets_is_refused(fake_cubicle_home, tmp_path, caplog):
    """THE ATTACK: an innocent-looking path that is really a symlink into
    the secrets tree. Lexically it contains no '..' and no '.cubicle', so
    the backend accepts it — this is the only layer that can see through."""
    home, _ = fake_cubicle_home
    link = tmp_path / "innocent-data"
    link.symlink_to(home / "office-secrets")

    volumes: dict[str, dict] = {}
    with caplog.at_level(logging.WARNING):
        _apply_extra_mounts(volumes, [_mount(str(link))], "cbcl-office-test")

    assert volumes == {}, "the secrets tree must never reach Docker"
    assert any("Cubicle config/secrets tree" in r.message for r in caplog.records)


def test_symlink_to_cubicle_root_is_refused(fake_cubicle_home, tmp_path, caplog):
    home, _ = fake_cubicle_home
    link = tmp_path / "backup"
    link.symlink_to(home)

    volumes: dict[str, dict] = {}
    with caplog.at_level(logging.WARNING):
        _apply_extra_mounts(volumes, [_mount(str(link))], "cbcl-office-test")

    assert volumes == {}


def test_path_containing_the_secrets_tree_is_refused(
    fake_cubicle_home, tmp_path, caplog,
):
    """Mounting the PARENT exposes ~/.cubicle as a subtree — same result."""
    volumes: dict[str, dict] = {}
    with caplog.at_level(logging.WARNING):
        _apply_extra_mounts(volumes, [_mount(str(tmp_path))], "cbcl-office-test")

    assert volumes == {}


def test_legitimate_directory_still_mounts(fake_cubicle_home):
    """The guard must not break ordinary mounts — a real sibling directory
    with no relationship to the secrets tree is still accepted."""
    _, data = fake_cubicle_home
    volumes: dict[str, dict] = {}
    _apply_extra_mounts(volumes, [_mount(str(data))], "cbcl-office-test")

    assert str(data) in volumes
    assert volumes[str(data)]["bind"] == "/data"


def test_symlink_to_a_legitimate_directory_still_mounts(
    fake_cubicle_home, tmp_path,
):
    """Symlinks are not banned — only ones resolving somewhere sensitive."""
    _, data = fake_cubicle_home
    link = tmp_path / "data-link"
    link.symlink_to(data)

    volumes: dict[str, dict] = {}
    _apply_extra_mounts(volumes, [_mount(str(link))], "cbcl-office-test")

    assert str(link) in volumes
