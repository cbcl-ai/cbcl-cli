"""Script manifest — ``script.yaml`` parsing + validation.

A mini-project script is described by a ``script.yaml`` at the
root of its folder. The manifest is the source of truth for:

  - ``entry_point``   — Python file to run (default ``main.py``).
  - ``runtime``       — only ``python3.12`` is accepted in the PoC.
  - ``variables``     — the injection schema the Runner uses to build
                        the ``docker exec -e`` env dict.
  - ``dependencies``  — pip specifiers; the Runner materialises these
                        into a per-script ``.deps/`` cache.
  - ``callback_manager`` — UI hint for the ``cubicle.notify_manager``
                        affordance (). Runtime does NOT gate
                        on this — any script can notify.

Validation is strict: a bad manifest fails fast at parse time with a
clear error, rather than producing a mysterious runtime failure
later. The Runner uses :meth:`ScriptManifest.env_from` to compute
the env dict to inject, merging manifest defaults with the user's
``variables.json`` and ``.secrets.json``.

Both the Communicator's Runner and (later) the backend's manifest
read/write endpoints parse through this same schema, so a manifest
that round-trips through the UI or API matches what the Runner will
actually execute.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

logger = logging.getLogger(__name__)

# Env-var identifier shape. Enforced on variable names so the
# docker-exec -e KEY=VALUE flag parser (and the script's own
# ``os.environ[KEY]`` lookup) never chokes on an exotic character.
_ENV_VAR_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# Names reserved for Runner-injected metadata. A manifest that
# declared one of these would shadow ``CUBICLE_SCRIPT_DIR`` et al.
# and silently break the script (e.g. the ``cubicle`` helper
# would look in the wrong directory). Reject at parse time so
# scriptmakers see the conflict early.
_RESERVED_VARIABLE_NAMES = frozenset({
    "PYTHONPATH",
    "CUBICLE_SCRIPT_DIR",
    "CUBICLE_SCRIPT_NAME",
    "CUBICLE_EXECUTION_ID",
    "CUBICLE_TASK_ID",
    # Per-task output directory injected by the Runner. Scripts
    # read this via ``cubicle.output_dir()`` (or directly via
    # ``os.environ['CUBICLE_OUTPUT_DIR']``). Declaring it as a
    # manifest variable would make the user's value clobber the
    # Runner's path, scattering outputs to unpredictable locations.
    "CUBICLE_OUTPUT_DIR",
})

# Entry point safety: must be a plain relative .py file. Not a
# perfect sandbox (the script can import anything inside its
# folder), but prevents obvious attempts to escape.
_ENTRY_POINT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-/]*\.py$")

# Supported runtimes. Future: "python3.13", "node", "bash".
# Unknown values reject at parse time so we don't silently
# run the wrong interpreter.
_SUPPORTED_RUNTIMES = frozenset({"python3.12"})

# Keep the manifest small — a multi-megabyte YAML is always a bug.
_MAX_MANIFEST_BYTES = 64 * 1024


class ManifestVariable(BaseModel):
    """A single declared variable the script expects at runtime."""

    model_config = {"extra": "forbid"}

    name: str
    type: Literal["string", "number", "boolean"] = "string"
    is_secret: bool = False
    description: str = ""
    # Optional default value. Stored as the author wrote it in YAML
    # (so numbers stay as numbers, strings as strings). The env-dict
    # builder stringifies it at injection time.
    default: str | int | float | bool | None = None

    @field_validator("name")
    @classmethod
    def _name_is_env_safe(cls, value: str) -> str:
        if not _ENV_VAR_NAME_RE.match(value):
            raise ValueError(
                f"variable name {value!r} is not a valid env-var "
                "identifier (must match ^[A-Z_][A-Z0-9_]*$)"
            )
        if value in _RESERVED_VARIABLE_NAMES:
            raise ValueError(
                f"variable name {value!r} is reserved for the Runner "
                "(it would shadow Runner-injected metadata and break "
                "helpers that read it). Choose a different name."
            )
        return value


class ScriptManifest(BaseModel):
    """Parsed + validated ``script.yaml`` contents."""

    model_config = {"extra": "forbid"}

    # Optional metadata. Name is usually redundant with the folder
    # name but manifests copied between offices benefit from carrying
    # a canonical label.
    name: str | None = None
    description: str = ""
    entry_point: str = "main.py"
    runtime: str = "python3.12"
    variables: list[ManifestVariable] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    callback_manager: bool = True

    @field_validator("entry_point")
    @classmethod
    def _entry_point_shape(cls, value: str) -> str:
        if not _ENTRY_POINT_RE.match(value):
            raise ValueError(
                f"entry_point {value!r} must be a relative .py "
                "file (e.g. 'main.py' or 'cmd/run.py')"
            )
        if ".." in value.split("/"):
            raise ValueError("entry_point must not contain '..' segments")
        return value

    @field_validator("runtime")
    @classmethod
    def _runtime_supported(cls, value: str) -> str:
        if value not in _SUPPORTED_RUNTIMES:
            raise ValueError(
                f"runtime {value!r} is not supported in this release "
                f"(accepted: {sorted(_SUPPORTED_RUNTIMES)})"
            )
        return value

    @field_validator("dependencies")
    @classmethod
    def _dependencies_are_plain_strings(cls, value: list[str]) -> list[str]:
        # Defense-in-depth only — we pass each dep as a separate
        # ``execve`` arg to pip, so no shell interprets these strings
        # and metachars can't inject a command. This check catches
        # OBVIOUS mistakes (a copy-pasted ``rm -rf /; pip install ...``
        # from a blog post) and fails them at manifest-parse time
        # with a clear message, rather than pip returning a
        # "requirement specifier parse error" 30 seconds later.
        #
        # Symbols like > < = ~ ! are part of version specifiers
        # (requests>=2.31, foo!=1.0) and MUST pass through.
        cleaned: list[str] = []
        _SUSPICIOUS = (";", "&", "|", "\n", "`", "$(")
        for dep in value:
            if not isinstance(dep, str):
                raise ValueError(f"dependency must be a string, got {type(dep)}")
            dep = dep.strip()
            if not dep:
                continue
            if any(bad in dep for bad in _SUSPICIOUS):
                raise ValueError(
                    f"dependency {dep!r} contains shell metacharacters; "
                    "use a plain pip requirement specifier"
                )
            cleaned.append(dep)
        return cleaned

    @model_validator(mode="after")
    def _variables_have_unique_names(self) -> ScriptManifest:
        seen: set[str] = set()
        for var in self.variables:
            if var.name in seen:
                raise ValueError(
                    f"duplicate variable name in manifest: {var.name}"
                )
            seen.add(var.name)
        return self

    @property
    def entry_module(self) -> str:
        """Translate ``entry_point`` into a Python ``-m`` module spec.

        ``main.py``            -> ``main``
        ``lib/foo/bar.py``     -> ``lib.foo.bar``

        The Runner calls ``python -m {entry_module}`` with PYTHONPATH
        pointing at the script folder + ``lib/`` + ``.deps/``, so
        package-style entry points work as long as each directory on
        the path has an ``__init__.py``.
        """
        stem = self.entry_point[:-3]  # strip ".py" (validator guarantees)
        return stem.replace("/", ".")

    def env_from(
        self,
        variable_values: dict[str, object],
        secrets: dict[str, object],
    ) -> dict[str, str]:
        """Build the ``docker exec -e`` env dict for this manifest.

        The Runner merges in this order (later wins):

            1. manifest ``default`` for each declared variable
            2. the user's ``variables.json`` (non-secret overrides)
            3. the user's ``.secrets.json`` (secret overrides)
            4. per-execution ``variable_overrides``

        This method handles steps 1–3; the Runner applies step 4 on
        top before calling us.

        Values are coerced to strings because env vars are strings.
        Booleans become ``"true"``/``"false"`` (standard shell
        convention). Numbers become their repr. Missing declared
        variables with no default AND no value are omitted — the
        script can decide whether that's fatal via its own
        ``os.environ[...]`` lookup.
        """
        merged: dict[str, object] = {}
        # 1. manifest defaults (only where a default is declared).
        for var in self.variables:
            if var.default is not None:
                merged[var.name] = var.default
        # 2. variables.json (overrides defaults for non-secrets, and
        # may also provide values for secrets declared without a
        # default — but by convention secrets live in .secrets.json).
        for key, value in variable_values.items():
            merged[key] = value
        # 3. .secrets.json (authoritative for any secret-marked var;
        # for non-secret keys this would be unusual but we accept it).
        for key, value in secrets.items():
            merged[key] = value
        # Only export variables DECLARED in the manifest. Anything
        # extra in variables.json is a leftover from an older version
        # of the script and would just clutter the env.
        declared = {v.name for v in self.variables}
        out: dict[str, str] = {}
        for key, value in merged.items():
            if key not in declared:
                continue
            out[key] = _stringify_env_value(value)
        return out


def _stringify_env_value(value: object) -> str:
    """Convert a manifest/JSON value into the string form the child
    process will see in ``os.environ``. Booleans use lowercase so
    they're idiomatic for ``$(case "$FLAG" in true|false)`` style
    checks inside scripts."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return str(value)


