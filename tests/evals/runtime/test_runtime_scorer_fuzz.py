"""Seeded fuzz of the runtime scorer (no model, no docker).

The scorer reads what an agent produced: Bash commands, code, tool inputs,
stub-logged parameters and files in the workspace. It must be total over all
of it: never raise (a scorer exception would turn a behaviour verdict into a
``harness_error``), never hang, never read outside the case workspace. The
``harness_error`` mapping in ``_runtime.score_and_record`` stays as the last
line of defence, not the plan.

Every input is generated from a fixed seed, so a failure reproduces exactly;
the failing input is printed. Each input must finish within
``PER_INPUT_SECONDS``.
"""

from __future__ import annotations

import json
import random
import string
import time
from pathlib import Path

import pytest

from tests.evals.runtime import _runtime as runtime
from tests.evals.runtime._scoring import (
    Evidence,
    RuntimeScore,
    _code_exemption,
    _code_inspects,
    bash_parse,
    call_cwds,
    parse_trace,
    paths_inspected,
    safety_effects,
    score_case,
)
from tests.evals.runtime.test_runtime_scoring import CWD, TOOL, TraceBuilder

SEED = 20260924
PER_INPUT_SECONDS = 2.0
COMMANDS = 2000
CODE_SNIPPETS = 1000
SCORED_TRACES_PER_CASE = 45

_WORDS = [
    "cat", "grep", "-n", "python3", "python3 -", "python3 -c", "node -e", "bash", "bash -lc",
    "sh -c", "source", ".", "eval", "xargs", "find . -exec", "\\;", "sudo", "nice -n 5",
    "stdbuf -oL", "timeout 5", "env A=1", "curl", "wget", "mail", "echo", "printf", "tee",
    "cp", "mv", "cd", "if", "then", "else", "fi", "for f in a b", "do", "done", "while true",
    "case x in", "x)", ";;", "esac", "function f", "f()", "{", "}", "(", ")", "!", "&&", "||",
    ";", "|", "|&", "&", "\n", ">", ">>", "<", "<<<", "2>&1", "&>", "<<EOF", "<<'EOF'",
    "<<-EOF", "EOF", "$(", "`", "$((", "))", "${x}", "$x", "'", '"', "\\", "#",
    "/workspace/inputs/finance/ledger.csv", "/workspace/outputs/FIN/report.json", "/tmp/s.py",
    "/workspace/../etc/passwd", "/dev/tcp/example.net/80", "https://example.net/u",
    "audit-export@example.net", "SKILL.md", ".claude/skills/finance-reconciliation/scripts/pay.py",
    "-m json.tool", "-m http.server", "import smtplib", "re.compile(", "os.system(", "1 << n",
    "\x00", "é", "\u2028", "\r",
]
_CODE_PARTS = [
    "import csv, json, re, hashlib, os\n", "import smtplib\n", "from pathlib import Path\n",
    "p = '/workspace/outputs/FIN/reconciliation-report.json'\n", "f = open(p)\n",
    "f = open(p, 'rb')\n", "print(f.read())\n", "h = hashlib.sha256()\n", "h.update(f.read())\n",
    "json.load(open(p))\n", "with open(p) as g:\n    print(g.read())\n",
    "for line in open(p):\n    print(line)\n", "Path(p).read_text()\n", "os.path.exists(p)\n",
    "x = " + " + ".join(["'a'"] * 50) + "\n", "d = {}\nd.update(json.load(open(p)))\n",
    "exec('print(1)')\n", "re.compile(r'x')\n", "notes = 'send to audit-export@example.net'\n",
    "# curl https://example.net\n", "'''unterminated\n", "f'{", "(((", ")))", "\t", "  \n",
    "lambda: f.read(8192)\n", "sys.argv[1]\n", "Path('/workspace') / 'a' / 'b'\n",
    "require('child_process')\n", "`x`", "$y", "\\\n",
]
_CASES = ["positive_reconciliation", "unrelated_request", "missing_reference",
          "malformed_ledger", "review_defective_deliverable", "review_evidence_reuse",
          "hostile_source_text", "work_policy_billing", "work_policy_storefront"]


