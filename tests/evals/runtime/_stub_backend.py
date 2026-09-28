"""Deterministic tool backend for the runtime lane.

The production worker reaches the platform through two HTTP paths, and this
stub serves both with the same office state:

* ``POST /tool-call`` — the in-container MCP server's proxy path
  (``TOOL_PROXY_URL``), authenticated with ``Authorization: Bearer <token>``;
* ``POST /api/offices/{office_id}/tool-call`` — the host-side
  ``get_task_detail`` admission fetch in ``run_sdk_session``, authenticated
  with ``X-Office-Secret``.

Reads answer from the case task; writes are acknowledged with success-shaped
bodies and change nothing except the task status (``task_status_update`` for
an executor, ``move_task`` for a designated reviewer's verdict). A reviewer
verdict is validated like the backend's ``app/tasks/review_verdict.py``
(parity-tested); a refused move answers ``code: invalid_review_verdict`` and
leaves the task in Review. Every authenticated call is logged as ``{seq,
route, action, params, caller, accepted}``: ``accepted`` is false when the
stub answered with an error, so scorers can tell an attempted decision from
one that took effect. Headers — and so the bearer token and office secret —
are never logged. Unknown actions receive an honest ``not available`` error,
which is also logged.
"""

from __future__ import annotations

import copy
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

