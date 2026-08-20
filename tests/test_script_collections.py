"""Scripts ↔ Collections (spec ui-ux-aug19 Item 4, D4.1-D4.6).

Covers the script lane into the office-local collections datastore:

- ``POST /collections/rpc`` on the ToolProxyServer (D4.1/D4.2): the
  action whitelist (``data_import`` excluded), narrow-token auth
  (narrow OR main token on the route; narrow token refused on every
  OTHER route), DatastoreError status mapping, the 503 before
  ``set_datastore``, the body cap, and the debounced
  ``collection_rows_changed`` daemon→backend event (D4.6);
- ``ScriptRunner.set_collections_endpoint`` + the docker-launch env
  injection (D4.3 — the name-only ``-e KEY`` mechanism preserved);
- the manifest + in-container reserved-name mirrors (D4.3);
- the agent env chain (``build_mcp_config`` maps the narrow token,
  never widening what scripts can see);
- the stdlib SDK's ``cubicle.collections`` client driven against a
  LIVE proxy (D4.4);
- the ``script_sync`` SDK backfill (D4.5).
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from src.datastore import OfficeDatastore
from src.tool_proxy_server import (
    _COLLECTIONS_RPC_ACTIONS,
    ToolProxyServer,
)

# ─── fixtures ──────────────────────────────────────────────────────────


def _field(name: str, ftype: str, **kw) -> dict:
    return {
        "name": name,
        "type": ftype,
        "options": kw.get("options", []),
        "ref_to": kw.get("ref_to", ""),
        "required": kw.get("required", False),
        "help": "",
    }


LEADS = {
    "name": "leads",
    "display_name": "Leads",
    "schema": [
        _field("company", "text", required=True),
        _field("headcount", "number"),
    ],
    "schema_revision": 1,
}


@pytest.fixture
def datastore(tmp_path):
    config_store = SimpleNamespace(collections=[LEADS])
    return OfficeDatastore(tmp_path / "office.sqlite", config_store)


@pytest.fixture
async def proxy(datastore):
    """A live proxy with the datastore wired + a fast debounce."""
    ws_client = MagicMock()
    ws_client.connected = True
    ws_client.send = AsyncMock()
    server = ToolProxyServer(
        ws_client=ws_client, port=0, host="127.0.0.1",
    )
    server.set_datastore(datastore)
    # Wide enough that the test's own write loop (3 local HTTP
    # round trips) can never straddle a window under CI load —
    # the 0.05s original flaked when module import contention
    # pushed the loop past the window (2 flushes for 3 writes).
    server._rows_changed_debounce = 0.3
    await server.start()
    try:
        yield server, ws_client
    finally:
        await server.stop()


@pytest.fixture
async def proxy_unwired():
    """A live proxy WITHOUT set_datastore — the pre-wiring window."""
    ws_client = MagicMock()
    ws_client.connected = True
    ws_client.send = AsyncMock()
    server = ToolProxyServer(
        ws_client=ws_client, port=0, host="127.0.0.1",
    )
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def _rpc(
    server: ToolProxyServer,
    action: str,
    params: dict,
    *,
    token: str | None = "narrow",
) -> tuple[int, dict]:
    headers: dict[str, str] = {}
    if token == "narrow":
        headers["Authorization"] = f"Bearer {server.collections_token}"
    elif token == "main":
        headers["Authorization"] = f"Bearer {server.token}"
    elif token is not None:
        headers["Authorization"] = f"Bearer {token}"
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{server.port}/collections/rpc",
            json={"action": action, "params": params},
            headers=headers,
        ) as resp:
            return resp.status, await resp.json()


# ─── /collections/rpc: happy paths ─────────────────────────────────────


@pytest.mark.asyncio
async def test_rpc_round_trip_all_whitelisted_actions(proxy):
    """upsert → get → list → count → delete through the live route,
    with the datastore's verbatim per-action response shapes."""
    server, _ws = proxy

    status, body = await _rpc(server, "data_row_upsert", {
        "collection": "leads",
        "row_id": "acme",
        "data": {"company": "Acme Corp", "headcount": 40},
    })
    assert status == 200
    assert body["created"] is True
    assert body["row"]["id"] == "acme"
    assert body["row_count"] == 1

    status, body = await _rpc(server, "data_row_get", {
        "collection": "leads", "row_id": "acme",
    })
    assert status == 200
    assert body["row"]["data"]["company"] == "Acme Corp"

    status, body = await _rpc(server, "data_rows_list", {
        "collection": "leads", "search": "acme", "limit": 10,
    })
    assert status == 200
    assert body["total"] == 1
    assert body["rows"][0]["id"] == "acme"

    status, body = await _rpc(server, "data_rows_count", {
        "collection": "leads",
    })
    assert status == 200
    assert body == {"count": 1}

    status, body = await _rpc(server, "data_row_delete", {
        "collection": "leads", "row_id": "acme",
    })
    assert status == 200
    assert body["deleted"] is True
    assert body["row_count"] == 0


