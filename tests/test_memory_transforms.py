"""Office-memory v1 (T3.2) — memory tool param transforms.

Scope is derived SERVER-side: the transform injects ``task_id`` from the
session's TASK_ID env and ``context_key`` from CONTEXT_KEY, and DROPS any
client-supplied scope key (the ask_user_choice L-6 posture — a
hallucinated/stale scope must never pick which workstream's memory is
read or written).
"""
from __future__ import annotations

import pytest

from src._agent_image._mcp.transforms import transform_params


@pytest.fixture
def worker_env(monkeypatch):
    monkeypatch.setenv("TASK_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("AGENT_NAME", "analyst")
    monkeypatch.delenv("CONTEXT_KEY", raising=False)


@pytest.fixture
def manager_env(monkeypatch):
    monkeypatch.delenv("TASK_ID", raising=False)
    monkeypatch.setenv("AGENT_NAME", "")
    monkeypatch.setenv(
        "CONTEXT_KEY", "workstream:22222222-2222-2222-2222-222222222222",
    )


class TestMemoryRecallTransform:
    def test_worker_recall_injects_task_id(self, worker_env):
        out = transform_params("memory_recall", None, {"query": "auth"})
        assert out["task_id"] == "11111111-1111-1111-1111-111111111111"
        assert out["query"] == "auth"
        assert "context_key" not in out

    def test_manager_recall_injects_context_key(self, manager_env):
        out = transform_params("memory_recall", None, {"query": "auth"})
        assert out["context_key"] == (
            "workstream:22222222-2222-2222-2222-222222222222"
        )
        assert "task_id" not in out

    def test_client_supplied_scope_keys_are_dropped(self, worker_env):
        # A model-supplied task_id / context_key / workstream_id must never
        # survive — the env is the only scope source.
        out = transform_params("memory_recall", None, {
            "query": "x",
            "task_id": "99999999-9999-9999-9999-999999999999",
            "context_key": "workstream:evil",
            "workstream_id": "evil",
            "office_id": "evil",
            "scope": "office",
        })
        assert out["task_id"] == "11111111-1111-1111-1111-111111111111"
        assert "workstream_id" not in out
        assert "office_id" not in out
        assert "scope" not in out
        assert out.get("context_key") is None or "evil" not in str(
            out.get("context_key")
        )

    def test_recall_whitelist_passes_all_schema_params(self, worker_env):
        params = {
            "query": "pricing",
            "kind": "decision",
            "slug": "task:WR-003.T14",
            "include_office": False,
        }
        out = transform_params("memory_recall", None, dict(params))
        for key, value in params.items():
            assert out[key] == value

    def test_recall_without_env_scope_injects_nothing(self, monkeypatch):
        monkeypatch.delenv("TASK_ID", raising=False)
        monkeypatch.delenv("CONTEXT_KEY", raising=False)
        out = transform_params("memory_recall", None, {"query": "x"})
        assert "task_id" not in out
        assert "context_key" not in out


class TestMemoryRememberTransform:
    def test_remember_whitelist_and_context_injection(self, manager_env):
        params = {
            "kind": "decision",
            "title": "Deploy target is Hetzner",
            "body": "The user picked Hetzner over AWS on 2026-09-01.",
            "tags": ["infra"],
            "supersedes": "deploy-target",
            "office_wide": True,
        }
        out = transform_params("memory_remember", None, dict(params))
        for key, value in params.items():
            assert out[key] == value
        assert out["context_key"] == (
            "workstream:22222222-2222-2222-2222-222222222222"
        )

    def test_remember_drops_client_scope_keys(self, manager_env):
        out = transform_params("memory_remember", None, {
            "kind": "fact",
            "title": "t",
            "body": "b",
            "context_key": "workstream:evil",
            "task_id": "evil",
            "workstream_id": "evil",
        })
        assert out["context_key"] == (
            "workstream:22222222-2222-2222-2222-222222222222"
        )
        assert "workstream_id" not in out
        assert out.get("task_id") is None or out["task_id"] != "evil"

    def test_remember_does_not_leak_recall_only_params(self, manager_env):
        out = transform_params("memory_remember", None, {
            "kind": "fact", "title": "t", "body": "b", "query": "x",
            "slug": "y", "include_office": True,
        })
        assert "query" not in out
        assert "slug" not in out
        assert "include_office" not in out


class TestOtherActionsUntouched:
    def test_unrelated_action_passes_through(self, worker_env):
        params = {"task_id": "abc", "event_type": "comment", "content": "c"}
        # add_activity has its own transform; a bare unrelated action must
        # not be caught by the memory branch.
        out = transform_params("get_task_detail", None, dict(params))
        assert out == params


class TestCreateTaskAssignedReferences:
    """Office-memory final audit (spec §4.4/§6.5): the Manager assigns KB
    references on the brief via ``reference_doc_ids`` — the param must be
    in the create_task schema AND survive the wire path verbatim."""

    def test_manager_create_task_schema_exposes_reference_doc_ids(self):
        from src._agent_image._mcp.tools_manager import get_manager_tools

        for tool in get_manager_tools():
            if tool["name"] == "create_task":
                schema = tool["inputSchema"]
                prop = schema["properties"]["reference_doc_ids"]
                assert prop["type"] == "array"
                assert prop["items"] == {"type": "string"}
                # The description carries the mechanism's load-bearing
                # facts: it IS the Assigned-references trigger, ids come
                # from search_kb, and it is binding (unlike allowed_tools).
                assert "Assigned references" in prop["description"]
                assert "search_kb" in prop["description"]
                assert "NOT advisory" in prop["description"]
                # Optional — never part of brief completeness (spec §4.4).
                assert "reference_doc_ids" not in schema["required"]
                return
        raise AssertionError("create_task not found in the Manager catalog")

    def test_create_task_params_pass_through_untouched(self, worker_env):
        # create_task declares NO transform — every brief field
        # (reference_doc_ids included) must reach the backend verbatim,
        # even with session env vars set.
        params = {
            "workstream_id": "ws-1",
            "title": "Summarise the vendor contract",
            "assigned_agent": "analyst",
            "reviewer": "auditor",
            "goal": "g",
            "inputs": "i",
            "acceptance_criteria": ["a"],
            "verification_steps": "v",
            "reference_doc_ids": ["33333333-3333-3333-3333-333333333333"],
        }
        out = transform_params("create_task", None, dict(params))
        assert out == params


def test_worker_detail_merge_copies_memory_index() -> None:
    """Fail-closed tripwire (review finding 2026-09-02): the session-start
    detail refetch OVERWRITES the dispatch payload field-by-field — a feed
    field missing from the copy list is silently dropped, which is exactly
    how workstream_memory_index shipped dead the first time. Source-level
    pin, mirroring the handlers spawn pin pattern."""
    import inspect

    from src import _agent_worker_task

    source = inspect.getsource(_agent_worker_task)
    assert 'task_data["workstream_memory_index"] = detail.get(' in source