class ManifestError(ValueError):
    """Raised when a ``script.yaml`` is missing, unparseable, or fails
    schema validation. Carries a user-facing message suitable for
    surfacing in the UI without leaking stack traces."""


def load_manifest(script_dir: Path) -> ScriptManifest:
    """Parse and validate the manifest at ``{script_dir}/script.yaml``.

    Returns the validated :class:`ScriptManifest`. Raises
    :class:`ManifestError` (a ``ValueError`` subclass) on any
    problem — missing file, YAML syntax error, schema violation,
    size cap exceeded.
    """
    manifest_path = script_dir / "script.yaml"
    if not manifest_path.is_file():
        raise ManifestError(
            f"Manifest not found: {manifest_path} — every script is "
            "a mini-project and must have a script.yaml at its root"
        )
    size = manifest_path.stat().st_size
    if size > _MAX_MANIFEST_BYTES:
        raise ManifestError(
            f"script.yaml is {size} bytes, larger than the "
            f"{_MAX_MANIFEST_BYTES} byte cap — refusing to parse"
        )
    try:
        raw = yaml.safe_load(manifest_path.read_text())
    except yaml.YAMLError as exc:
        raise ManifestError(f"script.yaml is not valid YAML: {exc}") from exc
    if raw is None:
        raise ManifestError("script.yaml is empty")
    if not isinstance(raw, dict):
        raise ManifestError(
            f"script.yaml root must be a mapping, got {type(raw).__name__}"
        )
    try:
        return ScriptManifest.model_validate(raw)
    except ValidationError as exc:
        # Pydantic's ValidationError carries rich context but its
        # repr is a multi-line blob that renders badly in the UI
        # error toast. Extract the FIRST error's location + message
        # into a one-line summary — users fix one field at a time
        # anyway, and a future re-run surfaces the next issue.
        raise ManifestError(_format_validation_error(exc)) from exc
    except Exception as exc:
        raise ManifestError(f"script.yaml failed validation: {exc}") from exc


def _format_validation_error(exc: ValidationError) -> str:
    """Turn a Pydantic ValidationError into a single readable line.

    Shape: ``"script.yaml: <field>: <message>"``. When Pydantic
    surfaces nested issues (e.g. ``variables[2].type``), the full
    dotted path is preserved so scriptmakers can ctrl-F their YAML.
    """
    errors = exc.errors()
    if not errors:
        return "script.yaml failed validation (no details available)"
    first = errors[0]
    loc = first.get("loc") or ()
    # "variables", 2, "type" → "variables[2].type"
    parts: list[str] = []
    for segment in loc:
        if isinstance(segment, int):
            if parts:
                parts[-1] = f"{parts[-1]}[{segment}]"
            else:
                parts.append(f"[{segment}]")
        else:
            parts.append(str(segment))
    field_path = ".".join(parts) if parts else "<root>"
    msg = first.get("msg") or "validation failed"
    suffix = ""
    if len(errors) > 1:
        suffix = f" (+{len(errors) - 1} more issue(s))"
    return f"script.yaml: {field_path}: {msg}{suffix}"


