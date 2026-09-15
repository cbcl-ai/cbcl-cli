"""Isolation is a deliberate office-local opt-in, never a silent default."""

from dataclasses import asdict

import pytest
import yaml

from src.execution_policy import load_worker_execution_policy

OFFICE_ID = "11111111-1111-4111-8111-111111111111"


def test_default_and_unselected_offices_keep_existing_runtime(tmp_path):
    config = tmp_path / "config.yaml"
    assert load_worker_execution_policy(OFFICE_ID, config) is None
    config.write_text("platform_url: https://synthetic.invalid\n")
    assert load_worker_execution_policy(OFFICE_ID, config) is None
    config.write_text(yaml.safe_dump({"execution_containers": {"offices": [OFFICE_ID]}}))
    assert load_worker_execution_policy("22222222-2222-4222-8222-222222222222", config) is None


def test_selected_pool_has_explicit_aggregate_additional_caps(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"execution_containers": {
        "offices": [OFFICE_ID], "acknowledge_shared_auth": True,
    }}))
    policy = load_worker_execution_policy(OFFICE_ID, config)
    assert asdict(policy.resources) == {"cpu_millis": 1000, "memory_bytes": 2 * 1024 ** 3, "pids": 256}
    assert asdict(policy.budget) == {"max_workers": 2, "cpu_millis": 2000, "memory_bytes": 4 * 1024 ** 3, "pids": 512}


@pytest.mark.parametrize("changed", [
    {"acknowledge_shared_auth": False}, {"worker_cpus": float("nan")},
    {"worker_cpus": float("inf")}, {"worker_cpus": True}, {"worker_cpus": 0},
    {"worker_memory": "unlimited"}, {"worker_memory": "1m"},
    {"worker_pids": True}, {"worker_pids": 0},
    {"max_workers_per_office": 0}, {"max_workers_per_office": 21},
    {"max_worker_per_office": 2}, {"offices": [OFFICE_ID, OFFICE_ID]},
    {"offices": [True]}, {"offices": "all"}, {"offices": ["*"]},
])
def test_invalid_policy_never_falls_back_to_uncontained_execution(tmp_path, changed):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"execution_containers": {
        "offices": [OFFICE_ID], "acknowledge_shared_auth": True, **changed,
    }}))
    with pytest.raises(ValueError):
        load_worker_execution_policy(OFFICE_ID, config)
