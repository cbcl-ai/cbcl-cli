"""Container-only public Files boundary, independent of workspace Python imports."""

from __future__ import annotations

import base64
import ctypes
import errno
import fcntl
import io
import json
import mimetypes
import os
import re
import resource
import select
import signal
import stat
import sys
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MAX_ENTRIES = 2000
MAX_DEPTH = 20
MAX_READ_BYTES = 4 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
MAX_CHUNK_BYTES = 4 * 1024 * 1024
MAX_ZIP_INPUT_BYTES = 64 * 1024 * 1024
MAX_ZIP_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 6 * 1024 * 1024
MAX_RESPONSE_BYTES = 12 * 1024 * 1024
DEADLINE_SECONDS = 20
HELPER_PATH = "/usr/local/libexec/cubicle/secure_files.py"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_PROTECTED_NAMES = {
    ".claude-auth",
    "ssh-keys",
    ".ssh",
    ".cubicle",
    ".git",
    ".credentials.json",
    ".claude.json",
    ".mcp.json",
    ".secrets.json",
    ".env",
    ".env.local",
    ".env.production",
    ".npmrc",
    ".pypirc",
    ".netrc",
}
_PROTECTED_FILE_PREFIXES = tuple(
    f"{name}."
    for name in (
        ".credentials.json",
        ".claude.json",
        ".mcp.json",
        ".secrets.json",
        ".npmrc",
        ".pypirc",
        ".netrc",
    )
) + (".cubicle-files-",)
_TEXT_EXTS = {
    ".txt",
    ".md",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".cfg",
    ".ini",
    ".sh",
    ".bash",
    ".zsh",
    ".html",
    ".css",
    ".scss",
    ".less",
    ".xml",
    ".csv",
    ".log",
    ".sql",
    ".rs",
    ".go",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".rb",
    ".php",
}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg"}


def path_parts(relative_path: str) -> tuple[str, ...]:
    if not isinstance(relative_path, str) or len(relative_path) > 2048:
        raise FilesPolicyError("Invalid workspace path")
    if any(ord(character) < 32 or ord(character) == 127 for character in relative_path):
        raise FilesPolicyError("Control characters are not allowed in paths")
    normalized = relative_path.replace("\\", "/")
    if normalized.startswith("/") or ".." in normalized.split("/"):
        raise FilesPolicyError("Invalid workspace path")
    parts = tuple(part for part in normalized.split("/") if part not in {"", "."})
    if any(re.match(r"^[A-Za-z]:", part) for part in parts):
        raise FilesPolicyError("Drive-qualified paths are not allowed")
    if len(parts) > MAX_DEPTH or any(len(part.encode()) > 255 for part in parts):
        raise FilesPolicyError("Workspace path exceeds limits")
    return parts


def protected_path(parts: tuple[str, ...]) -> bool:
    for index, name in enumerate(parts):
        if name in _PROTECTED_NAMES or name.startswith(_PROTECTED_FILE_PREFIXES):
            return True
        if name.startswith(".env.") and name not in {".env.example", ".env.sample"}:
            return True
        if name == ".claude" and (
            index + 1 == len(parts) or parts[index + 1] != "skills"
        ):
            return True
    return False


def _classify_file(path: Path | str) -> str:
    extension = Path(path).suffix.lower()
    if extension in _TEXT_EXTS:
        return "text"
    if extension in _IMAGE_EXTS:
        return "image"
    if extension == ".pdf":
        return "pdf"
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    if mime.startswith("text/"):
        return "text"
    if mime.startswith("image/"):
        return "image"
    return "pdf" if mime == "application/pdf" else "binary"


def _mount_id(descriptor: int) -> str:
    with open(f"/proc/self/fdinfo/{descriptor}", encoding="ascii") as details:
        for line in details:
            if line.startswith("mnt_id:"):
                return line.split(":", 1)[1].strip()
    raise FilesPolicyError("Secure Files requires Linux mount identity support")


class FilesPolicyError(ValueError):
    pass


class _LimitedBuffer(io.BytesIO):
    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > MAX_ZIP_BYTES:
            raise FilesPolicyError(
                "ZIP output exceeds the 8 MiB export limit; select a smaller subfolder"
            )
        return super().write(data)


class UnsupportedFilesystemError(RuntimeError):
    pass


