"""Configuration management for the Cubicle Communicator.

Config file: ~/.cubicle/config.yaml
Credentials: ~/.cubicle/credentials.env
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import yaml

from src.paths import (
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

    ``container_cpus`` / ``container_memory`` are the per-office
    container resource-limit OVERRIDES (Office Settings → Resources,
    stored backend-side and shipped on both the discovery payload and
    ``sync_config``). ``None`` means "no override" — the host-global
    chain applies (``CBCL_OFFICE_CPUS``/``CBCL_OFFICE_MEMORY`` env →
    ``office_cpus``/``office_memory`` in ``~/.cubicle/config.yaml`` →
    built-in defaults). Like ``extra_mounts``, they apply at container
    CREATE time only; the sync-driven reconciler recreates the
    container (when idle) to apply a change.
    """

    id: str
    name: str
    extra_mounts: list[dict] = field(default_factory=list)
    container_cpus: float | None = None
    container_memory: str | None = None

    @property
    def workspace_path(self) -> str:
        """Derived workspace path: ``~/.cubicle/workspaces/{slug}/``."""
        return str(get_workspace_path(slugify(self.name)))


# Production platform URL — the public Cubicle platform serves both
# the SPA (https://app.cbcl.ai) and its REST + WebSocket API at
# ``/api/...`` on the same origin. There is no separate
# ``api.cbcl.ai`` subdomain; the cbcl daemon hits
# ``https://app.cbcl.ai/api/communicator/offices`` for discovery and
# ``wss://app.cbcl.ai/ws/connector/{oid}`` for the live connection.
#
# Developers running the platform locally override at runtime via
# the ``CBCL_PLATFORM_URL`` env var (e.g. ``CBCL_PLATFORM_URL=
# http://localhost:8000 cbcl setup``), or by setting ``platform_url``
# in ``~/.cubicle/config.yaml``. The env var beats the stored config
# beats this hardcoded default.
_PLATFORM_URL_DEFAULT = "https://app.cbcl.ai"


# Pre-domain-cutover IP. Any stored ``platform_url`` whose hostname
# matches gets transparently replaced with ``_PLATFORM_URL_DEFAULT``
# so legacy installs auto-heal on the next ``cbcl start``. Using
# ``urlparse(...).hostname`` instead of enumerating scheme+port
# combinations survives a future port change without a code edit.
_LEGACY_IP_HOST = "46.224.71.1"


def _is_legacy_platform_url(url: str) -> bool:
    """True iff ``url`` points at the pre-domain-cutover IP."""
    # urlparse returns hostname=None for malformed strings (no
    # exception). The == comparison handles None safely so no
    # try/except wrapper needed.
    return urlparse(url).hostname == _LEGACY_IP_HOST


def _resolve_default_platform_url() -> str:
    """Pick the default URL for a fresh ``Config()`` (no stored file).

    Precedence: ``CBCL_PLATFORM_URL`` env var → hardcoded default.

    The stored-config branch (a YAML file with ``platform_url`` set)
    is handled separately in :func:`load_config`, which extends this
    precedence to: env var → stored → hardcoded. Direct ``Config()``
    construction (CLI test paths, fresh-install setup) skips the
    stored-value awareness, which is intentional — anyone who can
    set the env var can also edit the YAML.
    """
    return os.environ.get("CBCL_PLATFORM_URL", "").strip() or _PLATFORM_URL_DEFAULT


@dataclass
class Config:
    platform_url: str = field(default_factory=_resolve_default_platform_url)
    anthropic_api_key: str = ""
    security_token: str = ""  # cbcl_co_ Company Token for platform auth
    # Local Redis URL — written by ``ensure_redis()`` after Docker
    # assigns a free ephemeral port. Empty string means "fall back
    # to redis://localhost:6379/0" (legacy default; only applies on
    # hosts where the operator already runs Redis manually on the
    # standard port).
    redis_url: str = ""


