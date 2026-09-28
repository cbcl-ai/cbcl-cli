"""Source-warning integration for generation surveys.

Current surveys preserve selected paths for the protected container
reader, which expands ZIP evidence in memory without extracting. The
unused host-side extractor (``src/source_archives.py``) and its tests were
removed (C4d-G9).
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

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


# ---------------------------------------------------------------------------
# No host-side archive preparation remains
# ---------------------------------------------------------------------------


def test_generation_has_no_host_archive_preparation_api() -> None:
    # Checked on disk next to the generator module: an editable install of
    # another checkout could otherwise satisfy an import lookup.
    assert not (Path(sg.__file__).parent / "source_archives.py").exists()
    assert not hasattr(sg, "_expand_source_archives_host")
    assert not hasattr(sg, "_swap_extracted_zip_paths")
    assert not hasattr(sg, "_extracted_zip_rel_paths_sync")


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


def test_long_warning_is_cut_at_a_word_boundary() -> None:
    text = "Source note: " + "alpha beta gamma " * 30
    (out,) = sg._cap_source_warnings([text])
    assert len(out) <= sg._SOURCE_WARNING_MAX_CHARS
    assert out.endswith("…")
    assert out[:-1].split()[-1] in {"alpha", "beta", "gamma"}


def test_unreadable_warning_names_whole_paths_and_keeps_the_advice() -> None:
    paths = [f"source/pack.zip!/models/estimation-model-{i:02}.xlsx" for i in range(12)]
    warning = sg._unreadable_sources_warning(paths)
    assert len(warning) <= sg._SOURCE_WARNING_MAX_CHARS
    assert warning.endswith("re-upload a text/CSV/HTML/PDF export if these encode method.")
    named = [path for path in paths if path in warning]
    assert named and warning.count(".xlsx") == len(named)
    assert f"and {len(paths) - len(named)} more" in warning
    # The cap leaves it untouched.
    assert sg._cap_source_warnings([warning]) == [warning]


def test_unreadable_warning_counts_a_path_too_long_to_name() -> None:
    warning = sg._unreadable_sources_warning(["source/" + "d" * 400 + ".xlsx"])
    assert "1 file" in warning and "ddd" not in warning
    assert warning.endswith("encode method.")


async def test_office_instructions_result_carries_source_warnings(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """The settings 3-tuple: extraction warning (corrupt zip) + brief
    truncation + remaining-unreadable names all reach the caller."""
    ws = tmp_path / "workspace"
    (ws / "source").mkdir(parents=True)
    (ws / "source" / "broken.zip").write_bytes(b"not a zip")

    async def prepared_survey(*args, warnings_sink=None, **kwargs):
        warnings_sink.append("source/broken.zip: archive could not be safely read.")
        return {
            "source_brief": "x" * 7000,
            "inventory": [{"path": "left.xlsx", "role": "quoting model"}],
        }

    survey_mock = AsyncMock(side_effect=prepared_survey)
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


async def test_scoped_survey_passes_archive_selection_without_host_extraction(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """The protected container reader, not the host, receives the selected ZIP."""
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
    assert not (ws / "source" / "framework").exists()
    scoped_prompt = survey_mock.await_args.args[2]
    assert "- /workspace/source/framework.zip" in scoped_prompt
    assert survey_mock.await_args.kwargs["source_paths"] == ["source/framework.zip"]
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
    async def prepared_survey(*args, warnings_sink=None, **kwargs):
        warnings_sink.append("source/broken.zip: archive could not be safely read.")
        return {
            "source_brief": "b",
            "inventory": [{"path": "left.xlsx", "role": "quoting model"}],
        }

    monkeypatch.setattr(sg, "_run_source_survey", AsyncMock(side_effect=prepared_survey))

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


async def test_scoped_survey_keeps_source_prompt_and_original_selection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """The original selection is passed to the protected evidence reader."""
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
    assert not (ws / "source" / "kit").exists()
    assert survey_mock.await_args.args[1] is SOURCE_SURVEY_PROMPT
    assert "- /workspace/source/kit.zip" in survey_mock.await_args.args[2]
    assert survey_mock.await_args.kwargs["source_paths"] == ["source/kit.zip"]


# ---------------------------------------------------------------------------
# Survey failure visibility
# ---------------------------------------------------------------------------


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
