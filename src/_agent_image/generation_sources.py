"""Prepare bounded, immutable source evidence without model filesystem tools."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import resource
import signal
import stat
import subprocess
import sys
import tempfile
import zipfile


if __package__:
    from .secure_files import SecureWorkspace, path_parts, protected_path
else:
    sys.path.insert(0, "/usr/local/libexec/cubicle")
    from secure_files import SecureWorkspace, path_parts, protected_path


POLICY_VERSION = 1
MAX_PATHS = 30
MAX_DOCUMENTS = 60
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_FILE_CHARACTERS = 24000
MAX_TOTAL_CHARACTERS = 160000
MAX_ARCHIVE_ENTRIES = 5000
MAX_ARCHIVE_FILES = 2500
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
_TEXT_SUFFIXES = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".csv",
        ".tsv",
        ".json",
        ".jsonl",
        ".yaml",
        ".yml",
        ".toml",
        ".xml",
        ".html",
        ".htm",
        ".rst",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".css",
        ".sql",
        ".ini",
    }
)


def allowed_source(path: str) -> bool:
    parts = path_parts(path)
    return (
        bool(parts)
        and not protected_path(parts)
        and not any(
            part.startswith(".") or part in {"private-runtime", "ssh-keys", "secrets"}
            for part in parts
        )
    )


def _pdf_limits() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 11))


def _pdf_text(content: bytes) -> str:
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(
            ["/usr/bin/pdftotext", "-f", "1", "-l", "30", "-enc", "UTF-8", "-", "-"],
            input=content,
            stdout=output,
            stderr=subprocess.DEVNULL,
            timeout=15,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            preexec_fn=_pdf_limits,
        )
        if result.returncode:
            raise ValueError("PDF conversion failed")
        output.seek(0)
        return output.read(1024 * 1024).decode("utf-8", errors="replace")


def prepare_sources(
    workspace: SecureWorkspace, paths: list[str], *, inventory_only: bool = False
) -> dict:
    if not isinstance(paths, list) or len(paths) > MAX_PATHS:
        raise ValueError("Source selection exceeds limits")
    result = {"policy_version": POLICY_VERSION, "documents": [], "warnings": []}
    documents = result["documents"]
    warnings = result["warnings"]
    seen: set[str] = set()
    total_bytes = 0
    total_characters = 0

    def warn(message: str) -> None:
        if len(warnings) < 20 and message not in warnings:
            warnings.append(message[:500])

    def add_document(path: str, content: bytes | None, reason: str = "") -> None:
        nonlocal total_characters
        if path in seen:
            return
        seen.add(path)
        if len(documents) >= MAX_DOCUMENTS:
            warn(
                "The source inventory exceeds 60 files; additional sources were omitted."
            )
            return
        entry = {"path": path, "content": "", "sha256": "", "truncated": False}
        if inventory_only:
            documents.append(entry)
            return
        if reason:
            entry["unreadable"] = reason
            warn(f"{path}: {reason}; studied by filename only.")
        else:
            raw = content if content is not None else b""
            entry["sha256"] = hashlib.sha256(raw).hexdigest()
            suffix = Path(path).suffix.lower()
            try:
                if suffix == ".pdf":
                    text = _pdf_text(raw)
                    warn(f"{path}: PDF evidence is limited to the first 30 pages.")
                elif suffix in _TEXT_SUFFIXES or (not suffix and b"\0" not in raw):
                    text = raw.decode("utf-8-sig")
                    if "\0" in text:
                        raise ValueError("binary data")
                else:
                    raise ValueError("unsupported format")
                limit = max(
                    0, min(MAX_FILE_CHARACTERS, MAX_TOTAL_CHARACTERS - total_characters)
                )
                entry["content"] = text[:limit]
                entry["truncated"] = len(text) > limit
                total_characters += len(entry["content"])
                if entry["truncated"]:
                    warn(
                        f"{path}: prepared evidence was truncated at its source context limit."
                    )
                if not text.strip():
                    warn(
                        f"{path}: no readable text was extracted; no content was studied."
                    )
            except (OSError, ValueError, subprocess.SubprocessError):
                entry["unreadable"] = "unsupported, unreadable or failed conversion"
                warn(
                    f"{path}: unreadable; provide a text/CSV/HTML export if it encodes method."
                )
        documents.append(entry)

    def add_archive(path: str, content: bytes) -> None:
        nonlocal total_bytes
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                entries = archive.infolist()
                files = [entry for entry in entries if not entry.is_dir()]
                if (
                    len(entries) > MAX_ARCHIVE_ENTRIES
                    or len(files) > MAX_ARCHIVE_FILES
                    or sum(entry.file_size for entry in files) > MAX_ARCHIVE_BYTES
                ):
                    raise ValueError("archive exceeds limits")
                for entry in files:
                    if len(documents) >= MAX_DOCUMENTS:
                        warn(
                            f"{path}: additional archive entries were omitted at the source limit."
                        )
                        break
                    mode = entry.external_attr >> 16
                    try:
                        permitted = allowed_source(entry.filename)
                    except ValueError:
                        permitted = False
                    if (
                        not permitted
                        or stat.S_ISLNK(mode)
                        or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                    ):
                        warn(
                            f"{path}: an unsafe or protected archive entry was excluded."
                        )
                        continue
                    item_path = f"{path}!/{entry.filename}"
                    if (
                        entry.file_size > MAX_FILE_BYTES
                        or total_bytes + entry.file_size > MAX_TOTAL_BYTES
                    ):
                        add_document(item_path, None, "source byte limit exceeded")
                        continue
                    with archive.open(entry) as source:
                        item = source.read(MAX_FILE_BYTES + 1)
                    if len(item) > MAX_FILE_BYTES:
                        add_document(item_path, None, "source byte limit exceeded")
                        continue
                    total_bytes += len(item)
                    add_document(item_path, item)
        except (
            OSError,
            ValueError,
            RuntimeError,
            zipfile.BadZipFile,
            NotImplementedError,
        ):
            add_document(path, None, "archive could not be safely read")

    selected: list[str] = []
    for path in paths:
        try:
            if not allowed_source(path):
                raise ValueError("protected source")
            try:
                candidates = workspace.list_files(path)
            except NotADirectoryError:
                candidates = [path]
            for candidate in candidates:
                if candidate not in selected and allowed_source(candidate):
                    selected.append(candidate)
        except FileNotFoundError:
            if path != "source/":
                warn("A selected source was missing; no contents were studied.")
        except (OSError, ValueError, TimeoutError):
            warn(
                "A selected source was missing, protected, unsafe, or outside the read limit."
            )
    for path in selected:
        if len(documents) >= MAX_DOCUMENTS:
            warn(
                "The source inventory exceeds 60 files; additional sources were omitted."
            )
            break
        if inventory_only:
            add_document(path, None)
            continue
        try:
            remaining = min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - total_bytes)
            content = workspace.read_bytes(path, limit=max(0, remaining))
            total_bytes += len(content)
            if Path(path).suffix.lower() == ".zip":
                add_archive(path, content)
            else:
                add_document(path, content)
        except (OSError, ValueError, TimeoutError):
            add_document(
                path, None, "missing, unsafe, changed, or source byte limit exceeded"
            )
    return result


def main() -> int:
    signal.signal(
        signal.SIGALRM, lambda *_unused: (_ for _ in ()).throw(TimeoutError())
    )
    signal.alarm(75)
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (45, 46))
    try:
        raw = sys.stdin.buffer.read(32769)
        if len(raw) > 32768:
            raise ValueError("Source request exceeds limits")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("Invalid source request")
        with SecureWorkspace() as workspace:
            result = prepare_sources(
                workspace,
                request.get("paths", []),
                inventory_only=request.get("inventory_only") is True,
            )
    except (OSError, ValueError, TypeError, TimeoutError):
        result = {
            "policy_version": POLICY_VERSION,
            "documents": [],
            "warnings": ["Source preparation failed; no source contents were studied."],
        }
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
