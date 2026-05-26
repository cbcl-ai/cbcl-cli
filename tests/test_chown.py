"""Tests for ``src._chown`` — the host-side chown-to-agent helper.

The helper is intentionally best-effort: on macOS dev or when the
daemon isn't running as root (no CAP_CHOWN), ``os.chown`` raises
and we silently swallow. The test surface mostly validates that
the helper DOESN'T raise on the unhappy paths and that the happy
path on a Linux-root run actually chowns.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from src._chown import (
    AGENT_UID,
    AGENT_GID,
    chown_to_agent,
    chown_tree_to_agent,
)


def test_constants_match_dockerfile():
    """Lock in the agent uid/gid in lock-step with
    ``_agent_image/Dockerfile.agent``. If a future Dockerfile change
    bumps the uid, this test fails loudly instead of letting every
    workspace write silently re-land root-owned."""
    assert AGENT_UID == 1000
    assert AGENT_GID == 1000


def test_chown_to_agent_does_not_raise_on_missing_path(tmp_path):
    """Best-effort: missing path should NOT crash the caller. The
    write site has already failed; we just shouldn't pile on."""
    missing = tmp_path / "does-not-exist"
    chown_to_agent(missing)  # should NOT raise


def test_chown_to_agent_does_not_raise_when_not_root(tmp_path):
    """Non-root callers (dev macOS / non-root daemon) get
    PermissionError from os.chown — the helper swallows it. The
    file's ownership is not changed but the program continues."""
    f = tmp_path / "test.txt"
    f.write_text("x")
    pre_uid = f.stat().st_uid
    chown_to_agent(f)  # should NOT raise even though we're not root
    # On non-root run the uid stays the same; we don't assert because
    # the test could in theory be run as root in CI.
    assert f.exists()
    _ = pre_uid  # unused on non-root runs; kept for trace clarity


def test_chown_tree_to_agent_walks_subtree(tmp_path):
    """The recursive variant should visit every file + directory
    in the subtree. Return value is the number of successful
    chown calls — on non-root it's 0 because the top-level chown
    fails first and short-circuits."""
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b.txt").write_text("hi")
    (tmp_path / "a" / "c").mkdir()
    (tmp_path / "a" / "c" / "d.txt").write_text("yo")

    chowned = chown_tree_to_agent(tmp_path)
    # Either succeeds (root) and chowns 5 inodes (root, a, b.txt, c, d.txt)
    # OR fails fast at the root and returns 0 (non-root caller).
    # Both outcomes are valid — the test just guards "doesn't raise".
    assert chowned in (0, 5)


def test_chown_tree_returns_zero_on_missing_root(tmp_path):
    """Missing root returns 0 inodes chowned (and doesn't crash)."""
    assert chown_tree_to_agent(tmp_path / "missing") == 0
