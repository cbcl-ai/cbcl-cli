"""pytest hooks that turn a behavioral-eval session into a truthful report.

Registered once from ``tests/evals/conftest.py`` (the common ancestor of both
lanes). Inert unless ``CUBICLE_LIVE_EVAL_REPORT_DIR`` or
``CUBICLE_LIVE_EVAL_REQUIRE_EXECUTION=1`` is set, so the default offline lane
pays nothing. Only items carrying the ``live_eval`` marker are reported.

Exit-code policy (``REQUIRE_EXECUTION=1`` only): a session whose selected live
cases produced zero behavioral verdicts exits ``NOT_EVALUATED_EXIT`` (10)
instead of pytest's 0/1. pytest's own 2/3/4/5 (interrupted, internal error,
usage error, nothing collected) are preserved.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest

from tests.evals import _live_report as live_report

__all__ = [
    "pytest_configure",
    "pytest_collection_modifyitems",
    "pytest_collection_finish",
    "pytest_collectreport",
    "pytest_runtest_call",
    "pytest_runtest_makereport",
    "pytest_sessionfinish",
    "pytest_terminal_summary",
]

_STATE_KEY = "_cubicle_live_eval_state"
DEFAULT_LIVE_TIMEOUT_SECONDS = 900
# ``pytest_collectreport`` receives no config object; keep the active one.
_ACTIVE_CONFIG: dict = {}


def _enabled() -> bool:
    return bool(os.environ.get(live_report.ENV_REPORT_DIR)) or (
        os.environ.get(live_report.ENV_REQUIRE_EXECUTION) == "1"
    )


def _state(config) -> dict | None:
    return getattr(config, _STATE_KEY, None) if config is not None else None


def _is_live(item) -> bool:
    return item.get_closest_marker("live_eval") is not None


def pytest_configure(config) -> None:
    _ACTIVE_CONFIG["config"] = config
    if not _enabled():
        return
    live_report.reset_registry()
    setattr(config, _STATE_KEY, {
        "started_at": live_report.utc_now(),
        "selected": {},
        "outcomes": {},
        "collection_errors": [],
        "report": None,
        "paths": None,
    })


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items) -> None:
    """Give live items a realistic per-item timeout.

    ``pyproject.toml`` sets ``timeout = 30`` for the fast suite; an Opus
    decision loop or a runtime CLI session legitimately takes minutes. Items
    that declare their own ``@pytest.mark.timeout`` keep it.
    """
    if not config.pluginmanager.hasplugin("timeout"):
        return
    seconds = int(
        os.environ.get("CUBICLE_LIVE_EVAL_TIMEOUT", DEFAULT_LIVE_TIMEOUT_SECONDS)
    )
    for item in items:
        if _is_live(item) and item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(seconds))


def _case_metadata(item) -> dict:
    marker = item.get_closest_marker("eval_case")
    kwargs = dict(marker.kwargs) if marker else {}
    base_id = kwargs.get("id") or item.nodeid.split("::", 1)[-1]
    callspec = getattr(item, "callspec", None)
    param_id = live_report.strip_trial_id(callspec.id) if callspec is not None else ""
    trial = callspec.params.get("eval_trial") if callspec is not None else None
    case_id = f"{base_id}[{param_id}]" if param_id else base_id
    try:
        source_sha = live_report.sha256_text(inspect.getsource(item.function))
    except (OSError, TypeError, AttributeError):
        source_sha = None
    fixture_hashes = {}
    root = Path(str(item.fspath)).parent
    for relative in kwargs.get("fixtures", ()) or ():
        path = root / relative
        fixture_hashes[relative] = (
            live_report.sha256_text(path.read_text(encoding="utf-8"))
            if path.is_file()
            else None
        )
    return {
        "case_id": case_id,
        "case_version": kwargs.get("version", "unversioned"),
        "lane": kwargs.get("lane"),
        "role": kwargs.get("role"),
        "critical": bool(kwargs.get("critical", False)),
        "trial": None if trial is None else int(trial) + 1,
        "nodeid": item.nodeid,
        "source_sha256": source_sha,
        "fixture_hashes": fixture_hashes,
        "fixture_sha256": (
            live_report.canonical_sha256(fixture_hashes) if fixture_hashes else None
        ),
        # The lane's shared scorer code: a change makes reports incomparable
        # even when no case version was bumped.
        "scorer_sha256": live_report.scorer_sha256(kwargs.get("lane")),
        # F06-acc-8: allowed tools, initial state and forbidden effects.
        "declared": kwargs.get("declared"),
    }


def pytest_collection_finish(session) -> None:
    state = _state(session.config)
    if state is None:
        return
    for item in session.items:
        if _is_live(item):
            state["selected"][item.nodeid] = _case_metadata(item)


def pytest_collectreport(report) -> None:
    """Record collection failures: a broken case module is not "zero cases"."""
    state = _state(_ACTIVE_CONFIG.get("config"))
    if state is None or not report.failed:
        return
    state["collection_errors"].append({
        "nodeid": report.nodeid,
        "detail": str(report.longrepr)[:2000],
    })


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    active = _state(item.config) is not None and _is_live(item)
    if active:
        live_report.begin_case(item.nodeid)
    try:
        yield
    finally:
        if active:
            live_report.end_case()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    state = _state(item.config)
    if state is None or not _is_live(item):
        return
    rep = outcome.get_result()
    phase = rep.when
    record = state["outcomes"].setdefault(item.nodeid, {"duration": 0.0})
    record["duration"] += float(getattr(rep, "duration", 0.0) or 0.0)
    if rep.skipped:
        longrepr = rep.longrepr
        if isinstance(longrepr, tuple) and len(longrepr) == 3:
            reason = longrepr[2]
        else:
            reason = None if longrepr is None else str(longrepr)
        record.update(outcome="skipped", phase=phase, skip_reason=reason)
        return
    if rep.failed:
        if record.get("outcome") == "failed":
            return  # keep the first failure (a teardown error follows the call)
        exc = call.excinfo.value if call.excinfo is not None else None
        record.update(
            outcome="failed",
            phase=phase,
            exc_type_name=type(exc).__name__ if exc is not None else None,
            exc_message=(str(exc) if exc is not None else str(rep.longrepr))[:4000],
            exc_category=getattr(exc, "category", None),
        )
        return
    if phase == "call" and record.get("outcome") is None:
        record.update(outcome="passed", phase="call")


def _build_cases(state: dict) -> list[dict]:
    cases = []
    for nodeid, metadata in state["selected"].items():
        outcome = state["outcomes"].get(nodeid, {})
        observations = live_report.observations_for(nodeid)
        if not outcome.get("outcome"):
            classified = {
                "classification": "harness_error",
                "subtype": "not_run",
                "detail": "The case was selected but never reported an outcome "
                "(interrupted session or crashed worker).",
            }
        else:
            classified = live_report.classify_case(
                outcome=outcome["outcome"],
                phase=outcome.get("phase", "call"),
                exc_type_name=outcome.get("exc_type_name"),
                exc_message=outcome.get("exc_message"),
                exc_category=outcome.get("exc_category"),
                observations=observations,
                skip_reason=outcome.get("skip_reason"),
            )
        cases.append({
            **metadata,
            **classified,
            "duration_seconds": round(outcome.get("duration", 0.0), 3),
            "calls": observations,
            "details": live_report.with_declared_effects(
                metadata.get("declared"), live_report.details_for(nodeid),
            ),
        })
    return cases


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus) -> None:
    state = _state(session.config)
    if state is None:
        return
    built = live_report.build_report(
        _build_cases(state),
        state["collection_errors"],
        started_at=state["started_at"],
        finished_at=live_report.utc_now(),
    )
    state["report"] = built
    directory = os.environ.get(live_report.ENV_REPORT_DIR)
    if directory:
        state["paths"] = live_report.write_report(directory, built)
    if (
        os.environ.get(live_report.ENV_REQUIRE_EXECUTION) == "1"
        and built["status"] == "not_evaluated"
        and int(session.exitstatus) in (0, 1)
        and (state["selected"] or state["collection_errors"])
    ):
        session.exitstatus = live_report.NOT_EVALUATED_EXIT
        state["exit_overridden"] = True


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    state = _state(config)
    if state is None or state.get("report") is None:
        return
    terminalreporter.section("cubicle live eval report")
    for line in live_report.render_markdown(state["report"]).splitlines()[:60]:
        terminalreporter.write_line(line)
    if state.get("paths"):
        terminalreporter.write_line(f"report: {state['paths'][0]}")
    if state.get("exit_overridden"):
        terminalreporter.write_line(
            f"exit status set to {live_report.NOT_EVALUATED_EXIT}: zero behavioral "
            "verdicts and CUBICLE_LIVE_EVAL_REQUIRE_EXECUTION=1"
        )
