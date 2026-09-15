"""Container boundary substitute for suites running only the host mock agent."""

import pytest


@pytest.fixture(autouse=True)
def mock_container_cleanup(monkeypatch):
    async def no_container_processes(container_name, marker):
        # These suites execute mock_agent_process.py on the host. They create
        # no Docker executions. Real marker cleanup has separate Docker tests.
        assert container_name in {"", "cbcl-office-test"}
        assert marker

    monkeypatch.setattr(
        "src.docker.task_process_cleanup.terminate_worker_execution",
        no_container_processes,
    )
