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
        "description": (
            "Omitted/null applies the office default to every role: the shared "
            "workspace, or only that phase's agent if the office reserves per agent. "
            "[] declares independence; named keys are office-exclusive; "
            "shared-workspace runs its execution alone where the office reserves "
            "per agent; "
            "profile: keys are reserved. Fixed while running, reviewing or cleanup "
            "is pending; never use [] merely to bypass conflicts. Profile tool "
            "lists do not establish independence."
        ),
    }
