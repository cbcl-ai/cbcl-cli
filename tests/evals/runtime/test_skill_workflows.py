"""Runtime lane: skill workflows through the real worker path (opt-in, paid).

Each case builds a disposable workspace with the production writers, starts
a ``cbcl-eval-<uuid>`` container from the local agent image, and runs the
real ``run_sdk_session`` once against the deterministic stub backend. The
verdict comes from ``_scoring.score_case`` over the CLI trace, the stub log
and the workspace — never from the model's own summary.

Requires ``CUBICLE_EVAL_RUNTIME=1``, a credential, docker and a
hash-matching image; otherwise every case is reported NOT EVALUATED. Before
the paid session, the stub is probed from inside the container; an
unreachable stub is NOT EVALUATED (``stub_unreachable``), and traces are
written with credential values redacted.
``CUBICLE_EVAL_MAX_COST_USD`` stops remaining cases once reached.
"""

from __future__ import annotations

import os
import shutil

import pytest

from tests.evals import _live_report as live_report
from tests.evals.runtime import _runtime as runtime
from tests.evals.runtime._scoring import init_summary, parse_trace
from tests.evals.runtime._stub_backend import StubToolBackend

pytestmark = [pytest.mark.live_eval, pytest.mark.runtime_eval]

_SPENT = {"usd": 0.0}


def _fixture_files(case: runtime.RuntimeCase) -> list[str]:
    """Every file the case's workspace is built from (the report hashes it)."""
    files = [f"cases/{case.name}.json"]
    agents = [case.data["agent"], case.data.get("executor_agent") or {}]
    for skill in sorted({skill for agent in agents for skill in agent.get("skills") or []}):
        skill_root = runtime.FIXTURES_DIR / "skills" / skill
        files += [
            path.relative_to(runtime.RUNTIME_ROOT).as_posix()
            for path in sorted(skill_root.rglob("*")) if path.is_file()
        ]
    sources = {*case.data["inputs"].values(), *(case.data.get("seeds") or {}).values()}
    files += [f"fixtures/{source}" for source in sorted(sources)]
    return files


def _params():
    params = []
    for name in runtime.case_names():
        case = runtime.load_case(name)
        params.append(pytest.param(name, id=name, marks=pytest.mark.eval_case(
            id=case.id, version=case.version, lane="runtime", role=case.data["role"],
            critical=bool(case.data.get("critical")), fixtures=_fixture_files(case),
            declared=runtime.case_declaration(case),
        )))
    return params


def _cost_ceiling() -> float | None:
    raw = os.environ.get(runtime.ENV_MAX_COST, "").strip()
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


@pytest.mark.timeout(1200)
@pytest.mark.parametrize("case_name", _params())
async def test_runtime_skill_workflow(case_name, eval_trial, tmp_path, monkeypatch):
    ceiling = _cost_ceiling()
    if ceiling is not None and _SPENT["usd"] >= ceiling:
        pytest.skip(live_report.not_evaluated_reason(
            "cost_ceiling_reached",
            f"{runtime.ENV_MAX_COST}={ceiling} was reached; this case started no session",
        ))
    case = runtime.load_case(case_name)
    workspace = runtime.build_case_workspace(case, tmp_path)
    runtime.make_container_writable(workspace.root)
    live_report.record_detail("fixture_sha256", runtime.fixture_sha256())
    live_report.record_detail("agent_image", runtime.agent_image_identity())
    live_report.record_detail("case_sha256", case.sha256)
    stub = StubToolBackend(
        office_id=workspace.office_id,
        task_detail=workspace.task_detail(),
        office_files=workspace.office_files(),
        host=runtime.stub_host(),
    )
    container = None
    try:
        await stub.start()
        container = runtime.start_container(workspace.root)
        reachable, detail = runtime.stub_reachable_from_container(
            container, runtime.container_proxy_url(stub.port),
        )
        if not reachable:
            pytest.skip(live_report.not_evaluated_reason(
                "stub_unreachable",
                f"the container cannot reach the stub ({detail}); set "
                f"{runtime.ENV_STUB_HOST}=0.0.0.0 or the docker bridge IP. No session started.",
            ))
        outcome = await runtime.run_case_session(
            workspace, container_name=container, stub=stub,
            monkeypatch=monkeypatch, timeout=runtime.case_timeout(),
        )
    finally:
        if container:
            runtime.remove_container(container)
        await stub.stop()

    observation = runtime.observation_for(workspace, outcome)
    live_report.record_call(observation)
    _SPENT["usd"] += float(observation.cost_usd or 0.0)
    trace = parse_trace(outcome.trace)
    live_report.record_detail("init", init_summary(trace))
    live_report.record_detail("trace_path", runtime.write_trace(
        outcome, stub.log, case, eval_trial, secrets=(stub.token, stub.office_secret),
    ))
    live_report.record_detail("stub_actions", [entry["action"] for entry in stub.log])
    # Records forbidden effects before any infrastructure error is raised.
    score = runtime.score_and_record(case, workspace, outcome, stub.log, stub.rejected_auth)
    shutil.rmtree(workspace.archive_root, ignore_errors=True)
    assert score.passed, "; ".join(f"{item.id}: {item.detail}" for item in score.failures())
