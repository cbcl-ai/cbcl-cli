"""Tool-result text the model receives (shared decision 3: C1-G2, C4c-G1, C4c-G3).

One rendering path for every successful Cubicle tool result:

* **Compact JSON with real characters** — ``separators=(",", ":")`` and
  ``ensure_ascii=False``. Indentation and ``\\uXXXX`` escapes used to cost
  ~1.5x (and up to 6x for non-Latin text), so legal content was cut far below
  its cap.
* **Per-action limits the CLI inlines by default.** The pinned Claude CLI
  (2.1.259, claude-agent-sdk 0.2.152) replaces an MCP result with a file path
  plus a 2 KB preview when it is over its persistence threshold, and
  token-counts unannotated results against ``MAX_MCP_OUTPUT_TOKENS`` (25,000
  by default, remotely configurable). A tool that declares
  ``_meta["anthropic/maxResultSizeChars"] = N`` in ``tools/list`` skips the
  token check and is inlined up to ``min(N, 500_000)`` characters, counted as
  JavaScript string length (UTF-16 code units). Every Cubicle tool declares
  its limit (``tool_meta``) and every result is rendered to at most that many
  UTF-16 units. No session environment variable is needed.
* **What the CLI can still replace (R12).** Under its default remote
  configuration the CLI inlines a single result within its declared limit.
  Two remote settings can change that. A per-assistant-message budget of
  200,000 characters (``Pnr``, gated by ``tengu_hawthorn_steeple``, off by
  default) totals every tool result of one assistant message and swaps the
  largest for a ``<persisted-output>`` file preview when the total is over
  it; no Cubicle tool is exempt. A per-tool override
  (``tengu_velvet_ibis``) can lower an annotated tool's threshold. Every
  limit here therefore stays below the per-message budget
  (``LARGE_RESULT_LIMIT``), so one read alone is never swapped; a large read
  that shares its message with other results still can be, and the server
  cannot see that. Every large-read tool description (``LARGE_READ_GUIDANCE``)
  and every large-read result the CLI's 2,000-character preview could show
  only in part (a leading ``_delivery`` note, inside that preview) tell the
  model that a preview is not the result: re-read alone before writing back,
  approving or proposing from it. The wholesale write-backs also need the
  ``read_receipt`` that ends a complete read (``read_receipts.py``).
* **Field-aware bounding.** A result still over its limit stays valid JSON:
  the longest text fields are shortened in the middle (criteria, verification
  steps and the newest activity are kept), long lists lose middle items, and a
  leading ``_truncated`` object names every shortened path, its full size and
  what to do. Only when that cannot fit is the text cut, at a code-point
  boundary, with an explicit marker saying the JSON is incomplete.
* **Section reads (L01).** A large read accepts ``section`` (a path the
  notice names, such as ``brief.inputs`` or ``graph.blocks[3]``) and
  ``offset``. The server re-fetches the result and returns just that part:
  text and lists in pages that continue at ``next_offset``, with a
  ``fingerprint`` that changes when the value does. The backend never sees
  these arguments; paths refer to the result the model received (after the
  lean projection).
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .read_receipts import READ_RECEIPT_KEY

# Claude CLI 2.1.259: the ``_meta`` key read per tool, and the ceiling it is
# clamped to (``RFe``). Unannotated tools persist above 50,000 characters.
CLI_RESULT_SIZE_META_KEY = "anthropic/maxResultSizeChars"
CLI_RESULT_SIZE_CEILING = 500_000

# Claude CLI 2.1.259 ``Pnr``: the per-assistant-message tool-result budget
# (remote flag ``tengu_hawthorn_steeple``, default off). Re-check on a CLI bump.
CLI_PER_MESSAGE_RESULT_BUDGET = 200_000
# The largest per-tool limit: under the per-message budget with headroom, so a
# single read is inlined even while that budget is switched on.
LARGE_RESULT_LIMIT = 190_000

DEFAULT_RESULT_LIMIT = 50_000
# Reads whose legal content exceeds the default. Keyed by backend action (a
# worker's get_my_brief dispatches as get_task_detail).
RESULT_LIMITS: dict[str, int] = {
    # Custom agent fields reach 105,000 characters, so every target read fits;
    # a proposal carries before/after values of up to 200,000 characters and
    # is shortened explicitly past this limit. The Manager must copy
    # ``before`` exactly, from a target read.
    "inspect_configuration": LARGE_RESULT_LIMIT,
    # A graph is capped at 96 KiB compact; the envelope adds manifest schema
    # and trigger config. update_flow_graph replaces the graph wholesale, and
    # a shortened read refuses the write-back until a complete re-read.
    "get_flow_graph": LARGE_RESULT_LIMIT,
    # Brief fields reach 50,000 characters each; specs have no content cap;
    # execution plans reach about 64,000 characters of text.
    "get_task_detail": LARGE_RESULT_LIMIT,
    "get_spec": LARGE_RESULT_LIMIT,
    "get_execution_plan": LARGE_RESULT_LIMIT,
}

# Reads with a large limit: their results feed wholesale write-backs,
# approvals and proposals, so they offer section reads and carry the preview
# guidance (``LARGE_READ_GUIDANCE``, ``_DELIVERY_NOTE``).
LARGE_READ_ACTIONS = frozenset(RESULT_LIMITS)

# Model-facing contract shared by every large-read tool definition.
LARGE_READ_GUIDANCE = (
    "Read large results alone: a `<persisted-output>` preview is not the "
    "result — re-read before writing back, approving or proposing."
)


# A real part of each large read's result, as its ``section`` example.
_SECTION_EXAMPLES: dict[str, str] = {
    "inspect_configuration": "fields.claude_md_content",
    "get_flow_graph": "graph.blocks",
    "get_task_detail": "brief.inputs",
    "get_spec": "spec.content",
    "get_execution_plan": "execution_plan.task_breakdown",
}


def section_read_properties(action: str) -> dict:
    """Fresh ``section``/``offset`` input properties for a large-read tool."""
    return {
        "section": {
            "type": "string",
            "description": (
                "Read one part in full: a path from `_truncated`, e.g. "
                f"`{_SECTION_EXAMPLES[action]}`."
            ),
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": "Where the `section` page starts: a `next_offset`.",
        },
    }


# Claude CLI 2.1.259 persists a swapped result as the pretty-printed JSON of
# its content blocks and previews that file's first ``cFe`` characters. A
# result that fits there whole reaches the model complete; any larger one may
# not (a per-tool threshold override, ``tengu_velvet_ibis``, can swap results
# of any size), so a large read's result leads with a note inside the preview.
CLI_PREVIEW_CHARS = 2_000
# The content-block JSON around the escaped text (46 characters in 2.1.259),
# with headroom.
_PREVIEW_WRAPPER_CHARS = 100
_DELIVERY_NOTE = (
    "Only if this text is inside a <persisted-output> preview: the full "
    "result did not reach you. Call this tool again alone in its turn before "
    "writing back, approving or proposing anything from it."
)
_DELIVERY_OPEN = (
    '{"_delivery":' + json.dumps(_DELIVERY_NOTE, ensure_ascii=False) + ","
)


def _previewed_whole(text: str) -> bool:
    """Whether the CLI's preview of ``text`` would hold all of it: its JSON
    escaping adds one character per quote or backslash."""
    escaped = js_length(text) + text.count('"') + text.count("\\")
    return escaped + _PREVIEW_WRAPPER_CHARS <= CLI_PREVIEW_CHARS


def strip_delivery_note(text: str) -> str:
    """``text`` without a leading ``_delivery`` note (for human-facing previews)."""
    if text.startswith(_DELIVERY_OPEN):
        return "{" + text[len(_DELIVERY_OPEN):]
    return text


# Never shortened: the review contract and bookkeeping a follow-up call needs.
PROTECTED_KEYS = frozenset({
    "acceptance_criteria",
    "verification_steps",
    "verification_plan",
})
# Lists whose LAST item is the newest and must survive intact.
NEWEST_LAST_LISTS = frozenset({"recent_activities"})

# What a shortened read of these actions means for a follow-up write.
_REMAINDER_HINTS: dict[str, str] = {
    "get_flow_graph": (
        "update_flow_graph for this flow is refused in this session until a "
        "complete get_flow_graph read (a section read does not count): "
        "writing back this graph would drop the omitted blocks, edges or text."
    ),
    "get_spec": (
        "update_spec for this spec is refused in this session until a "
        "complete get_spec read (a section read does not count): writing "
        "back this spec would drop the omitted text."
    ),
    "get_execution_plan": (
        "update_execution_plan for this scope is refused in this session "
        "until a complete get_execution_plan read (a section read does not "
        "count): writing back this plan would drop the omitted text or tasks."
    ),
    "inspect_configuration": (
        "propose_configuration needs every `before` value exactly: copy it "
        "from a complete section read of that field, never from shortened text."
    ),
}
_SECTION_GUIDANCE = (
    "Read an omitted part with this tool's `section` (its path) and `offset` "
    "(its `next_offset`), alone in its turn; continue from each returned "
    "`next_offset` until it is null. A different `fingerprint` means the value "
    "changed since this read."
)

_MIN_SHRINK_CHARS = 400  # a string is never shortened below this
_MAX_LIST_PASSES = 24


def result_limit(action: str) -> int:
    """The UTF-16 length limit for one ``action``'s result text."""
    return min(RESULT_LIMITS.get(action, DEFAULT_RESULT_LIMIT), CLI_RESULT_SIZE_CEILING)


