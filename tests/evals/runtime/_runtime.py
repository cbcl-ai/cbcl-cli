"""Runtime-lane harness: the production worker path in a disposable container.

A case runs through the real ``run_sdk_session`` → ``stream_cli_session`` →
``claude --print`` in the cbcl agent image, with the real in-container MCP
server and hooks. The workspace is built with the production writers
(``ClaudeMdWriter.sync_all`` and ``prepare_instance_workspace``, mirroring
``AgentSupervisor``), the task's platform calls are answered by
``StubToolBackend``, and every CLI message is captured as the trace.

Nothing here builds the agent image, pulls an image or pushes anywhere. The
container is created with an exact ``cbcl-eval-<uuid>`` name and the
``cbcl.eval=true`` label (never ``cbcl.managed`` or the ``cbcl-office-``
prefix, so daemon cleanup cannot touch it) and removed by that exact name.
Cubicle is subscription-only, so the lane signs in with a Claude subscription
token (``CLAUDE_CODE_OAUTH_TOKEN``, made with ``claude setup-token``) and never
with an API key. The token is forwarded by NAME only
(``-e CLAUDE_CODE_OAUTH_TOKEN``); its value stays in the docker client's
environment, never in argv or logs.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
import uuid
from contextlib import aclosing
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from tests.evals import _live_report as live_report

RUNTIME_ROOT = Path(__file__).parent
CASES_DIR = RUNTIME_ROOT / "cases"
FIXTURES_DIR = RUNTIME_ROOT / "fixtures"
DEFAULT_IMAGE = "cbcl-agent:latest"
DEFAULT_CASE_TIMEOUT_SECONDS = 900
SUBSCRIPTION_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
ENV_ENABLE = "CUBICLE_EVAL_RUNTIME"
ENV_IMAGE = "CUBICLE_EVAL_AGENT_IMAGE"
ENV_ALLOW_STALE = "CUBICLE_EVAL_ALLOW_STALE_IMAGE"
ENV_STUB_HOST = "CUBICLE_EVAL_STUB_HOST"
ENV_CASE_TIMEOUT = "CUBICLE_EVAL_CASE_TIMEOUT"
ENV_MAX_COST = "CUBICLE_EVAL_MAX_COST_USD"
PROXY_HOST = "host.docker.internal"
_IDENTITY_ENV = (
    "CUBICLE_AGENT_INSTANCE_ID", "CUBICLE_PROFILE_ID", "CUBICLE_EXECUTION_ATTEMPT_ID",
    "CUBICLE_EXECUTION_CYCLE", "CUBICLE_EXECUTION_GENERATION", "CUBICLE_EXECUTION_ASSIGNEE",
    "CUBICLE_REVIEW_RETRY_EPOCH", "CUBICLE_WORKER_EXECUTION_ID", "CUBICLE_COLLECTIONS_TOKEN",
)


# ── Cases and fixtures ──────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class RuntimeCase:
    name: str
    data: dict
    sha256: str

    @property
    def id(self) -> str:
        return str(self.data["id"])

    @property
    def version(self) -> int:
        return int(self.data["version"])

    @property
    def kind(self) -> str:
        return str(self.data["kind"])


def case_names() -> list[str]:
    return sorted(path.stem for path in CASES_DIR.glob("*.json"))


def load_case(name: str) -> RuntimeCase:
    raw = (CASES_DIR / f"{name}.json").read_bytes()
    return RuntimeCase(name, json.loads(raw), hashlib.sha256(raw).hexdigest())


def tree_sha256(root: Path) -> str:
    """Stable hash over relative paths, modes' executable bit and contents."""
    digest = hashlib.sha256()
    files = (
        p for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    )
    for path in sorted(files):
        relative = path.relative_to(root).as_posix()
        executable = "x" if path.stat().st_mode & stat.S_IXUSR else "-"
        digest.update(f"{relative}\0{executable}\0".encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def fixture_sha256() -> str:
    return tree_sha256(FIXTURES_DIR)


def skill_description(skill: str) -> str:
    """The SKILL.md frontmatter description (the index text agents read).

    Parsed with the shared ``skill_metadata`` contract, exactly as the
    backend derives a catalog description, so a fixture the platform would
    reject (invalid or missing frontmatter) fails here too.
    """
    from src.skill_metadata import (
        LISTING_DESCRIPTION_CAP,
        effective_description,
        parse_skill_md,
    )

    text = (FIXTURES_DIR / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    parsed = parse_skill_md(text, skill)
    description = effective_description(
        parsed, LISTING_DESCRIPTION_CAP, frontmatter_only=True,
    )
    if description is None:
        raise ValueError(
            f"fixture skill {skill!r} has no valid frontmatter description "
            f"(status {parsed.get('status')!r}, errors {parsed.get('errors')})"
        )
    return description


# ── Workspace built with the production writers ─────────────────────


@dataclasses.dataclass
class CaseWorkspace:
    case: RuntimeCase
    root: Path
    archive_root: Path
    office_id: str
    profile: dict
    task_data: dict
    input_hashes: dict[str, str]
    placed_files: list[str] = dataclasses.field(default_factory=list)

    @property
    def cwd(self) -> str:
        return str(self.task_data["agent_workspace"])

    @property
    def output_dir(self) -> str:
        return str(self.task_data["output_dir"])

    def task_detail(self) -> dict:
        """The authoritative detail the stub serves (dispatch has committed)."""
        keys = (
            "title", "description", "reviewer", "assigned_agent", "task_class",
            "effort_hint", "rework_count", "depends_on", "priority", "readable_id",
            "workstream_id", "workstream_short_code", "workstream_name",
            "execution_cycle", "execution_generation", "review_retry_epoch", "brief",
        )
        detail = {key: self.task_data.get(key) for key in keys}
        reviewing = self.task_data.get("status") == "review"
        detail.update({
            "id": self.task_data["task_id"],
            # Dispatch committed the phase: In Progress for execution, Review
            # stays Review for the designated reviewer.
            "status": "review" if reviewing else "in_progress",
            "scope_id": None,
            "spec_revision": None,
            "recent_activities": list(self.task_data.get("recent_activities") or []),
            "artifacts": list(self.task_data.get("artifacts") or []),
            "artifacts_partial": False,
            "workstream_memory_index": "",
            # The backend declares the directory it writes to; without it the
            # worker falls back to the legacy name-derived layout (X46).
            "workstream_workspace_dir": (
                self.task_data.get("workstream_context") or {}
            ).get("workspace_dir"),
            "workstream_has_spec": False,
            "execution_blocked": False,
            "human_action_request_id": None,
        })
        return detail

    def office_files(self) -> list[dict]:
        return [
            {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, relative)), "path": relative,
             "filename": Path(relative).name, "size": (self.root / relative).stat().st_size}
            for relative in (self.placed_files or sorted(self.input_hashes))
        ]


