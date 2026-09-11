"""Reconcile incremental and complete CLI messages by message/block identity."""

from __future__ import annotations


class ManagerTextStream:
    def __init__(self) -> None:
        self._messages: dict[str, dict[int, str]] = {}
        self._message_id = ""
        self._anonymous_index = 0
        self._block_index = 0
        self._block_kind = ""
        self._stream_offset = 0
        self._visible_blocks: set[tuple[str, int]] = set()
        self._completed: set[str] = set()

    def _start_message(self, message_id: str = "") -> None:
        if not message_id:
            self._anonymous_index += 1
            message_id = f"anonymous:{self._anonymous_index}"
        self._message_id = message_id
        self._messages.setdefault(message_id, {})
        self._block_kind = ""

    def _append(self, index: int, text: str) -> str:
        if not text or self._message_id in self._completed:
            return ""
        blocks = self._messages[self._message_id]
        identity = (self._message_id, index)
        separator = ""
        if identity not in self._visible_blocks:
            separator = "\n\n" if self._visible_blocks else ""
            self._visible_blocks.add(identity)
        blocks[index] = blocks.get(index, "") + text
        return separator + text

    def _increment(self, text: str) -> str:
        emitted = self._messages[self._message_id].get(self._block_index, "")
        offset = self._stream_offset
        self._stream_offset += len(text)
        overlap = min(len(text), max(0, len(emitted) - offset))
        if emitted[offset : offset + overlap] != text[:overlap]:
            return ""
        return self._append(self._block_index, text[overlap:])

    def incremental(self, event: dict) -> str:
        kind = event.get("type")
        if kind == "message_start":
            self._start_message((event.get("message") or {}).get("id", ""))
        elif kind == "content_block_start":
            if not self._message_id:
                self._start_message()
            self._block_index = event.get(
                "index", len(self._messages[self._message_id])
            )
            self._stream_offset = 0
            block = event.get("content_block") or {}
            self._block_kind = block.get("type", "")
            if self._block_kind == "text":
                return self._increment(block.get("text", ""))
        elif kind == "content_block_delta" and self._block_kind == "text":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                return self._increment(delta.get("text", ""))
        elif kind == "content_block_stop":
            self._block_kind = ""
        return ""

    def complete(self, message: dict) -> list[str]:
        message_id = message.get("id") or ""
        if message_id and message_id in self._completed:
            return []
        if message_id and message_id != self._message_id:
            current = self._messages.get(self._message_id, {})
            complete_blocks = message.get("content") or []
            matches_partial = current and all(
                index < len(complete_blocks)
                and complete_blocks[index].get("type") == "text"
                and (complete_blocks[index].get("text") or "").startswith(text)
                for index, text in current.items()
            )
            if self._message_id.startswith("anonymous:") and matches_partial:
                blocks = self._messages.pop(self._message_id)
                self._visible_blocks = {
                    (message_id if identity == self._message_id else identity, index)
                    for identity, index in self._visible_blocks
                }
                self._messages[message_id] = blocks
                self._message_id = message_id
            else:
                self._start_message(message_id)
        elif not self._message_id:
            self._start_message(message_id)
        chunks = []
        for index, block in enumerate(message.get("content") or []):
            if block.get("type") != "text":
                continue
            text = block.get("text") or ""
            emitted = self._messages[self._message_id].get(index, "")
            if text.startswith(emitted):
                chunk = self._append(index, text[len(emitted) :])
                if chunk:
                    chunks.append(chunk)
        self._completed.add(self._message_id)
        self._message_id = ""
        self._block_kind = ""
        return chunks
