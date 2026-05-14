"""WebSocket message handlers for SSH-key operations.

Two inbound message types from the backend's chat-WS relay:

- ``ssh_key_add``: ``{name, private_key, comment}``. We fingerprint
  the key, write the host file + the in-container file, and reply
  ``ssh_key_added`` with the metadata for the backend to persist.
- ``ssh_key_delete``: ``{name}``. We remove both the host file and
  the in-container file, and reply ``ssh_key_deleted``.

The private_key value is NEVER logged. The handler doesn't
``logger.debug(msg)`` the raw message — only opaque "handling
ssh_key_add for office X, name=Y" lines.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from src.config import OfficeConfig
from src.ssh_keys.fingerprint import SshKeyParseError, compute_fingerprint
from src.ssh_keys.store import (
    SshKeyStoreError,
    container_path_for,
    remove_key,
    write_key,
)

logger = logging.getLogger(__name__)


SendCallback = Callable[[dict], Any]
"""Async callable that sends a JSON message over the connector WS
back to the backend."""


async def handle_ssh_key_add(
    msg: dict,
    office: OfficeConfig,
    container_name: str | None,
    send: SendCallback,
) -> None:
    """Process an ``ssh_key_add`` message and reply with the result.

    Sends back to the backend (via the connector WS):
    - ``ssh_key_added`` on success — backend persists metadata
    - ``ssh_key_error`` on failure — backend forwards to the UI as
      a toast / inline error
    """
    name = (msg.get("name") or "").strip()
    private_key = msg.get("private_key") or ""
    comment_raw = msg.get("comment")
    comment = comment_raw.strip() if isinstance(comment_raw, str) else None

    if not name or not private_key:
        logger.warning(
            "ssh_key_add for office %s missing required fields",
            office.id,
        )
        await send({
            "type": "ssh_key_error",
            "operation": "add",
            "name": name,
            "error": "name and private_key are required",
        })
        return

    try:
        fp = compute_fingerprint(private_key)
    except SshKeyParseError as exc:
        logger.info(
            "ssh_key_add rejected for office %s (name=%s): %s",
            office.id, name, exc,
        )
        await send({
            "type": "ssh_key_error",
            "operation": "add",
            "name": name,
            "error": str(exc),
        })
        return

    try:
        in_container_path = write_key(
            office.name, name, private_key,
            container_name=container_name,
        )
    except SshKeyStoreError as exc:
        logger.exception(
            "ssh_key_add failed to write for office %s (name=%s)",
            office.id, name,
        )
        await send({
            "type": "ssh_key_error",
            "operation": "add",
            "name": name,
            "error": f"failed to write key: {exc}",
        })
        return

    logger.info(
        "ssh_key_add OK for office %s (name=%s, type=%s, fingerprint=%s)",
        office.id, name, fp.key_type, fp.fingerprint,
    )

    await send({
        "type": "ssh_key_added",
        "name": name,
        "fingerprint": fp.fingerprint,
        "comment": comment,
        "container_path": in_container_path,
    })


async def handle_ssh_key_delete(
    msg: dict,
    office: OfficeConfig,
    container_name: str | None,
    send: SendCallback,
) -> None:
    """Process an ``ssh_key_delete`` message.

    Deletes the host file and the in-container file. Replies
    ``ssh_key_deleted`` so the backend can prune any straggler
    metadata row (e.g. on host-side manual deletes that bypassed
    the API). Idempotent.
    """
    name = (msg.get("name") or "").strip()
    if not name:
        logger.warning(
            "ssh_key_delete for office %s missing 'name'", office.id,
        )
        return

    try:
        remove_key(office.name, name, container_name=container_name)
    except SshKeyStoreError as exc:
        logger.warning(
            "ssh_key_delete failed for office %s (name=%s): %s",
            office.id, name, exc,
        )
        # Still tell the backend so its metadata catches up.
    else:
        logger.info(
            "ssh_key_delete OK for office %s (name=%s)",
            office.id, name,
        )

    # Tell the backend the canonical container path that's gone so
    # it can broadcast a refresh to chat clients.
    try:
        path_hint = container_path_for(name)
    except SshKeyStoreError:
        path_hint = ""

    await send({
        "type": "ssh_key_deleted",
        "name": name,
        "container_path": path_hint,
    })
