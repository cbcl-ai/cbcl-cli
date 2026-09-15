# Cubicle Communicator

The Communicator (internally "Office Orchestrator") is a native Python CLI that
bridges the platform backend and local AI execution.  It manages Docker containers,
spawns Claude agent processes, operates the task queue, and handles the full task
lifecycle.

This source checkout may contain unreleased changes. The monorepo component and
operations references under `docs/` describe current behavior; dated test logs
and historical E2E scripts do not prove production readiness or activation.

## Quick Start

Use a separate development installation/token for development. Setup defaults
to the hosted platform; set the local platform URL deliberately. Do not run
these maintenance/auth commands against active customer offices casually.

```bash
pip install -e ".[docker,dev]"

cbcl setup          # Platform URL, build image, authenticate containers
cbcl start          # Start communicator (foreground)
cbcl start -d       # Start as daemon
cbcl status         # Show status
cbcl stop           # Stop communicator
cbcl auth           # Re-authenticate office containers
cbcl auth -o "Name" # Auth a specific office
cbcl auth --force   # Force re-auth (switch account)
```

## Architecture

```
cbcl start
  ├── FakeRedis queue/presence state (no Redis server required by default)
  ├── Host-only SQLite admission/recovery/completion ledgers
  ├── Per office:
  │   ├── Docker container (cbcl-office-{slug})
  │   ├── AgentSupervisor  (process pool — one OS process per agent)
  │   ├── TaskDispatcher    (in-process FakeRedis ZSET priority queue per agent)
  │   ├── WsTransport       (the live backend WebSocket channel)
  │   ├── BackendClient     (authenticated HTTP reads, claims and receipts)
  │   ├── Manager process   (long-lived, handles chat)
  │   └── Worker processes  (spawned per task, exit on completion)
  ├── HealthReporter (→ in-process FakeRedis every 30s)
  └── Watchdog (crash recovery)
```

Each agent runs in its own OS process, communicating via NDJSON over stdin/stdout.
The Claude CLI runs inside Docker containers via `docker exec`.

Default execution remains in the office container. Explicit local
`execution_containers.offices` UUID allowlisting plus
`acknowledge_shared_auth: true` selects private-PID attempt containers for real
task execution/review/triage only. Manager, Planner/Flow consults, generation and
managed scripts remain office-container based. The worker pool's per-office
resource budget is additional to the office container, not a combined cgroup cap.
Workspace and UUID-owned Claude auth/cache remain shared within an office.
Prompt/MCP temporary files are outside shared workspaces at
`/tmp/cbcl-session-files`; this is not complete credential isolation.

Missing review verdicts produce holds, not implicit approval. Durable completion
receipts retry reconciliation rather than rerun work; a Stop request is not a
confirmed termination receipt. Preserve runtime ledgers when investigating
uncertain processes. No production or real shared-auth acceptance is claimed here.

## Testing

### Prerequisites

| Test type | Backend | Redis | Communicator | Docker + Auth |
|-----------|---------|-------|--------------|---------------|
| Unit      |         |       |              |               |
| Integration |       | x     |              |               |
| E2E       | x       | x     | x            | x             |
| Benchmark |         |       |              |               |

Start infrastructure:
```bash
# From project root
docker compose up -d          # postgres + redis + backend
cbcl setup && cbcl start      # communicator + office containers
```

### Running Tests

```bash
cd communicator

# Monorepo unit + prompt tests (backend imports required by evals)
make test

# Integration tests — requires Redis at localhost:6379
make test-int

# E2E tests — requires full stack running
make test-e2e           # both tests
make test-e2e-flow      # single task lifecycle only
make test-e2e-multi     # multi-agent parallel only

# Unit + Integration together
make test-all

# Performance benchmarks
make test-bench
```

Or run directly with pytest / python:
```bash
# Standalone CLI unit lane; prompt evals additionally require backend imports.
python -m pytest tests/ --ignore=tests/integration --ignore=tests/e2e --ignore=tests/benchmarks --ignore=tests/evals -v

# Specific test file
python -m pytest tests/test_agent_supervisor.py -v

# E2E (standalone scripts, not pytest)
python tests/e2e/test_full_flow.py
python tests/e2e/test_multi_agent.py
```

### Test Inventory

This is an illustrative module map, not a suite count or release gate. The
monorepo `docs/06-operations/testing.md` separates mocked units, prompt/contract
tests, real Docker probes and separately authorized live AI acceptance. Default
pytest markers alone do not exclude every service-dependent test; inspect
`Makefile` and `pyproject.toml` before selecting a lane.

