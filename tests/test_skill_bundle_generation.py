"""F08 on the daemon: ``generate_skill`` with ``output_format: skill_bundle_v1``.

Only a backend that publishes the whole folder itself (``defer_write``)
receives companion files; every other request keeps the single-file
contract and the unchanged ``STANDALONE_SKILL_PROMPT``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.setup_generator as sg
from src._handlers._requests import dispatch_backend_request
from src._setup_prompts import STANDALONE_SKILL_PROMPT
from src._skill_bundle_prompt import (
    PROMPT_MAX_COMPANION_FILES,
    STANDALONE_SKILL_BUNDLE_PROMPT,
    normalize_bundle_files,
)

_MODEL = {
    "name": "score-leads",
    "display_name": "Score Leads",
    "description": "Scores leads. Use when a task asks to rank leads.",
    "body": "# Score Leads\n\nSee [rubric](references/rubric.md).",
    "parameter_schema": [],
    "files": [
        {"path": "references/rubric.md", "content": "# Rubric\n", "usage": "read"},
        {"path": "../escape.md", "content": "x", "usage": "read"},
        {"path": "scripts/score.py", "content": "print(1)\n", "usage": "run"},
    ],
}


def _fake_run(captured: dict):
    async def fake_run_chunk(container, system_prompt, user_prompt, **kwargs):
        captured["system_prompt"] = system_prompt
        return dict(_MODEL)

    return fake_run_chunk


def test_bundle_prompt_extends_the_single_file_prompt() -> None:
    assert STANDALONE_SKILL_BUNDLE_PROMPT.startswith(STANDALONE_SKILL_PROMPT)
    addendum = STANDALONE_SKILL_BUNDLE_PROMPT[len(STANDALONE_SKILL_PROMPT) :]
    for pin in (
        '"files"',
        "one level deep",
        "At most 4 files",
        "python3 scripts/<file>",
        "bash scripts/<file>",
        "exits non-zero",
        "longer than 100 lines",
        "from the skill's own folder",  # MV-B5: agents work elsewhere
        "absolute paths",
    ):
        assert pin in addendum, pin
    assert '"files"' not in STANDALONE_SKILL_PROMPT


async def test_bundle_output_returns_shape_checked_files(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(sg, "_run_chunk", _fake_run(captured))
    result = await sg.generate_skill_from_overview(
        "cbcl-office-test",
        "Score leads",
        requested_name="score-leads",
        output_format="skill_bundle_v1",
    )
    assert captured["system_prompt"] is STANDALONE_SKILL_BUNDLE_PROMPT
    assert result["bundle_version"] == 1
    assert result["files"] == [
        {"path": "references/rubric.md", "content": "# Rubric\n", "usage": "read"},
        {"path": "scripts/score.py", "content": "print(1)\n", "usage": "read"},
    ]
    assert result["playbook_content"].startswith("---\n")


async def test_single_file_output_drops_model_files(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(sg, "_run_chunk", _fake_run(captured))
    result = await sg.generate_skill_from_overview(
        "cbcl-office-test", "Score leads", requested_name="score-leads"
    )
    assert captured["system_prompt"] is STANDALONE_SKILL_PROMPT
    assert "files" not in result and "bundle_version" not in result


def test_normalize_bundle_files_filters_without_cutting() -> None:
    # C4d-G1: the prompt's count is guidance; cutting the list could drop a
    # file SKILL.md links, so every well-formed entry is kept.
    many = [
        {"path": f"references/r{i}.md", "content": "x", "usage": "read"}
        for i in range(PROMPT_MAX_COMPANION_FILES + 3)
    ]
    assert len(normalize_bundle_files(many)) == PROMPT_MAX_COMPANION_FILES + 3
    assert normalize_bundle_files("nope") == []
    assert normalize_bundle_files([{"path": "notes/x.md", "content": "x"}]) == []
    assert normalize_bundle_files([{"path": "references/x.md", "content": 3}]) == []


def _harness(monkeypatch):
    sent: list[dict] = []
    seen: dict = {}

    async def _send(frame: dict) -> None:
        sent.append(frame)

    async def _fake_generator(*args, **kwargs):
        seen.update(kwargs)
        return {"name": "score-leads", "playbook_content": "# body\n"}

    monkeypatch.setattr(
        "src.office_runtime.resolve_office_container_id",
        AsyncMock(return_value="a" * 64),
    )
    monkeypatch.setattr(sg, "generate_skill_from_overview", _fake_generator)
    router = SimpleNamespace(ws_client=SimpleNamespace(send=_send))
    office = SimpleNamespace(id="office-1", workspace_path="synthetic-workspace")
    return router, office, sent, seen


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"defer_write": True, "output_format": "skill_bundle_v1"}, "skill_bundle_v1"),
        # An inline (daemon-side) write is single-file: no companion files.
        ({"output_format": "skill_bundle_v1", "create_only": True}, None),
        ({"defer_write": True, "output_format": "zip"}, None),
        ({"defer_write": True}, None),
    ],
)
async def test_handler_requests_bundles_only_for_deferred_writes(
    monkeypatch, params, expected
) -> None:
    router, office, sent, seen = _harness(monkeypatch)
    fs_handler_calls: list = []

    async def _dispatch(action, action_params):  # pragma: no cover - not reached
        fs_handler_calls.append(action)
        return {"error": "unexpected", "status": 500}

    await dispatch_backend_request(
        {
            "request_id": "r1",
            "action": "generate_skill",
            "params": {"overview": "An overview.", "name": "score-leads", **params},
        },
        router=router,
        fs_handler=SimpleNamespace(_dispatch=_dispatch),
        office=office,
        redis_client=None,
        container_name="cbcl-office-acme",
    )
    assert seen["output_format"] == expected
    assert sent
