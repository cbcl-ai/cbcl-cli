"""Task-owned optional operation tools; no provider or domain is required."""


def operation_property() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "description": "Optional tracked intent for a long registered script. Reuse its key after response loss; an intentional new run needs a new key. Provide a SHA-256 of relevant inputs, never secrets. Added resource keys cannot remove the task's reservations. Independent review uses its own phase identity.",
        "properties": {
            "stage": {
                "type": "string",
                "enum": ["preparation", "execution", "verification"],
                "description": "Declared work stage for timing; does not change phase or authority.",
            },
            "key": {"type": "string", "maxLength": 120},
            "input_fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "resources": {
                "type": "array",
                "maxItems": 16,
                "items": {"type": "string", "maxLength": 120},
            },
        },
        "required": ["key", "input_fingerprint"],
    }


def operation_tools() -> list[dict]:
    descriptions = {
        "list": "List this task's tracked operations and retained outcomes. Inspect existing runs before launching again; the list is not a completion verdict.",
        "get": "Inspect one operation's run identity, local/external outcome, cleanup and evidence references. Reading never launches work. A succeeded operation is not task approval; verify required criteria independently.",
        "reconcile": "Reconcile the recorded external run after observer cleanup; never relaunch it. Requires the current task cycle and original phase. Never guess missing external identity. End the session after an accepted running receipt or accepted_wait; the daemon resumes it. A wait is not a result or verdict.",
        "cancel": "Stop the owned local operation or invoke its configured external cancellation entry point. Observer exit does not prove remote cancellation; unsupported cancellation remains unknown. Never broad-kill peers or replay writes. On accepted_wait, end this session for daemon resumption; no verdict.",
    }
    return [
        {
            "name": "list_operations" if action == "list" else f"{action}_operation",
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": (
                    {}
                    if action == "list"
                    else {
                        "operation_id": {
                            "type": "string",
                            "description": "Recorded operation UUID; never invent it.",
                        },
                    }
                ),
                "required": [] if action == "list" else ["operation_id"],
            },
            "action": f"operation_{action}",
            "local": True,
        }
        for action, description in descriptions.items()
    ]
