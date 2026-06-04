"""Tool-call activity enrichment for the worker activity feed.

Turns Claude CLI ``tool_use`` blocks (and their matching ``tool_result``)
into a compact, human-readable one-liner plus a structured ``details``
payload the UI renders CLI-style (``$ command`` + dimmed output).

Two hard guarantees, both enforced HERE (host side) so they hold before
anything leaves the user's machine for the platform DB:

1. **Redaction.** Secret-shaped substrings are scrubbed from every input
   AND output preview. Defense-in-depth, NOT a proof — pair it with (2).
2. **Secret-file output skip.** The OUTPUT of a ``Read`` of a
   ``.secrets.json`` (or any ``/.secrets`` path) is never captured.

The result is one enriched ``tool_run`` activity per tool call:
``content`` is the row label, ``details`` carries ``{tool, summary,
output_preview?, is_error?}``.
"""
from __future__ import annotations

import re

# Caps. Inputs/outputs are previews, not transcripts — the activity feed
# is a glanceable "what is it doing", not a log store.
_MAX_INPUT_PREVIEW = 600
_MAX_OUTPUT_PREVIEW = 600
_MAX_LABEL = 160  # the one-line ``content`` shown as the row label

_REDACTION = "«redacted»"

# Secret-shaped patterns. Each entry is (compiled_pattern, replacement).
# Conservative on purpose: better to miss an exotic token than to redact
# half the legitimate output. The key=value rule keeps the KEY visible
# (readability) and only masks the value.
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), _REDACTION),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), _REDACTION),
    (re.compile(r"gho_[A-Za-z0-9]{20,}"), _REDACTION),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), _REDACTION),
    (re.compile(r"AKIA[0-9A-Z]{16}"), _REDACTION),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), _REDACTION),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"), f"Bearer {_REDACTION}"),
    # JWTs (header.payload.signature)
    (
        re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}"),
        _REDACTION,
    ),
    # key = "value" / token: value — keep the key, mask the value.
    (
        re.compile(
            r"(?i)(\b(?:api[_-]?key|secret|token|password|passwd|pwd|"
            r"access[_-]?key|client[_-]?secret)\b\s*[:=]\s*)"
            r"['\"]?[A-Za-z0-9._\-/+]{6,}['\"]?"
        ),
        r"\1" + _REDACTION,
    ),
]


def redact_secrets(text: str | None) -> str:
    """Scrub secret-shaped substrings from ``text``. Never raises."""
    if not text:
        return text or ""
    out = text
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def _bare_name(name: str) -> str:
    """Strip an MCP namespace prefix (``mcp__server__Tool`` → ``Tool``)."""
    if name and "__" in name:
        return name.split("__")[-1]
    return name or ""


def is_secret_file_read(name: str, tool_input: dict | None) -> bool:
    """True for a ``Read`` whose target is a secrets file — its output is
    never captured."""
    if _bare_name(name) != "Read":
        return False
    file_path = (tool_input or {}).get("file_path") or ""
    return file_path.endswith(".secrets.json") or "/.secrets" in file_path


def summarize_tool_call(name: str, tool_input: dict | None) -> tuple[str, str]:
    """Return ``(label, input_preview)`` for a ``tool_use`` block.

    ``label`` is the short tool name (``Bash``); ``input_preview`` is the
    meaningful argument (command / path / pattern / query / url),
    redacted and capped.
    """
    inp = tool_input if isinstance(tool_input, dict) else {}
    bare = _bare_name(name)
    preview = ""

    if bare == "Bash":
        preview = (inp.get("command") or "").strip()
    elif bare in ("Read", "Write", "Edit", "MultiEdit"):
        preview = (inp.get("file_path") or "").strip()
    elif bare == "NotebookEdit":
        preview = (inp.get("notebook_path") or "").strip()
    elif bare in ("Glob", "Grep"):
        pattern = (inp.get("pattern") or "").strip()
        path = (inp.get("path") or "").strip()
        preview = f"{pattern}  in {path}" if path else pattern
    elif bare == "WebSearch":
        preview = (inp.get("query") or "").strip()
    elif bare == "WebFetch":
        preview = (inp.get("url") or "").strip()
    elif bare in ("Task", "Agent"):
        preview = (inp.get("description") or inp.get("prompt") or "").strip()
    else:
        # Generic: first short string-ish value, so unknown tools still
        # show something useful instead of a bare name.
        for value in inp.values():
            if isinstance(value, str) and value.strip():
                preview = value.strip()
                break

    preview = redact_secrets(preview)[:_MAX_INPUT_PREVIEW]
    return (bare or "tool"), preview


def normalize_tool_result(content: object) -> str:
    """A ``tool_result.content`` may be a plain string OR a list of
    ``{type, text}`` blocks. Flatten to a single text string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if text:
                    parts.append(str(text))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def output_preview(content: object) -> str:
    """Redacted, truncated preview of a tool result. Empty when blank."""
    text = normalize_tool_result(content).strip()
    if not text:
        return ""
    text = redact_secrets(text)
    if len(text) > _MAX_OUTPUT_PREVIEW:
        text = text[:_MAX_OUTPUT_PREVIEW] + " … (truncated)"
    return text


def build_tool_activity(
    name: str,
    tool_input: dict | None,
    *,
    result_content: object | None = None,
    is_error: bool = False,
    tool_use_id: str = "",
    running: bool = False,
) -> dict:
    """Build the ``{content, details}`` half of a ``tool_run`` PROGRESS
    frame for one tool call.

    Two events are emitted per tool so the feed stays LIVE (the command
    shows the instant the agent invokes it, even for a multi-minute Bash)
    AND carries output:

    * **start** (``running=True``, no ``result_content``): the command,
      tagged ``running``.
    * **end** (``result_content`` set): the command + redacted output preview.

    Both carry the same ``tool_use_id`` in ``details`` so the UI collapses
    the pair into one CLI block (preferring the end). When the result never
    arrives, the start row remains as the record of what was invoked.
    """
    label, preview = summarize_tool_call(name, tool_input)
    content = f"{label}: {preview}" if preview else f"Using {label}"
    if len(content) > _MAX_LABEL:
        content = content[:_MAX_LABEL] + "…"

    details: dict = {"tool": label, "summary": preview}
    if tool_use_id:
        details["tool_use_id"] = tool_use_id
    if running:
        details["running"] = True

    if result_content is not None and not is_secret_file_read(name, tool_input):
        out = output_preview(result_content)
        if out:
            details["output_preview"] = out
    if is_error:
        details["is_error"] = True

    return {"content": content, "details": details}
