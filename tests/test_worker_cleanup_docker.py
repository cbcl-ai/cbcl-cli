"""Opt-in Linux/Docker acceptance for exact-execution process cleanup."""

import json
import os
import subprocess
import time
import uuid

import pytest

from src.docker.task_process_cleanup import _CLEANUP_PROGRAM, _DISCOVER_PROGRAM


pytestmark = pytest.mark.skipif(
    os.environ.get("CUBICLE_RUN_PROCESS_CLEANUP_TESTS") != "1",
    reason="requires an explicitly enabled disposable Docker office image",
)


def docker_command(*arguments, input_text=None):
    return subprocess.run(
        ["docker", *arguments],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=15,
    )


def run_program(container_id, program, *arguments):
    return docker_command(
        "exec", "-i", "-u", "1000:1000", container_id,
        "/usr/local/bin/python3", "-I", "-S", "-", *arguments,
        input_text=program,
    )


@pytest.fixture(params=[False, True], ids=["legacy-tail", "init-reaper"])
def office_container(request):
    arguments = [
        "run", "--detach", "--network", "none", "--pids-limit", "128",
        "--memory", "128m", "--cpus", "0.5", "--user", "1000:1000",
        "--name", f"cbcl-cleanup-regression-{uuid.uuid4().hex}",
        "--label", "cbcl.audit.test.worker-cleanup=true",
    ]
    if request.param:
        arguments.append("--init")
    arguments.extend([
        "--entrypoint", "tail",
        os.environ.get("CUBICLE_PROCESS_TEST_IMAGE", "cbcl-agent:latest"),
        "-f", "/dev/null",
    ])
    created = docker_command(*arguments)
    assert created.returncode == 0, created.stderr
    container_id = created.stdout.strip()
    try:
        yield container_id, request.param
    finally:
        removed = docker_command("rm", "--force", container_id)
        assert removed.returncode == 0, removed.stderr


