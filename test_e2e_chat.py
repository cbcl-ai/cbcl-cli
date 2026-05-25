#!/usr/bin/env python3
"""E2E test: reproduce the exact chat flow and diagnose issues.

Run: python test_e2e_chat.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile

CONTAINER = "cbcl-office-recruitment"
MODEL = "claude-sonnet-4-6"
OFFICE_ID = "f512032c-7da3-443b-9f73-786afc5ea83f"
BACKEND_URL = "http://host.docker.internal:8000"


def step(msg):
    print(f"\n{'='*60}\n  {msg}\n{'='*60}")


def check(ok, msg):
    icon = "PASS" if ok else "FAIL"
    print(f"  [{icon}] {msg}")
    return ok


async def main():
    step("1. Check container is running")
    r = subprocess.run(["docker", "inspect", CONTAINER, "-f", "{{.State.Running}}"],
                       capture_output=True, text=True)
    if not check(r.stdout.strip() == "true", f"Container {CONTAINER} running"):
        print("  Start container first: cbcl setup")
        return

    step("2. Check Claude CLI version")
    r = subprocess.run(["docker", "exec", CONTAINER, "claude", "--version"],
                       capture_output=True, text=True, timeout=10)
    check(r.returncode == 0, f"Claude CLI: {r.stdout.strip()}")

    step("3. Check auth")
    r = subprocess.run(["docker", "exec", CONTAINER, "claude", "auth", "status"],
                       capture_output=True, text=True, timeout=10)
    print(f"  {r.stdout.strip()}")
    check("loggedIn" in r.stdout, "Auth status available")

    step("4. Test simple claude --print (no MCP, no system prompt)")
    r = subprocess.run(
        ["docker", "exec", CONTAINER, "stdbuf", "-oL",
         "claude", "--print", "--model", MODEL,
         "--output-format", "stream-json", "--verbose",
         "--permission-mode", "bypassPermissions",
         "-p", "say hello"],
        capture_output=True, text=True, timeout=30)
    got_response = "assistant" in r.stdout
    check(got_response, f"Simple query: rc={r.returncode}, stdout={len(r.stdout)}b")
    if not got_response:
        print(f"  STDOUT: {r.stdout[:300]}")
        print(f"  STDERR: {r.stderr[:300]}")

    step("5. Test with system-prompt-file via base64 encoding")
    prompt = "You are a helpful test assistant."
    import base64
    encoded = base64.b64encode(prompt.encode()).decode()
    prompt_path = "/workspace/.cubicle/.test-prompt"
    r = subprocess.run(
        ["docker", "exec", "-u", "agent", CONTAINER, "bash", "-c",
         f"mkdir -p /workspace/.cubicle && echo '{encoded}' | base64 -d > {prompt_path}"],
        capture_output=True, text=True, timeout=5)
    check(r.returncode == 0, f"Write prompt file: rc={r.returncode}")

    r = subprocess.run(
        ["docker", "exec", CONTAINER, "cat", "/workspace/.cubicle/.test-prompt"],
        capture_output=True, text=True, timeout=5)
    check(r.stdout.strip() == prompt, f"File content matches: '{r.stdout.strip()[:50]}'")

    r = subprocess.run(
        ["docker", "exec", CONTAINER, "stdbuf", "-oL",
         "claude", "--print", "--model", MODEL,
         "--output-format", "stream-json", "--verbose",
         "--permission-mode", "bypassPermissions",
         "--system-prompt-file", "/workspace/.cubicle/.test-prompt",
         "-p", "say hello"],
        capture_output=True, text=True, timeout=30)
    got_response = "assistant" in r.stdout
    check(got_response, f"With system-prompt-file: rc={r.returncode}, stdout={len(r.stdout)}b")
    if not got_response:
        print(f"  STDOUT: {r.stdout[:300]}")
        print(f"  STDERR: {r.stderr[:300]}")

    step("6. Test with MCP config (cubicle-tools)")
    mcp_config = json.dumps({
        "mcpServers": {
            "cubicle-tools": {
                "type": "stdio",
                "command": "python3",
                "args": ["/opt/cubicle/mcp_tool_server.py", "--role", "manager"],
                "env": {
                    "BACKEND_URL": BACKEND_URL,
                    "OFFICE_ID": OFFICE_ID,
                },
            }
        }
    })
    r = subprocess.run(
        ["docker", "exec", CONTAINER, "stdbuf", "-oL",
         "claude", "--print", "--model", MODEL,
         "--output-format", "stream-json", "--verbose",
         "--permission-mode", "bypassPermissions",
         "--mcp-config", mcp_config,
         "-p", "say hello"],
        capture_output=True, text=True, timeout=30)
    got_response = "assistant" in r.stdout
    has_tools = "cubicle-tools" in r.stdout
    check(got_response, f"With MCP: rc={r.returncode}, stdout={len(r.stdout)}b")
    check(has_tools, "cubicle-tools connected")
    if not got_response:
        print(f"  STDOUT: {r.stdout[:500]}")
        print(f"  STDERR: {r.stderr[:500]}")

    step("7. Test with BOTH system-prompt-file AND MCP config")
    r = subprocess.run(
        ["docker", "exec", CONTAINER, "stdbuf", "-oL",
         "claude", "--print", "--model", MODEL,
         "--output-format", "stream-json", "--verbose",
         "--permission-mode", "bypassPermissions",
         "--system-prompt-file", "/workspace/.cubicle/.test-prompt",
         "--mcp-config", mcp_config,
         "-p", "say hello"],
        capture_output=True, text=True, timeout=30)
    got_response = "assistant" in r.stdout
    check(got_response, f"With BOTH: rc={r.returncode}, stdout={len(r.stdout)}b")
    if not got_response:
        print(f"  STDOUT: {r.stdout[:500]}")
        print(f"  STDERR: {r.stderr[:500]}")

    step("8. Test via Python asyncio (same as agent_worker)")
    from src.docker.session_bridge import stream_cli_session

    messages = []
    try:
        async for msg in stream_cli_session(
            container_name=CONTAINER,
            model=MODEL,
            system_prompt="You are a test assistant.",
            prompt="say hello",
            mcp_config=json.loads(mcp_config),
        ):
            messages.append(msg)
            print(f"  MSG: type={msg.type}")
            if msg.type == "error":
                print(f"       error={msg.data}")
            if len(messages) >= 5:
                break
    except Exception as e:
        print(f"  EXCEPTION: {e}")

    got_any = len(messages) > 0
    got_assistant = any(m.type == "assistant" for m in messages)
    got_result = any(m.type == "result" for m in messages)
    check(got_any, f"Got {len(messages)} messages from stream_cli_session")
    check(got_assistant, "Got assistant message")
    check(got_result or len(messages) >= 3, "Got result or enough messages")

    step("DONE")


if __name__ == "__main__":
    asyncio.run(main())