def tool_meta(action: str) -> dict:
    """``tools/list`` ``_meta`` that makes the CLI inline results up to the limit."""
    return {CLI_RESULT_SIZE_META_KEY: result_limit(action)}


def js_length(text: str) -> int:
    """Length as the CLI measures it: JavaScript string length (UTF-16 units)."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def to_json_text(value: Any) -> str:
    """Compact JSON with real (unescaped) characters."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


@dataclass
class RenderedResult:
    text: str
    truncated: bool = False
    shortened: list[str] = field(default_factory=list)
    # The ``read_receipt`` that ends the text; None when none was embedded.
    receipt: str | None = None


class SectionReadError(ValueError):
    """A section read the model asked for cannot be served; the message says why."""


@dataclass(frozen=True)
class SectionRequest:
    path: str
    offset: int = 0


def render_result(
    result: Any, action: str, limit: int | None = None, receipt: str | None = None
) -> RenderedResult:
    """Render ``result`` for the model within ``limit`` UTF-16 units.

    ``receipt`` becomes the LAST key of a complete (unshortened) object
    result, where no ``<persisted-output>`` preview reaches it.
    """
    limit = result_limit(action) if limit is None else limit
    return _deliver(
        lambda budget: _render_whole(result, action, budget, limit, receipt),
        action,
        limit,
    )


