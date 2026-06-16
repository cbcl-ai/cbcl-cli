"""T2.2.3 — per-agent office-secret allowlist filter.

Verifies the pure ``apply_secret_env_allowlist`` helper that the worker
task runner uses to scope which office secrets reach an agent's session
env. Default (``None``) must be byte-identical to today's "inject all"
behaviour so the user-mandated model is preserved.
"""
from __future__ import annotations

from src._agent_worker_task import apply_secret_env_allowlist


_ENV = {
    "OPENAI_API_KEY": "sk-a",
    "GITLAB_PAT": "glpat-b",
    "SLACK_BOT_TOKEN": "xoxb-c",
}


def test_none_allowlist_injects_all_unchanged():
    # The default: no allowlist → identical dict, same values.
    out = apply_secret_env_allowlist(_ENV, None)
    assert out == _ENV


def test_empty_allowlist_injects_none():
    out = apply_secret_env_allowlist(_ENV, [])
    assert out == {}


def test_named_allowlist_filters_to_listed():
    out = apply_secret_env_allowlist(_ENV, ["OPENAI_API_KEY", "GITLAB_PAT"])
    assert out == {"OPENAI_API_KEY": "sk-a", "GITLAB_PAT": "glpat-b"}


def test_allowlist_name_not_in_store_is_skipped():
    # Listing a name the office doesn't have just yields nothing extra.
    out = apply_secret_env_allowlist(_ENV, ["DOES_NOT_EXIST"])
    assert out == {}


def test_allowlist_does_not_mutate_input():
    original = dict(_ENV)
    apply_secret_env_allowlist(_ENV, ["GITLAB_PAT"])
    assert _ENV == original


def test_empty_env_with_any_allowlist_is_empty():
    assert apply_secret_env_allowlist({}, ["OPENAI_API_KEY"]) == {}
    assert apply_secret_env_allowlist({}, None) == {}
