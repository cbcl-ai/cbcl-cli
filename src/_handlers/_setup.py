"""Office setup-wizard handler bodies (split from handlers.py).

Both helpers spawn a background task that drives ``setup_generator``
(AI-driven office config generation / improvement). It goes through
``handlers._spawn_background``, which keeps a strong reference: the event
loop holds tasks only weakly, so a bare ``asyncio.create_task`` result can
be garbage-collected mid-generation. They publish
``setup_generation_failed`` when the container isn't available so the
frontend can surface a clean error instead of timing out the request.
"""
from __future__ import annotations

import logging

from ._requests import _fence_user_input

logger = logging.getLogger(__name__)


# GEN-1 (wizard path): the multi-phase wizard generators previously received
# the user's free-text description / directive RAW — no length cap and no
# closing-tag escape — while the sync single-shot generators fence theirs.
# The wizard packs the whole office description into ``additional_context``,
# which the backend caps at 20000 chars (Pydantic ``Field(max_length=20000)``);
# we mirror that ceiling here (NOT the tighter 10k single-shot default) so a
# genuine long spec is never silently truncated, while still bounding the
# prompt budget across the wizard's ~6-15 LLM calls and neutralising the fence
# tokens (belt-and-suspenders against injection).
_WIZARD_INPUT_MAX = 20_000


def _sanitize_requirements(requirements: object) -> dict:
    """Cap + fence every free-text string in the wizard requirements dict.

    ``responsibility_areas`` / ``desired_agents`` / ``workflows`` /
    ``additional_context`` all reach the generation prompt directly. Non-string
    values (and a non-dict payload) pass through untouched.
    """
    if not isinstance(requirements, dict):
        return {}
    return {
        key: (
            _fence_user_input(value, max_len=_WIZARD_INPUT_MAX)
            if isinstance(value, str)
            else value
        )
        for key, value in requirements.items()
    }


async def run_generate_office_config(
    msg: dict,
    *,
    router,
    container_name: str,
    workspace_path: str | None = None,
) -> None:
    from src.handlers import _spawn_background
    from src.setup_generator import generate_office_config

    if not container_name:
        await router.publish_event({
            "type": "setup_generation_failed",
            "request_id": msg.get("request_id", ""),
            "error": "Docker container not available.",
        })
        return

    _spawn_background(generate_office_config(
        router=router,
        request_id=msg.get("request_id", ""),
        office_name=msg.get("office_name", ""),
        office_description=_fence_user_input(
            msg.get("office_description", ""), max_len=_WIZARD_INPUT_MAX,
        ),
        requirements=_sanitize_requirements(msg.get("requirements") or {}),
        skill_catalog=msg.get("skill_catalog") or [],
        container_name=container_name,
        # Compatibility argument, unused: sources are prepared inside the
        # office container (no host read root).
        workspace_path=workspace_path,
    ), name="setup-generate-config")


async def run_improve_office_config(
    msg: dict,
    *,
    router,
    container_name: str,
) -> None:
    """Apply a user directive to a draft office config.

    Path-B "Improve with AI": the user has a generated draft on the
    Review step and types a free-text adjustment ("add a content
    strategist", "make the writers more formal", etc.). We hand the
    current config + directive to :func:`improve_office_config` which
    runs a single Claude call to produce a revised config, then
    publishes ``setup_generation_complete`` to the same request_id
    key the frontend already polls.
    """
    from src.handlers import _spawn_background
    from src.setup_generator import improve_office_config

    if not container_name:
        await router.publish_event({
            "type": "setup_generation_failed",
            "request_id": msg.get("request_id", ""),
            "error": "Docker container not available.",
        })
        return

    _spawn_background(improve_office_config(
        router=router,
        request_id=msg.get("request_id", ""),
        office_name=msg.get("office_name", ""),
        current_config=msg.get("current_config", {}),
        directive=_fence_user_input(
            msg.get("directive", ""), max_len=_WIZARD_INPUT_MAX,
        ),
        # GEN-08: same curated catalog the generate pass receives.
        skill_catalog=msg.get("skill_catalog") or [],
        container_name=container_name,
    ), name="setup-improve-config")
