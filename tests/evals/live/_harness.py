"""API-lane harness: production prompts, production tools, no answer leakage.

What makes an API-lane case trustworthy (each property is pinned offline by
``tests/evals/test_live_eval_no_leakage.py``):

* The system prompt is only ever the PRODUCTION render (``ProductionPrompt`` can
  only be built by the ``render_*`` functions here; there is no suffix hook).
  The first user message is exactly what a user would type, placed the way
  production places it (``history_bootstrap`` for recovered history).
* Tools are exactly the catalog the session would register
  (``mcp_tool_server.select_session_tools``) under the production-visible
  ``mcp__cubicle-tools__`` prefix — never a hand-picked subset.
* The request carries the production reasoning configuration: adaptive
  thinking plus the role's effort (Manager ``xhigh``), and NO sampling
  parameters (current Opus models reject ``temperature``/``top_p``/``top_k``).
* A small bounded decision loop answers read-only tools from a deterministic
  ``StubOffice``; the first non-read tool call (or a final text turn) is the
  recorded decision and is never executed.
* Every provider interaction is recorded for the report; provider, transport
  and truncation problems raise ``EvalHarnessError``/``EvalProviderError`` so
  they can never be mistaken for behavior failures.

The Messages API is called with stdlib ``urllib`` so the default suite gains no
dependency. Model: ``CUBICLE_EVAL_MODEL`` overrides the Manager-tier default;
the value is read at call time and recorded in every observation.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable

from tests.evals import _live_report as live_report
from tests.evals.live._stub_office import READ_ONLY_MANAGER_TOOLS, StubOffice

# EVAL-02: the Manager tier (Opus). Production runs the CLI ``opus`` alias; the
# Messages API needs a concrete model id, recorded with every observation so a
# baseline is never silently compared across models.
MANAGER_TIER_MODEL = "claude-opus-4-8"
# Cheap, non-authoritative smoke tier; supports the same request surface
# (adaptive thinking + effort up to ``xhigh``).
SMOKE_MODEL = "claude-sonnet-5"
TOOL_NAME_PREFIX = "mcp__cubicle-tools__"
API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 16000
MAX_DECISION_MODEL_CALLS = 6
SAMPLING_PARAMS = ("temperature", "top_p", "top_k")
RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 529})
RETRY_AFTER_CAP_SECONDS = 30.0
_API_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
# SHA-256 of every system text a harness render function produced. Only a
# text in this set can become a ProductionPrompt, so an altered copy (a
# suffix, ``dataclasses.replace`` with a recomputed hash) is refused.
_RENDERED_SYSTEM_HASHES: set[str] = set()


def configured_model() -> str:
    """The model an API-lane case requests (env override read at call time)."""
    return os.environ.get("CUBICLE_EVAL_MODEL") or MANAGER_TIER_MODEL


def manager_effort() -> str:
    """The Manager session's production effort (``_session_policy``)."""
    from src._session_policy import DEFAULT_OPUS_EFFORT

    return DEFAULT_OPUS_EFFORT


def eval_trials() -> int:
    raw = os.environ.get("CUBICLE_EVAL_TRIALS", "1")
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


# ── Production prompts ────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class ProductionPrompt:
    """A system prompt rendered by production code — nothing appended.

    Construct only via ``render_production_manager_prompt`` or
    ``render_generator_prompt``. Construction is refused unless the text's
    hash was produced by one of those renders, which stops an accidental
    suffix or an altered copy (including ``dataclasses.replace`` with a
    recomputed hash). It is a guard against mistakes, not a security
    boundary: the offline no-leakage tests pin every request to production.
    """

    system: str
    kind: str
    components: tuple[tuple[str, str], ...]
    system_sha256: str

    def __post_init__(self) -> None:
        if live_report.sha256_text(self.system) != self.system_sha256:
            raise TypeError("ProductionPrompt system text differs from its render")
        if self.system_sha256 not in _RENDERED_SYSTEM_HASHES:
            raise TypeError(
                "ProductionPrompt is created only by the harness render functions"
            )

    def verify(self) -> None:
        """Refuse a prompt whose text was altered after rendering."""
        if live_report.sha256_text(self.system) != self.system_sha256:
            raise TypeError("ProductionPrompt system text differs from its render")


