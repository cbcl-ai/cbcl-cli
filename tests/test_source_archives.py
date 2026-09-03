"""Instruction-sources-v2 — host-side ZIP pre-extraction + the
``source_warnings`` surfacing.

Contract under test (``src/source_archives.py`` + the
``setup_generator`` wiring):

- each ``*.zip`` directly under ``source/`` expands into a sibling
  ``<stem>/`` directory (single shared root stripped; idempotent);
- zip-slip entries and nested archives are SKIPPED with warnings;
- per-archive caps (400 entries / 50 MB uncompressed) extract NOTHING
  when breached — a partial extraction would look complete;
- a corrupt zip warns and never raises;
- the scoped settings survey lists the EXTRACTED directory instead of
  the zip (``_swap_extracted_zip_paths``);
- user-actionable ``source_warnings`` ride every generation result:
  the settings 3-tuples and the wizard's final config payload.
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import src.source_archives as sa
import src.setup_generator as sg
from src._setup_prompts import (
    AGENT_DETAIL_PROMPT,
    INSTRUCTIONS_PROMPT,
    ROSTER_PROMPT,
    SOURCE_SURVEY_PROMPT,
    SYNTHESIZE_VISION_PROMPT,
)


def _make_zip(path: Path, entries: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


@pytest.fixture()
def source_dir(tmp_path: Path) -> Path:
    src = tmp_path / "workspace" / "source"
    src.mkdir(parents=True)
    return src


# ---------------------------------------------------------------------------
# expand_source_archives — extraction mechanics
# ---------------------------------------------------------------------------


def test_happy_extraction_multi_root_keeps_layout(source_dir: Path) -> None:
    _make_zip(
        source_dir / "docs.zip",
        {"a.md": "alpha", "sub/b.md": "beta"},
    )
    warnings = sa.expand_source_archives(source_dir)
    assert warnings == []
    assert (source_dir / "docs" / "a.md").read_text() == "alpha"
    assert (source_dir / "docs" / "sub" / "b.md").read_text() == "beta"


def test_single_root_is_flattened(source_dir: Path) -> None:
    """``framework.zip`` whose entries all live under one top-level dir
    extracts to ``source/framework/<files>``, not
    ``source/framework/framework-v3/<files>``."""
    _make_zip(
        source_dir / "framework.zip",
        {
            "framework-v3/": "",
            "framework-v3/playbook.md": "method",
            "framework-v3/sop/steps.md": "steps",
        },
    )
    warnings = sa.expand_source_archives(source_dir)
    assert warnings == []
    assert (source_dir / "framework" / "playbook.md").read_text() == "method"
    assert (source_dir / "framework" / "sop" / "steps.md").exists()
    assert not (source_dir / "framework" / "framework-v3").exists()


def test_single_loose_file_is_not_flattened(source_dir: Path) -> None:
    """A zip holding ONE root-level file must not strip the file's own
    name as a 'shared root'."""
    _make_zip(source_dir / "one.zip", {"only.md": "x"})
    assert sa.expand_source_archives(source_dir) == []
    assert (source_dir / "one" / "only.md").read_text() == "x"


def test_zip_slip_entries_are_skipped_with_warning(source_dir: Path) -> None:
    _make_zip(
        source_dir / "evil.zip",
        {"../escape.txt": "x", "/abs.txt": "y", "ok.md": "fine"},
    )
    warnings = sa.expand_source_archives(source_dir)
    assert (source_dir / "evil" / "ok.md").read_text() == "fine"
    # Nothing escaped the target directory.
    assert not (source_dir / "escape.txt").exists()
    assert not (source_dir.parent / "escape.txt").exists()
    slip = [w for w in warnings if "zip-slip" in w]
    assert slip and "2 unsafe" in slip[0]


def test_nested_archives_are_skipped_and_named(source_dir: Path) -> None:
    _make_zip(
        source_dir / "bundle.zip",
        {"inner.zip": "zzz", "deep/more.7z": "www", "ok.md": "fine"},
    )
    warnings = sa.expand_source_archives(source_dir)
    assert (source_dir / "bundle" / "ok.md").exists()
    assert not (source_dir / "bundle" / "inner.zip").exists()
    nested = [w for w in warnings if "nested archives" in w]
    assert nested
    assert "inner.zip" in nested[0]
    assert "more.7z" in nested[0]


def test_entry_cap_extracts_nothing(source_dir: Path, monkeypatch) -> None:
    assert sa._MAX_ARCHIVE_ENTRIES == 400  # the real product cap
    monkeypatch.setattr(sa, "_MAX_ARCHIVE_ENTRIES", 3)
    _make_zip(
        source_dir / "big.zip",
        {f"f{i}.md": "x" for i in range(4)},
    )
    warnings = sa.expand_source_archives(source_dir)
    assert not (source_dir / "big").exists()
    assert any("-file cap" in w for w in warnings)


def test_directory_only_entries_count_against_total_cap(
    source_dir: Path, monkeypatch
) -> None:
    """A zip of directory-only entries can't mkdir without bound — the
    total-infos cap covers what the file cap alone would miss."""
    monkeypatch.setattr(sa, "_MAX_ARCHIVE_TOTAL_INFOS", 3)
    with zipfile.ZipFile(source_dir / "dirs.zip", "w") as zf:
        for i in range(5):
            zf.writestr(f"d{i}/", "")
    warnings = sa.expand_source_archives(source_dir)
    assert not (source_dir / "dirs").exists()
    assert any("-entry cap" in w for w in warnings)


def test_reuploaded_zip_reextracts_over_stale_dir(source_dir: Path) -> None:
    """A changed zip with the same name must re-extract — the stale
    directory silently staying authoritative was a review finding."""
    zip_path = source_dir / "framework.zip"
    _make_zip(zip_path, {"doc.md": "OLD content"})
    assert sa.expand_source_archives(source_dir) == []
    assert (source_dir / "framework" / "doc.md").read_text() == "OLD content"

    _make_zip(zip_path, {"doc.md": "NEW content"})
    os.utime(zip_path, ns=(1, 1))  # force a distinct fingerprint
    assert sa.expand_source_archives(source_dir) == []
    assert (source_dir / "framework" / "doc.md").read_text() == "NEW content"


def test_markerless_user_directory_is_never_deleted(source_dir: Path) -> None:
    """A non-empty stem-named dir WITHOUT our extraction marker is
    user-managed content — left alone, no warning."""
    (source_dir / "framework").mkdir()
    (source_dir / "framework" / "mine.md").write_text("user file")
    _make_zip(source_dir / "framework.zip", {"doc.md": "zip content"})
    assert sa.expand_source_archives(source_dir) == []
    assert (source_dir / "framework" / "mine.md").read_text() == "user file"
    assert not (source_dir / "framework" / "doc.md").exists()


def test_stream_cap_breach_leaves_no_partial_target(
    source_dir: Path, monkeypatch
) -> None:
    """Declared sizes lie -> mid-stream cap breach discards the tmp dir;
    nothing complete-looking lands at the target."""
    zip_path = source_dir / "liar.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("a.md", "x" * 50)
        zf.writestr("b.md", "y" * 50)
    # Simulate the streamed byte-belt tripping on the second entry (a
    # zip whose declared sizes lied past the metadata pre-check).
    real_copy = sa._copy_capped
    calls = {"n": 0}

    def tripping_copy(zf, info, dest, budget):
        calls["n"] += 1
        if calls["n"] >= 2:
            return False
        return real_copy(zf, info, dest, budget)

    monkeypatch.setattr(sa, "_copy_capped", tripping_copy)
    warnings = sa.expand_source_archives(source_dir)
    assert not (source_dir / "liar").exists()
    assert not (source_dir / "liar.extracting").exists()
    assert any("studied by filename only" in w for w in warnings)


def test_corrupt_entry_midway_discards_tmp(source_dir: Path, monkeypatch) -> None:
    """A BadZipFile raised mid-stream (corrupt CRC on one entry) must
    discard the tmp dir like an IO failure — not leak it."""
    _make_zip(source_dir / "crc.zip", {"a.md": "aaa", "b.md": "bbb"})

    def raising_copy(zf, info, dest, budget):
        raise zipfile.BadZipFile("Bad CRC-32 for file 'a.md'")

    monkeypatch.setattr(sa, "_copy_capped", raising_copy)
    warnings = sa.expand_source_archives(source_dir)
    assert not (source_dir / "crc").exists()
    assert not (source_dir / "crc.extracting").exists()
    assert any("failed partway" in w for w in warnings)


def test_size_cap_extracts_nothing(source_dir: Path, monkeypatch) -> None:
    assert sa._MAX_ARCHIVE_UNCOMPRESSED_BYTES == 50 * 1024 * 1024
    monkeypatch.setattr(sa, "_MAX_ARCHIVE_UNCOMPRESSED_BYTES", 10)
    _make_zip(source_dir / "fat.zip", {"blob.bin": "x" * 100})
    warnings = sa.expand_source_archives(source_dir)
    assert not (source_dir / "fat").exists()
    assert any("cap" in w and "fat.zip" in w for w in warnings)


def test_idempotent_rerun_skips_existing_extraction(source_dir: Path) -> None:
    _make_zip(source_dir / "docs.zip", {"a.md": "alpha", "b.md": "beta"})
    assert sa.expand_source_archives(source_dir) == []
    # Mutate the extracted dir; a re-run must SKIP, not restore.
    (source_dir / "docs" / "a.md").unlink()
    assert sa.expand_source_archives(source_dir) == []
    assert not (source_dir / "docs" / "a.md").exists()
    assert (source_dir / "docs" / "b.md").exists()


def test_corrupt_zip_warns_and_never_raises(source_dir: Path) -> None:
    (source_dir / "broken.zip").write_bytes(b"this is not a zip")
    warnings = sa.expand_source_archives(source_dir)
    assert any("broken.zip" in w and "could not be read" in w for w in warnings)
    assert not (source_dir / "broken").exists()


def test_zips_in_subdirectories_are_not_expanded(source_dir: Path) -> None:
    _make_zip(source_dir / "sub" / "deep.zip", {"a.md": "x"})
    assert sa.expand_source_archives(source_dir) == []
    assert not (source_dir / "sub" / "deep").exists()


def test_target_name_taken_by_a_file_warns(source_dir: Path) -> None:
    _make_zip(source_dir / "docs.zip", {"a.md": "x"})
    (source_dir / "docs").write_text("a plain file in the way")
    warnings = sa.expand_source_archives(source_dir)
    assert any("docs.zip" in w and "not a directory" in w for w in warnings)


def test_missing_source_dir_is_a_silent_noop(tmp_path: Path) -> None:
    assert sa.expand_source_archives(tmp_path / "nope") == []


# ---------------------------------------------------------------------------
# The scoped path swap (settings surveys)
# ---------------------------------------------------------------------------


def test_swap_replaces_extracted_zip_and_keeps_the_rest(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "workspace"
    extracted = ws / "source" / "framework"
    extracted.mkdir(parents=True)
    (extracted / "playbook.md").write_text("method")

    out = sg._swap_extracted_zip_paths(
        ["source/framework.zip", "source/notes.md", "source/other.zip"],
        str(ws),
    )
    # The extracted dir replaces the zip; a non-extracted zip stays so
    # its unreadable warning still fires honestly.
    assert out == ["source/framework/", "source/notes.md", "source/other.zip"]


def test_swap_without_workspace_path_is_identity() -> None:
    paths = ["source/a.zip", "source/b.md"]
    assert sg._swap_extracted_zip_paths(paths, None) == paths


# ---------------------------------------------------------------------------
# source_warnings — every result shape carries the degradations
# ---------------------------------------------------------------------------


def test_cap_source_warnings_bounds_the_wire() -> None:
    raw = ["dup", "dup", "", "   ", 42, "x" * 400] + [  # type: ignore[list-item]
        f"warning {i}" for i in range(15)
    ]
    out = sg._cap_source_warnings(raw)  # type: ignore[arg-type]
    assert len(out) == sg._SOURCE_WARNINGS_MAX == 10
    assert out[0] == "dup"  # deduped
    assert len(out[1]) == sg._SOURCE_WARNING_MAX_CHARS == 300
    assert all(isinstance(w, str) and w for w in out)


async def test_office_instructions_result_carries_source_warnings(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """The settings 3-tuple: extraction warning (corrupt zip) + brief
    truncation + remaining-unreadable names all reach the caller."""
    ws = tmp_path / "workspace"
    (ws / "source").mkdir(parents=True)
    (ws / "source" / "broken.zip").write_bytes(b"not a zip")

    survey_mock = AsyncMock(
        return_value={
            "source_brief": "x" * 7000,
            "inventory": [{"path": "left.xlsx", "role": "quoting model"}],
        }
    )
    monkeypatch.setattr(sg, "_run_source_survey", survey_mock)

    async def fake_run_chunk(container, system_prompt, user_prompt, **kwargs):
        return {"instructions": "# Office\n\n## Mission\nOk."}

    monkeypatch.setattr(sg, "_run_chunk", fake_run_chunk)

    out, changes, warnings = await sg.generate_office_instructions(
        "cbcl-office-test",
        "Quote Shop",
        None,
        "",
        "ground it",
        "regenerate",
        sources=["source/broken.zip", "source/left.xlsx"],
        workspace_path=str(ws),
    )
    assert "## Mission" in out
    assert changes == []
    assert any("broken.zip" in w for w in warnings)
    assert any("truncated" in w for w in warnings)
    assert any("left.xlsx" in w for w in warnings)
    assert len(warnings) <= 10
    assert all(len(w) <= 300 for w in warnings)


async def test_workstream_result_carries_source_warnings(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ws = tmp_path / "workspace"
    (ws / "source").mkdir(parents=True)

    survey_mock = AsyncMock(
        return_value={
            "source_brief": "y" * 7000,
            "inventory": [],
        }
    )
    monkeypatch.setattr(sg, "_run_source_survey", survey_mock)

    async def fake_run_chunk(container, system_prompt, user_prompt, **kwargs):
        return {"context_notes": "### Conventions\nGrounded."}

    monkeypatch.setattr(sg, "_run_chunk", fake_run_chunk)

    text, changes, warnings = await sg.generate_workstream_context_note(
        "cbcl-office-test",
        "Quoting",
        "cite it",
        "Quote Shop",
        sources=["source/notes.md"],
        workspace_path=str(ws),
    )
    assert text.startswith("### Conventions")
    assert any("truncated" in w for w in warnings)


async def test_scoped_survey_lists_extracted_dir_instead_of_zip(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """E2E through the settings path: the zip expands host-side, the
    survey listing carries the extracted DIRECTORY (trailing slash) and
    the zip itself stays out of the list."""
    ws = tmp_path / "workspace"
    _make_zip(
        ws / "source" / "framework.zip",
        {"framework-v3/playbook.md": "method"},
    )
    survey_mock = AsyncMock(
        return_value={
            "source_brief": "b",
            "inventory": [],
        }
    )
    monkeypatch.setattr(sg, "_run_source_survey", survey_mock)

    async def fake_run_chunk(container, system_prompt, user_prompt, **kwargs):
        return {"instructions": "# Office\n\n## Mission\nOk."}

    monkeypatch.setattr(sg, "_run_chunk", fake_run_chunk)

    out, changes, warnings = await sg.generate_office_instructions(
        "cbcl-office-test",
        "Quote Shop",
        None,
        "",
        "ground it",
        "regenerate",
        sources=["source/framework.zip"],
        workspace_path=str(ws),
    )
    # Extraction really happened on the "host".
    assert (ws / "source" / "framework" / "playbook.md").read_text() == "method"
    scoped_prompt = survey_mock.await_args.args[2]
    assert "- /workspace/source/framework/" in scoped_prompt
    assert "framework.zip" not in scoped_prompt
    assert warnings == []


# ---------------------------------------------------------------------------
# Wizard — the final config payload carries source_warnings
# ---------------------------------------------------------------------------


class _FakeRouter:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish_event(self, event: dict) -> None:
        self.events.append(event)


_WIZARD_AGENT = {
    "name": "quote-builder",
    "display_name": "Quote Builder",
    "avatar_emoji": "\U0001f9f0",
    "role_description": "Owns the quoting pipeline.",
    "model": "opus",
    "allowed_tools": ["Read", "Write"],
    "skill_template_ids": [],
    "skill_names": [],
}


@pytest.fixture()
def wizard_chunks(monkeypatch):
    async def fake_run_chunk(container, system_prompt, user_prompt, **kwargs):
        if system_prompt is SYNTHESIZE_VISION_PROMPT:
            return {"vision": "## Mission\nQuote fast."}
        if system_prompt is INSTRUCTIONS_PROMPT:
            return {"instructions": "## Mission\nQuote things."}
        if system_prompt is ROSTER_PROMPT:
            return {"agents": [dict(_WIZARD_AGENT)]}
        if system_prompt is AGENT_DETAIL_PROMPT:
            return {"system_prompt": "sp", "claude_md_content": "notes"}
        raise AssertionError("unexpected system prompt in test")

    monkeypatch.setattr(sg, "_run_chunk", fake_run_chunk)


async def test_wizard_config_carries_source_warnings(
    monkeypatch,
    tmp_path: Path,
    wizard_chunks,
) -> None:
    ws = tmp_path / "workspace"
    (ws / "source").mkdir(parents=True)
    (ws / "source" / "broken.zip").write_bytes(b"not a zip")

    monkeypatch.setattr(
        sg,
        "_container_has_source_files",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        sg,
        "_run_source_survey",
        AsyncMock(
            return_value={
                "source_brief": "b",
                "inventory": [{"path": "left.xlsx", "role": "quoting model"}],
            }
        ),
    )

    router = _FakeRouter()
    await sg.generate_office_config(
        router=router,
        request_id="req-1",
        office_name="Quote Shop",
        office_description="We quote fabrication jobs.",
        requirements={},
        skill_catalog=[],
        container_name="cbcl-office-test",
        workspace_path=str(ws),
    )
    final = router.events[-1]
    assert final["type"] == "setup_generation_complete"
    warnings = final["config"]["source_warnings"]
    assert any("broken.zip" in w for w in warnings)
    assert any("left.xlsx" in w for w in warnings)


async def test_wizard_config_source_warnings_empty_on_clean_run(
    monkeypatch,
    wizard_chunks,
) -> None:
    """No workspace_path (older wiring) + a clean survey = the honest
    empty list, and the expansion never touches the filesystem."""
    monkeypatch.setattr(
        sg,
        "_container_has_source_files",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        sg,
        "_run_source_survey",
        AsyncMock(
            return_value={
                "source_brief": "b",
                "inventory": [{"path": "notes.md", "role": "process notes"}],
            }
        ),
    )

    router = _FakeRouter()
    await sg.generate_office_config(
        router=router,
        request_id="req-2",
        office_name="Quote Shop",
        office_description="We quote fabrication jobs.",
        requirements={},
        skill_catalog=[],
        container_name="cbcl-office-test",
    )
    final = router.events[-1]
    assert final["type"] == "setup_generation_complete"
    assert final["config"]["source_warnings"] == []


async def test_expansion_runs_before_the_scoped_survey_prompt_is_built(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """SOURCE_SURVEY_PROMPT is still the system prompt on the scoped
    call (the swap changes only the listing)."""
    ws = tmp_path / "workspace"
    _make_zip(ws / "source" / "kit.zip", {"kit-v1/a.md": "x"})
    survey_mock = AsyncMock(
        return_value={
            "source_brief": "b",
            "inventory": [],
        }
    )
    monkeypatch.setattr(sg, "_run_source_survey", survey_mock)

    async def fake_run_chunk(container, system_prompt, user_prompt, **kwargs):
        return {"context_notes": "### Conventions\nOk."}

    monkeypatch.setattr(sg, "_run_chunk", fake_run_chunk)

    await sg.generate_workstream_context_note(
        "cbcl-office-test",
        "Quoting",
        "use the kit",
        "Quote Shop",
        sources=["source/kit.zip"],
        workspace_path=str(ws),
    )
    assert (ws / "source" / "kit" / "a.md").read_text() == "x"
    assert survey_mock.await_args.args[1] is SOURCE_SURVEY_PROMPT
    assert "- /workspace/source/kit/" in survey_mock.await_args.args[2]


# ---------------------------------------------------------------------------
# Review-round coverage: marker-aware swap, scoped extraction, guards
# ---------------------------------------------------------------------------


def _extract_current(source_dir: Path, name: str, files: dict) -> Path:
    """Make a zip + a CURRENT (marker-matched) extraction of it."""
    zip_path = source_dir / name
    _make_zip(zip_path, files)
    assert sa.expand_source_archives(source_dir) == []
    return zip_path


def test_swap_refuses_stale_marker_dir(tmp_path: Path) -> None:
    """The headline daemon finding: a re-uploaded zip whose re-extraction
    failed must NOT be swapped for its now-STALE directory — the zip
    stays listed so the honest warning fires instead of the survey
    silently grounding on outdated content."""
    ws = tmp_path / "workspace"
    source = ws / "source"
    source.mkdir(parents=True)
    zip_path = _extract_current(source, "framework.zip", {"doc.md": "v1"})

    # Re-upload a CORRUPT v2: extraction fails, old dir + stale marker stay.
    zip_path.write_bytes(b"not a zip at all")
    os.utime(zip_path, ns=(7, 7))
    warnings = sa.expand_source_archives(source)
    assert any("could not be read" in w for w in warnings)
    assert (source / "framework" / "doc.md").read_text() == "v1"

    out = sg._swap_extracted_zip_paths(["source/framework.zip"], str(ws))
    assert out == ["source/framework.zip"], (
        "a stale-markered dir must never stand in for the current zip"
    )
    # And the wizard-side suppression set must not claim it either.
    assert sg._extracted_zip_rel_paths_sync(str(ws)) == set()


def test_swap_ignores_zips_outside_source_top_level(tmp_path: Path) -> None:
    """Only zips DIRECTLY under source/ are ever extracted — a subdir zip
    (or any other path) must never swap to a coincidental sibling dir."""
    ws = tmp_path / "workspace"
    sub = ws / "source" / "docs"
    sub.mkdir(parents=True)
    (sub / "misc").mkdir()
    (sub / "misc" / "unrelated.md").write_text("x")
    _make_zip(sub / "misc.zip", {"real.md": "content"})

    out = sg._swap_extracted_zip_paths(["source/docs/misc.zip"], str(ws))
    assert out == ["source/docs/misc.zip"]


def test_extracted_rel_paths_are_full_paths_not_basenames(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "workspace"
    source = ws / "source"
    source.mkdir(parents=True)
    _extract_current(source, "framework.zip", {"doc.md": "v1"})
    assert sg._extracted_zip_rel_paths_sync(str(ws)) == {
        "source/framework.zip"
    }


def test_suppression_keys_on_relative_path(tmp_path: Path) -> None:
    """#24 + #21: the unreadable-warning suppression fires for an
    extracted top-level zip and does NOT fire for a same-named zip in a
    subdirectory."""
    sink: list[str] = []
    block = sg._build_source_survey_block(
        {
            "source_brief": "brief",
            "inventory": [
                {"path": "source/framework.zip", "role": "the framework"},
                {"path": "source/docs/framework.zip", "role": "a copy"},
            ],
        },
        warnings_sink=sink,
        extracted_zip_paths={"source/framework.zip"},
    )
    assert block
    joined = " ".join(sink)
    assert "source/docs/framework.zip" in joined
    assert joined.count("framework.zip") == 1


def test_only_names_scopes_extraction_and_warnings(tmp_path: Path) -> None:
    """#6: a scoped generation extracts (and warns about) ONLY the zips
    the request attached — unrelated archives in source/ stay silent."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "attached.zip").write_bytes(b"corrupt")
    (source / "unrelated.zip").write_bytes(b"also corrupt")

    warnings = sa.expand_source_archives(
        source, only_names={"attached.zip"}
    )
    joined = " ".join(warnings)
    assert "attached.zip" in joined
    assert "unrelated.zip" not in joined