#### Unit Tests (`tests/test_*.py`)

| File | What it tests |
|------|--------------|
| `test_agent_protocol.py` | NDJSON IPC message serialization/deserialization |
| `test_agent_supervisor.py` | Process pool: spawn, heartbeat, crash detection, shutdown |
| `test_agent_worker.py` | Worker subprocess: task assignment, completion, cancellation |
| `test_task_dispatcher.py` | Redis ZSET queue consumer: priority ordering, dispatch, reconciliation |
| `test_session_manager_redis.py` | Manager session persistence: save, resume, context switching |
| `test_manager_controller.py` | Manager subprocess proxy: chat routing, response streaming |
| `test_script_runner.py` | Background script execution: start, monitor, progress, cleanup |
| `test_script_runner_redis.py` | Script runner with Redis event publishing |
| `test_health_reporter.py` | Periodic health reporting to Redis |
| `test_watchdog.py` | Crash recovery: stuck task detection, re-dispatch |
| `test_daemon_process_model.py` | Daemon startup, shutdown, signal handling |
| `test_handlers_process_model.py` | Event handler wiring: task_ready, task_moved, task_updated |
| `test_container_manager.py` | Docker container lifecycle: start, stop, image build |
| `test_claude_md_writer.py` | CLAUDE.md + agent/workstream config file generation |
| `test_manifest.py` / `test_variable_bindings.py` | Script manifest and variable-binding resolution |
| `test_runtime_state.py` | Durable admission, drain and recovery-state handling |
| `test_paths.py` | Path utilities: slugify, workspace paths |
| `test_daemon.py` | Daemon PID management, process detection |

#### Integration Tests (`tests/integration/`)

| File | What it tests |
|------|--------------|
| `test_full_lifecycle.py` | Full task lifecycle with real Redis (create → dispatch → complete) |
| `test_crash_recovery.py` | Agent crash → task recovery → re-dispatch |
| `test_concurrent_agents.py` | Multiple agents working simultaneously, queue contention |

#### E2E Tests (`tests/e2e/`)

These are historical standalone scripts (not the hermetic pytest unit gate)
that use **real AI agents**. They require a separately authorized disposable
full stack and compatible lifecycle fixtures. Their old unassign/reviewer
sequence is not the current no-unassign/review-hold contract; do not treat an
unrun script or its old estimated duration as current acceptance evidence.

| File | What it tests | Duration |
|------|--------------|----------|
| `test_full_flow.py` | Historical single-task live scenario; inspect/update its fixture assumptions before use | Not a current SLA |
| `test_multi_agent.py` | Historical multi-agent live scenario; not all current system agents or recovery modes | Not a current SLA |

**E2E test details — `test_multi_agent.py`:**

| Task | Agent | Tools exercised |
|------|-------|----------------|
| Research framework comparison | analyst | Write, WebSearch, `office_save_file` |
| Create project checklist | manager-assistant | Write, `office_save_file` |
| Write CSV-to-JSON script | automation-script-developer | Write, Bash, `register_script` |
| Audit workspace files | auditor | Read, Glob, Bash |
| Generate + register status report | analyst | Write, `office_save_file`, `office_attach_to_task` |

#### Benchmarks (`tests/benchmarks/`)

| File | What it tests |
|------|--------------|
| `test_performance.py` | Queue throughput, dispatch latency, message routing speed |

### Environment Variables

| Variable | Default | Used by |
|----------|---------|---------|
| `BACKEND_URL` | `http://localhost:8000` | E2E tests |
| `REDIS_URL` | `redis://localhost:6379/0` | E2E tests, integration tests |
| `E2E_STEP_TIMEOUT` | `300` (5 min) | E2E tests — max wait per lifecycle step |

### Troubleshooting E2E Tests

**"Communicator did not connect within 120s"**
The communicator needs to discover the office.  Ensure `cbcl start` is running.
If the office was just created, normal discovery polling is approximately 15s;
startup admission/credential failures may intentionally prevent readiness.

**Tasks stuck in Review**
Inspect the task's current reviewer, typed hold, execution/Stop receipt and daemon
health. A missing verdict, pending script verification, stopped admission or
uncertain process are different conditions. Use the liveness runbook; do not
blanket-restart active offices or delete recovery state to clear a Review card.

**"No connected office found"**
The `test_multi_agent.py` test requires an existing "E2E Flow Test" office.
Run `test_full_flow.py` first (it creates the office if needed).
