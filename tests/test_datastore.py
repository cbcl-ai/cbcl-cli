"""Tests for the office-local collections datastore (Flow Studio FS-P1).

Covers ``src/datastore.py``: store CRUD, schema validation (incl. the
``ref`` ``{id, display}`` shape and per-row ``params_schema`` values),
CSV import + its caps, the inbound-``ref`` delete warnings, the
health-heartbeat counts, and the ``data_*`` RequestBridge actions
end-to-end through ``dispatch_backend_request`` against a temp dir.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src._handlers._requests import dispatch_backend_request
from src.datastore import (
    IMPORT_MAX_CSV_CHARS,
    IMPORT_MAX_ERRORS,
    IMPORT_MAX_ROWS,
    DatastoreError,
    OfficeDatastore,
    coerce_csv_cell,
    validate_row_data,
)


# ─── fixtures ──────────────────────────────────────────────────────────


def _field(name: str, ftype: str, **kw) -> dict:
    """A CollectionField in the canonical synced shape (all keys)."""
    return {
        "name": name,
        "type": ftype,
        "options": kw.get("options", []),
        "ref_to": kw.get("ref_to", ""),
        "required": kw.get("required", False),
        "help": "",
    }


CLIENTS = {
    "name": "clients",
    "display_name": "Clients",
    "schema": [
        _field("company", "text", required=True),
        _field("headcount", "number"),
        _field("active", "bool"),
        _field("tier", "enum", options=["gold", "silver"]),
        _field("signed_on", "date"),
    ],
    "schema_revision": 1,
}

SERVICES = {
    "name": "services",
    "display_name": "Services Catalog",
    "schema": [
        _field("title", "text", required=True),
        _field("client", "ref", ref_to="clients"),
        _field("parameters", "params_schema"),
    ],
    "schema_revision": 1,
}


@pytest.fixture
def config_store():
    return SimpleNamespace(collections=[CLIENTS, SERVICES])


@pytest.fixture
def store(tmp_path, config_store):
    return OfficeDatastore(tmp_path / "office.sqlite", config_store)


@pytest.fixture
def office():
    o = MagicMock()
    o.id = "11111111-1111-1111-1111-111111111111"
    return o


@pytest.fixture
def router():
    r = MagicMock()
    r.ws_client = MagicMock()
    r.ws_client.send = AsyncMock()
    return r


async def _rpc(router, office, store, action: str, params: dict) -> dict:
    """Round-trip one data_* action through the request dispatcher and
    return the response frame's ``data``."""
    router.ws_client.send.reset_mock()
    await dispatch_backend_request(
        {
            "type": "request",
            "request_id": "req-1",
            "action": action,
            "params": params,
        },
        router=router,
        fs_handler=MagicMock(),
        office=office,
        redis_client=None,
        container_name="",
        datastore=store,
    )
    router.ws_client.send.assert_awaited_once()
    frame = router.ws_client.send.call_args[0][0]
    assert frame["type"] == "response"
    assert frame["request_id"] == "req-1"
    return frame["data"]


# ─── CRUD ──────────────────────────────────────────────────────────────


async def test_upsert_creates_row_with_minted_id(store):
    out = await store.upsert_row("clients", {"company": "Acme"})
    assert out["created"] is True
    assert out["row_count"] == 1
    assert len(out["row"]["id"]) == 32  # uuid4().hex
    assert out["row"]["data"] == {"company": "Acme"}
    assert out["row"]["created_at"] == out["row"]["updated_at"]


async def test_upsert_by_id_updates_and_preserves_created_at(store):
    first = await store.upsert_row(
        "clients",
        {"company": "Acme"},
        row_id="acme",
    )
    assert first["created"] is True
    second = await store.upsert_row(
        "clients",
        {"company": "Acme Corp", "headcount": 40},
        row_id="acme",
    )
    assert second["created"] is False
    assert second["row_count"] == 1
    assert second["row"]["created_at"] == first["row"]["created_at"]
    # Data is replaced whole
    assert second["row"]["data"] == {"company": "Acme Corp", "headcount": 40}


async def test_get_row_roundtrip(store):
    await store.upsert_row("clients", {"company": "Acme"}, row_id="acme")
    out = await store.get_row("clients", "acme")
    assert out["row"]["id"] == "acme"
    assert out["row"]["data"]["company"] == "Acme"


async def test_get_row_missing_is_404(store):
    with pytest.raises(DatastoreError) as exc_info:
        await store.get_row("clients", "nope")
    assert exc_info.value.status == 404


