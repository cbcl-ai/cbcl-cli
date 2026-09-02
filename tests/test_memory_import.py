"""Office-memory v1 (T3.6) — learnings.md parser + on-connect importer.

The caller is unit-tested against a MOCKED httpx transport — the backend
endpoint (``POST /api/offices/{oid}/memories/import-lessons``) may not
exist until integration, and these tests must never touch a live
backend.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.memory_import import (
    LEARNINGS_FILENAME,
    MAX_LESSONS_PER_IMPORT,
    MIGRATED_FILENAME,
    import_workstream_learnings,
    parse_learnings_markdown,
    run_learnings_import,
)


_FIXTURE = """Some preamble a human typed (dropped — no heading yet).

## WR-003.T14 — missing test evidence
- What went wrong: submitted without running the suite
- What would have prevented it: run pytest before update_status

## WR-003.T20 — stale API docs
- What went wrong: used the v1 endpoint
- What would have prevented it: check the vendor changelog first
"""


class TestParser:
    def test_parses_heading_sections(self):
        lessons = parse_learnings_markdown(_FIXTURE)
        assert len(lessons) == 2
        assert lessons[0]["title"] == "WR-003.T14 — missing test evidence"
        assert "run pytest before update_status" in lessons[0]["body"]
        assert lessons[1]["title"] == "WR-003.T20 — stale API docs"
        # Preamble before the first heading is dropped.
        assert "preamble" not in lessons[0]["body"]

    def test_headingless_file_becomes_one_lesson(self):
        lessons = parse_learnings_markdown("just two lines\nof notes")
        assert len(lessons) == 1
        assert lessons[0]["title"] == "Imported learnings"
        assert lessons[0]["body"] == "just two lines\nof notes"

    def test_empty_and_whitespace_yield_nothing(self):
        assert parse_learnings_markdown("") == []
        assert parse_learnings_markdown("   \n\n  ") == []

    def test_empty_bodied_sections_are_skipped(self):
        text = "## title only, no body\n\n## real one\n- body line\n"
        lessons = parse_learnings_markdown(text)
        assert len(lessons) == 1
        assert lessons[0]["title"] == "real one"

    def test_caps_title_and_body_but_never_the_section_count(self):
        # The parser is UNCAPPED (final audit): every section survives —
        # the importer chunks POSTs at MAX_LESSONS_PER_IMPORT instead of
        # truncating the file's tail.
        many = "\n".join(
            f"## lesson {i}\n- body {i}\n" for i in range(150)
        )
        lessons = parse_learnings_markdown(many)
        assert len(lessons) == 150
        assert lessons[-1]["title"] == "lesson 149"
        long_section = "## " + "T" * 400 + "\n" + "B" * 5000 + "\n"
        (lesson,) = parse_learnings_markdown(long_section)
        assert len(lesson["title"]) == 120
        assert len(lesson["body"]) == 2000


def _client_returning(status_code: int, json_body: dict | None = None):
    """An httpx.AsyncClient whose transport answers every POST."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status_code, json=json_body or {})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client, calls


def _write_learnings(tmp_path: Path, slug: str, text: str = _FIXTURE) -> Path:
    ws_dir = tmp_path / "workstreams" / slug
    ws_dir.mkdir(parents=True)
    path = ws_dir / LEARNINGS_FILENAME
    path.write_text(text, encoding="utf-8")
    return path