@pytest.mark.asyncio
async def test_rpc_accepts_main_token_too(proxy):
    """D4.2: the MAIN proxy token stays valid on /collections/rpc so
    agent-side callers need no second credential."""
    server, _ws = proxy
    status, body = await _rpc(
        server, "data_rows_count", {"collection": "leads"}, token="main",
    )
    assert status == 200
    assert body == {"count": 0}


# ─── /collections/rpc: auth ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rpc_rejects_missing_and_wrong_token(proxy):
    server, _ws = proxy
    status, body = await _rpc(
        server, "data_rows_count", {"collection": "leads"}, token=None,
    )
    assert status == 401
    assert body["error"] == "unauthorized"
    status, _body = await _rpc(
        server, "data_rows_count", {"collection": "leads"},
        token="not-the-token",
    )
    assert status == 401


@pytest.mark.asyncio
async def test_narrow_token_refused_on_other_routes(proxy):
    """The narrow token opens /collections/rpc ONLY — presenting it
    on /tool-call or /script-execute-host is a 401, so script code
    can never reach the agent tool surface or office-secret
    injection (the whole point of the second token, D4.2)."""
    server, _ws = proxy
    async with aiohttp.ClientSession() as session:
        for path in ("/tool-call", "/script-execute-host"):
            async with session.post(
                f"http://127.0.0.1:{server.port}{path}",
                json={"action": "get_board", "params": {}},
                headers={
                    "Authorization": (
                        f"Bearer {server.collections_token}"
                    ),
                },
            ) as resp:
                assert resp.status == 401, path


# ─── /collections/rpc: guards ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_rpc_503_before_set_datastore(proxy_unwired):
    server = proxy_unwired
    status, body = await _rpc(
        server, "data_rows_count", {"collection": "leads"},
    )
    assert status == 503
    assert "Restart cbcl" in body["error"]


@pytest.mark.asyncio
async def test_rpc_unknown_action_400_and_no_data_import(proxy):
    """The whitelist is the five row actions — ``data_import`` is
    deliberately NOT script-reachable in v1 (D4.1)."""
    server, _ws = proxy
    assert "data_import" not in _COLLECTIONS_RPC_ACTIONS

    status, body = await _rpc(server, "data_import", {
        "collection": "leads", "csv": "company\nAcme",
    })
    assert status == 400
    assert "unknown collections action" in body["error"]

    status, _body = await _rpc(server, "fs_read", {"path": "x"})
    assert status == 400


@pytest.mark.asyncio
async def test_rpc_datastore_error_status_mapping(proxy):
    """DatastoreError business failures map to their own status —
    404 unknown collection / missing row, 400 schema violation."""
    server, _ws = proxy
    status, body = await _rpc(server, "data_rows_count", {
        "collection": "nope",
    })
    assert status == 404
    assert "unknown collection" in body["error"]

    status, _body = await _rpc(server, "data_row_get", {
        "collection": "leads", "row_id": "missing",
    })
    assert status == 404

    status, body = await _rpc(server, "data_row_upsert", {
        "collection": "leads", "data": {"headcount": 3},
    })
    assert status == 400  # required "company" missing
    assert "error" in body


@pytest.mark.asyncio
async def test_rpc_malformed_bodies_400(proxy):
    server, _ws = proxy
    async with aiohttp.ClientSession() as session:
        headers = {
            "Authorization": f"Bearer {server.collections_token}",
            "Content-Type": "application/json",
        }
        url = f"http://127.0.0.1:{server.port}/collections/rpc"
        async with session.post(
            url, data=b"not json", headers=headers,
        ) as resp:
            assert resp.status == 400
        async with session.post(
            url, data=b'["a", "list"]', headers=headers,
        ) as resp:
            assert resp.status == 400
        async with session.post(
            url,
            data=json.dumps(
                {"action": "data_rows_count", "params": "nope"}
            ).encode(),
            headers=headers,
        ) as resp:
            assert resp.status == 400