def render_production_manager_prompt(
    context_key: str,
    context_data: dict,
    *,
    is_fresh_session: bool = False,
    office_config: dict | None = None,
) -> ProductionPrompt:
    """Render the Manager system prompt the platform ships.

    The shared office file and ``agents/manager/CLAUDE.md`` come from the real
    workspace writer (optional saved Office instructions included), followed
    by the per-turn ``build_dynamic_context`` block. ``is_fresh_session``
    defaults to False because both production call sites pass False: recovered
    chat history travels in the first USER message (``manager_user_turn``),
    not in the system prompt.
    """
    from src.config_sync.claude_md_writer import ClaudeMdWriter
    from src.config_sync.sync_service import ConfigStore
    from src.orchestrator.manager_context import build_dynamic_context

    config = {
        "office_name": context_data.get("office_name", "Test Office"),
        **(office_config or {}),
    }
    with TemporaryDirectory(prefix="cbcl-prompt-eval-") as workspace:
        writer = ClaudeMdWriter(workspace)
        writer.ensure_directory_structure()
        writer.write_office_claude_md(config)
        writer.write_manager_claude_md(config)
        office = (Path(workspace) / "CLAUDE.md").read_text()
        static = (Path(workspace) / "agents/manager/CLAUDE.md").read_text()
    dynamic = build_dynamic_context(
        context_key, context_data, ConfigStore(), is_fresh_session
    )
    parts = (("office_claude_md", office), ("manager_claude_md", static),
             ("dynamic_context", dynamic))
    system = "\n\n".join(text for _, text in parts)
    system_sha256 = live_report.sha256_text(system)
    _RENDERED_SYSTEM_HASHES.add(system_sha256)
    return ProductionPrompt(
        system=system,
        kind="manager",
        components=tuple((name, live_report.sha256_text(text)) for name, text in parts),
        system_sha256=system_sha256,
    )


def render_generator_prompt(constant_name: str) -> ProductionPrompt:
    """A generation system prompt exactly as ``src._setup_prompts`` ships it."""
    from src import _setup_prompts

    text = getattr(_setup_prompts, constant_name)
    if not isinstance(text, str):
        raise TypeError(f"{constant_name} is not a prompt constant")
    system_sha256 = live_report.sha256_text(text)
    _RENDERED_SYSTEM_HASHES.add(system_sha256)
    return ProductionPrompt(
        system=text,
        kind="generator",
        components=((constant_name, system_sha256),),
        system_sha256=system_sha256,
    )


def manager_user_turn(user_text: str, context_data: dict) -> str:
    """The first user message of a fresh Manager session, as production builds it."""
    from src.orchestrator._manager_continuity import history_bootstrap

    return history_bootstrap(user_text, context_data, fresh=True)


# ── Production tools ──────────────────────────────────────────────────


def manager_session_tools(context_key: str) -> list[dict]:
    """The Manager catalog one MCP session would register for ``context_key``."""
    from src._agent_image.mcp_tool_server import select_session_tools

    return select_session_tools("manager", "", "manager", None, context_key)


_RESOLVE_WORKSTREAM_KEY = "workstream:00000000-0000-4000-8000-00000000f068"


def resolve_allowed_tools(spec: str) -> list[dict]:
    """The exact catalog a declared ``allowed_tools`` spec names.

    ``none`` is a tool-free call; ``manager:workstream`` /
    ``manager:general_chat`` the Manager session for that context;
    ``worker:<task_mode>:<agent_name>`` a worker session (runtime lane). The
    same production selector is used, so a declaration cannot name a
    friendlier surface than the session gets.
    """
    from src._agent_image.mcp_tool_server import select_session_tools

    parts = spec.split(":")
    if spec == "none":
        return []
    if parts[0] == "manager" and len(parts) == 2 and parts[1] in ("workstream", "general_chat"):
        key = _RESOLVE_WORKSTREAM_KEY if parts[1] == "workstream" else "general_chat"
        return manager_session_tools(key)
    if parts[0] == "worker" and len(parts) == 3 and parts[1] and parts[2]:
        return select_session_tools("worker", parts[2], parts[1], None, "")
    raise ValueError(f"unresolvable allowed_tools spec {spec!r}")


def api_tool_definitions(tools: list[dict]) -> list[dict]:
    """Expose catalog tools as the model sees them in production."""
    definitions = []
    for tool in tools:
        name = TOOL_NAME_PREFIX + tool["name"]
        if not _API_TOOL_NAME.match(name):
            raise ValueError(f"tool name {name!r} is not a valid API tool name")
        definitions.append({
            "name": name,
            "description": tool["description"],
            "input_schema": tool["inputSchema"],
        })
    return definitions


def bare_tool_name(api_name: str) -> str:
    return api_name[len(TOOL_NAME_PREFIX):] if api_name.startswith(TOOL_NAME_PREFIX) else api_name


