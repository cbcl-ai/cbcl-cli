"""Offline tests for truthful behavioral-eval reporting (F10).

Covers the stdlib report module (classification, aggregation, rendering,
ensure/compare), the API-lane sender's error/usage handling, and an
end-to-end pytest subprocess run of the report plugin, including the
exit-code override that makes a zero-verdict run fail instead of passing.
No model or network calls are made.
"""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
import textwrap
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.evals import _live_report as live_report
from tests.evals.live import _harness

COMMUNICATOR_ROOT = Path(__file__).resolve().parents[2]


def _completed(**overrides) -> dict:
    observation = live_report.CallObservation(
        runtime=live_report.RUNTIME_MESSAGES_API, completed=True,
        configured_model="claude-opus-4-8", observed_model="claude-opus-4-8",
        stop_reason="tool_use",
    )
    data = observation.to_dict()
    data.update(overrides)
    return data


# ── classification ────────────────────────────────────────────────────


@pytest.mark.parametrize("kwargs,expected", [
    (dict(outcome="skipped", skip_reason=live_report.not_evaluated_reason(
        "missing_credentials", "no key")), "not_evaluated"),
    (dict(outcome="skipped", skip_reason="Skipped: not_evaluated:docker_unavailable: x"),
     "not_evaluated"),
    (dict(outcome="skipped", skip_reason="unrelated"), "skipped"),
    (dict(outcome="passed", observations=[_completed()]), "passed"),
    (dict(outcome="passed", observations=[]), "harness_error"),
    (dict(outcome="passed", observations=[{"completed": False}]), "harness_error"),
    (dict(outcome="failed", phase="setup", exc_type_name="RuntimeError"), "setup_error"),
    (dict(outcome="failed", exc_type_name="EvalProviderError",
          exc_category="harness_config_error"), "harness_config_error"),
    (dict(outcome="failed", exc_type_name="EvalProviderError",
          exc_category="provider_unavailable"), "provider_unavailable"),
    (dict(outcome="failed", exc_type_name="Failed", exc_message="Timeout >900.0s"),
     "transport_error"),
    (dict(outcome="failed", exc_type_name="JSONDecodeError",
          observations=[_completed(stop_reason="max_tokens")]), "output_truncated"),
    (dict(outcome="failed", exc_type_name="AssertionError",
          observations=[_completed(stop_reason="refusal")]), "refusal"),
    (dict(outcome="failed", exc_type_name="JSONDecodeError",
          observations=[_completed(stop_reason="end_turn")]), "behavior_failed"),
    (dict(outcome="failed", exc_type_name="AssertionError",
          observations=[_completed()]), "behavior_failed"),
    (dict(outcome="failed", exc_type_name="AssertionError", observations=[]),
     "harness_error"),
])
def test_classify_matrix(kwargs, expected):
    assert live_report.classify_case(**kwargs)["classification"] == expected


def test_output_contract_subtype_distinguishes_parse_failures():
    result = live_report.classify_case(
        outcome="failed", exc_type_name="JSONDecodeError",
        observations=[_completed(stop_reason="end_turn")],
    )
    assert result["subtype"] == "output_contract"


# ── aggregation ───────────────────────────────────────────────────────


def _case(classification: str, subtype: str | None = None, **extra) -> dict:
    return {"case_id": extra.pop("case_id", "c"), "classification": classification,
            "subtype": subtype, **extra}


@pytest.mark.parametrize("cases,errors,status,reason", [
    ([], [], "not_evaluated", "no_cases_selected"),
    ([_case("not_evaluated", "missing_credentials")] * 2, [], "not_evaluated",
     "missing_credentials"),
    ([_case("not_evaluated", "missing_credentials"),
      _case("not_evaluated", "docker_unavailable")], [], "not_evaluated", "mixed"),
    ([_case("harness_config_error")] * 3, [], "not_evaluated", "all_harness_config_error"),
    ([_case("passed")], [{"nodeid": "x"}], "incomplete", "some_cases_not_evaluated"),
    ([], [{"nodeid": "x"}], "not_evaluated", "collection_error"),
    ([_case("passed"), _case("provider_unavailable")], [], "incomplete",
     "some_cases_not_evaluated"),
    ([_case("passed"), _case("behavior_failed")], [], "failed", "behavior_failures"),
    ([_case("passed"), _case("passed")], [], "passed", "all_cases_passed"),
])
def test_aggregate_statuses(cases, errors, status, reason):
    summary = live_report.aggregate(cases, errors)
    assert (summary["status"], summary["status_reason"]) == (status, reason)


def test_zero_evaluated_run_is_never_green():
    report = live_report.build_report(
        [_case("skipped"), _case("not_evaluated", "missing_credentials")], [],
        started_at="t0", finished_at="t1", environment={},
    )
    assert report["status"] == "not_evaluated"
    assert report["counts"]["evaluated"] == 0


# ── rendering / secrets ───────────────────────────────────────────────


def test_report_never_contains_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET-TEST")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-SECRET-TEST")
    report = live_report.build_report(
        [_case("passed", calls=[_completed()])], [], started_at="a", finished_at="b",
    )
    serialized = json.dumps(report) + live_report.render_markdown(report)
    assert "SECRET-TEST" not in serialized
    assert report["environment"]["api_key_present"] is True
    assert report["environment"]["oauth_token_present"] is True


def test_markdown_headline_names_not_evaluated_and_reason():
    report = live_report.build_report(
        [_case("not_evaluated", "missing_credentials")], [],
        started_at="a", finished_at="b", environment={},
    )
    markdown = live_report.render_markdown(report)
    assert markdown.startswith(
        "# Live eval report: NOT EVALUATED - 0 behavioral verdicts (missing_credentials)"
    )


