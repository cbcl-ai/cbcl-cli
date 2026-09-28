"""Generation failures reach the user consistently (C4d-G7).

Curated, actionable hints are ``GenerationError`` and shown verbatim on
every surface; any other exception (which can carry raw CLI stderr,
workspace paths or token prefixes) collapses to a generic message, on the
synchronous generate RPCs and on the asynchronous setup wizard alike.
"""

from __future__ import annotations

import pytest

import src.setup_generator as sg
from src._handlers._requests import _safe_generation_error
from src._setup_cli import _empty_cli_output_error
from src._setup_json import GenerationError, user_safe_generation_message


def test_helper_forwards_only_curated_messages() -> None:
    assert user_safe_generation_message(GenerationError("retry"), "generic") == "retry"
    raw = RuntimeError("Claude CLI failed (rc=1): /home/agent/.claude token=abc")
    assert user_safe_generation_message(raw, "generic") == "generic"
    assert _safe_generation_error(raw, "generic") == "generic"


async def test_empty_skill_md_hint_reaches_the_sync_caller(monkeypatch) -> None:
    async def fake_run_chunk(container, system_prompt, user_prompt, **kwargs):
        return {"name": "x", "display_name": "X", "body": "   "}

    monkeypatch.setattr(sg, "_run_chunk", fake_run_chunk)
    with pytest.raises(GenerationError) as caught:
        await sg.generate_skill_from_overview("cbcl-office-test", "An overview.")
    message = _safe_generation_error(caught.value, "Skill generation failed.")
    assert "empty SKILL.md" in message


@pytest.mark.parametrize(
    ("raw_response", "expected"),
    [
        ({"agents": "nope"}, "Improving the office setup failed"),
        ("not-an-object", "Improve returned a non-object response"),
    ],
)
async def test_wizard_failure_publishes_only_safe_text(
    monkeypatch, raw_response, expected
) -> None:
    async def fake_run_chunk(container, system_prompt, user_prompt, **kwargs):
        if raw_response == {"agents": "nope"}:
            raise RuntimeError("Claude CLI failed (rc=1): secret stderr /home/agent")
        return raw_response

    monkeypatch.setattr(sg, "_run_chunk", fake_run_chunk)

    class Router:
        def __init__(self) -> None:
            self.events: list[dict] = []

        async def publish_event(self, event: dict) -> None:
            self.events.append(event)

    router = Router()
    await sg.improve_office_config(
        router,
        "req-1",
        "Office",
        {"instructions": "# O", "agents": [], "skills": []},
        "directive",
        "cbcl-office-test",
    )
    failed = [e for e in router.events if e["type"] == "setup_generation_failed"]
    assert len(failed) == 1
    assert failed[0]["error"].startswith(expected)
    assert "secret stderr" not in failed[0]["error"]


def test_empty_output_guidance_keeps_stderr_in_the_log(caplog) -> None:
    error = _empty_cli_output_error(model="opus", stderr="token=abc123")
    assert isinstance(error, GenerationError)
    assert "token=abc123" not in str(error)
    assert "cbcl auth" in str(error)


# R14: admission refusals (usage limit, office maintenance) carry fixed,
# user-safe text. A retry cannot succeed until capacity is verified or
# maintenance ends, so the setup user must see the real reason instead of
# "check the logs and retry".

_QUOTA_TEXT = (
    "Claude usage limit reached. AI work will resume after capacity is verified."
)
_MAINTENANCE_TEXT = "Office maintenance pauses new worker and script execution"


def _admission_errors() -> list[BaseException]:
    from src.runtime_state import AdmissionPaused, QuotaPaused

    return [QuotaPaused(_QUOTA_TEXT), AdmissionPaused(_MAINTENANCE_TEXT)]


def test_helper_forwards_admission_refusals() -> None:
    for exc in _admission_errors():
        assert user_safe_generation_message(exc, "generic") == str(exc)
        assert _safe_generation_error(exc, "generic") == str(exc)


class _Router:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish_event(self, event: dict) -> None:
        self.events.append(event)


def _failed_error(router: _Router) -> str:
    failed = [e for e in router.events if e["type"] == "setup_generation_failed"]
    assert len(failed) == 1
    return failed[0]["error"]


@pytest.mark.parametrize("index", [0, 1])
async def test_wizard_generate_publishes_the_admission_reason(monkeypatch, index) -> None:
    exc = _admission_errors()[index]

    async def fake_run_chunk(container, system_prompt, user_prompt, **kwargs):
        raise exc

    async def no_sources(*args, **kwargs):
        return False

    monkeypatch.setattr(sg, "_run_chunk", fake_run_chunk)
    monkeypatch.setattr(sg, "_container_has_source_files", no_sources)
    router = _Router()
    await sg.generate_office_config(
        router=router,
        request_id="req-1",
        office_name="Office",
        office_description="We quote jobs.",
        requirements={},
        skill_catalog=[],
        container_name="cbcl-office-test",
    )
    assert _failed_error(router) == str(exc)


@pytest.mark.parametrize("index", [0, 1])
async def test_wizard_improve_publishes_the_admission_reason(monkeypatch, index) -> None:
    exc = _admission_errors()[index]

    async def fake_run_chunk(container, system_prompt, user_prompt, **kwargs):
        raise exc

    monkeypatch.setattr(sg, "_run_chunk", fake_run_chunk)
    router = _Router()
    await sg.improve_office_config(
        router,
        "req-1",
        "Office",
        {"instructions": "# O", "agents": [], "skills": []},
        "directive",
        "cbcl-office-test",
    )
    assert _failed_error(router) == str(exc)
