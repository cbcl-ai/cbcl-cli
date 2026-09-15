"""Opt-in no-bind kernel/ledger acceptance, without Claude auth or mount claims."""

import os
import subprocess
import uuid

import docker
import pytest

from src.docker.execution_ledger import ExecutionBudget, ExecutionResources
from src.docker.execution_launcher import ExecutionContainerManager, OfficeExecutionContext, RUNTIME_PATH


pytestmark = pytest.mark.skipif(
    os.environ.get("CUBICLE_RUN_EXECUTION_BOUNDARY_TESTS") != "1",
    reason="requires explicitly enabled owned no-bind Docker containers",
)


async def test_real_launcher_reopens_ledger_and_stops_only_its_worker(tmp_path):
    endpoint = subprocess.run(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], check=True, capture_output=True, text=True, timeout=10).stdout.strip()
    client = docker.DockerClient(base_url=endpoint, timeout=15)
    image = client.images.get(os.environ.get("CUBICLE_PROCESS_TEST_IMAGE", "cbcl-agent:latest"))
    context = OfficeExecutionContext(str(uuid.uuid4()), "e" * 64, image.id, (), network_mode="none")
    resources = ExecutionResources(500, 128 * 1024 * 1024, 64)
    budget = ExecutionBudget(2, 1000, 256 * 1024 * 1024, 128)
    path = tmp_path / "private" / "executions.sqlite3"
    manager = ExecutionContainerManager(client, path, context, resources, budget)
    identities = []
    attempts = []
    try:
        for _ in range(2):
            attempt_id = str(uuid.uuid4())
            attempts.append(attempt_id)
            identities.append(await manager.prepare(str(uuid.uuid4()), attempt_id))
        target, sibling = (client.containers.get(identity.container_id) for identity in identities)
        program = (
            "import subprocess; "
            "child=subprocess.Popen(['python3','-I','-S','-c','import time; time.sleep(120)'], "
            "env={},start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "print(child.pid)"
        )
        target_child = target.exec_run(["python3", "-I", "-S", "-c", program])
        sibling_child = sibling.exec_run(["python3", "-I", "-S", "-c", program])
        assert target_child.exit_code == sibling_child.exit_code == 0
        sibling_pid = int(sibling_child.output)
        result = target.exec_run(["python3", "-I", "-S", "-c", f"from pathlib import Path; Path('{RUNTIME_PATH}/private-config').write_text('synthetic')"])
        assert result.exit_code == 0
        reopened = ExecutionContainerManager(client, path, context, resources, budget)
        assert not await reopened.available()
        assert (await reopened.reconcile())[0]["state"] == "running_reconciliation_required"
        await reopened.stop_task(identities[0].task_id)
        target.reload()
        assert target.attrs["State"]["Pid"] == 0 and not target.attrs["State"]["Running"]
        result = sibling.exec_run(["python3", "-I", "-S", "-c", f"from pathlib import Path; assert Path('/proc/{sibling_pid}/stat').exists(); assert not Path('{RUNTIME_PATH}/private-config').exists()"])
        assert result.exit_code == 0
        assert await reopened.available()
    finally:
        for attempt_id in attempts:
            row = manager.ledger.get(attempt_id)
            if row is None or not row["container_id"]:
                continue
            container = client.containers.get(row["container_id"])
            container.reload()
            assert container.id == row["container_id"]
            assert container.labels.get("cbcl.execution.office") == context.office_id
            assert container.labels.get("cbcl.execution.attempt") == attempt_id
            container.remove(force=True)
        await manager.close()
