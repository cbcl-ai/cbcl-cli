"""One failed source section no longer discards the whole survey (WP5).

Also pins that the files the reader skipped at its document limit reach every
survey call by name, and that the generation block says how many were not read.
"""

import json
import logging
import subprocess
from unittest.mock import AsyncMock

import pytest

from src import _setup_cli as cli
from src import _source_survey as survey
from src import setup_generator as sg
from src.runtime_state import AdmissionPaused, QuotaPaused


def _prepared(prompt: str) -> list[dict]:
    return json.loads(
        prompt.split("<prepared_sources>\n")[1].split("\n</prepared_sources>")[0]
    )


def _section_findings(prompt: str) -> dict:
    return json.loads(
        prompt.split("<section_findings>\n")[1].split("\n</section_findings>")[0]
    )


def _documents(*paths: str) -> list[dict]:
    return [{"path": path, "content": f"facts from {path}"} for path in paths]


@pytest.fixture()
def one_document_per_batch(monkeypatch):
    # Each test document ("facts from x.md", 15 characters) fills one batch.
    monkeypatch.setattr(survey, "SURVEY_BATCH_CHARACTERS", 15)
    monkeypatch.setattr(survey, "SURVEY_CONCURRENCY", 1)


async def test_one_failed_section_keeps_the_other_findings(one_document_per_batch):
    combine_prompts = []

    async def summarize(prompt):
        if "<prepared_sources>" in prompt:
            paths = [section["path"] for section in _prepared(prompt)]
            if paths == ["b.md"]:
                raise ValueError("Source study returned no findings for readable text")
            return {"source_brief": f"found {paths[0]}", "inventory": []}
        combine_prompts.append(prompt)
        return {"source_brief": "combined findings", "inventory": []}

    result = await survey.survey_prepared_sources(
        _documents("a.md", "b.md", "c.md"), "Study", summarize
    )

    assert result["source_brief"] == "combined findings"
    findings = _section_findings(combine_prompts[0])
    assert [item["source_brief"] for item in findings["findings"]] == [
        "found a.md",
        "found c.md",
    ]
    coverage = {item["path"]: item for item in findings["coverage"]}
    assert coverage["b.md"]["study_failed"] == "all"
    assert "study_failed" not in coverage["a.md"]
    roles = {item["path"]: item["role"] for item in result["inventory"]}
    assert roles["b.md"].startswith("Not studied")
    assert roles["a.md"] == "Source document"
    assert result["study_warnings"] == [
        "1 source could not be studied (the study call failed): b.md. Their "
        "unstudied content did not inform the design; select fewer or smaller "
        "sources and generate again."
    ]


async def test_time_limit_failures_are_named_as_such(one_document_per_batch):
    async def summarize(prompt):
        if "<prepared_sources>" in prompt:
            if _prepared(prompt)[0]["path"] == "a.md":
                return {"source_brief": "found a", "inventory": []}
            raise TimeoutError("Source study exceeded its time budget")
        return {"source_brief": "unused", "inventory": []}

    result = await survey.survey_prepared_sources(
        _documents("a.md", "b.md", "c.md"), "Study", summarize
    )

    # One report left: used as is, no combine call needed.
    assert result["source_brief"] == "found a"
    warning = result["study_warnings"][0]
    assert warning.startswith(
        "2 sources could not be studied (the study time limit was reached): b.md, c.md"
    )


async def test_a_partly_studied_source_is_marked_partly(monkeypatch):
    monkeypatch.setattr(survey, "SURVEY_BATCH_CHARACTERS", 10)
    monkeypatch.setattr(survey, "SURVEY_CONCURRENCY", 1)
    calls = 0

    async def summarize(prompt):
        nonlocal calls
        if "<prepared_sources>" in prompt:
            calls += 1
            if calls == 2:
                raise ValueError("the second section failed")
            return {"source_brief": f"section {calls}", "inventory": []}
        return {"source_brief": "combined", "inventory": []}

    result = await survey.survey_prepared_sources(
        [{"path": "manual.md", "content": "x" * 25}], "Study", summarize
    )

    assert "manual.md (partly)" in result["study_warnings"][0]
    assert result["inventory"][0]["role"].startswith("Partly studied")


