"""Daemon-side Manager-poke idempotency LRU (T3.2.1, 07/G14).

The poke ingest paths in ``_manager_action_requests`` build
*deterministic* ``conversation_id`` values ("so duplicates don't
double-prompt"), but until this module nothing ever CHECKED them —
the deterministic ids were aspirational. With the Phase-3 backstops
(the action-request ager's 30-minute re-poke, the connector-reconnect
re-derive of ``scope_completed`` / ``task_completed``) a poke that DID
land would be delivered again and double-prompt the Manager every
time, making those backstops unsafe to ship.

This LRU is the daemon half of the 07 §5 "idempotent double-delivery"
obligation (the backend half lives with the ager, T3.1.1):

* Bounded (default 256 entries) — oldest-marked id evicted first.
* In-memory only. A daemon restart forgets it; the worst case is ONE
  duplicate Manager prompt after a restart, which is acceptable.
* Callers check :meth:`seen` BEFORE dispatching and :meth:`mark`
  AFTER a *successful* Manager turn (``handle_chat_message``'s T3.2.5
  return flag). Marking only on success keeps the re-poke mechanisms
  useful: a poke whose Manager turn FAILED is not recorded, so the
  ager / reconnect re-delivery passes the dedup check and actually
  reaches the Manager.
"""

from __future__ import annotations

from collections import OrderedDict

# Default capacity. 256 comfortably covers every distinct poke an
# office produces between daemon restarts while bounding memory to a
# few KB of id strings.
DEFAULT_MAX_SIZE = 256


class PokeDedupLRU:
    """Small bounded LRU set of processed poke ``conversation_id``s."""

    def __init__(self, max_size: int = DEFAULT_MAX_SIZE) -> None:
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self._max_size = max_size
        # OrderedDict used as an ordered set: key = conversation_id,
        # value unused. Oldest insertion is evicted first.
        self._entries: "OrderedDict[str, None]" = OrderedDict()

    def seen(self, conversation_id: str) -> bool:
        """Return True iff this id was already marked as processed.

        A hit refreshes the entry's recency so a hot id (e.g. an
        auto-decide row the ager keeps re-poking while the user is
        away) isn't evicted by unrelated churn.
        """
        if not conversation_id:
            return False
        if conversation_id in self._entries:
            self._entries.move_to_end(conversation_id)
            return True
        return False

    def mark(self, conversation_id: str) -> None:
        """Record an id as processed, evicting the oldest past capacity."""
        if not conversation_id:
            return
        self._entries[conversation_id] = None
        self._entries.move_to_end(conversation_id)
        while len(self._entries) > self._max_size:
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, conversation_id: str) -> bool:
        return conversation_id in self._entries