def test_unknown_usage_renders_unknown_not_zero():
    call = _completed(usage=live_report.usage_from_provider({"output_tokens": 12}))
    report = live_report.build_report(
        [_case("passed", calls=[call], case_version=1)], [],
        started_at="a", finished_at="b", environment={},
    )
    assert call["usage"]["input_tokens"] is None
    assert "unknown/12/unknown" in live_report.render_markdown(report)


def test_report_files_redact_credential_values(tmp_path, monkeypatch):
    key = "sk-ant-oat01-synthetic\"token/with+chars"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", key)
    case = {"nodeid": "x::y", "classification": "harness_error",
            "error": {"message": f"env printed ANTHROPIC_API_KEY={key}"}}
    now = live_report.utc_now()
    report = live_report.build_report([case], [], started_at=now, finished_at=now)
    json_path, md_path = live_report.write_report(tmp_path, report)
    for path in (json_path, md_path):
        text = path.read_text(encoding="utf-8")
        assert key not in text and json.dumps(key)[1:-1] not in text
    assert live_report.REDACTED in json_path.read_text(encoding="utf-8")
    assert json.loads(json_path.read_text(encoding="utf-8"))["environment"]["oauth_token_present"]


def test_placeholder_credentials_do_not_rewrite_ordinary_text():
    text = '{"cases": [], "status": "passed"}'
    assert live_report.redact_secrets(text, environ={"ANTHROPIC_API_KEY": "cases"}) == text
    secret = "sk-ant-api03-" + "x" * 20
    assert secret not in live_report.redact_secrets(
        f"key={secret}", environ={"ANTHROPIC_API_KEY": secret},
    )


def test_ensure_writes_stub_only_when_missing(tmp_path):
    target = tmp_path / live_report.REPORT_JSON
    assert live_report.ensure(target, "job killed") is True
    stub = json.loads(target.read_text())
    assert stub["status"] == "not_evaluated" and stub["status_reason"] == "report_missing"
    assert (tmp_path / live_report.REPORT_MD).is_file()
    target.write_text(json.dumps({"status": "passed"}))
    assert live_report.ensure(target, "again") is False
    assert json.loads(target.read_text()) == {"status": "passed"}


# ── compare ───────────────────────────────────────────────────────────


def _report(cases: list[dict], status: str = "passed") -> dict:
    return {"status": status, "cases": cases}


def _cmp_case(case_id, classification, *, version=1, model="claude-opus-4-8",
              critical=False, prompt_hash="a") -> dict:
    return {
        "case_id": case_id, "case_version": version, "lane": "api",
        "critical": critical, "classification": classification,
        "calls": [{"runtime": "anthropic_messages_api", "configured_model": model,
                   "effort_requested": "xhigh", "thinking_requested": "adaptive",
                   "max_tokens": 16000, "system_prompt_sha256": prompt_hash}],
    }


def test_compare_flags_incomparable_and_regressions_without_averaging():
    baseline = _report([
        _cmp_case("a", "passed"), _cmp_case("b", "passed", critical=True),
        _cmp_case("c", "passed"), _cmp_case("d", "passed"),
    ])
    candidate = _report([
        _cmp_case("a", "passed", version=2),
        _cmp_case("b", "behavior_failed", critical=True, prompt_hash="b"),
        _cmp_case("c", "passed", model="claude-sonnet-5"),
    ], status="failed")
    result = live_report.compare(baseline, candidate)
    rows = {row["case_id"]: row for row in result["cases"]}
    assert result["cases"][0]["case_id"] == "b"  # critical regression first
    assert rows["b"]["transition"] == "regressed"
    assert rows["b"]["changed_inputs"] == ["system_prompt_sha256"]
    assert rows["a"]["comparison"] == "not_comparable"
    assert rows["c"]["comparison"] == "not_comparable"
    assert rows["d"]["comparison"] == "missing_on_one_side"
    rendered = live_report.render_comparison(result)
    assert "average" not in rendered.lower().replace("no cross-case average", "")


def _eff_case(case_id, classification, *, tokens=(1000, 200), elapsed=10.0,
              critical=False, effects=None, version=1) -> dict:
    case = _cmp_case(case_id, classification, critical=critical, version=version)
    case["calls"][0].update({
        "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1],
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        "elapsed_seconds": elapsed,
    })
    if effects is not None:
        case["details"] = {"forbidden_effects": effects}
    return case


def test_compare_presents_efficiency_only_when_both_sides_pass():
    baseline = _report([_eff_case("a", "passed"), _eff_case("a", "passed", tokens=(1200, 300))])
    candidate = _report([_eff_case("a", "passed", tokens=(800, 150), elapsed=6.0)])
    result = live_report.compare(baseline, candidate)
    row = result["cases"][0]
    assert result["efficiency_claims_allowed"] is True
    assert row["efficiency"]["status"] == "comparable"
    assert row["efficiency"]["delta"] == {
        "input_tokens": -300.0, "output_tokens": -100.0, "elapsed_seconds": -4.0,
    }
    rendered = live_report.render_comparison(result)
    assert "in -300 / out -100 / -4s" in rendered
    assert "not presented as gains" not in rendered


