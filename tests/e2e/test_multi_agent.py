#!/usr/bin/env python3
"""E2E test: Multi-agent parallel tasks with file and script tool testing.

Creates 5 tasks for all 4 system agents (analyst, auditor, automation-script-
developer, manager-assistant) plus one that exercises file registration and
script registration tools.  All tasks run in parallel and are tracked through
the full lifecycle to Done.

Requires:
- Backend running at http://localhost:8000
- Redis running at localhost:6379
- Communicator running (cbcl start)
- Agent container running with Claude CLI auth

Usage:
    python tests/e2e/test_multi_agent.py
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
logger = logging.getLogger("e2e-multi")

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
STEP_TIMEOUT = int(os.environ.get("E2E_STEP_TIMEOUT", "300"))
POLL_INTERVAL = 3
TEST_OFFICE_NAME = "E2E Flow Test"


# ---------------------------------------------------------------------------
# Helpers  (same as test_full_flow.py)
# ---------------------------------------------------------------------------

async def api(method: str, path: str, body: dict | None = None):
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


async def get_activities(oid: str, tid: str) -> list:
    d = await api("GET", f"/api/offices/{oid}/tasks/{tid}/activities?limit=200")
    return d.get("items", [])


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

async def setup_office(redis) -> dict:
    offices = await api("GET", "/api/offices")
    for o in offices:
        if o["name"] == TEST_OFFICE_NAME:
            oid = o["id"]
            h = await redis.get(f"office:{oid}:health")
            if h:
                logger.info("Reusing existing office %s", oid[:12])
                ws = await api("POST", f"/api/offices/{oid}/workstreams", {
                    "name": f"Multi-Agent {int(time.time()) % 100000}",
                    "description": "Multi-agent E2E test workstream",
                })
                agents = await api("GET", f"/api/offices/{oid}/agents")
                logger.info("Agents: %s", [a["name"] for a in agents])
                return {"id": oid, "ws_id": ws["id"], "agents": agents}

    raise RuntimeError(
        f"No connected '{TEST_OFFICE_NAME}' office found. "
        "Run test_full_flow.py first or create the office manually."
    )


# ---------------------------------------------------------------------------
# Task definitions
# ---------------------------------------------------------------------------

def make_task_defs(ws_id: str) -> list[dict]:
    """Return 5 task definitions — one per agent, with tool-testing tasks."""
    return [
        # 1. Analyst — research task (uses WebSearch/WebFetch)
        {
            "workstream_id": ws_id,
            "title": "E2E: Research top 3 Python web frameworks",
            "description": "Research and compare the top 3 Python web frameworks.",
            "assigned_agent": "analyst",
            "priority": "high",
            "goal": "Produce a brief comparison of Django, FastAPI, and Flask. Save as a file.",
            "context": "E2E test. Produce a concise markdown comparison table.",
            "inputs": "None — use your knowledge.",
            "output_format": "Markdown file at /workspace/outputs/framework-comparison.md with a table.",
            "acceptance_criteria": [
                "Covers Django, FastAPI, Flask",
                "Includes comparison criteria (performance, learning curve, ecosystem)",
                "File saved to /workspace/outputs/",
            ],
            "allowed_tools": ["Read", "Write", "Glob", "Grep", "WebSearch", "WebFetch"],
            "required_skills": [],
            "risks_and_edge_cases": "Keep it concise — max 50 lines.",
            "verification_steps": "Read the output file and verify all 3 frameworks are covered.",
        },
        # 2. Manager Assistant — quick lookup + file save
        {
            "workstream_id": ws_id,
            "title": "E2E: Create a project checklist template",
            "description": "Create a reusable project checklist template file.",
            "assigned_agent": "manager-assistant",
            "priority": "medium",
            "goal": "Write a project kickoff checklist template and save it as a file.",
            "context": "E2E test. Simple file creation task for the Manager Assistant.",
            "inputs": "None needed.",
            "output_format": "Markdown file at /workspace/outputs/project-checklist.md",
            "acceptance_criteria": [
                "Checklist has at least 8 items",
                "Covers planning, execution, review phases",
                "File saved to /workspace/outputs/",
            ],
            "allowed_tools": ["Read", "Write", "Glob", "Grep"],
            "required_skills": [],
            "risks_and_edge_cases": "Keep it practical and generic.",
            "verification_steps": "Read the file, count checklist items.",
        },
        # 3. Automation Script Developer — write a script + register it
        {
            "workstream_id": ws_id,
            "title": "E2E: Write a CSV-to-JSON converter script",
            "description": "Write a Python script that converts CSV to JSON.",
            "assigned_agent": "automation-script-developer",
            "priority": "medium",
            "goal": "Write a Python script at /workspace/.scripts/csv-to-json/script.py that converts CSV to JSON. Register the script via register_script tool.",
            "context": "E2E test. Script must use {{INPUT_FILE}} and {{OUTPUT_FILE}} placeholders.",
            "inputs": "None — write from scratch.",
            "output_format": "Python script at /workspace/.scripts/csv-to-json/script.py and script registered via register_script.",
            "acceptance_criteria": [
                "Script file exists at /workspace/.scripts/csv-to-json/script.py",
                "Script uses {{INPUT_FILE}} and {{OUTPUT_FILE}} variable placeholders",
                "Script registered via register_script tool",
                "Includes error handling for missing files",
            ],
            "allowed_tools": ["Read", "Write", "Bash", "Glob", "Grep"],
            "required_skills": [],
            "risks_and_edge_cases": "Ensure variable placeholders use double-brace syntax.",
            "verification_steps": "Read script.py and verify it has proper structure and placeholders.",
        },
        # 4. Auditor — verify an existing file (reads outputs from task 1)
        {
            "workstream_id": ws_id,
            "title": "E2E: Audit workspace output files",
            "description": "List all files in /workspace/outputs/ and verify they are valid markdown.",
            "assigned_agent": "auditor",
            "priority": "low",
            "goal": "List files in /workspace/outputs/, read each .md file, verify they are valid markdown.",
            "context": "E2E test. Audit whatever files exist in the outputs directory.",
            "inputs": "/workspace/outputs/ directory.",
            "output_format": "Activity checkpoint listing each file found and whether it is valid markdown.",
            "acceptance_criteria": [
                "All .md files in /workspace/outputs/ are checked",
                "Report posted as activity checkpoint",
            ],
            "allowed_tools": ["Read", "Glob", "Grep", "Bash"],
            "required_skills": [],
            "risks_and_edge_cases": "Directory may be empty if other tasks haven't completed yet.",
            "verification_steps": "Check that at least one activity checkpoint was posted.",
        },
        # 5. Analyst — file registration + artifact attachment test
        {
            "workstream_id": ws_id,
            "title": "E2E: Generate and register a status report",
            "description": "Write a status report file, register it via office_save_file, and attach it to this task.",
            "assigned_agent": "analyst",
            "priority": "low",
            "goal": "Write /workspace/outputs/status-report.md, register it via save_file tool, and attach it to this task as an artifact.",
            "context": "E2E test. Tests the file registration and artifact attachment flow.",
            "inputs": "None needed.",
            "output_format": "Markdown file registered and attached as task artifact.",
            "acceptance_criteria": [
                "File exists at /workspace/outputs/status-report.md",
                "File registered via save_file tool",
                "File attached to this task as artifact",
            ],
            "allowed_tools": ["Read", "Write", "Glob", "Grep"],
            "required_skills": [],
            "risks_and_edge_cases": "Ensure file path starts with /workspace/.",
            "verification_steps": "Verify file is registered and attached via activities.",
        },
    ]


# ---------------------------------------------------------------------------
# Task lifecycle tracker
# ---------------------------------------------------------------------------

async def track_task_lifecycle(
    oid: str, tid: str, rid: str, agent: str, title: str,
) -> dict:
    """Track a single task through the full lifecycle.

    Returns a result dict with pass/fail status and timing.
    """
    tag = f"{rid}({agent})"
    start = time.monotonic()
    result = {"rid": rid, "agent": agent, "title": title, "steps": {}}

    try:
        # 1. Wait for in_progress
        t = await wait_status(oid, tid, "in_progress", desc=f"{tag} in_progress")
        result["steps"]["in_progress"] = True
        logger.info("  ✓ %s picked up by %s", tag, t.get("assigned_agent", "?"))

        # 2. Wait for review (agent completes)
        t = await wait_status(oid, tid, "review", desc=f"{tag} review")
        result["steps"]["review"] = True
        logger.info("  ✓ %s reached review", tag)

        # 3. Wait for done (full review cycle via MA)
        t = await wait_status(oid, tid, {"done", "ready"}, desc=f"{tag} done")
        result["steps"]["final"] = True
        final_status = t["status"]
        logger.info("  ✓ %s final status: %s", tag, final_status)

        elapsed = time.monotonic() - start
        result["status"] = "PASS"
        result["final_status"] = final_status
        result["elapsed"] = elapsed
        logger.info("  ✅ %s PASSED in %.0fs", tag, elapsed)

    except (TimeoutError, AssertionError) as exc:
        elapsed = time.monotonic() - start
        result["status"] = "FAIL"
        result["error"] = str(exc)
        result["elapsed"] = elapsed
        logger.error("  ❌ %s FAILED after %.0fs: %s", tag, elapsed, exc)

    return result


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------

async def verify_files(oid: str) -> dict:
    """Check that expected output files were registered."""
    logger.info("\n=== Verifying file registrations ===")
    files = await tool_call(oid, "office_list_files", {"limit": 50})
    file_list = files.get("files", [])
    logger.info("  Registered files: %d", len(file_list))
    for f in file_list:
        logger.info("    - %s (%s) [%s]", f.get("title", "?"), f.get("file_type", "?"), ", ".join(f.get("tags", [])))
    return {"file_count": len(file_list), "files": file_list}


async def verify_scripts(oid: str) -> dict:
    """Check that the CSV-to-JSON script was registered."""
    logger.info("\n=== Verifying script registrations ===")
    scripts = await api("GET", f"/api/offices/{oid}/scripts")
    logger.info("  Registered scripts: %d", len(scripts))
    for s in scripts:
        logger.info("    - %s (%s)", s.get("display_name", "?"), s.get("name", "?"))
    csv_scripts = [s for s in scripts if "csv" in s.get("name", "").lower() or "csv" in s.get("display_name", "").lower()]
    return {"script_count": len(scripts), "csv_script_found": len(csv_scripts) > 0}


async def verify_activities(oid: str, tid: str, agent: str) -> int:
    """Return count of activities from the given agent for the task."""
    acts = await get_activities(oid, tid)
    agent_acts = [a for a in acts if a.get("actor") == agent]
    return len(agent_acts)


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------

async def test_multi_agent(office: dict, redis) -> list[dict]:
    oid = office["id"]
    ws_id = office["ws_id"]
    task_defs = make_task_defs(ws_id)

    # --- Create all 5 tasks ---
    logger.info("\n" + "=" * 60)
    logger.info("  CREATING %d TASKS", len(task_defs))
    logger.info("=" * 60)

    tasks = []
    for i, td in enumerate(task_defs):
        result = await tool_call(oid, "create_task", td)
        tid = result.get("task_id") or result.get("id", "")
        rid = result.get("readable_id", "")
        assert tid, f"Create failed for task {i+1}: {result}"
        logger.info("  [%d/%d] Created %s → %s (agent=%s)",
                     i + 1, len(task_defs), rid, td["title"][:50], td["assigned_agent"])
        tasks.append({
            "id": tid,
            "rid": rid,
            "agent": td["assigned_agent"],
            "title": td["title"],
        })

    # --- Track all tasks through lifecycle in parallel ---
    logger.info("\n" + "=" * 60)
    logger.info("  TRACKING %d TASKS (parallel)", len(tasks))
    logger.info("=" * 60)

    trackers = [
        track_task_lifecycle(oid, t["id"], t["rid"], t["agent"], t["title"])
        for t in tasks
    ]
    results = await asyncio.gather(*trackers)

    # --- Verify tools ---
    file_check = await verify_files(oid)
    script_check = await verify_scripts(oid)

    # --- Activity spot checks ---
    logger.info("\n=== Activity counts per task ===")
    for t, r in zip(tasks, results):
        count = await verify_activities(oid, t["id"], t["agent"])
        logger.info("  %s (%s): %d activities from %s", t["rid"], r.get("status", "?"), count, t["agent"])

    return results, file_check, script_check


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        office = await setup_office(redis)
        results, file_check, script_check = await test_multi_agent(office, redis)

        # --- Summary ---
        logger.info("\n" + "=" * 60)
        logger.info("  MULTI-AGENT TEST SUMMARY")
        logger.info("=" * 60)

        passed = sum(1 for r in results if r["status"] == "PASS")
        failed = sum(1 for r in results if r["status"] == "FAIL")

        for r in results:
            icon = "✅" if r["status"] == "PASS" else "❌"
            elapsed = r.get("elapsed", 0)
            err = f" — {r['error'][:60]}" if r.get("error") else ""
            logger.info(
                "  %s %s (%s): %s in %.0fs%s",
                icon, r["rid"], r["agent"], r["status"], elapsed, err,
            )

        logger.info("")
        logger.info("  Files registered: %d", file_check["file_count"])
        logger.info("  CSV script found: %s", "Yes" if script_check["csv_script_found"] else "No")
        logger.info("")
        logger.info("  Tasks: %d passed, %d failed out of %d", passed, failed, len(results))

        # Redis queue cleanup check
        oid = office["id"]
        for agent in ["analyst", "manager-assistant", "auditor", "automation-script-developer"]:
            qs = await redis.zcard(f"office:{oid}:aq:{agent}:queue")
            if qs > 0:
                logger.warning("  Queue '%s' has %d leftover entries", agent, qs)

        logger.info("\n" + "=" * 60)
        logger.info("  Office ID: %s", office["id"])
        logger.info("=" * 60)

        if failed > 0:
            sys.exit(1)

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
