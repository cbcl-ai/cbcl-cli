"""Dynamic-loader environment names Cubicle never hands to a script or session.

The daemon starts every agent session and every script with a ``docker exec``
client that runs on the HOST. Office-secret and script-variable values ride
that client's environment (only their names are on its command line), so a
value named LD_PRELOAD would make the host load a library of its choosing
into the client. ``docker/session_bridge.py`` and
``scripts/script_runner.py`` drop such names with a WARNING, and the
in-container script executor (``_mcp_script_exec``) drops them too, so a
script sees the same variables whichever path launches it.

One definition on both sides of the container boundary, in this pure module:
the daemon imports it through ``src/host_loader_env.py``. The backend refuses
the same names as office-secret and skill-secret names
(``backend/app/office_secrets/schemas.py``, ``RESERVED_LOADER_ENV_*``);
``tests/test_host_loader_env_parity.py`` pins the two copies together.
"""

from __future__ import annotations

LOADER_ENV_NAMES = frozenset({"LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT"})
LOADER_ENV_PREFIXES = ("DYLD_",)

# Why such a name is refused as an office-secret reference, for the teaching
# errors that refuse one.
LOADER_REASON = (
    "LD_PRELOAD, LD_LIBRARY_PATH, LD_AUDIT and names starting with DYLD_ "
    "tell the host which library to load, and Cubicle never passes them to "
    "a run"
)


def is_loader_env_name(name: str) -> bool:
    """True when ``name`` (any case) tells the host loader what to load."""
    upper = (name or "").upper()
    return upper in LOADER_ENV_NAMES or upper.startswith(LOADER_ENV_PREFIXES)