def build_profile(case: RuntimeCase, agent: dict | None = None) -> dict:
    agent = agent or case.data["agent"]
    return {
        "name": agent["name"],
        "agent_type": "custom",
        "display_name": agent["display_name"],
        "avatar_emoji": "📊",
        "role_description": agent["role_description"],
        "system_prompt": agent["system_prompt"],
        "model": configured_model_for_runtime(),
        "effort": "xhigh",
        "allowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        "is_active": True,
        "claude_md_content": "",
        "skills": [
            {
                "name": skill,
                "display_name": skill.replace("-", " ").title(),
                "description": skill_description(skill),
                "parameter_schema": [],
            }
            for skill in agent["skills"]
        ],
        "connectors": [],
        "subagents": [],
    }


def configured_model_for_runtime() -> str:
    """Specialist preset uses the Opus family alias unless overridden."""
    override = os.environ.get("CUBICLE_EVAL_MODEL", "").strip()
    return override or "opus"


def _copy_tree(source: Path, target: Path, removed: set[str], prefix: str) -> None:
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source).as_posix()
        if f"{prefix}/{relative}" in removed:
            continue
        destination = target / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            destination.chmod(stat.S_IMODE(path.stat().st_mode) & 0o777)


REVIEWER_ROLE = "worker_reviewer"
EXECUTOR_ROLE = "worker_executor"


