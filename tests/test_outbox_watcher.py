"""Tests for the script → Manager outbox watcher.

Covers the core contract:
  - Only valid notify payloads reach the Manager.
  - The atomic-claim rename prevents double processing.
  - Malformed payloads land in a rejected/ archive so scriptmakers
    can debug without losing the original.
  - Path-traversal attacks via ``attachments`` are dropped before
    the payload reaches the Manager.
  - Workstream name / UUID / ``general_chat`` all resolve to the
    correct backend context_key.
  - The stdlib-only ``cubicle_helper`` round-trips through the
    schema cleanly (locks the public API for Phase 6 bootstrap).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.scripts.outbox_watcher import (  # noqa: E402
    OutboxNotifyPayload,
    _attachment_is_safe,
    _resolve_context_key,
    prune_processed,
    scan_and_dispatch,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeConfigStore:
    """Minimal stand-in for ``ConfigStore.get_workstream_list``."""

    def __init__(self, workstreams: list[dict]):
        self._workstreams = workstreams

    def get_workstream_list(self) -> list[dict]:
        return self._workstreams


@pytest.fixture
def script_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".scripts" / "my-script"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def outbox(script_dir: Path) -> Path:
    o = script_dir / ".outbox"
    o.mkdir()
    return o


def _drop(outbox: Path, name: str, payload: dict | str) -> Path:
    """Atomic drop for tests — same contract as the `cubicle`
    helper but synchronous."""
    path = outbox / name
    if isinstance(payload, dict):
        path.write_text(json.dumps(payload))
    else:
        path.write_text(payload)
    return path


# ---------------------------------------------------------------------------
# Happy path: valid drop reaches the Manager
# ---------------------------------------------------------------------------


class TestHappyPath:

    @pytest.mark.asyncio
    async def test_valid_drop_routes_to_manager(self, tmp_path, outbox):
        config = _FakeConfigStore([{
            "id": "ws-uuid-1", "name": "Recruitment",
            "description": "", "priority": "high",
        }])
        manager = AsyncMock()

        _drop(outbox, "notify-1.json", {
            "v": 1,
            "action": "notify_manager",
            "workstream": "Recruitment",
            "message": "Sourced 87 profiles, please review.",
            "execution_id": "exec-abc",
        })

        dispatched = await scan_and_dispatch(
            script_dir=outbox.parent,
            script_name="my-script",
            office_id="office-42",
            config_store=config,
            manager=manager,
            workspace_root=tmp_path,
        )

        assert dispatched == 1
        manager.ingest_script_message.assert_awaited_once()
        kwargs = manager.ingest_script_message.await_args.kwargs
        assert kwargs["context_key"] == "workstream:ws-uuid-1"
        assert kwargs["script_name"] == "my-script"
        assert kwargs["content"] == "Sourced 87 profiles, please review."
        assert kwargs["execution_id"] == "exec-abc"
        # Original dropped file is gone; landed in processed/.
        assert not (outbox / "notify-1.json").exists()
        processed_root = outbox / ".processed"
        archived = list(processed_root.rglob("notify-1.json"))
        assert len(archived) == 1

    @pytest.mark.asyncio
    async def test_general_chat_resolves_to_literal_context(
        self, tmp_path, outbox
    ):
        config = _FakeConfigStore([])
        manager = AsyncMock()
        _drop(outbox, "notify-gc.json", {
            "v": 1, "action": "notify_manager",
            "workstream": "general_chat",
            "message": "done",
        })
        await scan_and_dispatch(
            script_dir=outbox.parent,
            script_name="s",
            office_id="o",
            config_store=config,
            manager=manager,
            workspace_root=tmp_path,
        )
        kwargs = manager.ingest_script_message.await_args.kwargs
        assert kwargs["context_key"] == "general_chat"

    @pytest.mark.asyncio
    async def test_uuid_resolves(self, tmp_path, outbox):
        config = _FakeConfigStore([{
            "id": "ws-abc-123", "name": "Any Name",
        }])
        manager = AsyncMock()
        _drop(outbox, "notify-uuid.json", {
            "v": 1, "action": "notify_manager",
            "workstream": "ws-abc-123",  # UUID, not name
            "message": "done",
        })
        await scan_and_dispatch(
            script_dir=outbox.parent, script_name="s", office_id="o",
            config_store=config, manager=manager,
            workspace_root=tmp_path,
        )
        kwargs = manager.ingest_script_message.await_args.kwargs
        assert kwargs["context_key"] == "workstream:ws-abc-123"

    @pytest.mark.asyncio
    async def test_name_match_is_case_insensitive(self, tmp_path, outbox):
        config = _FakeConfigStore([
            {"id": "ws-1", "name": "Recruitment"},
        ])
        manager = AsyncMock()
        _drop(outbox, "notify-case.json", {
            "v": 1, "action": "notify_manager",
            "workstream": "RECRUITMENT",
            "message": "done",
        })
        await scan_and_dispatch(
            script_dir=outbox.parent, script_name="s", office_id="o",
            config_store=config, manager=manager,
            workspace_root=tmp_path,
        )
        kwargs = manager.ingest_script_message.await_args.kwargs
        assert kwargs["context_key"] == "workstream:ws-1"


# ---------------------------------------------------------------------------
# Rejection paths
# ---------------------------------------------------------------------------


class TestRejections:

    @pytest.mark.asyncio
    async def test_malformed_json_archived_not_lost(self, tmp_path, outbox):
        manager = AsyncMock()
        _drop(outbox, "notify-bad.json", "this is not json{")

        dispatched = await scan_and_dispatch(
            script_dir=outbox.parent, script_name="s", office_id="o",
            config_store=_FakeConfigStore([]), manager=manager,
            workspace_root=tmp_path,
        )
        assert dispatched == 0
        manager.ingest_script_message.assert_not_called()
        # Original gone, rejected archive has a copy so the
        # scriptmaker can see what failed.
        assert not (outbox / "notify-bad.json").exists()
        rejected = list(outbox.rglob("*notify-bad.json"))
        assert len(rejected) == 1
        assert "rejected" in str(rejected[0])

    @pytest.mark.asyncio
    async def test_schema_violation_archived(self, tmp_path, outbox):
        manager = AsyncMock()
        # Missing required `workstream` + oversize message.
        _drop(outbox, "notify-schema.json", {
            "v": 1, "action": "notify_manager",
            "message": "x",
        })
        await scan_and_dispatch(
            script_dir=outbox.parent, script_name="s", office_id="o",
            config_store=_FakeConfigStore([]), manager=manager,
            workspace_root=tmp_path,
        )
        manager.ingest_script_message.assert_not_called()
        rejected = list(
            (outbox / ".processed").rglob("schema.*.json"),
        )
        assert len(rejected) == 1

    @pytest.mark.asyncio
    async def test_oversized_payload_rejected_before_parse(
        self, tmp_path, outbox
    ):
        # Payload > 32KB → reject without parsing. The size check
        # prevents a runaway file from hogging memory.
        manager = AsyncMock()
        giant = "x" * 40_000
        _drop(outbox, "notify-big.json", {
            "v": 1, "action": "notify_manager",
            "workstream": "x", "message": giant,
        })
        await scan_and_dispatch(
            script_dir=outbox.parent, script_name="s", office_id="o",
            config_store=_FakeConfigStore([]), manager=manager,
            workspace_root=tmp_path,
        )
        manager.ingest_script_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_workstream_archived(self, tmp_path, outbox):
        manager = AsyncMock()
        _drop(outbox, "notify-unknown.json", {
            "v": 1, "action": "notify_manager",
            "workstream": "Does-Not-Exist",
            "message": "done",
        })
        await scan_and_dispatch(
            script_dir=outbox.parent, script_name="s", office_id="o",
            config_store=_FakeConfigStore([
                {"id": "ws-1", "name": "Something Else"},
            ]),
            manager=manager,
            workspace_root=tmp_path,
        )
        manager.ingest_script_message.assert_not_called()
        rejected = list(
            (outbox / ".processed").rglob("unknown-workstream.*.json"),
        )
        assert len(rejected) == 1


# ---------------------------------------------------------------------------
# Attachments — path traversal defense
# ---------------------------------------------------------------------------


class TestAttachments:

    def test_safe_relative_paths_accepted(self, tmp_path):
        assert _attachment_is_safe("outputs/result.json", tmp_path)
        assert _attachment_is_safe("a/b/c.txt", tmp_path)

    def test_absolute_paths_rejected(self, tmp_path):
        assert not _attachment_is_safe("/etc/passwd", tmp_path)
        assert not _attachment_is_safe("/tmp/x", tmp_path)

    def test_traversal_rejected(self, tmp_path):
        assert not _attachment_is_safe("../escape.txt", tmp_path)
        assert not _attachment_is_safe("a/../../escape", tmp_path)

    def test_empty_string_rejected(self, tmp_path):
        assert not _attachment_is_safe("", tmp_path)

    @pytest.mark.asyncio
    async def test_unsafe_attachments_dropped_but_message_still_sent(
        self, tmp_path, outbox
    ):
        config = _FakeConfigStore([
            {"id": "ws-1", "name": "Recruitment"},
        ])
        manager = AsyncMock()
        _drop(outbox, "notify-att.json", {
            "v": 1, "action": "notify_manager",
            "workstream": "Recruitment",
            "message": "check out the results",
            "attachments": [
                "outputs/good.json",
                "../outside.txt",  # dropped
                "/etc/passwd",     # dropped
            ],
        })
        await scan_and_dispatch(
            script_dir=outbox.parent, script_name="s", office_id="o",
            config_store=config, manager=manager,
            workspace_root=tmp_path,
        )
        kwargs = manager.ingest_script_message.await_args.kwargs
        assert kwargs["attachments"] == ["outputs/good.json"]


# ---------------------------------------------------------------------------
# Resolver edge cases
# ---------------------------------------------------------------------------


class TestResolver:

    def test_empty_workstream_returns_none(self):
        assert _resolve_context_key("", _FakeConfigStore([])) is None
        assert _resolve_context_key("  ", _FakeConfigStore([])) is None

    def test_general_alias(self):
        # The legacy "general" alias maps to general_chat for
        # backwards-compat with scripts that might use the shorter
        # form.
        assert _resolve_context_key("general", _FakeConfigStore([])) == "general_chat"

    def test_uuid_takes_precedence_over_name(self):
        # If a workstream name IS also a UUID (pathological case),
        # the UUID match wins because it's exact and unambiguous.
        store = _FakeConfigStore([
            {"id": "abc-123", "name": "Some Name"},
            {"id": "other", "name": "abc-123"},
        ])
        assert _resolve_context_key("abc-123", store) == "workstream:abc-123"


# ---------------------------------------------------------------------------
# Claim + retry semantics
# ---------------------------------------------------------------------------


class TestClaim:

    @pytest.mark.asyncio
    async def test_already_claimed_files_are_skipped(
        self, tmp_path, outbox
    ):
        # A file with the `.processing` suffix is being handled by
        # a concurrent run — the scan must leave it alone. Without
        # this check, a crash-restart would double-dispatch.
        manager = AsyncMock()
        (outbox / "notify-inflight.json.processing").write_text(
            json.dumps({"v": 1, "action": "x", "workstream": "y", "message": "z"})
        )
        dispatched = await scan_and_dispatch(
            script_dir=outbox.parent, script_name="s", office_id="o",
            config_store=_FakeConfigStore([]), manager=manager,
            workspace_root=tmp_path,
        )
        assert dispatched == 0
        manager.ingest_script_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_json_files_ignored(self, tmp_path, outbox):
        # Only notify-*.json files are scanned. Other files in
        # .outbox/ (e.g. README.md left by a curious operator)
        # must be ignored, not accidentally parsed as JSON.
        manager = AsyncMock()
        (outbox / "README.md").write_text("hi")
        dispatched = await scan_and_dispatch(
            script_dir=outbox.parent, script_name="s", office_id="o",
            config_store=_FakeConfigStore([]), manager=manager,
            workspace_root=tmp_path,
        )
        assert dispatched == 0

    @pytest.mark.asyncio
    async def test_processed_subdir_ignored(self, tmp_path, outbox):
        # The scan must NOT descend into .processed/ and re-dispatch
        # yesterday's archived notifications.
        manager = AsyncMock()
        old_archive = outbox / ".processed" / "2025-01-01"
        old_archive.mkdir(parents=True)
        (old_archive / "notify-yesterday.json").write_text(
            json.dumps({
                "v": 1, "action": "notify_manager",
                "workstream": "general_chat", "message": "old",
            })
        )
        dispatched = await scan_and_dispatch(
            script_dir=outbox.parent, script_name="s", office_id="o",
            config_store=_FakeConfigStore([]), manager=manager,
            workspace_root=tmp_path,
        )
        assert dispatched == 0


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


class TestRetention:

    @pytest.mark.asyncio
    async def test_prune_removes_old_day_dirs(self, tmp_path):
        script_dir = tmp_path / ".scripts" / "s"
        archive = script_dir / ".outbox" / ".processed"
        archive.mkdir(parents=True)
        old = archive / "2020-01-01"
        old.mkdir()
        (old / "notify-ancient.json").write_text("{}")
        # Rewind mtime to last year.
        import time as _time
        old_time = _time.time() - 365 * 86400
        os.utime(old, (old_time, old_time))
        # Recent entry stays.
        recent = archive / "today"
        recent.mkdir()

        removed = await prune_processed(script_dir, retention_days=7)
        assert removed == 1
        assert not old.exists()
        assert recent.exists()


# ---------------------------------------------------------------------------
# Stale-claim reaper — orphaned ``.processing`` files
# ---------------------------------------------------------------------------


class TestStaleClaimReaper:
    """Without the startup reaper, a watcher that crashed mid-ingest
    would leave a ``.processing`` file permanently stuck — the scan's
    ``.processing`` filter would skip it forever and the notification
    would be silently lost.

    Reaping happens ONLY at office startup (Phase 9 change). Mid-loop
    reaping was wrong because a long Manager ingest can legitimately
    hold a claim past the stale threshold — per-tick reap would then
    archive a payload still being ingested. Fixed by moving the
    reaper into the daemon init path.
    """

    def test_old_processing_file_reaped_to_rejected_on_startup(
        self, tmp_path, outbox
    ):
        from src.scripts.outbox_watcher import reap_stale_claims_on_startup

        stuck = outbox / "notify-old.json.processing"
        stuck.write_text(json.dumps({
            "v": 1, "action": "notify_manager",
            "workstream": "general_chat", "message": "hi",
        }))
        # Backdate 1 hour — past the 10-minute stale threshold.
        import time as _time
        old_time = _time.time() - 3600
        os.utime(stuck, (old_time, old_time))

        reaped = reap_stale_claims_on_startup(outbox.parent)

        assert reaped == 1
        # Not in the main outbox anymore — stale reaper moved it.
        assert not stuck.exists()
        # Rejected archive contains it with the stale-claim prefix
        # so an operator listing the dir sees WHY it was moved.
        rejected = list(
            (outbox / ".processed").rglob("stale-claim.*.json"),
        )
        assert len(rejected) == 1

    def test_fresh_processing_file_left_alone_on_startup(
        self, tmp_path, outbox
    ):
        from src.scripts.outbox_watcher import reap_stale_claims_on_startup

        fresh = outbox / "notify-fresh.json.processing"
        fresh.write_text("{}")
        # Mtime is now — a concurrent scan on a sibling cbcl process
        # might legitimately be running. The reaper stays out of its
        # way even at startup.
        reaped = reap_stale_claims_on_startup(outbox.parent)

        assert reaped == 0
        assert fresh.exists()

    @pytest.mark.asyncio
    async def test_scan_does_NOT_reap_during_loop(
        self, tmp_path, outbox
    ):
        """Regression guard: per-tick reap was the original
        implementation and reintroducing it would break long Manager
        ingests. Ensure ``scan_and_dispatch`` leaves stale claims
        alone — only the startup reaper touches them.
        """
        manager = AsyncMock()
        stuck = outbox / "notify-old.json.processing"
        stuck.write_text("{}")
        import time as _time
        old_time = _time.time() - 3600
        os.utime(stuck, (old_time, old_time))

        await scan_and_dispatch(
            script_dir=outbox.parent, script_name="s", office_id="o",
            config_store=_FakeConfigStore([]), manager=manager,
            workspace_root=tmp_path,
        )

        # Stale claim still sits in the outbox — only startup
        # reaper would move it. No Manager ingest either.
        assert stuck.exists()
        manager.ingest_script_message.assert_not_called()


class TestConcurrentScans:
    """Regression guard for C2: two concurrent scans of the same
    script directory must not race on the archive-move path. The
    per-dir ``asyncio.Lock`` in ``_SCAN_LOCKS`` serialises them.
    """

    @pytest.mark.asyncio
    async def test_second_concurrent_scan_skipped(
        self, tmp_path, outbox
    ):
        import asyncio as _asyncio
        from src.scripts.outbox_watcher import _SCAN_LOCKS

        _SCAN_LOCKS.clear()

        ws_id = "ad909b73-66ef-46ac-b983-e1a30fd68d34"
        store = _FakeConfigStore([{"id": ws_id, "name": "WS"}])

        _drop(outbox, "notify-1776000000000-aaa11111.json", {
            "v": 1, "action": "notify_manager",
            "workstream": ws_id, "message": "hi",
        })

        ingest_started = _asyncio.Event()
        ingest_release = _asyncio.Event()

        async def slow_ingest(*args, **kwargs):
            ingest_started.set()
            # Hold the first scan open long enough that a second
            # scan overlaps it.
            await ingest_release.wait()

        manager = AsyncMock()
        manager.ingest_script_message.side_effect = slow_ingest

        scan1 = _asyncio.create_task(scan_and_dispatch(
            script_dir=outbox.parent, script_name="s", office_id="o",
            config_store=store, manager=manager,
            workspace_root=tmp_path,
        ))
        await ingest_started.wait()
        # Now the lock is held by scan1; scan2 should fast-path
        # return 0.
        dispatched_2 = await scan_and_dispatch(
            script_dir=outbox.parent, script_name="s", office_id="o",
            config_store=store, manager=manager,
            workspace_root=tmp_path,
        )
        assert dispatched_2 == 0
        # Release scan1 and wait for it.
        ingest_release.set()
        dispatched_1 = await scan1
        assert dispatched_1 == 1

        # Final: manager called exactly once total — scan2 saw the
        # lock held and skipped, scan1 delivered the payload.
        assert manager.ingest_script_message.await_count == 1


class TestArchiveCollision:
    """Regression guard for H1: if a stale-claim reap archives a
    file with the same filename a fresh drop later gets archived
    with on the same day, ``shutil.move`` must NOT silently
    overwrite the first archive. The ``_pick_unique_dest`` helper
    suffixes with ``-1``, ``-2``, … when the dest already exists.
    """

    def test_colliding_archive_names_get_suffixed(
        self, tmp_path, outbox
    ):
        from src.scripts.outbox_watcher import _pick_unique_dest
        day_dir = outbox / ".processed" / "2026-04-21"
        day_dir.mkdir(parents=True)
        first = day_dir / "notify-42.json"
        first.write_text("one")
        # Next call with the same name should return a non-colliding
        # candidate, not first.
        picked = _pick_unique_dest(day_dir, "notify-42.json")
        assert picked != first
        assert not picked.exists()
        assert picked.name.startswith("notify-42-1")


# ---------------------------------------------------------------------------
# Ingest-error archive path + symlink attachment safety
# ---------------------------------------------------------------------------


class TestIngestError:

    @pytest.mark.asyncio
    async def test_manager_raise_archives_as_ingest_error(
        self, tmp_path, outbox
    ):
        # Regression for audit L5 — when the Manager's ingest
        # raises, we must NOT retry on the next tick (double-fire)
        # and must NOT leave the file stuck. Archive as
        # ``ingest-error`` so the scriptmaker can see the
        # failure, then move on.
        config = _FakeConfigStore([{"id": "ws-1", "name": "R"}])
        manager = AsyncMock()
        manager.ingest_script_message.side_effect = RuntimeError(
            "simulated manager outage"
        )

        _drop(outbox, "notify-boom.json", {
            "v": 1, "action": "notify_manager",
            "workstream": "R", "message": "hi",
        })

        dispatched = await scan_and_dispatch(
            script_dir=outbox.parent, script_name="s", office_id="o",
            config_store=config, manager=manager,
            workspace_root=tmp_path,
        )
        assert dispatched == 0
        # Not stuck in outbox.
        assert not (outbox / "notify-boom.json").exists()
        # Archived with the ingest-error reason.
        rejected = list(
            (outbox / ".processed").rglob("ingest-error.*.json"),
        )
        assert len(rejected) == 1


class TestSymlinkAttachment:

    def test_symlink_pointing_outside_workspace_rejected(self, tmp_path):
        # resolve() walks the symlink — if the target is outside
        # workspace, relative_to() raises and the attachment is
        # rejected. Locks the audit L6 concern.
        from src.scripts.outbox_watcher import _attachment_is_safe
        outside = tmp_path.parent / "outside"
        outside.mkdir(exist_ok=True)
        link = tmp_path / "escape"
        link.symlink_to(outside)
        assert _attachment_is_safe("escape", tmp_path) is False

    def test_symlink_pointing_inside_workspace_accepted(self, tmp_path):
        # A symlink that stays inside the workspace is fine.
        from src.scripts.outbox_watcher import _attachment_is_safe
        target = tmp_path / "real.txt"
        target.write_text("x")
        link = tmp_path / "aliased.txt"
        link.symlink_to(target)
        assert _attachment_is_safe("aliased.txt", tmp_path) is True


# ---------------------------------------------------------------------------
# Schema — version gate + byte budget + emitted_at sanity
# ---------------------------------------------------------------------------


class TestSchemaGates:
    """Audit follow-ups M1 / M3 / M4 — lock the narrowed schema."""

    def test_v2_payload_rejected(self):
        # Future payload shape must not silently run through v1
        # code. Lock via Literal[1].
        with pytest.raises(Exception):  # ValidationError
            OutboxNotifyPayload.model_validate({
                "v": 2, "action": "notify_manager",
                "workstream": "x", "message": "y",
            })

    def test_non_notify_action_rejected(self):
        with pytest.raises(Exception):
            OutboxNotifyPayload.model_validate({
                "v": 1, "action": "execute",
                "workstream": "x", "message": "y",
            })

    def test_message_utf8_byte_budget_enforced(self):
        # 8 K characters of a 3-byte char = 24 K bytes, exceeds
        # the 32 K whole-payload cap's own sibling: the 32 K
        # message-byte budget. Actually 8000 * 3 = 24000 < 32768
        # — OK. Push higher to trigger:
        # Each "日" is 3 bytes UTF-8. ~12000 chars exceeds 32K.
        big = "日" * 12000
        with pytest.raises(Exception):
            OutboxNotifyPayload.model_validate({
                "v": 1, "action": "notify_manager",
                "workstream": "x", "message": big,
            })

    def test_emitted_at_nan_rejected(self):
        with pytest.raises(Exception):
            OutboxNotifyPayload.model_validate({
                "v": 1, "action": "notify_manager",
                "workstream": "x", "message": "y",
                "emitted_at": float("nan"),
            })

    def test_emitted_at_negative_rejected(self):
        with pytest.raises(Exception):
            OutboxNotifyPayload.model_validate({
                "v": 1, "action": "notify_manager",
                "workstream": "x", "message": "y",
                "emitted_at": -1.0,
            })

    def test_attachment_length_capped(self):
        # Cap prevents a single-attachment payload from bloating
        # a log line. 800 chars > _MAX_ATTACHMENT_PATH_CHARS (512).
        with pytest.raises(Exception):
            OutboxNotifyPayload.model_validate({
                "v": 1, "action": "notify_manager",
                "workstream": "x", "message": "y",
                "attachments": ["a" * 800],
            })


# ---------------------------------------------------------------------------
# cubicle_helper round-trip
# ---------------------------------------------------------------------------


def _load_cubicle_helper():
    """Load the helper module without installing it — same trick a
    Phase 6 bootstrap will use to copy this file into ``lib/cubicle/``.
    """
    helper_path = (
        Path(__file__).resolve().parent.parent
        / "src" / "scripts" / "templates" / "cubicle_helper.py"
    )
    spec = importlib.util.spec_from_file_location(
        "cubicle_helper", helper_path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCubicleHelperRoundTrip:
    """Locks the contract between the stdlib-only helper script
    authors use and the Pydantic schema the watcher enforces. A
    drift between the two would break every v2 script silently."""

    def test_drop_shape_passes_schema(self, tmp_path, monkeypatch):
        cubicle = _load_cubicle_helper()
        monkeypatch.setenv("CUBICLE_SCRIPT_DIR", str(tmp_path))
        monkeypatch.setenv("CUBICLE_EXECUTION_ID", "exec-xyz")
        monkeypatch.setenv("CUBICLE_SCRIPT_NAME", "my-script")

        fname = cubicle.notify_manager(
            workstream="Recruitment",
            message="hi",
            attachments=["outputs/a.json"],
        )
        dropped = tmp_path / ".outbox" / fname
        assert dropped.is_file()

        payload = json.loads(dropped.read_text())
        # Validates through the exact same schema the watcher uses —
        # if the helper ever drifts, this test breaks loudly.
        validated = OutboxNotifyPayload.model_validate(payload)
        assert validated.workstream == "Recruitment"
        assert validated.message == "hi"
        assert validated.attachments == ["outputs/a.json"]
        assert validated.execution_id == "exec-xyz"
        assert validated.script_name == "my-script"

    def test_raises_without_cubicle_script_dir(self, monkeypatch):
        cubicle = _load_cubicle_helper()
        monkeypatch.delenv("CUBICLE_SCRIPT_DIR", raising=False)
        with pytest.raises(RuntimeError, match="CUBICLE_SCRIPT_DIR"):
            cubicle.notify_manager("general_chat", "x")