def _receipt_member(receipt: str) -> str:
    return to_json_text({READ_RECEIPT_KEY: receipt})[1:-1]


def _render_whole(
    result: Any, action: str, budget: int, declared: int, receipt: str | None
) -> RenderedResult:
    """``result`` within ``budget``; notices and markers name ``declared``."""
    member = _receipt_member(receipt) if receipt and isinstance(result, dict) else ""
    if member:
        budget -= js_length(member) + 1  # its separating comma
    text = to_json_text(result)
    if js_length(text) <= budget:
        if not member:
            return RenderedResult(text)
        separator = "," if len(text) > 2 else ""
        return RenderedResult(text[:-1] + separator + member + "}", receipt=receipt)
    original_length = js_length(text)
    if isinstance(result, dict):
        bounded = _bound_dict(
            json.loads(text), action, budget, declared, original_length
        )
        if bounded is not None:
            return bounded
    return RenderedResult(
        _cut_text(
            text, budget, declared, original_length, action, _part_paths(result, "")
        ),
        truncated=True,
    )


def _deliver(
    render: Callable[[int], RenderedResult], action: str, limit: int
) -> RenderedResult:
    """Lead a large read's result with ``_DELIVERY_NOTE`` unless the CLI's
    preview would show it whole.

    The note sits inside the CLI's ``<persisted-output>`` preview, so a result
    swapped for a file still tells the model it did not arrive.
    """
    rendered = render(limit)
    if (
        action not in LARGE_READ_ACTIONS
        or not rendered.text.startswith("{")
        or _previewed_whole(rendered.text)
    ):
        return rendered
    note_chars = js_length(_DELIVERY_OPEN) - 1  # the object's own "{" is reused
    if js_length(rendered.text) + note_chars > limit:
        rendered = render(limit - note_chars)
    return RenderedResult(
        _DELIVERY_OPEN + rendered.text[1:],
        rendered.truncated,
        rendered.shortened,
        rendered.receipt,
    )


