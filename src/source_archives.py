"""Host-side ZIP pre-extraction for the source-grounded survey.

Incident (2026-09-02, office-instructions generation run): 4 of the 6
uploaded source files were .zip archives. The in-container survey runs
with Read/Glob/Grep only, so every archive was studied by FILENAME
alone — ~90% of the office's real source material silently never
reached the generation prompt. The fix is host-side and pre-survey:
expand each ``*.zip`` sitting DIRECTLY under the workspace ``source/``
directory into a sibling directory named after the zip's stem (the
workspace is bind-mounted, so the extracted files appear under
``/workspace/source/<stem>/`` in the container), and return
user-actionable WARNING strings for anything that could not be
expanded.

Deliberately NOT handled: recursion into subdirectories, nested-archive
extraction, and the non-zip archive formats (.tar/.gz/.rar/.7z) — those
stay filename-only exactly as before (the survey's unreadable-extension
warning covers them).

All filesystem work here is SYNCHRONOUS — callers wrap the call in
``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import zipfile
from pathlib import Path

from src._chown import chown_to_agent

logger = logging.getLogger(__name__)

# Serializes extraction process-wide: two concurrent generations for the
# same office (or same-slug offices) would otherwise race each other on
# the shared per-zip tmp dir and could promote a PARTIAL extraction with
# a valid marker — the exact state the marker design exists to prevent.
# Extraction is rare and bounded, so one coarse lock is the right size.
_EXTRACT_LOCK = threading.Lock()


# Per-archive extraction caps. Metadata-declared totals are checked
# BEFORE any byte lands on disk; an over-cap archive extracts NOTHING
# (a partial extraction would look complete to the survey). The
# byte-count belt in ``_copy_capped`` re-enforces the size cap on the
# actual stream so a zip lying about ``file_size`` can't bomb the disk.
# ``_MAX_ARCHIVE_TOTAL_INFOS`` additionally bounds directory-only
# entries, which the file cap alone would let mkdir without limit.
_MAX_ARCHIVE_ENTRIES = 400
_MAX_ARCHIVE_TOTAL_INFOS = 800
_MAX_ARCHIVE_UNCOMPRESSED_BYTES = 50 * 1024 * 1024

# Written INSIDE a completed extraction dir; records the source zip's
# stat so a re-uploaded (changed) zip re-extracts instead of the stale
# directory silently staying authoritative, and so a partial extraction
# (which never gets a marker — the tmp dir is discarded) is never
# mistaken for a complete one. A non-empty target WITHOUT a marker is
# treated as user-managed content and left alone.
_EXTRACTION_MARKER = ".cbcl-extracted.json"

# Nested archives inside a zip are skipped (never recursively expanded)
# — same family the survey flags as unreadable.
_NESTED_ARCHIVE_SUFFIXES = (".zip", ".tar", ".gz", ".rar", ".7z")

_COPY_CHUNK_BYTES = 1024 * 1024

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def expand_source_archives(
    source_dir: Path, only_names: set[str] | None = None
) -> list[str]:
    """Expand every ``*.zip`` directly under ``source_dir``; return warnings.

    Idempotent via the freshness marker: an up-to-date extraction of the
    SAME zip (marker matches size+mtime) is skipped silently; a STALE
    marker (re-uploaded zip) rebuilds; a non-empty markerless directory
    is user-managed content and never touched. Never raises — every
    failure degrades to a WARNING string (the survey then studies the
    zip by filename only, exactly the pre-fix behaviour).

    ``only_names`` (scoped settings surveys) restricts extraction — and
    therefore the warnings — to the listed zip basenames, so a
    generation that attached two files never surfaces warnings about
    unrelated archives elsewhere in ``source/``.
    """
    warnings: list[str] = []
    if not source_dir.is_dir():
        return warnings
    try:
        candidates = sorted(
            p
            for p in source_dir.iterdir()
            if p.is_file() and p.suffix.lower() == ".zip"
        )
    except OSError as exc:
        logger.warning("Could not list %s for archives: %s", source_dir, exc)
        return warnings
    if only_names is not None:
        candidates = [p for p in candidates if p.name in only_names]
    for zip_path in candidates:
        try:
            with _EXTRACT_LOCK:
                warnings.extend(_expand_one(zip_path))
        except Exception as exc:  # noqa: BLE001 — never fail a survey
            logger.warning(
                "Unexpected failure expanding %s: %s",
                zip_path,
                exc,
                exc_info=True,
            )
            warnings.append(
                f"{zip_path.name}: could not be extracted — it was studied "
                "by filename only."
            )
    return warnings


def extraction_target(zip_path: Path) -> Path:
    """The sibling directory a zip extracts into (``foo.zip`` → ``foo/``).
    The ONE home of the naming convention — survey-side helpers must ask
    here instead of re-deriving it."""
    return zip_path.with_suffix("")


def _has_content_besides_marker(target: Path) -> bool:
    try:
        return target.is_dir() and any(
            p.name != _EXTRACTION_MARKER for p in target.iterdir()
        )
    except OSError:
        return False


def usable_extraction_dir(zip_path: Path) -> bool:
    """True when the zip's sibling directory is safe to survey IN PLACE
    of the zip: either OUR extraction whose marker matches the CURRENT
    zip, or a markerless non-empty directory (user-managed manual
    extraction). A STALE marker (re-uploaded zip whose re-extraction
    failed) returns False so the zip stays listed and its failure
    warning stands — surveying outdated content as current was the
    review's headline daemon finding."""
    target = extraction_target(zip_path)
    if not _has_content_besides_marker(target):
        return False
    if (target / _EXTRACTION_MARKER).exists():
        return _marker_matches(target, _zip_fingerprint(zip_path))
    return True


