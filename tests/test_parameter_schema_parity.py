"""The daemon and the backend normalize a generated parameter_schema alike.

``generate_skill_from_overview`` normalizes the model's ``parameter_schema``
with ``_setup_skill_render.normalize_parameter_schema``; the backend then runs
``app/skills/parameters.normalize_generated_parameter_schema`` on that output.
If the two disagree, the result depends on the daemon version: an older
daemon's output took the other rules (string booleans, over-long names and
types, JSON defaults). These tests pin the daemon to the backend's rules
(B5-hygiene-02). In a standalone CLI checkout the backend comparisons skip.
"""

from __future__ import annotations

import pytest

from src._setup_skill_render import normalize_parameter_schema
from tests import backend_boundary

RAW_SCHEMAS = [
    {"name": "REGION", "is_secret": "true", "default_value": {"b": 1, "a": 2}},
    [
        {"name": "N" * 260, "type": "string"},
        {"name": "MODE", "type": "t" * 60, "default_value": ["x", 1]},
        {"name": "MODE", "default_value": "duplicate"},
        {"name": "  ", "default_value": 1},
        "not an object",
        {"name": "LIMIT", "type": "number", "default_value": 10},
        {"name": "FLAG", "type": "boolean", "default_value": False},
        {"name": "API_KEY", "is_secret": "yes"},
        {"name": "TOKEN", "is_secret": 1},
        {"name": "ANTHROPIC_API_KEY", "is_secret": True},
        {"name": "PLAIN", "is_secret": "no", "description": "  Used for X.  "},
    ],
    {"name": "Ünïcode", "default_value": {"ключ": "значення"}},
    [],
    None,
]


def _backend():
    return backend_boundary.import_backend("app.skills.parameters")


def _secret_params():
    return backend_boundary.import_backend("app.skills.secret_params")


@pytest.mark.parametrize("raw", RAW_SCHEMAS)
def test_daemon_output_is_what_the_backend_would_produce(raw):
    parameters = _backend()
    daemon = normalize_parameter_schema(raw)
    backend, _dropped = parameters.normalize_generated_parameter_schema(raw)
    # The backend also drops secrets whose names the office cannot store or
    # that are reserved Claude sign-in names; the daemon leaves that decision
    # (and its report) to the backend.
    storable = {entry["name"] for entry in backend}
    kept = [
        entry
        for entry in daemon
        if entry["name"] in storable or not entry["is_secret"]
    ]
    assert kept == [
        {**entry, "description": _stripped(entry["description"])} for entry in backend
    ]


@pytest.mark.parametrize("raw", RAW_SCHEMAS)
def test_backend_keeps_the_daemon_output_unchanged(raw):
    parameters = _backend()
    secret_params = _secret_params()
    daemon = normalize_parameter_schema(raw)
    again, _dropped = parameters.normalize_generated_parameter_schema(daemon)
    assert again == [
        entry
        for entry in daemon
        if not entry["is_secret"]
        or secret_params.generated_secret_refusal(entry["name"]) is None
    ]


def test_string_true_marks_a_secret():
    entries = normalize_parameter_schema([{"name": "API_KEY", "is_secret": "true"}])
    assert entries[0]["is_secret"] is True


def _stripped(value: str | None) -> str | None:
    """The daemon trims description whitespace; the backend keeps it."""
    if value is None:
        return None
    return value.strip() or None