def case_workstreams(case: RuntimeCase) -> list[dict]:
    """The office's workstreams: ``workstreams`` (schema 2) or the single
    legacy ``workstream``."""
    return list(case.data.get("workstreams") or [case.data["workstream"]])


def task_workstream(case: RuntimeCase) -> dict:
    """The workstream the case task belongs to (``task.workstream`` names it)."""
    workstreams = case_workstreams(case)
    wanted = case.data["task"].get("workstream")
    for workstream in workstreams:
        if wanted is None or workstream["name"] == wanted:
            return workstream
    raise ValueError(f"case {case.id} task names unknown workstream {wanted!r}")


def _output_relative(path: str, output_dir: str) -> str:
    """A workspace-relative path with ``{output_dir}`` resolved."""
    resolved = path.replace("{output_dir}", output_dir)
    return str(PurePosixPath(resolved).relative_to("/workspace")) if resolved.startswith(
        "/workspace/"
    ) else resolved


def _with_output_dir(value: Any, output_dir: str) -> Any:
    return json.loads(json.dumps(value).replace("{output_dir}", output_dir))


def work_policy_block(text: str | None) -> dict | None:
    """The pinned snapshot shape the backend writes (``{text, revision, sha256}``)."""
    normalized = (text or "").replace("\r\n", "\n").strip()
    if not normalized:
        return None
    return {
        "text": normalized,
        "revision": 1,
        "sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    }


def build_case_workspace(case: RuntimeCase, base_dir: Path) -> CaseWorkspace:
    """Build the disposable workspace exactly as the daemon would.

    Schema-2 cases may add a reviewer role (the task is in Review and the
    case agent is its designated reviewer; ``executor_agent`` is the
    executor), several workstreams with instructions, an Office work policy,
    ``seeds`` (the executor's deliverables and evidence already on disk),
    ``seed_activities`` and ``artifacts``. Every input and seed is hashed as a
    protected file except those listed in ``mutable_inputs``.
    """
    from src.agent_instance_workspace import prepare_instance_workspace
    from src.config_sync.claude_md_writer import ClaudeMdWriter
    from src.orchestrator.worker_prompt import task_output_dir
    from src.paths import workstream_dir_slug

    token = uuid.uuid4().hex[:12]
    root = base_dir / f"cbcl-eval-{token}"
    archive_root = base_dir / f"snapshots-{token}"
    root.mkdir(parents=True)
    office_id = str(uuid.uuid4())
    reviewing = case.data.get("role") == REVIEWER_ROLE
    profile = build_profile(case)
    policy = work_policy_block(case.data.get("work_policy"))
    if "work_policy" in case.data:
        profile["office_work_policy"] = policy
    agents = [profile]
    if reviewing:
        agents.append(build_profile(case, case.data["executor_agent"]))

    workstream_rows = []
    for workstream in case_workstreams(case):
        workstream_rows.append({
            "id": str(uuid.uuid4()), "name": workstream["name"],
            "short_code": workstream["short_code"],
            "context_notes": workstream.get("context_notes", ""),
            "description": "", "goals": "", "priority": "medium", "status": "active",
            "workspace_dir": workstream_dir_slug(workstream["name"], workstream["short_code"]),
        })
    current = task_workstream(case)
    workstream_row = next(row for row in workstream_rows if row["name"] == current["name"])

    ClaudeMdWriter(str(root)).sync_all({
        "office_name": case.data["office_name"],
        "agents": agents,
        "workstreams": workstream_rows,
        "specs": [],
        "work_policy": policy["text"] if policy else None,
        "work_policy_revision": policy["revision"] if policy else 0,
    })

    removed = set(case.data.get("removed_skill_files") or [])
    for skill in case.data["agent"]["skills"]:
        _copy_tree(FIXTURES_DIR / "skills" / skill, root / ".claude" / "skills" / skill,
                   removed, skill)
    placed: dict[str, str] = {}
    for relative, source in sorted(case.data["inputs"].items()):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES_DIR / source, destination)
        destination.chmod(stat.S_IMODE((FIXTURES_DIR / source).stat().st_mode) & 0o777)
        placed[relative] = hashlib.sha256(destination.read_bytes()).hexdigest()

    task_id = str(uuid.uuid4())
    executor_name = case.data["task"].get("assigned_agent") if reviewing else profile["name"]
    task_data: dict[str, Any] = {
        "task_id": task_id,
        "id": task_id,
        "readable_id": f"{current['short_code']}-001.T01",
        "title": case.data["task"]["title"],
        "description": case.data["task"]["description"],
        "status": "review" if reviewing else "ready",
        "assigned_agent": executor_name,
        "reviewer": profile["name"] if reviewing else "auditor",
        "priority": "medium",
        "task_class": "assignment",
        "effort_hint": None,
        "depends_on": [],
        "rework_count": 0,
        "workstream_id": workstream_row["id"],
        "workstream_short_code": current["short_code"],
        "workstream_name": current["name"],
        "workstream_context": {
            "name": current["name"], "description": "", "goals": "",
            "short_code": current["short_code"],
            "workspace_dir": workstream_row["workspace_dir"],
        },
        "recent_activities": [],
        "artifacts": [],
        "attempt_id": str(uuid.uuid4()),
        "agent_instance_id": str(uuid.uuid4()),
        "profile_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"cubicle-eval:{profile['name']}")),
        "profile_revision": f"eval-{case.sha256[:12]}",
        "execution_cycle": 1,
        "execution_generation": 1,
        "review_retry_epoch": 0,
    }
    output_dir = task_output_dir(task_data)
    task_data["output_dir"] = output_dir
    for key, source in sorted((case.data.get("seeds") or {}).items()):
        relative = _output_relative(key, output_dir)
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES_DIR / source, destination)
        placed[relative] = hashlib.sha256(destination.read_bytes()).hexdigest()
    task_data["brief"] = _with_output_dir(case.data["task"]["brief"], output_dir)
    task_data["recent_activities"] = _with_output_dir(
        case.data.get("seed_activities") or [], output_dir,
    )
    task_data["artifacts"] = [
        {"file_path": f"/workspace/{_output_relative(path, output_dir)}",
         "file_title": PurePosixPath(path).name}
        for path in case.data.get("artifacts") or []
    ]
    task_data["agent_workspace"] = prepare_instance_workspace(
        str(root), archive_root, profile, task_data, current_skills=profile["skills"],
    )
    mutable = set(case.data.get("mutable_inputs") or [])
    input_hashes = {path: digest for path, digest in placed.items() if path not in mutable}
    return CaseWorkspace(case, root, archive_root, office_id, profile, task_data,
                         input_hashes, placed_files=sorted(placed))


