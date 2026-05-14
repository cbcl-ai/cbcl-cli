# Cubicle Communicator — Makefile
#
# Usage:
#   make test          Run unit tests (fast, no external deps)
#   make test-int      Run integration tests (requires Redis)
#   make test-e2e      Run E2E tests (requires full stack)
#   make test-all      Run unit + integration tests
#   make test-bench    Run performance benchmarks
#
# E2E prerequisites:
#   1. docker compose up -d          (backend, postgres, redis)
#   2. cbcl setup && cbcl start      (communicator + office containers)

.PHONY: test test-unit test-int test-integration test-e2e test-e2e-flow test-e2e-multi test-all test-bench lint

# ---------------------------------------------------------------------------
# Unit tests — no external services needed (uses fakeredis/mocks)
# ---------------------------------------------------------------------------

test: test-unit

test-unit:
	@echo "=== Unit tests ==="
	python -m pytest tests/ \
		--ignore=tests/integration \
		--ignore=tests/e2e \
		--ignore=tests/benchmarks \
		-v

# ---------------------------------------------------------------------------
# Integration tests — requires Redis at localhost:6379
# ---------------------------------------------------------------------------

test-int: test-integration

test-integration:
	@echo "=== Integration tests (requires Redis) ==="
	python -m pytest tests/integration/ -v

# ---------------------------------------------------------------------------
# E2E tests — requires full stack running
#   Backend (localhost:8000), Redis (localhost:6379),
#   Communicator (cbcl start), Docker containers with Claude auth
# ---------------------------------------------------------------------------

test-e2e: test-e2e-flow test-e2e-multi

test-e2e-flow:
	@echo "=== E2E: Single task full lifecycle ==="
	python tests/e2e/test_full_flow.py

test-e2e-multi:
	@echo "=== E2E: Multi-agent parallel (5 tasks, 4 agents) ==="
	python tests/e2e/test_multi_agent.py

# ---------------------------------------------------------------------------
# All tests (unit + integration, excludes E2E which needs live stack)
# ---------------------------------------------------------------------------

test-all:
	@echo "=== All tests (unit + integration) ==="
	python -m pytest tests/ \
		--ignore=tests/e2e \
		--ignore=tests/benchmarks \
		-v

# ---------------------------------------------------------------------------
# Benchmarks — performance tests (resource-intensive)
# ---------------------------------------------------------------------------

test-bench:
	@echo "=== Benchmarks ==="
	python -m pytest tests/benchmarks/ -v

# ---------------------------------------------------------------------------
# Lint / type check
# ---------------------------------------------------------------------------

lint:
	python -m ruff check src/ tests/
	python -m mypy src/ --ignore-missing-imports