async def test_delete_is_idempotent(store):
    await store.upsert_row("clients", {"company": "Acme"}, row_id="acme")
    first = await store.delete_row("clients", "acme")
    assert first["deleted"] is True
    assert first["row_count"] == 0
    second = await store.delete_row("clients", "acme")
    assert second["deleted"] is False
    assert second["warnings"] == []


async def test_count_rows(store):
    assert (await store.count_rows("clients"))["count"] == 0
    await store.upsert_row("clients", {"company": "A"})
    await store.upsert_row("clients", {"company": "B"})
    assert (await store.count_rows("clients"))["count"] == 2
    # Other collections don't bleed in
    assert (await store.count_rows("services"))["count"] == 0


async def test_unknown_collection_is_404(store):
    for coro in (
        store.list_rows("ghosts"),
        store.get_row("ghosts", "x"),
        store.upsert_row("ghosts", {"a": 1}),
        store.delete_row("ghosts", "x"),
        store.count_rows("ghosts"),
        store.import_csv("ghosts", "a\n1"),
    ):
        with pytest.raises(DatastoreError) as exc_info:
            await coro
        assert exc_info.value.status == 404


# ─── list: pagination, filter, search ──────────────────────────────────


async def test_list_pagination_and_total(store):
    for i in range(5):
        await store.upsert_row(
            "clients",
            {"company": f"C{i}"},
            row_id=f"c{i}",
        )
    out = await store.list_rows("clients", limit=2, offset=2)
    assert out["total"] == 5
    assert out["limit"] == 2
    assert out["offset"] == 2
    assert [r["id"] for r in out["rows"]] == ["c2", "c3"]


async def test_list_limit_is_clamped(store):
    out = await store.list_rows("clients", limit=9999, offset=-3)
    assert out["limit"] == 200
    assert out["offset"] == 0


async def test_list_filter_exact_match_and(store):
    await store.upsert_row(
        "clients",
        {"company": "A", "tier": "gold", "active": True},
    )
    await store.upsert_row(
        "clients",
        {"company": "B", "tier": "gold", "active": False},
    )
    await store.upsert_row(
        "clients",
        {"company": "C", "tier": "silver", "active": True},
    )
    out = await store.list_rows(
        "clients",
        filters={"tier": "gold", "active": True},
    )
    assert out["total"] == 1
    assert out["rows"][0]["data"]["company"] == "A"


async def test_list_filter_ref_matches_on_id(store):
    await store.upsert_row(
        "services",
        {"title": "S1", "client": {"id": "acme", "display": "Acme"}},
    )
    await store.upsert_row(
        "services",
        {"title": "S2", "client": {"id": "other", "display": "Other"}},
    )
    out = await store.list_rows("services", filters={"client": "acme"})
    assert out["total"] == 1
    assert out["rows"][0]["data"]["title"] == "S1"


async def test_list_search_case_insensitive_over_text_fields_only(store):
    await store.upsert_row(
        "clients",
        {"company": "Golden Gate LLC", "tier": "silver"},
    )
    await store.upsert_row(
        "clients",
        {"company": "Acme", "tier": "gold"},
    )
    out = await store.list_rows("clients", search="GOLD")
    # Matches the text field "Golden Gate LLC"; does NOT match the
    # enum value "gold" on Acme (search covers text-typed fields only).
    assert out["total"] == 1
    assert out["rows"][0]["data"]["company"] == "Golden Gate LLC"


async def test_list_rejects_non_object_filter(store):
    with pytest.raises(DatastoreError) as exc_info:
        await store.list_rows("clients", filters=["not", "a", "dict"])
    assert exc_info.value.status == 400


