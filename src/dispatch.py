"""Script and secret dispatch handlers for incoming messages.

Processes script_execute, script_secret_update, and skill_secret_update
messages from the platform backend.
"""

from __future__ import annotations

import logging

from src.scripts.script_runner import ScriptRunner
from src.scripts.secrets_store import SecretsStore

logger = logging.getLogger("cbcl.dispatch")


async def handle_script_execute(
    message: dict, script_runner: ScriptRunner,
) -> None:
    """Handle script_execute: manual trigger from UI.

    Logs a clear entry on receive so an operator running ``cbcl`` can
    confirm the message reached the communicator — before this log
    line was added, a silent handler dispatch made it impossible to
    tell from the cbcl output whether a "script didn't run"
    complaint was a transport issue or a runner-side failure.
    """
    script_name = message.get("script_name")
    if not script_name or not isinstance(script_name, str):
        logger.warning("script_execute message missing or invalid script_name")
        return
    variable_overrides = message.get("variable_overrides") or {}
    task_id = message.get("task_id")
    logger.info(
        "script_execute received: script=%s task_id=%s overrides=%s",
        script_name, task_id,
        list(variable_overrides.keys()) if variable_overrides else [],
    )
    try:
        exec_id = await script_runner.execute(
            script_name=script_name, variable_overrides=variable_overrides,
            task_id=task_id, triggered_by="user",
            # Backend resolves these from the linked task at relay
            # time so the Runner can inject CUBICLE_OUTPUT_DIR. Both
            # are optional — manual UI triggers without a task arrive
            # with neither set, and the Runner falls back to the
            # legacy flat /workspace/outputs/ path.
            workstream_short_code=message.get("workstream_short_code") or None,
            scope_readable_id=message.get("scope_readable_id") or None,
        )
        logger.info("Manual script execution started: %s (%s)", script_name, exec_id)
    except FileNotFoundError:
        logger.error("Script not found for manual execution: %s", script_name)
    except Exception as exc:
        logger.exception("Failed to execute script '%s' manually: %s", script_name, exc)


async def handle_script_secret_update(
    message: dict, secrets_store: SecretsStore,
) -> None:
    """Handle script_secret_update: store secret locally."""
    script_name = message.get("script_name", "")
    var_name = message.get("variable_name", "")
    value = message.get("value", "")
    if not script_name or not var_name:
        logger.warning("script_secret_update missing script_name or variable_name")
        return
    secrets_store.set_script_secret(script_name, var_name, value)
    logger.info("Secret updated for script '%s': %s", script_name, var_name)


async def handle_skill_secret_update(
    message: dict, secrets_store: SecretsStore,
) -> None:
    """Handle skill_secret_update: store skill secret locally."""
    skill_name = message.get("skill_name", "")
    param_name = message.get("param_name", "")
    value = message.get("value", "")
    if not skill_name or not param_name:
        logger.warning("skill_secret_update missing skill_name or parameter_name")
        return
    secrets_store.set_skill_secret(skill_name, param_name, value)
    logger.info("Secret updated for skill '%s': %s", skill_name, param_name)
