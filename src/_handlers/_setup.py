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
    ))
