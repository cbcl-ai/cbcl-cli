"""The chat-history legend describes exactly the tags the backend emits.

The backend tags each row of the Manager's recovered conversation
(``app.messages.history.DIALOGUE_ROLE_TAGS``, rendered by
``context_builder._fetch_recent_messages``). The legend that explains those
tags to the Manager lives in the communicator (``render_chat_history``).
They drifted once: the stall hotfix added ``[PLATFORM]`` rows, and the legend
kept describing ``[SYSTEM]`` and ``[ASSISTANT]``, which the backend never
emits, so the Manager had no word for a platform notice in its history.
"""

from __future__ import annotations

import re

from src.orchestrator.manager_context import render_chat_history
from tests.backend_boundary import import_backend

history = import_backend("app.messages.history")


def _legend() -> str:
    rendered = render_chat_history("[USER]: hello")
    return " ".join(rendered.split("<user_message>", 1)[0].split())


def test_the_legend_describes_every_tag_the_backend_emits_and_no_other() -> None:
    described = set(re.findall(r"\[([A-Z]+)(?: <name>)?\]", _legend()))
    assert described == set(history.DIALOGUE_ROLE_TAGS.values())


def test_the_legend_says_a_platform_notice_is_not_the_users_words() -> None:
    legend = _legend()
    assert "[PLATFORM] an automatic platform notice" in legend
    assert "not the user's words" in legend
    # Attributed user rows carry the speaker's name inside the tag.
    assert "[USER <name>]" in legend
