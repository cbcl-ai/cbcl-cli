"""``stubbed_mcp_script_exec`` removes only what it owns on exit.

Popping an unrelated module (stdlib or third-party) imported lazily inside
the context makes the next import create a second module object while
earlier-imported code keeps the old classes, so ``isinstance`` and ``except``
checks disagree depending on test order.
"""
from __future__ import annotations

import pathlib
import sys

from tests.agent_image_stub import AGENT_IMAGE_DIR, stubbed_mcp_script_exec

_OWNED = ("_mcp_backend", "_mcp_script_exec", "_mcp", "_mcp.capacity_wait")


def test_exit_keeps_unrelated_modules_and_drops_the_agent_image(
    tmp_path: pathlib.Path,
) -> None:
    module_name = f"stub_probe_{tmp_path.name.replace('-', '_')}"
    (tmp_path / f"{module_name}.py").write_text("class Marker:\n    pass\n")
    sys.path.insert(0, str(tmp_path))
    before = set(sys.modules)
    image_on_path = str(AGENT_IMAGE_DIR) in sys.path
    try:
        with stubbed_mcp_script_exec() as module:
            assert pathlib.Path(module.__file__).parent == AGENT_IMAGE_DIR
            probe = __import__(module_name)
        # An unrelated module imported meanwhile stays the same object.
        assert sys.modules.get(module_name) is probe
        # The stub and the agent-image modules it pulled in are gone.
        leaked = [
            name for name in _OWNED if name not in before and name in sys.modules
        ]
        assert leaked == []
        assert (str(AGENT_IMAGE_DIR) in sys.path) == image_on_path
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop(module_name, None)