# ── section reads ───────────────────────────────────────────────────

_SECTION_PATH = re.compile(r"[^.\[\]]+(?:\.[^.\[\]]+|\[\d+\])*")
_SECTION_STEP = re.compile(r"\.?([^.\[\]]+)|\[(\d+)\]")


def pop_section_request(action: str, params: dict) -> SectionRequest | None:
    """Take the ``section``/``offset`` arguments of a large read out of ``params``.

    They select part of the result this server renders; the backend never
    sees them. ``None`` means an ordinary whole read.
    """
    if action not in LARGE_READ_ACTIONS:
        return None
    section = params.pop("section", None)
    offset = params.pop("offset", None)
    if isinstance(offset, str) and offset.strip().isdigit():
        offset = int(offset)
    if section in (None, "") and offset in (None, 0):
        return None
    if not isinstance(section, str) or not _SECTION_PATH.fullmatch(section.strip()):
        raise SectionReadError(
            "`offset` continues a `section`: pass `section` as a path from the "
            f"`_truncated` notice, e.g. `{_SECTION_EXAMPLES[action]}`."
        )
    if offset is None:
        offset = 0
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise SectionReadError(
            "`offset` must be a non-negative integer: the `next_offset` of the "
            "notice or of the previous section read."
        )
    return SectionRequest(section.strip(), offset)


def render_section(
    result: Any, action: str, request: SectionRequest, limit: int | None = None
) -> RenderedResult:
    """Render ONE part of ``result`` (a page of it, for text and lists)."""
    limit = result_limit(action) if limit is None else limit
    value = _resolve_section(result, request.path)
    return _deliver(
        lambda budget: _render_section_value(value, action, request, budget, limit),
        action,
        limit,
    )


def _resolve_section(result: Any, path: str) -> Any:
    node, walked = result, ""
    for match in _SECTION_STEP.finditer(path):
        key, index = match.groups()
        where = f"`{walked}`" if walked else "the result"
        if isinstance(node, list) and (index is not None or key.isdigit()):
            position = int(index if index is not None else key)
            if position >= len(node):
                raise SectionReadError(
                    f"No `{path}` in this result: {where} has {len(node)} items."
                )
            node, walked = node[position], _join(walked, position)
        elif isinstance(node, dict) and key is not None and key in node:
            node, walked = node[key], _join(walked, key)
        else:
            if isinstance(node, dict):
                parts = "keys " + ", ".join(f"`{k}`" for k in list(node)[:30])
            elif isinstance(node, list):
                parts = f"{len(node)} items, read by index: `{walked}[0]`"
            else:
                parts = f"no parts (it is {type(node).__name__})"
            raise SectionReadError(f"No `{path}` in this result: {where} has {parts}.")
    return node


