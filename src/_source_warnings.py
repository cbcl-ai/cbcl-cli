"""The length of a user-facing source warning, and the two ways to fit it.

Every source warning reaches the wizard's Review step and Office settings cut
to ``SOURCE_WARNING_CHARACTERS``. Warnings that name files are built to fit
whole, so the cap never cuts a path or drops the advice at the end.

The in-container source reader (``_agent_image/generation_sources.py``) runs
standalone and keeps its own copy of the limit; a test pins the two equal.
"""

from __future__ import annotations

SOURCE_WARNING_CHARACTERS = 300


def fit_names(
    head: str,
    names: list[str],
    tail: str,
    *,
    total: int | None = None,
    unnamed: str | None = None,
    limit: int = SOURCE_WARNING_CHARACTERS,
) -> str:
    """``head: a, b and N more`` + ``tail``, naming as many whole names as fit.

    ``total`` counts every file when ``names`` is only the first of them.
    When no name fits, ``unnamed`` (for example ``"3 files"``) stands in for
    the list; without it the warning is ``head + tail``.
    """
    count = len(names) if total is None else total
    shown: list[str] = []
    for name in names:
        candidate = [*shown, name]
        hidden = count - len(candidate)
        more = f" and {hidden} more" if hidden else ""
        if len(head + ": " + ", ".join(candidate) + more + tail) > limit:
            break
        shown = candidate
    if not shown:
        if unnamed is None:
            return cut_at_word(head + tail, limit)
        return cut_at_word(f"{head}: {unnamed}{tail}", limit)
    hidden = count - len(shown)
    more = f" and {hidden} more" if hidden else ""
    return head + ": " + ", ".join(shown) + more + tail


def cut_at_word(text: str, limit: int = SOURCE_WARNING_CHARACTERS) -> str:
    """``text`` within ``limit`` characters, cut at a word with an ellipsis.

    A single word longer than the limit is cut hard.
    """
    if len(text) <= limit:
        return text
    head = text[: max(0, limit - 1)]
    cut = head.rfind(" ")
    if cut > 0:
        head = head[:cut].rstrip(" ,;:")
    return head + "…"
