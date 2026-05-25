"""Tests for the foreground-PID-file fix + /proc daemon discovery.

Two things together unblock the operator who runs ``cbcl start`` in
the foreground (the default) and later wants to ``cbcl status`` /
``cbcl stop`` from a different shell:

1. ``_start_foreground`` must write a PID file (it didn't before
   v0.2.8 — only ``_start_daemon`` did).
2. ``find_running_daemon_pid`` is the fallback for daemons started
   by a PRE-v0.2.8 cbcl, which run with no PID file. It scans
   ``/proc`` for the canonical cbcl argv signature so ``cbcl
   status`` / ``cbcl stop`` keep working across the upgrade.

These tests are Linux-only because the fallback is /proc-based.
Skip on macOS so the suite is portable.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from src.daemon import find_running_daemon_pid


pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="/proc-based scan is Linux-only by design",
)


class TestFindRunningDaemonPid:
    """The /proc scan must match real cbcl daemons and ignore noise."""

    def test_matches_real_daemon_argv(self, tmp_path):
        # Fake /proc layout: one PID with a cbcl-shaped cmdline.
        proc = tmp_path / "proc"
        proc.mkdir()
        target_pid = 99999
        target_dir = proc / str(target_pid)
        target_dir.mkdir()
        # NUL-separated argv as the kernel exposes it.
        argv = b"\x00".join([
            b"/root/.local/share/pipx/venvs/cubicle-communicator/bin/python",
            b"/root/.local/bin/cbcl",
            b"start",
        ]) + b"\x00"
        (target_dir / "cmdline").write_bytes(argv)

        with patch("src.daemon.Path", lambda p: proc if p == "/proc" else Path(p)):
            assert find_running_daemon_pid() == target_pid

    def test_returns_none_when_no_match(self, tmp_path):
        # /proc has other processes but none look like cbcl.
        proc = tmp_path / "proc"
        proc.mkdir()
        for pid, argv in (
            (1, b"/sbin/init\x00"),
            (1000, b"/usr/bin/sshd\x00-D\x00"),
            (2000, b"/usr/bin/python\x00random_script.py\x00"),
        ):
            d = proc / str(pid)
            d.mkdir()
            (d / "cmdline").write_bytes(argv)

        with patch("src.daemon.Path", lambda p: proc if p == "/proc" else Path(p)):
            assert find_running_daemon_pid() is None

    def test_skips_self_so_status_doesnt_match_itself(self, tmp_path):
        # Edge case the operator hits if they run "cbcl status" and
        # we accidentally find OUR OWN process (cbcl status also has
        # "cbcl" + "status" in argv — different subcommand but the
        # bare-cbcl check still triggers if we're not careful).
        proc = tmp_path / "proc"
        proc.mkdir()
        self_dir = proc / str(os.getpid())
        self_dir.mkdir()
        # Pretend we ARE a running cbcl start (which we obviously
        # aren't, but the test exercises the skip-self guard).
        (self_dir / "cmdline").write_bytes(
            b"/usr/bin/python\x00/usr/local/bin/cbcl\x00start\x00"
        )

        with patch("src.daemon.Path", lambda p: proc if p == "/proc" else Path(p)):
            assert find_running_daemon_pid() is None

    def test_ignores_grep_cbcl_and_other_substring_matches(self, tmp_path):
        # The literal "cbcl" appears in many unrelated commands —
        # ``grep cbcl``, ``vim cbcl.log``, ``cat cbcl_overview.md``.
        # The argv[1].endswith("/cbcl") check filters these out.
        proc = tmp_path / "proc"
        proc.mkdir()
        for pid, argv in (
            (3000, b"grep\x00--color=auto\x00cbcl\x00"),
            (3001, b"vim\x00cbcl_deployment.md\x00"),
            (3002, b"cat\x00/var/log/cbcl_overview.txt\x00"),
        ):
            d = proc / str(pid)
            d.mkdir()
            (d / "cmdline").write_bytes(argv)

        with patch("src.daemon.Path", lambda p: proc if p == "/proc" else Path(p)):
            assert find_running_daemon_pid() is None

    def test_matches_cbcl_start_not_cbcl_status(self, tmp_path):
        # Only "start" subcommand counts as a daemon — other
        # subcommands (status / stop / setup / logs) are one-shots
        # and shouldn't be treated as the daemon process.
        proc = tmp_path / "proc"
        proc.mkdir()
        for pid, argv in (
            (4000, b"/usr/bin/python\x00/root/.local/bin/cbcl\x00status\x00"),
            (4001, b"/usr/bin/python\x00/root/.local/bin/cbcl\x00setup\x00"),
            (4002, b"/usr/bin/python\x00/root/.local/bin/cbcl\x00logs\x00-f\x00"),
        ):
            d = proc / str(pid)
            d.mkdir()
            (d / "cmdline").write_bytes(argv)

        with patch("src.daemon.Path", lambda p: proc if p == "/proc" else Path(p)):
            assert find_running_daemon_pid() is None

    def test_returns_none_when_proc_missing(self, tmp_path):
        # macOS / Windows / chroot env — /proc may not exist.
        missing = tmp_path / "definitely-not-proc"
        with patch("src.daemon.Path", lambda p: missing if p == "/proc" else Path(p)):
            assert find_running_daemon_pid() is None

    def test_skips_pid_dirs_that_vanish_mid_scan(self, tmp_path):
        # /proc/<pid>/cmdline may disappear between iterdir() and
        # read_bytes() if the process exits — we should swallow the
        # OSError and keep going, not crash with EBADF.
        proc = tmp_path / "proc"
        proc.mkdir()
        # One vanished entry (dir exists, no cmdline file) followed
        # by a real match.
        vanished = proc / "5000"
        vanished.mkdir()
        # No cmdline file written — read_bytes will raise OSError.

        target = proc / "5001"
        target.mkdir()
        (target / "cmdline").write_bytes(
            b"/usr/bin/python\x00/root/.local/bin/cbcl\x00start\x00"
        )

        with patch("src.daemon.Path", lambda p: proc if p == "/proc" else Path(p)):
            assert find_running_daemon_pid() == 5001