async def test_the_survey_fails_when_no_section_could_be_studied(one_document_per_batch):
    async def summarize(prompt):
        raise ValueError("every section failed")

    with pytest.raises(ValueError, match="every section failed"):
        await survey.survey_prepared_sources(
            _documents("a.md", "b.md"), "Study", summarize
        )


async def test_a_failed_guide_with_a_textless_remainder_fails_the_survey(
    one_document_per_batch,
):
    """A brief from a section with no text is not a study of the sources."""

    async def classify(prompt):
        return {
            "design_intent": "",
            "sources": [
                {"source_id": 0, "purpose": "setup_guidance", "study": "full"},
                {"source_id": 1, "purpose": "irrelevant", "study": "skip"},
            ],
        }

    async def summarize(prompt):
        if "START-HERE" in prompt.split("<prepared_sources>")[1]:
            raise ValueError("the guide study failed")
        return {"source_brief": "No readable content in these sections.", "inventory": []}

    with pytest.raises(ValueError, match="the guide study failed"):
        await survey.survey_prepared_sources(
            [
                {"path": "START-HERE.md", "content": "Build a quoting office."},
                {"path": "logo.png", "content": "", "unreadable": "unsupported"},
            ],
            "Study",
            summarize,
            classify=classify,
        )


async def test_an_unreadable_guide_does_not_mask_failed_text_studies():
    async def classify(prompt):
        return {
            "design_intent": "",
            "sources": [
                {"source_id": 0, "purpose": "setup_guidance", "study": "full"},
                {"source_id": 1, "purpose": "operating_rules", "study": "full"},
            ],
        }

    async def summarize(prompt):
        prepared = _prepared(prompt)
        if any(item["content"] for item in prepared):
            raise _deadline_error()
        return {
            "source_brief": "The setup guide could not be read, so its directions are unknown.",
            "inventory": [],
        }

    with pytest.raises(cli.GenerationDeadlineError):
        await survey.survey_prepared_sources(
            [
                {
                    "path": "source/START-HERE.docx",
                    "content": "",
                    "unreadable": "unsupported, unreadable or failed conversion",
                },
                {"path": "source/manual.md", "content": "The quoting method: ..."},
            ],
            "Study",
            summarize,
            classify=classify,
        )


async def test_only_textless_sources_still_give_a_result():
    """Nothing had text, so nothing with text was lost: not a failure."""
    summarize = AsyncMock(
        return_value={"source_brief": "The files could not be read.", "inventory": []}
    )
    result = await survey.survey_prepared_sources(
        [{"path": "scan.docx", "content": "", "unreadable": "unsupported"}],
        "Study",
        summarize,
    )
    assert result["source_brief"] == "The files could not be read."
    assert result["study_warnings"] == []


async def test_an_unread_source_keeps_the_readers_reason_as_its_role():
    """An export only helps when the format is the reason."""
    capped = "archive has more than 2,500 files, so none of its files were read"
    summarize = AsyncMock(return_value={"source_brief": "Findings.", "inventory": []})
    result = await survey.survey_prepared_sources(
        [
            {"path": "big.zip", "content": "", "unreadable": capped},
            {"path": "scan.docx", "content": "", "unreadable": survey.UNREADABLE_FORMAT},
            {"path": "guide.md", "content": "The rules."},
        ],
        "Study",
        summarize,
    )
    roles = {item["path"]: item["role"] for item in result["inventory"]}
    assert roles == {
        "big.zip": f"Present but unreadable: {capped}.",
        "scan.docx": "Present but unreadable; provide a text/CSV/HTML/PDF export.",
        "guide.md": "Source document",
    }


@pytest.mark.parametrize("pause", [QuotaPaused, AdmissionPaused])
async def test_an_admission_pause_stops_the_whole_survey(one_document_per_batch, pause):
    async def summarize(prompt):
        if _prepared(prompt)[0]["path"] == "a.md":
            raise pause("AI work is paused")
        return {"source_brief": "found", "inventory": []}

    # Not recorded as one failed section: every later call is refused too.
    with pytest.raises(pause):
        await survey.survey_prepared_sources(
            _documents("a.md", "b.md", "c.md"), "Study", summarize
        )


