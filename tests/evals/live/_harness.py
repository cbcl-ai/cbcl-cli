"""Tiny helper for live prompt-regression evals (P6.1).

Calls the Anthropic HTTP API directly via stdlib ``urllib`` — no
``anthropic`` SDK dependency so the rest of the test suite doesn't
pull it in. The blocking ``urllib.request`` call runs in a thread
via ``asyncio.to_thread`` so pytest-asyncio's event loop isn't
frozen while the API request is in flight.

Model pinning: ``DEFAULT_MODEL`` defaults to a date-pinned snapshot
(not an alias). Anthropic re-routes aliases at will, so without a
date-pinned id an eval baseline silently drifts. Override via
``CUBICLE_EVAL_MODEL`` env var when you want to re-baseline.
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from dataclasses import dataclass

# EVAL-02: pin the live evals to the platform's MANAGER TIER (Opus), not an
# off-tier Sonnet — a brief-quality / routing regression only shows up on the
# model family that actually ships. ``SMOKE_MODEL`` (cheap Sonnet) stays
# available for a fast, non-authoritative smoke run via CUBICLE_EVAL_MODEL.
# Date-pinned so a vendor-side alias re-route doesn't silently flip a baseline.
MANAGER_TIER_MODEL = "claude-opus-4-8"       # tracks backend default_manager_model() == "opus"
SMOKE_MODEL = "claude-sonnet-4-5-20250929"   # cheap smoke tier only
DEFAULT_MODEL = os.environ.get("CUBICLE_EVAL_MODEL", MANAGER_TIER_MODEL)


def render_production_manager_prompt(
    context_key: str,
    context_data: dict,
    *,
    is_fresh_session: bool = True,
    eval_json_suffix: str | None = None,
) -> str:
    """Render the REAL Manager system prompt the platform ships.

    EVAL-02: the live evals previously sent a 10-line hand-written stub, so NO
    change to ``MANAGER_CLAUDE_MD`` or ``build_dynamic_context`` could affect
    their outcome — a placebo by construction. This composes the production
    artifact exactly as the daemon does: the static ``MANAGER_CLAUDE_MD``
    (auto-discovered from ``/workspace/CLAUDE.md``, allowlist filled) followed
    by the per-turn ``build_dynamic_context`` block (passed via
    ``--system-prompt-file``). A fresh ``ConfigStore`` is fine because
    ``build_dynamic_context`` prefers the ``team_roster`` / ``workstream_list``
    values carried in ``context_data`` (the same path the backend feeds).

    ``eval_json_suffix``: the live harness uses the plain /v1/messages API (no
    tools), so the model can't emit a real ``create_task`` tool call. The
    suffix asks it to render the payload it WOULD pass to ``create_task`` as
    JSON — exercising the production prompt's brief-authoring rules without a
    tool-use round-trip.
    """
    from src.config_sync._tool_allowlist import render_manager_allowlist
    from src.config_sync.claude_md_content import MANAGER_CLAUDE_MD
    from src.config_sync.sync_service import ConfigStore
    from src.orchestrator.manager_context import build_dynamic_context

    static = (
        MANAGER_CLAUDE_MD.replace(
            "{manager_tool_allowlist}", render_manager_allowlist()
        )
        .replace("{office_name}", context_data.get("office_name", "Test Office"))
        .replace("{office_specs_index}", "")
    )
    dynamic = build_dynamic_context(
        context_key, context_data, ConfigStore(), is_fresh_session
    )
    parts = [static, dynamic]
    if eval_json_suffix:
        parts.append(eval_json_suffix)
    return "\n\n".join(parts)


@dataclass
class EvalResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int


def _sync_call(
    api_key: str,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    temperature: float,
) -> EvalResponse:
    """Blocking inner — called from a worker thread, never the loop."""
    payload = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read())
    content = body.get("content", [])
    text_parts = [
        block.get("text", "") for block in content
        if block.get("type") == "text"
    ]
    usage = body.get("usage", {})
    return EvalResponse(
        text="\n".join(text_parts),
        model=body.get("model", model),
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
    )


async def call_claude(
    *,
    system: str,
    user: str,
    max_tokens: int = 1024,
    temperature: float = 0.0,
    model: str | None = None,
) -> EvalResponse:
    """Call the Anthropic /v1/messages API without blocking the event loop.

    Raises if the call fails. Tests call from inside a `@pytest.mark.live_eval`
    function and the conftest skips them when the key is absent, so we
    don't need to handle the no-key case here.

    P6.1 review fix: the previous synchronous version froze the
    asyncio loop for up to 60s per request because pytest-asyncio's
    ``asyncio_mode = "auto"`` wraps every test in the loop. Run the
    blocking urllib call in a worker thread.
    """
    api_key = os.environ["ANTHROPIC_API_KEY"]
    model = model or DEFAULT_MODEL
    return await asyncio.to_thread(
        _sync_call, api_key, model, system, user, max_tokens, temperature,
    )
