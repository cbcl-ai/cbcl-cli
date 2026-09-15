"""Standalone installer runtime, executed in the same kernel as pip.

The stable lock inode is never unlinked. Pip inherits its descriptor so a
disconnected client or killed wrapper cannot release a still-running pip's
lock. This is cooperative process ownership, not a hostile-code sandbox.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

LOCK_FORMAT = "cbcl-deps-install-v1"
MARKER_ENV = "CUBICLE_WORKER_EXECUTION_ID"


def requirements_digest(requirements_file: Path) -> str:
    return hashlib.sha256(requirements_file.read_bytes()).hexdigest()


def cache_valid(requirements_file: Path, stamp: Path) -> bool:
    try:
        receipt = json.loads(stamp.read_text())
        return (
            isinstance(receipt, dict)
            and receipt.get("requirements_sha256") == requirements_digest(requirements_file)
        )
    except (OSError, ValueError):
        return False


def marked_processes_present(marker: str) -> bool:
    if not re.fullmatch(r"[0-9a-f]{64}", marker):
        return True
    if not Path("/proc/self/stat").exists():
        return True
    expected = f"{MARKER_ENV}={marker}".encode()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if entry.stat().st_uid != os.geteuid():
                continue
            state = (entry / "stat").read_bytes().rsplit(b")", 1)[1].split()[0]
            if state in (b"Z", b"X"):
                continue
            if expected in (entry / "environ").read_bytes().split(b"\0"):
                return True
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            return True
    return False


def _write_record(descriptor: int, record: dict) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.ftruncate(descriptor, 0)
    encoded = json.dumps(record, sort_keys=True).encode()
    while encoded:
        written = os.write(descriptor, encoded)
        if written <= 0:
            raise OSError("Cannot persist dependency install ownership")
        encoded = encoded[written:]
    os.fsync(descriptor)


def _read_record(descriptor: int) -> dict:
    os.lseek(descriptor, 0, os.SEEK_SET)
    raw = os.read(descriptor, 16385)
    if not raw:
        return {}
    try:
        record = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(
            "Legacy or incomplete dependency lock requires verified reconciliation; "
            "age is not proof of cleanup"
        ) from exc
    if not isinstance(record, dict) or record.get("format") != LOCK_FORMAT:
        raise RuntimeError("Unknown dependency lock requires verified reconciliation")
    return record


def _stop_group(process: subprocess.Popen) -> None:
    for stop_signal in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, stop_signal)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            continue
    if process.poll() is None:
        raise RuntimeError("Dependency process termination is unconfirmed")


def install(
    requirements_file: Path, deps_dir: Path, *, marker: str,
    install_timeout: float, lock_timeout: float,
) -> None:
    import fcntl

    if not re.fullmatch(r"[0-9a-f]{64}", marker):
        raise ValueError("A valid dependency execution marker is required")
    deps_dir.mkdir(parents=True, exist_ok=True)
    lock_path = deps_dir / ".installing.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError("Dependency lock must be a regular file")
        deadline = time.monotonic() + lock_timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Timed out waiting for the current dependency installer; "
                        "its lock remains intact"
                    )
                time.sleep(0.1)
        previous = _read_record(descriptor)
        if previous and previous.get("state") not in {
            "running", "uncertain", "failed", "complete",
        }:
            raise RuntimeError("Unknown dependency install state requires verified reconciliation")
        if (
            previous.get("state") in {"running", "uncertain"}
            and marked_processes_present(str(previous.get("marker") or ""))
        ):
            raise RuntimeError("Previous dependency processes require verified reconciliation")
        stamp = deps_dir / ".installed_at"
        if previous.get("state") == "complete" and cache_valid(requirements_file, stamp):
            return
        digest = requirements_digest(requirements_file)
        receipt = {
            "format": LOCK_FORMAT, "state": "running", "marker": marker,
            "requirements_sha256": digest,
        }
        stamp.unlink(missing_ok=True)
        _write_record(descriptor, receipt)
        command = [
            sys.executable, "-m", "pip", "install", "--no-input",
            "--disable-pip-version-check", "--no-warn-script-location", "--upgrade",
            "--target", str(deps_dir), "-r", str(requirements_file),
        ]
        process = None
        try:
            process = subprocess.Popen(command, start_new_session=True, pass_fds=(descriptor,))
            try:
                result = process.wait(timeout=install_timeout)
            except subprocess.TimeoutExpired as exc:
                _stop_group(process)
                raise RuntimeError("Dependency installation timed out") from exc
            if result:
                raise RuntimeError(f"pip install failed (exit {result})")
            if Path("/proc/self/stat").exists() and marked_processes_present(marker):
                raise RuntimeError("Dependency child processes remain after pip exited")
            if requirements_digest(requirements_file) != digest:
                raise RuntimeError("Requirements changed during installation; cache was not accepted")
        except BaseException:
            receipt["state"] = "uncertain" if process is not None else "failed"
            _write_record(descriptor, receipt)
            raise
        stamp.write_text(json.dumps({"requirements_sha256": digest}, sort_keys=True))
        receipt["state"] = "complete"
        _write_record(descriptor, receipt)
    finally:
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--lock-timeout", type=float, required=True)
    arguments = parser.parse_args()
    try:
        install(
            arguments.requirements, arguments.target,
            marker=os.environ.get(MARKER_ENV, ""),
            install_timeout=arguments.timeout, lock_timeout=arguments.lock_timeout,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
