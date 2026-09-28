"""Truthful reporting for the opt-in behavioral eval lanes (stdlib only).

Both lanes (``live/`` — Messages API decisions, ``runtime/`` — the Claude CLI
inside the cbcl agent image) record what actually happened per case and write
``live-eval-report.json`` + ``live-eval-summary.md``. The rules that make the
report trustworthy:

* A run that produced **zero behavioral verdicts** is ``not_evaluated`` — never
  green by omission. With ``CUBICLE_LIVE_EVAL_REQUIRE_EXECUTION=1`` the pytest
  exit code becomes ``NOT_EVALUATED_EXIT`` (10) for such a run.
* Provider, transport and harness failures are separated from behavior
  failures. A truncated or refused response is an error, not a failed verdict.
* Unknown values are recorded as ``null``; nothing unknown becomes ``0``.
* A case that "passed" without any recorded model/runtime observation cannot
  prove it executed, so it is classified ``harness_error``.
* Credentials are never written — only a boolean ``*_present`` flag. Every
  report and runtime trace file passes through ``redact_secrets`` first, so a
  credential value that reached a tool result (``env`` in a Bash call) or an
  error detail is replaced by ``[REDACTED]``.

CLI (run from ``communicator/``)::

    python -m tests.evals._live_report ensure eval-reports/live-eval-report.json \
        --reason "pytest did not write a report"
    python -m tests.evals._live_report compare BASE.json CANDIDATE.json \
        [--markdown OUT.md]
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import datetime as _dt
import hashlib
import io
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import tokenize
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
REPORT_TYPE = "cubicle.live_eval"
NOT_EVALUATED_EXIT = 10
REPORT_JSON = "live-eval-report.json"
REPORT_MD = "live-eval-summary.md"
ENV_REPORT_DIR = "CUBICLE_LIVE_EVAL_REPORT_DIR"
ENV_REQUIRE_EXECUTION = "CUBICLE_LIVE_EVAL_REQUIRE_EXECUTION"

# Skip reasons of the form ``not_evaluated:<code>: <detail>`` mark a case that
# could not run for an environmental reason (no key, no docker, stale image).
NOT_EVALUATED_PREFIX = "not_evaluated:"
MISSING_CREDENTIALS = "missing_credentials"

CREDENTIAL_ENV = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
REDACTED = "[REDACTED]"
# Shorter values are placeholders, not credentials (real keys and the stub's
# per-run tokens are far longer); redacting them would rewrite ordinary words
# such as report field names.
MIN_REDACTED_LENGTH = 8

RUNTIME_MESSAGES_API = "anthropic_messages_api"
RUNTIME_AGENT_IMAGE_CLI = "claude_cli_in_cbcl_agent_image"

# Classifications that are behavioral VERDICTS (the only ones that count as
# "evaluated"). Everything else is an error, a skip or not_evaluated.
VERDICT_CLASSIFICATIONS = frozenset({"passed", "behavior_failed"})
ERROR_CLASSIFICATIONS = frozenset({
    "output_truncated",
    "refusal",
    "provider_unavailable",
    "transport_error",
    "harness_config_error",
    "credentials_error",
    "runtime_infrastructure_error",
    "harness_error",
    "setup_error",
})

# Combinations the product supports that a report must never imply it covered.
SUPPORTED_COMBINATIONS: tuple[tuple[str, str], ...] = (
    ("api", "manager"),
    ("api", "generator"),
    ("runtime", "worker_executor"),
    ("runtime", "worker_reviewer"),
    ("runtime", "worker_triage"),
    ("runtime", "manager"),
    ("runtime", "planner"),
    ("runtime", "manager_assistant"),
)

_LIMITS = (
    "Results are per-trial samples (n = trials per case); a single pass is "
    "evidence, not a guarantee.",
    "API lane: Messages API decision loop with the production-rendered Manager "
    "prompt and production-selected tool catalog. Production runs the Claude "
    "CLI, which delivers CLAUDE.md files as memory context rather than as one "
    "system prompt; read-only tools are answered by a deterministic stub and "
    "no decision is executed.",
    "Runtime lane: real Claude CLI in the cbcl agent image against a stub "
    "backend and a synthetic workspace; it does not prove behavior with real "
    "office data, connectors or the hosted backend.",
    "Cost is recorded only when the runtime reports it; no price table is "
    "applied to API-lane token counts.",
)


def redact_secrets(
    text: str, extra: Iterable[str] = (), environ: dict | None = None,
) -> str:
    """Replace every credential value (and any ``extra`` secret) in ``text``.

    Both the raw value and its JSON-escaped spelling are replaced, longest
    first, so serialized traces and reports are covered. Values shorter than
    ``MIN_REDACTED_LENGTH`` are placeholders and are left alone.
    """
    environ = os.environ if environ is None else environ
    values = {environ.get(name) or "" for name in CREDENTIAL_ENV}
    values.update(str(value) for value in extra if value)
    spellings: set[str] = set()
    for value in values:
        if len(value) >= MIN_REDACTED_LENGTH:
            spellings.add(value)
            spellings.add(json.dumps(value)[1:-1])
    for spelling in sorted(spellings, key=len, reverse=True):
        if spelling:
            text = text.replace(spelling, REDACTED)
    return text


def not_evaluated_reason(code: str, detail: str) -> str:
    """Build a pytest skip reason the report classifies as ``not_evaluated``."""
    return f"{NOT_EVALUATED_PREFIX}{code}: {detail}"


def parse_not_evaluated(reason: str | None) -> tuple[str, str] | None:
    if not reason:
        return None
    text = reason
    # pytest renders skip reasons as "Skipped: <reason>" in some paths.
    if text.startswith("Skipped: "):
        text = text[len("Skipped: "):]
    if not text.startswith(NOT_EVALUATED_PREFIX):
        return None
    code, _, detail = text[len(NOT_EVALUATED_PREFIX):].partition(":")
    return code.strip() or "unspecified", detail.strip()


class EvalHarnessError(Exception):
    """An error that prevents a behavioral verdict (never a behavior failure).

    ``category`` is one of ``ERROR_CLASSIFICATIONS``.
    """

    def __init__(self, category: str, message: str, **details: Any) -> None:
        if category not in ERROR_CLASSIFICATIONS:
            raise ValueError(f"unknown eval error category {category!r}")
        super().__init__(message)
        self.category = category
        self.message = message
        self.details = details


class EvalProviderError(EvalHarnessError):
    """The model provider or transport failed; carries the provider identity."""

    def __init__(
        self,
        category: str,
        message: str,
        *,
        status: int | None = None,
        error_type: str | None = None,
        request_id: str | None = None,
        attempts: list[dict] | None = None,
    ) -> None:
        super().__init__(
            category,
            message,
            status=status,
            error_type=error_type,
            request_id=request_id,
        )
        self.status = status
        self.error_type = error_type
        self.request_id = request_id
        self.attempts = list(attempts or [])


def classify_http_status(status: int | None) -> str:
    """Map a provider HTTP status to an error category."""
    if status in (401, 402, 403):
        return "credentials_error"
    if status in (408, 429, 500, 502, 503, 504, 529):
        return "provider_unavailable"
    if status is not None and 400 <= status < 500:
        return "harness_config_error"
    return "provider_unavailable" if status else "transport_error"


# ── Observations ──────────────────────────────────────────────────────


@dataclasses.dataclass
class CallObservation:
    """One provider/runtime interaction. ``None`` always means unknown."""

    runtime: str
    configured_model: str | None = None
    observed_model: str | None = None
    effort_requested: str | None = None
    thinking_requested: str | None = None
    sampling_params_sent: list[str] = dataclasses.field(default_factory=list)
    max_tokens: int | None = None
    system_prompt_sha256: str | None = None
    system_prompt_chars: int | None = None
    tool_catalog_sha256: str | None = None
    tool_count: int | None = None
    stop_reason: str | None = None
    usage: dict[str, int | float | None] = dataclasses.field(default_factory=dict)
    cost_usd: float | None = None
    message_id: str | None = None
    request_id: str | None = None
    attempts: list[dict] = dataclasses.field(default_factory=list)
    elapsed_seconds: float | None = None
    completed: bool = False
    error: dict | None = None
    response_excerpt: str | None = None
    extra: dict = dataclasses.field(default_factory=dict)

    @property
    def model_mismatch(self) -> bool | None:
        if not self.configured_model or not self.observed_model:
            return None
        return not self.observed_model.startswith(self.configured_model)

    def to_dict(self) -> dict:
        data = dataclasses.asdict(self)
        data["model_mismatch"] = self.model_mismatch
        return data


USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def usage_from_provider(raw: object) -> dict[str, int | float | None]:
    """Keep provider usage names; an absent key is ``None``, never 0."""
    usage = raw if isinstance(raw, dict) else {}
    return {key: usage.get(key) for key in USAGE_KEYS}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_sha256(value: object) -> str:
    return sha256_text(json.dumps(value, sort_keys=True, separators=(",", ":")))


# ── Active-case registry ──────────────────────────────────────────────
# The live lanes run sequentially (no xdist), so one module-level registry is
# safe. ``record_*`` are no-ops outside a case so unit tests that call the
# harness directly are unaffected.

_ACTIVE: dict[str, Any] = {"nodeid": None, "calls": {}, "details": {}}


def begin_case(nodeid: str) -> None:
    _ACTIVE["nodeid"] = nodeid
    _ACTIVE["calls"].setdefault(nodeid, [])
    _ACTIVE["details"].setdefault(nodeid, {})


def end_case() -> None:
    _ACTIVE["nodeid"] = None


def active_case() -> str | None:
    return _ACTIVE["nodeid"]


def record_call(observation: CallObservation | dict) -> None:
    nodeid = _ACTIVE["nodeid"]
    if nodeid is None:
        return
    data = observation.to_dict() if isinstance(observation, CallObservation) else dict(observation)
    _ACTIVE["calls"][nodeid].append(data)


def record_detail(key: str, value: Any) -> None:
    nodeid = _ACTIVE["nodeid"]
    if nodeid is None:
        return
    _ACTIVE["details"][nodeid][key] = value


def observations_for(nodeid: str) -> list[dict]:
    return list(_ACTIVE["calls"].get(nodeid, []))


def details_for(nodeid: str) -> dict:
    return dict(_ACTIVE["details"].get(nodeid, {}))


def reset_registry() -> None:
    _ACTIVE["nodeid"] = None
    _ACTIVE["calls"] = {}
    _ACTIVE["details"] = {}


# ── Classification ────────────────────────────────────────────────────

_OUTPUT_CONTRACT_EXCEPTIONS = frozenset({
    "JSONDecodeError", "KeyError", "TypeError", "ValueError", "IndexError",
})


def classify_case(
    *,
    outcome: str,
    phase: str = "call",
    exc_type_name: str | None = None,
    exc_message: str | None = None,
    exc_category: str | None = None,
    observations: Iterable[dict] = (),
    skip_reason: str | None = None,
) -> dict:
    """Classify one case outcome. Returns ``{"classification", "subtype", ...}``."""
    observations = list(observations)
    completed = [obs for obs in observations if obs.get("completed")]
    if outcome == "skipped":
        parsed = parse_not_evaluated(skip_reason)
        if parsed:
            return {"classification": "not_evaluated", "subtype": parsed[0],
                    "detail": parsed[1]}
        return {"classification": "skipped", "subtype": None,
                "detail": skip_reason}
    if outcome == "passed":
        if phase != "call":
            return {"classification": "passed", "subtype": None, "detail": None}
        if not observations:
            return {
                "classification": "harness_error",
                "subtype": "no_observation",
                "detail": "The case passed without recording any model or "
                "runtime observation, so execution cannot be shown.",
            }
        if not completed:
            return {"classification": "harness_error", "subtype": "no_completed_call",
                    "detail": "No observation completed successfully."}
        return {"classification": "passed", "subtype": None, "detail": None}
    # failed
    if phase != "call":
        return {"classification": "setup_error", "subtype": phase,
                "detail": exc_message}
    if exc_category in ERROR_CLASSIFICATIONS:
        return {"classification": exc_category, "subtype": exc_type_name,
                "detail": exc_message}
    if exc_type_name == "Failed" and exc_message and "Timeout" in exc_message:
        return {"classification": "transport_error", "subtype": "pytest_timeout",
                "detail": exc_message}
    if completed:
        last_stop = completed[-1].get("stop_reason")
        if last_stop == "max_tokens":
            return {"classification": "output_truncated", "subtype": "max_tokens",
                    "detail": exc_message}
        if last_stop == "refusal":
            return {"classification": "refusal", "subtype": None,
                    "detail": exc_message}
        subtype = (
            "output_contract"
            if exc_type_name in _OUTPUT_CONTRACT_EXCEPTIONS
            else "assertion"
        )
        return {"classification": "behavior_failed", "subtype": subtype,
                "detail": exc_message}
    return {"classification": "harness_error", "subtype": exc_type_name,
            "detail": exc_message}


def _case_status(classification: str) -> str:
    if classification == "passed":
        return "passed"
    if classification == "behavior_failed":
        return "failed"
    if classification == "not_evaluated":
        return "not_evaluated"
    if classification == "skipped":
        return "skipped"
    return "error"


def aggregate(cases: list[dict], collection_errors: list[dict]) -> dict:
    counts = {
        "selected": len(cases),
        "evaluated": 0,
        "passed": 0,
        "behavior_failed": 0,
        "errors": 0,
        "not_evaluated": 0,
        "skipped": 0,
        "collection_errors": len(collection_errors),
    }
    reasons: set[str] = set()
    error_kinds: set[str] = set()
    for case in cases:
        classification = case.get("classification")
        if classification in VERDICT_CLASSIFICATIONS:
            counts["evaluated"] += 1
            counts["passed" if classification == "passed" else "behavior_failed"] += 1
        elif classification == "not_evaluated":
            counts["not_evaluated"] += 1
            reasons.add(case.get("subtype") or "unspecified")
        elif classification == "skipped":
            counts["skipped"] += 1
        else:
            counts["errors"] += 1
            error_kinds.add(classification or "unknown")
    if counts["evaluated"] == 0:
        status = "not_evaluated"
        if collection_errors:
            status_reason = "collection_error"
        elif not cases:
            status_reason = "no_cases_selected"
        elif counts["not_evaluated"] == len(cases) and len(reasons) == 1:
            status_reason = next(iter(reasons))
        elif counts["errors"] == len(cases) and len(error_kinds) == 1:
            status_reason = f"all_{next(iter(error_kinds))}"
        else:
            status_reason = "mixed"
    elif counts["behavior_failed"]:
        status, status_reason = "failed", "behavior_failures"
    elif counts["evaluated"] < counts["selected"] or collection_errors:
        status, status_reason = "incomplete", "some_cases_not_evaluated"
    else:
        status, status_reason = "passed", "all_cases_passed"
    return {"counts": counts, "status": status, "status_reason": status_reason}


# ── Report assembly ───────────────────────────────────────────────────


def _git_sha() -> str | None:
    sha = os.environ.get("CI_COMMIT_SHA")
    if sha:
        return sha
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _package_version() -> str | None:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - stdlib always has it on 3.12
        return None
    try:
        return version("cubicle-communicator")
    except PackageNotFoundError:
        return None


def environment_snapshot() -> dict:
    """Run identity. Credentials are reduced to presence booleans."""
    try:
        import pytest  # local import keeps the CLI usable without pytest

        pytest_version = pytest.__version__
    except ImportError:  # pragma: no cover
        pytest_version = None
    return {
        "git_sha": _git_sha(),
        "pipeline_source": os.environ.get("CI_PIPELINE_SOURCE"),
        "pipeline_id": os.environ.get("CI_PIPELINE_ID"),
        "job_url": os.environ.get("CI_JOB_URL"),
        "communicator_version": _package_version(),
        "python": platform.python_version(),
        "pytest": pytest_version,
        "api_key_present": bool(os.environ.get(CREDENTIAL_ENV[0])),
        "oauth_token_present": bool(os.environ.get(CREDENTIAL_ENV[1])),
        "configured_model_override": os.environ.get("CUBICLE_EVAL_MODEL"),
        "trials": os.environ.get("CUBICLE_EVAL_TRIALS"),
        "require_execution": os.environ.get(ENV_REQUIRE_EXECUTION) == "1",
    }


def _untested(cases: list[dict]) -> list[str]:
    exercised = {
        (case.get("lane"), case.get("role"))
        for case in cases
        if case.get("classification") in VERDICT_CLASSIFICATIONS
    }
    return [f"{lane}:{role}" for lane, role in SUPPORTED_COMBINATIONS
            if (lane, role) not in exercised]


def build_report(
    cases: list[dict],
    collection_errors: list[dict],
    *,
    started_at: str,
    finished_at: str,
    environment: dict | None = None,
) -> dict:
    summary = aggregate(cases, collection_errors)
    for case in cases:
        case["status"] = _case_status(case.get("classification", ""))
    return {
        "schema_version": SCHEMA_VERSION,
        "report_type": REPORT_TYPE,
        "status": summary["status"],
        "status_reason": summary["status_reason"],
        "started_at": started_at,
        "finished_at": finished_at,
        "environment": environment if environment is not None else environment_snapshot(),
        "counts": summary["counts"],
        "cases": cases,
        "collection_errors": collection_errors,
        "untested_supported_combinations": _untested(cases),
        "limits": list(_LIMITS),
    }


def not_evaluated_stub(reason: str) -> dict:
    now = utc_now()
    report = build_report([], [], started_at=now, finished_at=now)
    report["status_reason"] = "report_missing"
    report["note"] = reason
    return report


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


# ── Rendering ─────────────────────────────────────────────────────────


def _fmt(value: object) -> str:
    return "unknown" if value is None else str(value)


def _case_tokens(case: dict) -> tuple[object, object, object]:
    totals: dict[str, int | None] = {"input_tokens": 0, "output_tokens": 0,
                                     "cache_read_input_tokens": 0}
    calls = case.get("calls") or []
    if not calls:
        return None, None, None
    for key in totals:
        values = [(call.get("usage") or {}).get(key) for call in calls]
        if any(value is None for value in values):
            totals[key] = None
        else:
            totals[key] = sum(values)
    return (totals["input_tokens"], totals["output_tokens"],
            totals["cache_read_input_tokens"])


def render_markdown(report: dict) -> str:
    counts = report.get("counts", {})
    status = report.get("status")
    reason = report.get("status_reason")
    if status == "not_evaluated":
        headline = f"NOT EVALUATED - 0 behavioral verdicts ({reason})"
    elif status == "failed":
        headline = (f"FAILED - {counts.get('behavior_failed', 0)} behavior "
                    f"failure(s) in {counts.get('evaluated', 0)} verdict(s)")
    elif status == "incomplete":
        headline = (f"INCOMPLETE - {counts.get('evaluated', 0)} of "
                    f"{counts.get('selected', 0)} selected case(s) produced a verdict")
    else:
        headline = f"PASSED - {counts.get('passed', 0)} verdict(s), all passed"
    lines = [f"# Live eval report: {headline}", ""]
    if report.get("note"):
        lines += [f"Note: {report['note']}", ""]
    lines += ["| selected | evaluated | passed | behavior failed | errors | "
              "not evaluated | skipped | collection errors |",
              "|---|---|---|---|---|---|---|---|",
              "| {selected} | {evaluated} | {passed} | {behavior_failed} | {errors} | "
              "{not_evaluated} | {skipped} | {collection_errors} |".format(
                  **{key: counts.get(key, 0) for key in (
                      "selected", "evaluated", "passed", "behavior_failed",
                      "errors", "not_evaluated", "skipped", "collection_errors")}),
              ""]
    cases = report.get("cases") or []
    if cases:
        lines += ["| case | lane | classification | model | tokens in/out/cache-read | "
                  "cost USD | elapsed s |", "|---|---|---|---|---|---|---|"]
        for case in cases:
            calls = case.get("calls") or []
            configured = calls[0].get("configured_model") if calls else None
            observed = calls[-1].get("observed_model") if calls else None
            tokens = "/".join(_fmt(value) for value in _case_tokens(case))
            costs = [call.get("cost_usd") for call in calls]
            cost = (None if not calls or any(c is None for c in costs)
                    else round(sum(costs), 4))
            label = f"{case.get('case_id')}@{case.get('case_version')}"
            if case.get("trial") is not None:
                label += f" (trial {case['trial']})"
            classification = case.get("classification")
            if case.get("subtype"):
                classification = f"{classification} ({case['subtype']})"
            lines.append(
                f"| {label} | {_fmt(case.get('lane'))} | {classification} | "
                f"{_fmt(configured)} -> {_fmt(observed)} | {tokens} | {_fmt(cost)} | "
                f"{_fmt(case.get('duration_seconds'))} |"
            )
        lines.append("")
    problems = [case for case in cases if case.get("status") in ("failed", "error")]
    if problems:
        lines += ["## Failures and errors", ""]
        for case in problems:
            detail = (case.get("detail") or "")[:600].replace("\n", " ")
            lines.append(f"- `{case.get('case_id')}` {case.get('classification')}: {detail}")
        lines.append("")
    not_evaluated = [case for case in cases if case.get("status") == "not_evaluated"]
    if not_evaluated:
        lines += ["## Not evaluated", ""]
        for case in not_evaluated:
            lines.append(f"- `{case.get('case_id')}` {case.get('subtype')}: "
                         f"{case.get('detail') or ''}")
        lines.append("")
    if report.get("collection_errors"):
        lines += ["## Collection errors", ""]
        for error in report["collection_errors"]:
            lines.append(f"- `{error.get('nodeid')}`")
        lines.append("")
    lines += ["## Untested supported combinations", ""]
    lines += [f"- {item}" for item in report.get("untested_supported_combinations", [])]
    lines += ["", "## Limits", ""]
    lines += [f"- {item}" for item in report.get("limits", [])]
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_report(directory: str | Path, report: dict) -> tuple[Path, Path]:
    directory = Path(directory)
    json_path = directory / REPORT_JSON
    md_path = directory / REPORT_MD
    _atomic_write(
        json_path, redact_secrets(json.dumps(report, indent=2, sort_keys=True) + "\n"),
    )
    _atomic_write(md_path, redact_secrets(render_markdown(report)))
    return json_path, md_path


def ensure(path: str | Path, reason: str) -> bool:
    """Write a ``not_evaluated`` stub when no report exists. Returns True if written."""
    path = Path(path)
    if path.exists():
        return False
    stub = not_evaluated_stub(reason)
    write_report(path.parent, stub)
    if path.name != REPORT_JSON:
        _atomic_write(path, json.dumps(stub, indent=2, sort_keys=True) + "\n")
    return True


# ── Comparison ────────────────────────────────────────────────────────

EVALS_ROOT = Path(__file__).resolve().parent
# The code that turns a trace or a model response into a verdict, per lane:
# the scorers and stubs, the harness that decides which call is judged and
# which failure is a harness error (runtime ``score_and_record`` and the
# case workspace's protected inputs; the API lane's decision loop and
# read-only tool set), this module's classification, and the report plugin
# that picks which failure, phase and category ``classify_case`` receives.
# Case data (runtime/cases, goldens) is versioned per case; this code is
# shared by every case of the lane, so a change to it would otherwise alter
# outcomes without any case version changing.
SCORER_SOURCES: dict[str, tuple[str, ...]] = {
    "runtime": ("runtime/_scoring.py", "runtime/_stub_backend.py", "runtime/_runtime.py",
                "_live_report.py", "_live_report_plugin.py"),
    "api": ("live/_checks.py", "live/_harness.py", "live/_stub_office.py", "_live_report.py",
            "_live_report_plugin.py"),
}


# Structural tokens whose text is only layout (indent width, line endings).
_LAYOUT_TOKENS = (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER)


def _normalized_source(data: bytes) -> bytes:
    """Python source as its token stream without comments, docstrings or
    layout, so an editorial change keeps the digest: editing, adding or
    removing a module, class or function docstring, a comment, blank lines,
    spacing inside a line, or the indent width. Nesting is kept (INDENT and
    DEDENT stay as markers), so moving a statement in or out of a block
    changes it. Two limits: adding a docstring to a one-line body
    (``def f(): return x``) changes the block structure, and a body that is
    only a docstring cannot lose it without gaining a ``pass``. Source that
    does not parse is hashed as raw bytes. (Not ``ast.dump``: its output differs
    between Python versions.)"""
    try:
        text = data.decode("utf-8")
        docstrings = set()
        for node in ast.walk(ast.parse(text)):
            body = getattr(node, "body", None)
            if (isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                    and body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add((body[0].value.lineno, body[0].value.col_offset))
        tokens: list[str] = []
        in_docstring = False
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type in (tokenize.COMMENT, tokenize.NL):
                continue
            if token.type == tokenize.STRING and token.start in docstrings:
                in_docstring = True
                continue
            if in_docstring:
                if token.type == tokenize.STRING:  # implicit concatenation
                    continue
                in_docstring = False
                # The docstring statement's own terminator; a statement after
                # ``;`` on the same line is code and stays.
                if token.type == tokenize.NEWLINE or (
                        token.type == tokenize.OP and token.string == ";"):
                    continue
            name = tokenize.tok_name[token.type]
            tokens.append(name if token.type in _LAYOUT_TOKENS else f"{name} {token.string}")
    except (SyntaxError, ValueError, UnicodeDecodeError, tokenize.TokenError):
        return data
    return "\n".join(tokens).encode()


def scorer_sha256(lane: object, root: Path | None = None) -> str | None:
    """Digest of the lane's shared verdict-determining code (``None`` for
    other lanes), comments and docstrings excluded."""
    sources = SCORER_SOURCES.get(str(lane))
    if not sources:
        return None
    base = root or EVALS_ROOT
    digest = hashlib.sha256()
    for relative in sources:
        path = base / relative
        digest.update(f"{relative}\0".encode())
        digest.update(hashlib.sha256(_normalized_source(path.read_bytes())).digest()
                      if path.is_file() else b"missing")
    return digest.hexdigest()


def _scorer_changed(before: list[dict], after: list[dict]) -> bool:
    """True when both sides recorded their scorer digest and it differs.
    Reports from before the digest existed are not judged by it."""
    old = {case.get("scorer_sha256") for case in before}
    new = {case.get("scorer_sha256") for case in after}
    return None not in old | new and old != new


def comparison_key(case: dict) -> tuple:
    first = (case.get("calls") or [{}])[0]
    return (
        case.get("case_id"),
        case.get("case_version"),
        case.get("lane"),
        first.get("runtime"),
        first.get("configured_model"),
        first.get("effort_requested"),
        first.get("thinking_requested"),
        first.get("max_tokens"),
    )


def _agent_image(case: dict) -> dict | None:
    details = case.get("details") if isinstance(case.get("details"), dict) else {}
    image = details.get("agent_image")
    return image if isinstance(image, dict) else None


def _input_hashes(case: dict) -> dict:
    first = (case.get("calls") or [{}])[0]
    return {
        "system_prompt_sha256": first.get("system_prompt_sha256"),
        "tool_catalog_sha256": first.get("tool_catalog_sha256"),
        "source_sha256": case.get("source_sha256"),
        "fixture_sha256": case.get("fixture_sha256"),
        # The runtime lane has no tool-catalog hash; the image label is what
        # identifies the in-container MCP/tool code that ran.
        "agent_image_mcp_server_hash": (_agent_image(case) or {}).get("mcp_server_hash"),
    }


def _stale_image(cases: list[dict]) -> bool:
    """True when a runtime case ran on an image built from other sources
    (``CUBICLE_EVAL_ALLOW_STALE_IMAGE=1``): it is not evidence for the source."""
    return any(
        image is not None and image.get("matches_source") is not True
        for image in map(_agent_image, cases)
    )


def _grouped(report: dict) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for case in report.get("cases") or []:
        grouped.setdefault(case.get("case_id"), []).append(case)
    return grouped


def compare(baseline: dict, candidate: dict) -> dict:
    """Per-case comparison. Never averages across different cases.

    Token and elapsed-time deltas are an efficiency claim, so they are only
    presented when the claim is earned (F10 acceptance 5): both sides of the
    row are comparable, every trial on both sides passed its required
    outcomes, and neither side ran on a stale agent image. The comparison as a
    whole allows no efficiency claim (every row is suppressed, and the
    blockers say why) when:

    * a critical case regressed;
    * a critical case did not pass every candidate trial, whatever the reason
      (a failure, an error, a refusal, a truncation, a missing case, or a
      version change that made the row incomparable);
    * any case on either side recorded a forbidden effect;
    * any case ran on a stale agent image;
    * either report is ``not_evaluated`` or no case is comparable.
    """
    base, cand = _grouped(baseline), _grouped(candidate)
    rows = []
    for case_id in sorted(set(base) | set(cand), key=str):
        before, after = base.get(case_id, []), cand.get(case_id, [])
        row: dict[str, Any] = {
            "case_id": case_id,
            "critical": any(case.get("critical") for case in before + after),
            "baseline": _side(before),
            "candidate": _side(after),
            "stale_image": _stale_image(before + after),
        }
        if not before or not after:
            row["comparison"] = "missing_on_one_side"
        elif {comparison_key(c) for c in before} != {comparison_key(c) for c in after}:
            row["comparison"] = "not_comparable"
            row["reason"] = "case version, lane, runtime, model or request config differ"
        elif _scorer_changed(before, after):
            # The same behaviour can score differently under changed scorer
            # code; a transition would measure the scorer, not the change.
            row["comparison"] = "not_comparable"
            row["reason"] = ("the verdict-determining code differs between the reports "
                             "(scorer_sha256); re-run the baseline with the candidate's harness "
                             "to compare them")
        elif row["stale_image"]:
            # A stale image measures other sources; no transition is evidence.
            row["comparison"] = "not_comparable"
            row["reason"] = "a runtime case ran on an agent image that does not match its source"
        else:
            row["comparison"] = "comparable"
            changed = sorted(
                key for key in _input_hashes(before[0])
                if _input_hashes(before[0])[key] != _input_hashes(after[0])[key]
            )
            row["changed_inputs"] = changed
            row["transition"] = _transition(row["baseline"], row["candidate"])
        rows.append(row)
    critical_regressions = sorted(
        str(row["case_id"]) for row in rows
        if row["critical"] and row.get("transition") == "regressed"
    )
    critical_unproven = sorted(
        str(row["case_id"]) for row in rows
        if row["critical"] and str(row["case_id"]) not in critical_regressions
        and (row["candidate"]["n"] == 0
             or row["candidate"]["passed"] != row["candidate"]["n"])
    )
    with_effects = sorted(
        str(row["case_id"]) for row in rows
        if row["baseline"]["forbidden_effects"] or row["candidate"]["forbidden_effects"]
    )
    stale = sorted(str(row["case_id"]) for row in rows if row["stale_image"])
    blockers = []
    if critical_regressions:
        blockers.append(f"critical regressions: {', '.join(critical_regressions)}")
    if critical_unproven:
        blockers.append(
            "critical cases not passing on every candidate trial: "
            + ", ".join(critical_unproven)
        )
    if with_effects:
        blockers.append(f"forbidden effects in: {', '.join(with_effects)}")
    if stale:
        blockers.append(f"stale agent image in: {', '.join(stale)}")
    statuses = {"baseline": baseline.get("status"), "candidate": candidate.get("status")}
    not_evaluated = [label for label, status in statuses.items() if status == "not_evaluated"]
    if not_evaluated:
        blockers.append(f"{' and '.join(not_evaluated)} not evaluated")
    if not any(row["comparison"] == "comparable" for row in rows):
        blockers.append("no comparable case")
    for row in rows:
        row["efficiency"] = _efficiency(
            row, base.get(row["case_id"], []), cand.get(row["case_id"], []), blockers,
        )
    rows.sort(key=lambda row: (row.get("transition") != "regressed",
                               not row["critical"], str(row["case_id"])))
    return {
        "baseline_status": baseline.get("status"),
        "candidate_status": candidate.get("status"),
        "efficiency_claims_allowed": not blockers,
        "efficiency_blockers": blockers,
        "cases": rows,
    }


def forbidden_effects(case: dict) -> list[str]:
    """Forbidden effects a lane recorded for one case (``details``)."""
    details = case.get("details") if isinstance(case.get("details"), dict) else {}
    return [str(effect) for effect in details.get("forbidden_effects") or []]


def with_declared_effects(declared: dict | None, details: dict) -> dict:
    """``details`` plus one forbidden effect per tool the case declared
    forbidden that the deciding response called (F06-acc-8). The API lane
    records every tool of that response (``decision_tools``: the decision
    plus any other call in the same turn, so ``[create_task, delete_task]``
    still records delete_task); older reports carry only ``decision_tool``.
    The runtime lane records its own safety failures. Details are returned
    unchanged otherwise."""
    forbidden = set((declared or {}).get("forbidden_effects") or [])
    tools = details.get("decision_tools")
    if not isinstance(tools, list):
        tools = [details.get("decision_tool")]
    called = [tool for tool in dict.fromkeys(tools) if isinstance(tool, str) and tool in forbidden]
    if not called:
        return details
    effects = {f"decision: called {tool}, which this case declares forbidden" for tool in called}
    existing = [str(item) for item in details.get("forbidden_effects") or []]
    return {**details, "forbidden_effects": sorted({*existing, *effects})}


def _side(cases: list[dict]) -> dict:
    verdicts = [c for c in cases if c.get("classification") in VERDICT_CLASSIFICATIONS]
    return {
        "n": len(cases),
        "verdicts": len(verdicts),
        "passed": sum(1 for c in verdicts if c.get("classification") == "passed"),
        "classifications": sorted({str(c.get("classification")) for c in cases}),
        "forbidden_effects": sorted({effect for c in cases for effect in forbidden_effects(c)}),
    }


def _mean(values: list[float | int | None]) -> float | None:
    """Mean over known values; any unknown makes the mean unknown."""
    if not values or any(value is None for value in values):
        return None
    return sum(values) / len(values)


def _case_elapsed(case: dict) -> float | None:
    calls = case.get("calls") or []
    values = [call.get("elapsed_seconds") for call in calls]
    if not calls or any(value is None for value in values):
        return None
    return float(sum(values))


def _efficiency_side(cases: list[dict]) -> dict:
    tokens = [_case_tokens(case) for case in cases]
    return {
        "input_tokens": _mean([t[0] for t in tokens]),
        "output_tokens": _mean([t[1] for t in tokens]),
        "elapsed_seconds": _mean([_case_elapsed(case) for case in cases]),
    }


def _efficiency(row: dict, before: list[dict], after: list[dict],
                blockers: list[str]) -> dict:
    """Per-row efficiency, gated on outcomes, authority boundaries and the
    comparison-wide ``blockers`` (which suppress every row)."""
    reasons: list[str] = []
    if row["comparison"] != "comparable":
        reasons.append(f"rows are {row['comparison']}")
    if row.get("stale_image"):
        reasons.append("ran on an agent image that does not match its source")
    for label, side in (("baseline", row["baseline"]), ("candidate", row["candidate"])):
        if side["n"] == 0 or side["passed"] != side["n"]:
            reasons.append(
                f"{label} did not pass its required outcomes on every trial "
                f"({side['passed']}/{side['n']})"
            )
        if side["forbidden_effects"]:
            reasons.append(f"{label} recorded forbidden effects")
    reasons += [f"comparison-wide: {blocker}" for blocker in blockers]
    if reasons:
        return {"status": "suppressed", "reasons": reasons, "delta": None}
    base, cand = _efficiency_side(before), _efficiency_side(after)
    delta = {
        key: (None if base[key] is None or cand[key] is None
              else round(cand[key] - base[key], 3))
        for key in base
    }
    status = "unknown" if all(value is None for value in delta.values()) else "comparable"
    return {"status": status, "reasons": [], "baseline": base, "candidate": cand,
            "delta": delta}


def _transition(before: dict, after: dict) -> str:
    if not before["verdicts"] or not after["verdicts"]:
        return "no_verdict_on_one_side"
    before_rate = before["passed"] / before["verdicts"]
    after_rate = after["passed"] / after["verdicts"]
    if after_rate < before_rate:
        return "regressed"
    if after_rate > before_rate:
        return "improved"
    return "unchanged"


def _fmt_delta(value: float | None, unit: str = "") -> str:
    if value is None:
        return "unknown"
    return f"{value:+g}{unit}"


def _render_efficiency(efficiency: dict | None) -> str:
    if not efficiency:
        return "-"
    if efficiency["status"] == "suppressed":
        return "suppressed"
    delta = efficiency["delta"] or {}
    return (f"in {_fmt_delta(delta.get('input_tokens'))} / "
            f"out {_fmt_delta(delta.get('output_tokens'))} / "
            f"{_fmt_delta(delta.get('elapsed_seconds'), 's')}")


def render_comparison(result: dict) -> str:
    lines = [
        "# Live eval comparison",
        "",
        f"Baseline status: {result['baseline_status']}; candidate status: "
        f"{result['candidate_status']}. Rows are per case; there is no "
        "cross-case average.",
        "",
    ]
    if result.get("efficiency_claims_allowed") is False:
        lines += [
            "Efficiency deltas are not presented as gains: "
            + "; ".join(result.get("efficiency_blockers") or []) + ".",
            "",
        ]
    stale = [str(row["case_id"]) for row in result["cases"] if row.get("stale_image")]
    if stale:
        lines += [
            "Warning: these cases ran on an agent image that does not match its "
            f"source, so they are not evidence for a source change: {', '.join(stale)}.",
            "",
        ]
    lines += [
        "Efficiency is candidate minus baseline (mean tokens per trial, mean model/"
        "runtime seconds). It is shown only when both sides passed every required "
        "outcome and nothing blocks efficiency claims for the whole comparison.",
        "",
        "| case | critical | comparison | transition | baseline pass/verdicts (n) | "
        "candidate pass/verdicts (n) | changed inputs | efficiency Δ |",
        "|---|---|---|---|---|---|---|---|",
    ]
    suppressed = []
    for row in result["cases"]:
        base, cand = row["baseline"], row["candidate"]
        lines.append(
            f"| {row['case_id']} | {'yes' if row['critical'] else 'no'} | "
            f"{row['comparison']} | {row.get('transition', '-')} | "
            f"{base['passed']}/{base['verdicts']} ({base['n']}) | "
            f"{cand['passed']}/{cand['verdicts']} ({cand['n']}) | "
            f"{', '.join(row.get('changed_inputs') or []) or '-'} | "
            f"{_render_efficiency(row.get('efficiency'))} |"
        )
        efficiency = row.get("efficiency") or {}
        if efficiency.get("status") == "suppressed":
            suppressed.append(f"- `{row['case_id']}`: {'; '.join(efficiency['reasons'])}")
    if suppressed:
        lines += ["", "## Efficiency suppressed", "", *suppressed]
    return "\n".join(lines) + "\n"


# ── CLI ───────────────────────────────────────────────────────────────


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.evals._live_report")
    commands = parser.add_subparsers(dest="command", required=True)
    ensure_parser = commands.add_parser("ensure", help="write a not_evaluated stub if missing")
    ensure_parser.add_argument("path")
    ensure_parser.add_argument("--reason", default="pytest did not write a report")
    compare_parser = commands.add_parser("compare", help="compare two reports per case")
    compare_parser.add_argument("baseline")
    compare_parser.add_argument("candidate")
    compare_parser.add_argument("--markdown")
    args = parser.parse_args(argv)
    if args.command == "ensure":
        written = ensure(args.path, args.reason)
        print(("wrote not_evaluated stub: " if written else "report present: ") + args.path)
        return 0
    result = compare(
        json.loads(Path(args.baseline).read_text()),
        json.loads(Path(args.candidate).read_text()),
    )
    markdown = render_comparison(result)
    if args.markdown:
        _atomic_write(Path(args.markdown), markdown)
    print(markdown)
    return 0


_TRIAL_TOKEN = re.compile(r"(?:^|-)trial\d+(?=$|-)")


def strip_trial_id(callspec_id: str) -> str:
    return _TRIAL_TOKEN.sub("", callspec_id).strip("-")


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess test
    sys.exit(_main())
