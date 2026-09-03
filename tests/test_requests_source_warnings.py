"""Pins the daemon→backend RPC threading for instruction-sources-v2.

Review finding #26: the backend route tests pin their own fake
``call_generator``, so nothing pinned the ACTUAL daemon-side response
shape — the 3-tuple unpack from the generators, the ``source_warnings``
wire key beside ``instructions``/``changes`` (and beside
``context_notes``/``changes``), and the ``workspace_path`` kwarg that
enables the pre-survey zip expansion. A rename on either side would
otherwise only surface on a live daemon.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src._handlers._requests import dispatch_backend_request


def _harness(monkeypatch, action_module_attr: str, result: tuple):
    """Fake router + generator returning ``result`` as the 3-tuple."""
    sent: list[dict] = []

    async def _send(frame: dict) -> None:
        sent.append(frame)

    router = SimpleNamespace(ws_client=SimpleNamespace(send=_send))
    office = SimpleNamespace(workspace_path="/tmp/ws-under-test")
    captured_kwargs: dict = {}

    async def _fake_generator(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return result

    import src.setup_generator as sg

    monkeypatch.setattr(sg, action_module_attr, _fake_generator)
    return router, office, sent, captured_kwargs


@pytest.mark.asyncio
async def test_office_instructions_response_carries_source_warnings(
    monkeypatch,
) -> None:
    router, office, sent, kwargs = _harness(
        monkeypatch,
        "generate_office_instructions",
        ("# Doc", ["changed X"], ["quoter.xlsx: studied by filename only."]),
    )
    await dispatch_backend_request(
        {
            "request_id": "req-1",
            "action": "generate_office_instructions",
            "params": {"directive": "improve it", "office_name": "Acme"},
        },
        router=router,
        fs_handler=None,
        office=office,
        redis_client=None,
        container_name="cbcl-office-acme",
    )
    assert len(sent) == 1
    data = sent[0]["data"]
    assert data["instructions"] == "# Doc"
    assert data["changes"] == ["changed X"]
    assert data["source_warnings"] == [
        "quoter.xlsx: studied by filename only."
    ]
    # The HOST workspace root reaches the generator — without it the
    # pre-survey zip expansion silently no-ops.
    assert kwargs.get("workspace_path") == "/tmp/ws-under-test"


@pytest.mark.asyncio
async def test_workstream_context_response_carries_source_warnings(
    monkeypatch,
) -> None:
    router, office, sent, kwargs = _harness(
        monkeypatch,
        "generate_workstream_context_note",
        ("notes body", [], ["framework.zip: nothing extractable inside."]),
    )
    await dispatch_backend_request(
        {
            "request_id": "req-2",
            "action": "generate_workstream_context",
            "params": {
                "workstream_name": "WS",
                "brief": "a brief long enough to pass",
            },
        },
        router=router,
        fs_handler=None,
        office=office,
        redis_client=None,
        container_name="cbcl-office-acme",
    )
    assert len(sent) == 1
    data = sent[0]["data"]
    assert data["context_notes"] == "notes body"
    assert data["source_warnings"] == [
        "framework.zip: nothing extractable inside."
    ]
    assert kwargs.get("workspace_path") == "/tmp/ws-under-test"
