"""Shared fixtures for live (real-API) prompt regression evals (P6.1).

Skip every test in this folder when ``ANTHROPIC_API_KEY`` is not set
so a fresh clone still passes the default test run without leaking
keys or burning quota.
"""
from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config, items):
    """Auto-skip every live_eval test when the API key is absent."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    skip_marker = pytest.mark.skip(
        reason="ANTHROPIC_API_KEY not set — live evals skipped"
    )
    for item in items:
        if "live_eval" in item.keywords:
            item.add_marker(skip_marker)