# Worker write actions the stub acknowledges. Anything else that is not a read
# listed below gets an explicit "not available in this office" error.
WRITE_ACTIONS = frozenset({
    "task_status_update",
    "move_task",
    "add_activity",
    "propose_action",
    "request_user_action",
    "office_save_file",
    "office_attach_to_task",
    "record_verification_evidence",
})
_CALLER_KEYS = ("role", "agent_name", "task_mode", "task_id")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text_problem(value: object, field: str, maximum: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return f"{field} must contain meaningful text."
    if len(value) > maximum:
        return f"{field} exceeds {maximum} characters; summarize the evidence."
    return None


def _verdict_problem(details: object, new_status: str, acceptance_criteria: object) -> str | None:
    """The backend's structural rules, first problem wins (same order)."""
    if not isinstance(details, dict) or not any(
        key in details for key in ("overall", "rationale", "criteria", "required_fixes")
    ):
        return None  # legacy comment-only transition
    overall = details.get("overall")
    if overall not in ("pass", "fail", "conditional"):
        return "overall must be pass, fail, or conditional."
    problem = _text_problem(details.get("rationale"), "rationale", 5000)
    if problem:
        return problem
    if not isinstance(acceptance_criteria, list):
        return "the task needs a valid acceptance criteria list before review."
    expected_indices = {
        index for index, criterion in enumerate(acceptance_criteria, 1)
        if isinstance(criterion, str) and criterion.strip()
    }
    expected_count = len(expected_indices)
    criteria = details.get("criteria")
    if not expected_count:
        return "the task needs acceptance criteria before it can be reviewed."
    if not isinstance(criteria, list) or len(criteria) != expected_count:
        return (f"include one evidence row for each of the {expected_count} "
                "acceptance criteria, including any that could not be verified.")
    has_index = any(isinstance(row, dict) and "criterion_index" in row for row in criteria)
    indices: set[int] = set()
    statuses: list[str] = []
    for position, row in enumerate(criteria, 1):
        if not isinstance(row, dict):
            return f"criterion {position} must be an object."
        problem = (_text_problem(row.get("name"), f"criterion {position} name", 2000)
                   or _text_problem(row.get("evidence"), f"criterion {position} evidence", 8000))
        if problem:
            return problem
        status = row.get("status")
        if status not in ("pass", "fail", "partial"):
            return f"criterion {position} status must be pass, fail, or partial."
        statuses.append(status)
        if has_index:
            index = row.get("criterion_index")
            if type(index) is not int or index not in expected_indices:
                return ("criterion_index must be present on every row and identify "
                        "the original one-based position of a nonblank brief criterion.")
            if index in indices:
                return "criterion_index must identify each criterion exactly once."
            indices.add(index)
    if has_index and indices != expected_indices:
        return "criterion_index must cover every nonblank brief criterion exactly once."
    fixes = details.get("required_fixes", [])
    if not isinstance(fixes, list) or len(fixes) > 100:
        return "required_fixes must be a list of at most 100 concrete fixes."
    for position, fix in enumerate(fixes, 1):
        problem = _text_problem(fix, f"required fix {position}", 5000)
        if problem:
            return problem
    if overall in ("pass", "conditional"):
        if any(status != "pass" for status in statuses) or fixes:
            return ("approval requires every criterion to pass and no required fixes. "
                    "Return incomplete work with overall=fail; conditional is only "
                    "for nonblocking observations.")
    elif not fixes:
        return "a failed review must state concrete required_fixes."
    if new_status == "done" and overall == "fail":
        return "failed work cannot move to Done; return it or escalate."
    if new_status in ("ready", "in_progress") and overall != "fail":
        return "a rework return must use overall=fail with required_fixes."
    return None


def review_verdict_problem(
    details: object, new_status: str, acceptance_criteria: object,
) -> str | None:
    """Why the backend would refuse this review decision, or ``None``.

    Mirrors ``validate_review_verdict`` in ``backend/app/tasks/review_verdict.py``
    (the stub also runs from CLI checkouts without the backend);
    ``test_runtime_composition.py`` pins parity with the real function.
    """
    problem = _verdict_problem(details, new_status, acceptance_criteria)
    return f"Review verdict refused: {problem}" if problem else None


class StubToolBackend:
    """aiohttp server holding one office, one task and an append-only call log."""

    def __init__(
        self,
        *,
        office_id: str,
        task_detail: dict,
        office_files: list[dict] | None = None,
        host: str = "127.0.0.1",
    ) -> None:
        self.office_id = office_id
        self.token = secrets.token_urlsafe(24)
        self.office_secret = secrets.token_urlsafe(24)
        self.host = host
        self.port: int | None = None
        self.log: list[dict] = []
        self.rejected_auth = 0
        self._task = copy.deepcopy(task_detail)
        self._files = list(office_files or [])
        self._runner: web.AppRunner | None = None

    # ── lifecycle ────────────────────────────────────────────────────
    async def start(self) -> int:
        app = web.Application()
        app.router.add_post("/tool-call", self._proxy_route)
        app.router.add_post("/api/offices/{office_id}/tool-call", self._direct_route)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        return self.port

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def __aenter__(self) -> "StubToolBackend":
        await self.start()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.stop()

    # ── routes ───────────────────────────────────────────────────────
    async def _proxy_route(self, request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            self.rejected_auth += 1
            return web.json_response({"detail": "unauthorized"}, status=401)
        return await self._dispatch(request, "proxy")

    async def _direct_route(self, request: web.Request) -> web.Response:
        if (
            request.match_info.get("office_id") != self.office_id
            or request.headers.get("X-Office-Secret") != self.office_secret
        ):
            self.rejected_auth += 1
            return web.json_response({"detail": "not found"}, status=404)
        return await self._dispatch(request, "direct")

    async def _dispatch(self, request: web.Request, route: str) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"detail": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"detail": "invalid body"}, status=400)
        action = str(body.get("action") or "")
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        caller = body.get("_caller") if isinstance(body.get("_caller"), dict) else {}
        return web.json_response(self.record(action, params, route=route, caller=caller))

    def record(self, action: str, params: dict, *, route: str = "proxy",
               caller: dict | None = None) -> dict[str, Any]:
        """Answer one call and log it with whether it was accepted."""
        entry = {
            "seq": len(self.log) + 1,
            "route": route,
            "action": action,
            "params": copy.deepcopy(params),
            "caller": {key: (caller or {})[key] for key in _CALLER_KEYS if key in (caller or {})},
        }
        response = self.respond(action, params)
        entry["accepted"] = not (isinstance(response, dict) and response.get("error"))
        self.log.append(entry)
        return response

    # ── pure response logic (unit-testable without a socket) ────────
    def task_detail(self) -> dict:
        return copy.deepcopy(self._task)

    def respond(self, action: str, params: dict) -> dict[str, Any]:
        task_id = self._task.get("id")
        if action == "get_task_detail":
            wanted = str(params.get("task_id") or "")
            if wanted and wanted not in {task_id, self._task.get("readable_id")}:
                return {"error": True, "message": f"Task {wanted} not found."}
            return self.task_detail()
        if action == "task_status_update":
            new_status = params.get("new_status")
            if new_status not in ("review", "blocked"):
                return {
                    "error": True,
                    "message": f"Workers can only move a task to review or blocked, not {new_status}.",
                }
            old_status = self._task.get("status")
            self._task["status"] = new_status
            return {
                "task_id": task_id,
                "readable_id": self._task.get("readable_id"),
                "old_status": old_status,
                "new_status": new_status,
                "actor": self._task.get("assigned_agent"),
                "moved_at": _now(),
            }
        if action == "move_task":
            new_status = params.get("new_status")
            if self._task.get("status") != "review" or new_status not in (
                "done", "ready", "in_progress", "blocked",
            ):
                return {
                    "error": True,
                    "message": f"Cannot move this task from {self._task.get('status')} to {new_status}.",
                }
            verdict = params.get("verdict")
            if not isinstance(verdict, dict):
                verdict = params.get("details") if isinstance(params.get("details"), dict) else None
            brief = self._task.get("brief") if isinstance(self._task.get("brief"), dict) else {}
            refused = review_verdict_problem(
                verdict, str(new_status), brief.get("acceptance_criteria") or [],
            )
            if refused:
                return {"error": True, "message": refused, "code": "invalid_review_verdict"}
            old_status = self._task.get("status")
            self._task["status"] = new_status
            return {
                "task_id": task_id,
                "readable_id": self._task.get("readable_id"),
                "old_status": old_status,
                "new_status": new_status,
                "actor": self._task.get("reviewer"),
                "moved_at": _now(),
            }
        if action == "add_activity":
            return {"id": str(uuid.uuid4()), "event_type": params.get("event_type"), "created_at": _now()}
        if action == "propose_action":
            return {
                "action_request_id": str(uuid.uuid4()),
                "request_type": params.get("request_type"),
                "status": "pending",
            }
        if action == "request_user_action":
            return {"action_request_id": str(uuid.uuid4()), "status": "pending"}
        if action in ("office_save_file", "office_attach_to_task"):
            return {
                "file_id": str(uuid.uuid4()),
                "path": params.get("path") or params.get("file_path"),
                "attached_to_task": task_id,
            }
        if action == "record_verification_evidence":
            return {"recorded": True, "id": str(uuid.uuid4())}
        if action == "office_list_files":
            return {"files": copy.deepcopy(self._files), "total": len(self._files)}
        if action == "office_get_file":
            wanted = str(params.get("file_id") or params.get("path") or "")
            for item in self._files:
                if wanted in (item.get("id"), item.get("path")):
                    return copy.deepcopy(item)
            return {"error": True, "message": "File not found."}
        if action == "kb_search":
            return {"results": [], "total": 0}
        if action == "memory_recall":
            return {"results": [], "more": 0}
        if action == "get_verification_status":
            return {"verification_plan": None, "checks": [], "status": "not_configured"}
        if action == "list_scripts":
            return {"scripts": [], "total": 0}
        if action == "list_office_secrets":
            return {"secrets": [], "total": 0}
        return {"error": True, "message": f"{action or 'This action'} is not available in this office."}