@pytest.mark.parametrize("candidate_cases,reason", [
    ([_eff_case("a", "passed", tokens=(10, 1)), _eff_case("a", "behavior_failed", tokens=(10, 1))],
     "candidate did not pass its required outcomes"),
    ([_eff_case("a", "provider_unavailable", tokens=(10, 1))],
     "candidate did not pass its required outcomes"),
    ([_eff_case("a", "passed", tokens=(10, 1), effects=["payment script executed"])],
     "candidate recorded forbidden effects"),
], ids=["failed_trial", "error_only", "forbidden_effect"])
def test_compare_suppresses_efficiency_without_earned_outcomes(candidate_cases, reason):
    """C15: a cheaper or faster side is not a gain unless outcomes hold."""
    baseline = _report([_eff_case("a", "passed", tokens=(1000, 200))])
    result = live_report.compare(baseline, _report(candidate_cases))
    efficiency = result["cases"][0]["efficiency"]
    assert efficiency["status"] == "suppressed" and efficiency["delta"] is None
    assert any(reason in item for item in efficiency["reasons"]), efficiency["reasons"]
    rendered = live_report.render_comparison(result)
    assert "| suppressed |" in rendered and "in -990" not in rendered
    assert reason in rendered


def test_a_critical_regression_suppresses_every_efficiency_claim():
    baseline = _report([
        _eff_case("fast", "passed", tokens=(1000, 200)),
        _eff_case("guard", "passed", critical=True),
    ])
    candidate = _report([
        _eff_case("fast", "passed", tokens=(100, 20)),
        _eff_case("guard", "behavior_failed", critical=True),
    ])
    result = live_report.compare(baseline, candidate)
    rows = {row["case_id"]: row for row in result["cases"]}
    assert result["efficiency_claims_allowed"] is False
    assert "critical regressions: guard" in result["efficiency_blockers"]
    assert rows["fast"]["efficiency"]["status"] == "suppressed"
    assert rows["fast"]["efficiency"]["delta"] is None
    rendered = live_report.render_comparison(result)
    assert "Efficiency deltas are not presented as gains: critical regressions: guard" in rendered
    assert "in -900" not in rendered


def _critical_pair(candidate_guard: list[dict]) -> dict:
    """compare() of a fast unaffected row plus a critical guard row."""
    baseline = _report([
        _eff_case("fast", "passed", tokens=(1000, 200)),
        *[_eff_case("guard", "passed", critical=True) for _ in range(3)],
    ])
    candidate = _report([_eff_case("fast", "passed", tokens=(100, 20)), *candidate_guard])
    return live_report.compare(baseline, candidate)


def _assert_all_suppressed(result: dict, blocker: str) -> None:
    rows = {row["case_id"]: row for row in result["cases"]}
    assert result["efficiency_claims_allowed"] is False
    assert any(blocker in item for item in result["efficiency_blockers"]), result
    assert rows["fast"]["efficiency"]["status"] == "suppressed"
    assert rows["fast"]["efficiency"]["delta"] is None
    rendered = live_report.render_comparison(result)
    assert "in -900" not in rendered
    assert "Efficiency deltas are not presented as gains" in rendered


@pytest.mark.parametrize("guard", [
    [_eff_case("guard", "refusal", critical=True)],
    [_eff_case("guard", "output_truncated", critical=True)],
    [_eff_case("guard", "runtime_infrastructure_error", critical=True)],
    [],                                                        # omitted from the candidate
    [_eff_case("guard", "passed", critical=True),              # 1 passed + 2 refusals
     _eff_case("guard", "refusal", critical=True),
     _eff_case("guard", "refusal", critical=True)],
    [_eff_case("guard", "behavior_failed", critical=True, version=2)],  # version bumped
    [_eff_case("guard", "not_evaluated", critical=True)],
], ids=["refusal", "truncated", "infra-error", "omitted", "mixed", "bumped-and-failing",
        "not-evaluated"])
def test_a_critical_case_not_passing_in_the_candidate_suppresses_every_claim(guard):
    _assert_all_suppressed(
        _critical_pair(guard), "critical cases not passing on every candidate trial: guard",
    )


def test_a_critical_case_failing_on_both_sides_suppresses_every_claim():
    baseline = _report([_eff_case("fast", "passed", tokens=(1000, 200)),
                        _eff_case("guard", "behavior_failed", critical=True)])
    candidate = _report([_eff_case("fast", "passed", tokens=(100, 20)),
                         _eff_case("guard", "behavior_failed", critical=True)])
    _assert_all_suppressed(live_report.compare(baseline, candidate), "guard")


def test_critical_cases_passing_everywhere_allow_claims():
    result = _critical_pair([_eff_case("guard", "passed", critical=True) for _ in range(3)])
    rows = {row["case_id"]: row for row in result["cases"]}
    assert result["efficiency_claims_allowed"] is True, result["efficiency_blockers"]
    assert rows["fast"]["efficiency"]["delta"]["input_tokens"] == -900


def test_a_forbidden_effect_suppresses_every_row_consistently():
    baseline = _report([_eff_case("fast", "passed", tokens=(1000, 200)),
                        _eff_case("guard", "passed")])
    candidate = _report([_eff_case("fast", "passed", tokens=(100, 20)),
                         _eff_case("guard", "passed", effects=["R5: payment script executed"])])
    result = live_report.compare(baseline, candidate)
    _assert_all_suppressed(result, "forbidden effects in: guard")
    # Flag and rows agree: no row presents a delta while claims are refused.
    assert all(row["efficiency"]["delta"] is None for row in result["cases"])


def _with_image(case: dict, label: str, matches: bool) -> dict:
    case["details"] = {"agent_image": {"image": "cbcl-agent:eval", "mcp_server_hash": label,
                                       "source_mcp_server_hash": "src", "matches_source": matches}}
    return case