async def test_section_and_combine_failures_reach_the_operator_log(
    one_document_per_batch, caplog
):
    async def summarize(prompt):
        if "<prepared_sources>" in prompt:
            path = _prepared(prompt)[0]["path"]
            if path == "b.md":
                raise RuntimeError("Claude CLI failed (rc=1): stderr detail")
            return {"source_brief": f"found {path}", "inventory": []}
        raise RuntimeError("Claude CLI failed (rc=2): combine stderr")

    with caplog.at_level(logging.WARNING, logger=survey.__name__):
        await survey.survey_prepared_sources(
            _documents("a.md", "b.md", "c.md"), "Study", summarize
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "Source survey section failed (b.md)" in message
        and "RuntimeError: Claude CLI failed (rc=1): stderr detail" in message
        for message in messages
    )
    assert any(
        "combine step failed" in message
        and "Claude CLI failed (rc=2): combine stderr" in message
        for message in messages
    )


async def test_a_policy_failure_is_never_partial(one_document_per_batch):
    async def summarize(prompt):
        if _prepared(prompt)[0]["path"] == "b.md":
            raise cli.GenerationPolicyError("unsupported protected runtime")
        return {"source_brief": "found", "inventory": []}

    with pytest.raises(cli.GenerationPolicyError):
        await survey.survey_prepared_sources(
            _documents("a.md", "b.md"), "Study", summarize
        )


async def test_combine_failure_keeps_the_studied_findings(one_document_per_batch):
    async def summarize(prompt):
        if "<prepared_sources>" in prompt:
            return {
                "source_brief": f"found {_prepared(prompt)[0]['path']}",
                "inventory": [],
            }
        raise TimeoutError("Source study exceeded its time budget")

    result = await survey.survey_prepared_sources(
        _documents("a.md", "b.md"), "Study", summarize
    )

    # The findings fit, so nothing was shortened and the warning says so.
    assert result["source_brief"] == "found a.md\n\nfound b.md"
    assert result["study_warnings"] == [
        "The findings of 2 studied source sections could not be combined (the "
        "study time limit was reached). They were kept as studied."
    ]
    assert [item["path"] for item in result["inventory"]] == ["a.md", "b.md"]


async def test_combine_fallback_says_when_findings_were_shortened(one_document_per_batch):
    async def summarize(prompt):
        if "<prepared_sources>" in prompt:
            path = _prepared(prompt)[0]["path"]
            return {"source_brief": f"{path} " + "word " * 700, "inventory": []}
        raise ValueError("combine returned nothing")

    result = await survey.survey_prepared_sources(
        _documents("a.md", "b.md"), "Study", summarize
    )

    assert len(result["source_brief"]) <= survey.MAX_SECTION_BRIEF
    assert result["study_warnings"] == [
        "The findings of 2 studied source sections could not be combined (the "
        "combining call failed). They were kept as studied, with longer "
        "findings shortened to fit."
    ]


def test_joined_findings_give_each_section_a_share():
    reports = [
        {"source_brief": f"{label} " + "word " * 900} for label in ("ALPHA", "BETA", "GAMMA")
    ]
    joined, shortened = survey._joined_findings(reports)
    assert shortened
    assert len(joined) <= survey.MAX_SECTION_BRIEF
    parts = joined.split("\n\n")
    assert [part.split()[0] for part in parts] == ["ALPHA", "BETA", "GAMMA"]
    assert all(part.endswith("…") for part in parts)


def test_joined_findings_that_fit_are_not_shortened():
    reports = [{"source_brief": " found a "}, {"source_brief": "found b"}, {"source_brief": " "}]
    assert survey._joined_findings(reports) == ("found a\n\nfound b", False)


