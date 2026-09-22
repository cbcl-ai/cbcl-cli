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
        "description": "Omitted/null reserves shared-workspace for every role, including review/triage; [] declares independence; named keys are office-exclusive. Fixed while running, reviewing or cleanup is pending; never use [] merely to bypass conflicts. Profile tool lists do not establish independence.",
    }
