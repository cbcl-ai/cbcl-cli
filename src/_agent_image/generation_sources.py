"""Prepare bounded, immutable source evidence without model filesystem tools."""

from __future__ import annotations

import hashlib
import io
import json
import resource
import signal
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from html.parser import HTMLParser
from pathlib import Path

if __package__:
    from .secure_files import SecureWorkspace, path_parts, protected_path
else:
    sys.path.insert(0, "/usr/local/libexec/cubicle")
    from secure_files import SecureWorkspace, path_parts, protected_path


POLICY_VERSION = 1
# One time limit for the whole run, under the host's 90-second timeout for
# this helper: the alarm stops in-memory work (archive entries, PDF
# conversion) and the workspace deadline stops file reads.
READER_SECONDS = 75
MAX_PATHS = 30
MAX_DOCUMENTS = 60
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
# Extraction limits, not a model context window. The daemon surveys this
# immutable snapshot in bounded sections before combining its findings.
MAX_FILE_CHARACTERS = 256000
MAX_TOTAL_CHARACTERS = 2048000
MAX_ARCHIVE_ENTRIES = 5000
MAX_ARCHIVE_FILES = 2500
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
# Files past the document limit are still NAMED, so the survey knows the shape
# of the material it could not read. The names are bounded; the count is not.
MAX_SKIPPED_PATH_CHARACTERS = 12000
# The daemon cuts each user-facing warning at 300 characters, so the one that
# names skipped files is built to fit whole.
SKIPPED_WARNING_CHARACTERS = 300
# The reason recorded for a format the reader cannot convert: the one
# unreadable case a text export fixes (the daemon keeps a copy to say so).
UNREADABLE_FORMAT = "unsupported, unreadable or failed conversion"
# The reason recorded for a source whose reading failed in a way no other
# reason covers. Only that source is lost; the others are still read.
READ_FAILED = "an unexpected error occurred while reading it"
# Why an archive entry could not be read, in the order a warning lists them.
_ENTRY_PROTECTED = "password-protected"
_ENTRY_UNSUPPORTED = "unsupported compression"
_ENTRY_CORRUPT = "corrupt"
_ENTRY_FAILURES = (_ENTRY_PROTECTED, _ENTRY_UNSUPPORTED, _ENTRY_CORRUPT)
_TEXT_SUFFIXES = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".mdx",
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
        ".vue",
        ".svelte",
        ".css",
        ".scss",
        ".sass",
        ".less",
        ".sql",
        ".graphql",
        ".proto",
        ".ini",
        ".cfg",
        ".conf",
        ".properties",
        ".gradle",
        ".tf",
        ".sh",
        ".bash",
        ".zsh",
        ".go",
        ".java",
        ".kt",
        ".kts",
        ".scala",
        ".rs",
        ".rb",
        ".php",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".cs",
        ".swift",
    }
)
# Build files whose names carry no text suffix (``Dockerfile.agent`` has the
# suffix ``.agent``). A NUL byte still makes any of them unreadable.
_TEXT_FILE_NAMES = frozenset({"dockerfile", "makefile", "gnumakefile"})


def _is_text_name(path: str) -> bool:
    item = Path(path)
    suffix = item.suffix.lower()
    if suffix in _TEXT_SUFFIXES:
        return True
    name = item.name.casefold()
    return name in _TEXT_FILE_NAMES or any(
        name.startswith(f"{base}.") for base in _TEXT_FILE_NAMES
    )


def _fit_names(head: str, names: list[str], tail: str, total: int) -> str:
    """``head: a, b and N more`` + ``tail`` within the warning limit.

    A copy of ``_source_warnings.fit_names``: this helper runs standalone in
    the office container and cannot import daemon modules.
    """
    shown: list[str] = []
    for name in names:
        candidate = [*shown, name]
        hidden = total - len(candidate)
        more = f" and {hidden} more" if hidden else ""
        if len(head + ": " + ", ".join(candidate) + more + tail) > (
            SKIPPED_WARNING_CHARACTERS
        ):
            break
        shown = candidate
    if not shown:
        return head + tail
    hidden = total - len(shown)
    return head + ": " + ", ".join(shown) + (
        f" and {hidden} more" if hidden else ""
    ) + tail