def case_declaration(case: RuntimeCase) -> dict:
    """The F06-acc-8 declaration recorded with the case's report entry.

    ``allowed_tools`` names the production worker session for the case role
    (resolvable with the same selector the MCP server uses); the initial
    state lists what the workspace holds before the run; the forbidden
    effects are the stub actions the R5 safety item refuses.
    """
    data = case.data
    mode = "review" if data.get("role") == REVIEWER_ROLE else "execute"
    workstream = task_workstream(case)
    seeds = sorted(key.replace("{output_dir}", f"outputs/{workstream['short_code']}")
                   for key in data.get("seeds") or {})
    skills = data["agent"].get("skills") or []
    parts = [
        f"{data.get('role')} for '{data['task']['title']}' in workstream "
        f"'{workstream['name']}' (task {'in Review' if mode == 'review' else 'dispatched'})",
        f"inputs: {', '.join(sorted(data['inputs']))}",
    ]
    if seeds:
        parts.append(f"seeded: {', '.join(seeds)}")
    parts.append(f"skills: {', '.join(skills) if skills else 'none'}")
    if data.get("work_policy"):
        parts.append("Office work policy pinned")
    return {
        "allowed_tools": f"worker:{mode}:{data['agent']['name']}",
        "initial_state": "; ".join(parts),
        "forbidden_effects": sorted(set(data.get("forbidden_actions") or [])),
    }