# ── Request builder ───────────────────────────────────────────────────


def build_messages_request(
    prompt: ProductionPrompt,
    user_text: str,
    tools: list[dict],
    *,
    model: str,
    effort: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    conversation: tuple[dict, ...] | list[dict] = (),
) -> dict:
    """Pure Messages API body for one decision-loop call.

    ``conversation`` holds the assistant/tool-result turns that follow the
    first user message; the first user message is always ``user_text``.
    """
    if not isinstance(prompt, ProductionPrompt):
        raise TypeError("system text must come from a ProductionPrompt render")
    prompt.verify()
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": prompt.system,
        "messages": [{"role": "user", "content": user_text}, *conversation],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
    }
    if tools:
        body["tools"] = api_tool_definitions(tools)
        body["tool_choice"] = {"type": "auto"}
    return body


# ── Sender ────────────────────────────────────────────────────────────


@dataclasses.dataclass
class ApiResponse:
    content: list[dict]
    stop_reason: str | None
    model: str | None
    usage: dict
    message_id: str | None
    request_id: str | None
    elapsed_seconds: float

    @property
    def text(self) -> str:
        return "\n".join(
            block.get("text", "") for block in self.content if block.get("type") == "text"
        )

    @property
    def tool_calls(self) -> list[dict]:
        return [block for block in self.content if block.get("type") == "tool_use"]


def _header(headers: object, name: str) -> str | None:
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    try:
        value = getter(name)
    except Exception:  # noqa: BLE001 — header objects from test doubles vary
        return None
    return value if isinstance(value, str) else None


def _retry_delay(headers: object, attempt: int) -> float:
    raw = _header(headers, "retry-after")
    try:
        if raw is not None:
            return min(max(float(raw), 0.0), RETRY_AFTER_CAP_SECONDS)
    except ValueError:
        pass
    return min(2.0 * (2 ** attempt), RETRY_AFTER_CAP_SECONDS)