@pytest.mark.asyncio
async def test_rpc_oversized_body_413(proxy):
    server, _ws = proxy
    big = {"action": "data_row_upsert", "params": {
        "collection": "leads",
        "data": {"company": "x" * (4 * 1024 * 1024 + 100)},
    }}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{server.port}/collections/rpc",
            json=big,
            headers={
                "Authorization": f"Bearer {server.collections_token}",
            },
        ) as resp:
            assert resp.status == 413


# ─── D4.6: the debounced collection_rows_changed event ─────────────────


@pytest.mark.asyncio
async def test_rows_changed_event_debounced_to_one_frame(proxy):
    """A tight write loop emits ONE frame per debounce window,
    carrying the LATEST row_count."""
    server, ws_client = proxy
    for i in range(3):
        status, _body = await _rpc(server, "data_row_upsert", {
            "collection": "leads",
            "row_id": f"row-{i}",
            "data": {"company": f"Co {i}"},
        })
        assert status == 200

    def _frames() -> list[dict]:
        return [
            call.args[0] for call in ws_client.send.await_args_list
            if call.args[0].get("type") == "collection_rows_changed"
        ]

    # Poll until the trailing flush lands, then settle one further
    # window to prove no SECOND frame follows it.
    for _ in range(60):
        if _frames():
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.4)
    rows_changed = _frames()
    assert len(rows_changed) == 1
    assert rows_changed[0] == {
        "type": "collection_rows_changed",
        "collection": "leads",
        "row_count": 3,
    }


@pytest.mark.asyncio
async def test_rows_changed_not_scheduled_for_reads(proxy):
    server, ws_client = proxy
    await _rpc(server, "data_rows_list", {"collection": "leads"})
    await _rpc(server, "data_rows_count", {"collection": "leads"})
    await asyncio.sleep(0.15)
    assert not [
        call for call in ws_client.send.await_args_list
        if call.args[0].get("type") == "collection_rows_changed"
    ]


# ─── D4.3: ScriptRunner env injection (docker launch path) ─────────────


def _runner(tmp_path, container: str | None = "cbcl-office-test"):
    from src.scripts.script_runner import ScriptRunner

    (tmp_path / ".scripts" / "test-script").mkdir(parents=True)
    return ScriptRunner(
        workspace_path=str(tmp_path),
        secrets_store=MagicMock(),
        variable_manager=MagicMock(),
        container_name=container,
    )


def _launch(runner, tmp_path, manifest_env: dict | None = None):
    from src.scripts.manifest import ScriptManifest

    script_dir = tmp_path / ".scripts" / "test-script"
    exec_dir = script_dir / "executions" / "exec-x"
    exec_dir.mkdir(parents=True, exist_ok=True)
    return runner._build_launch_command(
        script_dir=script_dir,
        manifest=ScriptManifest(),
        script_name="test-script",
        exec_id="exec-x",
        task_id=None,
        manifest_env=manifest_env or {},
        exec_dir=exec_dir,
    )


def test_docker_launch_injects_collections_endpoint(tmp_path):
    """After set_collections_endpoint, the docker launch env carries
    both vars — via the name-only ``-e KEY`` mechanism (NEW-4): names
    in argv, values only in the client env."""
    runner = _runner(tmp_path)
    runner.set_collections_endpoint(
        "http://host.docker.internal:9876", "narrow-tok",
    )
    argv, launch_env = _launch(runner, tmp_path)
    assert launch_env is not None
    assert (
        launch_env["CUBICLE_TOOL_PROXY_URL"]
        == "http://host.docker.internal:9876"
    )
    assert launch_env["CUBICLE_COLLECTIONS_TOKEN"] == "narrow-tok"
    # Name-only -e flags: the names ride argv, the VALUES never do.
    assert "CUBICLE_TOOL_PROXY_URL" in argv
    assert "CUBICLE_COLLECTIONS_TOKEN" in argv
    assert "narrow-tok" not in argv


