"""Token-usage split for session logs (F07 measurement).

The Claude CLI reports usage per API call (``assistant`` frames) and
cumulatively per run (the ``result`` frame). Uncached input, prompt-cache
writes and prompt-cache reads have very different cost and latency, and the
Manager's session-rotation threshold counts all three — so logs must show
them separately to measure what a prompt change actually saves. This module
only parses and formats; it never feeds a decision (rotation and retry keep
their own arithmetic).
"""
from __future__ import annotations

from dataclasses import dataclass


_KEYS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


def _is_count(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _count(usage: dict, key: str) -> int:
    value = usage.get(key)
    if not _is_count(value):
        return 0
    return int(value) if value > 0 else 0


@dataclass(frozen=True)
class UsageBreakdown:
    """One usage object's token counts, split by kind."""

    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0

    @classmethod
    def from_usage(cls, usage: object) -> "UsageBreakdown | None":
        """Parse a CLI ``usage`` object; ``None`` when none was reported.

        ``None`` (logged as ``unavailable``) covers a missing, non-dict or
        empty usage and one with no numeric count at all, so an absent
        figure never looks measured. Once any count is present, a malformed
        sibling field counts as zero.
        """
        if not isinstance(usage, dict):
            return None
        if not any(_is_count(usage.get(key)) for key in _KEYS):
            return None
        return cls(
            input_tokens=_count(usage, "input_tokens"),
            cache_creation_input_tokens=_count(usage, "cache_creation_input_tokens"),
            cache_read_input_tokens=_count(usage, "cache_read_input_tokens"),
            output_tokens=_count(usage, "output_tokens"),
        )

    def describe(self) -> str:
        """``input=… cache_creation=… cache_read=… output=…`` for a log line."""
        return (
            f"input={self.input_tokens} "
            f"cache_creation={self.cache_creation_input_tokens} "
            f"cache_read={self.cache_read_input_tokens} "
            f"output={self.output_tokens}"
        )


def describe_usage(usage: UsageBreakdown | None) -> str:
    """Log text for ``usage``; ``unavailable`` when no usage was reported.

    A stream that ended without the frame carrying usage must not log zeros
    (or an earlier attempt's figures) as if they were measured.
    """
    return usage.describe() if usage is not None else "unavailable"