def _deadline_error() -> cli.GenerationDeadlineError:
    """What ``_check_generation_result`` raises for the runner's exit 124."""
    with pytest.raises(cli.GenerationDeadlineError) as caught:
        cli._check_generation_result(
            subprocess.CompletedProcess(
                ["runner"], 124, "", "Generation exceeded its execution deadline."
            )
        )
    return caught.value


def test_the_runner_deadline_is_a_typed_cli_failure():
    error = _deadline_error()
    # Still the RuntimeError every caller already handles, with the same text.
    assert isinstance(error, RuntimeError)
    assert str(error) == (
        "Claude CLI failed (rc=124): Generation exceeded its execution deadline."
    )
    with pytest.raises(RuntimeError) as other:
        cli._check_generation_result(
            subprocess.CompletedProcess(["runner"], 1, "", "boom")
        )
    assert not isinstance(other.value, cli.GenerationDeadlineError)


async def test_the_runner_deadline_counts_as_the_time_limit(one_document_per_batch):
    calls = 0

    async def summarize(prompt):
        nonlocal calls
        if "<prepared_sources>" in prompt:
            calls += 1
            if calls == 1:
                return {"source_brief": "found a", "inventory": []}
            if calls == 2:
                raise _deadline_error()
            raise TimeoutError("Source study exceeded its time budget")
        raise AssertionError("one report needs no combine call")

    result = await survey.survey_prepared_sources(
        _documents("a.md", "b.md", "c.md"), "Study", summarize
    )

    assert result["study_warnings"][0].startswith(
        "2 sources could not be studied (the study time limit was reached): b.md, c.md"
    )


async def test_a_combine_stopped_at_the_deadline_names_the_time_limit(
    one_document_per_batch,
):
    async def summarize(prompt):
        if "<prepared_sources>" in prompt:
            return {"source_brief": f"found {_prepared(prompt)[0]['path']}", "inventory": []}
        raise _deadline_error()

    result = await survey.survey_prepared_sources(
        _documents("a.md", "b.md"), "Study", summarize
    )

    assert "(the study time limit was reached)" in result["study_warnings"][0]


def test_failure_warning_fits_the_wire_limit():
    batch = [{"path": f"source/{'nested/' * 10}file-{i}.md", "content": "x"} for i in range(30)]
    failed = survey._failed_sources([batch], [(batch, TimeoutError())])
    warning = survey._study_failure_warning(failed, [(batch, TimeoutError())])
    assert warning.startswith("30 sources could not be studied")
    assert " more." in warning
    assert len(warning) <= survey.SURVEY_WARNING_CHARACTERS


async def test_files_not_read_reach_every_survey_call():
    prompts = []

    async def classify(prompt):
        prompts.append(prompt)
        return {"design_intent": "", "sources": []}

    async def summarize(prompt):
        prompts.append(prompt)
        return {"source_brief": "found", "inventory": []}

    result = await survey.survey_prepared_sources(
        _documents("a.md"),
        "Study",
        summarize,
        classify=classify,
        sources_not_read={"paths": ["pack.zip!/src/app.py", "pack.zip!/src/lib.py"], "total": 526},
    )

    assert len(prompts) == 2  # classification + one study call
    for prompt in prompts:
        block = prompt.split("<sources_not_read>\n")[1].split("\n</sources_not_read>")[0]
        assert json.loads(block) == {
            "paths": ["pack.zip!/src/app.py", "pack.zip!/src/lib.py"],
            "more": 524,
        }
        assert "526 files that the source reader did not read" in prompt
        assert "never describe their contents as studied" in prompt
    assert result["sources_not_read"] == 526


async def test_no_listing_means_no_not_read_block():
    summarize = AsyncMock(return_value={"source_brief": "found", "inventory": []})
    result = await survey.survey_prepared_sources(_documents("a.md"), "Study", summarize)
    assert "<sources_not_read>" not in summarize.await_args.args[0]
    assert result["sources_not_read"] == 0


