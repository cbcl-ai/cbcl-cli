"""Runtime-lane prerequisites and trial expansion.

Runtime cases carry both ``live_eval`` and ``runtime_eval``. When a
prerequisite is missing — the ``CUBICLE_EVAL_RUNTIME=1`` opt-in, a Claude
credential, a reachable docker daemon, or a local ``cbcl-agent`` image whose
``mcp_server_hash`` label matches the source — every runtime case is skipped
with a ``not_evaluated:<code>`` reason, which the report counts as NOT
EVALUATED rather than passed. Nothing here builds or pulls an image.
"""

from __future__ import annotations

import pytest

from tests.evals import _live_report as live_report
from tests.evals.live._harness import eval_trials
from tests.evals.runtime._runtime import runtime_prerequisites


def pytest_generate_tests(metafunc) -> None:
    if "eval_trial" in metafunc.fixturenames:
        metafunc.parametrize(
            "eval_trial",
            range(eval_trials()),
            ids=[f"trial{index + 1}" for index in range(eval_trials())],
        )


def pytest_collection_modifyitems(config, items) -> None:
    runtime_items = [item for item in items if "runtime_eval" in item.keywords]
    if not runtime_items:
        return
    missing = runtime_prerequisites()
    if missing is None:
        return
    skip_marker = pytest.mark.skip(reason=live_report.not_evaluated_reason(*missing))
    for item in runtime_items:
        item.add_marker(skip_marker)
