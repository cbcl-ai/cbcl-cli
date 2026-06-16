"""T8.2.2 — office bring-up is parallel + connecting-set deduped."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src import daemon as d


def _office(oid):
    return SimpleNamespace(id=oid, name=f"office-{oid}")


@pytest.mark.asyncio
async def test_spawn_dedups_in_flight(monkeypatch):
    connecting = {"o1"}
    connected: dict = {}
    bg: list = []
    called = []

    async def _fake_connect(office, *a, **k):
        called.append(office.id)

    monkeypatch.setattr(d, "_connect_office_process_model", _fake_connect)
    d._spawn_office_connect(_office("o1"), None, None, None, connected, bg, connecting=connecting)
    d._spawn_office_connect(_office("o2"), None, None, None, connected, bg, connecting=connecting)
    await asyncio.gather(*bg)
    assert called == ["o2"]


@pytest.mark.asyncio
async def test_slow_office_does_not_block_fast_one(monkeypatch):
    connecting: set = set()
    connected: dict = {}
    bg: list = []
    order = []

    async def _fake_connect(office, *a, **k):
        if office.id == "slow":
            await asyncio.sleep(0.05)
        order.append(office.id)

    monkeypatch.setattr(d, "_connect_office_process_model", _fake_connect)
    monkeypatch.setattr(d, "_office_connect_sem", asyncio.Semaphore(3))
    d._spawn_office_connect(_office("slow"), None, None, None, connected, bg, connecting=connecting)
    d._spawn_office_connect(_office("fast"), None, None, None, connected, bg, connecting=connecting)
    await asyncio.gather(*bg)
    assert order == ["fast", "slow"]
    assert connecting == set()
