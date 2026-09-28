"""Offline composition checks for the runtime lane (no model, no docker).

The runtime lane is only runtime evidence if it drives the production
worker path. These tests build a real workspace with the production writers,
run the real ``run_sdk_session`` against the real stub on 127.0.0.1 with
``stream_cli_session`` replaced by a recorder, and pin what the CLI would
have been given: the task Agent's instance directory, its retained skill
index, the native-tool denials, the production effort, the production worker
prompt and the proxy-only MCP environment. Container argv and prerequisite
probing are checked without starting docker.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import aiohttp
import pytest

from src.docker import session_bridge
from src.docker.container_manager import _compute_mcp_server_hash
from src.orchestrator.worker_prompt import build_worker_prompt
from tests.evals import _live_report as live_report
from tests.evals.runtime import _runtime as runtime
from tests.evals.runtime._stub_backend import StubToolBackend


def _fake_stream(recorded: dict):
    async def fake_stream_cli_session(*args, **kwargs):
        recorded.update(kwargs)
        yield session_bridge.SessionMessage("system", {
            "type": "system", "subtype": "init", "session_id": "sess-1",
            "model": "claude-opus-test", "tools": ["Read", "Bash"],
            "mcp_servers": [{"name": "cubicle-tools", "status": "connected"}],
        })
        yield session_bridge.SessionMessage("result", {
            "type": "result", "subtype": "success", "is_error": False,
            "session_id": "sess-1", "total_cost_usd": 0.01,
        })

    return fake_stream_cli_session


async def _compose(tmp_path, monkeypatch, case_name="positive_reconciliation"):
    monkeypatch.delenv("CUBICLE_EVAL_MODEL", raising=False)
    case = runtime.load_case(case_name)
    workspace = runtime.build_case_workspace(case, tmp_path)
    recorded: dict = {}
    monkeypatch.setattr(session_bridge, "stream_cli_session", _fake_stream(recorded))
    async with StubToolBackend(
        office_id=workspace.office_id, task_detail=workspace.task_detail(),
        office_files=workspace.office_files(),
    ) as stub:
        outcome = await runtime.run_case_session(
            workspace, container_name="cbcl-eval-test", stub=stub,
            monkeypatch=monkeypatch, timeout=30,
        )
    return workspace, stub, outcome, recorded


async def test_session_runs_in_the_task_agent_instance_directory(tmp_path, monkeypatch):
    workspace, stub, outcome, kwargs = await _compose(tmp_path, monkeypatch)
    assert outcome.error is None and outcome.session_id == "sess-1"
    instance = workspace.task_data["agent_instance_id"]
    assert kwargs["cwd"] == f"/workspace/agents/.instances/{instance}" == workspace.cwd
    assert kwargs["container_name"] == "cbcl-eval-test"
    host_instance = workspace.root / "agents" / ".instances" / instance
    assert (workspace.root / "CLAUDE.md").is_file()
    assert (workspace.root / "agents" / "finance-analyst" / "CLAUDE.md").is_file()
    claude_md = (host_instance / "CLAUDE.md").read_text()
    for skill in ("finance-reconciliation", "expense-claim-review"):
        assert f"`.claude/skills/{skill}/SKILL.md`" in claude_md
        assert runtime.skill_description(skill) in claude_md
        assert (host_instance / ".claude" / "skills" / skill / "SKILL.md").is_file()
    validator = host_instance / ".claude/skills/finance-reconciliation/scripts/validate.py"
    assert validator.stat().st_mode & stat.S_IXUSR
    assert (host_instance / ".claude" / "settings.json").is_file()


async def test_session_uses_production_policy_prompt_and_proxy_only_mcp(tmp_path, monkeypatch):
    workspace, stub, outcome, kwargs = await _compose(tmp_path, monkeypatch)
    for tool in ("Skill", "Task", "Agent", "Workflow"):
        assert tool in kwargs["disallowed_tools"]
    assert kwargs["effort"] == "xhigh"
    assert kwargs["settings_json"] is None
    assert kwargs["model"] == "opus"
    assert kwargs["allowed_tools"] is None
    assert kwargs["system_prompt"] == build_worker_prompt(outcome.task_data)
    assert workspace.task_data["brief"]["inputs"] in kwargs["system_prompt"]
    assert kwargs["secret_env"] is None
    assert kwargs["env_overrides"]["CLAUDE_CODE_DISABLE_WORKFLOWS"] == "1"
    env = kwargs["mcp_config"]["mcpServers"]["cubicle-tools"]["env"]
    assert env["TOOL_PROXY_URL"] == f"http://host.docker.internal:{stub.port}"
    assert env["TOOL_PROXY_TOKEN"] == stub.token
    assert "OFFICE_TOOL_SECRET" not in env
    assert env["TASK_MODE"] == "execute"
    assert env["AGENT_NAME"] == "finance-analyst"
    assert env["TASK_ID"] == workspace.task_data["task_id"]
    assert env["CUBICLE_TASK_OUTPUT_DIR"] == workspace.output_dir == "/workspace/outputs/FIN"
    # The host admission fetch reached the stub with the office secret.
    assert [(entry["route"], entry["action"]) for entry in stub.log] == [
        ("direct", "get_task_detail"),
    ]
    assert stub.rejected_auth == 0
    observation = runtime.observation_for(workspace, outcome)
    assert observation.runtime == live_report.RUNTIME_AGENT_IMAGE_CLI
    assert observation.completed and observation.effort_requested == "xhigh"


async def test_removed_skill_files_are_absent_everywhere(tmp_path, monkeypatch):
    workspace, _, outcome, _ = await _compose(tmp_path, monkeypatch, "missing_reference")
    assert outcome.error is None
    relative = Path("finance-reconciliation/references/matching-rules.md")
    instance = workspace.root / "agents/.instances" / workspace.task_data["agent_instance_id"]
    assert not (workspace.root / ".claude/skills" / relative).exists()
    assert not (instance / ".claude/skills" / relative).exists()
    assert (instance / ".claude/skills/finance-reconciliation/SKILL.md").is_file()


async def test_malformed_case_places_the_bad_ledger_at_the_brief_path(tmp_path):
    case = runtime.load_case("malformed_ledger")
    workspace = runtime.build_case_workspace(case, tmp_path)
    ledger = (workspace.root / "inputs/finance/ledger.csv").read_text()
    assert "89.9O" in ledger
    assert "/workspace/inputs/finance/ledger.csv" in workspace.task_data["brief"]["inputs"]


async def test_stub_requires_credentials_and_never_logs_them(tmp_path):
    async with StubToolBackend(office_id="office-1", task_detail={"id": "t", "status": "in_progress"}) as stub:
        base = f"http://127.0.0.1:{stub.port}"
        body = {"action": "get_task_detail", "params": {"task_id": "t"}, "_caller": {"role": "worker"}}
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{base}/tool-call", json=body) as response:
                assert response.status == 401
            async with session.post(f"{base}/api/offices/office-1/tool-call", json=body,
                                    headers={"X-Office-Secret": "wrong"}) as response:
                assert response.status == 404
            async with session.post(f"{base}/tool-call", json=body,
                                    headers={"Authorization": f"Bearer {stub.token}"}) as response:
                assert (await response.json())["id"] == "t"
            async with session.post(f"{base}/tool-call", json={"action": "delete_task", "params": {}},
                                    headers={"Authorization": f"Bearer {stub.token}"}) as response:
                assert (await response.json())["error"] is True
    assert stub.rejected_auth == 2
    serialized = json.dumps(stub.log)
    assert stub.token not in serialized and stub.office_secret not in serialized
    assert [entry["action"] for entry in stub.log] == ["get_task_detail", "delete_task"]


def test_container_command_is_disposable_bounded_and_name_only(tmp_path):
    command = runtime.container_run_command(
        "cbcl-eval-x", "cbcl-agent:latest", tmp_path,
        {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-secret-value"},
    )
    joined = " ".join(command)
    assert "oauth-secret-value" not in joined
    assert command[command.index("-e") + 1] == "CLAUDE_CODE_OAUTH_TOKEN"
    for flag in ("--pull", "--init", "--user", "--memory", "--cpus", "--pids-limit", "--add-host"):
        assert flag in command
    assert command[command.index("--label") + 1] == "cbcl.eval=true"
    assert "docker.sock" not in joined and "cbcl.managed" not in joined
    assert f"{tmp_path}:/workspace" in command
    removed = []
    runtime.remove_container("cbcl-office-real", lambda argv: removed.append(argv))
    runtime.remove_container("cbcl-eval-x", lambda argv: removed.append(argv))
    assert removed == [["docker", "rm", "-f", "cbcl-eval-x"]]


def test_container_command_never_forwards_an_api_key(tmp_path):
    """Subscription-only: the lane signs in with a Claude subscription token."""
    command = runtime.container_run_command(
        "cbcl-eval-x", "cbcl-agent:latest", tmp_path,
        {"ANTHROPIC_API_KEY": "sk-secret-value", "CLAUDE_CODE_OAUTH_TOKEN": "t"},
    )
    assert "ANTHROPIC_API_KEY" not in command
    assert [command[i + 1] for i, arg in enumerate(command) if arg == "-e"] == [
        "CLAUDE_CODE_OAUTH_TOKEN",
    ]


def _runner(outputs: dict):
    def run(argv):
        key = argv[1] if argv[1] != "image" else "image"
        code, stdout = outputs[key]
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr="")
    return run


@pytest.mark.parametrize("environ,which,outputs,expected", [
    ({}, "/usr/bin/docker", {}, "runtime_lane_disabled"),
    ({"CUBICLE_EVAL_RUNTIME": "1"}, "/usr/bin/docker", {}, live_report.MISSING_CREDENTIALS),
    ({"CUBICLE_EVAL_RUNTIME": "1", "ANTHROPIC_API_KEY": "k"}, "/usr/bin/docker", {},
     live_report.MISSING_CREDENTIALS),
    ({"CUBICLE_EVAL_RUNTIME": "1", "CLAUDE_CODE_OAUTH_TOKEN": "t"}, None, {},
     "docker_unavailable"),
    ({"CUBICLE_EVAL_RUNTIME": "1", "CLAUDE_CODE_OAUTH_TOKEN": "t"}, "/usr/bin/docker",
     {"version": (1, "")}, "docker_unavailable"),
    ({"CUBICLE_EVAL_RUNTIME": "1", "CLAUDE_CODE_OAUTH_TOKEN": "t"}, "/usr/bin/docker",
     {"version": (0, "27"), "image": (1, "")}, "agent_image_missing"),
    ({"CUBICLE_EVAL_RUNTIME": "1", "CLAUDE_CODE_OAUTH_TOKEN": "t"}, "/usr/bin/docker",
     {"version": (0, "27"), "image": (0, '{"mcp_server_hash": "stale"}')}, "agent_image_stale"),
])
def test_prerequisites_report_each_missing_piece(environ, which, outputs, expected):
    reason = runtime.runtime_prerequisites(environ, _runner(outputs), lambda _: which)
    assert reason is not None and reason[0] == expected


def test_prerequisites_accept_a_hash_matching_image_and_explicit_stale_override():
    labels = json.dumps({"mcp_server_hash": _compute_mcp_server_hash()})
    environ = {"CUBICLE_EVAL_RUNTIME": "1", "CLAUDE_CODE_OAUTH_TOKEN": "t"}
    ok = _runner({"version": (0, "27"), "image": (0, labels)})
    assert runtime.runtime_prerequisites(environ, ok, lambda _: "/usr/bin/docker") is None
    stale = _runner({"version": (0, "27"), "image": (0, '{"mcp_server_hash": "old"}')})
    allowed = {**environ, "CUBICLE_EVAL_ALLOW_STALE_IMAGE": "1"}
    assert runtime.runtime_prerequisites(allowed, stale, lambda _: "/usr/bin/docker") is None


def _local_exec(argv):
    """Run a ``docker exec <name> ...`` probe command locally instead."""
    assert argv[:2] == ["docker", "exec"] and argv[2].startswith("cbcl-eval-")
    import sys

    return subprocess.run([sys.executable, *argv[4:]], capture_output=True, text=True,
                          timeout=30, check=False)


async def test_stub_probe_detects_reachable_and_unreachable_stubs(tmp_path):
    import asyncio
    import socket

    async with StubToolBackend(office_id="o", task_detail={"id": "t"}) as stub:
        url = f"http://127.0.0.1:{stub.port}"
        reachable, _ = await asyncio.to_thread(
            runtime.stub_reachable_from_container, "cbcl-eval-x", url, _local_exec,
        )
        assert reachable
        # The probe is invisible to scoring: no logged call, no rejected auth.
        assert stub.log == [] and stub.rejected_auth == 0
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
    reachable, detail = await asyncio.to_thread(
        runtime.stub_reachable_from_container, "cbcl-eval-x",
        f"http://127.0.0.1:{closed_port}", _local_exec,
    )
    assert not reachable and detail


def test_container_proxy_url_uses_the_docker_host_alias():
    assert runtime.container_proxy_url(4321) == "http://host.docker.internal:4321"


def test_write_trace_redacts_credentials_and_stub_secrets(tmp_path, monkeypatch):
    key = "sk-ant-api03-synthetic-SECRET-value-0123456789"
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    monkeypatch.setenv(live_report.ENV_REPORT_DIR, str(tmp_path))
    env_dump = f"PATH=/usr/bin\nANTHROPIC_API_KEY={key}\nHOME=/home/agent"
    outcome = runtime.SessionOutcome(
        trace=[{"type": "user", "data": {"message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": env_dump},
        ]}}}],
        stream_kwargs={}, worker_messages=[], session_id=None, total_cost=None,
        elapsed_seconds=0.0,
    )
    stub_log = [{"seq": 1, "route": "proxy", "action": "add_activity",
                 "params": {"content": "proxy-token-XYZ-987 and " + key}, "caller": {}}]
    path = runtime.write_trace(
        outcome, stub_log, runtime.load_case("positive_reconciliation"), 0,
        secrets=("proxy-token-XYZ-987",),
    )
    written = Path(path).read_text(encoding="utf-8")
    assert key not in written and "proxy-token-XYZ-987" not in written
    assert written.count(live_report.REDACTED) == 3
    for line in written.splitlines():
        json.loads(line)


# ── F06 behavioural cases (C14): reviewer and Office-policy composition ──


async def test_reviewer_case_runs_the_designated_reviewer_on_seeded_output(tmp_path, monkeypatch):
    workspace, stub, outcome, kwargs = await _compose(
        tmp_path, monkeypatch, "review_defective_deliverable",
    )
    assert outcome.error is None and outcome.session_id == "sess-1"
    task = workspace.task_data
    assert (task["status"], task["assigned_agent"], task["reviewer"]) == (
        "review", "finance-analyst", "finance-controller",
    )
    env = kwargs["mcp_config"]["mcpServers"]["cubicle-tools"]["env"]
    assert env["TASK_MODE"] == "review" and env["AGENT_NAME"] == "finance-controller"
    # The executor's deliverable is on disk and protected from the reviewer.
    report = "outputs/FIN/reconciliation-report.json"
    assert (workspace.root / report).is_file() and report in workspace.input_hashes
    prompt = kwargs["system_prompt"]
    assert prompt == build_worker_prompt(outcome.task_data)
    assert f"/workspace/{report}" in prompt
    assert "1 unmatched (INV-2025-0917" in prompt
    # Both Profiles are materialized; the session runs as the reviewer.
    assert (workspace.root / "agents/finance-analyst/CLAUDE.md").is_file()
    assert (workspace.root / "agents/finance-controller/CLAUDE.md").is_file()
    assert [(entry["route"], entry["action"]) for entry in stub.log] == [
        ("direct", "get_task_detail"),
    ]


async def test_evidence_reuse_case_seeds_a_receipt_for_the_exact_inputs(tmp_path, monkeypatch):
    workspace, _, outcome, kwargs = await _compose(tmp_path, monkeypatch, "review_evidence_reuse")
    assert outcome.error is None
    receipt = json.loads((workspace.root / "outputs/FIN/validation.json").read_text())
    for name in ("invoices", "ledger"):
        entry = receipt["receipt"][name]
        assert entry["path"] == f"/workspace/inputs/finance/{name}.csv"
        assert entry["sha256"] == workspace.input_hashes[f"inputs/finance/{name}.csv"]
    assert "validation.json" in kwargs["system_prompt"]


@pytest.mark.parametrize("case_name,own_flag,other_flag", [
    ("work_policy_storefront", "--strict", "--ledger"),
    ("work_policy_billing", "--ledger", "--strict"),
])
async def test_policy_case_delivers_one_policy_and_this_workstreams_command(
    tmp_path, monkeypatch, case_name, own_flag, other_flag,
):
    from tests.evals.runtime._scoring import _workstream_claude_md

    workspace, _, outcome, kwargs = await _compose(tmp_path, monkeypatch, case_name)
    assert outcome.error is None
    case = workspace.case.data
    instance = workspace.root / "agents/.instances" / workspace.task_data["agent_instance_id"]
    assert case["work_policy"] in (instance / "CLAUDE.md").read_text()
    assert case["work_policy"] in (workspace.root / "agents/operations-engineer/CLAUDE.md").read_text()
    # The worker prompt names this workstream's instructions; only they carry
    # the project-specific command (the policy and the brief do not).
    instructions = _workstream_claude_md(case)
    assert instructions in kwargs["system_prompt"]
    text = (workspace.root / instructions.removeprefix("/workspace/")).read_text()
    assert own_flag in text and other_flag not in text
    assert own_flag not in kwargs["system_prompt"] and own_flag not in case["work_policy"]
    assert workspace.task_data["workstream_name"] == case["expected"]["workstream"]


async def test_stub_accepts_a_reviewer_move_only_from_review():
    stub = StubToolBackend(office_id="o", task_detail={
        "id": "t", "status": "review", "assigned_agent": "a", "reviewer": "r",
    })
    moved = stub.respond("move_task", {"task_id": "t", "new_status": "ready"})
    assert moved["new_status"] == "ready" and moved["actor"] == "r"
    refused = stub.respond("move_task", {"task_id": "t", "new_status": "done"})
    assert refused["error"] is True


_BRIEF_CRITERIA = ["Every invoice is checked.", "", "The report lists the unmatched invoices."]


def _rows(*statuses, indices=(1, 3)):
    return [{"criterion_index": index, "name": f"c{index}", "status": status, "evidence": "seen"}
            for index, status in zip(indices, statuses)]


def _details(overall="fail", rows=None, fixes=("Fix it.",), **extra):
    return {"overall": overall, "rationale": "Compared both files.",
            "criteria": _rows("pass", "fail") if rows is None else rows,
            "required_fixes": list(fixes), **extra}


# (details, new_status, refused?) — one row per rule of review_verdict.py.
VERDICT_RULES = [
    pytest.param(None, "ready", False, id="legacy_comment_only"),
    pytest.param({"comment_only": True}, "done", False, id="no_verdict_keys"),
    pytest.param(_details(), "ready", False, id="valid_return"),
    pytest.param(_details("pass", _rows("pass", "pass"), ()), "done", False, id="valid_approval"),
    pytest.param(_details("fail", _rows("pass", "fail"), ("Fix.",)), "blocked", False,
                 id="fail_to_blocked"),
    pytest.param(_details(overall="maybe"), "ready", True, id="bad_overall"),
    pytest.param({**_details(), "rationale": "  "}, "ready", True, id="blank_rationale"),
    pytest.param(_details(fixes=()), "ready", True, id="fail_without_fixes"),
    pytest.param(_details(rows=_rows("fail", indices=(1,))), "ready", True, id="partial_coverage"),
    pytest.param(_details(rows=_rows("pass", "fail", indices=(1, 1))), "ready", True,
                 id="duplicate_index"),
    pytest.param(_details(rows=_rows("pass", "fail", indices=(1, 2))), "ready", True,
                 id="index_of_a_blank_criterion"),
    pytest.param(_details(rows=[{"name": "c1", "status": "pass", "evidence": "x"},
                                {"criterion_index": 3, "name": "c3", "status": "fail",
                                 "evidence": "x"}]), "ready", True, id="mixed_indexing"),
    pytest.param(_details(rows=[{"name": "c1", "status": "pass", "evidence": "x"},
                                {"name": "c3", "status": "fail", "evidence": "x"}]), "ready",
                 False, id="legacy_unindexed_rows"),
    pytest.param(_details(rows=_rows("pass", "unsure")), "ready", True, id="bad_row_status"),
    pytest.param(_details(rows=[*_rows("pass", indices=(1,)), {
        "criterion_index": 3, "name": "c3", "status": "fail", "evidence": ""}]), "ready", True,
        id="blank_evidence"),
    pytest.param(_details("pass", _rows("pass", "partial"), ()), "done", True,
                 id="approval_with_partial_row"),
    pytest.param(_details("pass", _rows("pass", "pass"), ("Nit.",)), "done", True,
                 id="approval_with_fixes"),
    pytest.param(_details("conditional", _rows("pass", "pass"), ()), "done", False,
                 id="conditional_approval"),
    pytest.param(_details(), "done", True, id="done_with_fail"),
    pytest.param(_details("pass", _rows("pass", "pass"), ()), "ready", True, id="return_with_pass"),
    pytest.param(_details(fixes=("",)), "ready", True, id="blank_fix"),
]


@pytest.mark.parametrize("details,new_status,refused", VERDICT_RULES)
def test_stub_refuses_the_verdicts_the_backend_refuses(details, new_status, refused):
    """EV-7: the stub applies the backend's structural verdict rules and
    leaves a refused task in Review; the log records the outcome."""
    stub = StubToolBackend(office_id="o", task_detail={
        "id": "t", "status": "review", "reviewer": "r",
        "brief": {"acceptance_criteria": _BRIEF_CRITERIA},
    })
    params = {"task_id": "t", "new_status": new_status, "comment": "Decision."}
    if details is not None:
        params["verdict"] = details
    response = stub.record("move_task", params)
    assert stub.log[-1]["accepted"] is (not refused)
    if refused:
        assert response["code"] == "invalid_review_verdict"
        assert response["message"].startswith("Review verdict refused: ")
        assert stub.task_detail()["status"] == "review"
    else:
        assert response["new_status"] == new_status


@pytest.mark.parametrize("details,new_status,refused", VERDICT_RULES)
def test_stub_verdict_rules_match_the_backend(details, new_status, refused):
    """Parity with the real ``validate_review_verdict``: the same verdicts
    are refused, with the same message."""
    from tests.backend_boundary import import_backend
    from tests.evals.runtime._stub_backend import review_verdict_problem

    backend = import_backend("app.tasks.review_verdict")
    exceptions = import_backend("app.core.exceptions")
    try:
        backend.validate_review_verdict(details, new_status, _BRIEF_CRITERIA)
        backend_message = None
    except exceptions.BadRequestException as error:
        assert error.code == "invalid_review_verdict"
        backend_message = error.detail
    stub_message = review_verdict_problem(details, new_status, _BRIEF_CRITERIA)
    assert (stub_message is not None) is refused
    assert stub_message == backend_message


def test_stub_logs_unknown_actions_as_not_accepted():
    stub = StubToolBackend(office_id="o", task_detail={"id": "t", "status": "in_progress"})
    stub.record("delete_task", {"task_id": "t"})
    stub.record("add_activity", {"task_id": "t", "event_type": "checkpoint"})
    assert [(entry["action"], entry["accepted"]) for entry in stub.log] == [
        ("delete_task", False), ("add_activity", True),
    ]


@pytest.mark.parametrize("labels,allow,matches", [
    (None, None, True),               # hash-matching image
    ('{"mcp_server_hash": "old"}', "1", False),   # stale image admitted explicitly
    ("{}", None, False),              # image built without the label
])
def test_agent_image_identity_records_whether_the_image_matches_the_source(labels, allow, matches):
    source = _compute_mcp_server_hash()
    stdout = labels if labels is not None else json.dumps({"mcp_server_hash": source})
    environ = {"CUBICLE_EVAL_AGENT_IMAGE": "cbcl-agent:eval"}
    if allow:
        environ["CUBICLE_EVAL_ALLOW_STALE_IMAGE"] = allow
    identity = runtime.agent_image_identity(
        environ, lambda argv: subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=""),
    )
    assert identity["image"] == "cbcl-agent:eval"
    assert identity["source_mcp_server_hash"] == source
    assert identity["matches_source"] is matches
    assert identity["stale_allowed"] is bool(allow)
