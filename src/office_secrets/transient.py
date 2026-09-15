import json
import re
from datetime import UTC, datetime

from src.office_secrets.store import (
    OfficeSecretStoreError,
    _atomic_write,
    _office_secrets_path,
    fingerprint_value,
)

_NAME = re.compile(r"^CBCL_INPUT_[0-9A-F]{32}_[1-9][0-9]{0,5}$")


def _directory(office_slug):
    office_path = _office_secrets_path(office_slug)
    return office_path.parent / "transient" / office_path.stem


def set_transient_secret(office_slug, name, value, expires_at, *, binding=None):
    if not _NAME.fullmatch(name) or not isinstance(value, str) or not value:
        raise OfficeSecretStoreError("Invalid secure input")
    try:
        deadline = datetime.fromisoformat(expires_at)
        remaining = (deadline - datetime.now(UTC)).total_seconds()
    except (ValueError, TypeError):
        raise OfficeSecretStoreError("Invalid secure input expiry") from None
    if not 0 < remaining <= 3600:
        raise OfficeSecretStoreError("Secure input expired; request a fresh attempt")
    record = {"value": value, "expires_at": deadline.isoformat()}
    if binding is not None:
        record["binding"] = binding
    record_path = _directory(office_slug) / f"{name}.json"
    if record_path.exists():
        existing = _read_record(record_path)
        if existing != record:
            raise OfficeSecretStoreError(
                "This secure input was already submitted; request a fresh attempt"
            )
    _atomic_write(record_path, record)
    return fingerprint_value(value)


def _read_record(record_path):
    try:
        record = json.loads(record_path.read_text())
        if not isinstance(record, dict) or not isinstance(record.get("value"), str):
            raise ValueError
        deadline = datetime.fromisoformat(record["expires_at"])
        if deadline.tzinfo is None:
            raise ValueError
        return record
    except (OSError, ValueError, KeyError, TypeError):
        raise OfficeSecretStoreError(
            "Secure input store is unreadable; request a fresh attempt"
        ) from None


def purge_expired_inputs(office_slug):
    directory = _directory(office_slug)
    if not directory.exists():
        return
    for record_path in directory.glob("CBCL_INPUT_*.json"):
        if not _NAME.fullmatch(record_path.stem):
            continue
        record = _read_record(record_path)
        if datetime.fromisoformat(record["expires_at"]) <= datetime.now(UTC):
            record_path.unlink(missing_ok=True)


def delete_transient_secret(office_slug, name):
    if not _NAME.fullmatch(name):
        raise OfficeSecretStoreError("Invalid secure input name")
    (_directory(office_slug) / f"{name}.json").unlink(missing_ok=True)


def resolve_human_action_input(
    office_slug, request_id, task_id, script_name, variable_name
):
    import uuid

    try:
        request_identity = uuid.UUID(str(request_id))
    except (ValueError, TypeError):
        raise OfficeSecretStoreError("Invalid secure request reference") from None
    name = f"CBCL_INPUT_{request_identity.hex.upper()}_1"
    record = _read_record(_directory(office_slug) / f"{name}.json")
    expected = {
        "task_id": str(task_id),
        "script_name": script_name,
        "variable_name": variable_name,
    }
    if record.get("binding") != expected:
        raise OfficeSecretStoreError(
            "Secure input belongs to a different task, script or variable"
        )
    if datetime.fromisoformat(record["expires_at"]) <= datetime.now(UTC):
        raise OfficeSecretStoreError(
            "Secure input expired; request a fresh authorization attempt"
        )
    return record["value"]


def human_action_overrides(overrides, declared_variables):
    references = {}
    for variable_name, value in (overrides or {}).items():
        if not isinstance(value, dict) or "from_human_action" not in value:
            continue
        if (
            declared_variables.get(variable_name) is not True
            or set(value) != {"from_human_action"}
            or not isinstance(value["from_human_action"], str)
        ):
            raise OfficeSecretStoreError(
                "Human input overrides require a declared secret variable and only a request reference"
            )
        references[variable_name] = value["from_human_action"]
    return references
