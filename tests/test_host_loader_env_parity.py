"""The daemon and the backend reserve the same dynamic-loader names.

The backend refuses office-secret and skill-secret names with
``app/office_secrets/schemas.reserved_loader_env_violation``; the daemon
drops the same names before an office secret or a script variable reaches a
host process (``src/host_loader_env.is_loader_env_name``, used by
``docker/session_bridge.py`` and ``scripts/script_runner.py``). The two rules
are separate copies on either side of the platform boundary, so a name added
to one side only would let it through the other. In a standalone CLI
checkout the backend comparisons skip.
"""

from __future__ import annotations

import pytest

from src import host_loader_env
from tests import backend_boundary

NAMES = [
    # Reserved, in any case.
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "ld_preload",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "dyld_fallback_library_path",
    # Not reserved: near misses and ordinary credentials.
    "LD_PRELOADER",
    "MY_LD_PRELOAD",
    "LDAP_URL",
    "DYLD",
    "OPENAI_API_KEY",
    "",
]


def _backend():
    return backend_boundary.import_backend("app.office_secrets.schemas")


def test_both_sides_reserve_the_same_names_and_prefixes():
    schemas = _backend()
    assert host_loader_env.LOADER_ENV_NAMES == schemas.RESERVED_LOADER_ENV_NAMES
    assert host_loader_env.LOADER_ENV_PREFIXES == schemas.RESERVED_LOADER_ENV_PREFIXES


@pytest.mark.parametrize("name", NAMES)
def test_both_sides_agree_on_each_name(name):
    schemas = _backend()
    daemon = host_loader_env.is_loader_env_name(name)
    backend = schemas.reserved_loader_env_violation(name) is not None
    assert daemon == backend, name


def test_the_corpus_covers_both_outcomes():
    verdicts = {host_loader_env.is_loader_env_name(name) for name in NAMES}
    assert verdicts == {True, False}


def test_the_session_bridge_and_script_runner_use_the_shared_rule():
    """Both host ``docker exec`` paths keep the loader names and the docker
    client's own connection names out, from the one definition of each."""
    from src.docker import session_bridge
    from src.docker.client_env import DOCKER_CLIENT_ENV_NAMES
    from src.scripts import script_runner

    for owned in (session_bridge._SESSION_OWNED_ENV, script_runner._CLIENT_OWNED_ENV):
        assert host_loader_env.LOADER_ENV_NAMES <= owned
        assert DOCKER_CLIENT_ENV_NAMES <= owned
