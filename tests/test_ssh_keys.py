"""Tests for the per-office SSH-key plumbing (fingerprint + store
+ handlers). Pins the contract with the backend: the handler
replies ``ssh_key_added`` after a write and ``ssh_key_error`` on
failure; private-key text never appears in any return value."""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.config import OfficeConfig
from src.ssh_keys.fingerprint import SshKeyParseError, compute_fingerprint
from src.ssh_keys.handlers import (
    handle_ssh_key_add,
    handle_ssh_key_delete,
)
from src.ssh_keys.store import (
    SshKeyStoreError,
    container_path_for,
    list_host_keys,
    remove_key,
    write_key,
)


# ── helpers ──────────────────────────────────────────────────────────


@pytest.fixture
def fresh_ed25519_key():
    """Generate a real ed25519 keypair on the fly and return
    (private_text, expected_fingerprint)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        key_path = Path(tmpdir) / "k"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "",
             "-C", "smoke", "-f", str(key_path), "-q"],
            check=True,
        )
        priv = key_path.read_text()
        # Standard SHA256:<base64> via ssh-keygen
        out = subprocess.run(
            ["ssh-keygen", "-lf", str(key_path.with_suffix(".pub"))],
            capture_output=True, text=True, check=True,
        )
        # "256 SHA256:<b64> smoke (ED25519)"
        fp = out.stdout.split()[1]
    return priv, fp


@pytest.fixture
def workspace_isolated(tmp_path, monkeypatch):
    """Redirect ~/.cubicle/workspaces/ to a tmp dir so the test
    can write keys without touching the user's real config."""
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    # ``get_workspace_path`` reads from ``~/.cubicle/workspaces`` via
    # cubicle_home(). Easiest hook: patch get_workspace_path to use tmp_path.
    from src import paths as paths_mod

    def fake_workspace(slug: str) -> Path:
        d = workspaces / slug
        d.mkdir(parents=True, exist_ok=True)
        return d

    monkeypatch.setattr(paths_mod, "get_workspace_path", fake_workspace)

    # Also patch the helpers in ssh_keys.store that already
    # imported get_workspace_path at module load time.
    from src.ssh_keys import store as store_mod
    monkeypatch.setattr(store_mod, "get_workspace_path", fake_workspace)

    return workspaces


# ── fingerprint ──────────────────────────────────────────────────────


class TestComputeFingerprint:
    def test_matches_ssh_keygen_for_fresh_ed25519(self, fresh_ed25519_key):
        priv, expected = fresh_ed25519_key
        result = compute_fingerprint(priv)
        assert result.fingerprint == expected
        assert result.key_type == "ssh-ed25519"
        # Public key text starts with the type token, ends after the
        # base64 blob (+ optional comment).
        assert result.public_key.startswith("ssh-ed25519 ")

    def test_empty_input_rejected(self):
        with pytest.raises(SshKeyParseError):
            compute_fingerprint("")

    def test_non_pem_rejected(self):
        with pytest.raises(SshKeyParseError, match=r"BEGIN"):
            compute_fingerprint("not a key at all")

    def test_legacy_encrypted_marker_rejected(self):
        # PEM-style encrypted key marker. ssh-keygen never even
        # gets called because we shortcut on the marker.
        body = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "Proc-Type: 4,ENCRYPTED\n"
            "DEK-Info: AES-128-CBC,xxxx\n"
            "..content..\n"
            "-----END RSA PRIVATE KEY-----\n"
        )
        with pytest.raises(SshKeyParseError, match="encrypted"):
            compute_fingerprint(body)

    def test_modern_openssh_encrypted_rejected(self, tmp_path):
        """An OpenSSH-format encrypted key (no PEM marker) is
        detected via ssh-keygen returning the passphrase error.
        Skipped if ssh-keygen isn't installed (CI without OpenSSH)."""
        key_path = tmp_path / "encrypted"
        try:
            subprocess.run(
                ["ssh-keygen", "-t", "ed25519", "-N", "secret",
                 "-C", "enc", "-f", str(key_path), "-q"],
                check=True,
            )
        except FileNotFoundError:
            pytest.skip("ssh-keygen not installed")
        priv = key_path.read_text()
        with pytest.raises(SshKeyParseError, match="encrypted|passphrase"):
            compute_fingerprint(priv)