class TestImportOne:
    @pytest.mark.asyncio
    async def test_renames_only_on_2xx(self, tmp_path):
        path = _write_learnings(tmp_path, "website-redesign")
        client, calls = _client_returning(200, {"imported": 2, "skipped": 0})
        async with client:
            ok = await import_workstream_learnings(
                learnings_path=path,
                workstream_id="ws-1",
                platform_url="http://backend.test",
                office_id="office-1",
                security_token="cbcl_co_test",
                client=client,
            )
        assert ok is True
        assert not path.exists()
        assert (path.parent / MIGRATED_FILENAME).is_file()
        # Contract: one POST, right URL, Company-Token bearer, ≤100 lessons.
        (request,) = calls
        assert request.url.path == (
            "/api/offices/office-1/memories/import-lessons"
        )
        assert request.headers["authorization"] == "Bearer cbcl_co_test"
        import json

        body = json.loads(request.content)
        assert body["workstream_id"] == "ws-1"
        assert len(body["lessons"]) == 2
        assert body["lessons"][0]["title"].startswith("WR-003.T14")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [404, 500, 503])
    async def test_non_2xx_leaves_the_file(self, tmp_path, status):
        path = _write_learnings(tmp_path, "website-redesign")
        client, _ = _client_returning(status)
        async with client:
            ok = await import_workstream_learnings(
                learnings_path=path,
                workstream_id="ws-1",
                platform_url="http://backend.test",
                office_id="office-1",
                security_token="tok",
                client=client,
            )
        assert ok is False
        assert path.is_file()
        assert not (path.parent / MIGRATED_FILENAME).exists()

    @pytest.mark.asyncio
    async def test_transport_error_leaves_the_file(self, tmp_path):
        path = _write_learnings(tmp_path, "website-redesign")

        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("backend down", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
        async with client:
            ok = await import_workstream_learnings(
                learnings_path=path,
                workstream_id="ws-1",
                platform_url="http://backend.test",
                office_id="office-1",
                security_token="tok",
                client=client,
            )
        assert ok is False
        assert path.is_file()

    @pytest.mark.asyncio
    async def test_150_sections_post_two_chunks_then_rename(self, tmp_path):
        """Final audit: a >100-section file must NOT truncate — it POSTs
        in ≤100-lesson chunks (2 for 150) and renames only after both
        returned 2xx."""
        import json

        many = "\n".join(f"## lesson {i}\n- body {i}\n" for i in range(150))
        path = _write_learnings(tmp_path, "website-redesign", text=many)
        client, calls = _client_returning(200, {"imported": 1, "skipped": 0})
        async with client:
            ok = await import_workstream_learnings(
                learnings_path=path,
                workstream_id="ws-1",
                platform_url="http://backend.test",
                office_id="office-1",
                security_token="tok",
                client=client,
            )
        assert ok is True
        assert len(calls) == 2
        first = json.loads(calls[0].content)["lessons"]
        second = json.loads(calls[1].content)["lessons"]
        assert len(first) == MAX_LESSONS_PER_IMPORT
        assert len(second) == 50
        # Order preserved across the chunk boundary — nothing dropped.
        assert first[0]["title"] == "lesson 0"
        assert second[0]["title"] == "lesson 100"
        assert second[-1]["title"] == "lesson 149"
        assert not path.exists()
        assert (path.parent / MIGRATED_FILENAME).is_file()

    @pytest.mark.asyncio
    async def test_mid_chunk_failure_leaves_the_file(self, tmp_path):
        """Final audit: chunk 1 succeeds, chunk 2 fails → NO rename (the
        content-hash slugs make the next connect's re-post of chunk 1
        idempotent, so keeping the whole file loses nothing)."""
        many = "\n".join(f"## lesson {i}\n- body {i}\n" for i in range(150))
        path = _write_learnings(tmp_path, "website-redesign", text=many)
        statuses = iter([200, 500])
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(next(statuses), json={})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with client:
            ok = await import_workstream_learnings(
                learnings_path=path,
                workstream_id="ws-1",
                platform_url="http://backend.test",
                office_id="office-1",
                security_token="tok",
                client=client,
            )
        assert ok is False
        assert len(calls) == 2
        assert path.is_file()
        assert not (path.parent / MIGRATED_FILENAME).exists()

    @pytest.mark.asyncio
    async def test_empty_file_migrates_without_a_post(self, tmp_path):
        path = _write_learnings(tmp_path, "website-redesign", text="  \n")
        client, calls = _client_returning(200)
        async with client:
            ok = await import_workstream_learnings(
                learnings_path=path,
                workstream_id="ws-1",
                platform_url="http://backend.test",
                office_id="office-1",
                security_token="tok",
                client=client,
            )
        assert ok is True
        assert calls == []  # nothing to import → no POST
        assert (path.parent / MIGRATED_FILENAME).is_file()


def _store(workstreams: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(workstreams=workstreams)


class TestRunImport:
    @pytest.mark.asyncio
    async def test_imports_matched_dirs_and_leaves_unmatched(
        self, tmp_path, monkeypatch,
    ):
        _write_learnings(tmp_path, "website-redesign")
        _write_learnings(tmp_path, "unknown-project")
        store = _store([{"id": "ws-1", "name": "Website Redesign"}])

        client, calls = _client_returning(200, {"imported": 2, "skipped": 0})

        # Patch AsyncClient so run_learnings_import uses the mock transport.
        class _ClientFactory:
            def __call__(self, *args, **kwargs):
                return client

        monkeypatch.setattr(
            "src.memory_import.httpx.AsyncClient", _ClientFactory(),
        )
        result = await run_learnings_import(
            workspace_path=tmp_path,
            platform_url="http://backend.test",
            office_id="office-1",
            security_token="tok",
            config_store=store,
            config_wait_seconds=0.0,
        )
        assert result == {"migrated": 1, "left": 1}
        matched = tmp_path / "workstreams" / "website-redesign"
        unmatched = tmp_path / "workstreams" / "unknown-project"
        assert (matched / MIGRATED_FILENAME).is_file()
        assert (unmatched / LEARNINGS_FILENAME).is_file()
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_no_files_is_a_cheap_noop(self, tmp_path):
        (tmp_path / "workstreams").mkdir()
        result = await run_learnings_import(
            workspace_path=tmp_path,
            platform_url="http://backend.test",
            office_id="office-1",
            security_token="tok",
            config_store=_store([]),
            config_wait_seconds=0.0,
        )
        assert result == {"migrated": 0, "left": 0}

    @pytest.mark.asyncio
    async def test_empty_config_store_leaves_files_for_next_connect(
        self, tmp_path,
    ):
        _write_learnings(tmp_path, "website-redesign")
        result = await run_learnings_import(
            workspace_path=tmp_path,
            platform_url="http://backend.test",
            office_id="office-1",
            security_token="tok",
            config_store=_store([]),
            config_wait_seconds=0.0,
        )
        assert result == {"migrated": 0, "left": 1}
        assert (
            tmp_path / "workstreams" / "website-redesign" / LEARNINGS_FILENAME
        ).is_file()

    def test_office_init_spawns_the_import(self):
        # Wiring pin (the reconnect-heal seam): office bring-up must
        # fire-and-forget run_learnings_import beside the history
        # backfill — a heavier integration test would need the whole
        # init_office_process_model object graph, so pin the seam at
        # source level (the eval-family precedent for wiring facts).
        import inspect

        from src import handlers

        src_text = inspect.getsource(handlers.init_office_process_model)
        assert "run_learnings_import(" in src_text
        assert "_spawn_background" in src_text

    @pytest.mark.asyncio
    async def test_archived_workstream_dirs_are_skipped(
        self, tmp_path, caplog,
    ):
        """Final audit: ``workstreams/.archived/`` (the orphan sweep's
        archive destination) is deliberately out of the migration's
        scope — its workstreams no longer exist backend-side. The files
        stay untouched, don't count as ``left``, and the skip is logged
        once."""
        archived = tmp_path / "workstreams" / ".archived" / "old-project"
        archived.mkdir(parents=True)
        (archived / LEARNINGS_FILENAME).write_text(_FIXTURE, encoding="utf-8")
        import logging

        with caplog.at_level(logging.INFO, logger="src.memory_import"):
            result = await run_learnings_import(
                workspace_path=tmp_path,
                platform_url="http://backend.test",
                office_id="office-1",
                security_token="tok",
                config_store=_store([{"id": "ws-1", "name": "Old Project"}]),
                config_wait_seconds=0.0,
            )
        assert result == {"migrated": 0, "left": 0}
        assert (archived / LEARNINGS_FILENAME).is_file()
        assert not (archived / MIGRATED_FILENAME).exists()
        skip_lines = [
            r for r in caplog.records
            if "out of the migration's scope" in r.getMessage()
        ]
        assert len(skip_lines) == 1

    @pytest.mark.asyncio
    async def test_already_migrated_dir_is_not_reimported(self, tmp_path):
        ws_dir = tmp_path / "workstreams" / "website-redesign"
        ws_dir.mkdir(parents=True)
        (ws_dir / MIGRATED_FILENAME).write_text("## old\n- body\n")
        result = await run_learnings_import(
            workspace_path=tmp_path,
            platform_url="http://backend.test",
            office_id="office-1",
            security_token="tok",
            config_store=_store([{"id": "ws-1", "name": "Website Redesign"}]),
            config_wait_seconds=0.0,
        )
        assert result == {"migrated": 0, "left": 0}
