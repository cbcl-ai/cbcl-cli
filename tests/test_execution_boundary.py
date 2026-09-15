"""Only verified per-attempt containers are valid lifecycle stop targets."""

from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from src.docker.execution_boundary import (
    ExecutionBoundaryError, ExecutionContainerIdentity, execution_labels, stop_execution_container,
)


def identity():
    return ExecutionContainerIdentity("a" * 64, "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222", "33333333-3333-4333-8333-333333333333")


def container_fixture():
    owner = identity()
    container = MagicMock()
    container.attrs = {
        "Id": owner.container_id,
        "Config": {"Labels": execution_labels(owner.office_id, owner.task_id, owner.attempt_id)},
        "HostConfig": {"Privileged": False, "PidMode": "", "NetworkMode": "none", "Init": True},
        "State": {"Running": True, "Pid": 123, "Status": "running"},
    }
    client = MagicMock()
    client.containers.get.return_value = container
    return owner, container, client


@pytest.mark.parametrize("mismatch", ["office", "task", "attempt", "id", "privileged", "host_pid", "shared_pid", "host_network", "init"])
def test_wrong_or_uncontained_owner_is_never_killed(mismatch):
    owner, container, client = container_fixture()
    if mismatch in {"office", "task", "attempt"}:
        container.attrs["Config"]["Labels"][f"cbcl.execution.{mismatch}"] = "another-owner"
    elif mismatch == "id":
        container.attrs["Id"] = "b" * 64
    else:
        field, value = {
            "privileged": ("Privileged", True), "host_pid": ("PidMode", "host"),
            "shared_pid": ("PidMode", "container:other"), "host_network": ("NetworkMode", "host"), "init": ("Init", False),
        }[mismatch]
        container.attrs["HostConfig"][field] = value
    with pytest.raises(ExecutionBoundaryError):
        stop_execution_container(client, owner)
    container.kill.assert_not_called()
    container.remove.assert_not_called()


def test_exact_owner_is_stopped_but_not_automatically_removed():
    owner, container, client = container_fixture()

    def stopped(**kwargs):
        container.attrs["State"] = {"Running": False, "Pid": 0, "Status": "exited"}

    container.wait.side_effect = stopped
    stop_execution_container(client, owner)
    client.containers.get.assert_called_once_with(owner.container_id)
    container.kill.assert_called_once_with(signal="SIGKILL")
    container.remove.assert_not_called()


def test_post_kill_unconfirmed_state_is_not_success():
    owner, container, client = container_fixture()
    original = deepcopy(container.attrs)
    with pytest.raises(ExecutionBoundaryError, match="not confirmed"):
        stop_execution_container(client, owner)
    assert container.attrs == original


def test_a_name_or_short_id_can_never_be_a_stop_receipt():
    owner = identity()
    with pytest.raises(ValueError, match="immutable"):
        ExecutionContainerIdentity("cbcl-office-name", owner.office_id, owner.task_id, owner.attempt_id)