def test_docker_launch_reserved_reassert_beats_hostile_manifest_env(
    tmp_path,
):
    """Defence in depth: even a manifest_env that somehow bypassed
    the parse-time reserved-name rejection cannot shadow the
    Runner-owned endpoint/credential."""
    runner = _runner(tmp_path)
    runner.set_collections_endpoint(
        "http://host.docker.internal:9876", "narrow-tok",
    )
    _argv, launch_env = _launch(runner, tmp_path, manifest_env={
        "CUBICLE_TOOL_PROXY_URL": "http://evil.example",
        "CUBICLE_COLLECTIONS_TOKEN": "stolen",
    })
    assert (
        launch_env["CUBICLE_TOOL_PROXY_URL"]
        == "http://host.docker.internal:9876"
    )
    assert launch_env["CUBICLE_COLLECTIONS_TOKEN"] == "narrow-tok"


def test_docker_launch_without_endpoint_injects_nothing(tmp_path):
    """Pre-Item-4 posture preserved: no endpoint wired → no vars —
    the SDK then raises its teaching error instead of half-working."""
    runner = _runner(tmp_path)
    argv, launch_env = _launch(runner, tmp_path)
    assert "CUBICLE_TOOL_PROXY_URL" not in launch_env
    assert "CUBICLE_COLLECTIONS_TOKEN" not in launch_env
    assert "CUBICLE_COLLECTIONS_TOKEN" not in argv


def test_host_fallback_never_injects_collections_endpoint(tmp_path):
    """host.docker.internal is meaningless outside the container —
    the host-fallback (test-only) path stays uninjected."""
    runner = _runner(tmp_path, container=None)
    runner.set_collections_endpoint(
        "http://host.docker.internal:9876", "narrow-tok",
    )
    _argv, env = _launch(runner, tmp_path)
    assert "CUBICLE_TOOL_PROXY_URL" not in env
    assert "CUBICLE_COLLECTIONS_TOKEN" not in env


# ─── D4.3: reserved names, both mirrors ────────────────────────────────


def test_manifest_rejects_reserved_collections_names():
    from pydantic import ValidationError

    from src.scripts.manifest import ManifestVariable

    for name in ("CUBICLE_TOOL_PROXY_URL", "CUBICLE_COLLECTIONS_TOKEN"):
        with pytest.raises(ValidationError, match="reserved"):
            ManifestVariable(name=name, type="string")


def test_reserved_mirrors_stay_in_lockstep():
    """The in-container mirror must equal the manifest's list — a
    name reserved on one launch path but declarable on the other
    would make the same manifest valid or invalid depending on who
    triggers the run. (Read via AST — ``_mcp_script_exec`` imports
    its in-container flat-module siblings, so a plain import needs
    the agent-image dir on sys.path.)"""
    import ast

    from src.scripts.manifest import _RESERVED_VARIABLE_NAMES

    source_path = (
        Path(__file__).resolve().parent.parent
        / "src" / "_agent_image" / "_mcp_script_exec.py"
    )
    tree = ast.parse(source_path.read_text())
    mirror: frozenset[str] | None = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and getattr(node.targets[0], "id", "") == "_RESERVED_ENV_NAMES"
        ):
            call = node.value
            assert isinstance(call, ast.Call)  # frozenset({...})
            mirror = frozenset(ast.literal_eval(call.args[0]))
    assert mirror is not None, "_RESERVED_ENV_NAMES not found"
    assert mirror == _RESERVED_VARIABLE_NAMES


# ─── D4.3: the agent env chain (in-container path) ─────────────────────


def _worker():
    return SimpleNamespace(
        backend_url="http://host.docker.internal:8000",
        office_id="ofc-1",
        agent_name="analyst",
    )


def _mcp_env(cfg: dict) -> dict:
    return cfg["mcpServers"]["cubicle-tools"]["env"]


def test_mcp_env_maps_narrow_collections_token(monkeypatch):
    from src._agent_worker_mcp import build_mcp_config

    monkeypatch.setenv(
        "CUBICLE_TOOL_PROXY_URL", "http://host.docker.internal:9876",
    )
    monkeypatch.setenv("CUBICLE_TOOL_PROXY_TOKEN", "main-tok")
    monkeypatch.setenv("CUBICLE_COLLECTIONS_TOKEN", "narrow-tok")
    cfg = build_mcp_config(_worker(), "worker", task_id="t-1")
    env = _mcp_env(cfg)
    assert env["TOOL_PROXY_TOKEN"] == "main-tok"
    assert env["COLLECTIONS_TOKEN"] == "narrow-tok"


