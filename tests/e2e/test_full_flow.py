#!/usr/bin/env python3
"""E2E test: Full task lifecycle — create → execute → review → done.

Tests the complete 12-step task flow with REAL AI agents (no mocks).
Requires:
- Backend running at http://localhost:8000
- Redis running at localhost:6379
- Communicator running (cbcl start)
- Agent container running with Claude CLI auth

Usage:
    python tests/e2e/test_full_flow.py

The test reuses an existing "E2E Flow Test" office if one exists (avoids
communicator rediscovery latency).  Creates a fresh workstream per run.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

import httpx
import redis.asyncio as aioredis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("e2e")

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
STEP_TIMEOUT = int(os.environ.get("E2E_STEP_TIMEOUT", "300"))
POLL_INTERVAL = 3
TEST_OFFICE_NAME = "E2E Flow Test"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def api(method: str, path: str, body: dict | None = None):
    """Thin HTTP wrapper.  Returns parsed JSON (dict or list)."""
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=30.0) as c:
        if method == "GET":
            r = await c.get(path)
        elif method == "POST":
            r = await c.post(path, json=body)
        elif method == "PUT":
            r = await c.put(path, json=body)
        elif method == "DELETE":
            r = await c.delete(path)
        else:
            raise ValueError(method)
        if r.status_code >= 400:
            logger.error("API %s %s → %d: %s", method, path, r.status_code, r.text[:300])
        return r.json() if r.text else {}


async def tool_call(oid: str, action: str, params: dict) -> dict:
    return await api("POST", f"/api/offices/{oid}/tool-call", {"action": action, "params": params})


async def wait_status(oid: str, tid: str, target: str | set, timeout: int = STEP_TIMEOUT, desc: str = "") -> dict:
    if isinstance(target, str):
        target = {target}
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        t = await api("GET", f"/api/offices/{oid}/tasks/{tid}")
        s = t.get("status", "")
        a = t.get("assigned_agent") or "(none)"
        if s != last:
            logger.info("  [%s] status=%s agent=%s", desc, s, a)
            last = s
        if s in target:
            return t
        await asyncio.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Task did not reach {target} in {timeout}s ({desc}, last={last})")


async def wait_assigned(oid: str, tid: str, timeout: int = STEP_TIMEOUT, desc: str = "") -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t = await api("GET", f"/api/offices/{oid}/tasks/{tid}")
        a = t.get("assigned_agent") or ""
        if a:
            logger.info("  [%s] assigned to %s", desc, a)
            return t
        await asyncio.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Task not assigned in {timeout}s ({desc})")


async def wait_unassigned(oid: str, tid: str, timeout: int = STEP_TIMEOUT, desc: str = "") -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t = await api("GET", f"/api/offices/{oid}/tasks/{tid}")
        a = t.get("assigned_agent") or ""
        if not a:
            logger.info("  [%s] unassigned", desc)
            return t
        await asyncio.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Task not unassigned in {timeout}s ({desc})")


async def get_activities(oid: str, tid: str) -> list:
    d = await api("GET", f"/api/offices/{oid}/tasks/{tid}/activities?limit=100")
    return d.get("items", [])


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

async def setup_office(redis) -> dict:
    """Reuse existing E2E office or create a new one.

    Always creates a fresh workstream so previous test tasks don't interfere.
    """
    offices = await api("GET", "/api/offices")

    # Reuse existing office if communicator is already connected
    for o in offices:
        if o["name"] == TEST_OFFICE_NAME:
            oid = o["id"]
            h = await redis.get(f"office:{oid}:health")
            if h:
                logger.info("Reusing existing office %s (communicator connected)", oid[:12])
                # Create a fresh workstream for this test run
                ws = await api("POST", f"/api/offices/{oid}/workstreams", {
                    "name": f"E2E Run {int(time.time()) % 100000}",
                    "description": "Workstream for E2E testing",
                })
                logger.info("Created workstream %s", ws["id"][:12])
                agents = await api("GET", f"/api/offices/{oid}/agents")
                logger.info("Agents: %s", [a["name"] for a in agents])
                return {"id": oid, "ws_id": ws["id"], "agents": agents}
            else:
                logger.info("Existing office found but communicator not connected — recreating")
                await api("DELETE", f"/api/offices/{oid}")
                logger.info("Deleted stale office %s", oid[:8])

    # No usable office — create fresh
    office = await api("POST", "/api/offices", {
        "name": TEST_OFFICE_NAME,
        "manager_base_prompt": "E2E test manager.",
        "manager_model": "claude-sonnet-4-6",
    })
    oid = office["id"]
    logger.info("Created office %s", oid[:12])

    # Wait for communicator to discover and connect
    logger.info("Waiting for communicator to connect (up to 120s)...")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        h = await redis.get(f"office:{oid}:health")
        if h:
            logger.info("Communicator connected!")
            break
        await asyncio.sleep(3)
    else:
        raise RuntimeError("Communicator did not connect within 120s")

    ws = await api("POST", f"/api/offices/{oid}/workstreams", {
        "name": f"E2E Run {int(time.time()) % 100000}",
        "description": "Workstream for E2E testing",
    })
    logger.info("Created workstream %s", ws["id"][:12])

    agents = await api("GET", f"/api/offices/{oid}/agents")
    logger.info("Agents: %s", [a["name"] for a in agents])

    return {"id": oid, "ws_id": ws["id"], "agents": agents}


# ---------------------------------------------------------------------------
# Happy path test
# ---------------------------------------------------------------------------

async def test_happy_path(office: dict, redis) -> None:
    oid = office["id"]
    ws_id = office["ws_id"]
    passed = 0

    # STEP 1-2: Create task → Ready
    logger.info("\n=== STEP 1-2: Create task with brief ===")
    result = await tool_call(oid, "create_task", {
        "workstream_id": ws_id,
        "title": "E2E: Write a haiku about testing",
        "description": "Write a haiku about software testing.",
        "assigned_agent": "analyst",
        "priority": "high",
        "goal": "Write a haiku (5-7-5) about software testing. Save it as a file.",
        "context": "E2E test task. Haiku must follow 5-7-5 syllable pattern.",
        "inputs": "None needed.",
        "output_format": "A markdown file with the haiku, saved via save_file.",
        "acceptance_criteria": ["Haiku follows 5-7-5 pattern", "Topic is software testing", "File saved via save_file"],
        "allowed_tools": ["Read", "Write", "Glob"],
        "required_skills": [],
        "risks_and_edge_cases": "Ensure syllable count is correct.",
        "verification_steps": "Count syllables: 5, 7, 5.",
    })
    tid = result.get("task_id") or result.get("id", "")
    rid = result.get("readable_id", "")
    assert tid, f"Create failed: {result}"
    t = await wait_status(oid, tid, "ready", timeout=10, desc="Ready")
    assert t["assigned_agent"] == "analyst"
    logger.info("✓ STEP 1-2 PASSED: task %s in Ready, assigned to analyst", rid)
    passed += 1

    # STEP 3-5: Agent picks up → In Progress
    logger.info("\n=== STEP 3-5: Agent picks up task ===")
    t = await wait_status(oid, tid, "in_progress", desc="In Progress")
    assert t.get("assigned_agent") == "analyst"
    logger.info("✓ STEP 3-5 PASSED: in_progress, assigned to analyst")
    passed += 1

    # STEP 6-7: Agent completes → Review. Under the no-unassign-after-Ready
    # invariant the executor STAYS assigned through review (reviews are routed
    # by the separate `reviewer` field, not by unassigning).
    logger.info("\n=== STEP 6-7: Agent completes → Review (executor stays assigned) ===")
    t = await wait_status(oid, tid, "review", desc="Review")
    assert t.get("assigned_agent") == "analyst", (
        f"executor must stay assigned through review, got {t.get('assigned_agent')!r}"
    )
    acts = await get_activities(oid, tid)
    analyst_acts = [a for a in acts if a["actor"] == "analyst"]
    assert len(analyst_acts) > 0, "No activities from analyst"
    logger.info("✓ STEP 6-7 PASSED: Review, still assigned to analyst (%d analyst activities)", len(analyst_acts))
    passed += 1

    # STEP 8-9: The reviewer (the task's `reviewer` field — manager-assistant
    # by default) is dispatched and resolves the task DIRECTLY with move_task.
    # No unassign dance; the executor remains assigned the whole time.
    logger.info("\n=== STEP 8-9: Reviewer resolves via move_task ===")
    t = await wait_status(oid, tid, {"done", "ready"}, desc="Reviewer resolves")
    assert t.get("assigned_agent") == "analyst", (
        "executor must remain assigned through the reviewer's resolution, "
        f"got {t.get('assigned_agent')!r}"
    )
    logger.info("✓ STEP 8-9 PASSED: reviewer resolved → %s (executor still analyst)", t["status"])
    passed += 1

    # STEP 10: final status is a terminal/rework decision.
    logger.info("\n=== STEP 10: final decision ===")
    t = await wait_status(oid, tid, {"done", "ready"}, desc="Final decision")
    logger.info("✓ STEP 10 PASSED: final status=%s", t["status"])
    passed += 1

    # Final summary
    logger.info("\n=== FINAL SUMMARY ===")
    acts = await get_activities(oid, tid)
    logger.info("Total activities: %d", len(acts))
    for a in acts:
        logger.info("  [%s] %s: %s", a["event_type"][:12], a["actor"][:15], a["content"][:80])

    logger.info("\n✅ HAPPY PATH: %d/6 steps passed!", passed)
    return t["status"]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        office = await setup_office(redis)

        # Run happy path
        final_status = await test_happy_path(office, redis)

        # Redis cleanup check
        oid = office["id"]
        for agent in ["analyst", "manager-assistant", "auditor"]:
            qs = await redis.zcard(f"office:{oid}:aq:{agent}:queue")
            if qs > 0:
                logger.warning("Queue %s has %d entries (should be 0)", agent, qs)

        logger.info("\n" + "=" * 60)
        logger.info("E2E TEST COMPLETE — office kept for inspection")
        logger.info("Office ID: %s", office["id"])
        logger.info("=" * 60)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except TimeoutError as e:
        logger.error("TIMEOUT: %s", e)
        sys.exit(1)
    except AssertionError as e:
        logger.error("ASSERTION FAILED: %s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Interrupted")
        sys.exit(130)