def test_a_stale_image_run_is_not_comparable_and_blocks_claims():
    baseline = _report([_eff_case("fast", "passed", tokens=(1000, 200)),
                        _with_image(_eff_case("rt", "passed"), "src", True)])
    candidate = _report([_eff_case("fast", "passed", tokens=(100, 20)),
                         _with_image(_eff_case("rt", "passed", tokens=(10, 1)), "old", False)])
    result = live_report.compare(baseline, candidate)
    rows = {row["case_id"]: row for row in result["cases"]}
    assert rows["rt"]["comparison"] == "not_comparable" and "transition" not in rows["rt"]
    _assert_all_suppressed(result, "stale agent image in: rt")
    assert "not evidence for a source change: rt" in live_report.render_comparison(result)


def test_an_image_label_change_is_a_changed_input():
    baseline = _report([_with_image(_eff_case("rt", "passed"), "one", True)])
    candidate = _report([_with_image(_eff_case("rt", "passed"), "two", True)])
    row = live_report.compare(baseline, candidate)["cases"][0]
    assert row["comparison"] == "comparable"
    assert "agent_image_mcp_server_hash" in row["changed_inputs"]


@pytest.mark.parametrize("baseline_status,candidate_status,cases,blocker", [
    ("not_evaluated", "passed", True, "baseline not evaluated"),
    ("passed", "not_evaluated", True, "candidate not evaluated"),
    ("passed", "passed", False, "no comparable case"),
])
def test_claims_are_never_vacuously_allowed(baseline_status, candidate_status, cases, blocker):
    baseline = _report([_eff_case("a", "passed")] if cases else [], status=baseline_status)
    candidate = _report([_eff_case("a", "passed", tokens=(10, 1))] if cases else
                        [_eff_case("b", "passed")], status=candidate_status)
    result = live_report.compare(baseline, candidate)
    assert result["efficiency_claims_allowed"] is False
    assert blocker in result["efficiency_blockers"]


def test_unknown_usage_is_unknown_efficiency_not_zero():
    baseline = _report([_eff_case("a", "passed")])
    candidate = _report([_eff_case("a", "passed")])
    candidate["cases"][0]["calls"][0]["usage"]["input_tokens"] = None
    candidate["cases"][0]["calls"][0]["elapsed_seconds"] = None
    efficiency = live_report.compare(baseline, candidate)["cases"][0]["efficiency"]
    assert efficiency["status"] == "comparable"
    assert efficiency["delta"]["input_tokens"] is None
    assert efficiency["delta"]["elapsed_seconds"] is None
    assert efficiency["delta"]["output_tokens"] == 0


def test_compare_cli_writes_markdown(tmp_path):
    base = tmp_path / "base.json"
    cand = tmp_path / "cand.json"
    base.write_text(json.dumps(_report([_cmp_case("a", "passed")])))
    cand.write_text(json.dumps(_report([_cmp_case("a", "behavior_failed")])))
    out = tmp_path / "cmp.md"
    assert live_report._main(["compare", str(base), str(cand), "--markdown", str(out)]) == 0
    assert "| a |" in out.read_text()


# ── API-lane sender ───────────────────────────────────────────────────


def _body() -> dict:
    return _harness.build_messages_request(
        _harness.render_generator_prompt("INSTRUCTIONS_PROMPT"), "hello", [],
        model="claude-opus-4-8", effort="xhigh",
    )


def _http_error(status: int, body: dict, headers: dict | None = None):
    return urllib.error.HTTPError(
        "https://api.anthropic.com/v1/messages", status, "error",
        headers or {}, io.BytesIO(json.dumps(body).encode()),
    )


def _ok_response(payload: dict):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return response


def test_default_request_carries_no_sampling_params_and_production_reasoning():
    body = _body()
    assert not {"temperature", "top_p", "top_k"} & set(body)
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "xhigh"}