def _fingerprint(value: Any) -> str:
    text = value if isinstance(value, str) else to_json_text(value)
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def _render_section_value(
    value: Any, action: str, request: SectionRequest, limit: int, declared: int
) -> RenderedResult:
    """One part within ``limit``; notices and markers name ``declared``."""
    path, offset = request.path, request.offset
    fingerprint = _fingerprint(value)
    if isinstance(value, (str, list)):
        return _page(value, path, offset, fingerprint, limit)
    if offset:
        raise SectionReadError(
            f"`offset` pages text or a list; `{path}` is a single "
            f"{type(value).__name__}: read it without `offset`, or one of its "
            f"parts, e.g. `{path}.<key>`."
        )
    head = to_json_text({"section": path, "fingerprint": fingerprint})[:-1]
    head += ',"value":'
    budget = limit - js_length(head) - 1
    text = to_json_text(value)
    if js_length(text) <= budget:
        return RenderedResult(head + text + "}")
    original_length = js_length(text)
    if isinstance(value, dict):
        bounded = _bound_dict(
            json.loads(text), action, budget, declared, original_length, root=path
        )
        if bounded is not None:
            return RenderedResult(head + bounded.text + "}", True, bounded.shortened)
    cut = _cut_text(
        head + text + "}",
        limit,
        declared,
        original_length,
        action,
        _part_paths(value, path),
    )
    return RenderedResult(cut, truncated=True)


def _page(
    value: str | list, path: str, offset: int, fingerprint: str, limit: int
) -> RenderedResult:
    """The longest page of ``value`` from ``offset`` within ``limit``."""
    is_text = isinstance(value, str)
    total = len(value)
    if offset > total or (offset == total and total):
        unit = "characters" if is_text else "items"
        raise SectionReadError(
            f"offset {offset:,} is past the end of `{path}` ({total:,} {unit})."
        )

    def render(end: int) -> str:
        page: dict[str, Any] = {
            "section": path,
            "offset": offset,
            "next_offset": end if end < total else None,
            "total_chars" if is_text else "total_items": total,
            "fingerprint": fingerprint,
        }
        if not is_text and end == offset < total:
            page["note"] = (
                f"Item {offset} alone does not fit in one result: read it with "
                f"`section` `{path}[{offset}]`, then continue at `next_offset`."
            )
            page["next_offset"] = offset + 1 if offset + 1 < total else None
        page["text" if is_text else "items"] = value[offset:end]
        return to_json_text(page)

    if js_length(render(total)) <= limit:
        return RenderedResult(render(total))
    low, high, end = offset + 1, total - 1, offset
    while low <= high:  # rendering grows with ``end`` below ``total``
        middle = (low + high) // 2
        if js_length(render(middle)) <= limit:
            end, low = middle, middle + 1
        else:
            high = middle - 1
    if is_text:
        end = max(end, offset + 1)  # always progress; a page holds one character
    return RenderedResult(render(end), truncated=True, shortened=[path])


def _part_paths(value: Any, root: str) -> list[str]:
    """Paths of the largest parts of a dict, for a cut result's marker."""
    if not isinstance(value, dict):
        return []
    largest = sorted(value, key=lambda key: -len(to_json_text(value[key])))
    return [_join(root, key) for key in largest[:8]]


# ── field-aware bounding ────────────────────────────────────────────


@dataclass
class _Slot:
    parent: Any
    key: Any
    path: str
    original: Any


def _join(path: str, key: Any) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else str(key)


