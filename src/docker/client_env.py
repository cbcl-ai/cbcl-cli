"""Environment names the host ``docker exec`` client needs from its own env.

The daemon starts every agent session (``docker/session_bridge.py``) and
every script (``scripts/script_runner.py``) with a ``docker exec`` client on
the HOST, whose environment also carries office-secret and script-variable
values (only their names are on its command line). The client finds its
binaries and home through PATH and HOME and reaches the Docker daemon through
the DOCKER_* settings, so neither path lets a secret or variable replace
them. The dynamic-loader names (``src/host_loader_env.py``) are kept out the
same way.
"""

from __future__ import annotations

DOCKER_CLIENT_ENV_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "DOCKER_HOST",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    }
)