def make_container_writable(root: Path) -> None:
    """Let uid 1000 in the container write the host-created workspace."""
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        path.chmod(mode | (0o777 if path.is_dir() else 0o666))


# ── Prerequisites ───────────────────────────────────────────────────


Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)


def runtime_prerequisites(
    environ: dict | None = None, runner: Runner = _run, which: Callable = shutil.which,
) -> tuple[str, str] | None:
    """``None`` when a real runtime case can run, else ``(code, detail)``."""
    environ = os.environ if environ is None else environ
    if environ.get(ENV_ENABLE) != "1":
        return ("runtime_lane_disabled",
                f"{ENV_ENABLE}=1 was not set; the runtime lane started no container")
    if not environ.get(SUBSCRIPTION_TOKEN_ENV):
        return (live_report.MISSING_CREDENTIALS,
                (f"{SUBSCRIPTION_TOKEN_ENV} is not set: the runtime lane signs "
                 "in with a Claude subscription token (claude setup-token), "
                 "never an API key"))
    if which("docker") is None:
        return ("docker_unavailable", "The docker CLI is not installed")
    version = runner(["docker", "version", "--format", "{{.Server.Version}}"])
    if version.returncode != 0:
        return ("docker_unavailable", "The docker daemon is not reachable")
    image = environ.get(ENV_IMAGE) or DEFAULT_IMAGE
    inspected = runner(["docker", "image", "inspect", image, "--format", "{{json .Config.Labels}}"])
    if inspected.returncode != 0:
        return ("agent_image_missing",
                f"Image {image} is not present locally (the lane never builds or pulls it)")
    from src._agent_image.image_hash import LABEL
    from src.docker.container_manager import _compute_mcp_server_hash

    try:
        labels = json.loads(inspected.stdout or "null") or {}
    except ValueError:
        labels = {}
    expected = _compute_mcp_server_hash()
    if labels.get(LABEL) != expected and environ.get(ENV_ALLOW_STALE) != "1":
        return ("agent_image_stale",
                f"Image {image} mcp_server_hash does not match the source ({expected[:12]})")
    return None


# ── Container lifecycle ─────────────────────────────────────────────


def agent_image_identity(environ: dict | None = None, runner: Runner = _run) -> dict:
    """Which image a runtime case ran on, for the report.

    ``matches_source`` is False when ``CUBICLE_EVAL_ALLOW_STALE_IMAGE=1``
    admitted an image built from other sources: that evidence describes the
    image, not the current source tree.
    """
    from src._agent_image.image_hash import LABEL
    from src.docker.container_manager import _compute_mcp_server_hash

    environ = os.environ if environ is None else environ
    image = environ.get(ENV_IMAGE) or DEFAULT_IMAGE
    inspected = runner(["docker", "image", "inspect", image, "--format", "{{json .Config.Labels}}"])
    labels: object = {}
    if inspected.returncode == 0:
        try:
            labels = json.loads(inspected.stdout or "null") or {}
        except ValueError:
            labels = {}
    label = labels.get(LABEL) if isinstance(labels, dict) else None
    source = _compute_mcp_server_hash()
    return {
        "image": image,
        "mcp_server_hash": label,
        "source_mcp_server_hash": source,
        "matches_source": label == source,
        "stale_allowed": environ.get(ENV_ALLOW_STALE) == "1",
    }


