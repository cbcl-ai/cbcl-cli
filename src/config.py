"""Configuration management for the Cubicle Communicator.

Config file: ~/.cubicle/config.yaml
Credentials: ~/.cubicle/credentials.env
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src.paths import (
    CUBICLE_HOME,
    ensure_cubicle_dirs,
    get_config_path,
    get_credentials_path,
    get_workspace_path,
    slugify,
)


@dataclass
class OfficeConfig:
    """Office discovered from the platform server.

    ``extra_mounts`` is the per-office "Security → Extra mounts"
    list — additional Docker volume mounts the user has configured
    (e.g. SSH key into ``/home/agent/.ssh/``). Each item is
    ``{host_path: str, container_path: str, read_only: bool}``.
    The Communicator merges these into the docker volumes dict
    on container (re)create.
    """

    id: str
    name: str
    extra_mounts: list[dict] = field(default_factory=list)

    @property
    def workspace_path(self) -> str:
        """Derived workspace path: ``~/.cubicle/workspaces/{slug}/``."""
        return str(get_workspace_path(slugify(self.name)))


@dataclass
class Config:
    platform_url: str = "http://localhost:8000"
    anthropic_api_key: str = ""
    execution_mode: str = "docker"  # "docker" or "direct"
    deployment_mode: str = "local"  # "local" or "remote"
    security_token: str = ""  # cbcl_co_ Company Token for platform auth


def ensure_config_dir() -> None:
    """Create ``~/.cubicle/`` and subdirectories if they do not exist."""
    ensure_cubicle_dirs()


def config_exists() -> bool:
    """Check if the config file exists."""
    return get_config_path().exists()


def load_config() -> Config:
    """Load config from ``~/.cubicle/config.yaml``."""
    config_file = get_config_path()
    if not config_file.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_file}\n"
            "Run 'cbcl setup' to create one."
        )

    with open(config_file) as f:
        data = yaml.safe_load(f) or {}

    return Config(
        platform_url=data.get("platform_url", "http://localhost:8000"),
        anthropic_api_key=data.get("anthropic_api_key", ""),
        execution_mode=data.get("execution_mode", "docker"),
        deployment_mode=data.get("deployment_mode", "local"),
        security_token=data.get("security_token", ""),
    )


def save_config(config: Config) -> None:
    """Save config to ``~/.cubicle/config.yaml``."""
    ensure_config_dir()
    config_file = get_config_path()

    data = {
        "platform_url": config.platform_url,
        "anthropic_api_key": config.anthropic_api_key,
        "execution_mode": config.execution_mode,
        "deployment_mode": config.deployment_mode,
        "security_token": config.security_token,
    }

    with open(config_file, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    os.chmod(config_file, 0o600)


def ensure_credentials_file() -> None:
    """Create empty ``credentials.env`` if it does not exist."""
    ensure_config_dir()
    creds_file = get_credentials_path()
    if not creds_file.exists():
        with open(creds_file, "w") as f:
            f.write(
                "# Cubicle integration credentials\n"
                "# Add tokens for third-party services here.\n"
                "# Example: SLACK_BOT_TOKEN=xoxb-...\n"
            )
        os.chmod(str(creds_file), 0o600)


# ---------------------------------------------------------------------------
# Office discovery — fetch from platform server
# ---------------------------------------------------------------------------


_DISCOVERY_PATH = "/api/communicator/offices"


def _discovery_headers(security_token: str | None) -> dict[str, str]:
    """Return Authorization headers for the Bearer-authed discovery endpoint.

    The Communicator authenticates with a Company Token
    (``cbcl_co_...``) — the SAME token used for the /ws/connector
    handshake. We do not fall back to the cookie-only ``/api/offices``
    endpoint here because that path requires an Employee browser
    session, which a headless daemon does not have.
    """
    if not security_token:
        return {}
    return {"Authorization": f"Bearer {security_token}"}


async def fetch_offices(
    platform_url: str, security_token: str | None = None,
) -> list[OfficeConfig]:
    """Fetch offices visible to this Communicator from the platform.

    Calls ``GET /api/communicator/offices`` with the Company Token
    Bearer header. The endpoint filters STRICTLY by token affinity:
    only offices whose ``company_token_id`` equals THIS token's id
    are returned. Multiple tokens per Company = multi-machine
    deployments; each daemon sees only the offices the user has
    bound to it via Office Settings > Connection. Unassigned
    offices (``company_token_id IS NULL``) are invisible to every
    daemon and the empty-list path is the user's signal to bind
    one in the UI.

    Raises :class:`ConnectionError` if the server is unreachable.
    Raises :class:`httpx.HTTPStatusError` on 4xx/5xx (most commonly 401
    when no token is configured — see CLI-010).
    """
    import httpx

    url = f"{platform_url.rstrip('/')}{_DISCOVERY_PATH}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers=_discovery_headers(security_token))
        resp.raise_for_status()

    offices = []
    for item in resp.json():
        offices.append(_office_from_payload(item))
    return offices


def fetch_offices_sync(
    platform_url: str, security_token: str | None = None,
) -> list[OfficeConfig]:
    """Synchronous version of :func:`fetch_offices` for CLI commands."""
    import httpx

    url = f"{platform_url.rstrip('/')}{_DISCOVERY_PATH}"
    resp = httpx.get(
        url, headers=_discovery_headers(security_token), timeout=10.0,
    )
    resp.raise_for_status()

    offices = []
    for item in resp.json():
        offices.append(_office_from_payload(item))
    return offices


def _office_from_payload(item: dict) -> OfficeConfig:
    """Parse a discovery-endpoint office row.

    ``extra_mounts`` is optional in the payload so older backend
    builds that don't ship the field still work — the Communicator
    just sees an empty list and adds no extra mounts.
    """
    raw_mounts = item.get("extra_mounts") or []
    mounts: list[dict] = []
    if isinstance(raw_mounts, list):
        for m in raw_mounts:
            if not isinstance(m, dict):
                continue
            host_path = str(m.get("host_path") or "").strip()
            container_path = str(m.get("container_path") or "").strip()
            if not host_path or not container_path:
                continue
            mounts.append({
                "host_path": host_path,
                "container_path": container_path,
                "read_only": bool(m.get("read_only", True)),
            })
    return OfficeConfig(
        id=item["id"], name=item["name"], extra_mounts=mounts,
    )


# ---------------------------------------------------------------------------
# Scoped API key store — avoids polluting os.environ globally
# ---------------------------------------------------------------------------

_configured_key: str = ""

import logging

_config_logger = logging.getLogger(__name__)


def set_api_key(key: str) -> None:
    """Store the configured API key.

    The key is passed as ``ANTHROPIC_API_KEY`` env var to Docker containers.
    The Claude CLI inside the container reads it natively.
    """
    global _configured_key
    _configured_key = key

    if key:
        _config_logger.info("API key configured")


def get_api_key() -> str:
    """Return the configured API key."""
    return _configured_key