def _error_body(error: urllib.error.HTTPError) -> dict:
    try:
        parsed = json.loads(error.read() or b"{}")
    except (ValueError, OSError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _observation(body: dict, *, attempts: list[dict], elapsed: float) -> live_report.CallObservation:
    system = body.get("system") if isinstance(body.get("system"), str) else ""
    tools = body.get("tools")
    return live_report.CallObservation(
        runtime=live_report.RUNTIME_MESSAGES_API,
        configured_model=body.get("model"),
        effort_requested=(body.get("output_config") or {}).get("effort"),
        thinking_requested=(body.get("thinking") or {}).get("type"),
        sampling_params_sent=[key for key in SAMPLING_PARAMS if key in body],
        max_tokens=body.get("max_tokens"),
        system_prompt_sha256=live_report.sha256_text(system),
        system_prompt_chars=len(system),
        tool_catalog_sha256=live_report.canonical_sha256(tools) if tools else None,
        tool_count=len(tools) if tools else 0,
        attempts=attempts,
        elapsed_seconds=round(elapsed, 3),
    )


def _excerpt(content: list[dict]) -> str:
    parts = []
    for block in content:
        if block.get("type") == "text":
            parts.append(block.get("text", "")[:2000])
        elif block.get("type") == "tool_use":
            parts.append(
                f"tool_use {block.get('name')}: "
                + json.dumps(block.get("input"), ensure_ascii=False)[:4000]
            )
    return "\n".join(parts)[:6000]


def send_messages_request(
    body: dict,
    *,
    api_key: str | None = None,
    timeout: float | None = None,
    max_retries: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ApiResponse:
    """POST one Messages request with bounded, classified retries (blocking)."""
    key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise live_report.EvalProviderError(
            "credentials_error", "ANTHROPIC_API_KEY is not set for the API lane"
        )
    timeout = timeout or float(os.environ.get("CUBICLE_EVAL_HTTP_TIMEOUT", "600"))
    retries = max_retries if max_retries is not None else int(
        os.environ.get("CUBICLE_EVAL_MAX_RETRIES", "2")
    )
    payload = json.dumps(body).encode("utf-8")
    attempts: list[dict] = []
    started = time.monotonic()
    for attempt in range(retries + 1):
        request = urllib.request.Request(
            API_URL,
            data=payload,
            headers={
                "x-api-key": key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            method="POST",
        )
        attempt_started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                headers = getattr(response, "headers", None)
            parsed = json.loads(raw)
        except urllib.error.HTTPError as error:
            error_body = _error_body(error)
            detail = error_body.get("error") if isinstance(error_body.get("error"), dict) else {}
            request_id = error_body.get("request_id") or _header(error.headers, "request-id")
            attempts.append({
                "status": error.code, "error_type": detail.get("type"),
                "request_id": request_id,
                "elapsed_s": round(time.monotonic() - attempt_started, 3),
            })
            if error.code in RETRYABLE_STATUSES and attempt < retries:
                sleep(_retry_delay(error.headers, attempt))
                continue
            category = live_report.classify_http_status(error.code)
            message = str(detail.get("message") or error.reason)[:500]
            observation = _observation(body, attempts=attempts,
                                       elapsed=time.monotonic() - started)
            observation.error = {"category": category, "status": error.code,
                                 "error_type": detail.get("type"),
                                 "message": message, "request_id": request_id}
            live_report.record_call(observation)
            raise live_report.EvalProviderError(
                category, f"HTTP {error.code}: {message}", status=error.code,
                error_type=detail.get("type"), request_id=request_id,
                attempts=attempts,
            ) from error
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
            attempts.append({
                "status": None, "error_type": type(error).__name__, "request_id": None,
                "elapsed_s": round(time.monotonic() - attempt_started, 3),
            })
            if attempt < retries:
                sleep(_retry_delay(None, attempt))
                continue
            observation = _observation(body, attempts=attempts,
                                       elapsed=time.monotonic() - started)
            observation.error = {"category": "transport_error", "status": None,
                                 "error_type": type(error).__name__,
                                 "message": str(error)[:500], "request_id": None}
            live_report.record_call(observation)
            raise live_report.EvalProviderError(
                "transport_error", f"{type(error).__name__}: {error}"[:500],
                attempts=attempts,
            ) from error
        except ValueError as error:  # undecodable 200 body
            observation = _observation(body, attempts=attempts,
                                       elapsed=time.monotonic() - started)
            observation.error = {"category": "harness_error", "message": str(error)[:500]}
            live_report.record_call(observation)
            raise live_report.EvalHarnessError(
                "harness_error", f"undecodable provider response: {error}"
            ) from error
        request_id = parsed.get("request_id") or _header(headers, "request-id")
        attempts.append({"status": 200, "error_type": None, "request_id": request_id,
                         "elapsed_s": round(time.monotonic() - attempt_started, 3)})
        content = parsed.get("content") if isinstance(parsed.get("content"), list) else []
        result = ApiResponse(
            content=content,
            stop_reason=parsed.get("stop_reason"),
            model=parsed.get("model"),
            usage=parsed.get("usage") if isinstance(parsed.get("usage"), dict) else {},
            message_id=parsed.get("id"),
            request_id=request_id,
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        observation = _observation(body, attempts=attempts, elapsed=result.elapsed_seconds)
        observation.observed_model = result.model
        observation.stop_reason = result.stop_reason
        observation.usage = live_report.usage_from_provider(result.usage)
        observation.message_id = result.message_id
        observation.request_id = request_id
        observation.completed = True
        observation.response_excerpt = _excerpt(content)
        live_report.record_call(observation)
        return result
    raise AssertionError("unreachable")  # pragma: no cover


def _raise_for_incomplete(response: ApiResponse) -> None:
    if response.stop_reason == "max_tokens":
        raise live_report.EvalHarnessError(
            "output_truncated", "response hit max_tokens before a decision"
        )
    if response.stop_reason == "refusal":
        raise live_report.EvalHarnessError("refusal", "the model declined the request")


# ── Decision loop ─────────────────────────────────────────────────────


@dataclasses.dataclass
class Decision:
    """The first non-read action (never executed) or the final text turn."""

    kind: str  # "tool_call" | "final_text"
    tool_name: str | None = None
    tool_input: dict | None = None
    text: str = ""
    read_calls: list[tuple[str, dict]] = dataclasses.field(default_factory=list)
    model_calls: int = 0
    # Every other tool the deciding response called, in order (reads the
    # stub had not answered included): ``[create_task, delete_task]`` in one
    # turn is a create_task decision that also attempted delete_task.
    other_tool_names: list[str] = dataclasses.field(default_factory=list)

    @property
    def tool_names(self) -> list[str]:
        """The decision tool followed by every other tool in its response."""
        if self.kind != "tool_call":
            return []
        return [str(self.tool_name), *self.other_tool_names]

    def summary(self) -> str:
        if self.kind == "tool_call":
            return f"{self.tool_name}({json.dumps(self.tool_input, ensure_ascii=False)[:1500]})"
        return f"final text: {self.text[:1500]!r}"


Sender = Callable[[dict], ApiResponse]


async def run_decision_loop(
    prompt: ProductionPrompt,
    user_text: str,
    tools: list[dict],
    office: StubOffice,
    *,
    effort: str,
    model: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_model_calls: int = MAX_DECISION_MODEL_CALLS,
    send: Sender | None = None,
) -> Decision:
    """Let the model read (stub answers) until it decides; never execute."""
    model = model or configured_model()
    send = send or send_messages_request
    offered = {tool["name"] for tool in tools}
    conversation: list[dict] = []
    read_calls: list[tuple[str, dict]] = []
    for call_number in range(1, max_model_calls + 1):
        body = build_messages_request(
            prompt, user_text, tools, model=model, effort=effort,
            max_tokens=max_tokens, conversation=conversation,
        )
        response = await asyncio.to_thread(send, body)
        _raise_for_incomplete(response)
        tool_uses = response.tool_calls
        if not tool_uses:
            return Decision("final_text", text=response.text,
                            read_calls=read_calls, model_calls=call_number)
        results = []
        for position, block in enumerate(tool_uses):
            name = bare_tool_name(block.get("name", ""))
            arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
            if name not in READ_ONLY_MANAGER_TOOLS or name not in offered:
                others = [bare_tool_name(other.get("name", ""))
                          for other in tool_uses[position + 1:]]
                return Decision("tool_call", tool_name=name, tool_input=arguments,
                                text=response.text, read_calls=read_calls,
                                model_calls=call_number, other_tool_names=others)
            answer = office.dispatch(name, arguments)
            read_calls.append((name, arguments))
            results.append({
                "type": "tool_result",
                "tool_use_id": block.get("id"),
                "content": json.dumps(answer, ensure_ascii=False),
            })
        # Echo the assistant turn unchanged (thinking blocks included) and
        # return every read result in ONE user message.
        conversation.append({"role": "assistant", "content": response.content})
        conversation.append({"role": "user", "content": results})
    raise AssertionError(
        f"no decision after {max_model_calls} model calls; reads: "
        f"{[name for name, _ in read_calls]}"
    )


async def run_single_turn(
    prompt: ProductionPrompt,
    user_text: str,
    *,
    effort: str,
    model: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    send: Sender | None = None,
) -> ApiResponse:
    """One tool-free call (generation prompts)."""
    body = build_messages_request(
        prompt, user_text, [], model=model or configured_model(), effort=effort,
        max_tokens=max_tokens,
    )
    response = await asyncio.to_thread(send or send_messages_request, body)
    _raise_for_incomplete(response)
    return response


def parse_json_object(text: str) -> dict:
    """Parse a generator's JSON reply (fenced or bare)."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("generator reply is not a JSON object")
    return value


# ── Case helpers ──────────────────────────────────────────────────────


async def decide_as_manager(
    office: StubOffice,
    user_text: str,
    *,
    send: Sender | None = None,
    context_overrides: dict | None = None,
) -> Decision:
    """One Manager turn exactly as production composes it; returns the decision.

    System text = the production render for the office context; user text =
    what the user typed (placed by ``history_bootstrap``); tools = the
    production-selected catalog for the context. Nothing is appended.
    """
    context = office.context_data(**(context_overrides or {}))
    prompt = render_production_manager_prompt(office.context_key, context)
    tools = manager_session_tools(office.context_key)
    decision = await run_decision_loop(
        prompt,
        manager_user_turn(user_text, context),
        tools,
        office,
        effort=manager_effort(),
        send=send,
    )
    live_report.record_detail("decision", decision.summary())
    live_report.record_detail(
        "decision_tool", decision.tool_name if decision.kind == "tool_call" else None,
    )
    live_report.record_detail("decision_tools", decision.tool_names)
    live_report.record_detail("reads", [name for name, _ in decision.read_calls])
    return decision


def generator_effort(constant_name: str) -> str:
    """The effort production passes for one generation prompt."""
    from src import _setup_cli

    if constant_name == "WORKSTREAM_CONTEXT_PROMPT":
        return _setup_cli._SYNC_GENERATION_EFFORT or "high"
    return _setup_cli._DEFAULT_GENERATION_EFFORT or "medium"


async def generate_as_production(
    constant_name: str, user_text: str, *, send: Sender | None = None
) -> dict:
    """One tool-free generation call with the shipped prompt and effort."""
    response = await run_single_turn(
        render_generator_prompt(constant_name),
        user_text,
        effort=generator_effort(constant_name),
        send=send,
    )
    return parse_json_object(response.text)
