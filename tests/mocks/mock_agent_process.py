"""Mock agent process for integration testing.

Simulates agent behavior without calling the Claude API.
Controlled via environment variables.

Usage (spawned by AgentSupervisor with modified command):
    python communicator/tests/mocks/mock_agent_process.py --role worker --agent-name test
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def send(msg: dict) -> None:
    """Write an NDJSON message to stdout."""
    sys.stdout.write(json.dumps(msg, separators=(",", ":"), default=str) + "\n")
    sys.stdout.flush()


def recv() -> dict | None:
    """Read one NDJSON message from stdin. Returns None on EOF."""
    line = sys.stdin.readline()
    if not line:
        return None
    try:
        return json.loads(line.strip())
    except json.JSONDecodeError:
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=["manager", "worker"], required=True)
    parser.add_argument("--agent-name", required=True)
    parser.add_argument("--workspace-path", default="/tmp/mock-workspace")
    parser.add_argument("--office-id", default="test-office")
    parser.add_argument("--backend-url", default="")
    args = parser.parse_args()

    delay = float(os.environ.get("MOCK_DELAY", "2"))
    crash_after = os.environ.get("MOCK_CRASH_AFTER")
    hang = os.environ.get("MOCK_HANG")
    progress_steps = int(os.environ.get("MOCK_PROGRESS_STEPS", "3"))
    response_chunks = int(os.environ.get("MOCK_RESPONSE_CHUNKS", "3"))
    exit_code = int(os.environ.get("MOCK_EXIT_CODE", "0"))

    # Send ready message
    send({
        "type": "ready",
        "pid": os.getpid(),
        "agent_name": args.agent_name,
    })

    if hang:
        # Hang forever (heartbeat timeout test)
        try:
            time.sleep(86400)
        except KeyboardInterrupt:
            pass
        return

    # Main command loop
    while True:
        msg = recv()
        if msg is None:
            break  # stdin closed

        msg_type = msg.get("type", "")

        if msg_type == "shutdown":
            break

        if msg_type == "ping":
            send({"type": "pong"})
            continue

        if msg_type == "assign_task":
            task_id = msg.get("task_id", "")
            readable_id = msg.get("readable_id", "")

            if crash_after:
                time.sleep(float(crash_after))
                sys.exit(1)  # Simulate crash

            # Simulate work with progress events
            step_delay = delay / max(progress_steps, 1)
            for i in range(progress_steps):
                time.sleep(step_delay)
                send({
                    "type": "progress",
                    "task_id": task_id,
                    "event_type": "checkpoint",
                    "content": f"Step {i + 1}/{progress_steps} complete",
                })

            # Send completion
            send({
                "type": "task_complete",
                "task_id": task_id,
                "status": "review",
                "comment": f"Task {readable_id} complete (mock).",
                "token_cost": 0.05,
                "session_id": f"mock-session-{task_id}",
            })

            if exit_code != 0:
                sys.exit(exit_code)

            # Worker processes exit after one task
            if args.role == "worker":
                break

        elif msg_type == "chat_message":
            conversation_id = msg.get("conversation_id", "")
            context_key = msg.get("context_key", "general_chat")

            if crash_after:
                time.sleep(float(crash_after))
                sys.exit(1)

            # Simulate streaming response
            chunk_delay = delay / max(response_chunks, 1)
            for i in range(response_chunks):
                time.sleep(chunk_delay)
                send({
                    "type": "response_chunk",
                    "conversation_id": conversation_id,
                    "context_key": context_key,
                    "content": f"Response chunk {i + 1}/{response_chunks}. ",
                })

            # Send final
            send({
                "type": "response_final",
                "conversation_id": conversation_id,
                "context_key": context_key,
                "token_cost": 0.03,
                "session_id": "mock-manager-session",
            })

            # Manager stays alive for more messages (do not break)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