def test_mcp_env_collections_token_absent_on_old_daemon(monkeypatch):
    from src._agent_worker_mcp import build_mcp_config

    monkeypatch.setenv(
        "CUBICLE_TOOL_PROXY_URL", "http://host.docker.internal:9876",
    )
    monkeypatch.setenv("CUBICLE_TOOL_PROXY_TOKEN", "main-tok")
    monkeypatch.delenv("CUBICLE_COLLECTIONS_TOKEN", raising=False)
    cfg = build_mcp_config(_worker(), "worker", task_id="t-1")
    assert "COLLECTIONS_TOKEN" not in _mcp_env(cfg)


# ─── D4.4: the SDK client against a live proxy ─────────────────────────


def _load_sdk():
    helper_path = (
        Path(__file__).resolve().parent.parent
        / "src" / "scripts" / "templates" / "cubicle_helper.py"
    )
    spec = importlib.util.spec_from_file_location(
        "cubicle_sdk_under_test", helper_path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_sdk_round_trip_against_live_proxy(proxy, monkeypatch):
    """The stdlib client end to end: upsert → get → query → count →
    delete over real HTTP with the narrow token."""
    server, _ws = proxy
    sdk = _load_sdk()
    monkeypatch.setenv(
        "CUBICLE_TOOL_PROXY_URL", f"http://127.0.0.1:{server.port}",
    )
    monkeypatch.setenv(
        "CUBICLE_COLLECTIONS_TOKEN", server.collections_token,
    )

    def _drive() -> dict:
        created = sdk.collections.upsert(
            "leads", {"company": "Acme Corp"}, row_id="acme",
        )
        row = sdk.collections.get("leads", "acme")
        page = sdk.collections.query("leads", search="acme")
        count = sdk.collections.count("leads")
        deleted = sdk.collections.delete("leads", "acme")
        return {
            "created": created, "row": row, "page": page,
            "count": count, "deleted": deleted,
        }

    # urllib is blocking — run off the proxy's event loop.
    out = await asyncio.to_thread(_drive)
    assert out["created"]["created"] is True
    assert out["row"]["data"]["company"] == "Acme Corp"
    assert out["page"]["total"] == 1
    assert out["count"] == 1
    assert out["deleted"]["deleted"] is True


@pytest.mark.asyncio
async def test_sdk_maps_business_errors_to_collections_error(
    proxy, monkeypatch,
):
    server, _ws = proxy
    sdk = _load_sdk()
    monkeypatch.setenv(
        "CUBICLE_TOOL_PROXY_URL", f"http://127.0.0.1:{server.port}",
    )
    monkeypatch.setenv(
        "CUBICLE_COLLECTIONS_TOKEN", server.collections_token,
    )

    def _missing_collection():
        sdk.collections.count("nope")

    with pytest.raises(sdk.CollectionsError) as exc_info:
        await asyncio.to_thread(_missing_collection)
    assert exc_info.value.status == 404
    assert "unknown collection" in exc_info.value.message

    monkeypatch.setenv("CUBICLE_COLLECTIONS_TOKEN", "wrong-token")

    def _bad_token():
        sdk.collections.count("leads")

    with pytest.raises(sdk.CollectionsError) as exc_info:
        await asyncio.to_thread(_bad_token)
    assert exc_info.value.status == 401


def test_sdk_missing_env_raises_teaching_error(monkeypatch):
    sdk = _load_sdk()
    monkeypatch.delenv("CUBICLE_TOOL_PROXY_URL", raising=False)
    monkeypatch.delenv("CUBICLE_COLLECTIONS_TOKEN", raising=False)
    with pytest.raises(sdk.CollectionsError) as exc_info:
        sdk.collections.query("leads")
    assert exc_info.value.status == 0
    assert "needs a newer cbcl" in exc_info.value.message
    assert "Restart cbcl" in exc_info.value.message


def test_sdk_carries_version_sentinel():
    sdk = _load_sdk()
    assert isinstance(sdk.__CUBICLE_SDK_VERSION__, int)
    # >= 3: the v3 transport hardening (proxy-free opener, OSError
    # mapping, 401 teaching copy) must ship through the D4.5 backfill
    # — without the bump already-bootstrapped scripts never get it.
    assert sdk.__CUBICLE_SDK_VERSION__ >= 3


# ─── D4.5: the script_sync SDK backfill ────────────────────────────────


def _template_text() -> str:
    return (
        Path(__file__).resolve().parent.parent
        / "src" / "scripts" / "templates" / "cubicle_helper.py"
    ).read_text()


def _synced_script(tmp_path, name: str = "myscript") -> Path:
    """Lay down a minimal user project the sync will walk."""
    script_dir = tmp_path / ".scripts" / name
    (script_dir / "lib").mkdir(parents=True)
    (script_dir / "main.py").write_text("USER_CODE = True\n")
    (script_dir / "script.yaml").write_text("description: user's\n")
    return script_dir


def _sync(tmp_path, name: str = "myscript") -> None:
    from src.config_sync.script_sync import ScriptSyncer

    syncer = ScriptSyncer(str(tmp_path), office_id="office-1")
    syncer._sync_scripts_blocking([{"name": name}])


def test_backfill_writes_sdk_when_missing(tmp_path):
    script_dir = _synced_script(tmp_path)
    _sync(tmp_path)
    sdk_file = script_dir / "lib" / "cubicle" / "__init__.py"
    assert sdk_file.read_text() == _template_text()


def test_backfill_rewrites_stale_sentinel(tmp_path):
    script_dir = _synced_script(tmp_path)
    sdk_file = script_dir / "lib" / "cubicle" / "__init__.py"
    sdk_file.parent.mkdir(parents=True)
    sdk_file.write_text("__CUBICLE_SDK_VERSION__ = 1\nOLD = True\n")
    _sync(tmp_path)
    assert sdk_file.read_text() == _template_text()


def test_backfill_rewrites_sentinel_less_legacy_sdk(tmp_path):
    """Pre-sentinel SDKs (every script bootstrapped before Item 4)
    read as version 0 and are upgraded."""
    script_dir = _synced_script(tmp_path)
    sdk_file = script_dir / "lib" / "cubicle" / "__init__.py"
    sdk_file.parent.mkdir(parents=True)
    sdk_file.write_text("def notify_manager(message):\n    pass\n")
    _sync(tmp_path)
    assert sdk_file.read_text() == _template_text()


def test_backfill_leaves_current_sentinel_alone(tmp_path):
    """A copy carrying the CURRENT sentinel is not rewritten — the
    sync stays write-free in the steady state."""
    import re

    match = re.search(
        r"^__CUBICLE_SDK_VERSION__\s*=\s*(\d+)",
        _template_text(),
        re.MULTILINE,
    )
    assert match is not None
    current = int(match.group(1))
    script_dir = _synced_script(tmp_path)
    sdk_file = script_dir / "lib" / "cubicle" / "__init__.py"
    sdk_file.parent.mkdir(parents=True)
    marker = f"__CUBICLE_SDK_VERSION__ = {current}\nMARKER = True\n"
    sdk_file.write_text(marker)
    _sync(tmp_path)
    assert sdk_file.read_text() == marker


def test_backfill_never_downgrades_newer_sdk(tmp_path):
    script_dir = _synced_script(tmp_path)
    sdk_file = script_dir / "lib" / "cubicle" / "__init__.py"
    sdk_file.parent.mkdir(parents=True)
    newer = "__CUBICLE_SDK_VERSION__ = 999\nFROM_THE_FUTURE = True\n"
    sdk_file.write_text(newer)
    _sync(tmp_path)
    assert sdk_file.read_text() == newer


def test_backfill_never_touches_user_files(tmp_path):
    script_dir = _synced_script(tmp_path)
    (script_dir / "lib" / "helpers.py").write_text("MINE = 1\n")
    _sync(tmp_path)
    assert (script_dir / "main.py").read_text() == "USER_CODE = True\n"
    assert (
        script_dir / "script.yaml"
    ).read_text() == "description: user's\n"
    assert (
        script_dir / "lib" / "helpers.py"
    ).read_text() == "MINE = 1\n"


# ─── D4.1: body reading — full accumulation + the in-read cap ──────────


@pytest.mark.asyncio
async def test_rpc_reads_large_body_fully(proxy):
    """A large (but under-cap) body must round-trip — the handler
    accumulates via readany() to EOF. A single StreamReader.read(n)
    returns only what is ALREADY buffered (~64KB under flow control),
    so before the accumulation loop a ~1MB legal upsert 400'd on the
    truncated JSON prefix."""
    server, _ws = proxy
    big_text = "x" * (1024 * 1024)  # 1 MB — no datastore size cap
    status, body = await _rpc(server, "data_row_upsert", {
        "collection": "leads",
        "row_id": "big",
        "data": {"company": big_text},
    })
    assert status == 200
    assert body["created"] is True

    status, body = await _rpc(server, "data_row_get", {
        "collection": "leads", "row_id": "big",
    })
    assert status == 200
    assert body["row"]["data"]["company"] == big_text


@pytest.mark.asyncio
async def test_rpc_oversized_chunked_body_413(proxy):
    """A CHUNKED body carries no Content-Length, so the header check
    can't fire — the in-read cap must return 413 mid-stream."""
    server, _ws = proxy

    async def _gen():
        chunk = b'{"pad": "' + b"x" * (1024 * 1024)
        for _ in range(5):  # 5 MB > the 4 MB cap
            yield chunk

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{server.port}/collections/rpc",
            data=_gen(),
            headers={
                "Authorization": f"Bearer {server.collections_token}",
                "Content-Type": "application/json",
            },
        ) as resp:
            assert resp.status == 413


