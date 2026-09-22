"""Park model attempts on durable capacity receipts; wake models, never scripts."""

import asyncio
import logging
import time

from src.capacity_wait_state import ACTIVE_WAIT_STATES, has_lineage, matches_task
from src.operation_state import OperationConflict
from src.operations.host_capacity import HostCapacityUnavailable
from src.scripts.managed_operations import capacity

logger = logging.getLogger(__name__)


class CapacityWaitAccepted(HostCapacityUnavailable):
    def __init__(self, wait: dict):
        super().__init__(
            "Capacity wait accepted; end this session. The daemon will resume this task phase when eligible."
        )
        self.receipt = {
            "accepted": True,
            "status": "waiting_for_capacity",
            "wait": {
                "wait_id": wait["wait_id"],
                "operation_id": wait["operation_id"],
                "task_id": wait["task_id"],
                "execution_cycle": wait["cycle"],
                "phase": wait["phase"],
                "state": "waiting",
                "retry_after_seconds": 30,
            },
        }


def accept_wait(
    runner, record, task, caller, action, overrides
) -> CapacityWaitAccepted | None:
    wait = runner._runtime_state.register_capacity_wait(
        record,
        task or {},
        caller or {},
        action=action,
        had_variable_overrides=bool(overrides),
    )
    return CapacityWaitAccepted(wait) if wait else None


class CapacityWaitCoordinator:
    def __init__(self, runner, *, clock=time.time):
        self.runner = runner
        self.runtime = runner._runtime_state
        self.clock = clock
        self.fetch_task = None
        self._reconcile_after_id = ""

    def retire(self, wait: dict) -> None:
        budget = capacity(self.runner)
        # Only never-started priority is withdrawn. Started/unknown reservations
        # remain governed by exact physical/remote cleanup receipts.
        if budget:
            budget.abandon_wait(wait["operation_id"], self.runtime.office_id)
        record = self.runtime.get_operation(wait["operation_id"])
        if record and record["state"] == "queued" and not record.get("external_ref"):
            try:
                self.runtime.cancel_queued_operation(record["operation_id"])
            except OperationConflict:
                pass  # A concurrent accepted launch owns its separate lease.
        self.runtime.retire_capacity_wait(wait["wait_id"])

    def can_dispatch(self, task: dict) -> bool:
        task.pop("capacity_wait_resume", None)
        wait = self.runtime.capacity_wait(
            str(task.get("task_id") or task.get("id") or "")
        )
        if not wait or wait["state"] not in ACTIVE_WAIT_STATES:
            return True
        if not has_lineage(task):
            return False
        try:
            if wait["state"] == "resuming" or self._older(wait, task):
                return False
            if not matches_task(wait, task):
                self.retire(wait)
                return True
            if task.get("execution_blocked") or task.get("human_action_request_id"):
                return False
            if self.clock() < wait["next_check_at"]:
                return False
            self.runtime.delay_capacity_wait(wait["wait_id"])
            budget = capacity(self.runner)
            if budget:
                budget.renew_wait(wait["operation_id"], self.runtime.office_id)
                eligibility = budget.wait_eligibility(
                    wait["operation_id"], self.runtime.office_id
                )
                if (
                    eligibility["state"] != "waiting"
                    or eligibility["eligible"] is not True
                ):
                    return False
            task["capacity_wait_resume"] = wait["resume_context"]
            return True
        except Exception:
            logger.warning(
                "Capacity wait remains deferred for %s", wait["task_id"], exc_info=True
            )
            return False

    @staticmethod
    def _older(wait: dict, task: dict) -> bool:
        # A board/GET response can predate a concurrent local claim binding.
        return has_lineage(task) and (
            task["execution_cycle"] < wait["cycle"]
            or (
                task["execution_cycle"] == wait["cycle"]
                and (
                    task["execution_generation"] < wait["generation"]
                    or (
                        task["execution_generation"] == wait["generation"]
                        and task["review_retry_epoch"] < wait["epoch"]
                    )
                )
            )
        )

    async def reconcile(self, tasks: list[dict]) -> None:
        """A paginated board is a projection, never proof a task disappeared.

        Inspect at most 100 waits per tick, with four bounded detail requests
        in flight. Unknown reads and in-flight claims retain durable intent.
        """
        self.runtime.reconcile_capacity_claim_gaps()
        by_id = {str(task.get("id") or task.get("task_id")): task for task in tasks}
        waits = self.runtime.active_capacity_waits(after_id=self._reconcile_after_id)
        self._reconcile_after_id = waits[-1]["task_id"] if len(waits) == 100 else ""
        semaphore = asyncio.Semaphore(4)

        async def inspect(wait):
            task = by_id.get(wait["task_id"])
            try:
                if wait["state"] == "resuming" or (task and self._older(wait, task)):
                    return
                if task is None or not matches_task(wait, task):
                    if self.fetch_task is None:
                        return
                    async with semaphore:
                        known, task = await self.fetch_task(wait["task_id"])
                    # A newer local claim may have committed during the read.
                    current = self.runtime.capacity_wait(wait["task_id"])
                    if not current or any(
                        current[key] != wait[key]
                        for key in (
                            "wait_id",
                            "state",
                            "attempt_id",
                            "generation",
                            "pending_resume_attempt_id",
                        )
                    ):
                        return
                    if not known or (
                        task is not None
                        and (not has_lineage(task) or self._older(wait, task))
                    ):
                        return
                    if task is None or not matches_task(wait, task):
                        self.retire(wait)
                        return
                if not task.get("execution_blocked") and not task.get(
                    "human_action_request_id"
                ):
                    budget = capacity(self.runner)
                    if budget:
                        budget.renew_wait(wait["operation_id"], self.runtime.office_id)
            except Exception:
                logger.warning(
                    "Capacity wait reconciliation deferred for %s",
                    wait["task_id"],
                    exc_info=True,
                )

        await asyncio.gather(*(inspect(wait) for wait in waits))
