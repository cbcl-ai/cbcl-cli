"""Inspection and explicit recovery of task-owned operations; never task approval."""


async def get_operation(runner, operation_id: str) -> dict:
    """Inspect a host-owned receipt; reading never launches or approves work."""
    from src.operation_state import OperationConflict
    from src.scripts import managed_operations

    record = (
        runner._runtime_state.get_operation(operation_id)
        if runner._runtime_state
        else None
    )
    if record is None:
        raise ValueError("Unknown operation")
    execution_id = record.get("execution_id")
    if execution_id:
        # The recurring retained-lease sweep also runs during normal launches.
        # A bound receipt may still be preparing dependencies, before the live
        # process map exists. Its exact starting lease owns that interval.
        starting = getattr(runner, "_starting_resource_leases", set())
        if starting and any(
            lease["execution_id"] == execution_id and lease["lease_id"] in starting
            for lease in runner._runtime_state.active_script_resources()
        ):
            await managed_operations.publish(runner, record)
            return record
        active = execution_id in runner._active
        await runner.get_status(execution_id)
        record = runner._runtime_state.get_operation(operation_id)
        if record.get("execution_id") != execution_id:
            # Inspection awaited an older observer. A new claim detaches its
            # execution ID before any await, so old receipts cannot settle it.
            await managed_operations.publish(runner, record)
            return record
        if active and execution_id in runner._active:
            # Preserve a provider run reference before completion/restart.
            path = (
                runner._workspace
                / ".scripts"
                / record["script_name"]
                / "executions"
                / execution_id
            )
            try:
                receipt = managed_operations.read_receipt(path)
                if receipt:
                    record = runner._runtime_state.update_operation(
                        operation_id, receipt=receipt
                    )
            except (ValueError, OSError, OperationConflict):
                pass  # malformed adapter data cannot replace authoritative state
        elif (
            record["state"] in {"preparing", "running"}
            or not record["cleanup_confirmed"]
        ):
            leases = runner._runtime_state.active_script_resources()
            clean = not any(item["execution_id"] == execution_id for item in leases)
            clean = clean and execution_id not in runner._uncertain_scripts
            # Workspace status.json is writable by the script. After host
            # observer loss it cannot attest an exit, even if it says 0.
            # Actual exits settle through the live completion callback;
            # recovery preserves identity and requires a new observer.
            record = managed_operations.settle(
                runner,
                operation_id,
                execution_id,
                state="unknown",
                exit_code=None,
                cleanup_confirmed=clean,
            )
    # The operation receipt and shared capacity live in distinct SQLite
    # stores. Replay this idempotent release after a crash between commits.
    managed_operations.release_capacity(runner, record)
    await managed_operations.publish(runner, record)
    return record


async def control_operation(
    runner, operation_id: str, action: str, caller: dict
) -> dict:
    """Explicit same-cycle control; external reconciliation uses a separate entry."""
    from src.operation_state import OperationConflict
    from src.scripts.manifest import load_manifest

    if action not in {"reconcile", "cancel"}:
        raise ValueError("Unknown operation action")
    record = await runner.get_operation(operation_id)
    await runner._assert_task_runnable(record["task_id"], caller)
    if (
        caller.get("role") != "worker"
        or caller.get("task_id") != record["task_id"]
        or caller.get("execution_cycle") != record["cycle"]
        or caller.get("task_mode") != record["phase"]
    ):
        raise OperationConflict(
            "Operation control requires its current task cycle and phase"
        )
    if (
        record["state"] in {"succeeded", "failed", "cancelled"}
        and record["cleanup_confirmed"]
    ):
        return record
    if (
        record["state"] == "queued"
        and not record.get("external_ref")
        and action == "cancel"
    ):
        from src.scripts.managed_operations import capacity, publish

        record = runner._runtime_state.cancel_queued_operation(operation_id)
        budget = capacity(runner)
        if budget:
            budget.abandon_wait(operation_id, runner._runtime_state.office_id)
        wait = runner._runtime_state.capacity_wait(record["task_id"])
        if wait and wait["operation_id"] == operation_id:
            runner._runtime_state.retire_capacity_wait(wait["wait_id"])
        await publish(runner, record)
        return record
    if action == "cancel" and record.get("execution_id") in runner._active:
        await runner.kill(record["execution_id"])
        record = await runner.get_operation(operation_id)
    if record["mechanism"] == "local":
        if action == "cancel":
            if record.get("execution_id"):
                await runner.kill(record["execution_id"])
            return await runner.get_operation(operation_id)
        raise OperationConflict(
            "A local lost outcome cannot be inferred; inspect evidence and use a new intent only after cleanup"
        )
    manifest = load_manifest(runner._workspace / ".scripts" / record["script_name"])
    if not getattr(manifest, f"operation_{action}_entry_point", None):
        raise OperationConflict(
            f"This adapter does not support {action}; external outcome remains unknown"
        )
    if not record.get("external_ref"):
        raise OperationConflict(
            "External run identity is unavailable; reconcile the provider before any retry"
        )
    record = runner._runtime_state.claim_operation_observer(operation_id)
    await runner.execute(
        record["script_name"],
        task_id=record["task_id"],
        triggered_by=caller["agent_name"],
        execution_caller=caller,
        _operation_record=record,
        _operation_action=action,
    )
    return await runner.get_operation(operation_id)


async def reconcile_operations(runner) -> None:
    """Run after exact script cleanup during startup; never relaunch a side effect."""
    import logging
    from src.scripts import managed_operations

    runtime = runner._runtime_state
    if runtime is None:
        return
    budget = managed_operations.capacity(runner)
    inventories = [runtime.operations_requiring_recovery]
    if budget:
        inventories.append(
            lambda **page: budget.list_reserved_operations(runtime.office_id, **page)
        )
    for inventory in inventories:
        after_id = None
        while operation_ids := inventory(after_id=after_id, limit=100):
            for operation_id in operation_ids:
                try:
                    record = runtime.get_operation(operation_id)
                    if record is None:
                        raise ValueError(
                            "Capacity reservation has no matching operation receipt"
                        )
                    # No execution ID was durably bound, so this observer could
                    # not have reached preparation/spawn. Do not disturb a live
                    # launch if a caller invokes reconciliation after startup.
                    if (
                        record["state"] == "preparing"
                        and not record.get("execution_id")
                        and not runner._starting_by_task.get(record["task_id"])
                    ):
                        runtime.update_operation(
                            operation_id,
                            state="unknown" if record.get("external_ref") else "failed",
                            cleanup_confirmed=True,
                        )
                    await get_operation(runner, operation_id)
                except Exception:
                    logging.getLogger(__name__).warning(
                        "Operation startup reconciliation remains incomplete for %s",
                        operation_id,
                        exc_info=True,
                    )
            after_id = operation_ids[-1]