# ---------------------------------------------------------------------------
# Office-container resource limits (office_cpus / office_memory)
# ---------------------------------------------------------------------------
#
# Historically the office container's Docker limits were hard-coded in
# ``container_manager.py`` (4 CPUs / 8 GB). That capped Claude Code
# dynamic-workflow subagent concurrency — the workflow runtime sizes
# its parallel-subagent pool from visible cores (≈ cores − 2), so a
# 4-CPU container tops out at ~2 concurrent subagents — and starved
# tool execution on busy offices. These knobs make the limits
# user-configurable:
#
#   ~/.cubicle/config.yaml:
#       office_cpus: 8        # float, CPUs per office container (1..64)
#       office_memory: 16g    # \d+[gm] — Docker mem_limit string
#
#   Env overrides (env wins over YAML):
#       CBCL_OFFICE_CPUS=8 CBCL_OFFICE_MEMORY=16g cbcl start
#
# Invalid values NEVER crash the daemon: each key independently logs a
# WARNING and falls back to its default (an invalid env value falls all
# the way to the default — it does not fall through to the YAML value,
# because an operator who set the env var meant to override the file).
#
# The limits are applied by ``container_manager.start_office`` at
# container CREATE time only — changing them requires the office
# container to be recreated (``cbcl stop && cbcl start``; see the
# docblock there).

DEFAULT_OFFICE_CPUS = 4.0
DEFAULT_OFFICE_MEMORY = "8g"

_OFFICE_CPUS_MIN = 1.0
_OFFICE_CPUS_MAX = 64.0

# Docker mem_limit shorthand we accept: an integer count of gigabytes
# or megabytes ("8g", "512m"). Matched case-insensitively and
# normalised to lowercase. Deliberately narrower than everything
# Docker itself accepts ("1.5g", "8gb", raw bytes) — one canonical
# shape keeps validation, logs, and docs unambiguous.
_OFFICE_MEMORY_RE = re.compile(r"^\d+[gm]$")


@dataclass(frozen=True)
class OfficeResourceLimits:
    """Resolved per-office-container Docker resource limits."""

    cpus: float = DEFAULT_OFFICE_CPUS
    memory: str = DEFAULT_OFFICE_MEMORY


def _coerce_office_cpus(raw: object, source: str) -> float | None:
    """Validate an ``office_cpus`` candidate. None = invalid (warned)."""
    # bool is an int subclass; ``office_cpus: true`` in YAML would
    # otherwise silently become 1.0 — reject it as a config mistake.
    if isinstance(raw, bool) or raw is None:
        _config_logger.warning(
            "Invalid office_cpus from %s: %r — falling back to default %s",
            source, raw, DEFAULT_OFFICE_CPUS,
        )
        return None
    try:
        cpus = float(str(raw).strip())
    except (TypeError, ValueError):
        _config_logger.warning(
            "Invalid office_cpus from %s: %r (not a number) — "
            "falling back to default %s",
            source, raw, DEFAULT_OFFICE_CPUS,
        )
        return None
    if not (_OFFICE_CPUS_MIN <= cpus <= _OFFICE_CPUS_MAX):
        _config_logger.warning(
            "office_cpus from %s out of range [%s..%s]: %r — "
            "falling back to default %s",
            source, _OFFICE_CPUS_MIN, _OFFICE_CPUS_MAX, raw,
            DEFAULT_OFFICE_CPUS,
        )
        return None
    return cpus


def _coerce_office_memory(raw: object, source: str) -> str | None:
    """Validate an ``office_memory`` candidate. None = invalid (warned)."""
    if raw is None or isinstance(raw, bool):
        value = ""
    else:
        value = str(raw).strip().lower()
    if not _OFFICE_MEMORY_RE.match(value):
        _config_logger.warning(
            "Invalid office_memory from %s: %r (expected \\d+[gm], "
            "e.g. '8g' or '512m') — falling back to default %r",
            source, raw, DEFAULT_OFFICE_MEMORY,
        )
        return None
    return value


