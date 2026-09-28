"""API-lane prerequisites and trial expansion.

Without ``ANTHROPIC_API_KEY`` every live API case is skipped with a
``not_evaluated:missing_credentials`` reason, which the report plugin counts
as NOT EVALUATED — a key-less run can never read as a pass. With
``CUBICLE_LIVE_EVAL_REQUIRE_EXECUTION=1`` such a run exits 10.

``CUBICLE_EVAL_TRIALS`` (default 1) repeats every case that requests the
``eval_trial`` fixture so per-case pass counts can be reported.
"""

from __future__ import annotations

import os

import pytest

from tests.evals import _live_report as live_report
from tests.evals.live._harness import eval_trials

MISSING_KEY_SKIP_REASON = live_report.not_evaluated_reason(
    live_report.MISSING_CREDENTIALS,
    "ANTHROPIC_API_KEY is not set; the API lane made no model calls",
)


def pytest_generate_tests(metafunc) -> None:
    if "eval_trial" in metafunc.fixturenames:
        metafunc.parametrize(
            "eval_trial",
            range(eval_trials()),
            ids=[f"trial{index + 1}" for index in range(eval_trials())],
        )


def pytest_collection_modifyitems(config, items) -> None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    skip_marker = pytest.mark.skip(reason=MISSING_KEY_SKIP_REASON)
    for item in items:
        if "live_eval" in item.keywords and "runtime_eval" not in item.keywords:
            item.add_marker(skip_marker)
