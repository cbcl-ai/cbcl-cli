"""Tests for the per-office shared-secret plumbing (store + handlers).

Pins the contract with the backend: the handler replies
``office_secret_added`` after a write and ``office_secret_error`` on
failure; the secret value never appears in any return value, log
line, or stored metadata.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from src.config import OfficeConfig
from src.office_secrets.handlers import (
    handle_office_secret_delete,
    handle_office_secret_set,
)
from src.office_secrets.store import (
    OfficeSecretStoreError,
    delete_office_secret,
    fingerprint_value,
    list_office_secret_names,
    read_office_secrets,
    set_office_secret,
)


# ── helpers ──────────────────────────────────────────────────────────


@pytest.fixture
def workspace_isolated(tmp_path, monkeypatch):
    """Redirect ``~/.cubicle/office-secrets/`` to a tmp dir so the
    test can write the secrets file without touching the user's real
    config.

    Critically: the office-secrets dir must live OUTSIDE the tmp
    workspaces dir, mirroring production layout. If the test ever
    saw the file under a "workspaces/<slug>/" path it would mask
    the very security boundary the feature is meant to enforce.

    Returns the office-secrets dir so the per-test assertions can
    point at ``<office-secrets-dir>/<slug>.json`` directly.
    """
    office_secrets_dir = tmp_path / "office-secrets"
    office_secrets_dir.mkdir()
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()

    def fake_office_secrets_path(slug: str):
        return office_secrets_dir / f"{slug}.json"

    def fake_workspace(slug: str) -> Path:
        d = workspaces / slug
        d.mkdir(parents=True, exist_ok=True)
        return d

    from src import paths as paths_mod
    monkeypatch.setattr(
        paths_mod, "get_office_secrets_path", fake_office_secrets_path,
    )
    monkeypatch.setattr(paths_mod, "get_workspace_path", fake_workspace)
    from src.office_secrets import store as store_mod
    monkeypatch.setattr(
        store_mod, "get_office_secrets_path", fake_office_secrets_path,
    )

    return office_secrets_dir


@pytest.fixture
def office():
    return OfficeConfig(
        id="11111111-1111-1111-1111-111111111111", name="Office",
    )


# ── store: fingerprint + file ────────────────────────────────────────


class TestFingerprint:
    def test_deterministic_first_16_hex(self):
        value = "sk-abc123"
        assert fingerprint_value(value) == hashlib.sha256(
            value.encode(),
        ).hexdigest()[:16]

    def test_different_values_yield_different_fingerprints(self):
        assert fingerprint_value("a") != fingerprint_value("b")


class TestStore:
    def test_set_writes_file_with_0600(self, workspace_isolated):
        set_office_secret("Office", "API_KEY", "sk-abc123")
        # workspace_isolated is the office-secrets dir; file is keyed
        # by slugified office name.
        secrets_file = workspace_isolated / "office.json"
        assert secrets_file.exists()
        # 0600 — the file holds raw secret values; SSH-style strict
        # permissions are the bouncer against a misconfigured home
        # dir.
        mode = secrets_file.stat().st_mode & 0o777
        assert mode == 0o600
        assert json.loads(secrets_file.read_text()) == {
            "API_KEY": "sk-abc123",
        }

    def test_set_replaces_existing_value(self, workspace_isolated):
        set_office_secret("Office", "API_KEY", "first")
        fp = set_office_secret("Office", "API_KEY", "second")
        assert fp == fingerprint_value("second")
        assert read_office_secrets("Office") == {"API_KEY": "second"}

    def test_multiple_secrets_coexist(self, workspace_isolated):
        set_office_secret("Office", "A", "1")
        set_office_secret("Office", "B", "2")
        set_office_secret("Office", "C", "3")
        assert read_office_secrets("Office") == {
            "A": "1", "B": "2", "C": "3",
        }
        assert list_office_secret_names("Office") == ["A", "B", "C"]

    def test_delete_removes_entry(self, workspace_isolated):
        set_office_secret("Office", "A", "1")
        set_office_secret("Office", "B", "2")
        delete_office_secret("Office", "A")
        assert read_office_secrets("Office") == {"B": "2"}

    def test_delete_idempotent(self, workspace_isolated):
        # Deleting a key that doesn't exist must NOT raise.
        delete_office_secret("Office", "GHOST")
        # Empty file (or no file) should still produce an empty map.
        assert read_office_secrets("Office") == {}

    def test_name_rejects_lowercase(self, workspace_isolated):
        with pytest.raises(OfficeSecretStoreError):
            set_office_secret("Office", "api_key", "x")

    def test_name_rejects_leading_digit(self, workspace_isolated):
        with pytest.raises(OfficeSecretStoreError):
            set_office_secret("Office", "1API", "x")

    def test_name_rejects_invalid_chars(self, workspace_isolated):
        with pytest.raises(OfficeSecretStoreError):
            set_office_secret("Office", "API-KEY", "x")
        with pytest.raises(OfficeSecretStoreError):
            set_office_secret("Office", "A.B", "x")

    def test_name_rejects_path_traversal(self, workspace_isolated):
        # Even though the file is one shared JSON, defense-in-depth
        # against a future "one file per key" refactor.
        with pytest.raises(OfficeSecretStoreError):
            set_office_secret("Office", "../etc", "x")

    def test_empty_value_rejected(self, workspace_isolated):
        with pytest.raises(OfficeSecretStoreError):
            set_office_secret("Office", "API_KEY", "")

    def test_file_lives_outside_workspace_bind_mount(
        self, workspace_isolated, tmp_path,
    ):
        """SECURITY: the host secrets file MUST NOT live anywhere
        under the workspace directory. The workspace is bind-mounted
        into every agent container at ``/workspace`` (read-write),
        so any file inside it is readable by every agent via the
        Read tool. This test pins the "office secret values never
        reach the container" invariant — if it fails, the threat
        model is broken regardless of any in-container MCP refusals.
        """
        from src.office_secrets.store import host_secrets_path_for_office
        from src.paths import get_workspace_path, slugify

        set_office_secret("Office", "API_KEY", "sentinel-value")
        secrets_file = host_secrets_path_for_office("Office")
        workspace_dir = get_workspace_path(slugify("Office"))

        # Resolve both to absolute paths to defeat any symlink games.
        secrets_abs = secrets_file.resolve()
        workspace_abs = workspace_dir.resolve()
        # Use relative_to() — it RAISES when ``secrets_abs`` is not
        # under ``workspace_abs``. The negative assertion is exactly
        # what we want.
        with pytest.raises(ValueError):
            secrets_abs.relative_to(workspace_abs)

    def test_corrupt_file_raises_in_strict_mode(
        self, workspace_isolated,
    ):
        """The runner-facing reader (strict) must distinguish a
        corrupt file from an absent secret. Without the distinction,
        a corrupt file looks like every secret was deleted and the
        user gets a flood of setup_office_secret cards."""
        from src.office_secrets.store import (
            CorruptOfficeSecretsError,
            read_office_secrets,
        )

        # Seed a valid secret first so the file exists. Use a
        # value marker unlikely to appear in any tmpdir path.
        marker = "secret_marker_yzqw_1234"
        set_office_secret("Office", "API_KEY", marker)
        secrets_file = workspace_isolated / "office.json"
        # Corrupt the file mid-flight.
        secrets_file.write_text("{not json")
        with pytest.raises(CorruptOfficeSecretsError) as exc_info:
            read_office_secrets("Office")
        # Error message must not include the value — that would be
        # a leak path if the strict reader ever returned the file
        # content in the exception.
        assert marker not in str(exc_info.value)

    def test_corrupt_file_returns_empty_in_lenient_mode(
        self, workspace_isolated,
    ):
        """Set/delete paths use lenient mode — a corrupt file
        becomes an empty baseline so the next write atomically
        replaces it. (Set must not crash on a corrupt prior file.)"""
        # Pre-corrupt the file.
        secrets_file = workspace_isolated / "office.json"
        secrets_file.write_text("garbage")
        # set_office_secret should succeed and overwrite.
        fp = set_office_secret("Office", "RECOVERED", "v")
        assert fp == fingerprint_value("v")
        # File now has only the new entry.
        from src.office_secrets.store import read_office_secrets
        assert read_office_secrets("Office") == {"RECOVERED": "v"}

    def test_atomic_write_no_partial_file_on_crash(
        self, workspace_isolated, monkeypatch,
    ):
        """If the file write raises mid-flight, the existing file
        must remain at its old contents — the new value MUST NOT be
        observable as a partial blob."""
        set_office_secret("Office", "API_KEY", "good")
        secrets_file = workspace_isolated / "office.json"
        original = secrets_file.read_text()

        # Force os.replace to fail AFTER a tempfile has been written.
        def _boom(*a, **k):
            raise OSError("simulated rename failure")

        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(OfficeSecretStoreError):
            set_office_secret("Office", "API_KEY", "bad_value")

        # Original file untouched.
        assert secrets_file.read_text() == original


# ── handlers ─────────────────────────────────────────────────────────


class TestOfficeSecretSetHandler:
    @pytest.mark.asyncio
    async def test_happy_path_emits_office_secret_added(
        self, workspace_isolated, office,
    ):
        replies: list[dict] = []

        async def send(msg: dict) -> None:
            replies.append(msg)

        await handle_office_secret_set(
            {
                "name": "OPENAI_API_KEY",
                "value": "sk-abc123",
                "description": "OpenAI key",
            },
            office, send=send,
        )

        assert len(replies) == 1
        msg = replies[0]
        assert msg["type"] == "office_secret_added"
        assert msg["name"] == "OPENAI_API_KEY"
        assert msg["fingerprint"] == fingerprint_value("sk-abc123")
        assert msg["description"] == "OpenAI key"
        # The file is NOT bind-mounted into the container; the
        # metadata field carries a ``host-only:`` marker so any UI
        # surface that renders it can show the "value never enters
        # the container filesystem" hint instead of a misleading
        # path.
        assert msg["container_path"] == "host-only:office-secrets"
        # The reply MUST NOT echo the secret value.
        for v in msg.values():
            assert "sk-abc123" not in str(v), (
                "secret value leaked into reply"
            )

    @pytest.mark.asyncio
    async def test_missing_fields_emit_office_secret_error(self, office):
        replies: list[dict] = []

        async def send(msg: dict) -> None:
            replies.append(msg)

        await handle_office_secret_set(
            {"name": "", "value": ""}, office, send=send,
        )
        assert replies and replies[0]["type"] == "office_secret_error"
        assert replies[0]["operation"] == "add"

    @pytest.mark.asyncio
    async def test_invalid_name_emits_office_secret_error(
        self, workspace_isolated, office,
    ):
        replies: list[dict] = []

        async def send(msg: dict) -> None:
            replies.append(msg)

        await handle_office_secret_set(
            {"name": "lowercase_bad", "value": "x"}, office, send=send,
        )
        assert replies and replies[0]["type"] == "office_secret_error"
        assert "invalid" in replies[0]["error"]


class TestOfficeSecretDeleteHandler:
    @pytest.mark.asyncio
    async def test_emits_office_secret_deleted(
        self, workspace_isolated, office,
    ):
        replies: list[dict] = []

        async def send(msg: dict) -> None:
            replies.append(msg)

        # Seed an entry, then delete.
        set_office_secret("Office", "API_KEY", "x")
        await handle_office_secret_delete(
            {"name": "API_KEY"}, office, send=send,
        )
        assert replies[0]["type"] == "office_secret_deleted"
        assert replies[0]["name"] == "API_KEY"
        assert read_office_secrets("Office") == {}

    @pytest.mark.asyncio
    async def test_delete_missing_still_replies(
        self, workspace_isolated, office,
    ):
        replies: list[dict] = []

        async def send(msg: dict) -> None:
            replies.append(msg)

        await handle_office_secret_delete(
            {"name": "NEVER_EXISTED"}, office, send=send,
        )
        # Reply emitted so backend can reconcile its metadata.
        assert replies[0]["type"] == "office_secret_deleted"


# ── log-leak guard ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_does_not_log_value(
    workspace_isolated, office, caplog,
):
    """Successful AND failing set paths must NOT emit a log line
    containing the secret value or the JSON encoding of the
    persisted map (which embeds the value).

    Strengthened in the F18 review: previously this only checked
    for the raw value substring, but a future change that
    serialised the full mapping into a log line would still escape
    detection because JSON-encoding might mangle the marker string
    if it contained quotes. We check both the raw marker AND the
    JSON-encoded form."""
    import json
    caplog.set_level(logging.DEBUG)

    secret_value = "marker_value_does_not_leak_99zzz"
    replies: list[dict] = []

    async def send(msg: dict) -> None:
        replies.append(msg)

    # Happy path.
    await handle_office_secret_set(
        {
            "name": "MARKER",
            "value": secret_value,
            "description": "leak test",
        },
        office, send=send,
    )
    json_form = json.dumps({"MARKER": secret_value})
    for record in caplog.records:
        msg = record.getMessage()
        assert secret_value not in msg, (
            f"raw secret value leaked into log: {msg!r}"
        )
        assert json_form not in msg, (
            f"JSON-encoded secret leaked into log: {msg!r}"
        )

    # Failure path — force the write to fail so the exception path
    # runs. The handler MUST NOT include exc.args (which could
    # contain the path) in the user-facing error frame, and the
    # exception-logging path still must not carry the value.
    caplog.clear()
    from unittest.mock import patch
    with patch(
        "src.office_secrets.store._atomic_write",
        side_effect=OSError("/some/path/details {0}".format(secret_value)),
    ):
        replies.clear()
        await handle_office_secret_set(
            {
                "name": "FAIL_MARKER",
                "value": secret_value,
                "description": "fail",
            },
            office, send=send,
        )
    # User-facing frame is a constant string — no exc-derived data
    # (the OSError message we injected carried the path AND the
    # value; neither should leak to the user).
    assert replies and replies[0]["type"] == "office_secret_error"
    assert replies[0]["error"] == "failed to write office secret to disk"
    # And nothing in the captured logs carries the value either.
    for record in caplog.records:
        assert secret_value not in record.getMessage()
