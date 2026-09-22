"""Manager-only instruction inspection and human-approved proposals."""

CONFIGURATION_TOOLS = [
    {
        "name": "inspect_configuration",
        "action": "inspect_configuration",
        "description": "Read current Office instructions, Workstream context, or a custom Profile's instructions before proposing edits. Returns recent proposal decisions and feedback. Pass proposal_id to inspect an existing proposal, including its exact edits. READ-ONLY. Do not use for credentials, permissions, built-in Manager prompts, CI files or runtime settings. Use list_agents for Profile config IDs; never pass a task Agent UUID; office target_id defaults to this office.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "enum": ["office", "workstream", "agent"]},
                "target_id": {"type": "string"},
                "proposal_id": {"type": "string"},
            },
        },
    },
    {
        "name": "propose_configuration",
        "action": "propose_configuration",
        "description": "Post an exact, coordinated instruction change set for human approval in this chat. Inspect every target first; before must match the current value verbatim (null is distinct from empty). Explain evidence, expected benefit and tradeoffs. Only Office claude_md_content, Workstream context_notes, and custom Profile role_description/system_prompt/claude_md_content are supported. No configuration is applied by this tool. User can approve, request corrections, or decline. Stop after posting; never treat a chat yes or tool success as approval. On correction, inspect the proposal and current settings, then propose a replacement. Proactive suggestions require repeated concrete evidence, use proactive=true and a stable topic; recent decisions suppress repeat suggestions for seven days.",
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
        _property["description"] = _PARAMETER_DESCRIPTIONS[_name]