def _rename_noreplace(
    source_parent: int, source: str, destination_parent: int, destination: str
) -> None:
    rename = ctypes.CDLL(None, use_errno=True).renameat2
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    if (
        rename(
            source_parent, source.encode(), destination_parent, destination.encode(), 1
        )
        != 0
    ):
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FilesPolicyError("Destination already exists")
        if error in {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP}:
            raise UnsupportedFilesystemError(
                "Atomic no-overwrite rename is unsupported by this workspace filesystem"
            )
        raise OSError(error, "Rename failed")


class SecureWorkspace:
    """Hold a no-follow root descriptor; public operations never reopen by pathname."""

    def __init__(self, root: str | Path = "/workspace") -> None:
        self.root = Path(root)
        if not self.root.is_absolute():
            raise FilesPolicyError("A trusted absolute workspace root is required")
        descriptor = os.open("/", _DIRECTORY_FLAGS)
        try:
            for part in self.root.parts[1:]:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            self.root_fd = descriptor
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.root_stat = os.fstat(descriptor)
            self.mount_id = _mount_id(descriptor)
        except BaseException:
            os.close(descriptor)
            raise
        self.deadline = time.monotonic() + DEADLINE_SECONDS
        self.entries = 0

    def __enter__(self) -> "SecureWorkspace":
        return self

    def __exit__(self, *unused: object) -> None:
        os.close(self.root_fd)

    def _tick(self, count: int = 0) -> None:
        self.entries += count
        if self.entries > MAX_ENTRIES:
            raise FilesPolicyError(
                "Workspace operation exceeds the 2000-entry limit; select a smaller subfolder"
            )
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Workspace operation timed out")
        current = os.stat(self.root, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (
            self.root_stat.st_dev,
            self.root_stat.st_ino,
        ):
            raise FilesPolicyError("Workspace root changed during the operation")

    def _parts(
        self, relative_path: str, *, root_allowed: bool = False
    ) -> tuple[str, ...]:
        parts = path_parts(relative_path)
        if not parts and not root_allowed:
            raise FilesPolicyError(
                "The workspace root cannot be used for this operation"
            )
        if protected_path(parts):
            raise FilesPolicyError(
                "Protected runtime paths are not accessible through Files"
            )
        return parts

    def _validate_fd(self, descriptor: int, *, directory: bool) -> os.stat_result:
        metadata = os.fstat(descriptor)
        if _mount_id(descriptor) != self.mount_id:
            raise FilesPolicyError("Mounted paths are not accessible through Files")
        if directory:
            if not stat.S_ISDIR(metadata.st_mode):
                raise FilesPolicyError("Not a directory")
        elif not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise FilesPolicyError("Only regular, single-link files are supported")
        return metadata

    @contextmanager
    def _directory(self, parts: tuple[str, ...], *, create: bool = False):
        descriptor = os.dup(self.root_fd)
        try:
            for part in parts:
                self._tick()
                if create:
                    try:
                        os.mkdir(part, mode=0o755, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                try:
                    self._validate_fd(child, directory=True)
                except BaseException:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
            self._tick()
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def _file(
        self, relative_path: str, *, flags: int = os.O_RDONLY, create: bool = False
    ):
        parts = self._parts(relative_path)
        with self._directory(parts[:-1], create=create) as parent:
            descriptor = os.open(parts[-1], flags | _FILE_FLAGS, 0o644, dir_fd=parent)
            try:
                metadata = self._validate_fd(descriptor, directory=False)
                self._tick()
                yield descriptor, metadata
            finally:
                os.close(descriptor)

    def read_bytes(self, relative_path: str, limit: int = MAX_READ_BYTES) -> bytes:
        if not isinstance(limit, int) or limit < 0 or limit > MAX_ZIP_INPUT_BYTES:
            raise FilesPolicyError("Invalid read limit")
        with self._file(relative_path) as (descriptor, metadata):
            if metadata.st_size > limit:
                raise FilesPolicyError("File exceeds the read limit")
            return self._read_fd(descriptor, limit)

    def _read_fd(self, descriptor: int, limit: int) -> bytes:
        initial = self._validate_fd(descriptor, directory=False)
        chunks = []
        total = 0
        while True:
            self._tick()
            data = os.read(descriptor, min(65536, limit - total + 1))
            if not data:
                break
            total += len(data)
            if total > limit:
                raise FilesPolicyError("File exceeds the read limit")
            chunks.append(data)
        final = self._validate_fd(descriptor, directory=False)
        if (initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) != (
            final.st_size,
            final.st_mtime_ns,
            final.st_ctime_ns,
        ):
            raise FilesPolicyError("File changed while being read; retry the operation")
        return b"".join(chunks)

    def _names(self, descriptor: int) -> list[str]:
        names = []
        with os.scandir(descriptor) as entries:
            for entry in entries:
                self._tick(1)
                path_parts(entry.name)
                names.append(entry.name)
        return sorted(names, key=str.casefold)

    def _walk(
        self, descriptor: int, parts: tuple[str, ...], *, reject_protected: bool = False
    ):
        if len(parts) > MAX_DEPTH:
            raise FilesPolicyError("Workspace operation exceeds the depth limit")
        for name in self._names(descriptor):
            child_parts = (*parts, name)
            if protected_path(child_parts):
                if reject_protected:
                    raise FilesPolicyError(
                        "The folder contains protected runtime paths"
                    )
                continue
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                try:
                    self._validate_fd(child, directory=True)
                    yield child_parts, None, os.fstat(child)
                    yield from self._walk(
                        child, child_parts, reject_protected=reject_protected
                    )
                finally:
                    os.close(child)
            else:
                child = os.open(name, os.O_RDONLY | _FILE_FLAGS, dir_fd=descriptor)
                try:
                    checked = self._validate_fd(child, directory=False)
                    yield child_parts, child, checked
                finally:
                    os.close(child)

    def list_files(self, relative_path: str = "") -> list[str]:
        parts = self._parts(relative_path, root_allowed=True)
        with self._directory(parts) as descriptor:
            return [
                "/".join(child)
                for child, file_fd, _metadata in self._walk(descriptor, parts)
                if file_fd is not None
            ]

    def _tree(self, params: dict) -> dict:
        parts = self._parts(params.get("subfolder", "") or "", root_allowed=True)

        def visit(descriptor: int, current: tuple[str, ...], depth: int) -> dict:
            metadata = self._validate_fd(descriptor, directory=True)
            children = []
            if depth < 5:
                for name in self._names(descriptor):
                    child_parts = (*current, name)
                    if (
                        name.startswith(".")
                        or name in {"node_modules", "__pycache__"}
                        or protected_path(child_parts)
                    ):
                        continue
                    child_metadata = os.stat(
                        name, dir_fd=descriptor, follow_symlinks=False
                    )
                    if stat.S_ISDIR(child_metadata.st_mode):
                        child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                        try:
                            children.append(visit(child, child_parts, depth + 1))
                        finally:
                            os.close(child)
                    else:
                        child = os.open(
                            name, os.O_RDONLY | _FILE_FLAGS, dir_fd=descriptor
                        )
                        try:
                            child_metadata = self._validate_fd(child, directory=False)
                            children.append(
                                {
                                    "name": name,
                                    "path": "/".join(child_parts),
                                    "type": "file",
                                    "size": child_metadata.st_size,
                                    "file_kind": _classify_file(name),
                                    "modified": datetime.fromtimestamp(
                                        child_metadata.st_mtime, timezone.utc
                                    ).isoformat(),
                                }
                            )
                        finally:
                            os.close(child)
            children.sort(
                key=lambda child: (child["type"] != "folder", child["name"].casefold())
            )
            return {
                "name": current[-1] if current else "workspace",
                "path": "/".join(current),
                "type": "folder",
                "size": sum(child["size"] for child in children),
                "modified": datetime.fromtimestamp(
                    metadata.st_mtime, timezone.utc
                ).isoformat(),
                "children": children,
            }

        with self._directory(parts) as descriptor:
            return {**visit(descriptor, parts, 0), "root": "/workspace"}

    def _read(self, params: dict) -> dict:
        relative_path = params.get("path", "")
        with self._file(relative_path) as (descriptor, metadata):
            kind = _classify_file(relative_path)
            content = (
                self._read_fd(descriptor, MAX_READ_BYTES).decode(errors="replace")
                if kind == "text"
                else None
            )
            return {
                "path": relative_path,
                "content": content,
                "size": metadata.st_size,
                "file_kind": kind,
            }

    def _stat(self, params: dict) -> dict:
        relative_path = params.get("path", "")
        with self._file(relative_path) as (_descriptor, metadata):
            return {
                "path": relative_path,
                "size": metadata.st_size,
                "mime_type": mimetypes.guess_type(relative_path)[0]
                or "application/octet-stream",
                "file_kind": _classify_file(relative_path),
                "modified": datetime.fromtimestamp(
                    metadata.st_mtime, timezone.utc
                ).isoformat(),
            }

    def _download(self, params: dict) -> dict:
        relative_path = params.get("path", "")
        content = self.read_bytes(relative_path, MAX_DOWNLOAD_BYTES)
        return {
            "path": relative_path,
            "size": len(content),
            "content_base64": base64.b64encode(content).decode(),
            "mime_type": mimetypes.guess_type(relative_path)[0]
            or "application/octet-stream",
        }

    def _download_chunk(self, params: dict) -> dict:
        relative_path = params.get("path", "")
        offset, length = int(params.get("offset", 0)), int(params.get("length", 0))
        if offset < 0 or not 1 <= length <= MAX_CHUNK_BYTES:
            raise FilesPolicyError("Invalid chunk offset or length")
        with self._file(relative_path) as (descriptor, metadata):
            data = os.pread(descriptor, length, offset)
            self._validate_fd(descriptor, directory=False)
            return {
                "path": relative_path,
                "offset": offset,
                "chunk_base64": base64.b64encode(data).decode(),
                "chunk_size": len(data),
                "total_size": metadata.st_size,
                "eof": offset + len(data) >= metadata.st_size,
            }

    def _write(self, params: dict) -> dict:
        relative_path = params.get("path", "")
        content = params.get("content", "")
        if not isinstance(content, str) or len(content.encode()) > MAX_READ_BYTES:
            raise FilesPolicyError("Content exceeds the write limit")
        self._replace(relative_path, content.encode())
        return {"path": relative_path, "size": len(content.encode())}

    def _replace(self, relative_path: str, content: bytes) -> None:
        parts = self._parts(relative_path)
        with self._directory(parts[:-1], create=True) as parent:
            try:
                existing = os.open(parts[-1], os.O_RDONLY | _FILE_FLAGS, dir_fd=parent)
            except FileNotFoundError:
                pass
            else:
                try:
                    self._validate_fd(existing, directory=False)
                finally:
                    os.close(existing)
            temporary = f".cubicle-files-{uuid.uuid4().hex}.tmp"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_FLAGS,
                0o644,
                dir_fd=parent,
            )
            try:
                with os.fdopen(descriptor, "wb") as target:
                    target.write(content)
                    target.flush()
                    os.fsync(target.fileno())
                self._tick()
                os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def _upload_chunk(self, params: dict) -> dict:
        relative_path = params.get("path", "")
        offset = int(params.get("offset", 0))
        if offset < 0:
            raise FilesPolicyError("offset must be non-negative")
        encoded = params.get("chunk_base64", "")
        if (
            not isinstance(encoded, str)
            or len(encoded) > (MAX_CHUNK_BYTES + 2) // 3 * 4
        ):
            raise FilesPolicyError("chunk too large")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as error:
            raise FilesPolicyError("chunk_base64 is not valid base64") from error
        if len(data) > MAX_CHUNK_BYTES:
            raise FilesPolicyError("chunk too large")
        if offset == 0:
            self._replace(relative_path, data)
            total = len(data)
        else:
            with self._file(relative_path, flags=os.O_WRONLY) as (descriptor, metadata):
                if metadata.st_size != offset:
                    raise FilesPolicyError(
                        "offset mismatch: chunk dropped or reordered"
                    )
                written = os.pwrite(descriptor, data, offset)
                if written != len(data):
                    raise OSError("Incomplete file write")
                os.fsync(descriptor)
                total = os.fstat(descriptor).st_size
        return {
            "path": relative_path,
            "bytes_written": len(data),
            "total_size": total,
            "done": bool(params.get("done", False)),
        }

    def _mkdir(self, params: dict) -> dict:
        relative_path = params.get("path", "")
        parts = self._parts(relative_path)
        with self._directory(parts[:-1], create=True) as parent:
            os.mkdir(parts[-1], mode=0o755, dir_fd=parent)
        return {"path": relative_path}

    def _mutation_check(self, parent: int, parts: tuple[str, ...]) -> os.stat_result:
        descriptor = os.open(parts[-1], os.O_RDONLY | _FILE_FLAGS, dir_fd=parent)
        try:
            metadata = os.fstat(descriptor)
            directory = stat.S_ISDIR(metadata.st_mode)
            self._validate_fd(descriptor, directory=directory)
            if directory:
                for _entry in self._walk(descriptor, parts, reject_protected=True):
                    pass
            return metadata
        finally:
            os.close(descriptor)

    def _rename(self, params: dict) -> dict:
        source = self._parts(params.get("old_path", ""))
        destination = self._parts(params.get("new_path", ""))
        if destination[: len(source)] == source:
            raise FilesPolicyError("Cannot move a path into itself")
        with self._directory(source[:-1]) as source_parent, self._directory(
            destination[:-1], create=True
        ) as destination_parent:
            self._mutation_check(source_parent, source)
            self._tick()
            _rename_noreplace(
                source_parent, source[-1], destination_parent, destination[-1]
            )
        return {"old_path": params["old_path"], "new_path": params["new_path"]}

    def _delete(self, params: dict) -> dict:
        parts = self._parts(params.get("path", ""))

        def remove(parent: int, current: tuple[str, ...]) -> None:
            self._tick()
            if protected_path(current):
                raise FilesPolicyError("The folder contains protected runtime paths")
            descriptor = os.open(current[-1], os.O_RDONLY | _FILE_FLAGS, dir_fd=parent)
            try:
                metadata = os.fstat(descriptor)
                directory = stat.S_ISDIR(metadata.st_mode)
                self._validate_fd(descriptor, directory=directory)
                if directory:
                    for name in self._names(descriptor):
                        remove(descriptor, (*current, name))
                latest = os.stat(current[-1], dir_fd=parent, follow_symlinks=False)
                if (latest.st_dev, latest.st_ino) != (metadata.st_dev, metadata.st_ino):
                    raise FilesPolicyError("File changed during deletion")
                if directory:
                    os.rmdir(current[-1], dir_fd=parent)
                else:
                    os.unlink(current[-1], dir_fd=parent)
            finally:
                os.close(descriptor)

        with self._directory(parts[:-1]) as parent:
            self._mutation_check(parent, parts)
            self.entries = 0
            remove(parent, parts)
        return {"path": params["path"]}

    def _download_zip(self, params: dict) -> dict:
        parts = self._parts(params.get("path", ""), root_allowed=True)
        total = 0
        output = _LimitedBuffer()
        with self._directory(parts) as descriptor, zipfile.ZipFile(
            output, "w", zipfile.ZIP_DEFLATED
        ) as archive:
            for child, file_fd, metadata in self._walk(descriptor, parts):
                if file_fd is None:
                    continue
                total += metadata.st_size
                if total > MAX_ZIP_INPUT_BYTES:
                    raise FilesPolicyError(
                        "ZIP inputs exceed the 64 MiB export limit; select a smaller subfolder"
                    )
                content = self._read_fd(file_fd, metadata.st_size)
                archive.writestr("/".join(child[len(parts) :]), content)
                self._tick()
        return {
            "content_base64": base64.b64encode(output.getvalue()).decode(),
            "folder_name": parts[-1] if parts else "workspace",
        }

    def _skills_discovered(self, params: dict) -> dict:
        del params
        try:
            paths = self.list_files(".claude/skills")
        except FileNotFoundError:
            return {"skills": []}
        groups = {}
        for path in paths:
            parts = path_parts(path)
            if len(parts) < 4:
                continue
            name = parts[2]
            groups.setdefault(name, []).append(path)
        with self._directory((".claude", "skills")) as descriptor:
            for name in self._names(descriptor):
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    groups.setdefault(name, [])
        discovered = []
        for name, paths in sorted(groups.items()):
            files = []
            folders = set()
            frontmatter = {}
            for path in paths:
                relative = "/".join(path_parts(path)[3:])
                metadata = self._stat({"path": path})
                is_skill = relative == "SKILL.md"
                files.append(
                    {
                        "name": relative,
                        "size": metadata["size"],
                        "type": "file",
                        "is_skill_md": is_skill,
                    }
                )
                components = relative.split("/")
                for depth in range(1, len(components)):
                    folders.add("/".join(components[:depth]))
                if is_skill:
                    text = self.read_bytes(path).decode(errors="replace")
                    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
                    if match:
                        for line in match[1].splitlines():
                            if ":" in line and not line.startswith((" ", "-")):
                                key, _, value = line.partition(":")
                                frontmatter[key.strip()] = value.strip().strip("\"'")
            files.extend(
                {"name": folder, "size": 0, "type": "folder", "is_skill_md": False}
                for folder in sorted(folders)
            )
            discovered.append(
                {
                    "name": name,
                    "display_name": frontmatter.get("name", name),
                    "description": frontmatter.get("description", ""),
                    "files": files,
                    "has_skill_md": any(entry["is_skill_md"] for entry in files),
                }
            )
        return {"skills": discovered}

    def dispatch(self, action: str, params: dict) -> dict:
        handlers = {
            "fs_tree": self._tree,
            "fs_read": self._read,
            "fs_write": self._write,
            "fs_mkdir": self._mkdir,
            "fs_rename": self._rename,
            "fs_delete": self._delete,
            "fs_download": self._download,
            "fs_download_zip": self._download_zip,
            "fs_stat": self._stat,
            "fs_download_chunk": self._download_chunk,
            "fs_upload_chunk": self._upload_chunk,
            "fs_list_skills": self._skills_discovered,
        }
        if action not in handlers or not isinstance(params, dict):
            raise FilesPolicyError("Unknown or invalid filesystem request")
        return handlers[action](params)


def execute(request: dict, root: str | Path = "/workspace") -> dict:
    try:
        with SecureWorkspace(root) as workspace:
            return workspace.dispatch(
                request.get("action", ""), request.get("params", {})
            )
    except FileNotFoundError:
        return {"error": "File or directory not found", "status": 404}
    except UnsupportedFilesystemError:
        return {
            "error": "Atomic no-overwrite rename is unsupported by this workspace filesystem; use a compatible filesystem",
            "status": 501,
        }
    except FilesPolicyError as error:
        return {"error": str(error), "status": 400}
    except (
        ValueError,
        TypeError,
        FileExistsError,
        NotADirectoryError,
        IsADirectoryError,
    ):
        return {
            "error": "Unsafe or invalid Files request; check the path and operation limits",
            "status": 400,
        }
    except TimeoutError:
        return {
            "error": "Files operation timed out; reduce the requested data",
            "status": 408,
        }
    except BlockingIOError:
        return {
            "error": "Another workspace operation is active; retry shortly",
            "status": 429,
        }
    except OSError:
        return {
            "error": "Files access denied or changed during the operation",
            "status": 400,
        }


def cancel_request(request_id: str) -> None:
    target = [HELPER_PATH.encode(), b"--request-id", request_id.encode()]
    for name in os.listdir("/proc"):
        if not name.isdecimal() or int(name) == os.getpid():
            continue
        descriptor = None
        try:
            descriptor = os.pidfd_open(int(name))
            with open(f"/proc/{name}/cmdline", "rb") as command:
                arguments = command.read(8192).split(b"\0")
            if any(
                arguments[index : index + 3] == target
                for index in range(len(arguments) - 2)
            ):
                signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                if not select.select([descriptor], [], [], 0.5)[0]:
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                    if not select.select([descriptor], [], [], 2)[0]:
                        raise RuntimeError(
                            "Files helper termination could not be confirmed"
                        )
        except (FileNotFoundError, ProcessLookupError):
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)


def _interrupt(*unused: object) -> None:
    raise TimeoutError("Files operation interrupted")


def main() -> int:
    if (
        len(sys.argv) != 3
        or sys.argv[1] not in {"--request-id", "--cancel"}
        or not re.fullmatch(r"[0-9a-f]{32}", sys.argv[2])
    ):
        return 2
    if sys.argv[1] == "--cancel":
        cancel_request(sys.argv[2])
        return 0
    signal.signal(signal.SIGALRM, _interrupt)
    signal.signal(signal.SIGTERM, _interrupt)
    signal.alarm(DEADLINE_SECONDS)
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_CPU, (15, 16))
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise FilesPolicyError("Request exceeds limits")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise FilesPolicyError("Invalid request")
        result = execute(request)
    except (ValueError, TypeError, TimeoutError):
        result = {"error": "Invalid or expired Files request", "status": 400}
    output = json.dumps(result, ensure_ascii=True).encode()
    if len(output) > MAX_RESPONSE_BYTES:
        output = b'{"error":"Files response exceeds limits","status":413}'
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