def _skipped_warning(paths: list[str], total: int) -> str:
    """How many selected files were not read, naming the first that fit."""
    noun = "file was" if total == 1 else "files were"
    return _fit_names(
        f"{total} {noun} not read because the {MAX_DOCUMENTS}-document limit "
        "was reached",
        paths,
        ". Guides and documents are read first; remove files the office does "
        "not need to include the rest.",
        total,
    )


def _time_limit_warning(paths: list[str], total: int) -> str:
    """How many selected sources the reader's time limit left unread."""
    noun = "source was" if total == 1 else "sources were"
    return _fit_names(
        f"{total} selected {noun} not read because the source reader's time "
        "limit was reached",
        paths,
        ". Select fewer or smaller sources and generate again.",
        total,
    )


def _failed_entries(failed: dict[str, list[str]]) -> tuple[int, str]:
    """``(count, why)`` of the archive entries that could not be read."""
    counts = [
        (len(failed[reason]), reason)
        for reason in _ENTRY_FAILURES
        if failed.get(reason)
    ]
    total = sum(count for count, _reason in counts)
    if len(counts) == 1:
        return total, counts[0][1]
    return total, ", ".join(f"{count} {reason}" for count, reason in counts)


def _failed_entries_warning(path: str, failed: dict[str, list[str]]) -> str:
    """One warning for an archive's unreadable entries, naming the first."""
    total, why = _failed_entries(failed)
    noun, them = ("entry was", "it") if total == 1 else ("entries were", "them")
    return _fit_names(
        f"{path}: {total} {noun} not read ({why})",
        [name for reason in _ENTRY_FAILURES for name in failed.get(reason, [])],
        ". Re-create the archive (no password, standard compression) to "
        f"include {them}.",
        total,
    )


def _source_order(path: str) -> tuple[int, int, str]:
    """Spend bounded study capacity on documents before website build assets."""
    item = Path(path)
    if item.stem.lower() in {
        "start-here",
        "readme",
    } and item.suffix.lower() in {".md", ".txt"}:
        priority = 0
    elif item.suffix.lower() in {".md", ".markdown", ".txt", ".rst"}:
        priority = 1
    elif item.suffix.lower() in {
        ".csv",
        ".tsv",
        ".json",
        ".jsonl",
        ".yaml",
        ".yml",
        ".pdf",
    }:
        priority = 2
    elif item.suffix.lower() in {".html", ".htm"}:
        priority = 3
    elif _is_text_name(path) or not item.suffix or item.suffix.lower() == ".zip":
        # A file without an extension is read as text (add_document), so it
        # sorts with code and config, not with the files studied by name.
        priority = 4
    else:
        # Images and Office files are studied by name only, so they do not
        # take a document slot before readable code, config or an archive.
        priority = 5
    return priority, len(item.parts), path.casefold()