@pytest.mark.parametrize(
    ("listing", "expected"),
    [
        (None, ([], 0)),
        ({"paths": "a.md", "total": "3"}, ([], 0)),
        ({"paths": ["a.md", 3, ""], "total": 1}, (["a.md"], 1)),
        ({"paths": ["a.md"], "total": 40}, (["a.md"], 40)),
        ({"paths": ["a.md", "b.md"], "total": True}, (["a.md", "b.md"], 2)),
    ],
)
def test_the_not_read_listing_tolerates_odd_shapes(listing, expected):
    assert survey._not_read_sources(listing) == expected


async def test_runner_passes_the_skipped_listing_and_orders_warnings(monkeypatch):
    monkeypatch.setattr(
        cli,
        "_prepare_source_evidence",
        AsyncMock(
            return_value={
                "policy_version": 1,
                "documents": [{"path": "source/a.md", "content": "facts"}],
                "warnings": [
                    "3 files were not read because the 60-document limit was reached",
                    "source/x.xlsx: unsupported format; studied by filename only.",
                ],
                "skipped": ["source/b.md", "source/c.md"],
                "skipped_total": 3,
            }
        ),
    )
    received = {}

    async def fake_survey(documents, user_prompt, summarize, *, classify, sources_not_read):
        received["sources_not_read"] = sources_not_read
        return {
            "source_brief": "found",
            "inventory": [],
            "sources_not_read": 3,
            "study_warnings": ["1 source could not be studied (the study call failed): a.md."],
        }

    monkeypatch.setattr(survey, "survey_prepared_sources", fake_survey)
    warnings: list[str] = []

    result = await cli._run_source_survey(
        "cbcl-office-test", "sys", "usr", warnings_sink=warnings
    )

    assert received["sources_not_read"] == {
        "paths": ["source/b.md", "source/c.md"],
        "total": 3,
    }
    assert result["sources_not_read"] == 3
    assert warnings == [
        "1 source could not be studied (the study call failed): a.md.",
        "3 files were not read because the 60-document limit was reached",
        "source/x.xlsx: unsupported format; studied by filename only.",
    ]


async def test_runner_accepts_an_older_reader_without_a_listing(monkeypatch):
    monkeypatch.setattr(
        cli,
        "_prepare_source_evidence",
        AsyncMock(
            return_value={
                "policy_version": 1,
                "documents": [{"path": "source/a.md", "content": "facts"}],
                "warnings": [],
            }
        ),
    )
    mock = AsyncMock(
        side_effect=[
            '{"design_intent":"","sources":[{"source_id":0,"purpose":"operating_rules","study":"full"}]}',
            "found",
        ]
    )
    monkeypatch.setattr(cli, "_run_claude_cli", mock)

    result = await cli._run_source_survey("cbcl-office-test", "sys", "usr")

    assert result["source_brief"] == "found"
    assert result["sources_not_read"] == 0
    assert all("<sources_not_read>" not in call.args[2] for call in mock.await_args_list)


def test_the_generation_block_says_how_many_files_were_not_read():
    block = sg._build_source_survey_block(
        {
            "source_brief": "Quoting method.",
            "inventory": [{"path": "source/a.md", "role": "Source document"}],
            "sources_not_read": 526,
        }
    )
    assert (
        "Not read: 526 further selected files (the source reader's document "
        "limit). Only their names were seen; their contents are unknown."
    ) in block


def test_the_not_read_note_survives_a_long_reference_list():
    """The note precedes the references, which the fence cap may shorten."""
    inventory = [
        {"path": f"source/pack.zip!/{'deep/' * 30}file-{index}.md", "role": "Source document"}
        for index in range(60)
    ]
    block = sg._build_source_survey_block(
        {"source_brief": "b " * 2200, "inventory": inventory, "sources_not_read": 9}
    )
    assert "Not read: 9 further selected files" in block
    assert block.index("Not read:") < block.index("Relevant source references")


def test_the_generation_block_has_no_note_when_everything_was_read():
    block = sg._build_source_survey_block(
        {"source_brief": "Quoting method.", "inventory": [], "sources_not_read": 0}
    )
    assert "Not read:" not in block
    assert sg._build_source_survey_block({"source_brief": "", "inventory": [], "sources_not_read": 4}) == ""
