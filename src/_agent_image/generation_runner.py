"""Pinned, tool-free generation launcher installed outside the office workspace."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


SUPPORTED_SDK_VERSION = "0.2.152"
SUPPORTED_CLI_VERSION = "2.1.259"
POLICY_VERSION = 1
CLI_PATH = "/usr/local/bin/claude"
MAX_REQUEST_BYTES = 4 * 1024 * 1024
POLICY_EXIT_CODE = 78
POLICY_ERROR_PREFIX = "GENERATION_POLICY_ERROR:"
_PROFILES = frozenset({"draft", "survey", "diagnostic"})
_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
_AUTH_ENV = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
)
_EXECUTABLE_SETTINGS = frozenset(
    {
        "hooks",
        "policyHelper",
        "apiKeyHelper",
        "statusLine",
        "fileSuggestion",
        "subagentStatusLine",
        "awsAuthRefresh",
        "awsCredentialExport",
        "env",
    }
)


class PolicyError(RuntimeError):
    """A policy incompatibility that must never trigger a weaker retry."""


def generation_environment(source: dict[str, str]) -> dict[str, str]:
    environment = {key: value for key, value in source.items() if key in _AUTH_ENV}
    environment.update(
        {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/home/agent",
            "LANG": "C.UTF-8",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        }
    )
    return environment


def reject_executable_managed_policy(root: Path = Path("/etc/claude-code")) -> None:
    if root.is_symlink():
        raise PolicyError("Managed generation policy directory cannot be validated.")
    candidates = [root / "managed-settings.json"]
    dropins = root / "managed-settings.d"
    if dropins.is_symlink():
        raise PolicyError("Managed generation policy directory cannot be validated.")
    if dropins.exists():
        candidates.extend(dropins.glob("*.json"))
    for candidate in candidates:
        if candidate.is_symlink():
            raise PolicyError("Managed generation policy cannot be a symlink.")
        if not candidate.exists():
            continue
        try:
            if candidate.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("unsupported managed settings file")
            settings = json.loads(candidate.read_text())
            if not isinstance(settings, dict):
                raise ValueError("invalid managed settings")
        except (OSError, ValueError) as exc:
            raise PolicyError("Managed generation policy cannot be validated.") from exc
        if any(settings.get(key) for key in _EXECUTABLE_SETTINGS):
            raise PolicyError(
                "Read-only generation does not support executable managed settings."
            )


def validate_cli_version(environment: dict[str, str]) -> None:
    result = subprocess.run(
        [CLI_PATH, "--version"],
        capture_output=True,
        text=True,
        timeout=15,
        env=environment,
    )
    expected = f"{SUPPORTED_CLI_VERSION} (Claude Code)"
    if result.returncode != 0 or result.stdout.strip() != expected:
        raise PolicyError(
            f"Read-only generation requires Claude Code {SUPPORTED_CLI_VERSION}. "
            "Rebuild the office agent image."
        )


def build_generation_argv(request: dict, directory: Path) -> list[str]:
    if request.get("policy_version") != POLICY_VERSION:
        raise PolicyError("Unsupported read-only generation policy version.")
    if request.get("profile") not in _PROFILES:
        raise PolicyError("Unsupported generation profile.")
    model = request.get("model")
    if not isinstance(model, str) or not re.fullmatch(
        r"[A-Za-z0-9._:/-]{1,200}", model
    ):
        raise PolicyError("Invalid generation model.")
    max_turns = request.get("max_turns", 4)
    if type(max_turns) is not int or not 1 <= max_turns <= 30:
        raise PolicyError("Invalid generation turn limit.")
    output_format = request.get("output_format", "text")
    if output_format not in {"text", "json"}:
        raise PolicyError("Invalid generation output format.")
    effort = request.get("effort")
    if effort is not None and effort not in _EFFORTS:
        raise PolicyError("Invalid generation effort.")
    command = [
        CLI_PATH,
        "--print",
        "--safe-mode",
        "--restricted",
        "--tools",
        "",
        "--disallowed-tools",
        "*",
        "--strict-mcp-config",
        "--mcp-config",
        str(directory / "mcp.json"),
        "--setting-sources",
        "",
        "--settings",
        '{"disableAllHooks":true}',
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--disable-slash-commands",
        "--no-chrome",
        "--no-session-persistence",
        "--model",
        model,
        "--max-turns",
        str(max_turns),
        "--output-format",
        output_format,
        "--system-prompt-file",
        str(directory / "system.txt"),
    ]
    if effort:
        command.extend(["--effort", effort])
    return command


def run_generation(request: dict) -> int:
    timeout = request.get("timeout", 360)
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise PolicyError("Invalid generation timeout.")
    for field in ("system_prompt", "user_prompt"):
        if not isinstance(request.get(field), str):
            raise PolicyError("Invalid generation prompt.")
    environment = generation_environment(dict(os.environ))
    reject_executable_managed_policy()
    validate_cli_version(environment)
    with tempfile.TemporaryDirectory(prefix="cubicle-generation-") as temporary:
        directory = Path(temporary)
        command = build_generation_argv(request, directory)
        (directory / "mcp.json").write_text('{"mcpServers":{}}')
        (directory / "system.txt").write_text(request["system_prompt"])
        try:
            result = subprocess.run(
                command,
                input=request["user_prompt"],
                text=True,
                capture_output=True,
                cwd=directory,
                env=environment,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            print("Generation exceeded its execution deadline.", file=sys.stderr)
            return 124
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr[-4000:])
        return result.returncode


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise PolicyError("Generation input exceeds its size limit.")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise PolicyError("Invalid generation request.")
        return run_generation(request)
    except (PolicyError, OSError, ValueError, subprocess.SubprocessError) as exc:
        message = (
            str(exc)
            if isinstance(exc, PolicyError)
            else "Generation launcher unavailable."
        )
        print(f"{POLICY_ERROR_PREFIX} {message}", file=sys.stderr)
        return POLICY_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
