"""WebSocket message handlers for office-secret operations.

Two inbound message types from the backend's chat-WS relay:

- ``office_secret_set``: ``{name, value, description}``. We write
  the host secrets file, compute the fingerprint, and reply
  ``office_secret_added`` with the metadata for the backend to
  persist.
- ``office_secret_delete``: ``{name}``. We remove the entry from
  the host secrets file and reply ``office_secret_deleted``.

The secret value is NEVER logged. The handler doesn't
``logger.debug(msg)`` the raw message — only opaque
"handling office_secret_set for office X, name=Y" lines.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from src.config import OfficeConfig
from src.office_secrets.store import (
    OfficeSecretStoreError,
    container_path_for_office_secrets,
    delete_office_secret,
    set_office_secret,
)

logger = logging.getLogger(__name__)


SendCallback = Callable[[dict], Any]
"""Async callable that sends a JSON message over the connector WS
back to the backend."""


async def handle_office_secret_set(
    msg: dict,
    office: OfficeConfig,
    send: SendCallback,
) -> None:
    """Process an ``office_secret_set`` message and reply.

    Sends back:
    - ``office_secret_added`` on success — backend persists metadata
    - ``office_secret_error`` on failure — backend forwards to UI
    """
    name = (msg.get("name") or "").strip()
    value = msg.get("value") or ""
    desc_raw = msg.get("description")
    description = (
        desc_raw.strip() if isinstance(desc_raw, str) else None
    )

    if not name or not value:
        logger.warning(
            "office_secret_set for office %s missing required fields",
            office.id,
        )
        await send({
            "type": "office_secret_error",
            "operation": "add",
            "name": name,
            "error": "name and value are required",
        })
        return

    try:
        # office.slug, NOT office.name (2026-09-09 prod incident): the
        # store keys the host file by slugify(arg), and worker sessions
        # READ it by the pinned workspace slug (07/H-16). Writing by the
        # CURRENT name meant a renamed office's new secrets landed in a
        # stray file agents never read, while pre-rename secrets kept
        # working — exactly the "August secret arrives, today's doesn't"
        # shape.
        if name.startswith("CBCL_INPUT_"):
            from src.office_secrets.transient import set_transient_secret

            fingerprint = set_transient_secret(
                office.slug, name, value, msg.get("expires_at"),
                binding={key: msg.get(key) for key in ("task_id", "script_name", "variable_name")},
            )
        else:
            fingerprint = set_office_secret(office.slug, name, value)
    except OfficeSecretStoreError as exc:
        logger.info(
            "office_secret_set rejected for office %s (name=%s): %s",
            office.id, name, exc,
        )
        await send({
            "type": "office_secret_error",
            "operation": "add",
            "name": name,
            "error": str(exc),
        })
        return
    except Exception:
        # Note: the exception value is intentionally NOT embedded in
        # the user-facing ``error`` field. ``exc`` from an OSError /
        # PermissionError typically includes the file path, which
        # gives away the on-disk layout — and a future storage
        # backend could include the value itself. Use a constant
        # message; the stack trace lives in ``logger.exception``
        # diagnostics where the value never reaches anyway (the
        # value isn't in ``exc.args``).
        logger.exception(
            "office_secret_set failed to write for office %s (name=%s)",
            office.id, name,
        )
        await send({
            "type": "office_secret_error",
            "operation": "add",
            "name": name,
            "error": "failed to write secret to disk",
        })
        return

    logger.info(
        "office_secret_set OK for office %s (name=%s, fingerprint=%s)",
        office.id, name, fingerprint,
    )
    await send({
        "type": "office_secret_added",
        "name": name,
        "fingerprint": fingerprint,
        "description": description,
        "container_path": container_path_for_office_secrets(),
    })


async def handle_office_secret_delete(
    msg: dict,
    office: OfficeConfig,
    send: SendCallback,
) -> None:
    """Process an ``office_secret_delete`` message.

    Idempotent: deleting a missing entry is a no-op. We still reply
    ``office_secret_deleted`` so the backend's reconcile path can
    prune any straggler metadata row (e.g. host-side manual edit).
    """
    name = (msg.get("name") or "").strip()
    if not name:
        logger.warning(
            "office_secret_delete for office %s missing 'name'",
            office.id,
        )
        return

    try:
        # office.slug for the same reason as the set path above.
        delete_office_secret(office.slug, name)
    except OfficeSecretStoreError as exc:
        logger.warning(
            "office_secret_delete failed for office %s (name=%s): %s",
            office.id, name, exc,
        )
        # Still tell the backend so its metadata catches up.
    else:
        logger.info(
            "office_secret_delete OK for office %s (name=%s)",
            office.id, name,
        )

    await send({
        "type": "office_secret_deleted",
        "name": name,
        "container_path": container_path_for_office_secrets(),
    })
