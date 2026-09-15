"""Uploaded guidance, standing rules and examples have different authority."""

import json
from unittest.mock import AsyncMock

import pytest
from src import _setup_cli as cli
from src import setup_generator as sg
from src._source_purpose import SOURCE_PURPOSE_PROMPT, plan_source_use, purpose_preview
from src._source_survey import survey_prepared_sources

DOCUMENTS = [
    {
        "path": "source/SETUP.md",
        "content": "Design an estimation office; the site is an output example. " * 400,
    },
    {
        "path": "source/pack.zip!/rules.md",
        "content": "Standing rule: owner approves prices." * 1000,
    },
    {
        "path": "source/pack.zip!/example/index.html",
        "content": "Sample client and sample amount." * 2000,
    },
    {
        "path": "source/pack.zip!/_next/bundle.js",
        "content": "irrelevant-bundle-canary" * 3000,
    },
]
PLAN = {
    "design_intent": "Estimation office. Reuse the website format, not its sample project.",
    "sources": [
        {"source_id": 0, "purpose": "setup_guidance", "study": "full"},
        {"source_id": 1, "purpose": "operating_rules", "study": "full"},
        {"source_id": 2, "purpose": "example", "study": "sample"},
        {"source_id": 3, "purpose": "irrelevant", "study": "skip"},
    ],
}


async def test_source_roles_control_full_reads_samples_and_skips():
    classify = AsyncMock(return_value=PLAN)
    selected, intent = await plan_source_use(
        DOCUMENTS, "Use the website only as an output example.", classify
    )
    assert selected[0]["content"] == DOCUMENTS[0]["content"]
    assert selected[1]["content"] == DOCUMENTS[1]["content"]
    assert len(selected[2]["content"]) < 11000 and selected[2]["sampled"]
    assert selected[3]["content"] == ""
    assert "output example" in classify.await_args.args[0]
    assert intent == PLAN["design_intent"]
    # Caller-owned snapshot is unchanged.
    assert "irrelevant-bundle-canary" in DOCUMENTS[3]["content"]


async def test_missing_decisions_and_rules_cannot_silently_skip_source_text():
    plan = {
        "design_intent": "Design",
        "sources": [
            {
                "source_id": 0,
                "purpose": "setup_guidance",
                "study": "skip",
            },
        ],
    }
    selected, _ = await plan_source_use(
        DOCUMENTS, "Design", AsyncMock(return_value=plan)
    )
    assert all(item["study"] == "full" for item in selected)
    assert all(
        item["content"] == original["content"]
        for item, original in zip(selected, DOCUMENTS)
    )


@pytest.mark.parametrize(
    "path", [-1, 900, None, [], True, "0"]
)
async def test_classification_cannot_widen_source_access(path):
    classify = AsyncMock(
        return_value={
            "design_intent": "Design",
            "sources": [
                {"source_id": path, "purpose": "reference", "study": "full"},
            ],
        }
    )
    with pytest.raises(ValueError, match="unavailable"):
        await plan_source_use(DOCUMENTS, "Design", classify)


def test_source_inventory_previews_do_not_send_whole_bundles():
    preview = purpose_preview(DOCUMENTS)
    assert len(preview[0]["excerpt"]) == 12000
    assert len(preview[-1]["excerpt"]) == 1600
    assert all(item["excerpt_only"] for item in preview)


async def test_study_and_reference_inventory_keep_source_purposes():
    calls = []

    async def summarize(prompt):
        calls.append(prompt)
        return {
            "source_brief": "An estimation office using a reference layout.",
            "inventory": [],
        }

    result = await survey_prepared_sources(
        DOCUMENTS,
        "Use the site as an example.",
        summarize,
        classify=AsyncMock(return_value=PLAN),
    )
    assert all("irrelevant-bundle-canary" not in prompt for prompt in calls)
    assert "one-time setup steps" in calls[0]
    warnings = []
    block = sg._build_source_survey_block(result, warnings_sink=warnings)
    assert "source/SETUP.md" not in block
    assert "bundle.js" not in block
    assert "source/pack.zip!/rules.md [operating_rules]" in block
    assert "source/pack.zip!/example/index.html [example]" in block
    assert not warnings


async def test_only_intentionally_skipped_source_warnings_are_suppressed(monkeypatch):
    monkeypatch.setattr(
        cli,
        "_prepare_source_evidence",
        AsyncMock(
            return_value={
                "documents": DOCUMENTS,
                "warnings": [
                    DOCUMENTS[3]["path"] + ": unreadable",
                    DOCUMENTS[1]["path"] + ": partial",
                    "Global source limit reached",
                ],
            }
        ),
    )

    async def generate(_container, system, _user, **kwargs):
        if system == SOURCE_PURPOSE_PROMPT:
            return json.dumps(PLAN)
        return "Estimation office."

    monkeypatch.setattr(cli, "_run_claude_cli", generate)
    warnings = []
    await cli._run_source_survey(
        "office", "Study", "Use site as example", warnings_sink=warnings
    )
    assert warnings == [
        DOCUMENTS[1]["path"] + ": partial",
        "Global source limit reached",
    ]


