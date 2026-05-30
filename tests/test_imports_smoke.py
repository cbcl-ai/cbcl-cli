"""Project-wide import / parse smoke test (IMP-02).

Many communicator modules are imported INLINE (inside function bodies),
often from fire-and-forget ``asyncio.create_task`` coroutines whose
exceptions are swallowed. A syntax/import error in such a module does
NOT fail at process start — it only fires the first time that path runs,
and the traceback can be lost. That is exactly how the "manual script
runs stuck at running forever" outage happened: a dangling ``try:`` made
``scripts/script_execution.py`` a SyntaxError for ~5 weeks because
nothing imported it eagerly.

This test makes any such error fail CI immediately:

* Every module under ``src/`` (except ``_agent_image/``) is imported.
* ``_agent_image/`` modules run INSIDE the agent container with a
  different ``sys.path`` (``/opt/cubicle``) and use bare imports like
  ``from _mcp_backend import ...`` that don't resolve from the daemon's
  ``src.`` package — so we byte-compile-check them with ``ast.parse``
  instead of importing.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

COMMUNICATOR_ROOT = Path(__file__).resolve().parent.parent
SRC = COMMUNICATOR_ROOT / "src"
AGENT_IMAGE = SRC / "_agent_image"


def _py_files(root: Path) -> list[Path]:
    return sorted(
        p
        for p in root.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def _module_name(path: Path) -> str:
    # communicator/src/scripts/foo.py -> "src.scripts.foo"
    rel = path.relative_to(COMMUNICATOR_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def test_all_daemon_modules_import() -> None:
    """Import every daemon-side module so a syntax/import error in any of
    them (including ones only reached via inline imports) fails here."""
    failures: list[str] = []
    for path in _py_files(SRC):
        # _agent_image runs in the container; parse-check it separately.
        if AGENT_IMAGE == path or AGENT_IMAGE in path.parents:
            continue
        name = _module_name(path)
        if not name:
            continue
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 — we want every failure
            failures.append(f"  {name}: {type(exc).__name__}: {exc}")
    assert not failures, "Modules failed to import:\n" + "\n".join(failures)


def test_agent_image_modules_parse() -> None:
    """``_agent_image/`` modules can't be imported from the daemon's
    sys.path (bare ``/opt/cubicle`` imports), so syntax-check them."""
    failures: list[str] = []
    for path in _py_files(AGENT_IMAGE):
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            failures.append(f"  {path}: {exc}")
    assert not failures, "Agent-image modules failed to parse:\n" + "\n".join(
        failures
    )
