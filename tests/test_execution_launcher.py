"""Durable isolated-container admission never guesses that ambiguous work stopped."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace
import uuid

import pytest

from src.docker.execution_ledger import ExecutionAdmissionError, ExecutionBudget, ExecutionLedger, ExecutionResources, read_execution_inventory
from src.docker.execution_launcher import ExecutionContainerManager, ExecutionMount, ExecutionReconciliationRequired, OfficeExecutionContext, RUNTIME_PATH


def identifier(number):
    return str(uuid.UUID(int=number))


class Container:
    def __init__(self, image, options, serial):
        self.id = f"{serial:064x}"
        self.options = options
        self.killed = False
        self.started = 0
        self.attrs = {
            "Id": self.id, "Image": image,
            "Config": {"Labels": options["labels"], "User": options["user"]},
            "HostConfig": {
                "Privileged": options["privileged"], "PidMode": "", "Init": options["init"],
                "NetworkMode": options["network_mode"], "Memory": options["mem_limit"],
                "NanoCpus": options["nano_cpus"], "PidsLimit": options["pids_limit"],
                "RestartPolicy": options["restart_policy"], "CapDrop": options["cap_drop"],
                "SecurityOpt": options["security_opt"], "Tmpfs": options["tmpfs"],
            },
            "State": {"Running": False, "Pid": 0, "Status": "created"},
        }

    def reload(self):
        pass

    def start(self):
        self.started += 1
        self.attrs["State"] = {"Running": True, "Pid": 123, "Status": "running"}

    def exec_run(self, command, **kwargs):
        return SimpleNamespace(exit_code=0)

    def kill(self, **kwargs):
        self.killed = True
        self.attrs["State"] = {"Running": False, "Pid": 0, "Status": "exited"}

    def wait(self, **kwargs):
        return {"StatusCode": 137}


class Containers:
    def __init__(self):
        self.items = {}
        self.before_create = None
        self.after_create = None

    def create(self, image, **options):
        if self.before_create:
            self.before_create()
        container = Container(image, options, len(self.items) + 1)
        self.items[container.id] = container
        if self.after_create:
            self.after_create(container)
        return container

    def get(self, container_id):
        return self.items[container_id]

    def list(self, **kwargs):
        return list(self.items.values())


class ContainerAPI:
    def __init__(self, containers):
        self.containers = containers

    def create_host_config(self, **options):
        return options

    def create_container(self, image, host_config, **options):
        return {"Id": self.containers.create(image, **host_config, **options).id}


@pytest.fixture
def manager(tmp_path):
    context = OfficeExecutionContext(identifier(1), "a" * 64, "sha256:" + "b" * 64, ())
    client = SimpleNamespace(containers=Containers(), close=lambda: None)
    client.api = ContainerAPI(client.containers)
    return ExecutionContainerManager(client, tmp_path / "private" / "executions.sqlite3", context, ExecutionResources(500, 128 * 1024 * 1024, 64), ExecutionBudget(2, 1000, 256 * 1024 * 1024, 128))


def reopened(manager):
    return ExecutionContainerManager(manager.client, manager.ledger.database_path, manager.context, manager.resources, manager.budget)


async def test_prepare_is_idempotent_and_stop_never_resurrects_old_attempt(manager):
    identity = await manager.prepare(identifier(2), identifier(3))
    container = manager.client.containers.get(identity.container_id)
    assert container.started == 1
    assert manager.ledger.get(identifier(3))["state"] == "running"
    assert container.options["tmpfs"][RUNTIME_PATH].endswith("uid=1000,gid=1000")
    assert "cbcl.managed" not in container.options["labels"]
    assert not await manager.task_available(identifier(2))
    second = reopened(manager)
    assert await second.prepare(identifier(2), identifier(3)) == identity
    with pytest.raises(ExecutionAdmissionError, match="already has"):
        await second.prepare(identifier(2), identifier(4))
    await manager.stop(identity)
    assert container.killed
    assert await second.task_available(identifier(2))
    with pytest.raises(ExecutionReconciliationRequired):
        await second.prepare(identifier(2), identifier(3))
    assert container.started == 1


async def test_ambiguous_create_recovers_identity_but_never_starts_business(manager):
    def lost_response(container):
        raise TimeoutError("create response lost")

    manager.client.containers.after_create = lost_response
    with pytest.raises(TimeoutError):
        await manager.prepare(identifier(2), identifier(3))
    row = manager.ledger.get(identifier(3))
    assert row["container_id"] is None and row["state"] == "uncertain"
    second = reopened(manager)
    reports = await second.reconcile()
    assert reports[0]["state"] == "created_reconciliation_required"
    assert not await second.task_available(identifier(2))
    assert next(iter(manager.client.containers.items.values())).started == 0
    with pytest.raises(ExecutionReconciliationRequired):
        await second.prepare(identifier(2), identifier(3))
    with pytest.raises(ExecutionReconciliationRequired):
        await second.stop_task(identifier(2))
    assert not await second.task_available(identifier(2))


async def test_cancellation_preserves_inflight_prepare_until_exact_stop(manager):
    entered, release = threading.Event(), threading.Event()

    def blocked_create():
        entered.set()
        assert release.wait(5)

    manager.client.containers.before_create = blocked_create
    caller = asyncio.create_task(manager.prepare(identifier(2), identifier(3)))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert not await manager.task_available(identifier(2))
        with pytest.raises(ExecutionReconciliationRequired):
            await manager.close()
    finally:
        release.set()
    await manager.stop_attempt(identifier(2), identifier(3))
    assert manager.ledger.get(identifier(3))["state"] == "stopped"
    assert next(iter(manager.client.containers.items.values())).killed


async def test_lost_container_or_changed_owner_never_releases_budget(manager):
    identity = await manager.prepare(identifier(2), identifier(3))
    container = manager.client.containers.get(identity.container_id)
    container.attrs["Config"]["Labels"]["cbcl.execution.task"] = identifier(4)
    with pytest.raises(Exception):
        await manager.stop(identity)
    assert not container.killed
    assert not await manager.task_available(identifier(2))
    assert (await manager.reconcile())[0]["state"] == "unconfirmed"


@pytest.mark.parametrize("change", ["mount", "capabilities", "added_capability", "security", "ports", "network"])
async def test_altered_execution_properties_refuse_cleanup_or_adoption(manager, change):
    identity = await manager.prepare(identifier(2), identifier(3))
    container = manager.client.containers.get(identity.container_id)
    if change == "mount":
        container.attrs["Mounts"] = [{"Type": "bind", "Source": "/unexpected", "Destination": "/workspace", "RW": True}]
    elif change == "capabilities":
        container.attrs["HostConfig"]["CapDrop"] = []
    elif change == "added_capability":
        container.attrs["HostConfig"]["CapAdd"] = ["SYS_ADMIN"]
    elif change == "security":
        container.attrs["HostConfig"]["SecurityOpt"].append("seccomp=unconfined")
    elif change == "ports":
        container.attrs["HostConfig"]["PortBindings"] = {"80/tcp": [{"HostPort": "8888"}]}
    else:
        container.attrs["HostConfig"]["NetworkMode"] = "none"
    with pytest.raises(ExecutionReconciliationRequired):
        await manager.stop(identity)
    assert not container.killed
    assert (await reopened(manager).reconcile())[0]["state"] == "unconfirmed"


async def test_rediscovery_verifies_properties_before_binding_candidate(manager):
    def lost_response(container):
        container.attrs["HostConfig"]["CapDrop"] = []
        raise TimeoutError("create response lost")

    manager.client.containers.after_create = lost_response
    with pytest.raises(TimeoutError):
        await manager.prepare(identifier(2), identifier(3))
    assert (await reopened(manager).reconcile())[0]["state"] == "unconfirmed"
    assert manager.ledger.get(identifier(3))["container_id"] is None


async def test_create_acknowledgment_is_retained_before_followup_inspection(manager, monkeypatch):
    def failed_inspection(container_id):
        row = manager.ledger.get(identifier(3))
        assert row["container_id"] == container_id
        raise RuntimeError("Docker inspection unavailable")

    monkeypatch.setattr(manager.client.containers, "get", failed_inspection)
    with pytest.raises(RuntimeError, match="inspection unavailable"):
        await manager.prepare(identifier(2), identifier(3))
    row = manager.ledger.get(identifier(3))
    assert row["container_id"] == next(iter(manager.client.containers.items))
    assert row["state"] == "uncertain"
    assert not await manager.task_available(identifier(2))


async def test_bootstrap_failure_keeps_known_running_stop_proof_without_reusing_attempt(manager):
    def broken_bootstrap(container):
        container.exec_run = lambda *args, **kwargs: SimpleNamespace(exit_code=1)

    manager.client.containers.after_create = broken_bootstrap
    with pytest.raises(ExecutionReconciliationRequired, match="configuration link"):
        await manager.prepare(identifier(2), identifier(3))
    row = manager.ledger.get(identifier(3))
    assert row["state"] == "running_unready"
    second = reopened(manager)
    with pytest.raises(ExecutionReconciliationRequired, match="never automatically relaunched"):
        await second.prepare(identifier(2), identifier(3))
    assert await second.stop_task(identifier(2))
    assert manager.client.containers.get(row["container_id"]).killed
    assert await second.task_available(identifier(2))


async def test_read_only_inventory_survives_restart_and_does_not_hide_reservations(manager, tmp_path):
    absent = tmp_path / "absent" / "executions.sqlite3"
    assert read_execution_inventory(absent) == []
    assert not absent.parent.exists()
    identity = await manager.prepare(identifier(2), identifier(3))
    inventory = read_execution_inventory(manager.ledger.database_path)
    assert len(inventory) == 1
    assert inventory[0]["office_id"] == identifier(1)
    assert inventory[0]["container_id"] == identity.container_id
    assert "spec_json" not in inventory[0]
    assert read_execution_inventory(manager.ledger.database_path, identifier(9)) == []
    await manager.stop(identity)
    assert read_execution_inventory(manager.ledger.database_path) == []


async def test_confirmed_budget_rejection_needs_no_container_cleanup(manager):
    await manager.prepare(identifier(2), identifier(3))
    await manager.prepare(identifier(4), identifier(5))
    assert not await manager.available()
    with pytest.raises(ExecutionAdmissionError, match="budget"):
        await manager.prepare(identifier(6), identifier(7))
    await manager.stop_attempt(identifier(6), identifier(7))
    assert len(manager.client.containers.items) == 2
    assert manager.ledger.get(identifier(7)) is None
    with pytest.raises(ExecutionAdmissionError):
        await reopened(manager).stop_attempt(identifier(6), identifier(7))


async def test_restarted_manager_can_stop_exact_previously_acknowledged_worker(manager):
    identity = await manager.prepare(identifier(2), identifier(3))
    second = reopened(manager)
    assert await second.stop_task(identifier(2))
    assert manager.client.containers.get(identity.container_id).killed
    assert await second.task_available(identifier(2))


async def test_explicit_shutdown_stops_known_workers_and_retains_unknown_creation(manager):
    identity = await manager.prepare(identifier(2), identifier(3))
    def lost_response(container):
        raise TimeoutError("create response lost")

    manager.client.containers.after_create = lost_response
    with pytest.raises(TimeoutError):
        await manager.prepare(identifier(4), identifier(5))
    second = reopened(manager)
    with pytest.raises(ExecutionReconciliationRequired, match="shutdown is not confirmed"):
        await second.stop_all()
    assert manager.client.containers.get(identity.container_id).killed
    assert not await second.task_available(identifier(4))


async def test_stop_observes_prepare_before_its_reservation_is_created(manager):
    caller = asyncio.create_task(manager.prepare(identifier(2), identifier(3)))
    await asyncio.sleep(0)
    assert await manager.stop_task(identifier(2))
    identity = await caller
    assert manager.client.containers.get(identity.container_id).killed
    assert manager.ledger.get(identifier(3))["state"] == "stopped"


def test_parallel_reservations_enforce_durable_pool_and_task_budget(manager):
    def reserve(number):
        ledger = ExecutionLedger(manager.ledger.database_path)
        try:
            return ledger.reserve(office_id=identifier(1), task_id=identifier(number), attempt_id=identifier(number + 10), fingerprint="same", office_container_id="a" * 64, image_id="sha256:" + "b" * 64, resources=manager.resources, budget=manager.budget)[1]
        except ExecutionAdmissionError:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(reserve, (2, 3, 4, 5))) == 2
    assert len(reopened(manager).ledger.unresolved(identifier(1))) == 2


def test_parallel_attempts_cannot_reserve_the_same_task_twice(manager):
    def reserve(number):
        try:
            return ExecutionLedger(manager.ledger.database_path).reserve(office_id=identifier(1), task_id=identifier(2), attempt_id=identifier(number), fingerprint="same", office_container_id="a" * 64, image_id="sha256:" + "b" * 64, resources=manager.resources, budget=manager.budget)[1]
        except ExecutionAdmissionError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(reserve, (3, 4))) == 1


def test_ledger_rejects_shared_parent_or_symlink(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    with pytest.raises(ExecutionAdmissionError, match="private"):
        ExecutionLedger(shared / "executions.sqlite3")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    (private / "executions.sqlite3").symlink_to(tmp_path / "unrelated")
    with pytest.raises(OSError):
        ExecutionLedger(private / "executions.sqlite3")


@pytest.mark.parametrize("source,target", [("/", "/data"), ("/var/run/docker.sock", "/data"), ("/proc/1", "/data"), ("/safe", "/tmp"), ("/safe", RUNTIME_PATH), ("/safe", "/home/agent"), ("/safe", "/opt/cubicle")])
def test_privileged_or_private_runtime_mounts_are_refused(source, target):
    with pytest.raises(ValueError):
        ExecutionMount(source, target)


def test_context_copies_only_exact_authorized_office_bind_mounts(tmp_path):
    expected = {"/workspace": tmp_path / "workspace", "/home/agent/.claude": tmp_path / "auth", "/home/agent/.ssh": tmp_path / "ssh"}
    container = SimpleNamespace(reload=lambda: None, attrs={
        "Id": "a" * 64, "Image": "sha256:" + "b" * 64,
        "Config": {"Labels": {"cbcl.managed": "true", "cbcl.office_id": identifier(1)}},
        "State": {"Running": True}, "HostConfig": {"Privileged": False, "PidMode": ""},
        "Mounts": [{"Source": str(source), "Destination": target, "Type": "bind", "RW": True} for target, source in expected.items()] + [{"Source": "/host/secrets", "Destination": "/secrets", "Type": "bind", "RW": False}],
    })
    context = OfficeExecutionContext.from_office_container(container, identifier(1), expected["/workspace"], expected["/home/agent/.claude"], expected["/home/agent/.ssh"])
    assert {mount.target for mount in context.mounts} == set(expected)
    container.attrs["Mounts"][1]["Source"] = "/another-office/auth"
    with pytest.raises(ExecutionAdmissionError, match="authorized"):
        OfficeExecutionContext.from_office_container(container, identifier(1), *expected.values())