def container_run_command(name: str, image: str, workspace: Path, environ: dict) -> list[str]:
    command = [
        "docker", "run", "-d", "--pull", "never", "--init",
        "--name", name, "--label", "cbcl.eval=true",
        "--user", "agent", "--memory", "4g", "--cpus", "2", "--pids-limit", "512",
        "--add-host", f"{PROXY_HOST}:host-gateway",
        "-v", f"{workspace}:/workspace",
    ]
    if environ.get(SUBSCRIPTION_TOKEN_ENV):
        # Name only; the value stays in the client env.
        command += ["-e", SUBSCRIPTION_TOKEN_ENV]
    command.append(image)
    return command


def start_container(workspace: Path, runner: Runner = _run) -> str:
    name = f"cbcl-eval-{uuid.uuid4()}"
    image = os.environ.get(ENV_IMAGE) or DEFAULT_IMAGE
    result = runner(container_run_command(name, image, workspace, os.environ))
    if result.returncode != 0:
        remove_container(name, runner)
        raise live_report.EvalHarnessError(
            "runtime_infrastructure_error", "The evaluation container did not start.",
            stderr=(result.stderr or "")[-500:],
        )
    return name


def remove_container(name: str, runner: Runner = _run) -> None:
    if name.startswith("cbcl-eval-"):
        runner(["docker", "rm", "-f", name])


# ── Session ─────────────────────────────────────────────────────────


class EvalWorker:
    """The attributes ``run_sdk_session`` reads from an ``AgentWorker``."""

    def __init__(self, *, agent_name: str, office_id: str, backend_url: str,
                 workspace_path: str) -> None:
        from src._agent_worker_mcp import build_mcp_config

        self.agent_name = agent_name
        self.office_id = office_id
        self.backend_url = backend_url
        self.workspace_path = workspace_path
        self.sent: list[dict] = []
        self._sidechain_failures = 0
        self._pending_spawns = 0
        self._terminal_action_completed = None
        self._flow_consult_summary = ""
        self._build_mcp_config = functools.partial(build_mcp_config, self)

    def _send(self, message: dict) -> None:
        self.sent.append(message)


@dataclasses.dataclass
class SessionOutcome:
    trace: list[dict]
    stream_kwargs: dict
    worker_messages: list[dict]
    session_id: str | None
    total_cost: float | None
    elapsed_seconds: float
    task_data: dict = dataclasses.field(default_factory=dict)
    error: dict | None = None


_PROBE_SOURCE = (
    "import sys, urllib.error, urllib.request\n"
    "try:\n"
    "    urllib.request.urlopen(sys.argv[1], timeout=5)\n"
    "except urllib.error.HTTPError:\n"
    "    pass\n"
    "except Exception as exc:\n"
    "    print(type(exc).__name__, str(exc)[:200])\n"
    "    sys.exit(3)\n"
)


def container_proxy_url(port: int, proxy_host: str = PROXY_HOST) -> str:
    """The stub URL as the in-container MCP server sees it."""
    return f"http://{proxy_host}:{port}"


def stub_reachable_from_container(
    container_name: str, url: str, runner: Runner = _run,
) -> tuple[bool, str]:
    """Probe the stub from inside the container before a paid session starts.

    Any HTTP answer (the stub refuses GET with 405) proves reachability and is
    never logged or counted as a rejected call; a connection error or timeout
    means the MCP server would fail every Cubicle call.
    """
    probe = f"{url}/tool-call"
    result = runner([
        "docker", "exec", container_name, "python3", "-I", "-S", "-c", _PROBE_SOURCE, probe,
    ])
    detail = ((result.stdout or "") + (result.stderr or "")).strip()[-300:]
    return result.returncode == 0, detail


def stub_host() -> str:
    return os.environ.get(ENV_STUB_HOST, "").strip() or "127.0.0.1"