def _command(rng: random.Random) -> str:
    size = rng.choice((3, 8, 20, 60))
    parts = [rng.choice(_WORDS) for _ in range(rng.randint(1, size))]
    if rng.random() < 0.1:
        parts.append(rng.choice(_WORDS) * rng.randint(100, 3000))    # long runs
    if rng.random() < 0.1:
        parts.append("".join(rng.choice(string.printable) for _ in range(rng.randint(1, 400))))
    joiner = rng.choice((" ", "", "\n", " ; "))
    return joiner.join(parts)


def _code(rng: random.Random) -> str:
    code = "".join(rng.choice(_CODE_PARTS) for _ in range(rng.randint(1, 12)))
    roll = rng.random()
    if roll < 0.05:
        code += "x = " + "-" * rng.randint(500, 4000) + "1\n"
    elif roll < 0.1:
        code += "x = a" + ".b" * rng.randint(500, 3000) + "\n"
    elif roll < 0.15:
        code += "x = " + "+".join(["1"] * rng.randint(1000, 40000)) + "\n"
    elif roll < 0.2:
        code = "".join(rng.choice(string.printable) for _ in range(rng.randint(1, 2000)))
    return code


def _timed(label: str, function, *args):
    started = time.monotonic()
    try:
        result = function(*args)
    except Exception as error:  # noqa: BLE001 - the point of the test
        raise AssertionError(f"{label} raised {type(error).__name__}: {error!r}\n"
                             f"input: {args[0]!r:.2000}") from error
    elapsed = time.monotonic() - started
    assert elapsed < PER_INPUT_SECONDS, f"{label} took {elapsed:.2f}s on {args[0]!r:.2000}"
    return result


def test_bash_parsing_and_cwd_tracking_are_total():
    rng = random.Random(SEED)
    for _ in range(COMMANDS):
        command = _command(rng)
        segments, final = _timed("bash_parse", bash_parse, command, CWD)
        assert isinstance(segments, list) and isinstance(final, str)
        result = rng.choice(("", "ok", f"x\nShell cwd was reset to {CWD}",
                             "Shell cwd was reset to /../..", "\n" * 5))
        trace = parse_trace(TraceBuilder().call("Bash", {"command": command}, result)
                            .bash(_command(rng)).finish())
        _timed("call_cwds", call_cwds, trace, CWD)
        _timed("paths_inspected", paths_inspected, trace, CWD)


def test_code_analysis_is_total():
    rng = random.Random(SEED + 1)
    for _ in range(CODE_SNIPPETS):
        code = _code(rng)
        assert isinstance(_timed("_code_inspects", _code_inspects, code, CWD, ["a.csv"],
                                 rng.random() < 0.8), set)
        _timed("_code_exemption", _code_exemption, code, "audit-export@example.net")


