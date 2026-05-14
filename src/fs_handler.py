"""Filesystem request handler for the communicator.

Handles file operation requests from the backend (via WebSocket) and
executes them on the local workspace filesystem. Sends responses back
to the backend.

Supported actions:
  - fs_tree: Get directory tree
  - fs_read: Read file content
  - fs_write: Write file content
  - fs_mkdir: Create directory
  - fs_rename: Rename file/directory
  - fs_delete: Delete file/directory
  - fs_download: Read file as base64 (for binary downloads)
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# File extensions considered text
_TEXT_EXTS = {
    ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml",
    ".yml", ".toml", ".cfg", ".ini", ".sh", ".bash", ".zsh", ".html",
    ".css", ".scss", ".less", ".xml", ".csv", ".log", ".env", ".sql",
    ".rs", ".go", ".java", ".c", ".cpp", ".h", ".rb", ".php",
}

# File extensions the browser can render inline via an <img> tag.
# svg is included because the same preview slot supports it
# (the browser sniffs by content-type).
_IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg",
}

# Directories to hide from tree
_HIDDEN_DIRS = {".git", "__pycache__", "node_modules", ".claude", ".cubicle"}


def _classify_file(path: Path) -> str:
    """Return a coarse file-kind hint the UI uses to pick a renderer.

    Distinct kinds:
    - ``text``   → read + display in the editor / markdown pane
    - ``image``  → ``<img src=rawUrl>`` (browser handles decoding)
    - ``pdf``    → ``<iframe src=rawUrl>`` (native PDF viewer)
    - ``binary`` → show metadata only; download via raw endpoint
    """
    ext = path.suffix.lower()
    if ext in _TEXT_EXTS:
        return "text"
    if ext in _IMAGE_EXTS:
        return "image"
    if ext == ".pdf":
        return "pdf"
    mime, _ = mimetypes.guess_type(str(path))
    if mime:
        if mime.startswith("text/"):
            return "text"
        if mime.startswith("image/"):
            return "image"
        if mime == "application/pdf":
            return "pdf"
    return "binary"


def _build_tree(path: Path, root: Path, max_depth: int = 5, depth: int = 0) -> dict:
    """Recursively build a directory tree."""
    rel = path.relative_to(root)
    stat = path.stat()

    if path.is_dir():
        children = []
        if depth < max_depth:
            try:
                for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                    # Skip hidden system directories
                    if child.name in _HIDDEN_DIRS or child.name.startswith("."):
                        continue
                    children.append(_build_tree(child, root, max_depth, depth + 1))
            except PermissionError:
                pass
        # Calculate folder size as sum of direct children sizes
        total_size = sum(
            c.get("size", 0) for c in children
        )
        return {
            "name": path.name,
            "path": str(rel) if str(rel) != "." else "",
            "type": "folder",  # Frontend expects "folder", not "directory"
            "size": total_size,
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "children": children,
        }
    else:
        return {
            "name": path.name,
            "path": str(rel),
            "type": "file",
            "size": stat.st_size,
            "file_kind": _classify_file(path),
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        }


def _safe_resolve(workspace: Path, rel_path: str) -> Path:
    """Resolve a relative path within workspace, blocking traversal."""
    if not rel_path:
        return workspace
    if ".." in rel_path.split("/") or rel_path.startswith("/"):
        raise ValueError("Invalid path")
    target = (workspace / rel_path).resolve()
    if not str(target).startswith(str(workspace.resolve())):
        raise ValueError("Path traversal not allowed")
    return target


class FsHandler:
    """Handles filesystem requests from the backend."""

    def __init__(self, workspace_path: str) -> None:
        self._workspace = Path(workspace_path)

    async def handle_request(self, message: dict, send_fn: Any) -> None:
        """Handle a filesystem request and send the response.

        Args:
            message: The full request message dict (type, request_id, action, params).
            send_fn: Async function to send response back via WS.
        """
        request_id = message.get("request_id", "")
        action = message.get("action", "")
        params = message.get("params", {})

        try:
            result = self._dispatch(action, params)
            await send_fn({
                "type": "response",
                "request_id": request_id,
                "data": result,
            })
        except ValueError as exc:
            await send_fn({
                "type": "response",
                "request_id": request_id,
                "data": {"error": str(exc), "status": 400},
            })
        except FileNotFoundError as exc:
            await send_fn({
                "type": "response",
                "request_id": request_id,
                "data": {"error": str(exc), "status": 404},
            })
        except Exception as exc:
            logger.exception("Filesystem operation failed: %s", action)
            await send_fn({
                "type": "response",
                "request_id": request_id,
                "data": {"error": f"Internal error: {exc}", "status": 500},
            })

    def _dispatch(self, action: str, params: dict) -> dict:
        """Route to the appropriate filesystem operation."""
        if action == "fs_tree":
            return self._tree(params)
        elif action == "fs_read":
            return self._read(params)
        elif action == "fs_write":
            return self._write(params)
        elif action == "fs_mkdir":
            return self._mkdir(params)
        elif action == "fs_rename":
            return self._rename(params)
        elif action == "fs_delete":
            return self._delete(params)
        elif action == "fs_download":
            return self._download(params)
        elif action == "fs_download_zip":
            return self._download_zip(params)
        elif action == "fs_stat":
            return self._stat(params)
        elif action == "fs_download_chunk":
            return self._download_chunk(params)
        elif action == "fs_upload_chunk":
            return self._upload_chunk(params)
        else:
            raise ValueError(f"Unknown filesystem action: {action}")

    def _tree(self, params: dict) -> dict:
        """Build a directory tree.

        If ``subfolder`` is empty (default) we build from the
        workspace root — the existing Files-page behaviour.
        If ``subfolder`` is set, we start the walk at that
        subdirectory but keep ``root`` as the workspace, so node
        paths stay workspace-relative and remain interchangeable
        with fs_read / fs_write / fs_raw endpoints. The Scripts
        mini-IDE uses this to scope the tree to ``.scripts/{name}/``
        without reshaping every consumer's notion of a path.
        """
        subfolder = params.get("subfolder", "") or ""
        start = _safe_resolve(self._workspace, subfolder)
        if subfolder and not start.exists():
            raise FileNotFoundError(f"Subfolder not found: {subfolder}")
        if subfolder and not start.is_dir():
            raise ValueError(f"Not a directory: {subfolder}")
        tree = _build_tree(start, self._workspace)
        tree["root"] = str(self._workspace)
        return tree

    def _read(self, params: dict) -> dict:
        path = _safe_resolve(self._workspace, params.get("path", ""))
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {params.get('path')}")
        kind = _classify_file(path)
        size = path.stat().st_size
        # Only text files return their full content inline — images /
        # PDFs / other binaries are fetched separately by the frontend
        # via the /fs/raw streaming endpoint (no base64 round-trip
        # through JSON, no WS message-size pressure). This keeps the
        # metadata response cheap and predictable regardless of file
        # size, which matters because tree-selection is a frequent
        # operation on Large workspaces.
        content: str | None = None
        if kind == "text":
            content = path.read_text(errors="replace")
        return {
            "path": params.get("path"),
            "content": content,
            "size": size,
            "file_kind": kind,
        }

    def _write(self, params: dict) -> dict:
        rel_path = params.get("path", "")
        content = params.get("content", "")
        path = _safe_resolve(self._workspace, rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return {"path": rel_path, "size": len(content.encode())}

    def _mkdir(self, params: dict) -> dict:
        rel_path = params.get("path", "")
        path = _safe_resolve(self._workspace, rel_path)
        if path.exists():
            raise ValueError(f"Already exists: {rel_path}")
        path.mkdir(parents=True, exist_ok=True)
        return {"path": rel_path}

    def _rename(self, params: dict) -> dict:
        old_path = params.get("old_path", "")
        new_path = params.get("new_path", "")
        source = _safe_resolve(self._workspace, old_path)
        dest = _safe_resolve(self._workspace, new_path)
        if not source.exists():
            raise FileNotFoundError(f"Source not found: {old_path}")
        if dest.exists():
            raise ValueError(f"Destination already exists: {new_path}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        source.rename(dest)
        return {"old_path": old_path, "new_path": new_path}

    def _delete(self, params: dict) -> dict:
        rel_path = params.get("path", "")
        path = _safe_resolve(self._workspace, rel_path)
        if not path.exists():
            raise FileNotFoundError(f"Not found: {rel_path}")
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        return {"path": rel_path}

    def _download_zip(self, params: dict) -> dict:
        import io
        import zipfile

        rel_path = params.get("path", "")
        path = _safe_resolve(self._workspace, rel_path)
        if not path.is_dir():
            raise FileNotFoundError(f"Folder not found: {rel_path}")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in sorted(path.rglob("*")):
                if not file_path.is_file():
                    continue
                rel = file_path.relative_to(path)
                if any(part in _HIDDEN_DIRS for part in rel.parts):
                    continue
                zf.write(file_path, arcname=str(rel))

        return {
            "content_base64": base64.b64encode(buf.getvalue()).decode(),
            "folder_name": path.name or "workspace",
        }

    def _download(self, params: dict) -> dict:
        rel_path = params.get("path", "")
        path = _safe_resolve(self._workspace, rel_path)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {rel_path}")
        data = path.read_bytes()
        mime, _ = mimetypes.guess_type(str(path))
        return {
            "path": rel_path,
            "content_base64": base64.b64encode(data).decode(),
            "size": len(data),
            "mime_type": mime or "application/octet-stream",
        }

    def _stat(self, params: dict) -> dict:
        """Return size + MIME for a file, without reading its bytes.

        The backend's streaming /fs/raw endpoint calls this first so it
        can set Content-Length + Content-Type headers and decide the
        chunk plan before touching the payload — cheap enough to run
        on every preview click.
        """
        rel_path = params.get("path", "")
        path = _safe_resolve(self._workspace, rel_path)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {rel_path}")
        mime, _ = mimetypes.guess_type(str(path))
        stat = path.stat()
        return {
            "path": rel_path,
            "size": stat.st_size,
            "mime_type": mime or "application/octet-stream",
            "file_kind": _classify_file(path),
            "modified": datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc,
            ).isoformat(),
        }

    def _download_chunk(self, params: dict) -> dict:
        """Read a byte range from a file and return it base64-encoded.

        Used by the streaming /fs/raw endpoint to pull a file in
        bounded-size slices (1 MB by default). The whole file never
        sits in the WS message path at once — critical for >10 MB PDFs
        with embedded images.

        ``offset`` and ``length`` are validated on the handler side:
        negative offsets are rejected, and ``length`` is capped at a
        hard ceiling so a misbehaving caller can't ask for a 1 GB
        chunk that would blow up memory.
        """
        rel_path = params.get("path", "")
        try:
            offset = int(params.get("offset", 0))
            length = int(params.get("length", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"offset/length must be ints: {exc}") from exc
        # 4 MB cap per chunk. Backend will iterate through chunks if the
        # file is larger; the cap keeps any single WS frame well below
        # the websockets-library default max_size (typically 1 MB incoming,
        # but we're sending server→backend direction so it's governed by
        # the server's frame limit — 4 MB base64 ~= 5.3 MB transmitted
        # which is safe on the backend's websockets defaults, which we
        # raise in the platform WS config).
        _MAX_CHUNK_BYTES = 4 * 1024 * 1024
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if length <= 0 or length > _MAX_CHUNK_BYTES:
            raise ValueError(
                f"length must be in 1..{_MAX_CHUNK_BYTES} (got {length})"
            )

        path = _safe_resolve(self._workspace, rel_path)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {rel_path}")

        total_size = path.stat().st_size
        if offset >= total_size:
            # Past EOF — return empty chunk so the backend's stream
            # loop terminates cleanly. Not an error.
            return {
                "path": rel_path,
                "offset": offset,
                "chunk_base64": "",
                "chunk_size": 0,
                "total_size": total_size,
                "eof": True,
            }

        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read(length)

        end = offset + len(data)
        return {
            "path": rel_path,
            "offset": offset,
            "chunk_base64": base64.b64encode(data).decode(),
            "chunk_size": len(data),
            "total_size": total_size,
            "eof": end >= total_size,
        }

    def _upload_chunk(self, params: dict) -> dict:
        """Append a base64-encoded chunk to a file on the workspace.

        Symmetric with _download_chunk — the backend pushes 1 MB slices
        through this action so uploads of ANY size (including 100 MB
        PDFs, multi-GB archives) never sit in a single WS frame.

        Protocol:
          - First chunk with offset=0 creates/truncates the target file.
            Parent directories are created on demand (mkdir -p).
          - Subsequent chunks with offset>0 append at that exact offset.
            The handler validates offset == current file size so a
            misordered/duplicated chunk can't corrupt the file.
          - The final chunk passes ``done=True`` so the handler can
            flush+close cleanly and return the final size.

        Accepts ANY file type. The workspace is user-owned; classifier
        heuristics and extension filtering belong in the UI, not here.
        """
        rel_path = params.get("path", "")
        chunk_base64 = params.get("chunk_base64", "")
        try:
            offset = int(params.get("offset", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"offset must be int: {exc}") from exc
        done = bool(params.get("done", False))

        _MAX_UPLOAD_CHUNK_BYTES = 4 * 1024 * 1024
        if offset < 0:
            raise ValueError("offset must be non-negative")

        try:
            data = base64.b64decode(chunk_base64) if chunk_base64 else b""
        except Exception as exc:
            raise ValueError(f"chunk_base64 is not valid base64: {exc}") from exc
        if len(data) > _MAX_UPLOAD_CHUNK_BYTES:
            raise ValueError(
                f"chunk too large: {len(data)} > {_MAX_UPLOAD_CHUNK_BYTES}"
            )

        path = _safe_resolve(self._workspace, rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        if offset == 0:
            # Fresh upload — truncate any prior file at this path.
            # Opens in wb so we don't need to os.unlink first.
            with path.open("wb") as fh:
                if data:
                    fh.write(data)
        else:
            # Append at the expected offset. Validate the file size
            # matches what the caller claims to append to; otherwise
            # we'd silently re-write middle bytes on a dropped chunk.
            current_size = path.stat().st_size if path.is_file() else 0
            if offset != current_size:
                raise ValueError(
                    f"offset mismatch: expected {current_size}, got {offset} "
                    "(chunk dropped or reordered)"
                )
            with path.open("ab") as fh:
                if data:
                    fh.write(data)

        final_size = path.stat().st_size if path.is_file() else 0
        return {
            "path": rel_path,
            "bytes_written": len(data),
            "total_size": final_size,
            "done": done,
        }