async def test_list_skips_malformed_stored_row(store, caplog):
    await store.upsert_row("clients", {"company": "Good"}, row_id="good")
    # Corrupt a row behind the store's back.
    conn = sqlite3.connect(store._db_path)
    with conn:
        conn.execute(
            "INSERT INTO rows (collection, id, data, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("clients", "bad", "{not json", "2026-01-01", "2026-01-01"),
        )
    conn.close()
    with caplog.at_level("WARNING"):
        out = await store.list_rows("clients")
    assert out["total"] == 1
    assert out["rows"][0]["id"] == "good"
    assert any("malformed" in r.message for r in caplog.records)


# ─── schema validation ─────────────────────────────────────────────────


async def test_upsert_unknown_field_rejected(store):
    with pytest.raises(DatastoreError) as exc_info:
        await store.upsert_row("clients", {"company": "A", "bogus": 1})
    assert exc_info.value.status == 400
    assert "unknown field" in str(exc_info.value)


async def test_upsert_missing_required_rejected(store):
    with pytest.raises(DatastoreError) as exc_info:
        await store.upsert_row("clients", {"headcount": 5})
    assert "missing required field 'company'" in str(exc_info.value)


@pytest.mark.parametrize(
    "data",
    [
        {"company": 42},  # text gets a number
        {"company": "A", "headcount": "ten"},  # number gets a string
        {"company": "A", "headcount": True},  # bool is NOT a number
        {"company": "A", "active": "yes"},  # bool gets a string
        {"company": "A", "tier": "bronze"},  # enum off-options
        {"company": "A", "signed_on": "someday"},  # unparseable date
    ],
)
async def test_upsert_type_mismatches_rejected(store, data):
    with pytest.raises(DatastoreError) as exc_info:
        await store.upsert_row("clients", data)
    assert exc_info.value.status == 400


async def test_upsert_valid_date_and_null_optional(store):
    out = await store.upsert_row(
        "clients",
        {"company": "A", "signed_on": "2026-08-05", "headcount": None},
    )
    assert out["created"] is True


async def test_upsert_bad_row_id_shape_rejected(store):
    with pytest.raises(DatastoreError) as exc_info:
        await store.upsert_row(
            "clients",
            {"company": "A"},
            row_id="not ok!",
        )
    assert exc_info.value.status == 400


async def test_upsert_non_dict_data_rejected(store):
    with pytest.raises(DatastoreError) as exc_info:
        await store.upsert_row("clients", ["not", "a", "dict"])
    assert exc_info.value.status == 400


# ─── ref shape ─────────────────────────────────────────────────────────


async def test_ref_valid_shape_accepted(store):
    out = await store.upsert_row(
        "services",
        {"title": "S", "client": {"id": "acme", "display": "Acme"}},
    )
    assert out["created"] is True
    # display is optional
    out = await store.upsert_row(
        "services",
        {"title": "S2", "client": {"id": "acme"}},
    )
    assert out["created"] is True


@pytest.mark.parametrize(
    "ref_value",
    [
        "acme",  # bare string
        {"display": "Acme"},  # missing id
        {"id": 42},  # non-str id
        {"id": ""},  # empty id
        {"id": "acme", "display": 7},  # non-str display
        {"id": "acme", "display": "A", "x": 1},  # unknown key
    ],
)
async def test_ref_bad_shapes_rejected(store, ref_value):
    with pytest.raises(DatastoreError) as exc_info:
        await store.upsert_row(
            "services",
            {"title": "S", "client": ref_value},
        )
    assert exc_info.value.status == 400


# ─── params_schema rows ────────────────────────────────────────────────


async def test_params_schema_valid_value_accepted(store):
    params = [
        {
            "name": "capacity",
            "type": "number",
            "options": [],
            "default": 10,
            "required": True,
            "help": "GB",
        },
        {
            "name": "region",
            "type": "enum",
            "options": ["eu", "us"],
            "default": "eu",
            "required": False,
            "help": "",
        },
    ]
    out = await store.upsert_row(
        "services",
        {"title": "S", "parameters": params},
    )
    assert out["created"] is True
    got = await store.get_row("services", out["row"]["id"])
    assert got["row"]["data"]["parameters"] == params


@pytest.mark.parametrize(
    "params_value",
    [
        {"name": "x"},  # not a list
        [{"type": "text"}],  # missing name
        [{"name": "x", "type": "ref"}],  # param can't be ref
        [{"name": "x", "type": "enum", "options": []}],  # enum w/o options
        [{"name": "x", "type": "number", "default": "9"}],  # bad default
        [
            {"name": "x", "type": "text"},
            {"name": "x", "type": "text"},
        ],  # duplicate names
    ],
)
async def test_params_schema_bad_values_rejected(store, params_value):
    with pytest.raises(DatastoreError) as exc_info:
        await store.upsert_row(
            "services",
            {"title": "S", "parameters": params_value},
        )
    assert exc_info.value.status == 400


def test_validate_row_data_is_pure():
    errors = validate_row_data(
        CLIENTS["schema"],
        {"company": "A", "tier": "gold"},
    )
    assert errors == []


# ─── CSV import ────────────────────────────────────────────────────────


async def test_import_happy_path_with_coercion(store):
    csv_text = "company,headcount,active\n" "Acme,40,true\n" "Globex,12,no\n"
    out = await store.import_csv("clients", csv_text)
    assert out == {
        "imported": 2,
        "skipped": 0,
        "errors": [],
        "row_count": 2,
    }
    rows = (await store.list_rows("clients"))["rows"]
    by_company = {r["data"]["company"]: r["data"] for r in rows}
    assert by_company["Acme"] == {
        "company": "Acme",
        "headcount": 40,
        "active": True,
    }
    assert by_company["Globex"]["active"] is False


async def test_import_bad_rows_skipped_with_row_numbered_errors(store):
    csv_text = (
        "company,headcount\n"
        "Acme,40\n"
        "Globex,notanumber\n"
        ",5\n"  # empty company cell → missing required
    )
    out = await store.import_csv("clients", csv_text)
    assert out["imported"] == 1
    assert out["skipped"] == 2
    assert len(out["errors"]) == 2
    assert out["errors"][0].startswith("row 3:")
    assert out["errors"][1].startswith("row 4:")
    assert out["row_count"] == 1


async def test_import_unknown_header_field_is_teaching_error(store):
    with pytest.raises(DatastoreError) as exc_info:
        await store.import_csv("clients", "company,bogus\nA,1\n")
    assert exc_info.value.status == 400
    assert "bogus" in str(exc_info.value)
    assert "company" in str(exc_info.value)  # names the valid fields


async def test_import_rejects_oversized_csv(store):
    big = "company\n" + "x" * (IMPORT_MAX_CSV_CHARS + 10)
    with pytest.raises(DatastoreError) as exc_info:
        await store.import_csv("clients", big)
    assert exc_info.value.status == 400
    assert "too large" in str(exc_info.value)


async def test_import_rejects_too_many_rows(store):
    csv_text = "company\n" + "acme\n" * (IMPORT_MAX_ROWS + 1)
    with pytest.raises(DatastoreError) as exc_info:
        await store.import_csv("clients", csv_text)
    assert exc_info.value.status == 400
    assert str(IMPORT_MAX_ROWS) in str(exc_info.value)


async def test_import_errors_capped_but_skips_counted(store):
    bad_rows = "\n".join(f"C{i},notanumber" for i in range(25))
    out = await store.import_csv(
        "clients",
        f"company,headcount\n{bad_rows}\n",
    )
    assert out["imported"] == 0
    assert out["skipped"] == 25
    assert len(out["errors"]) == IMPORT_MAX_ERRORS


async def test_import_ref_and_params_schema_cells(store):
    ref_json = json.dumps({"id": "acme", "display": "Acme"})
    params_json = json.dumps(
        [{"name": "capacity", "type": "number"}],
    )
    csv_text = (
        "title,client,parameters\n"
        f'S1,"{ref_json.replace(chr(34), chr(34) * 2)}",'
        f'"{params_json.replace(chr(34), chr(34) * 2)}"\n'
        "S2,bare-id,\n"
    )
    out = await store.import_csv("services", csv_text)
    assert out["imported"] == 2, out["errors"]
    rows = (await store.list_rows("services"))["rows"]
    by_title = {r["data"]["title"]: r["data"] for r in rows}
    assert by_title["S1"]["client"] == {"id": "acme", "display": "Acme"}
    assert by_title["S1"]["parameters"] == [
        {"name": "capacity", "type": "number"},
    ]
    # Bare cell wraps as {id, display}
    assert by_title["S2"]["client"] == {
        "id": "bare-id",
        "display": "bare-id",
    }


async def test_import_empty_csv_is_noop(store):
    out = await store.import_csv("clients", "")
    assert out == {"imported": 0, "skipped": 0, "errors": [], "row_count": 0}


def test_coerce_csv_cell_number_int_vs_float():
    field = _field("headcount", "number")
    assert coerce_csv_cell(field, "40") == 40
    assert coerce_csv_cell(field, "1.5") == 1.5
    with pytest.raises(ValueError):
        coerce_csv_cell(field, "many")


# ─── inbound-ref delete warnings ───────────────────────────────────────


async def test_delete_warns_on_inbound_refs(store):
    await store.upsert_row("clients", {"company": "Acme"}, row_id="acme")
    await store.upsert_row(
        "services",
        {"title": "S1", "client": {"id": "acme", "display": "Acme"}},
    )
    await store.upsert_row(
        "services",
        {"title": "S2", "client": {"id": "acme", "display": "Acme"}},
    )
    out = await store.delete_row("clients", "acme")
    assert out["deleted"] is True
    assert len(out["warnings"]) == 1
    assert "2 row(s)" in out["warnings"][0]
    assert "'services'" in out["warnings"][0]
    assert "'client'" in out["warnings"][0]


async def test_delete_without_inbound_refs_has_no_warnings(store):
    await store.upsert_row("clients", {"company": "Solo"}, row_id="solo")
    out = await store.delete_row("clients", "solo")
    assert out["deleted"] is True
    assert out["warnings"] == []


# ─── health heartbeat counts ───────────────────────────────────────────


async def test_collection_counts_zero_before_any_write(store):
    # DB file doesn't exist yet — synced names report 0.
    assert await store.collection_counts() == {"clients": 0, "services": 0}


async def test_collection_counts_after_writes(store):
    await store.upsert_row("clients", {"company": "A"})
    await store.upsert_row("clients", {"company": "B"})
    await store.upsert_row("services", {"title": "S"})
    assert await store.collection_counts() == {
        "clients": 2,
        "services": 1,
    }


async def test_health_report_carries_collections_map():
    from src.health.reporter import HealthReporter

    ds = MagicMock()
    ds.collection_counts = AsyncMock(return_value={"clients": 3})
    reporter = HealthReporter(office_id="o1", datastore=ds)
    report = await reporter._build_report()
    assert report["collections"] == {"clients": 3}


async def test_health_report_survives_datastore_failure():
    from src.health.reporter import HealthReporter

    ds = MagicMock()
    ds.collection_counts = AsyncMock(side_effect=RuntimeError("boom"))
    reporter = HealthReporter(office_id="o1", datastore=ds)
    report = await reporter._build_report()
    assert report["collections"] == {}


# ─── RPC actions end-to-end (dispatch_backend_request) ─────────────────


async def test_rpc_upsert_get_list_delete_roundtrip(router, office, store):
    out = await _rpc(
        router,
        office,
        store,
        "data_row_upsert",
        {"collection": "clients", "data": {"company": "Acme"}, "row_id": "acme"},
    )
    assert out["created"] is True
    assert out["row_count"] == 1

    out = await _rpc(
        router,
        office,
        store,
        "data_row_get",
        {"collection": "clients", "row_id": "acme"},
    )
    assert out["row"]["data"]["company"] == "Acme"

    out = await _rpc(
        router,
        office,
        store,
        "data_rows_list",
        {"collection": "clients", "limit": 10, "offset": 0},
    )
    assert out["total"] == 1

    out = await _rpc(
        router,
        office,
        store,
        "data_rows_count",
        {"collection": "clients"},
    )
    assert out["count"] == 1

    out = await _rpc(
        router,
        office,
        store,
        "data_row_delete",
        {"collection": "clients", "row_id": "acme"},
    )
    assert out["deleted"] is True


async def test_rpc_errors_use_error_status_convention(router, office, store):
    out = await _rpc(
        router,
        office,
        store,
        "data_row_get",
        {"collection": "clients", "row_id": "ghost"},
    )
    assert out["status"] == 404
    assert "ghost" in out["error"]

    out = await _rpc(
        router,
        office,
        store,
        "data_row_upsert",
        {"collection": "clients", "data": {"company": 42}},
    )
    assert out["status"] == 400

    out = await _rpc(
        router,
        office,
        store,
        "data_rows_list",
        {"collection": "unknown-coll"},
    )
    assert out["status"] == 404

    out = await _rpc(
        router,
        office,
        store,
        "data_bogus_action",
        {"collection": "clients"},
    )
    assert out["status"] == 400
    assert "data_bogus_action" in out["error"]


async def test_rpc_import(router, office, store):
    out = await _rpc(
        router,
        office,
        store,
        "data_import",
        {"collection": "clients", "csv": "company\nAcme\nGlobex\n"},
    )
    assert out == {
        "imported": 2,
        "skipped": 0,
        "errors": [],
        "row_count": 2,
    }


async def test_rpc_without_datastore_is_503(router, office):
    await dispatch_backend_request(
        {
            "type": "request",
            "request_id": "req-9",
            "action": "data_rows_count",
            "params": {"collection": "clients"},
        },
        router=router,
        fs_handler=MagicMock(),
        office=office,
        redis_client=None,
        container_name="",
        datastore=None,
    )
    frame = router.ws_client.send.call_args[0][0]
    assert frame["data"]["status"] == 503


# ─── schema cache from sync_config ─────────────────────────────────────


async def test_config_store_caches_collections_from_sync(tmp_path):
    from src.config_sync.sync_service import ConfigStore

    config_store = ConfigStore()
    await config_store.update_from_sync(
        {"config": {"office_name": "T", "collections": [CLIENTS]}},
    )
    assert config_store.collections == [CLIENTS]

    ds = OfficeDatastore(tmp_path / "t.sqlite", config_store)
    out = await ds.upsert_row("clients", {"company": "Synced"})
    assert out["created"] is True
    # A later sync REPLACES the cache — removed collections go 404.
    await config_store.update_from_sync(
        {"config": {"office_name": "T", "collections": []}},
    )
    with pytest.raises(DatastoreError) as exc_info:
        await ds.upsert_row("clients", {"company": "Gone"})
    assert exc_info.value.status == 404
