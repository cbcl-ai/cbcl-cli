# Cubicle Communicator

The Communicator (internally "Office Orchestrator") is a native Python CLI that
bridges the platform backend and local AI execution.  It manages Docker containers,
spawns Claude agent processes, operates the task queue, and handles the full task
lifecycle.

## Quick Start

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
  ├── Redis connection
  ├── Per office:
  │   ├── Docker container (cbcl-office-{slug})
  │   ├── AgentSupervisor  (process pool — one OS process per agent)
  │   ├── TaskDispatcher    (in-process FakeRedis ZSET priority queue per agent)
  │   ├── WsTransport       (the live backend WebSocket channel)
  │   ├── Manager process   (long-lived, handles chat)
  │   └── Worker processes  (spawned per task, exit on completion)
  ├── HealthReporter (→ in-process FakeRedis every 30s)
  └── Watchdog (crash recovery)
```

Each agent runs in its own OS process, communicating via NDJSON over stdin/stdout.
The Claude CLI runs inside Docker containers via `docker exec`.

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

# Unit tests — fast, no external deps (uses fakeredis/mocks)
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
# Unit tests
python -m pytest tests/ --ignore=tests/integration --ignore=tests/e2e --ignore=tests/benchmarks -v

# Specific test file
python -m pytest tests/test_agent_supervisor.py -v

# E2E (standalone scripts, not pytest)
python tests/e2e/test_full_flow.py
python tests/e2e/test_multi_agent.py
```

### Test Inventory

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
| `test_skill_mcp_loader.py` | Skill → MCP server config generation |
| `test_skill_env_builder.py` | Skill environment variable assembly |
| `test_worker_hooks.py` | SDK hooks: activity tracking, subagent lifecycle |
| `test_claude_md_writer.py` | CLAUDE.md + agent/workstream config file generation |
| `test_variable_injector.py` | Jinja2 script variable injection |
| `test_paths.py` | Path utilities: slugify, workspace paths |
| `test_daemon.py` | Daemon PID management, process detection |

#### Integration Tests (`tests/integration/`)

| File | What it tests |
|------|--------------|
| `test_full_lifecycle.py` | Full task lifecycle with real Redis (create → dispatch → complete) |
| `test_crash_recovery.py` | Agent crash → task recovery → re-dispatch |
| `test_concurrent_agents.py` | Multiple agents working simultaneously, queue contention |

#### E2E Tests (`tests/e2e/`)

These are standalone scripts (not pytest) that test with **real AI agents** making
actual Claude API calls.  They require the full stack running.

| File | What it tests | Duration |
|------|--------------|----------|
| `test_full_flow.py` | Single task through 12-step lifecycle: create → ready → in_progress → review → unassign → MA assigns reviewer → reviewer works → unassign → MA decision → done | ~2 min |
| `test_multi_agent.py` | 5 tasks across all 4 system agents in parallel. Tests file registration (`office_save_file`), script registration (`register_script`), artifact attachment, and full lifecycle for each. | ~6 min |

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
If the office was just created, the communicator polls every 60s.

**Tasks stuck in Review**
This was a race condition fixed in commit `4e39d20`. Ensure you're running the
latest communicator code.  Restart with `cbcl stop && cbcl start`.

**"No connected office found"**
The `test_multi_agent.py` test requires an existing "E2E Flow Test" office.
Run `test_full_flow.py` first (it creates the office if needed).