# ─── D4.6: the debounce's finally-re-arm + stop() ──────────────────────


@pytest.mark.asyncio
async def test_rows_changed_write_during_inflight_send_gets_followup_flush(
    proxy,
):
    """A mutation landing while the flush's ``send`` is in flight
    stashes a fresh count but finds the task "not done" and arms no
    new one — ONLY the finally-block re-arm rescues it. Drive a write
    into that window and assert a SECOND frame carries the newer
    count; without the re-arm the freshest row_count is silently
    stranded until the next write (masked by the ~30s heartbeat)."""
    server, ws_client = proxy
    send_started = asyncio.Event()
    send_release = asyncio.Event()

    async def _blocking_send(frame: dict) -> None:
        if frame.get("type") == "collection_rows_changed":
            send_started.set()
            await send_release.wait()

    ws_client.send = AsyncMock(side_effect=_blocking_send)

    def _frames() -> list[dict]:
        return [
            call.args[0] for call in ws_client.send.await_args_list
            if call.args[0].get("type") == "collection_rows_changed"
        ]

    status, _body = await _rpc(server, "data_row_upsert", {
        "collection": "leads", "row_id": "a", "data": {"company": "A"},
    })
    assert status == 200
    # Wait until flush #1 is INSIDE ws_client.send (past its pending
    # pop), so the next write deterministically lands in the
    # in-flight-send window.
    await asyncio.wait_for(send_started.wait(), timeout=5)
    status, _body = await _rpc(server, "data_row_upsert", {
        "collection": "leads", "row_id": "b", "data": {"company": "B"},
    })
    assert status == 200
    send_release.set()

    for _ in range(100):
        if len(_frames()) >= 2:
            break
        await asyncio.sleep(0.05)
    frames = _frames()
    assert len(frames) == 2
    assert frames[0]["row_count"] == 1
    assert frames[1]["row_count"] == 2  # the freshest count, re-armed


