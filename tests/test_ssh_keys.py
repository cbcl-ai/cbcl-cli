"""Tests for the per-office SSH-key plumbing (fingerprint + store
+ handlers). Pins the contract with the backend: the handler
replies ``ssh_key_added`` after a write and ``ssh_key_error`` on
failure; private-key text never appears in any return value."""
from __future__ import annotations

import subprocess
import shutil
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

OFFICE_ID = "11111111-1111-1111-1111-111111111111"


# ── helpers ──────────────────────────────────────────────────────────


@pytest.fixture
def fresh_ed25519_key():
    """Generate a real ed25519 keypair on the fly and return
    (private_text, expected_fingerprint)."""
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen is not installed in this test image")
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
def stored_key():
    return "synthetic private key bytes", "synthetic-fingerprint"


@pytest.fixture
def workspace_isolated(tmp_path, monkeypatch):
    from src import office_runtime, paths

    monkeypatch.setattr(paths, "CUBICLE_HOME", tmp_path / "home")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with office_runtime.runtime_lock(OFFICE_ID):
        office_runtime.prepare_runtime(OFFICE_ID, workspace)
    return office_runtime.ssh_keys_dir(OFFICE_ID)


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
    def test_write_creates_file_with_0600(self, stored_key, workspace_isolated):
        private_key, _ = stored_key
        result = write_key(OFFICE_ID, "prod-key", private_key, container_name=None)
        host = workspace_isolated / "prod-key"
        assert host.exists()
        assert host.stat().st_mode & 0o777 == 0o600
        assert result == "/home/agent/.ssh/prod-key"

    def test_write_chowns_staged_key_to_agent_uid(self, stored_key, workspace_isolated):
        private_key, _ = stored_key
        with patch("src.ssh_keys.store.chown_to_agent") as chown:
            write_key(OFFICE_ID, "gitlab-key", private_key, container_name=None)
        chown.assert_any_call(workspace_isolated)
        assert any(call.args[0].name.startswith(".key-") for call in chown.call_args_list)
        assert (workspace_isolated / "gitlab-key").read_text().endswith("\n")

    def test_live_bind_mount_does_not_need_docker_exec(self, stored_key, workspace_isolated):
        private_key, _ = stored_key
        with patch("subprocess.run") as execute:
            write_key(OFFICE_ID, "k1", private_key, container_name="office-container")
        execute.assert_not_called()
        assert (workspace_isolated / "k1").read_text().endswith("\n")

    def test_remove_drops_host_file(self, stored_key, workspace_isolated):
        private_key, _ = stored_key
        write_key(OFFICE_ID, "key", private_key, container_name=None)
        remove_key(OFFICE_ID, "key", container_name=None)
        assert not (workspace_isolated / "key").exists()

    def test_remove_idempotent(self, workspace_isolated):
        remove_key(OFFICE_ID, "ghost", container_name=None)

    @pytest.mark.parametrize("name", ["../outside", "a/b", "", ".hidden"])
    def test_name_validation_rejects_path_traversal(self, name):
        with pytest.raises(SshKeyStoreError):
            container_path_for(name)

    def test_name_validation_allows_normal(self):
        assert container_path_for("id_ed25519") == "/home/agent/.ssh/id_ed25519"
        assert container_path_for("prod-server") == "/home/agent/.ssh/prod-server"
        assert container_path_for("my.key") == "/home/agent/.ssh/my.key"

    def test_list_host_keys_returns_filenames_only(self, stored_key, workspace_isolated):
        private_key, _ = stored_key
        write_key(OFFICE_ID, "k1", private_key, container_name=None)
        write_key(OFFICE_ID, "k2", private_key, container_name=None)
        assert list_host_keys(OFFICE_ID) == ["k1", "k2"]

    def test_missing_runtime_never_writes_legacy_workspace(self, workspace_isolated):
        with pytest.raises(SshKeyStoreError, match="unavailable"):
            write_key("22222222-2222-2222-2222-222222222222", "key", "synthetic", container_name=None)


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

        with patch("src.ssh_keys.store.chown_to_agent"):
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

        with patch("src.ssh_keys.store.chown_to_agent"):
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
