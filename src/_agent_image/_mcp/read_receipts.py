"""Read receipts: a wholesale write-back needs proof that its read arrived whole.

``update_flow_graph``, ``update_spec`` and ``update_execution_plan`` replace a
stored graph, spec or execution plan wholesale. The Claude CLI can replace a
large read with a 2 KB ``<persisted-output>`` file preview when it shares an
assistant message with other tool results (R12/L03), and this server cannot
see that. So a complete, unshortened read of one of these targets ends with a
fresh ``read_receipt``, the LAST key of the result: a preview never holds it,
and ``Read`` of the persisted single-line JSON cuts that line before its end.
The write must echo the receipt of the latest complete read of the same target
in this session. A shortened read issues none, so the write stays refused
until a complete re-read; a ``section`` read issues none and changes nothing.
A target this session never read stays writable (a from-scratch draft).
"""
from __future__ import annotations

import secrets
from typing import Any

READ_RECEIPT_KEY = "read_receipt"

# Guarded write action -> the read action whose complete result carries the
# receipt, and the target named in refusals.
GUARDED_WRITES: dict[str, tuple[str, str]] = {
    "update_flow_graph": ("get_flow_graph", "flow"),
    "update_spec": ("get_spec", "spec"),
    "update_execution_plan": ("get_execution_plan", "execution plan"),
}
RECEIPT_READS = frozenset(read for read, _ in GUARDED_WRITES.values())


def read_receipt_guidance(read_action: str) -> str:
    """The description sentence of a read whose result carries a receipt."""
    write = next(
        write for write, (read, _) in GUARDED_WRITES.items() if read == read_action
    )
    return f"A complete result ends with `{READ_RECEIPT_KEY}`, which {write} must pass."


def without_unserved_receipt_guidance(tools: list[dict]) -> list[dict]:
    """Drop a read's receipt sentence when the session does not serve its write.

    The sentence names the write that must pass the receipt, but a catalog can
    serve the read without the write: the Manager holds ``get_spec`` while
    ``update_spec`` is Planner-only, and General Chat strips
    ``update_execution_plan``. Naming a tool the session cannot call invites
    an Unknown-tool round trip, so the served copy keeps the sentence only
    where the write is served too.
    """
    served = {tool["name"] for tool in tools}
    result = []
    for tool in tools:
        write = next(
            (w for w, (read, _) in GUARDED_WRITES.items() if read == tool["name"]),
            None,
        )
        if write is not None and write not in served:
            sentence = " " + read_receipt_guidance(tool["name"])
            description = tool.get("description", "")
            if sentence in description:
                tool = {**tool, "description": description.replace(sentence, "")}
        result.append(tool)
    return result


def read_receipt_property(read_action: str) -> dict:
    """The ``read_receipt`` input property of a guarded write tool."""
    return {
        "type": "string",
        "description": (
            f"The `{READ_RECEIPT_KEY}` at the END of your latest complete "
            f"{read_action} result for this target, required once you read "
            "it in this session (Manager: this turn). A `<persisted-output>` "
            "preview never "
            f"contains it: call {read_action} again alone in its turn."
        ),
    }


def _keys(prefix: str, *values: Any) -> set[str]:
    return {
        f"{prefix}:{str(value).strip().lower()}"
        for value in values
        if value not in (None, "")
    }


def read_targets(action: str, arguments: dict, result: Any) -> set[str]:
    """Every identifier of the target one guarded read returned."""
    body = result if isinstance(result, dict) else {}
    if action == "get_flow_graph":
        return _keys(
            "flow",
            arguments.get("flow_id"),
            arguments.get("flow_name"),
            body.get("flow_id"),
            body.get("name"),
        )
    if action == "get_spec":
        keys = _keys("spec", arguments.get("spec_id"))
        keys |= _keys("spec-ws", arguments.get("workstream_id"))
        spec = body.get("spec")
        if isinstance(spec, dict):
            keys |= _keys("spec", spec.get("id"))
            if spec.get("workstream_id"):
                keys |= _keys("spec-ws", spec.get("workstream_id"))
            else:  # an office-shared spec is written by its name
                keys |= _keys("spec-name", spec.get("name"))
        return keys
    if action == "get_execution_plan":
        return _keys("plan", arguments.get("scope_id"), body.get("scope_id"))
    return set()


def write_targets(action: str, arguments: dict) -> set[str]:
    """The identifiers of the target one guarded write replaces."""
    if action == "update_flow_graph":
        return _keys("flow", arguments.get("flow_id"), arguments.get("flow_name"))
    if action == "update_spec":
        if arguments.get("workstream_id"):
            return _keys("spec-ws", arguments.get("workstream_id"))
        return _keys("spec-name", arguments.get("name"))
    if action == "update_execution_plan":
        return _keys("plan", arguments.get("scope_id"))
    return set()


class ReadReceipts:
    """This session's latest whole read of each guarded target."""

    def __init__(self) -> None:
        # target key -> receipt of its latest whole read; None when that
        # read was shortened (no complete read to write back from).
        self._latest: dict[str, str | None] = {}

    @staticmethod
    def new_receipt(action: str) -> str | None:
        """A fresh receipt for a whole read of ``action``; None elsewhere."""
        return secrets.token_hex(6) if action in RECEIPT_READS else None

    def record(
        self, action: str, arguments: dict, result: Any, receipt: str | None
    ) -> None:
        """Remember a whole read: its receipt, or None when it was shortened."""
        for key in read_targets(action, arguments, result):
            self._latest[key] = receipt

    def refusal(self, action: str, arguments: dict) -> str:
        """Why a guarded write is refused; empty when it may proceed."""
        if action not in GUARDED_WRITES:
            return ""
        read, target = GUARDED_WRITES[action]
        latest = [
            self._latest[key]
            for key in write_targets(action, arguments)
            if key in self._latest
        ]
        if not latest:
            return ""
        if None in latest:
            return (
                f"{action} refused: your last {read} read of this {target} in "
                "this session was shortened to fit the tool-result limit (see "
                "its _truncated notice), so what you hold is incomplete. "
                f"{action} replaces the stored {target} wholesale; writing it "
                "back would drop the omitted parts. Call "
                f"{read} again and edit only from a complete read. If the read "
                f"stays shortened, do not write the {target}: report that it "
                "is too large to edit in one read."
            )
        if arguments.get(READ_RECEIPT_KEY) in latest and len(set(latest)) == 1:
            return ""
        return (
            f"{action} refused: pass `{READ_RECEIPT_KEY}` from the END of your "
            f"latest complete {read} result for this {target} in this "
            "session. If you do not have it, that read reached you only as a "
            f"`<persisted-output>` preview, not in full: call {read} again "
            "alone in its turn and write from that complete result."
        )
