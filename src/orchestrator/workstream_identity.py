"""A task's workstream identity: directory name and freshness (X46/X48).

The workstream directory (``/workspace/workstreams/<dir>/``: CLAUDE.md,
spec.md, plan.md, intake records, task-owned outputs) is the ``workspace_dir``
the backend declares — derived from the workstream NAME, with a
``ws-<short_code>`` fallback for names without ASCII letters or digits — or,
from an older backend that declares none, the legacy ``slugify(name)``
directory that backend still writes to. A task payload queued before a rename
still carries the old name and directory, so every consumer refreshes them
from the freshest source before it computes a path.
"""

from __future__ import annotations

from typing import Any

from src.paths import declared_workstream_dir, slugify


def workstream_directory_for_task(task_data: dict[str, Any]) -> str:
    """The task's workstream directory name under ``/workspace/workstreams/``.

    The directory the daemon writes the workstream CLAUDE.md to and the
    backend writes spec.md, plan.md and intake files to
    (``paths.declared_workstream_dir``).
    """
    context = task_data.get("workstream_context") or {}
    if not isinstance(context, dict):
        context = {}
    name = context.get("name") or task_data.get("workstream_name") or ""
    declared = context.get("workspace_dir") or task_data.get("workstream_workspace_dir")
    if not name and not declared:
        return slugify(str(task_data.get("workstream_id") or "unscoped"))
    return declared_workstream_dir(declared, name)


def refresh_workstream_identity(
    task: dict[str, Any],
    detail: dict[str, Any] | None,
    workstream: dict[str, Any] | None = None,
    *,
    detail_carries_directory: bool = True,
) -> None:
    """Keep a task's workstream name/short code/directory current (X46).

    ``detail`` is an authoritative task detail (``workstream_name`` /
    ``workstream_short_code`` / ``workstream_workspace_dir``); ``workstream``
    is the synced workstream row (``workspace_dir``). Detail wins, the synced
    row is next, the queued payload is the fallback. Description and goals in
    ``workstream_context`` are kept. A task without a ``workstream_context``
    gets none created (the prompt then relies on the top-level fields, which
    are updated too).

    A fresh name without a fresh directory means an older backend, so the
    queued directory is dropped and the legacy layout follows the fresh name
    instead of a stale declaration. Only a source a current backend declares
    the directory in can say that: a synced row, or a ``detail`` from the
    session-start ``get_task_detail``. The REST task detail never carries the
    directory (``detail_carries_directory=False``), so on its own it keeps the
    one the task was dispatched with.
    """
    fresh_name = fresh_code = fresh_dir = ""
    if isinstance(detail, dict):
        fresh_name = str(detail.get("workstream_name") or "")
        fresh_code = str(detail.get("workstream_short_code") or "")
        fresh_dir = str(detail.get("workstream_workspace_dir") or "")
    if isinstance(workstream, dict):
        fresh_name = fresh_name or str(workstream.get("name") or "")
        fresh_code = fresh_code or str(workstream.get("short_code") or "")
        fresh_dir = fresh_dir or str(workstream.get("workspace_dir") or "")
    if not fresh_name and not fresh_code and not fresh_dir:
        return
    could_declare = isinstance(workstream, dict) or (
        isinstance(detail, dict) and detail_carries_directory
    )
    drop_directory = bool(fresh_name) and not fresh_dir and could_declare
    if fresh_name:
        task["workstream_name"] = fresh_name
    if fresh_code:
        task["workstream_short_code"] = fresh_code
    if fresh_dir:
        task["workstream_workspace_dir"] = fresh_dir
    elif drop_directory:
        task.pop("workstream_workspace_dir", None)
    context = task.get("workstream_context")
    if not isinstance(context, dict):
        return
    context = dict(context)
    if fresh_name:
        context["name"] = fresh_name
    if fresh_code:
        context["short_code"] = fresh_code
    if fresh_dir:
        context["workspace_dir"] = fresh_dir
    elif drop_directory:
        context.pop("workspace_dir", None)
    task["workstream_context"] = context
