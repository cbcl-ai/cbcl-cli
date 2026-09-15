"""Quota, expired-token refresh and credential-store regressions."""

import json
import subprocess
from unittest.mock import patch

import pytest

from src.auth_helpers import (
    AuthVerificationUnavailableError,
    oauth_profile_metadata,
    verify_claude_in_container,
    warm_claude_in_container,
)

CONTAINER = "a" * 64
LIMIT_MESSAGE = "You've hit your session limit · resets 10:50pm (UTC)"


def profile_result(state):
    return subprocess.CompletedProcess([], 0, json.dumps({"state": state}), "")


@pytest.mark.parametrize("state, expected", [("valid", True), ("missing", False)])
def test_login_check_does_not_need_model_capacity(state, expected):
    with patch("src.auth_helpers.subprocess.run", return_value=profile_result(state)), patch("src._setup_cli._probe_claude_works") as probe:
        assert verify_claude_in_container(CONTAINER) is expected
    probe.assert_not_called()


@pytest.mark.parametrize("outcome", ["valid", "invalid"])
def test_expired_token_rechecked_after_cli_refresh_even_when_model_is_capped(outcome):
    with patch("src.auth_helpers.subprocess.run", side_effect=[profile_result("invalid"), profile_result(outcome)]), patch("src._setup_cli._probe_claude_works", return_value=None) as probe:
        assert verify_claude_in_container(CONTAINER) is (outcome == "valid")
    probe.assert_called_once()


@pytest.mark.parametrize("result", [profile_result("unavailable"), profile_result({}), subprocess.CompletedProcess([], 0, "bad json", ""), subprocess.CompletedProcess([], 1, "", "docker unavailable")])
def test_verification_outage_is_unknown_not_unauthenticated(result):
    with patch("src.auth_helpers.subprocess.run", return_value=result):
        with pytest.raises(AuthVerificationUnavailableError, match="Recheck"):
            verify_claude_in_container(CONTAINER)


def test_verification_timeout_is_unknown():
    with patch("src.auth_helpers.subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 10)):
        with pytest.raises(AuthVerificationUnavailableError):
            verify_claude_in_container(CONTAINER)


def test_usage_limit_is_a_warning_with_authenticated_true():
    warnings = []
    # Real diagnostic adapter + classifier, with the production CLI output.
    responses = [profile_result("valid"), subprocess.CompletedProcess([], 1, LIMIT_MESSAGE, "")]
    with patch("src.auth_helpers.subprocess.run", side_effect=responses):
        assert verify_claude_in_container(CONTAINER, warning_sink=warnings) is True
    assert "usage limit" in warnings[0]
    assert "22:50 UTC" in warnings[0]
    assert "Signing in again won't reset" in warnings[0]


def test_keepalive_does_not_mark_usage_limited_account_as_auth_expired():
    responses = [subprocess.CompletedProcess([], 1, LIMIT_MESSAGE, ""), profile_result("valid")]
    with patch("src.auth_helpers.subprocess.run", side_effect=responses):
        assert warm_claude_in_container(CONTAINER) is True


def test_login_rejected_during_capacity_check_does_not_leave_stale_success():
    responses = [profile_result("valid"), subprocess.CompletedProcess([], 1, "Failed to authenticate: OAuth session expired and could not be refreshed", ""), profile_result("invalid")]
    warnings = []
    with patch("src.auth_helpers.subprocess.run", side_effect=responses):
        assert verify_claude_in_container(CONTAINER, warning_sink=warnings) is False
    assert warnings == []


def test_policy_failure_during_expired_token_refresh_remains_typed():
    from src._setup_cli import GenerationPolicyError

    with patch("src.auth_helpers._profile_auth_state", return_value="invalid"), patch("src._setup_cli._probe_claude_works", side_effect=GenerationPolicyError("upgrade required")):
        with pytest.raises(GenerationPolicyError, match="upgrade"):
            verify_claude_in_container(CONTAINER)


def test_current_and_legacy_profile_metadata():
    assert oauth_profile_metadata({"account": {"uuid": "account"}, "organization": {"organization_type": "claude_max", "rate_limit_tier": "default_claude_max_20x"}}) == {"subscriptionType": "max", "rateLimitTier": "default_claude_max_20x"}
    assert oauth_profile_metadata({"subscription_type": "pro"})["subscriptionType"] == "pro"
    assert oauth_profile_metadata({})["subscriptionType"] is None
    assert oauth_profile_metadata({"organization": {"organization_type": {}}, "subscription_type": [], "rate_limit_tier": {}}) == {"subscriptionType": None, "rateLimitTier": None}


def test_exact_production_limit_enters_existing_defer_lifecycle():
    from src.orchestrator.error_classifier import ErrorClass, classify_error

    remedy = classify_error(LIMIT_MESSAGE)
    assert remedy.error_class == ErrorClass.USAGE_LIMIT_EXCEEDED
    assert remedy.reset_at.hour == 22
    assert remedy.reset_at.minute == 50


@pytest.mark.parametrize("status, body, expected", [
    (200, {"account": {"uuid": "synthetic-account"}}, "valid"),
    (401, {"error": {"message": "synthetic-secret"}}, "invalid"),
    (429, {"error": {"message": "rate limited"}}, "unavailable"),
    (503, {}, "unavailable"),
    (200, {}, "unavailable"),
])
def test_actual_node_probe_keeps_tokens_inside_container(status, body, expected, tmp_path):
    import shutil
    from src.auth_helpers import _AUTH_PROFILE_SCRIPT

    node = shutil.which("node")
    if not node:
        pytest.skip("Node runtime required for the in-container probe contract")
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": "synthetic-secret"}}))
    harness = r"""
const fs = require('fs'), vm = require('vm'), {EventEmitter} = require('events');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const wrappedFs = {...fs, openSync: (path, flags) => {
  if (path !== '/home/agent/.claude/.credentials.json') throw Error('wrong credential path');
  return fs.openSync(input.path, flags);
}};
const https = {request: (options, callback) => {
  if (options.hostname !== 'api.anthropic.com' || options.path !== '/api/oauth/profile') throw Error('wrong provider');
  if (options.headers.Authorization !== 'Bearer synthetic-secret') throw Error('wrong token');
  const request = new EventEmitter();
  request.destroy = () => {};
  request.end = () => {
    const response = new EventEmitter(); response.statusCode = input.status;
    callback(response); response.emit('data', JSON.stringify(input.body)); response.emit('end');
  };
  return request;
}};
vm.runInNewContext(input.script, {console, require: name => name === 'fs' ? wrappedFs : https});
"""
    result = subprocess.run([node, "-e", harness], input=json.dumps({"script": _AUTH_PROFILE_SCRIPT, "path": str(credentials), "status": status, "body": body}), capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"state": expected}
    assert "synthetic-secret" not in result.stdout + result.stderr