# ── store ────────────────────────────────────────────────────────────


class TestStore:
    def test_write_creates_file_with_0600(
        self, fresh_ed25519_key, workspace_isolated,
    ):
        priv, _ = fresh_ed25519_key
        # docker exec is best-effort — patch to a no-op so the test
        # doesn't try to talk to Docker.
        with patch("src.ssh_keys.store._docker_write_inside") as exec_mock:
            container_path = write_key(
                "Test Office", "prod-key", priv, container_name=None,
            )
        host = workspace_isolated / "test-office" / "ssh-keys" / "prod-key"
        assert host.exists()
        # SSH refuses keys > 600; checking the mode here means an
        # implementation bug surfaces as a unit test failure rather
        # than as a confused "Permissions are too open" inside the
        # container.
        mode = host.stat().st_mode & 0o777
        assert mode == 0o600
        assert container_path == "/home/agent/.ssh/prod-key"
        # No container_name → docker exec NOT called.
        assert exec_mock.call_count == 0

    def test_write_chowns_key_to_agent_uid(
        self, fresh_ed25519_key, workspace_isolated,
    ):
        """The durable fix for the root:root permission bug: the host key
        file (bind-mounted into the container at /home/agent/.ssh) is
        chowned to the agent uid so the agent user can READ it — otherwise
        ``git clone git@gitlab.com:...`` fails with a permission error."""
        priv, _ = fresh_ed25519_key
        with patch("src.ssh_keys.store._docker_write_inside"), patch(
            "src.ssh_keys.store.chown_to_agent"
        ) as chown_file, patch(
            "src.ssh_keys.store.chown_tree_to_agent"
        ) as chown_tree:
            write_key("Test Office", "gitlab-key", priv, container_name=None)
        host = (
            workspace_isolated / "test-office" / "ssh-keys" / "gitlab-key"
        )
        # The key file itself is chowned to the agent uid.
        chown_file.assert_any_call(host)
        # The ssh-keys dir tree is chowned on ensure (heals stranded keys).
        assert chown_tree.call_count >= 1

    def test_write_calls_docker_exec_when_container_present(
        self, fresh_ed25519_key, workspace_isolated,
    ):
        priv, _ = fresh_ed25519_key
        with patch(
            "src.ssh_keys.store._docker_write_inside",
        ) as exec_mock:
            write_key(
                "Test Office", "k1", priv,
                container_name="cbcl-office-test-office",
            )
        assert exec_mock.call_count == 1
        args, _ = exec_mock.call_args
        # (container_name, safe_name, body)
        assert args[0] == "cbcl-office-test-office"
        assert args[1] == "k1"
        # body MUST end with newline per OpenSSH convention.
        assert args[2].endswith("\n")

    def test_remove_drops_host_file(
        self, fresh_ed25519_key, workspace_isolated,
    ):
        priv, _ = fresh_ed25519_key
        with patch("src.ssh_keys.store._docker_write_inside"):
            write_key("Office", "k", priv, container_name=None)
        host_path = (
            workspace_isolated / "office" / "ssh-keys" / "k"
        )
        assert host_path.exists()
        with patch("src.ssh_keys.store._docker_remove_inside"):
            remove_key("Office", "k", container_name=None)
        assert not host_path.exists()

    def test_remove_idempotent(self, workspace_isolated):
        # Removing a key that was never written must not raise.
        with patch("src.ssh_keys.store._docker_remove_inside"):
            remove_key(
                "Empty Office", "ghost", container_name=None,
            )

    def test_name_validation_rejects_path_traversal(
        self, workspace_isolated,
    ):
        with pytest.raises(SshKeyStoreError):
            container_path_for("../outside")
        with pytest.raises(SshKeyStoreError):
            container_path_for("a/b")
        with pytest.raises(SshKeyStoreError):
            container_path_for("")
        with pytest.raises(SshKeyStoreError):
            container_path_for(".hidden")

    def test_name_validation_allows_normal(self):
        assert container_path_for("id_ed25519") == "/home/agent/.ssh/id_ed25519"
        assert container_path_for("prod-server") == "/home/agent/.ssh/prod-server"
        assert container_path_for("my.key") == "/home/agent/.ssh/my.key"

    def test_list_host_keys_returns_filenames_only(
        self, fresh_ed25519_key, workspace_isolated,
    ):
        priv, _ = fresh_ed25519_key
        with patch("src.ssh_keys.store._docker_write_inside"):
            write_key("Office", "k1", priv, container_name=None)
            write_key("Office", "k2", priv, container_name=None)
        # Hidden tmpfile should NOT show up.
        names = list_host_keys("Office")
        assert names == ["k1", "k2"]