def test_marker_only_zip_is_not_treated_as_extracted(tmp_path: Path) -> None:
    """#10: a zip whose every entry is filtered (nested-only) must not
    promote a marker-only dir — it stays unextracted with a warning, and
    the survey-side helpers keep the zip listed."""
    import zipfile as zf_mod

    ws = tmp_path / "workspace"
    source = ws / "source"
    source.mkdir(parents=True)
    with zf_mod.ZipFile(source / "nested-only.zip", "w") as zf:
        zf.writestr("inner.zip", "PK")
    warnings = sa.expand_source_archives(source)
    assert any("nothing extractable" in w for w in warnings)
    assert not (source / "nested-only").exists()
    assert sg._swap_extracted_zip_paths(
        ["source/nested-only.zip"], str(ws)
    ) == ["source/nested-only.zip"]


def test_copy_capped_belt_trips_on_lying_sizes(tmp_path: Path) -> None:
    """#25: the streamed byte belt itself — a budget smaller than the
    real content stops the copy and reports False."""
    import zipfile as zf_mod

    zip_path = tmp_path / "belt.zip"
    with zf_mod.ZipFile(zip_path, "w") as zf:
        zf.writestr("big.md", "x" * 100)
    with zf_mod.ZipFile(zip_path) as zf:
        info = zf.infolist()[0]
        budget = [10]
        ok = sa._copy_capped(zf, info, tmp_path / "out" / "big.md", budget)
    assert ok is False
    assert budget[0] < 0


