"""Optional domain-neutral check plans and current-task evidence tools."""


def verification_plan_property() -> dict:
    check = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
                "pattern": "^[a-zA-Z0-9_.-]+$",
            },
            "criterion_indices": {
                "type": "array",
                "minItems": 1,
                "maxItems": 100,
                "uniqueItems": True,
                "items": {"type": "integer", "minimum": 1},
            },
            "owner": {"type": "string", "enum": ["executor", "reviewer", "automation"]},
            "method": {"type": "string", "minLength": 1, "maxLength": 2000},
            "scope": {"type": "string", "minLength": 1, "maxLength": 4000},
            "required": {"type": "boolean", "default": True},
            "freshness": {
                "type": "string",
                "enum": ["current_cycle", "current_attempt"],
                "default": "current_cycle",
                "description": "Automation uses current_cycle; current_attempt is for fresh executor/reviewer inspection.",
            },
            "max_age_seconds": {"type": "integer", "minimum": 1, "maximum": 31536000},
            "automation_phase": {
                "type": "string",
                "enum": ["execute", "review"],
                "default": "execute",
            },
        },
        "required": ["id", "criterion_indices", "owner", "method", "scope"],
        "allOf": [
            {
                "if": {
                    "properties": {"owner": {"const": "automation"}},
                    "required": ["owner"],
                },
                "then": {"properties": {"freshness": {"const": "current_cycle"}}},
            }
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "description": "Optional plan: group criteria by owner/method/scope. Cover every criterion with a required check; retain prose and independent gates. Ask tasks only use executor checks or execute-phase automation.",
        "properties": {
            "version": {"type": "integer", "const": 1},
            "checks": {"type": "array", "minItems": 1, "maxItems": 100, "items": check},
        },
        "required": ["version", "checks"],
    }


def verification_fingerprint_property() -> dict:
    return {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$",
        "description": "SHA256 of delivered artifacts and relevant source, environment and method input identities; match the operation identity. No code revision required.",
    }


def verification_tools() -> list[dict]:
    return [
        {
            "name": "record_verification_evidence",
            "description": "Record evidence for this task's current agent/phase; backend binds cycle, attempt and plan. Reuse receipt_key only for identical evidence. Automation needs a matching tracked outcome; pass also requires succeeded outcome and confirmed cleanup. Never invent identities or evidence.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "task_id": {"type": "string", "description": "Current task UUID."},
                    "check_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 100,
                        "description": "Exact plan check ID.",
                    },
                    "receipt_key": {
                        "type": "string",
                        "format": "uuid",
                        "description": "New UUID per evidence receipt; retain on retry.",
                    },
                    "input_fingerprint": verification_fingerprint_property(),
                    "source_identity": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 8000,
                        "description": "Actual deliverable/source versions and relevant context.",
                    },
                    "artifact_refs": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 50,
                        "items": {"type": "string", "minLength": 1, "maxLength": 2000},
                        "description": "Inspectable evidence links or paths.",
                    },
                    "outcome": {
                        "type": "string",
                        "enum": ["pass", "fail", "partial"],
                        "description": "Observed check result.",
                    },
                    "summary": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 8000,
                        "description": "Findings and applicability limits.",
                    },
                    "operation_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Required trusted operation for an automation-owned check.",
                    },
                },
                "required": [
                    "task_id",
                    "check_id",
                    "receipt_key",
                    "input_fingerprint",
                    "source_identity",
                    "artifact_refs",
                    "outcome",
                    "summary",
                ],
            },
            "action": "record_verification_evidence",
        },
        {
            "name": "get_verification_status",
            "description": "Inspect this task's required check coverage and evidence applicability. Optional fingerprint selects the current input set. Missing/stale/failed checks block acceptance; this read is not a verdict and never runs checks.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "task_id": {"type": "string", "description": "Current task UUID."},
                    "input_fingerprint": verification_fingerprint_property(),
                },
                "required": ["task_id"],
            },
            "action": "get_verification_status",
        },
    ]
