"""Opt-in physical private-PID execution boundary; not a production runtime switch."""

import os
import subprocess
import uuid

import docker
import pytest

from src.docker.execution_boundary import ExecutionBoundaryError, ExecutionContainerIdentity
from src.docker.execution_boundary import execution_labels, stop_execution_container


pytestmark = pytest.mark.skipif(
    os.environ.get("CUBICLE_RUN_EXECUTION_BOUNDARY_TESTS") != "1",
    reason="requires explicitly enabled owned disposable Docker containers",
)


@pytest.fixture
def execution_pair():
    endpoint = subprocess.run(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], check=True, capture_output=True, text=True, timeout=10).stdout.strip()
    client = docker.DockerClient(base_url=endpoint, timeout=15)
    owned = []
    try:
        office_id = str(uuid.uuid4())
        for _ in range(2):
            task_id, attempt_id = str(uuid.uuid4()), str(uuid.uuid4())
            container = client.containers.create(
                os.environ.get("CUBICLE_PROCESS_TEST_IMAGE", "cbcl-agent:latest"),
                ["-f", "/dev/null"], entrypoint="tail", detach=True,
                name=f"cbcl-boundary-regression-{uuid.uuid4().hex}",
                init=True, network_mode="none", privileged=False,
                user="1000:1000", cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
                mem_limit="128m", pids_limit=64,
                labels={**execution_labels(office_id, task_id, attempt_id), "cbcl.audit.test.execution-boundary": "true"},
            )
            owned.append((container, ExecutionContainerIdentity(container.id, office_id, task_id, attempt_id)))
            container.start()
        yield client, owned
    finally:
        for container, identity in owned:
            container.reload()
            assert container.id == identity.container_id
            assert container.labels.get("cbcl.audit.test.execution-boundary") == "true"
            container.remove(force=True)
        client.close()


def run_python(container, program):
    result = container.exec_run(["/usr/local/bin/python3", "-I", "-S", "-c", program], user="1000:1000")
    assert result.exit_code == 0, result.output.decode()
    return result.output.decode().strip()


def test_private_boundary_stops_env_cleared_detached_work_without_touching_sibling(execution_pair):
    client, executions = execution_pair
    target, target_identity = executions[0]
    sibling, sibling_identity = executions[1]
    program = (
        "import os, subprocess\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen(['/usr/local/bin/python3', '-I', '-S', '-c', "
        "'import time; time.sleep(120)'], env={}, start_new_session=True, "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "Path('/tmp/private-process').write_text(str(child.pid))\n"
        "print(child.pid)\n"
    )
    target_pid = int(run_python(target, program))
    sibling_pid = int(run_python(sibling, program))
    assert target_pid > 1 and sibling_pid > 1
    assert run_python(target, f"from pathlib import Path; assert Path('/proc/{target_pid}/environ').read_bytes() == b''; print('clear')") == "clear"
    wrong_owner = ExecutionContainerIdentity(target.id, target_identity.office_id, sibling_identity.task_id, sibling_identity.attempt_id)
    with pytest.raises(ExecutionBoundaryError):
        stop_execution_container(client, wrong_owner)
    target.reload()
    assert target.attrs["State"]["Running"] is True
    stop_execution_container(client, target_identity)
    target.reload()
    assert target.attrs["State"]["Running"] is False
    assert target.attrs["State"]["Pid"] == 0
    assert run_python(sibling, f"from pathlib import Path; assert Path('/proc/{sibling_pid}/stat').exists(); print('alive')") == "alive"


def test_runtime_files_are_private_to_each_execution_container(execution_pair):
    _, executions = execution_pair
    target = executions[0][0]
    sibling = executions[1][0]
    assert run_python(target, "from pathlib import Path; Path('/tmp/private-runtime').write_text('private'); print('ready')") == "ready"
    assert run_python(sibling, "from pathlib import Path; assert not Path('/tmp/private-runtime').exists(); print('isolated')") == "isolated"
