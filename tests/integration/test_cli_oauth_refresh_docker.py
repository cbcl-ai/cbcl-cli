"""Opt-in acceptance of genuine CLI refresh using only synthetic credentials.

Run on a Docker-capable host with communicator test dependencies installed::

    CUBICLE_RUN_CLI_OAUTH_REFRESH_TESTS=1 \
    CUBICLE_CLI_REFRESH_TEST_IMAGE=<verified-local-agent-image> \
      python -m pytest tests/integration/test_cli_oauth_refresh_docker.py -q

The image is never pulled. Containers have no network, host credentials,
workspaces, or Docker socket; a synthetic TLS provider listens on loopback.
Single and concurrent diagnostics deliberately receive model quota errors.
This tests the pinned CLI's file rotation and locking, not a live provider's
willingness to accept any particular refresh token or account credentials.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import uuid

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("CUBICLE_RUN_CLI_OAUTH_REFRESH_TESTS") != "1",
    reason="requires explicitly enabled synthetic OAuth Docker acceptance",
)


@pytest.mark.parametrize("concurrency", [1, 2], ids=["single", "concurrent"])
@pytest.mark.timeout(90)
def test_expired_oauth_refresh_persists_despite_model_quota(concurrency: int) -> None:
    image = os.environ.get("CUBICLE_CLI_REFRESH_TEST_IMAGE", "")
    assert image and not image.startswith("-"), "Set a verified local agent image"
    container_name = f"cbcl-synthetic-oauth-{uuid.uuid4().hex}"
    communicator = Path(__file__).resolve().parents[2]
    fixture = Path(__file__).parent / "fixtures" / "cli_oauth_refresh_probe.py"
    command = [
        "docker", "run", "--rm", "--pull", "never", "--init",
        "--network", "none", "--user", "root", "--name", container_name,
        "--pids-limit", "256", "--memory", "2g", "--cpus", "2",
        "--label", "cbcl.audit.test.synthetic-oauth=true",
        "--mount", f"type=bind,src={fixture},dst=/fixture/probe.py,readonly",
        "--mount", (
            f"type=bind,src={communicator / 'src' / 'auth_helpers.py'},"
            "dst=/fixture/auth_helpers.py,readonly"
        ),
    ]
    for hostname in (
        "platform.claude.com", "api.anthropic.com", "console.anthropic.com",
        "claude.ai",
    ):
        command.extend(["--add-host", f"{hostname}:127.0.0.1"])
    command.extend([
        image, "python", "/fixture/probe.py", "--concurrency", str(concurrency),
    ])
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=70)
        assert result.returncode == 0, result.stdout + result.stderr
        receipt = json.loads(result.stdout)
        assert receipt["cli_version"] == "2.1.259 (Claude Code)"
        assert receipt["refresh_request_count"] == 1
        assert receipt["model_session_count"] == concurrency
        assert receipt["access_token_rotated"] and receipt["refresh_token_rotated"]
        assert receipt["saved_expiry_in_future"]
        assert receipt["profile_before"] == "invalid"
        assert receipt["profile_after"] == "valid"
    finally:
        # Removes only this test's exact unique name, including on host timeout.
        subprocess.run(
            ["docker", "rm", "--force", container_name],
            capture_output=True, timeout=15,
        )