def _read_config_yaml_lenient() -> dict:
    """Best-effort read of ``~/.cubicle/config.yaml``.

    Unlike :func:`load_config` this NEVER raises: a missing file,
    unreadable file, parse error, or non-dict top level all yield
    ``{}``. Used by :func:`get_office_resource_limits`, which is
    called from the container-create path — a malformed config file
    must degrade to defaults, not crash the daemon mid-start.
    """
    config_file = get_config_path()
    try:
        with open(config_file) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        return {}
    except (OSError, yaml.YAMLError) as exc:
        _config_logger.warning(
            "Could not read %s (%s) — office resource limits fall "
            "back to defaults", config_file, exc,
        )
        return {}
    return data if isinstance(data, dict) else {}


def get_office_resource_limits() -> OfficeResourceLimits:
    """Resolve the office-container CPU/memory limits.

    Per-key precedence: ``CBCL_OFFICE_CPUS`` / ``CBCL_OFFICE_MEMORY``
    env var → ``office_cpus`` / ``office_memory`` in
    ``~/.cubicle/config.yaml`` → defaults (4 CPUs / "8g"). Invalid
    values WARN and fall back to the default for that key — this
    function never raises. Read fresh on every call (no caching) so
    the value applied is whatever is configured at container-create
    time.
    """
    data = _read_config_yaml_lenient()

    cpus = DEFAULT_OFFICE_CPUS
    env_cpus = os.environ.get("CBCL_OFFICE_CPUS", "").strip()
    if env_cpus:
        cpus = _coerce_office_cpus(env_cpus, "env CBCL_OFFICE_CPUS")
        cpus = DEFAULT_OFFICE_CPUS if cpus is None else cpus
    elif "office_cpus" in data:
        cpus = _coerce_office_cpus(
            data.get("office_cpus"), "config.yaml office_cpus",
        )
        cpus = DEFAULT_OFFICE_CPUS if cpus is None else cpus

    memory = DEFAULT_OFFICE_MEMORY
    env_memory = os.environ.get("CBCL_OFFICE_MEMORY", "").strip()
    if env_memory:
        memory = _coerce_office_memory(env_memory, "env CBCL_OFFICE_MEMORY")
        memory = DEFAULT_OFFICE_MEMORY if memory is None else memory
    elif "office_memory" in data:
        memory = _coerce_office_memory(
            data.get("office_memory"), "config.yaml office_memory",
        )
        memory = DEFAULT_OFFICE_MEMORY if memory is None else memory

    return OfficeResourceLimits(cpus=cpus, memory=memory)


def coerce_per_office_cpus(raw: object, source: str) -> float | None:
    """Validate a PER-OFFICE ``container_cpus`` override.

    ``None`` in = "no override" out (no warning). An invalid value
    WARNs (via :func:`_coerce_office_cpus`) and yields ``None`` — the
    office falls back to the host-global chain rather than crashing
    or half-applying.
    """
    if raw is None:
        return None
    return _coerce_office_cpus(raw, source)


def coerce_per_office_memory(raw: object, source: str) -> str | None:
    """Validate a PER-OFFICE ``container_memory`` override.

    Same ``None``-passthrough + warn-and-drop semantics as
    :func:`coerce_per_office_cpus`.
    """
    if raw is None:
        return None
    return _coerce_office_memory(raw, source)