def _entry_name(info: zipfile.ZipInfo) -> str:
    """Normalise an entry name to forward slashes (Windows-made zips)."""
    return info.filename.replace("\\", "/")


def _is_unsafe(name: str) -> bool:
    """Zip-slip guard: absolute paths and ``..`` segments never extract."""
    return (
        name.startswith("/")
        or bool(_WINDOWS_DRIVE_RE.match(name))
        or ".." in name.strip("/").split("/")
    )


def _single_root(names: list[str]) -> str | None:
    """The one shared top-level DIRECTORY of every entry, or ``None``.

    Flattening applies only when every entry lives under one root dir
    (``foo.zip`` → ``foo/…`` becomes ``foo/…`` not ``foo/foo/…``). A
    file entry sitting AT the root (no ``/`` in its name) means the zip
    has loose files — never flatten then, or the file's own name would
    be stripped.
    """
    roots = {n.strip("/").split("/")[0] for n in names if n.strip("/")}
    if len(roots) != 1:
        return None
    root = next(iter(roots))
    for name in names:
        stripped = name.strip("/")
        if stripped == root and not name.endswith("/"):
            return None  # a FILE named exactly like the root
    return root


def _copy_capped(
    zf: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path, budget: list[int]
) -> bool:
    """Stream one entry to ``dest``; ``budget[0]`` is the remaining
    uncompressed-byte allowance for the archive. Returns ``False`` when
    the stream ran past the budget (the declared sizes lied)."""
    _mkdir_owned(dest.parent)
    with zf.open(info) as src, open(dest, "wb") as out:
        while True:
            chunk = src.read(_COPY_CHUNK_BYTES)
            if not chunk:
                chown_to_agent(dest)
                return True
            budget[0] -= len(chunk)
            if budget[0] < 0:
                return False
            out.write(chunk)


def _mkdir_owned(path: Path) -> None:
    """``mkdir -p`` that chowns every newly created level to the agent
    uid — the daemon runs as root on prod hosts, and a root-owned dir in
    the bind-mounted workspace is unreadable/unwritable in-container."""
    missing: list[Path] = []
    probe = path
    while not probe.exists():
        missing.append(probe)
        probe = probe.parent
    path.mkdir(parents=True, exist_ok=True)
    for created in missing:
        chown_to_agent(created)


def _zip_fingerprint(zip_path: Path) -> dict[str, int] | None:
    try:
        st = zip_path.stat()
    except OSError:
        return None
    return {"zip_size": st.st_size, "zip_mtime_ns": st.st_mtime_ns}


def _marker_matches(target: Path, fingerprint: dict[str, int] | None) -> bool:
    try:
        stored = json.loads((target / _EXTRACTION_MARKER).read_text())
    except (OSError, ValueError):
        return False
    return fingerprint is not None and stored == fingerprint


def _is_dir_entry(info: zipfile.ZipInfo, entry: str) -> bool:
    """Directory detection that survives backslash-made (legacy Windows)
    zips: ``ZipInfo.is_dir()`` only recognises a trailing forward slash,
    so a ``dir\\`` entry would otherwise be treated as a zero-byte FILE
    named ``dir`` — blocking every real file underneath it."""
    return info.is_dir() or entry.endswith("/")