# ── handlers ─────────────────────────────────────────────────────────


@pytest.fixture
def office():
    return OfficeConfig(id="11111111-1111-1111-1111-111111111111", name="Office")


class TestSshKeyAddHandler:
    @pytest.mark.asyncio
    async def test_happy_path_emits_ssh_key_added(
        self, fresh_ed25519_key, workspace_isolated, office,
    ):
        priv, expected_fp = fresh_ed25519_key
        replies: list[dict] = []

        async def send(msg: dict) -> None:
            replies.append(msg)

        with patch("src.ssh_keys.store._docker_write_inside"):
            await handle_ssh_key_add(
                {
                    "name": "prod",
                    "private_key": priv,
                    "comment": "prod GH",
                },
                office, container_name=None, send=send,
            )

        assert len(replies) == 1
        msg = replies[0]
        assert msg["type"] == "ssh_key_added"
        assert msg["name"] == "prod"
        assert msg["fingerprint"] == expected_fp
        assert msg["comment"] == "prod GH"
        assert msg["container_path"] == "/home/agent/.ssh/prod"
        # The reply MUST NOT echo the private key.
        for v in msg.values():
            assert priv not in (str(v) or ""), "private_key leaked into reply"

    @pytest.mark.asyncio
    async def test_missing_fields_emit_ssh_key_error(self, office):
        replies: list[dict] = []

        async def send(msg: dict) -> None:
            replies.append(msg)

        await handle_ssh_key_add(
            {"name": "", "private_key": ""}, office,
            container_name=None, send=send,
        )
        assert replies and replies[0]["type"] == "ssh_key_error"
        assert replies[0]["operation"] == "add"

    @pytest.mark.asyncio
    async def test_invalid_key_emits_ssh_key_error(self, office):
        replies: list[dict] = []

        async def send(msg: dict) -> None:
            replies.append(msg)

        await handle_ssh_key_add(
            {"name": "bad", "private_key": "not a key"},
            office, container_name=None, send=send,
        )
        assert replies and replies[0]["type"] == "ssh_key_error"
        assert "BEGIN" in replies[0]["error"]


class TestSshKeyDeleteHandler:
    @pytest.mark.asyncio
    async def test_emits_ssh_key_deleted(self, workspace_isolated, office):
        replies: list[dict] = []

        async def send(msg: dict) -> None:
            replies.append(msg)

        with patch("src.ssh_keys.store._docker_remove_inside"):
            await handle_ssh_key_delete(
                {"name": "ghost"}, office,
                container_name=None, send=send,
            )

        assert replies[0]["type"] == "ssh_key_deleted"
        assert replies[0]["name"] == "ghost"
        assert replies[0]["container_path"] == "/home/agent/.ssh/ghost"

    @pytest.mark.asyncio
    async def test_missing_name_does_not_send(self, office):
        replies: list[dict] = []

        async def send(msg: dict) -> None:
            replies.append(msg)

        await handle_ssh_key_delete(
            {}, office, container_name=None, send=send,
        )
        assert replies == []
