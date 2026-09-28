"""Import ``_mcp_script_exec`` outside the agent image, without leaking.

``_mcp_script_exec`` imports its sibling ``_mcp_backend`` (resolvable only in
the image's flat ``/opt/cubicle/`` layout), at module level and lazily inside
functions. Tests therefore install a minimal ``_mcp_backend`` stub and put the
agent-image directory on ``sys.path`` while the importing test module runs.

The stub must not outlive that test module: a later module that imports the
real ``_mcp_backend`` (``mcp_tool_server``, the tool-catalog pins) would get
the incomplete stub from ``sys.modules`` and fail with an ImportError, so the
suite passed or failed depending on test order. :func:`stubbed_mcp_script_exec`
removes the stub and the agent-image modules it loaded on exit. Other modules
imported meanwhile (stdlib, third-party) stay: popping them would give a later
import a second module object whose classes fail ``isinstance`` checks against
the ones earlier code already holds.
"""
from __future__ import annotations

import contextlib
import importlib
import pathlib
import sys
import types
from collections.abc import Iterator

AGENT_IMAGE_DIR = (
    pathlib.Path(__file__).resolve().parent.parent / "src" / "_agent_image"
)


def _from_agent_image(module: types.ModuleType | None) -> bool:
    location = getattr(module, "__file__", None)
    return bool(location) and pathlib.Path(location).resolve().is_relative_to(
        AGENT_IMAGE_DIR
    )


@contextlib.contextmanager
def stubbed_mcp_script_exec() -> Iterator[types.ModuleType]:
    """Yield the real ``_mcp_script_exec`` imported against a stub backend.

    Keeps the stub and the import path in place until exit (the module's
    lazy imports resolve through them), then removes the stub and every
    agent-image module loaded meanwhile, restores ``sys.path`` and drops any
    attribute it added to a pre-existing ``_mcp_backend``.
    """
    before = set(sys.modules)
    stub = sys.modules.get("_mcp_backend")
    added_attrs: list[str] = []
    if stub is None:
        stub = types.ModuleType("_mcp_backend")
        sys.modules["_mcp_backend"] = stub
    if not hasattr(stub, "_get_session"):
        stub._get_session = lambda *a, **k: None
        added_attrs.append("_get_session")
    if not hasattr(stub, "_call_backend"):
        async def _call_backend(action, params):  # overridden per test
            return {}
        stub._call_backend = _call_backend
        added_attrs.append("_call_backend")
    path_added = str(AGENT_IMAGE_DIR) not in sys.path
    if path_added:
        sys.path.insert(0, str(AGENT_IMAGE_DIR))
    try:
        yield importlib.import_module("_mcp_script_exec")
    finally:
        if path_added:
            sys.path.remove(str(AGENT_IMAGE_DIR))
        for name in set(sys.modules) - before:
            if name == "_mcp_backend" or _from_agent_image(sys.modules.get(name)):
                sys.modules.pop(name, None)
        if "_mcp_backend" in before:
            for attr in added_attrs:
                delattr(sys.modules["_mcp_backend"], attr)
