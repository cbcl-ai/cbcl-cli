"""Variable Manager — handles script variable BINDINGS on the host.

Phase 1.5 of the Scripts marketplace renamed the responsibility of
this module: it used to be a thin read-only loader for the legacy
``{NAME: VALUE}`` shape of ``variables.json``. The new contract is a
**binding store**.

A binding answers the question "how do I resolve this declared
variable's value at run time?" for EACH variable in the script's
manifest. The two binding kinds are:

* ``{"kind": "literal", "value": <any>}`` — use the embedded value
  directly. For non-secret variables this is the standard path.
* ``{"kind": "office_secret", "ref": "<NAME>"}`` — resolve the value
  by name from the office-secrets store at execute time. The actual
  secret value never appears in this binding — only the reference.

The legacy bare-value shape (``{"BATCH_SIZE": 100}``) is still
accepted on read and normalised to ``{"kind": "literal", ...}``;
the writer always produces the explicit binding shape so the file
on disk is self-describing for users who inspect it.

Files this module manages:
- ``variables.json`` — non-secret bindings + office-secret refs. The
  whole file is plain JSON the user can ``cat`` to debug. Office
  secret VALUES are NEVER in this file — only their names.
- ``.secrets.json`` — managed by ``secrets_store.py``, kept in sync
  with the binding world via the contract: when a user chooses
  "Custom value" for a SECRET variable, the value goes here; when
  they switch to "Office Secret", we both write the binding to
  variables.json AND erase the stale entry from .secrets.json so
  the env build doesn't see a ghost value.

These files live in ``/workspace/.scripts/{script_name}/``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal, TypedDict

from src._chown import chown_to_agent


# Per-(workspace, script_name) async lock so concurrent
# ``set_binding`` calls don't race on the read-modify-write of
# variables.json. The bind_script_variable MCP tool (0.2.22) made
# the AI a concurrent writer alongside the UI chat-WS path; without
# this lock a back-to-back pair of binds could lose one of the two
# writes (second writer reads the file BEFORE the first writer's
# os.replace completes → overwrites with stale dict).
_SET_BINDING_LOCKS: dict[tuple[str, str], asyncio.Lock] = defaultdict(
    asyncio.Lock,
)


def _get_set_binding_lock(
    workspace: Path, script_name: str,
) -> asyncio.Lock:
    """Return the asyncio.Lock keyed on workspace+script_name.

    Per-script granularity — separate scripts can be bound in
    parallel; separate variables within ONE script serialise
    behind the same lock (cheap, the write is sub-ms).
    """
    return _SET_BINDING_LOCKS[(str(workspace), script_name)]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Binding shape
# ---------------------------------------------------------------------------
#
# A binding is one of:
#   {"kind": "literal", "value": <any>}
#   {"kind": "office_secret", "ref": "<NAME>"}
#
# Bare values (``"BATCH_SIZE": 100``) are accepted on READ for
# backwards compatibility with hand-edited variables.json files and
# the legacy create flow that wrote bare defaults. They are normalised
# to ``LiteralBinding`` at parse time. The writer emits explicit
# bindings only, so a round-trip "read + write" upgrades the file in
# place.


class LiteralBinding(TypedDict):
    kind: Literal["literal"]
    value: Any


class OfficeSecretBinding(TypedDict):
    kind: Literal["office_secret"]
    ref: str


Binding = LiteralBinding | OfficeSecretBinding


# Office secret name shape — same regex the backend office_secrets
# router enforces. Duplicated here (instead of imported across the
# host/container boundary) because the communicator can't import
# backend code; this module runs on the host.
_OFFICE_SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def normalise_binding(raw: Any, *, variable_name: str = "") -> Binding | None:
    """Normalise a value from ``variables.json`` into a Binding.

    Returns ``None`` if the entry is malformed (and logs at WARNING).
    A returned ``None`` means "no binding configured" — the env-build
    pipeline treats this the same as a missing entry and falls back
    to ``.secrets.json`` → manifest default → omit.

    Accepted input shapes:
      * ``None`` / missing key → returns ``None`` (no binding).
      * ``str | int | float | bool`` → wrapped as
        ``LiteralBinding(kind="literal", value=...)`` for back-compat.
      * ``{"kind": "literal", "value": ...}`` → returned as-is after
        a sanity check.
      * ``{"kind": "office_secret", "ref": "<NAME>"}`` → returned as-is
        after name shape validation.

    Other shapes (dicts with no ``kind``, lists, etc.) are rejected.
    """
    if raw is None:
        return None
    # Bare literal — legacy + ergonomic. Lists are valid JSON but
    # would be a surprising literal here; reject so a user who put a
    # list in variables.json gets a clear warning rather than a
    # mysterious string-coerced run.
    if isinstance(raw, (str, int, float, bool)):
        return {"kind": "literal", "value": raw}
    if not isinstance(raw, dict):
        logger.warning(
            "Ignoring malformed binding for variable %r: expected a "
            "literal value or a {kind, ...} object, got %s",
            variable_name, type(raw).__name__,
        )
        return None
    kind = raw.get("kind")
    if kind == "literal":
        if "value" not in raw:
            logger.warning(
                "Ignoring 'literal' binding for %r: missing 'value'",
                variable_name,
            )
            return None
        return {"kind": "literal", "value": raw["value"]}
    if kind == "office_secret":
        ref = raw.get("ref")
        if not isinstance(ref, str) or not _OFFICE_SECRET_NAME_RE.match(ref):
            logger.warning(
                "Ignoring 'office_secret' binding for %r: 'ref' missing "
                "or doesn't match office-secret name shape "
                "(^[A-Z][A-Z0-9_]{0,63}$). Got: %r",
                variable_name, ref,
            )
            return None
        return {"kind": "office_secret", "ref": ref}
    logger.warning(
        "Ignoring binding for %r with unknown kind %r",
        variable_name, kind,
    )
    return None


def resolve_binding(
    binding: Binding | None,
    office_secrets: dict[str, str] | None = None,
) -> tuple[bool, Any]:
    """Resolve a binding to its final value.

    Returns a ``(resolved, value)`` tuple so the caller can
    distinguish "no value available" from "value is None / empty
    string" (both are legitimate for a literal).

      * ``(True, value)`` — the binding produced a value.
      * ``(False, None)`` — the binding could not be resolved (missing
        office secret, or no binding at all). The caller should fall
        through to the next env source (secrets.json, manifest
        default, ...).

    The split keeps the env-build code at the call site readable —
    no sentinel objects, no exceptions for the common "fall through"
    case.
    """
    if binding is None:
        return False, None
    if binding["kind"] == "literal":
        return True, binding["value"]
    if binding["kind"] == "office_secret":
        if not office_secrets:
            return False, None
        ref = binding["ref"]
        if ref not in office_secrets:
            return False, None
        return True, office_secrets[ref]
    return False, None


class VariableManager:
    """Manages script variable BINDINGS on the host side.

    Parameters
    ----------
    workspace:
        Path to the workspace root (e.g., ``/workspace``).
    """

    def __init__(self, workspace: str) -> None:
        self._workspace = workspace

    def _variables_path(self, script_name: str) -> Path:
        return (
            Path(self._workspace)
            / ".scripts"
            / script_name
            / "variables.json"
        )

    def get_variables(self, script_name: str) -> dict:
        """Read the raw ``variables.json`` map without normalisation.

        Returns the raw ``{key: value}`` dict as it sits on disk. Used
        by callers that want the legacy bare-value view (e.g. some
        debug surfaces). New callers should use ``get_bindings()``
        which normalises every entry.

        Empty dict if file missing / unreadable.
        """
        var_file = self._variables_path(script_name)
        if var_file.exists():
            try:
                data = json.loads(var_file.read_text())
                if isinstance(data, dict):
                    return data
                logger.warning(
                    "variables.json for %s is not a JSON object, ignoring",
                    script_name,
                )
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "Failed to read variables for %s: %s",
                    script_name, exc,
                )
        return {}

    def get_bindings(self, script_name: str) -> dict[str, Binding]:
        """Read + normalise every entry in ``variables.json``.

        Each entry is passed through :func:`normalise_binding`, so the
        return value is exclusively a map of
        ``variable_name -> Binding`` with no bare-value entries.
        Malformed entries are dropped (and logged WARNING) rather
        than failing the whole read — a single bad variable
        shouldn't make every other variable unresolvable.
        """
        raw = self.get_variables(script_name)
        bindings: dict[str, Binding] = {}
        for name, value in raw.items():
            binding = normalise_binding(value, variable_name=name)
            if binding is not None:
                bindings[name] = binding
        return bindings

    def set_binding(
        self,
        script_name: str,
        variable_name: str,
        binding: Binding | None,
    ) -> None:
        """Write a binding for ``variable_name`` into ``variables.json``.

        ``binding = None`` deletes the entry (the run-time env build
        will then fall back to ``.secrets.json`` / manifest default).

        Always rewrites the WHOLE file atomically (tempfile + os.replace)
        so a crash mid-write can't leave a half-written file the next
        read would choke on. The new file uses the explicit binding
        shape — bare-value entries from earlier hand-edits get
        upgraded in place on the next write.

        Idempotent: a binding identical to the on-disk value still
        writes (cheap, keeps the code simple). Callers that need
        change-detection can compare ``get_bindings`` before + after.
        """
        var_file = self._variables_path(script_name)
        var_file.parent.mkdir(parents=True, exist_ok=True)
        # The script's directory may have been created by this call;
        # chown to the agent uid so the in-container agent can
        # subsequently traverse + write the binding file via its
        # own Edit tool. Idempotent: chowning an already-correct
        # owner is a no-op.
        chown_to_agent(var_file.parent)

        current = self.get_bindings(script_name)
        if binding is None:
            current.pop(variable_name, None)
        else:
            current[variable_name] = binding

        # Atomic write — tempfile + os.replace. A crash between the
        # write_text and the rename leaves the OLD file intact; a
        # crash after the rename leaves the NEW file intact. There is
        # no window where the file is half-written or missing.
        tmp = var_file.with_suffix(var_file.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(current, indent=2, sort_keys=True))
            # Chown the temp BEFORE the rename so the visible file
            # always has correct ownership — racing readers never
            # see a root-owned variables.json. (After ``os.replace``
            # the inode keeps its old owner — chowning post-rename
            # would still work but this ordering is one atomic
            # transition for an observer.)
            chown_to_agent(tmp)
            os.replace(tmp, var_file)
        except OSError as exc:
            # Best-effort cleanup of the temp; the real failure here
            # is the user's disk / permissions, surface that.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise OSError(  # noqa: B904 — chained via raise from below
                f"Failed to write variables.json for {script_name}: {exc}",
            ) from exc

    async def set_binding_async(
        self,
        script_name: str,
        variable_name: str,
        binding: Binding | None,
    ) -> None:
        """Async-locked wrapper around :meth:`set_binding`.

        Concurrent ``set_binding`` writers (e.g. the AI calling
        ``bind_script_variable`` while the user clicks Save in the
        Variables UI) would race on the read-modify-write of
        ``variables.json`` — second writer reads BEFORE the first
        finishes, then overwrites with stale dict losing the
        first writer's binding. Per-script asyncio.Lock serialises
        them. The lock has process-local scope, which is fine —
        the daemon is single-process.
        """
        lock = _get_set_binding_lock(self._workspace, script_name)
        async with lock:
            self.set_binding(script_name, variable_name, binding)