def process_state(container_id, process_id):
    result = run_program(
        container_id,
        "from pathlib import Path\nimport json, sys\n"
        "try:\n"
        "    state = Path('/proc', sys.argv[1], 'stat').read_text()"
        ".rsplit(')', 1)[1].split()[0]\n"
        "except FileNotFoundError:\n"
        "    state = None\n"
        "print(json.dumps(state))\n",
        str(process_id),
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_orphan_zombie_does_not_block_cleanup_or_recovery(office_container):
    container_id, init_enabled = office_container
    created = run_program(
        container_id,
        "import os, time\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    os._exit(0)\n"
        "time.sleep(0.15)\n"
        "print(child, flush=True)\n"
        "os._exit(0)\n",
    )
    assert created.returncode == 0, created.stderr
    child_id = int(created.stdout)
    deadline = time.monotonic() + 4
    while init_enabled and time.monotonic() < deadline:
        if process_state(container_id, child_id) is None:
            break
        time.sleep(0.05)
    assert process_state(container_id, child_id) == (None if init_enabled else "Z")

    cleanup = run_program(container_id, _CLEANUP_PROGRAM, uuid.uuid4().hex * 2)
    assert cleanup.returncode == 0, cleanup.stderr
    discovery = run_program(container_id, _DISCOVER_PROGRAM)
    assert discovery.returncode == 0, discovery.stderr
    assert json.loads(discovery.stdout) == []


def start_worker(
    container_id, marker, *, unreadable=False, marker_env="CUBICLE_WORKER_EXECUTION_ID"
):
    program = (
        "import os, time\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    os.setsid()\n"
        "time.sleep(60)\n"
    )
    if unreadable:
        program = (
            "import ctypes, time\n"
            "assert ctypes.CDLL(None).prctl(4, 0) == 0\n"
            "time.sleep(60)\n"
        )
    result = docker_command(
        "exec",
        "--detach",
        "--user",
        "1000:1000",
        "--env",
        f"{marker_env}={marker}",
        container_id,
        "/usr/local/bin/python3",
        "-I",
        "-S",
        "-c",
        program,
    )
    assert result.returncode == 0, result.stderr


def marked_processes(container_id, marker, marker_env="CUBICLE_WORKER_EXECUTION_ID"):
    result = run_program(
        container_id,
        "from pathlib import Path\nimport json, sys\n"
        "marker = (sys.argv[2] + '=' + sys.argv[1]).encode()\n"
        "processes = []\n"
        "for entry in Path('/proc').iterdir():\n"
        "    if not entry.name.isdigit():\n"
        "        continue\n"
        "    try:\n"
        "        state = (entry / 'stat').read_bytes().rsplit(b')', 1)[1].split()[0]\n"
        "        if state in {b'Z', b'X'}:\n"
        "            continue\n"
        "        if marker in (entry / 'environ').read_bytes().split(b'\\0'):\n"
        "            processes.append(int(entry.name))\n"
        "    except (FileNotFoundError, ProcessLookupError):\n"
        "        pass\n"
        "print(json.dumps(sorted(processes)))\n",
        marker,
        marker_env,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_cleanup_stops_detached_descendants_without_stopping_sibling(office_container):
    container_id, _ = office_container
    target_marker = uuid.uuid4().hex * 2
    sibling_marker = uuid.uuid4().hex * 2
    start_worker(container_id, target_marker)
    start_worker(container_id, sibling_marker)
    deadline = time.monotonic() + 4
    while True:
        target_processes = marked_processes(container_id, target_marker)
        sibling_processes = marked_processes(container_id, sibling_marker)
        if len(target_processes) == len(sibling_processes) == 2:
            break
        assert time.monotonic() < deadline
        time.sleep(0.05)
    cleanup = run_program(container_id, _CLEANUP_PROGRAM, target_marker)
    assert cleanup.returncode == 0, cleanup.stderr
    discovery = run_program(container_id, _DISCOVER_PROGRAM)
    assert discovery.returncode == 0, discovery.stderr
    assert json.loads(discovery.stdout) == [sibling_marker]
    assert marked_processes(container_id, sibling_marker) == sibling_processes
    assert marked_processes(container_id, target_marker) == []
    sibling_cleanup = run_program(container_id, _CLEANUP_PROGRAM, sibling_marker)
    assert sibling_cleanup.returncode == 0, sibling_cleanup.stderr


def test_live_unreadable_process_still_blocks_unconfirmed_cleanup(office_container):
    container_id, _ = office_container
    start_worker(container_id, uuid.uuid4().hex * 2, unreadable=True)
    deadline = time.monotonic() + 4
    while True:
        discovery = run_program(container_id, _DISCOVER_PROGRAM)
        if discovery.returncode != 0:
            break
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert "Cannot verify orphan worker cleanup" in discovery.stderr
    cleanup = run_program(container_id, _CLEANUP_PROGRAM, uuid.uuid4().hex * 2)
    assert cleanup.returncode != 0
    assert "Cannot inspect a worker-owned container process" in cleanup.stderr


async def test_legacy_script_cleanup_preserves_sibling_script_and_worker(
    office_container,
):
    from src.scripts.script_resources import terminate_legacy_script_execution

    container_id, _ = office_container
    target = "exec-2026-09-21T12-00-00-123abc"
    sibling = "exec-2026-09-21T12-00-00-456def"
    worker = uuid.uuid4().hex * 2
    script_marker = "CUBICLE_EXECUTION_ID"
    start_worker(container_id, target, marker_env=script_marker)
    start_worker(container_id, sibling, marker_env=script_marker)
    start_worker(container_id, worker)
    deadline = time.monotonic() + 4
    while True:
        siblings = marked_processes(container_id, sibling, script_marker)
        workers = marked_processes(container_id, worker)
        if (
            len(marked_processes(container_id, target, script_marker))
            == len(siblings)
            == len(workers)
            == 2
        ):
            break
        assert time.monotonic() < deadline
        time.sleep(0.05)
    await terminate_legacy_script_execution(container_id, target)
    assert marked_processes(container_id, target, script_marker) == []
    assert marked_processes(container_id, sibling, script_marker) == siblings
    assert marked_processes(container_id, worker) == workers