def test_extraction_chowns_created_paths(
    tmp_path: Path, monkeypatch
) -> None:
    """#27: every created dir, file and the marker goes through
    chown_to_agent — the daemon runs as root on prod hosts and a
    root-owned extraction is unreadable in-container."""
    chowned: list[str] = []
    monkeypatch.setattr(
        sa, "chown_to_agent", lambda p: chowned.append(str(p))
    )
    source = tmp_path / "source"
    source.mkdir()
    _make_zip(source / "kit.zip", {"a/one.md": "1", "two.md": "2"})
    assert sa.expand_source_archives(source) == []
    joined = " ".join(chowned)
    assert "one.md" in joined
    assert "two.md" in joined
    assert sa._EXTRACTION_MARKER in joined
    assert any(c.endswith("/a") for c in chowned)


def test_symlinked_zip_or_target_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    real = tmp_path / "outside.zip"
    _make_zip(real, {"doc.md": "x"})
    (source / "linked.zip").symlink_to(real)
    warnings = sa.expand_source_archives(source)
    assert any("symlink" in w for w in warnings)
    assert not (source / "linked").exists()


def test_backslash_directory_entries_extract(tmp_path: Path) -> None:
    """#12: a legacy Windows zip with backslash paths must extract its
    nested files instead of failing wholesale."""
    import zipfile as zf_mod

    source = tmp_path / "source"
    source.mkdir()
    with zf_mod.ZipFile(source / "win.zip", "w") as zf:
        zf.writestr("docs\\", "")
        zf.writestr("docs\\file.md", "windows content")
        zf.writestr("readme.md", "root file")  # two roots — no flattening
    assert sa.expand_source_archives(source) == []
    assert (source / "win" / "docs" / "file.md").read_text() == (
        "windows content"
    )
    assert (source / "win" / "readme.md").read_text() == "root file"


@pytest.mark.asyncio
async def test_wizard_total_survey_failure_is_a_visible_warning(
    monkeypatch,
    tmp_path: Path,
    wizard_chunks,
) -> None:
    """#5: a survey that dies entirely must reach the Review step as a
    source_warning — a log-only failure was the incident's silent half."""
    ws = tmp_path / "workspace"
    (ws / "source").mkdir(parents=True)
    (ws / "source" / "notes.md").write_text("real source")

    monkeypatch.setattr(
        sg,
        "_container_has_source_files",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        sg,
        "_run_source_survey",
        AsyncMock(side_effect=RuntimeError("survey session died")),
    )

    router = _FakeRouter()
    await sg.generate_office_config(
        router=router,
        request_id="req-fail",
        office_name="Quote Shop",
        office_description="We quote fabrication jobs.",
        requirements={},
        skill_catalog=[],
        container_name="cbcl-office-test",
        workspace_path=str(ws),
    )
    final = router.events[-1]
    assert final["type"] == "setup_generation_complete"
    warnings = final["config"]["source_warnings"]
    assert any("Source survey failed" in w for w in warnings)
