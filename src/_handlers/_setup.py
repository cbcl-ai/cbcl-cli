"""Office setup-wizard handler bodies (split from handlers.py).

Both helpers spawn an asyncio task that drives ``setup_generator``
(AI-driven office config generation / description analysis). They
publish ``setup_generation_failed`` or ``analyze_description_failed``
when the container isn't available so the frontend can surface a
clean error instead of timing out the request.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def run_generate_office_config(
    msg: dict,
    *,
    router,
    container_name: str,
) -> None:
    from src.setup_generator import generate_office_config

    if not container_name:
        await router.publish_event({
            "type": "setup_generation_failed",
            "request_id": msg.get("request_id", ""),
            "error": "Docker container not available.",
        })
        return

    asyncio.create_task(generate_office_config(
        router=router,
        request_id=msg.get("request_id", ""),
        office_name=msg.get("office_name", ""),
        office_description=msg.get("office_description", ""),
        requirements=msg.get("requirements", {}),
        skill_catalog=msg.get("skill_catalog") or [],
        container_name=container_name,
    ))


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
    from src.setup_generator import improve_office_config

    if not container_name:
        await router.publish_event({
            "type": "setup_generation_failed",
            "request_id": msg.get("request_id", ""),
            "error": "Docker container not available.",
        })
        return

    asyncio.create_task(improve_office_config(
        router=router,
        request_id=msg.get("request_id", ""),
        office_name=msg.get("office_name", ""),
        current_config=msg.get("current_config", {}),
        directive=msg.get("directive", ""),
        container_name=container_name,
    ))


async def run_analyze_office_description(
    msg: dict,
    *,
    router,
    container_name: str,
) -> None:
    from src.setup_generator import analyze_office_description

    if not container_name:
        await router.publish_event({
            "type": "analyze_description_failed",
            "request_id": msg.get("request_id", ""),
            "error": "Docker container not available.",
        })
        return

    asyncio.create_task(analyze_office_description(
        router=router,
        request_id=msg.get("request_id", ""),
        description=msg.get("description", ""),
        container_name=container_name,
        office_name=msg.get("office_name") or None,
    ))