def _collect(
    node: Any,
    path: str,
    strings: list[_Slot],
    lists: list[_Slot],
    originals: dict[int, int] | None = None,
) -> None:
    """Shrinkable string leaves and lists of an unprotected subtree."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in PROTECTED_KEYS:
                continue
            child = _join(path, key)
            if isinstance(value, str):
                strings.append(_Slot(node, key, child, value))
            elif isinstance(value, list):
                lists.append(_Slot(node, key, child, value))
                _collect_list(
                    value, child, key in NEWEST_LAST_LISTS, strings, lists, originals
                )
            else:
                _collect(value, child, strings, lists, originals)


def _collect_list(
    items: list,
    path: str,
    newest_last: bool,
    strings: list[_Slot],
    lists: list[_Slot],
    originals: dict[int, int] | None = None,
) -> None:
    last = len(items) - 1
    # A trimmed list keeps its original last item after the omission marker;
    # paths name that item by its original index, as a section read needs.
    original_last = (originals or {}).get(id(items))
    for index, value in enumerate(items):
        if newest_last and index == last:
            continue  # the newest entry is kept whole
        shown = original_last if original_last is not None and index == last else index
        child = _join(path, shown)
        if isinstance(value, str):
            strings.append(_Slot(items, index, child, value))
        elif isinstance(value, list):
            lists.append(_Slot(items, index, child, value))
            _collect_list(value, child, False, strings, lists, originals)
        else:
            _collect(value, child, strings, lists, originals)


def _kept_head(keep: int) -> int:
    """Characters a middle cut keeps before its omission marker."""
    return keep - keep // 3


def _middle_cut(value: str, keep: int, path: str) -> str:
    omitted = len(value) - keep
    head = _kept_head(keep)
    tail = keep - head
    marker = (
        f"\n…[{omitted:,} of {len(value):,} characters of {path} omitted here; "
        "see _truncated]…\n"
    )
    return value[:head] + marker + (value[len(value) - tail:] if tail else "")


def _notice(action: str, declared: int, original: int) -> dict:
    guidance = (
        "The listed parts were shortened in the middle to fit the "
        f"{declared:,}-character tool-result limit; everything else is complete. "
        "Omitted text is NOT empty or absent: do not act as if it were, and "
        "do not write this result back as a whole."
    )
    if action in LARGE_READ_ACTIONS:
        guidance = f"{guidance} {_SECTION_GUIDANCE}"
    hint = _REMAINDER_HINTS.get(action)
    return {
        "complete": False,
        "limit_chars": declared,
        "full_result_chars": original,
        "fields": [],
        "lists": [],
        "guidance": f"{guidance} {hint}" if hint else guidance,
    }


def _index_lists(node: Any, twin: Any, into: dict[int, tuple[list, list]]) -> None:
    """Map every list in ``node`` (kept alive in the entry) to its twin in an
    unshortened copy, so a trimmed list is fingerprinted as a section read of
    it would be, whatever strings inside it were shortened first."""
    if isinstance(node, dict):
        for key, value in node.items():
            _index_lists(value, twin[key], into)
    elif isinstance(node, list):
        into[id(node)] = (node, twin)
        for item, twin_item in zip(node, twin):
            _index_lists(item, twin_item, into)


def _bound_dict(
    data: dict,
    action: str,
    limit: int,
    declared: int,
    original_length: int,
    root: str = "",
) -> RenderedResult | None:
    """``data`` within ``limit``, shortened field by field; paths start at
    ``root`` (the section a section read renders), and the notice names the
    ``declared`` limit."""
    strings: list[_Slot] = []
    _collect(data, root, strings, [])
    candidates = [slot for slot in strings if len(slot.original) > _MIN_SHRINK_CHARS]
    notice = _notice(action, declared, original_length)
    # Where a section read continues each shortened part (large reads only).
    sectioned = action in LARGE_READ_ACTIONS
    prints = {
        slot.path: _fingerprint(slot.original) for slot in candidates if sectioned
    }
    twins: dict[int, tuple[list, list]] = {}
    if sectioned:
        _index_lists(data, copy.deepcopy(data), twins)

    def render(fields: list[dict], list_notes: list[dict]) -> str:
        notice["fields"] = fields
        notice["lists"] = list_notes
        return to_json_text({"_truncated": notice, **data})

    def shorten(ceiling: int) -> list[dict]:
        fields = []
        for slot in candidates:
            if len(slot.original) > ceiling:
                slot.parent[slot.key] = _middle_cut(slot.original, ceiling, slot.path)
                entry = {
                    "path": slot.path,
                    "chars": len(slot.original),
                    "kept_chars": ceiling,
                }
                if sectioned:
                    entry["next_offset"] = _kept_head(ceiling)
                    entry["fingerprint"] = prints[slot.path]
                fields.append(entry)
            else:
                slot.parent[slot.key] = slot.original
        return fields

    # 1) Shorten the longest strings to one common ceiling: the largest
    #    ceiling that fits (rendering is monotonic in the ceiling).
    fields: list[dict] = []
    if candidates:
        low = _MIN_SHRINK_CHARS
        high = max(len(slot.original) for slot in candidates) - 1
        best = None
        while low <= high:
            middle = (low + high) // 2
            if js_length(render(shorten(middle), [])) <= limit:
                best, low = middle, middle + 1
            else:
                high = middle - 1
        fields = shorten(_MIN_SHRINK_CHARS if best is None else best)
        if best is not None:
            return RenderedResult(render(fields, []), True, [f["path"] for f in fields])

    # 2) Still over: drop middle items of the largest lists. The first items
    #    and the last one stay, so a newest-last feed keeps its newest entry.
    list_notes: dict[str, dict] = {}
    originals: dict[int, int] = {}  # id(trimmed list) -> original last index
    text = render(fields, [])
    for _ in range(_MAX_LIST_PASSES):
        lists: list[_Slot] = []
        _collect(data, root, [], lists, originals)
        trimmable = [slot for slot in lists if len(slot.original) > 3]
        if not trimmable:
            break
        target = max(trimmable, key=lambda slot: js_length(to_json_text(slot.original)))
        items = target.original
        keep_head = max(1, (len(items) - 1) // 2)
        note = list_notes.setdefault(target.path, {"path": target.path, "items": len(items)})
        if sectioned and "fingerprint" not in note:
            # First trim of this path: ``items`` is still the original list,
            # though pass 1 may have shortened strings inside it.
            note["fingerprint"] = _fingerprint(twins[id(items)][1])
        # Counted against the ORIGINAL length: a list trimmed again drops its
        # earlier marker along with the other middle items.
        omitted = note["items"] - keep_head - 1
        trimmed = (
            items[:keep_head]
            + [f"…[{omitted:,} items of {target.path} omitted here; see _truncated]…"]
            + items[-1:]
        )
        target.parent[target.key] = trimmed
        originals[id(trimmed)] = note["items"] - 1
        note["kept_items"] = keep_head + 1
        if sectioned:
            note["next_offset"] = keep_head
        text = render(fields, list(list_notes.values()))
        if js_length(text) <= limit:
            shortened = [f["path"] for f in fields] + list(list_notes)
            return RenderedResult(text, True, shortened)
    return None


# ── last-resort cut ─────────────────────────────────────────────────

_TRAILING_ESCAPE = re.compile(r"(\\+)(u[0-9A-Fa-f]{0,3})?$")


def _drop_partial_escape(head: str) -> str:
    """Never end on half of a JSON escape (a lone ``\\`` or ``\\u12``)."""
    match = _TRAILING_ESCAPE.search(head)
    if match and len(match.group(1)) % 2 == 1:
        return head[: match.end(1) - 1]
    return head


def _cut_text(
    text: str,
    limit: int,
    declared: int,
    original_length: int,
    action: str,
    parts: list[str] | None = None,
) -> str:
    """The start of ``text`` and a marker, within ``limit``; the marker names
    the ``declared`` limit."""
    hint = _REMAINDER_HINTS.get(action, "")
    if parts and action in LARGE_READ_ACTIONS:
        hint = (
            "Read a part in full with this tool's `section`, e.g. "
            + ", ".join(f"`{part}`" for part in parts)
            + (f". {hint}" if hint else ".")
        )
    marker = (
        f"\n\n[TRUNCATED: this result exceeded the {declared:,}-character tool-result "
        f"limit ({original_length:,} characters); the text above is its start only "
        "and is NOT valid JSON. Do not treat missing parts as absent and do not "
        f"write this result back.{(' ' + hint) if hint else ''}]"
    )
    keep = max(0, limit - js_length(marker))
    return _drop_partial_escape(_cut_units(text, keep)) + marker


def _cut_units(text: str, units: int) -> str:
    """The longest prefix of ``text`` within ``units`` UTF-16 units, cut on a
    code-point boundary (a surrogate pair is never split)."""
    if len(text) == js_length(text):
        return text[:units]
    encoded = text.encode("utf-16-le", "surrogatepass")[: units * 2]
    if len(encoded) >= 2 and 0xD800 <= int.from_bytes(encoded[-2:], "little") <= 0xDBFF:
        encoded = encoded[:-2]
    return encoded.decode("utf-16-le", "surrogatepass")