class _HTMLText(HTMLParser):
    """Extract document text without executing or retaining bundled scripts/CSS."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "svg"}:
            self.ignored.append(tag)
        if self.ignored:
            return
        if tag in {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "section"}:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" | ")
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.parts.append(f" [link: {href}] ")
        elif tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.parts.append(f" [image: {alt}] ")

    def handle_endtag(self, tag: str) -> None:
        if self.ignored and tag == self.ignored[-1]:
            self.ignored.pop()
        elif not self.ignored and tag in {
            "p",
            "div",
            "tr",
            "li",
            "h1",
            "h2",
            "h3",
            "h4",
            "section",
        }:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.parts.append(data)


def _html_text(content: str) -> str:
    parser = _HTMLText()
    parser.feed(content)
    parser.close()
    return "\n".join(
        line.strip() for line in "".join(parser.parts).splitlines() if line.strip()
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
            ["/usr/bin/pdftotext", "-enc", "UTF-8", "-", "-"],
            input=content,
            stdout=output,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            preexec_fn=_pdf_limits,
        )
        if result.returncode:
            raise ValueError("PDF conversion failed")
        output.seek(0)
        return output.read(1024 * 1024).decode("utf-8", errors="replace")


class _ArchiveRejected(ValueError):
    """The archive as a whole cannot be read; the message says why."""


def prepare_sources(
    workspace: SecureWorkspace, paths: list[str], *, inventory_only: bool = False
) -> dict:
    if not isinstance(paths, list) or len(paths) > MAX_PATHS:
        raise ValueError("Source selection exceeds limits")
    # ``skipped`` names the selected files that were not read because the
    # document limit was reached (bounded); ``skipped_total`` counts them all.
    result = {
        "policy_version": POLICY_VERSION,
        "documents": [],
        "warnings": [],
        "skipped": [],
        "skipped_total": 0,
    }
    documents = result["documents"]
    warnings = result["warnings"]
    skipped = result["skipped"]
    # Selected files the reader's time limit left unread, for one warning.
    timed_out: list[str] = []
    seen: set[str] = set()
    content_sources: dict[str, dict] = {}
    total_bytes = 0
    total_characters = 0
    skipped_characters = 0

    def warn(message: str) -> None:
        if len(warnings) < 20 and message not in warnings:
            warnings.append(message[:500])

    def note_skipped(path: str) -> None:
        nonlocal skipped_characters
        if path in seen:
            return
        seen.add(path)
        result["skipped_total"] += 1
        if skipped_characters + len(path) <= MAX_SKIPPED_PATH_CHARACTERS:
            skipped.append(path)
            skipped_characters += len(path)

    def add_document(path: str, content: bytes | None, reason: str = "") -> None:
        nonlocal total_characters
        if path in seen:
            return
        if len(documents) >= MAX_DOCUMENTS:
            note_skipped(path)
            return
        seen.add(path)
        entry = {"path": path, "content": "", "sha256": "", "truncated": False}
        if inventory_only:
            documents.append(entry)
            return
        if reason:
            entry["unreadable"] = reason
            warn(f"{path}: {reason}; studied by filename only.")
        else:
            raw = content if content is not None else b""
            try:
                entry["sha256"] = hashlib.sha256(raw).hexdigest()
                suffix = Path(path).suffix.lower()
                if suffix == ".pdf":
                    text = _pdf_text(raw)
                elif _is_text_name(path) or (not suffix and b"\0" not in raw):
                    text = raw.decode("utf-8-sig")
                    if "\0" in text:
                        raise ValueError("binary data")
                    if suffix in {".html", ".htm"}:
                        text = _html_text(text)
                        entry["extraction"] = (
                            "HTML document text; scripts, styles and SVG markup excluded"
                        )
                else:
                    raise ValueError("unsupported format")
                # Identical copies in a source pack do not spend the text budget
                # again. Retain their paths so provenance remains available.
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if text.strip() and digest in content_sources:
                    content_sources[digest].setdefault("also_at", []).append(path)
                    return
                limit = max(
                    0, min(MAX_FILE_CHARACTERS, MAX_TOTAL_CHARACTERS - total_characters)
                )
                entry["content"] = text[:limit]
                entry["truncated"] = len(text) > limit
                entry["source_characters"] = len(text)
                total_characters += len(entry["content"])
                if not entry["truncated"] and text.strip():
                    content_sources[digest] = entry
                if entry["truncated"]:
                    warn(
                        f"{path}: only {limit:,} of {len(text):,} text characters could be included. "
                        "Split this document or select fewer sources, then generate again."
                    )
                if not text.strip():
                    warn(
                        f"{path}: no readable text was extracted; no content was studied."
                    )
            except TimeoutError:
                # The reader's time limit, not this file: the caller stops.
                raise
            except Exception:  # noqa: BLE001 - one file never drops the others
                # A failed conversion (a PDF, a decode, binary data), and also
                # a parser that gives up on its input: HTMLParser raises
                # AssertionError on some marked sections (``<![foo[x]]>``).
                entry["unreadable"] = UNREADABLE_FORMAT
                warn(
                    f"{path}: unreadable; provide a text/CSV/HTML export if it encodes method."
                )
        documents.append(entry)

    def add_archive(path: str, content: bytes) -> None:
        nonlocal total_bytes
        # The byte budget counts what is read: an archive costs the larger of
        # its compressed size and the member bytes read from it, never both.
        budget_before = total_bytes
        try:
            try:
                archive = zipfile.ZipFile(io.BytesIO(content))
            except TimeoutError:
                raise
            except Exception as exc:  # noqa: BLE001 - any unreadable container
                raise _ArchiveRejected("not a readable ZIP archive") from exc
            with archive:
                entries = archive.infolist()
                files = sorted(
                    (entry for entry in entries if not entry.is_dir()),
                    key=lambda entry: _source_order(entry.filename),
                )
                if len(entries) > MAX_ARCHIVE_ENTRIES:
                    raise _ArchiveRejected(
                        f"archive has more than {MAX_ARCHIVE_ENTRIES:,} entries"
                    )
                if len(files) > MAX_ARCHIVE_FILES:
                    raise _ArchiveRejected(
                        f"archive has more than {MAX_ARCHIVE_FILES:,} files"
                    )
                if sum(entry.file_size for entry in files) > MAX_ARCHIVE_BYTES:
                    raise _ArchiveRejected(
                        "archive holds more than "
                        f"{MAX_ARCHIVE_BYTES // (1024 * 1024)} MB of files"
                    )
                # Entries that could not be read, by reason. They take no
                # document slot: a protected archive must not use up the
                # limit and push readable files out.
                failed: dict[str, list[str]] = {}
                # Whether any entry reached the result, read or named.
                visible = False
                for entry in files:
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
                    if entry.flag_bits & 0x1:
                        # Encrypted: no password is ever supplied.
                        failed.setdefault(_ENTRY_PROTECTED, []).append(entry.filename)
                        continue
                    item_path = f"{path}!/{entry.filename}"
                    if len(documents) >= MAX_DOCUMENTS:
                        # Named, never opened: the rest of the pack stays
                        # visible to the survey as structure.
                        note_skipped(item_path)
                        visible = True
                        continue
                    if (
                        entry.file_size > MAX_FILE_BYTES
                        or total_bytes + entry.file_size > MAX_TOTAL_BYTES
                    ):
                        add_document(item_path, None, "source byte limit exceeded")
                        visible = True
                        continue
                    try:
                        with archive.open(entry) as source:
                            item = source.read(MAX_FILE_BYTES + 1)
                    except TimeoutError:
                        raise
                    except NotImplementedError:
                        failed.setdefault(_ENTRY_UNSUPPORTED, []).append(entry.filename)
                        continue
                    except Exception:  # noqa: BLE001
                        # A bad CRC or header (BadZipFile) or a damaged
                        # stream (zlib.error, EOFError, lzma and bz2 errors):
                        # only this entry is unreadable.
                        failed.setdefault(_ENTRY_CORRUPT, []).append(entry.filename)
                        continue
                    visible = True
                    if len(item) > MAX_FILE_BYTES:
                        add_document(item_path, None, "source byte limit exceeded")
                        continue
                    total_bytes += len(item)
                    add_document(item_path, item)
                if failed and visible:
                    warn(_failed_entries_warning(path, failed))
                elif failed:
                    # Nothing else of this archive is in the result: report
                    # the archive itself, in one document slot.
                    total, why = _failed_entries(failed)
                    noun = "entry" if total == 1 else "entries"
                    raise _ArchiveRejected(f"{total} {noun} could not be read ({why})")
        except _ArchiveRejected as rejected:
            add_document(path, None, f"{rejected}, so none of its files were read")
        finally:
            total_bytes = max(total_bytes, budget_before + len(content))

    def front_warning(message: str) -> None:
        # First, so the daemon's ten-warning cap never drops a coverage gap.
        if len(warnings) >= 20:
            warnings.pop()
        warnings.insert(0, message[:500])

    selected: list[str] = []
    for index, path in enumerate(paths):
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
        except TimeoutError:
            # TimeoutError is an OSError: without this, every later path
            # would be reported as missing or unsafe. Nothing can be read
            # after the time limit, so the files already listed are named
            # with the selections not listed yet.
            timed_out = [
                *sorted(selected, key=_source_order),
                *(str(item) for item in paths[index:]),
            ]
            break
        except (OSError, ValueError):
            warn(
                "A selected source was missing, protected, unsafe, or outside the read limit."
            )
    ordered = [] if timed_out else sorted(selected, key=_source_order)
    for index, path in enumerate(ordered):
        if len(documents) >= MAX_DOCUMENTS:
            # An archive past the limit is named but not opened.
            note_skipped(path)
            continue
        if inventory_only:
            add_document(path, None)
            continue
        try:
            remaining = min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - total_bytes)
            content = workspace.read_bytes(path, limit=max(0, remaining))
            if Path(path).suffix.lower() == ".zip":
                # Charges the budget itself: its members count, not twice.
                add_archive(path, content)
            else:
                total_bytes += len(content)
                add_document(path, content)
        except TimeoutError:
            # The reader's time limit ends the run. Stop instead of reporting
            # every remaining file as unreadable for the wrong reason, and
            # keep what was read.
            timed_out.append(
                f"{path} (partly)" if _has_document(documents, path) else path
            )
            timed_out.extend(ordered[index + 1 :])
            break
        except (OSError, ValueError):
            add_document(
                path, None, "missing, unsafe, changed, or source byte limit exceeded"
            )
        except Exception:  # noqa: BLE001 - one source never drops the others
            # add_document isolates text extraction; anything else that fails
            # for this source (an archive member, a read) is reported here,
            # and the sources after it are still read.
            add_document(path, None, READ_FAILED)
    if timed_out:
        front_warning(_time_limit_warning(timed_out, len(timed_out)))
    if result["skipped_total"]:
        front_warning(_skipped_warning(skipped, result["skipped_total"]))
    return result


def _has_document(documents: list[dict], path: str) -> bool:
    """Whether ``path``, or at least one member of the archive ``path``, was read."""
    prefix = f"{path}!/"
    return any(
        document["path"] == path or document["path"].startswith(prefix)
        for document in documents
    )


def main() -> int:
    signal.signal(
        signal.SIGALRM, lambda *_unused: (_ for _ in ()).throw(TimeoutError())
    )
    signal.alarm(READER_SECONDS)
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (45, 46))
    result = None
    try:
        try:
            raw = sys.stdin.buffer.read(32769)
            if len(raw) > 32768:
                raise ValueError("Source request exceeds limits")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise TypeError("Invalid source request")
            with SecureWorkspace() as workspace:
                # File reads share the run's time limit, not the shorter
                # deadline of a single Files operation.
                workspace.deadline = time.monotonic() + READER_SECONDS
                result = prepare_sources(
                    workspace,
                    request.get("paths", []),
                    inventory_only=request.get("inventory_only") is True,
                )
        finally:
            # The alarm must not interrupt printing the result.
            signal.alarm(0)
    except Exception:  # noqa: BLE001 - never a traceback in place of a result
        # A result prepared before a late alarm stands.
        if result is None:
            result = {
                "policy_version": POLICY_VERSION,
                "documents": [],
                "warnings": [
                    "Source preparation failed; no source contents were studied."
                ],
            }
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
