"""Shared task resource declaration schema (existing task-write tools only)."""


def execution_resources_property() -> dict:
    return {
        "type": ["array", "null"],
        "maxItems": 16,
        "uniqueItems": True,
        "items": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120,
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$",
        },
        # Shared by four Manager tools, two worker-pool tools and the Planner's
        # two, so every added character counts several times against the
        # catalog budgets (tests/evals/test_prompt_token_budget.py). The
        # fuller when-to-declare guidance is the dynamic policy block's
        # (``_content_contracts._RESOURCES``).
        "description": (
            "Null/omitted reserves only the agent doing each phase: its "
            "undeclared tasks run one at a time. [] declares isolated work "
            "(changes nothing shared) that can run beside them up to the "
            "per-Profile limit; a named key (e.g. deploy:staging) serializes "
            "only tasks naming it; shared-workspace runs the execution alone; "
            "profile: keys are reserved. Fixed while running, in review or "
            "cleaning up; never use [] merely to bypass conflicts; tool lists "
            "prove no independence."
        ),
    }
