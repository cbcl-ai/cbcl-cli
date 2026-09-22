"""Canonical decision input shared by Manager rendering and backend receipt tests.

No model, IO or runtime dependency. A preview must never become a decision input.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

ACTION_REQUEST_INLINE_LIMIT = 12_000


def serialize_action_request_input(justification: str, payload: Any) -> str:
    return json.dumps(
        {"justification": justification, "payload": payload},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )


def action_request_input_digest(justification: str, payload: Any) -> str:
    return hashlib.sha256(
        serialize_action_request_input(justification, payload).encode("utf-8")
    ).hexdigest()