def _expand_one(zip_path: Path) -> list[str]:
    name = zip_path.name
    target = extraction_target(zip_path)
    fingerprint = _zip_fingerprint(zip_path)

    # Symlink hardening: the workspace is agent-writable and the daemon
    # runs as root — never extract THROUGH a link (a linked target would
    # let extraction write outside the workspace; a linked zip obscures
    # what is actually being opened).
    if zip_path.is_symlink() or target.is_symlink():
        return [
            f"{name}: not extracted — the zip or its target directory is "
            "a symlink; it was studied by filename only."
        ]

    if target.exists():
        if not target.is_dir():
            return [
                f"{name}: not extracted — '{target.name}' already exists "
                "beside it and is not a directory."
            ]
        if _marker_matches(target, fingerprint):
            return []  # up-to-date extraction of THIS zip — idempotent skip
        has_marker = (target / _EXTRACTION_MARKER).exists()
        try:
            nonempty = any(target.iterdir())
        except OSError:
            nonempty = True
        if nonempty and not has_marker:
            # User-managed directory (manual extraction, or an unrelated
            # dir sharing the stem) — never delete content we didn't
            # write; the survey reads whatever is there.
            return []
        # Ours but stale (re-uploaded zip) or empty — rebuild below.

    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        logger.warning("Could not open %s as a zip: %s", zip_path, exc)
        return [
            f"{name}: could not be read as a zip archive — re-upload it "
            "or extract it manually."
        ]

    warnings: list[str] = []
    # Extract into a sibling tmp dir and promote atomically on success —
    # a cap breach or IO failure discards the tmp dir, so a PARTIAL
    # extraction never sits at the target looking complete.
    tmp = target.parent / f"{target.name}.extracting"
    complete = True
    with zf:
        infos = zf.infolist()
        file_infos = [
            i for i in infos if not _is_dir_entry(i, _entry_name(i))
        ]

        if len(file_infos) > _MAX_ARCHIVE_ENTRIES:
            return [
                f"{name}: not extracted — {len(file_infos)} files exceed "
                f"the {_MAX_ARCHIVE_ENTRIES}-file cap; it was studied by "
                "filename only."
            ]
        if len(infos) > _MAX_ARCHIVE_TOTAL_INFOS:
            return [
                f"{name}: not extracted — {len(infos)} total entries "
                f"exceed the {_MAX_ARCHIVE_TOTAL_INFOS}-entry cap; it was "
                "studied by filename only."
            ]
        total_bytes = sum(i.file_size for i in file_infos)
        if total_bytes > _MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            cap_mb = _MAX_ARCHIVE_UNCOMPRESSED_BYTES // (1024 * 1024)
            return [
                f"{name}: not extracted — {total_bytes // (1024 * 1024)} MB "
                f"uncompressed exceeds the {cap_mb} MB cap; it was studied "
                "by filename only."
            ]

        unsafe_count = 0
        nested: list[str] = []
        safe: list[tuple[zipfile.ZipInfo, str]] = []
        for info in infos:
            entry = _entry_name(info)
            if not entry.strip("/"):
                continue
            if _is_unsafe(entry):
                unsafe_count += 1
                continue
            if not _is_dir_entry(info, entry) and entry.lower().endswith(
                _NESTED_ARCHIVE_SUFFIXES
            ):
                nested.append(entry.rsplit("/", 1)[-1])
                continue
            safe.append((info, entry))

        if unsafe_count:
            warnings.append(
                f"{name}: skipped {unsafe_count} unsafe "
                f"entr{'y' if unsafe_count == 1 else 'ies'} with absolute "
                "or parent-directory paths (zip-slip guard)."
            )
        if nested:
            warnings.append(
                f"{name}: nested archives inside were not extracted: "
                + ", ".join(nested[:5])
                + ("…" if len(nested) > 5 else "")
                + " — extract them manually if they carry method."
            )

        root = _single_root([entry for _info, entry in safe])
        budget = [_MAX_ARCHIVE_UNCOMPRESSED_BYTES]
        files_written = 0
        try:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
            _mkdir_owned(tmp)
            for info, entry in safe:
                rel = entry.strip("/")
                if root is not None:
                    rel = rel[len(root) :].strip("/")
                    if not rel:
                        continue  # the root directory entry itself
                dest = tmp / rel
                if _is_dir_entry(info, entry):
                    _mkdir_owned(dest)
                    continue
                if not _copy_capped(zf, info, dest, budget):
                    complete = False
                    warnings.append(
                        f"{name}: not extracted — the archive's real "
                        "uncompressed size exceeds the "
                        f"{_MAX_ARCHIVE_UNCOMPRESSED_BYTES // (1024 * 1024)}"
                        " MB cap (declared sizes were smaller); it was "
                        "studied by filename only."
                    )
                    break
                files_written += 1
        except Exception as exc:  # noqa: BLE001
            # OSError = IO failure; BadZipFile = corrupt ENTRY (bad CRC);
            # RuntimeError = password-protected entry; NotImplementedError
            # = unsupported compression. ALL take the discard posture —
            # anything narrower leaks the partial tmp dir into source/
            # where the whole-dir survey and the Files UI would see it.
            logger.warning("Extraction of %s failed partway: %s", zip_path, exc)
            complete = False
            warnings.append(
                f"{name}: extraction failed partway — it was studied by "
                "filename only."
            )

    if complete and files_written == 0:
        # Every entry was filtered (nested-only, unsafe-only, dir-only,
        # or an empty zip): promoting a marker-only directory would make
        # the survey-side helpers treat the zip as extracted and
        # SUPPRESS its honest unreadable warning on every later run.
        complete = False
        warnings.append(
            f"{name}: nothing extractable inside — it was studied by "
            "filename only."
        )

    if not complete:
        shutil.rmtree(tmp, ignore_errors=True)
        return warnings

    try:
        marker = tmp / _EXTRACTION_MARKER
        marker.write_text(json.dumps(fingerprint or {}))
        chown_to_agent(marker)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        tmp.rename(target)
    except OSError as exc:
        logger.warning("Could not promote extraction of %s: %s", zip_path, exc)
        shutil.rmtree(tmp, ignore_errors=True)
        warnings.append(
            f"{name}: extraction could not be finalised — it was studied "
            "by filename only."
        )
    return warnings