def test_http_400_is_harness_config_error_without_retry(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(1)
        raise _http_error(400, {"type": "error", "request_id": "req_x", "error": {
            "type": "invalid_request_error", "message": "temperature is not supported"}})

    monkeypatch.setattr(_harness.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(live_report.EvalProviderError) as raised:
        _harness.send_messages_request(_body(), api_key="k", sleep=lambda _: None)
    assert raised.value.category == "harness_config_error"
    assert raised.value.status == 400
    assert raised.value.error_type == "invalid_request_error"
    assert raised.value.request_id == "req_x"
    assert len(calls) == 1


def test_missing_key_is_a_credentials_error_not_a_verdict(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(live_report.EvalProviderError) as raised:
        _harness.send_messages_request(_body())
    assert raised.value.category == "credentials_error"


def test_retry_is_bounded_and_honors_capped_retry_after(monkeypatch):
    sleeps: list[float] = []
    outcomes = [
        _http_error(529, {"error": {"type": "overloaded_error", "message": "busy"}},
                    {"retry-after": "120"}),
        _ok_response({"content": [{"type": "text", "text": "ok"}], "model": "m",
                      "stop_reason": "end_turn", "usage": {"input_tokens": 3}}),
    ]

    def fake_urlopen(request, timeout):
        item = outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(_harness.urllib.request, "urlopen", fake_urlopen)
    result = _harness.send_messages_request(
        _body(), api_key="k", max_retries=2, sleep=sleeps.append,
    )
    assert result.text == "ok"
    assert sleeps == [_harness.RETRY_AFTER_CAP_SECONDS]

    monkeypatch.setattr(
        _harness.urllib.request, "urlopen",
        lambda request, timeout: (_ for _ in ()).throw(
            _http_error(429, {"error": {"type": "rate_limit_error", "message": "slow"}})
        ),
    )
    sleeps.clear()
    with pytest.raises(live_report.EvalProviderError) as raised:
        _harness.send_messages_request(_body(), api_key="k", max_retries=2,
                                       sleep=sleeps.append)
    assert raised.value.category == "provider_unavailable"
    assert len(raised.value.attempts) == 3 and len(sleeps) == 2


@pytest.mark.parametrize("error", [
    urllib.error.URLError(TimeoutError("timed out")),
    TimeoutError("timed out"),
])
def test_transport_failures_are_classified_as_transport(monkeypatch, error):
    def fake_urlopen(request, timeout):
        raise error

    monkeypatch.setattr(_harness.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(live_report.EvalProviderError) as raised:
        _harness.send_messages_request(_body(), api_key="k", max_retries=0)
    assert raised.value.category == "transport_error"


def test_observation_records_unknown_usage_as_null_and_model_identity(monkeypatch):
    payload = {
        "id": "msg_1", "model": "claude-opus-4-8-20260101", "stop_reason": "tool_use",
        "content": [], "usage": {"input_tokens": 10, "cache_read_input_tokens": 4},
    }
    monkeypatch.setattr(_harness.urllib.request, "urlopen",
                        lambda request, timeout: _ok_response(payload))
    live_report.reset_registry()
    live_report.begin_case("node")
    try:
        _harness.send_messages_request(_body(), api_key="k")
    finally:
        live_report.end_case()
    [observation] = live_report.observations_for("node")
    assert observation["usage"] == {
        "input_tokens": 10, "output_tokens": None,
        "cache_creation_input_tokens": None, "cache_read_input_tokens": 4,
    }
    assert observation["observed_model"] == "claude-opus-4-8-20260101"
    assert observation["model_mismatch"] is False
    assert observation["message_id"] == "msg_1"
    assert observation["request_id"] is None  # MagicMock headers are not strings
    assert observation["sampling_params_sent"] == []
    assert observation["system_prompt_sha256"] == live_report.sha256_text(_body()["system"])


def test_tool_catalog_hash_is_canonical_and_description_sensitive():
    tools_a = [{"name": "x", "description": "d", "input_schema": {"b": 1, "a": 2}}]
    tools_b = [{"input_schema": {"a": 2, "b": 1}, "description": "d", "name": "x"}]
    tools_c = [{"name": "x", "description": "changed", "input_schema": {"a": 2, "b": 1}}]
    assert live_report.canonical_sha256(tools_a) == live_report.canonical_sha256(tools_b)
    assert live_report.canonical_sha256(tools_a) != live_report.canonical_sha256(tools_c)


# ── plugin end-to-end (subprocess) ────────────────────────────────────

_PLUGIN_CONFTEST = """
from tests.evals._live_report_plugin import (  # noqa: F401
    pytest_collection_finish, pytest_collection_modifyitems, pytest_collectreport,
    pytest_configure, pytest_runtest_call, pytest_runtest_makereport,
    pytest_sessionfinish, pytest_terminal_summary,
)
"""
_INI = """
[pytest]
markers =
    live_eval: live
    eval_case: identity
"""
_CASES = """
import pytest
from tests.evals import _live_report as live_report

pytestmark = pytest.mark.live_eval


def _observe():
    live_report.record_call(live_report.CallObservation(
        runtime=live_report.RUNTIME_MESSAGES_API, completed=True,
        configured_model="m", observed_model="m", stop_reason="end_turn",
    ))


@pytest.mark.eval_case(id="demo.pass", version=3, lane="api", role="manager")
def test_pass():
    _observe()


@pytest.mark.eval_case(id="demo.fail", version=1, lane="api", role="manager")
def test_behavior_failure():
    _observe()
    assert False, "wrong decision"
"""
_STATIC = """
def test_static():
    assert True
"""


def _plugin_project(tmp_path: Path, *, skip_all: bool = False,
                    broken_module: bool = False, passes_without_calls: bool = False) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    conftest = _PLUGIN_CONFTEST
    if skip_all:
        conftest += textwrap.dedent("""
            import pytest
            from tests.evals import _live_report as live_report


            def pytest_collection_modifyitems(config, items):
                marker = pytest.mark.skip(reason=live_report.not_evaluated_reason(
                    "missing_credentials", "no key"))
                for item in items:
                    if "live_eval" in item.keywords:
                        item.add_marker(marker)
        """)
    (project / "conftest.py").write_text(conftest)
    (project / "pytest.ini").write_text(_INI)
    cases = _CASES
    if passes_without_calls:
        cases = cases.replace("def test_pass():\n    _observe()", "def test_pass():\n    pass")
        cases = cases.split("@pytest.mark.eval_case(id=\"demo.fail\"")[0]
    (project / "test_cases.py").write_text(cases)
    (project / "test_static.py").write_text(_STATIC)
    if broken_module:
        (project / "test_broken.py").write_text(
            "import pytest\npytestmark = pytest.mark.live_eval\nimport not_a_module_xyz\n"
        )
    return project


def _run_pytest(project: Path, env_overrides: dict, *args: str) -> subprocess.CompletedProcess:
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("CUBICLE_LIVE_EVAL")
    }
    env["PYTHONPATH"] = str(COMMUNICATOR_ROOT)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", *args],
        cwd=project, env=env, capture_output=True, text=True, timeout=120,
    )


def test_plugin_end_to_end_reports_verdicts_and_keeps_exit_one(tmp_path):
    project = _plugin_project(tmp_path)
    reports = tmp_path / "reports"
    completed = _run_pytest(project, {
        "CUBICLE_LIVE_EVAL_REPORT_DIR": str(reports),
        "CUBICLE_LIVE_EVAL_REQUIRE_EXECUTION": "1",
    }, "-m", "live_eval")
    assert completed.returncode == 1, completed.stdout + completed.stderr
    report = json.loads((reports / live_report.REPORT_JSON).read_text())
    assert report["status"] == "failed"
    assert report["counts"]["selected"] == 2
    assert report["counts"]["passed"] == 1 and report["counts"]["behavior_failed"] == 1
    by_id = {case["case_id"]: case for case in report["cases"]}
    assert by_id["demo.pass"]["case_version"] == 3
    assert by_id["demo.fail"]["subtype"] == "assertion"
    assert (reports / live_report.REPORT_MD).is_file()
    assert "cubicle live eval report" in completed.stdout


def test_plugin_zero_verdict_run_exits_not_evaluated(tmp_path):
    project = _plugin_project(tmp_path, skip_all=True)
    reports = tmp_path / "reports"
    completed = _run_pytest(project, {
        "CUBICLE_LIVE_EVAL_REPORT_DIR": str(reports),
        "CUBICLE_LIVE_EVAL_REQUIRE_EXECUTION": "1",
    }, "-m", "live_eval")
    assert completed.returncode == live_report.NOT_EVALUATED_EXIT, completed.stdout
    report = json.loads((reports / live_report.REPORT_JSON).read_text())
    assert (report["status"], report["status_reason"]) == (
        "not_evaluated", "missing_credentials",
    )
    # Without REQUIRE the same run is reported honestly but keeps pytest's 0.
    completed = _run_pytest(project, {
        "CUBICLE_LIVE_EVAL_REPORT_DIR": str(tmp_path / "second"),
    }, "-m", "live_eval")
    assert completed.returncode == 0
    second = json.loads((tmp_path / "second" / live_report.REPORT_JSON).read_text())
    assert second["status"] == "not_evaluated"


def test_plugin_pass_without_observation_is_not_evidence(tmp_path):
    project = _plugin_project(tmp_path, passes_without_calls=True)
    reports = tmp_path / "reports"
    completed = _run_pytest(project, {
        "CUBICLE_LIVE_EVAL_REPORT_DIR": str(reports),
        "CUBICLE_LIVE_EVAL_REQUIRE_EXECUTION": "1",
    }, "-m", "live_eval")
    assert completed.returncode == live_report.NOT_EVALUATED_EXIT, completed.stdout
    report = json.loads((reports / live_report.REPORT_JSON).read_text())
    assert report["cases"][0]["classification"] == "harness_error"


def test_plugin_preserves_collection_error_exit(tmp_path):
    project = _plugin_project(tmp_path, broken_module=True)
    reports = tmp_path / "reports"
    completed = _run_pytest(project, {
        "CUBICLE_LIVE_EVAL_REPORT_DIR": str(reports),
        "CUBICLE_LIVE_EVAL_REQUIRE_EXECUTION": "1",
    }, "-m", "live_eval")
    assert completed.returncode == 2, completed.stdout
    report = json.loads((reports / live_report.REPORT_JSON).read_text())
    assert report["status_reason"] == "collection_error"
    assert any("test_broken" in error["nodeid"] for error in report["collection_errors"])


def test_plugin_is_inert_without_env(tmp_path):
    project = _plugin_project(tmp_path)
    completed = _run_pytest(project, {}, "-m", "live_eval")
    assert completed.returncode == 1
    assert "cubicle live eval report" not in completed.stdout
    assert not list(tmp_path.rglob(live_report.REPORT_JSON))


# ── F06-acc-8: declared forbidden effects reach the report ────────────

_DECLARED = {
    "allowed_tools": "manager:workstream",
    "initial_state": "Default workstream; no tasks.",
    "forbidden_effects": ["create_task", "delete_task"],
}


def test_a_declared_forbidden_decision_is_recorded_as_a_forbidden_effect():
    details = {"decision": "delete_task({...})", "decision_tool": "delete_task"}
    marked = live_report.with_declared_effects(_DECLARED, details)
    assert marked["forbidden_effects"] == [
        "decision: called delete_task, which this case declares forbidden",
    ]
    assert details.get("forbidden_effects") is None  # the input is not mutated
    merged = live_report.with_declared_effects(
        _DECLARED, {**details, "forbidden_effects": ["R5: input changed"]},
    )
    assert len(merged["forbidden_effects"]) == 2


def test_every_forbidden_tool_in_the_deciding_response_is_recorded():
    """EV-3: a turn of [create_task, delete_task] decides create_task but
    also attempted delete_task; each forbidden call is its own effect."""
    declared = {**_DECLARED, "forbidden_effects": ["delete_task", "archive_task"]}
    details = {"decision_tool": "create_task",
               "decision_tools": ["create_task", "delete_task", "archive_task", "delete_task"]}
    assert live_report.with_declared_effects(declared, details)["forbidden_effects"] == [
        "decision: called archive_task, which this case declares forbidden",
        "decision: called delete_task, which this case declares forbidden",
    ]
    clean = {"decision_tool": "create_task", "decision_tools": ["create_task", "get_board"]}
    assert live_report.with_declared_effects(declared, clean) == clean


@pytest.mark.parametrize("declared,details", [
    (_DECLARED, {"decision_tool": "ask_user_choice"}),   # allowed decision
    (_DECLARED, {"decision_tool": None}),                # final text
    (_DECLARED, {}),                                      # generator: nothing recorded
    (None, {"decision_tool": "delete_task"}),            # undeclared case
])
def test_declared_effects_leave_other_decisions_unchanged(declared, details):
    assert live_report.with_declared_effects(declared, details) == details


def test_a_declared_forbidden_decision_suppresses_efficiency_claims():
    baseline = _report([_eff_case("a", "passed")])
    effect = live_report.with_declared_effects(
        _DECLARED, {"decision_tool": "create_task"},
    )["forbidden_effects"]
    candidate = _report([_eff_case("a", "passed", tokens=(10, 1), effects=effect)])
    result = live_report.compare(baseline, candidate)
    assert result["efficiency_claims_allowed"] is False
    assert result["cases"][0]["efficiency"]["status"] == "suppressed"


_DECLARED_CASES = """
import pytest
from tests.evals import _live_report as live_report

pytestmark = pytest.mark.live_eval
DECLARED = {"allowed_tools": "manager:workstream", "initial_state": "empty workstream",
            "forbidden_effects": ["delete_task"]}


@pytest.mark.eval_case(id="demo.declared", version=1, lane="api", role="manager",
                       declared=DECLARED)
def test_declared():
    live_report.record_call(live_report.CallObservation(
        runtime=live_report.RUNTIME_MESSAGES_API, completed=True,
        configured_model="m", observed_model="m", stop_reason="tool_use",
    ))
    live_report.record_detail("decision_tool", "delete_task")
"""


def test_a_scorer_change_makes_every_row_of_the_lane_incomparable():
    """EVR-8 / STALE-CASE-VERSIONS: a shared scorer change alters outcomes
    without any case version changing; the recorded digest catches it."""
    baseline = _report([{**_eff_case("a", "passed"), "scorer_sha256": "old"}])
    candidate = _report([{**_eff_case("a", "behavior_failed"), "scorer_sha256": "new"}])
    result = live_report.compare(baseline, candidate)
    row = result["cases"][0]
    assert row["comparison"] == "not_comparable" and "scorer" in row["reason"]
    assert "transition" not in row
    assert result["efficiency_claims_allowed"] is False
    same = _report([{**_eff_case("a", "passed"), "scorer_sha256": "old"}])
    assert live_report.compare(baseline, same)["cases"][0]["comparison"] == "comparable"


def test_reports_without_a_scorer_digest_are_not_judged_by_it():
    baseline = _report([_eff_case("a", "passed")])
    candidate = _report([{**_eff_case("a", "passed"), "scorer_sha256": "new"}])
    assert live_report.compare(baseline, candidate)["cases"][0]["comparison"] == "comparable"


@pytest.mark.parametrize("lane,relative", [
    (lane, relative) for lane, sources in live_report.SCORER_SOURCES.items()
    for relative in sources
])
def test_scorer_digest_tracks_every_verdict_determining_file(tmp_path, lane, relative):
    """R3-DIGEST-SCOPE: the harness code that classifies exceptions, builds
    the protected inputs or picks the judged call is part of the digest."""
    for sources in live_report.SCORER_SOURCES.values():
        for name in sources:
            (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / name).write_text('"""Doc."""\nx = 1  # note\n')
    before = {name: live_report.scorer_sha256(name, tmp_path) for name in live_report.SCORER_SOURCES}
    (tmp_path / relative).write_text('"""Doc."""\nx = 2  # note\n')
    after = {name: live_report.scorer_sha256(name, tmp_path) for name in live_report.SCORER_SOURCES}
    assert after[lane] != before[lane]
    for other, sources in live_report.SCORER_SOURCES.items():
        if relative not in sources:
            assert after[other] == before[other]


# Modules under tests/evals that a scorer source imports but that cannot
# change a verdict. Each entry needs a reason; package markers are checked
# to hold nothing but a docstring.
_DIGEST_EXEMPT = {
    "__init__.py": "package marker",
    "live/__init__.py": "package marker",
    "runtime/__init__.py": "package marker",
}


def _evals_imports(path: Path) -> set[str]:
    """Every tests/evals module ``path`` imports, at any depth, as a path
    relative to EVALS_ROOT (a package resolves to its ``__init__.py``)."""
    root = live_report.EVALS_ROOT
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{path}: relative import; resolve it here first"
            names.add(node.module or "")
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    found = set()
    for name in names:
        if not name.startswith("tests.evals"):
            continue
        relative = Path(*name.split(".")[2:])
        if (root / relative).with_suffix(".py").is_file():
            found.add(relative.with_suffix(".py").as_posix())
        elif (root / relative / "__init__.py").is_file():
            found.add((relative / "__init__.py").as_posix())
    return found


@pytest.mark.parametrize("lane", sorted(live_report.SCORER_SOURCES))
def test_scorer_digest_covers_every_evals_module_the_scorers_import(lane):
    """B7c-05: the digest list is checked from outside itself. The report
    plugin is required explicitly (it imports ``_live_report``, not the other
    way round, so no import walk from the scorers would reach it); every
    tests/evals module reachable from the listed sources must be listed for
    the lane or exempt with a reason."""
    sources = live_report.SCORER_SOURCES[lane]
    assert "_live_report_plugin.py" in sources
    assert "_live_report.py" in sources
    root = live_report.EVALS_ROOT
    missing = {}
    pending, seen = list(sources), set()
    while pending:
        relative = pending.pop()
        if relative in seen:
            continue
        seen.add(relative)
        assert (root / relative).is_file(), f"{lane}: listed source {relative} does not exist"
        for imported in _evals_imports(root / relative):
            if imported in sources:
                pending.append(imported)
            elif imported not in _DIGEST_EXEMPT:
                missing.setdefault(imported, relative)
    assert not missing, (
        f"{lane}: add these to SCORER_SOURCES or _DIGEST_EXEMPT (imported by): {missing}"
    )


@pytest.mark.parametrize("relative", sorted(_DIGEST_EXEMPT))
def test_digest_exempt_package_markers_hold_no_code(relative):
    body = ast.parse((live_report.EVALS_ROOT / relative).read_text()).body
    code = [node for node in body if not (
        isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str))]
    assert not code, f"{relative} is exempt from the scorer digest but holds code"


def test_scorer_digest_ignores_comments_docstrings_and_layout(tmp_path):
    path = tmp_path / "runtime/_scoring.py"
    path.parent.mkdir(parents=True)
    path.write_text('"""Module doc."""\n\n\ndef f(x):\n    """Old doc."""\n    return x + 1  # a\n')
    before = live_report.scorer_sha256("runtime", tmp_path)
    path.write_text('"""New module doc."""\n\ndef f(x):\n    """New doc."""\n'
                    '    # a comment line\n    return x+1\n')
    assert live_report.scorer_sha256("runtime", tmp_path) == before
    path.write_text('"""New module doc."""\n\ndef f(x):\n    return x + 2\n')
    assert live_report.scorer_sha256("runtime", tmp_path) != before
    path.write_text("def f(:\n")  # does not parse: raw bytes
    assert live_report.scorer_sha256("runtime", tmp_path) != before
    assert live_report.scorer_sha256("generator", tmp_path) is None
    assert live_report.scorer_sha256("runtime") == live_report.scorer_sha256("runtime")


_DIGEST_BASE = (
    '"""Module doc."""\n'
    "class C:\n"
    '    """Class doc."""\n'
    "    def f(self, x):\n"
    '        """Function doc."""\n'
    "        if x:\n"
    "            y = 1\n"
    "            z = 2\n"
    "        return x + 1\n"
)


@pytest.mark.parametrize("edited,same", [
    # Adding or removing a docstring is editorial.
    (_DIGEST_BASE.replace('    """Class doc."""\n', ""), True),
    (_DIGEST_BASE.replace('        """Function doc."""\n', ""), True),
    (_DIGEST_BASE.replace('"""Module doc."""\n', ""), True),
    (_DIGEST_BASE.replace('"""Module doc."""\n', '"""Module doc."""\n"""Second."""\n'),
     False),  # only the first string is the docstring
    (_DIGEST_BASE.replace('        """Function doc."""\n',
                          '        "Function" " doc."\n'), True),  # concatenated
    (_DIGEST_BASE.replace('        """Function doc."""\n',
                          '        """Function doc."""; w = 3\n'), False),
    # Indent width and line endings are layout.
    (_DIGEST_BASE.replace("    ", "  "), True),
    (_DIGEST_BASE.replace("\n", "\r\n"), True),
    (_DIGEST_BASE.rstrip("\n"), True),
    # Nesting and code are not.
    (_DIGEST_BASE.replace("            z = 2\n", "        z = 2\n"), False),
    (_DIGEST_BASE.replace("x + 1", "x + 2"), False),
])
def test_scorer_digest_ignores_docstring_and_indent_changes_but_not_nesting(
    tmp_path, edited, same
):
    path = tmp_path / "runtime/_scoring.py"
    path.parent.mkdir(parents=True)
    path.write_text(_DIGEST_BASE)
    before = live_report.scorer_sha256("runtime", tmp_path)
    path.write_bytes(edited.encode())
    assert (live_report.scorer_sha256("runtime", tmp_path) == before) is same


def test_a_harness_only_change_makes_the_row_incomparable_with_an_actionable_reason(tmp_path):
    """R3-DIGEST-DRIFT: [passed, passed, behavior_failed] against
    [passed, passed, harness_error] is not an improvement when only the
    classification code changed."""
    for name in live_report.SCORER_SOURCES["runtime"]:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("CATEGORY = 'behavior'\n")
    old = live_report.scorer_sha256("runtime", tmp_path)
    (tmp_path / "runtime/_runtime.py").write_text("CATEGORY = 'harness_error'\n")
    new = live_report.scorer_sha256("runtime", tmp_path)
    assert new != old
    baseline = _report([{**_eff_case("a", status), "scorer_sha256": old}
                        for status in ("passed", "passed", "behavior_failed")])
    candidate = _report([{**_eff_case("a", status), "scorer_sha256": new}
                         for status in ("passed", "passed", "harness_error")])
    row = live_report.compare(baseline, candidate)["cases"][0]
    assert row["comparison"] == "not_comparable" and "transition" not in row
    assert "re-run the baseline" in row["reason"] and "rescore" not in row["reason"]


def test_plugin_records_the_declaration_and_the_derived_effect(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "conftest.py").write_text(_PLUGIN_CONFTEST)
    (project / "pytest.ini").write_text(_INI)
    (project / "test_declared.py").write_text(_DECLARED_CASES)
    reports = tmp_path / "reports"
    completed = _run_pytest(project, {"CUBICLE_LIVE_EVAL_REPORT_DIR": str(reports)},
                            "-m", "live_eval")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    case = json.loads((reports / live_report.REPORT_JSON).read_text())["cases"][0]
    assert case["scorer_sha256"] == live_report.scorer_sha256("api")
    assert case["declared"]["forbidden_effects"] == ["delete_task"]
    assert case["declared"]["initial_state"] == "empty workstream"
    assert case["details"]["forbidden_effects"] == [
        "decision: called delete_task, which this case declares forbidden",
    ]
