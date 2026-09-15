"""Real process and kernel-lock tests, using a local fake pip with no network."""

import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.scripts import deps_install_runtime as runtime

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX installer locking")

FAKE_PIP = """
import json
import os
from pathlib import Path
import time

directory = Path(os.environ["TEST_PIP_DIRECTORY"])
record = directory / (str(os.getpid()) + ".started")
record.write_text(json.dumps({"pid": os.getpid()}))
while not (directory / "release").exists():
    time.sleep(0.02)
"""


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for owned test process")


@pytest.fixture
def installer_processes(tmp_path):
    fake_pip = tmp_path / "fake-pip"
    fake_pip.mkdir()
    (fake_pip / "pip.py").write_text(FAKE_PIP)
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("synthetic==1\n")
    children = []

    def launch(*, lock_timeout=3, install_timeout=5):
        environment = dict(os.environ)
        environment.update({
            "PYTHONPATH": str(fake_pip),
            "TEST_PIP_DIRECTORY": str(tmp_path),
            runtime.MARKER_ENV: secrets.token_hex(32),
        })
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-", "--requirements", str(requirements),
             "--target", str(tmp_path / ".deps"), "--timeout", str(install_timeout),
             "--lock-timeout", str(lock_timeout)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=environment, start_new_session=True,
        )
        children.append(process)
        process.stdin.write(Path(runtime.__file__).read_bytes())
        process.stdin.close()
        process.stdin = None
        return process

    yield launch

    (tmp_path / "release").touch()
    for process in children:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
    for record in tmp_path.glob("*.started"):
        process_id = json.loads(record.read_text())["pid"]
        try:
            os.killpg(process_id, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_concurrent_installers_share_one_stable_kernel_lock(tmp_path, installer_processes):
    first = installer_processes()
    wait_until(lambda: len(list(tmp_path.glob("*.started"))) == 1)
    lock_inode = (tmp_path / ".deps/.installing.lock").stat().st_ino
    second = installer_processes()
    time.sleep(0.2)
    assert second.poll() is None
    assert len(list(tmp_path.glob("*.started"))) == 1
    (tmp_path / "release").touch()
    for process in (first, second):
        _stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr.decode()
    assert len(list(tmp_path.glob("*.started"))) == 1
    assert (tmp_path / ".deps/.installing.lock").stat().st_ino == lock_inode
    assert runtime.cache_valid(tmp_path / "requirements.txt", tmp_path / ".deps/.installed_at")


def test_killed_wrapper_does_not_release_live_pip_lock(tmp_path, installer_processes):
    first = installer_processes()
    wait_until(lambda: len(list(tmp_path.glob("*.started"))) == 1)
    first.kill()
    first.wait(timeout=5)
    second = installer_processes(lock_timeout=0.2)
    _stdout, stderr = second.communicate(timeout=5)
    assert second.returncode == 1
    assert b"lock remains intact" in stderr
    assert len(list(tmp_path.glob("*.started"))) == 1
    assert not (tmp_path / ".deps/.installed_at").exists()
    assert json.loads((tmp_path / ".deps/.installing.lock").read_text())["state"] == "running"


def test_requirements_change_during_install_cannot_publish_success(tmp_path, installer_processes):
    process = installer_processes()
    wait_until(lambda: len(list(tmp_path.glob("*.started"))) == 1)
    (tmp_path / "requirements.txt").write_text("synthetic==2\n")
    (tmp_path / "release").touch()
    _stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 1
    assert b"Requirements changed" in stderr
    assert not (tmp_path / ".deps/.installed_at").exists()
    assert json.loads((tmp_path / ".deps/.installing.lock").read_text())["state"] == "uncertain"


def test_installer_timeout_kills_owned_pip_and_keeps_incomplete_receipt(tmp_path, installer_processes):
    process = installer_processes(install_timeout=0.2)
    wait_until(lambda: len(list(tmp_path.glob("*.started"))) == 1)
    _stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 1
    assert b"installation timed out" in stderr
    assert not (tmp_path / ".deps/.installed_at").exists()
    assert json.loads((tmp_path / ".deps/.installing.lock").read_text())["state"] == "uncertain"
