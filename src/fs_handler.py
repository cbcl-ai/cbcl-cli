"""Supervise public Files operations in one verified office container."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)
HELPER_PATH = "/usr/local/libexec/cubicle/secure_files.py"
MAX_REQUEST_BYTES = 6 * 1024 * 1024
MAX_RESPONSE_BYTES = 12 * 1024 * 1024
EXEC_TIMEOUT = 24
_ACTIVE_CONTAINERS: set[str] = set()
_ACTIONS = frozenset(
    {
        "fs_tree",
        "fs_read",
        "fs_write",
        "fs_mkdir",
        "fs_rename",
        "fs_delete",
        "fs_download",
        "fs_download_zip",
        "fs_stat",
        "fs_download_chunk",
        "fs_upload_chunk",
        "fs_list_skills",
    }
)


async def _bounded_read(stream: asyncio.StreamReader, limit: int) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = await stream.read(min(65536, limit - total + 1))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise ValueError("Files helper output exceeds limits")
        chunks.append(chunk)


async def _run_process(
    arguments: list[str], payload: bytes, *, timeout: float, limit: int
) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:

        async def exchange():
            process.stdin.write(payload)
            await process.stdin.drain()
            process.stdin.close()
            output = await _bounded_read(process.stdout, limit)
            await process.wait()
            return process.returncode, output

        return await asyncio.wait_for(exchange(), timeout=timeout)
    except BaseException:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        raise


class FsHandler:
    """Relay only: no host filesystem traversal or fallback implementation."""

    def __init__(
        self,
        container_id: str,
        *,
        office_id: str,
        container_resolver: Callable[[], Awaitable[str]] | None = None,
    ) -> None:
        self._container_id = container_id
        self._office_id = str(uuid.UUID(office_id))
        self._container_resolver = container_resolver

    def _command(self, mode: str, request_id: str, container_id: str) -> list[str]:
        return [
            "docker",
            "exec",
            "-i",
            "--user",
            "1000:1000",
            "--workdir",
            "/",
            container_id,
            "/usr/local/bin/python3",
            "-I",
            "-S",
            HELPER_PATH,
            mode,
            request_id,
        ]

    async def _verify_container(self, container_id: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", container_id or ""):
            raise RuntimeError(
                "Office container unavailable; start or upgrade the communicator"
            )
        status, output = await _run_process(
            ["docker", "inspect", "--type", "container", container_id],
            b"",
            timeout=5,
            limit=128 * 1024,
        )
        if status != 0:
            raise RuntimeError(
                "Office container unavailable; start or upgrade the communicator"
            )
        try:
            container = json.loads(output)[0]
            valid = (
                container["Id"] == container_id
                and container["State"]["Running"] is True
                and container["Config"]["Labels"]["cbcl.office_id"] == self._office_id
                and any(
                    mount.get("Destination") == "/workspace"
                    for mount in container["Mounts"]
                )
            )
            if not valid:
                raise ValueError("Container ownership or state mismatch")
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise RuntimeError(
                "Office container identity is not verified; restart or upgrade the communicator"
            ) from error

    async def _cancel(self, request_id: str, container_id: str) -> None:
        status, _output = await _run_process(
            self._command("--cancel", request_id, container_id),
            b"",
            timeout=5,
            limit=1024,
        )
        if status != 0:
            raise RuntimeError("Container Files cancellation could not be confirmed")

    async def _dispatch(self, action: str, params: dict) -> dict:
        if action not in _ACTIONS or not isinstance(params, dict):
            return {"error": "Unknown or invalid filesystem request", "status": 400}
        payload = json.dumps({"action": action, "params": params}).encode()
        if len(payload) > MAX_REQUEST_BYTES:
            return {"error": "Files request exceeds limits", "status": 413}
        container_id = (
            await self._container_resolver()
            if self._container_resolver is not None
            else self._container_id
        )
        if container_id in _ACTIVE_CONTAINERS:
            return {
                "error": "Another Files operation is active; retry shortly",
                "status": 429,
            }
        _ACTIVE_CONTAINERS.add(container_id)
        request_id = uuid.uuid4().hex
        started = False
        try:
            await self._verify_container(container_id)
            started = True
            status, output = await _run_process(
                self._command("--request-id", request_id, container_id),
                payload,
                timeout=EXEC_TIMEOUT,
                limit=MAX_RESPONSE_BYTES,
            )
            if status != 0:
                raise RuntimeError(
                    "Secure Files helper unavailable; restart or upgrade the office image"
                )
            result = json.loads(output)
            if not isinstance(result, dict):
                raise RuntimeError("Invalid Files helper response")
            return result
        except BaseException:
            if started:
                cleanup = asyncio.create_task(self._cancel(request_id, container_id))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise
                except Exception:
                    logger.warning(
                        "Office Files helper cancellation could not be confirmed; its deadline remains active"
                    )
            raise
        finally:
            _ACTIVE_CONTAINERS.discard(container_id)

    async def handle_request(self, message: dict, send_fn: Any) -> None:
        try:
            result = await self._dispatch(
                message.get("action", ""), message.get("params", {})
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            result = {
                "error": "Files operation timed out; reduce the requested data",
                "status": 408,
            }
        except Exception:
            result = {
                "error": "Secure Files unavailable; start or upgrade this office's communicator and image",
                "status": 503,
            }
        await send_fn(
            {
                "type": "response",
                "request_id": message.get("request_id", ""),
                "data": result,
            }
        )
