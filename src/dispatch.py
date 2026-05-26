"""Script and secret dispatch handlers for incoming messages.

Processes script_execute, script_secret_update, and skill_secret_update
messages from the platform backend.
"""

from __future__ import annotations

import logging

from src.scripts.script_runner import (
    MissingOfficeSecretError,
    OfficeSecretsCorruptError,
    ScriptRunner,
)
from src.scripts.secrets_store import SecretsStore
from src.scripts.variable_manager import VariableManager, normalise_binding

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
    except MissingOfficeSecretError as exc:
        # Surface clearly so the operator running `cbcl` knows the
        # script is parked on a missing credential. The UI side gets
        # the failure via the standard script_status error event from
        # the spawn path — but this manual trigger never reached spawn
        # (refused at preflight). Logging is the only visibility for
        # this case until we wire a dedicated "execution refused"
        # frame back to the UI.
        logger.error(
            "Script '%s' refused: missing office secret(s): %s. "
            "Ask the user to add them in Settings → Security → "
            "Office Secrets, then retry.",
            script_name, ", ".join(exc.missing),
        )
    except OfficeSecretsCorruptError as exc:
        # Distinct from MissingOfficeSecretError so the operator can
        # tell "file is corrupt — restore from backup" from "secret
        # absent — user must add it". Without the distinction, a
        # corrupt file looks like every secret was deleted (because
        # every reference returns "missing"), which is a misleading
        # diagnostic when the real fix is a single file rewrite.
        logger.error(
            "Script '%s' refused: %s. Ask the user to fix the "
            "office secrets file (Settings → Security → Office "
            "Secrets) before retrying.",
            script_name, exc.detail,
        )
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


async def handle_script_variable_binding_set(
    message: dict,
    variable_manager: VariableManager,
    secrets_store: SecretsStore,
) -> None:
    """Phase 1.5: persist a per-variable BINDING from the UI.

    A binding tells the Runner how to resolve the variable at execute
    time. Two kinds:

      * ``literal``       — embed a non-secret value directly in
                            ``variables.json``.
      * ``office_secret`` — reference an office-secret name; the
                            Runner resolves the value at run time
                            from the host-only office secrets store.

    Literal SECRET values (a secret-marked variable bound to a custom
    literal) still flow through the existing ``script_secret_update``
    handler so the value lands in ``.secrets.json`` and never appears
    in the chat WS payload — that pathway is untouched.

    The message arrives via the sanitiser
    ``chat_helpers.sanitize_relay_message`` so by this point the
    binding dict is shape-vetted. ``normalise_binding`` runs again as
    defense-in-depth and to log a clear warning if the shape ever
    diverges.

    Side effect on rebinding to office_secret: when a user switches
    a secret variable's binding from "Custom literal" to "Office
    Secret", we drop any stale entry in ``.secrets.json`` for the
    same variable. The env-build chain already prefers bindings
    over ``.secrets.json``, so leaving the entry wouldn't change
    the resolved value at this moment — but it WOULD silently
    resurrect when the user later toggles back to "Custom" + clears
    the binding (env-build would then fall through to the stale
    secret instead of producing "no value", masking a delete that
    the user thought they made). Pre-emptive cleanup keeps the
    UI's binding state authoritative across toggle cycles.
    """
    script_name = message.get("script_name", "")
    var_name = message.get("variable_name", "")
    binding_raw = message.get("binding")
    if not script_name or not var_name:
        logger.warning(
            "script_variable_binding_set missing script_name or variable_name",
        )
        return
    # ``binding`` may be explicitly ``None`` to CLEAR the binding —
    # the user picking "(no binding)" or removing the value via the
    # UI's "Clear" affordance. Treat that as a delete.
    if binding_raw is None:
        try:
            await variable_manager.set_binding_async(
                script_name, var_name, None,
            )
            logger.info(
                "Binding cleared for script '%s' variable %s",
                script_name, var_name,
            )
        except OSError as exc:
            logger.error(
                "Failed to clear binding for script '%s' variable %s: %s",
                script_name, var_name, exc,
            )
        return

    binding = normalise_binding(binding_raw, variable_name=var_name)
    if binding is None:
        logger.warning(
            "script_variable_binding_set rejected: binding for %s.%s "
            "did not normalise (raw=%r)",
            script_name, var_name, binding_raw,
        )
        return

    try:
        await variable_manager.set_binding_async(
            script_name, var_name, binding,
        )
    except OSError as exc:
        logger.error(
            "Failed to persist binding for script '%s' variable %s: %s",
            script_name, var_name, exc,
        )
        return

    # When the new binding is ``office_secret``, drop any stale
    # literal secret value for the same variable so a later
    # binding-switch back to literal doesn't accidentally
    # resurrect it. Best-effort — a failure here is logged but
    # not propagated, the binding was the user's intent.
    if binding["kind"] == "office_secret":
        try:
            secrets_store.delete_script_secret(script_name, var_name)
        except Exception as exc:
            logger.warning(
                "Could not drop stale .secrets.json entry for %s.%s "
                "after rebinding to office_secret: %s",
                script_name, var_name, exc,
            )

    logger.info(
        "Binding set for script '%s' variable %s: kind=%s",
        script_name, var_name, binding["kind"],
    )


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