def resolve_office_resource_limits(
    container_cpus: object = None, container_memory: object = None,
) -> OfficeResourceLimits:
    """Resolve container limits WITH the per-office overrides applied.

    Per-key precedence (highest first):

    1. The per-office backend value (``container_cpus`` /
       ``container_memory`` on the office row — set in Office
       Settings → Resources; carried on the discovery payload and
       in every ``sync_config``). An explicit per-office value beats
       the host-global chain INCLUDING the env override — the UI
       value is the operator's most specific intent.
    2. The host-global chain of :func:`get_office_resource_limits`
       (env → ``~/.cubicle/config.yaml`` → built-in defaults).

    Invalid per-office values WARN and fall back to the host-global
    chain for that key — this function never raises.
    """
    host = get_office_resource_limits()

    cpus = host.cpus
    if container_cpus is not None:
        coerced_cpus = _coerce_office_cpus(
            container_cpus, "per-office container_cpus",
        )
        if coerced_cpus is not None:
            cpus = coerced_cpus

    memory = host.memory
    if container_memory is not None:
        coerced_memory = _coerce_office_memory(
            container_memory, "per-office container_memory",
        )
        if coerced_memory is not None:
            memory = coerced_memory

    return OfficeResourceLimits(cpus=cpus, memory=memory)


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

    # Env var beats stored config so devs can point a daemon at a
    # local backend without rewriting ``~/.cubicle/config.yaml``.
    # The hardcoded prod URL is the last fallback.
    env_url = os.environ.get("CBCL_PLATFORM_URL", "").strip()
    stored_url = (data.get("platform_url") or "").strip()
    # Auto-heal stored URLs pointing at the pre-domain-cutover IP
    # (now firewalled). Persist the heal so the next load doesn't
    # repeat the YAML-parse + rewrite cycle; operators who manually
    # re-set the legacy IP see one INFO line per healing event in
    # the log instead of a silent in-memory override.
    healed = False
    if _is_legacy_platform_url(stored_url):
        stored_url = ""
        healed = True
    platform_url = env_url or stored_url or _PLATFORM_URL_DEFAULT

    config = Config(
        platform_url=platform_url,
        anthropic_api_key=data.get("anthropic_api_key", ""),
        security_token=data.get("security_token", ""),
        redis_url=data.get("redis_url", ""),
    )
    if healed:
        logging.getLogger(__name__).info(
            "Auto-healed legacy platform_url=%r → %r (persisted to %s)",
            _LEGACY_IP_HOST, config.platform_url, config_file,
        )
        try:
            save_config(config)
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "Auto-heal: failed to persist healed platform_url "
                "(in-memory value still applied): %s", exc,
            )
    return config


def save_config(config: Config) -> None:
    """Save config to ``~/.cubicle/config.yaml``.

    Preserves keys the :class:`Config` dataclass doesn't manage
    (``office_cpus``, ``office_memory``, ``redis_url``, anything an
    operator hand-added): the existing file is re-read and the managed
    keys are merged over it. Before this merge a ``cbcl setup`` re-run
    (or the legacy-URL auto-heal) rewrote the file with only the three
    managed keys, silently dropping hand-edited settings.
    """
    ensure_config_dir()
    config_file = get_config_path()

    data = _read_config_yaml_lenient()
    data.update({
        "platform_url": config.platform_url,
        "anthropic_api_key": config.anthropic_api_key,
        "security_token": config.security_token,
    })

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


def _discovery_url(platform_url: str) -> str:
    """Build the discovery endpoint URL, tolerant of trailing slashes."""
    return f"{platform_url.rstrip('/')}{_DISCOVERY_PATH}"


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

    url = _discovery_url(platform_url)
    # 30s, not 10s, because the wizard's office-creation flow fires
    # 11 parallel agent-generation calls + 44 parallel skill-generation
    # calls against the same backend, each of which holds a worker
    # thread waiting on Claude. Under that load the discovery poll's
    # response time spikes past 10s and the poll repeatedly times out
    # with a (str-empty) httpx.ReadTimeout — which, combined with the
    # 15s poll interval, can leave a brand-new office un-discovered
    # for many cycles. 30s is still well under the 60s
    # connector-presence TTL so a runaway hang can't keep the daemon
    # offline indefinitely.
    async with httpx.AsyncClient(timeout=30.0) as client:
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

    url = _discovery_url(platform_url)
    resp = httpx.get(
        url, headers=_discovery_headers(security_token), timeout=30.0,
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
    just sees an empty list and adds no extra mounts. Same posture
    for the per-office resource limits (``container_cpus`` /
    ``container_memory``): absent or invalid → ``None`` → the
    host-global limit chain applies.
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
    office_name = item.get("name", "?")
    return OfficeConfig(
        id=item["id"], name=item["name"], extra_mounts=mounts,
        container_cpus=coerce_per_office_cpus(
            item.get("container_cpus"),
            f"discovery payload (office '{office_name}')",
        ),
        container_memory=coerce_per_office_memory(
            item.get("container_memory"),
            f"discovery payload (office '{office_name}')",
        ),
    )


# ---------------------------------------------------------------------------
# Scoped API key store — avoids polluting os.environ globally
# ---------------------------------------------------------------------------

_configured_key: str = ""


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