def host_side_url(port: int) -> str:
    host = stub_host()
    return f"http://{'127.0.0.1' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}"


async def run_case_session(
    workspace: CaseWorkspace,
    *,
    container_name: str,
    stub,
    monkeypatch,
    timeout: float,
    proxy_host: str = PROXY_HOST,
) -> SessionOutcome:
    """Run the real ``run_sdk_session`` once, capturing every CLI message."""
    from src import _agent_worker_task
    from src.docker import session_bridge
    from src.office_secrets import store as office_secret_store

    for key in _IDENTITY_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CUBICLE_TOOL_PROXY_URL", container_proxy_url(stub.port, proxy_host))
    monkeypatch.setenv("CUBICLE_TOOL_PROXY_TOKEN", stub.token)
    monkeypatch.setenv("CUBICLE_OFFICE_TOOL_SECRET", stub.office_secret)
    monkeypatch.setattr(_agent_worker_task, "_MAX_SESSION_ATTEMPTS", 1)
    monkeypatch.setattr(_agent_worker_task, "_INFRA_DEFER_DELAYS_SECONDS", ())
    monkeypatch.setattr(office_secret_store, "read_office_secrets", lambda _slug: {})

    trace: list[dict] = []
    stream_kwargs: dict = {}
    original = session_bridge.stream_cli_session

    async def tee(*args, **kwargs):
        stream_kwargs.update(kwargs)
        async with aclosing(original(*args, **kwargs)) as inner:
            async for message in inner:
                trace.append({"type": message.type, "data": message.data})
                yield message

    monkeypatch.setattr(session_bridge, "stream_cli_session", tee)
    worker = EvalWorker(
        agent_name=workspace.profile["name"],
        office_id=workspace.office_id,
        backend_url=host_side_url(stub.port),
        workspace_path=str(workspace.root),
    )
    agent_config = {**workspace.profile, "_container_name": container_name}
    task_data = json.loads(json.dumps(workspace.task_data))
    started = time.monotonic()
    session_id = total_cost = None
    error = None
    try:
        session_id, total_cost = await asyncio.wait_for(
            _agent_worker_task.run_sdk_session(worker, agent_config, task_data),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        error = {"kind": "timeout", "detail": f"session exceeded {timeout:.0f}s"}
    except _agent_worker_task.AgentErrorEscalation as exc:
        error = {"kind": "escalation", "detail": str(getattr(exc, "error_class", exc))}
    except Exception as exc:  # noqa: BLE001 - keep the partial trace for safety scoring
        error = {"kind": "exception", "detail": f"{type(exc).__name__}: {exc}"[:500]}
    deferred = task_data.get("_execution_deferred_reason")
    if deferred and error is None:
        error = {"kind": "not_admitted", "detail": str(deferred)}
    return SessionOutcome(
        trace=trace,
        stream_kwargs=stream_kwargs,
        worker_messages=worker.sent,
        session_id=session_id,
        total_cost=total_cost,
        elapsed_seconds=time.monotonic() - started,
        task_data=task_data,
        error=error,
    )


def score_and_record(case: RuntimeCase, workspace: CaseWorkspace, outcome: SessionOutcome,
                     stub_log: list[dict], stub_rejected_auth: int = 0):
    """Score one finished session and record its report details.

    Forbidden effects are recorded FIRST, from the workspace, the stub log and
    whatever trace exists, so a session that times out, escalates or ends in
    an infrastructure error after a payment or exfiltration still reports
    the effect. Then an unfinished session raises the runtime infrastructure
    error, and a finished one is scored (``score_case`` may raise the same
    error from its infrastructure gate).
    """
    from tests.evals.runtime._scoring import (
        Evidence,
        SafetyScoringError,
        parse_trace,
        safety_effects,
        score_case,
    )

    evidence = Evidence(
        trace=parse_trace(outcome.trace), stub_log=stub_log, workspace=workspace.root,
        cwd=workspace.cwd, output_dir=workspace.output_dir,
        input_hashes=workspace.input_hashes, stub_rejected_auth=stub_rejected_auth,
    )
    try:
        effects = safety_effects(case.data, evidence)
    except SafetyScoringError as error:
        live_report.record_detail("forbidden_effects", error.effects)
        raise live_report.EvalHarnessError(
            "harness_error", f"The runtime safety scorer raised: {error}",
        ) from error
    live_report.record_detail("forbidden_effects", effects)
    if outcome.error is not None:
        raise live_report.EvalHarnessError(
            "runtime_infrastructure_error",
            f"The worker session did not complete: {outcome.error['kind']}",
            **outcome.error,
        )
    try:
        score = score_case(case.data, evidence)
    except live_report.EvalHarnessError:
        raise
    except Exception as error:
        # A scorer bug is not a behaviour verdict; the effects above stay recorded.
        raise live_report.EvalHarnessError(
            "harness_error", f"The runtime scorer raised {type(error).__name__}: {error}",
        ) from error
    live_report.record_detail("rubric", score.summary())
    live_report.record_detail("forbidden_effects", score.forbidden_effects())
    return score


def case_timeout() -> float:
    try:
        return float(os.environ.get(ENV_CASE_TIMEOUT) or DEFAULT_CASE_TIMEOUT_SECONDS)
    except ValueError:
        return float(DEFAULT_CASE_TIMEOUT_SECONDS)


def observation_for(workspace: CaseWorkspace, outcome: SessionOutcome) -> live_report.CallObservation:
    from tests.evals.runtime._scoring import parse_trace

    trace = parse_trace(outcome.trace)
    result = trace.result or {}
    init = trace.init or {}
    completed = (
        outcome.error is None and trace.stream_error is None and bool(result)
        and not result.get("is_error")
    )
    kwargs = outcome.stream_kwargs
    system_prompt = str(kwargs.get("system_prompt") or "")
    return live_report.CallObservation(
        runtime=live_report.RUNTIME_AGENT_IMAGE_CLI,
        configured_model=str(workspace.profile["model"]),
        observed_model=init.get("model"),
        effort_requested=kwargs.get("effort"),
        system_prompt_sha256=live_report.sha256_text(system_prompt) if system_prompt else None,
        system_prompt_chars=len(system_prompt) or None,
        tool_count=len(init.get("tools") or []) or None,
        stop_reason=result.get("subtype"),
        usage=live_report.usage_from_provider(result.get("usage")),
        cost_usd=result.get("total_cost_usd"),
        elapsed_seconds=outcome.elapsed_seconds,
        completed=completed,
        error=outcome.error or (
            {"kind": "stream_error", "detail": str(trace.stream_error.get("error"))[:500]}
            if trace.stream_error else None
        ),
        extra={
            "claude_code_version": init.get("claude_code_version"),
            "num_turns": result.get("num_turns"),
            "session_id": outcome.session_id,
            "tool_calls": len(trace.calls),
        },
    )


def write_trace(outcome: SessionOutcome, stub_log: list[dict], case: RuntimeCase,
                trial: int, secrets: tuple[str, ...] = ()) -> str | None:
    """Persist the trace next to the report when a report dir is configured.

    The container holds the credential in its environment, so a Bash ``env``
    can copy it into a tool result: every line is redacted (credential values
    plus the stub's per-run ``secrets``) before it is written.
    """
    base = os.environ.get(live_report.ENV_REPORT_DIR)
    if not base:
        return None
    directory = Path(base) / "runtime-traces"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{case.name}-trial{trial + 1}.jsonl"
    lines = [json.dumps({"kind": "cli", **message}, default=str) for message in outcome.trace]
    lines += [json.dumps({"kind": "stub", **entry}, default=str) for entry in stub_log]
    with path.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(live_report.redact_secrets(line, secrets) + "\n")
    return str(path)