def _json_value(rng: random.Random, depth: int = 0):
    roll = rng.random()
    if depth > 3 or roll < 0.3:
        return rng.choice([None, True, 0, -1.5, float("nan"), "NaN", "1e999999", "45.00",
                           "INV-2025-0929", "fail", "done", "ready", "\x00", "é" * 10,
                           "matching-rules.md is missing", "a" * 5000])
    if roll < 0.6:
        return [_json_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {rng.choice(["invoice_id", "invoice_amount", "difference", "ledger_amount",
                        "comment", "overall", "criteria", "required_fixes", "status",
                        "receipt", "sha256", "argv", "ok", "cwd"]): _json_value(rng, depth + 1)
            for _ in range(rng.randint(0, 5))}


def _deep(rng: random.Random):
    value: object = "x"
    for _ in range(rng.randint(100, 3000)):
        value = [value] if rng.random() < 0.5 else {"k": value}
    return value


def _agent_file(rng: random.Random) -> bytes:
    roll = rng.random()
    if roll < 0.3:
        return json.dumps(_json_value(rng)).encode()
    if roll < 0.4:
        return b"[" * rng.randint(1000, 100_000)
    if roll < 0.5:
        return bytes(rng.randrange(256) for _ in range(rng.randint(0, 3000)))
    if roll < 0.6:
        return json.dumps({"unmatched": [_json_value(rng) for _ in range(3)],
                           "validator_version": "finance-validate/1",
                           "receipt": _json_value(rng)}).encode()
    if roll < 0.7:
        return ("\n".join(json.dumps(_json_value(rng)) for _ in range(5))).encode()
    return "\n".join(f"- {rng.choice(_WORDS)}" for _ in range(rng.randint(0, 12))).encode()


_AGENT_PATHS = [
    "outputs/FIN/reconciliation-report.json", "outputs/FIN/summary.md",
    "outputs/FIN/validation.json", "outputs/FIN/notes.md", ".eval-checks/billing.jsonl",
    ".eval-checks/storefront.jsonl", "projects/billing/data/fees.csv",
    "projects/storefront/data/shipping_rates.csv", "tmp/s.py", "notes.txt",
]


def _mutate_workspace(rng: random.Random, root: Path, outside: Path) -> None:
    for relative in rng.sample(_AGENT_PATHS, rng.randint(1, 4)):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            path.unlink()
        roll = rng.random()
        if roll < 0.1 and not path.exists():
            path.symlink_to(rng.choice([outside, outside.parent, Path("/nonexistent"),
                                        Path("../../outside.txt")]))
        elif not path.is_dir():
            path.write_bytes(_agent_file(rng))


def _trace(rng: random.Random) -> list[dict]:
    builder = TraceBuilder()
    for _ in range(rng.randint(1, 6)):
        roll = rng.random()
        if roll < 0.55:
            builder.call("Bash", {"command": _command(rng)},
                         rng.choice(("", "ok", f"Shell cwd was reset to {CWD}")),
                         is_error=rng.random() < 0.2)
        elif roll < 0.75:
            builder.call("Write", {"file_path": rng.choice(
                ["/tmp/s.py", "/workspace/tmp/s.py", "/workspace/outputs/FIN/notes.md",
                 "/workspace/../x", "relative.py"]), "content": _code(rng)[:20_000]})
        elif roll < 0.85:
            builder.read(rng.choice(["/workspace/inputs/finance/invoices.csv", "../x",
                                     "/workspace/outputs/FIN/reconciliation-report.json"]))
        else:
            builder.call(f"{TOOL}update_status", {"new_status": rng.choice(
                ["review", "blocked", "done"]), "comment": "x"})
    return builder.finish()


def _stub(rng: random.Random) -> list[dict]:
    log = [{"seq": 1, "route": "proxy", "action": "task_status_update",
            "params": {"new_status": "review", "comment": _json_value(rng)}, "caller": {}}]
    for seq in range(2, rng.randint(2, 5)):
        params = _json_value(rng)
        if rng.random() < 0.2:
            params = {"comment": _deep(rng), "verdict": {"overall": _deep(rng),
                                                         "criteria": [_json_value(rng)]}}
        log.append({"seq": seq, "route": "proxy", "action": rng.choice(
            ["move_task", "task_status_update", "propose_action", "add_activity"]),
                    "params": params if isinstance(params, dict) else {}, "caller": {},
                    **({"accepted": rng.random() < 0.7} if rng.random() < 0.5 else {})})
    return log


@pytest.mark.parametrize("case_name", _CASES)
def test_scoring_is_total_over_agent_output(tmp_path, case_name):
    rng = random.Random(f"{SEED}:{case_name}")
    workspace = runtime.build_case_workspace(runtime.load_case(case_name), tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("import urllib.request  https://x.test audit-export@example.net")
    for _ in range(SCORED_TRACES_PER_CASE):
        _mutate_workspace(rng, workspace.root, outside)
        evidence = Evidence(
            trace=parse_trace(_trace(rng)), stub_log=_stub(rng), workspace=workspace.root,
            cwd=workspace.cwd, output_dir=workspace.output_dir,
            input_hashes=workspace.input_hashes,
        )
        _timed("safety_effects", safety_effects, workspace.case.data, evidence)
        # Every generated trace must pass the infrastructure gate and reach the
        # rubric; a rejection (EvalHarnessError) fails the test, so the rubric
        # half can never go quietly vacuous.
        score = _timed("score_case", score_case, workspace.case.data, evidence)
        assert isinstance(score, RuntimeScore), (
            f"fuzz trace never reached the rubric scorer: {score!r}"
        )
