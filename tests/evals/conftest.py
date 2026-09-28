"""Shared eval-lane wiring.

Registers the live-eval report plugin exactly once for both behavioral lanes
(``live/`` and ``runtime/``). The hooks are inert unless
``CUBICLE_LIVE_EVAL_REPORT_DIR`` or ``CUBICLE_LIVE_EVAL_REQUIRE_EXECUTION=1``
is set; see ``tests/evals/README.md``.
"""

from tests.evals._live_report_plugin import (  # noqa: F401 — pytest hooks
    pytest_collection_finish,
    pytest_collection_modifyitems,
    pytest_collectreport,
    pytest_configure,
    pytest_runtest_call,
    pytest_runtest_makereport,
    pytest_sessionfinish,
    pytest_terminal_summary,
)