@pytest.mark.parametrize("surface", ["office", "workstream"])
async def test_improvement_request_and_current_context_reach_source_analysis(
    monkeypatch, surface
):
    survey = AsyncMock(
        return_value={"source_brief": "Source roles understood.", "inventory": []}
    )
    monkeypatch.setattr(sg, "_run_source_survey", survey)
    monkeypatch.setattr(
        sg,
        "_run_chunk",
        AsyncMock(
            return_value={
                "instructions": "Office produces estimates.",
                "context_notes": "Scope estimation.",
                "changes": [],
            }
        ),
    )
    request = "Use the website as an output example, never its customer or prices."
    if surface == "office":
        await sg.generate_office_instructions(
            "office",
            "Estimation",
            "Estimation mission",
            "Current office charter",
            request,
            "improve",
            ["source/pack.zip"],
        )
    else:
        await sg.generate_workstream_context_note(
            "office",
            "Estimation",
            request,
            mode="improve",
            current_notes="Current project charter",
            sources=["source/pack.zip"],
        )
    prompt = survey.await_args.args[2]
    assert request in prompt
    assert (
        "Current office charter" if surface == "office" else "Current project charter"
    ) in prompt
    assert "<user_input>" in prompt


async def test_malformed_json_gets_one_safe_retry_within_shared_budget(monkeypatch):
    monkeypatch.setattr(
        cli,
        "_prepare_source_evidence",
        AsyncMock(
            return_value={
                "documents": [{"path": "source/rules.md", "content": "Standing rules"}],
                "warnings": [],
            }
        ),
    )
    generate = AsyncMock(
        side_effect=[
            '{"source_brief":"bad "quote""}',
            '{"design_intent":"Design","sources":[{"source_id":0,"purpose":"operating_rules","study":"full"}]}',
            'Owner approves "final" prices.',
        ]
    )
    monkeypatch.setattr(cli, "_run_claude_cli", generate)
    result = await cli._run_source_survey("office", "Study", "Design")
    assert result["source_brief"] == 'Owner approves "final" prices.'
    assert generate.await_count == 3
    assert "Escape all quotes" in generate.await_args_list[1].args[2]
    assert 0 < generate.await_args.kwargs["timeout"] <= 300


async def test_repeated_malformed_json_stops_after_one_retry(monkeypatch):
    monkeypatch.setattr(
        cli,
        "_prepare_source_evidence",
        AsyncMock(
            return_value={
                "documents": [{"path": "source/rules.md", "content": "Standing rules"}],
                "warnings": ["Original read warning"],
            }
        ),
    )
    generate = AsyncMock(return_value='{"source_brief":"bad "quote""}')
    monkeypatch.setattr(cli, "_run_claude_cli", generate)
    warnings = []
    with pytest.raises(json.JSONDecodeError):
        await cli._run_source_survey(
            "office", "Study", "Design", warnings_sink=warnings
        )
    assert generate.await_count == 2
    assert warnings == ["Original read warning"]


async def test_overlong_findings_are_condensed_without_losing_inventory():
    summary = AsyncMock(
        side_effect=[
            {
                "source_brief": "Repeated findings. " * 300,
                "inventory": [
                    {
                        "path": "source/rules.md",
                        "purpose": "operating_rules",
                        "study": "full",
                        "role": "Approval method",
                    }
                ],
            },
            {"source_brief": "Owner approval is required.", "inventory": []},
        ]
    )
    result = await survey_prepared_sources(
        [{"path": "source/rules.md", "content": "Owner approval is required."}],
        "Design",
        summary,
    )
    assert summary.await_count == 2
    assert "<source_findings>" in summary.await_args.args[0]
    assert result["source_brief"] == "Owner approval is required."
    assert result["inventory"][0]["purpose"] == "operating_rules"
    assert result["inventory"][0]["role"] == "Approval method"


async def test_all_irrelevant_sources_do_not_trigger_empty_study_failure():
    classify = AsyncMock(
        return_value={
            "design_intent": "No relevant sources",
            "sources": [
                {"source_id": source_id, "purpose": "irrelevant", "study": "skip"}
                for source_id, item in enumerate(DOCUMENTS)
            ],
        }
    )
    summarize = AsyncMock()
    result = await survey_prepared_sources(
        DOCUMENTS, "Design from my description", summarize, classify=classify
    )
    summarize.assert_not_awaited()
    assert "no relevant operating requirements" in result["source_brief"]
    assert all(item["study"] == "skip" for item in result["inventory"])


async def test_complete_setup_guidance_precedes_and_informs_pack_study(monkeypatch):
    from src import _source_survey as survey

    monkeypatch.setattr(survey, "SURVEY_BATCH_CHARACTERS", 40000)
    calls = []

    async def summarize(prompt):
        calls.append(prompt)
        return {"source_brief": "Use the website only as an output layout.", "inventory": []}

    await survey.survey_prepared_sources(
        DOCUMENTS, "Design office", summarize, classify=AsyncMock(return_value=PLAN)
    )
    assert "source/SETUP.md" in calls[0]
    assert "Standing rule: owner approves prices." not in calls[0]
    assert "<setup_guide_findings>" in calls[1]
    assert "Use the website only as an output layout." in calls[1]
    assert "Standing rule: owner approves prices." in calls[1]
