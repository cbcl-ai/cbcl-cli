"""Manager-only instruction inspection and human-approved proposals."""

from .result_text import LARGE_READ_GUIDANCE, section_read_properties

CONFIGURATION_TOOLS = [
    {
        "name": "inspect_configuration",
        "action": "inspect_configuration",
        "description": "Read current Office instructions and work policy, Workstream context, or a custom Profile's instructions before proposing edits. Returns recent proposal decisions and feedback. Pass proposal_id to inspect an existing proposal, including its exact edits. READ-ONLY. Do not use for credentials, permissions, built-in Manager prompts, CI files or runtime settings. Use list_agents for Profile config IDs; never pass a task Agent UUID; office target_id defaults to this office. "
        + LARGE_READ_GUIDANCE,
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "enum": ["office", "workstream", "agent"]},
                "target_id": {"type": "string"},
                "proposal_id": {"type": "string"},
                **section_read_properties("inspect_configuration"),
            },
        },
    },
    {
        "name": "propose_configuration",
        "action": "propose_configuration",
        "description": "Post an exact, coordinated instruction change set for human approval in this chat. Inspect every target first; before must match the current value verbatim (null is distinct from empty). Explain evidence, expected benefit and tradeoffs. Only Office claude_md_content (Manager-only) or work_policy (every worker/reviewer, new assignments), Workstream context_notes, and custom Profile role_description/system_prompt/claude_md_content are supported. Skills, model/effort, tools and connectors are not targets. No configuration is applied by this tool. User can approve, request corrections, or decline. Stop after posting; never treat a chat yes or tool success as approval. On correction, inspect the proposal and current settings, then propose a replacement. Proactive suggestions require repeated concrete evidence, use proactive=true and a stable topic; recent decisions suppress repeat suggestions for seven days.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "maxLength": 160},
                "rationale": {"type": "string", "maxLength": 4000},
                "evidence": {"type": "string", "maxLength": 4000},
                "topic": {"type": "string", "pattern": "^[a-z][a-z0-9-]{1,79}$"},
                "proactive": {"type": "boolean"},
                "changes": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 12,
                    "items": {
                        "type": "object",
                        "properties": {
                            "target": {
                                "type": "string",
                                "enum": ["office", "workstream", "agent"],
                            },
                            "target_id": {"type": "string"},
                            "field": {
                                "type": "string",
                                "enum": [
                                    "claude_md_content",
                                    "context_notes",
                                    "system_prompt",
                                    "role_description",
                                    "work_policy",
                                ],
                            },
                            "before": {"type": ["string", "null"]},
                            "after": {"type": ["string", "null"]},
                            "reason": {"type": "string"},
                        },
                        "required": [
                            "target",
                            "target_id",
                            "field",
                            "before",
                            "after",
                            "reason",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["title", "rationale", "evidence", "topic", "changes"],
            "additionalProperties": False,
        },
    },
]

# Parameter intent is part of the model-facing contract, not inferred from names.
_PARAMETER_DESCRIPTIONS = {
    "target": "Instruction owner: office (default), workstream, or custom Profile (legacy target=agent).",
    "target_id": "Configuration UUID from live context/Profile catalog; never a task Agent or attempt UUID. Office defaults to this office.",
    "proposal_id": "Read this saved proposal instead of current target fields.",
    "title": "Short user-facing outcome for the proposal card.",
    "rationale": "Expected benefit and tradeoffs; preserve existing quality requirements.",
    "evidence": "Observed task IDs, timings or user request supporting the change; no invented metrics.",
    "topic": "Stable topic slug reused for retries, corrections and cooldown checks.",
    "proactive": "True for unsolicited advice; false when responding to an explicit request.",
    "changes": "One coordinated bundle of exact current and replacement instruction fields.",
}
for _tool in CONFIGURATION_TOOLS:
    for _name, _property in _tool["inputSchema"]["properties"].items():
        if "description" not in _property:  # section reads bring their own
            _property["description"] = _PARAMETER_DESCRIPTIONS[_name]

# The per-field character limits the backend enforces
# (``CONFIGURATION_FIELD_LIMITS`` in app/configuration_proposals/schemas.py);
# tests/evals/test_fx_prompts_pins.py keeps the two copies equal.
CONFIGURATION_FIELD_LIMITS: dict[str, dict[str, int]] = {
    "office": {"claude_md_content": 16_000, "work_policy": 4_000},
    "workstream": {"context_notes": 26_000},
    "agent": {
        "system_prompt": 50_000,
        "claude_md_content": 50_000,
        "role_description": 5_000,
    },
}
_LIMIT_TARGET_LABELS = {
    "office": "Office",
    "workstream": "Workstream",
    "agent": "agent",
}


def _render_field_limits() -> str:
    """``Office claude_md_content 16,000, work_policy 4,000; …`` — adjacent
    fields with the same limit share it (``system_prompt/claude_md_content``)."""
    parts = []
    for target, limits in CONFIGURATION_FIELD_LIMITS.items():
        groups: list[tuple[list[str], int]] = []
        for field, limit in limits.items():
            if groups and groups[-1][1] == limit:
                groups[-1][0].append(field)
            else:
                groups.append(([field], limit))
        rendered = ", ".join(
            f"{'/'.join(fields)} {limit:,}" for fields, limit in groups
        )
        parts.append(f"{_LIMIT_TARGET_LABELS[target]} {rendered}")
    return "; ".join(parts)


# X41: every field of one ``changes[]`` entry is described too — the
# linter walks nested properties now, and ``before``'s exact-match rule is
# the most common cause of a refused proposal.
_CHANGE_FIELD_DESCRIPTIONS = {
    "target": "Owner of this field: office, workstream, or agent (a custom Profile).",
    "target_id": "Configuration UUID of that office, workstream or Profile (never a task Agent UUID).",
    "field": "Instruction field to replace; must belong to the target type.",
    "before": "Current value copied VERBATIM from inspect_configuration (null only if unset).",
    "after": (
        "Complete replacement value; null only to unset a nullable field (never "
        "system_prompt/role_description). Character limits: "
        + _render_field_limits()
        + "."
    ),
    "reason": "Why this one field changes, in plain words.",
}
for _tool in CONFIGURATION_TOOLS:
    _changes = _tool["inputSchema"]["properties"].get("changes")
    if _changes:
        for _name, _property in _changes["items"]["properties"].items():
            _property["description"] = _CHANGE_FIELD_DESCRIPTIONS[_name]
