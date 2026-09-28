"""The backend and the agent image agree on what a human conversation row is.

``get_task_detail`` pins the newest human conversation rows (a comment,
answer or question by the user, the Manager or the Manager Assistant) into
its window: ``app/ws/request_handler.HUMAN_CONVERSATION_EVENT_TYPES`` /
``HUMAN_CONVERSATION_ACTORS``. The in-container projection
(``_agent_image/_mcp/transforms.py``) keeps those rows ahead of the newest-10
cut of other rows, and its ``activities_note`` promises every such row in the
window is kept. The two sets are separate copies on either side of the
platform boundary: an actor added to one side only would be pinned by the
backend and then dropped by the projection. In a standalone CLI checkout the
backend comparison skips.
"""

from __future__ import annotations

from src._agent_image._mcp import transforms

from tests import backend_boundary


def test_both_sides_define_the_same_human_conversation_rows():
    handler = backend_boundary.import_backend("app.ws.request_handler")
    assert transforms._HUMAN_CONVERSATION_EVENTS == frozenset(
        handler.HUMAN_CONVERSATION_EVENT_TYPES
    )
    assert transforms._HUMAN_CONVERSATION_ACTORS == frozenset(
        handler.HUMAN_CONVERSATION_ACTORS
    )