@pytest.mark.asyncio
async def test_stop_mid_debounce_emits_no_late_frame(proxy):
    """stop() cancels armed flush tasks AND clears the pending map
    BEFORE the cancellation is processed, so the cancelled task's
    finally-re-arm finds nothing — no frame lands after stop()."""
    server, ws_client = proxy
    status, _body = await _rpc(server, "data_row_upsert", {
        "collection": "leads", "row_id": "a", "data": {"company": "A"},
    })
    assert status == 200
    await server.stop()  # before the debounce window elapses
    await asyncio.sleep(0.5)  # a full window + slack
    assert not [
        call for call in ws_client.send.await_args_list
        if call.args[0].get("type") == "collection_rows_changed"
    ]


# ─── D4.4: SDK transport hardening (SDK v3) ────────────────────────────


@pytest.mark.asyncio
async def test_sdk_maps_response_phase_transport_failure(monkeypatch):
    """urllib wraps only the request-SEND phase in URLError; a proxy
    death while awaiting/reading the response surfaces as a raw
    OSError (RemoteDisconnected → ConnectionResetError). The SDK must
    map it to CollectionsError — the class docstring promises
    'transport problems alike'."""
    sdk = _load_sdk()

    async def _drop_without_response(reader, writer):
        await reader.read(1024)  # accept + read part of the request
        writer.close()

    drop_server = await asyncio.start_server(
        _drop_without_response, "127.0.0.1", 0,
    )
    port = drop_server.sockets[0].getsockname()[1]
    monkeypatch.setenv("CUBICLE_TOOL_PROXY_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("CUBICLE_COLLECTIONS_TOKEN", "tok")
    try:

        def _call():
            sdk.collections.count("leads")

        with pytest.raises(sdk.CollectionsError) as exc_info:
            await asyncio.to_thread(_call)
        assert exc_info.value.status == 0
        assert "dropped the connection" in exc_info.value.message
    finally:
        drop_server.close()
        await drop_server.wait_closed()


@pytest.mark.asyncio
async def test_sdk_ignores_proxy_env_vars(proxy, monkeypatch):
    """HTTP_PROXY is a legal manifest variable a script may declare
    for its OWN outbound calls — it must not reroute the SDK's
    host-local call (or leak the bearer) through a third-party proxy.
    With the default urllib opener this round trip would fail against
    the dead proxy address."""
    server, _ws = proxy
    sdk = _load_sdk()
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv(
        "CUBICLE_TOOL_PROXY_URL", f"http://127.0.0.1:{server.port}",
    )
    monkeypatch.setenv(
        "CUBICLE_COLLECTIONS_TOKEN", server.collections_token,
    )

    def _count() -> int:
        return sdk.collections.count("leads")

    assert await asyncio.to_thread(_count) == 0


@pytest.mark.asyncio
async def test_sdk_401_names_the_daemon_restart_rotation(
    proxy, monkeypatch,
):
    """The collections token is re-minted per daemon run, so a script
    that outlived a cbcl restart holds a stale one — the 401 must
    TEACH that instead of a bare 'unauthorized'."""
    server, _ws = proxy
    sdk = _load_sdk()
    monkeypatch.setenv(
        "CUBICLE_TOOL_PROXY_URL", f"http://127.0.0.1:{server.port}",
    )
    monkeypatch.setenv("CUBICLE_COLLECTIONS_TOKEN", "stale-token")

    def _call():
        sdk.collections.count("leads")

    with pytest.raises(sdk.CollectionsError) as exc_info:
        await asyncio.to_thread(_call)
    assert exc_info.value.status == 401
    assert "re-minted on every cbcl restart" in exc_info.value.message
    assert "Re-run this script" in exc_info.value.message


# ─── D4.5: backfill atomicity ──────────────────────────────────────────


def test_backfill_crash_mid_write_never_strands_truncated_sentinel(
    tmp_path, monkeypatch,
):
    """A failed/interrupted backfill write must leave old-or-new,
    NEVER a truncated file that still carries a CURRENT sentinel —
    the >= guard would then treat the broken copy as current forever
    (import cubicle → SyntaxError/AttributeError in every run, with
    no automatic repair path). The write is tmp + os.replace, so the
    crash shape below leaves the visible file untouched and the next
    sync repairs."""
    script_dir = _synced_script(tmp_path)
    sdk_file = script_dir / "lib" / "cubicle" / "__init__.py"
    sdk_file.parent.mkdir(parents=True)
    stale = "__CUBICLE_SDK_VERSION__ = 1\nOLD = True\n"
    sdk_file.write_text(stale)

    template = _template_text()
    # The interrupted write persists a PREFIX that already contains
    # the current sentinel line (the sentinel sits ~1.5KB into the
    # file, inside the first flush chunk), then dies — the
    # SIGKILL/ENOSPC shape.
    cut = template.index("\n", template.index("__CUBICLE_SDK_VERSION__"))
    truncated = template[: cut + 1]

    crash = {"armed": True}
    original_write_text = Path.write_text

    def _crashing_write_text(self, data, *args, **kwargs):
        if (
            crash["armed"]
            and self.name.endswith(".tmp")
            and "cubicle" in str(self)
        ):
            original_write_text(self, truncated)
            raise OSError(28, "No space left on device")
        return original_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _crashing_write_text)
    _sync(tmp_path)
    # The visible SDK file is bytewise the OLD copy — the failed write
    # never surfaced, so the sentinel guard still sees the stale
    # version and the self-heal stays armed.
    assert sdk_file.read_text() == stale

    crash["armed"] = False
    _sync(tmp_path)
    assert sdk_file.read_text() == _template_text()
