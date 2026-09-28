"""Container-only public Files boundary, independent of workspace Python imports."""

from __future__ import annotations

import base64
import ctypes
import errno
import fcntl
import hashlib
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
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MAX_ENTRIES = 2000
# Tree browsing only reads bounded metadata, unlike archive/read/mutation walks.
MAX_TREE_ENTRIES = 10_000
# Lazy tree browsing (``fs_tree`` with ``lazy: true``, the Files page) never
# fails because a workspace is large. It lists breadth-first from the
# requested folder; a folder past these budgets comes back unloaded
# (``children_loaded: false``) or truncated (``truncated``/``total_entries``)
# for the client to open on demand. Strict ``fs_tree`` (no ``lazy``) keeps its
# complete-or-error contract for callers that need a whole listing.
TREE_DEPTH = 5  # folder levels listed below the requested folder
LAZY_TREE_ENTRIES = 5000  # entries listed per lazy response
LAZY_DIRECTORY_ENTRIES = 2000  # entries listed per folder
LAZY_TREE_SECONDS = 8  # soft budget for expansion, well under DEADLINE_SECONDS
# The requested folder is always listed. Its directory scan stops after this
# many seconds, leaving the rest of LAZY_TREE_SECONDS to check the entries it
# found. A folder too large to scan in time lists the first names found as
# ``truncated`` without ``total_entries``: its size is not known.
LAZY_SCAN_SECONDS = 4
# Escaped JSON bytes per lazy response (``ensure_ascii`` output: a non-ASCII
# name costs six bytes a character), well under MAX_RESPONSE_BYTES.
LAZY_TREE_BYTES = 4 * 1024 * 1024
MAX_DEPTH = 20
MAX_READ_BYTES = 4 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
MAX_CHUNK_BYTES = 4 * 1024 * 1024
MAX_ZIP_INPUT_BYTES = 64 * 1024 * 1024
MAX_ZIP_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 6 * 1024 * 1024
MAX_RESPONSE_BYTES = 12 * 1024 * 1024
# Revision hashing (fs_hash / fs_read sha256 / fs_write_revision) reads at most
# this many bytes; larger files report no hash instead of failing.
MAX_HASH_BYTES = 8 * 1024 * 1024
# fs_list_skills returns the raw head of each SKILL.md (the daemon host
# parses it; this helper runs as ``python3 -I -S`` without PyYAML). One skill's
# head (raw bytes read) and the whole listing's heads are bounded separately.
# The listing budget counts JSON-ESCAPED bytes: the response is encoded with
# ``ensure_ascii=True``, where one undecodable byte becomes ``\ufffd`` (6
# bytes), so a raw-byte budget let heads grow ~6x past MAX_RESPONSE_BYTES.
SKILL_MD_HEAD_BYTES = 32 * 1024
SKILL_HEADS_TOTAL_BYTES = 4 * 1024 * 1024
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
# A Windows drive prefix ("C:"): never addressable (backend mirror:
# app/skills/bundles.py ``_DRIVE_QUALIFIED``).
_DRIVE_QUALIFIED = re.compile(r"^[A-Za-z]:")


def path_parts(relative_path: str) -> tuple[str, ...]:
    if not isinstance(relative_path, str) or len(relative_path) > 2048:
        raise FilesPolicyError("Invalid workspace path")
    if any(ord(character) < 32 or ord(character) == 127 for character in relative_path):
        raise FilesPolicyError("Control characters are not allowed in paths")
    normalized = relative_path.replace("\\", "/")
    if normalized.startswith("/") or ".." in normalized.split("/"):
        raise FilesPolicyError("Invalid workspace path")
    parts = tuple(part for part in normalized.split("/") if part not in {"", "."})
    if any(_DRIVE_QUALIFIED.match(part) for part in parts):
        raise FilesPolicyError("Drive-qualified paths are not allowed")
    if len(parts) > MAX_DEPTH or any(len(part.encode()) > 255 for part in parts):
        raise FilesPolicyError("Workspace path exceeds limits")
    return parts


def protected_path(parts: tuple[str, ...]) -> bool:
    """True when Files must refuse the path.

    Names compare case-insensitively: an office workspace on a
    case-insensitive host filesystem (macOS APFS through a Docker Desktop
    bind) resolves ``.GIT``, ``.Env`` or ``.CLAUDE`` to the protected entry.
    The protected literals are lowercase; ``casefold`` never makes an
    unprotected path protected on a case-sensitive host, it only refuses a
    differently-cased name there too.
    """
    folded = tuple(part.casefold() for part in parts)
    for index, name in enumerate(folded):
        if name in _PROTECTED_NAMES or name.startswith(_PROTECTED_FILE_PREFIXES):
            return True
        if name.startswith(".env.") and name not in {".env.example", ".env.sample"}:
            return True
        if name == ".claude" and (
            index + 1 == len(folded) or folded[index + 1] != "skills"
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


def _escaped_size(text: str) -> int:
    """Bytes ``text`` occupies inside the ``ensure_ascii`` JSON response."""
    return len(json.dumps(text, ensure_ascii=True)) - 2


def _fit_escaped(text: str, budget: int) -> str:
    """The longest prefix of ``text`` whose escaped size fits ``budget``."""
    if _escaped_size(text) <= budget:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if _escaped_size(text[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    return text[:low]


# A child that vanished, became a link or a non-directory, or is unreadable
# between listing and opening is omitted from a tree and counted.
_TREE_UNAVAILABLE_ERRNOS = frozenset(
    {errno.ENOENT, errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}
)
_TREE_IGNORED_NAMES = frozenset({"node_modules", "__pycache__"})
# Fixed JSON bytes of one lazy tree node besides its escaped name and path:
# the keys, type, size, file_kind or children, modified and the markers.
_LAZY_NODE_OVERHEAD_BYTES = 192
# Directory entries scanned between deadline and root-identity checks.
_LAZY_SCAN_CHECK_EVERY = 256


def _lazy_clock() -> float:
    """Monotonic clock for the lazy tree's soft budget (tests replace it)."""
    return time.monotonic()


def _tree_order(name: str) -> tuple[str, str]:
    """Case-insensitive name order with a deterministic tie-break."""
    return name.casefold(), name


def _tree_addressable(parts: tuple[str, ...]) -> bool:
    """True when Files can address this path: its name has no backslash
    (the helper reads one as a separator) and the whole path passes the
    public path rules (control characters, drive prefixes, component and
    path length, depth, non-UTF-8 names)."""
    if "\\" in parts[-1]:
        return False
    try:
        path_parts("/".join(parts))
    except (FilesPolicyError, UnicodeEncodeError):
        return False
    return True


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
# Names a directory listing never returns; checked with the whole-path rules.
_NOT_A_CHILD_NAME = frozenset({"", ".", ".."})


class _ChildRules:
    """``protected_path`` and ``_tree_addressable`` for the children of one
    folder, at a cost that depends on the child's name only.

    Checking the whole path for every entry a scan reads made one huge
    folder deep in a workspace slower in proportion to its depth, past the
    helper's CPU limit. The folder's own path is checked once here; each
    child then needs only its own name checked and two sums. The results
    equal the whole-path rules (tests compare them); a folder whose own path
    does not pass those rules uses them for every child.
    """

    def __init__(self, current: tuple[str, ...]) -> None:
        self.current = current
        prefix = "/".join(current)
        try:
            self.fast = path_parts(prefix) == current
        except (FilesPolicyError, UnicodeEncodeError):
            self.fast = False
        # Characters a child name may have within the 2048-character path.
        self.room = 2048 - (len(prefix) + 1 if current else 0)
        self.depth_left = len(current) < MAX_DEPTH
        # ``protected_path`` protects a path when any one component is
        # protected; only ``.claude`` also reads the next component, which
        # must be ``skills``. So the folder's part of the answer is one of
        # two values, decided by whether the child's name folds to "skills".
        self.protected_before_skills = protected_path((*current, "skills"))
        self.protected_before_other = protected_path((*current, "a"))

    def protected(self, name: str) -> bool:
        """``protected_path((*current, name))``."""
        if protected_path((name,)):
            return True
        if name.casefold() == "skills":
            return self.protected_before_skills
        return self.protected_before_other

    def addressable(self, name: str) -> bool:
        """``_tree_addressable((*current, name))``."""
        if not self.fast or name in _NOT_A_CHILD_NAME or "/" in name:
            return _tree_addressable((*self.current, name))
        if (
            not self.depth_left
            or len(name) > self.room
            or "\\" in name
            or _CONTROL_CHARACTERS.search(name)
            or _DRIVE_QUALIFIED.match(name)
        ):
            return False
        try:
            return len(name.encode()) <= 255
        except UnicodeEncodeError:
            return False


def _lazy_node_bytes(name: str, path: str) -> int:
    return _escaped_size(name) + _escaped_size(path) + _LAZY_NODE_OVERHEAD_BYTES


class _FirstNames:
    """The first names in tree order seen so far, in bounded memory.

    Keeps at least ``keep`` spare names beyond the ``keep`` a folder lists,
    so an entry that fails full validation is replaced by the next one.
    Once it has pruned, a name after the last one kept is only counted.
    """

    def __init__(self, keep: int) -> None:
        self.keep = max(keep, 1)
        self.count = 0
        self.keys: list[tuple[str, str]] = []
        self.bound: tuple[str, str] | None = None

    def add(self, name: str) -> None:
        self.count += 1
        key = _tree_order(name)
        if self.bound is not None and key >= self.bound:
            return
        self.keys.append(key)
        if len(self.keys) > 4 * self.keep:
            self.keys.sort()
            del self.keys[2 * self.keep :]
            self.bound = self.keys[-1]

    def first(self) -> list[str]:
        self.keys.sort()
        return [name for _folded, name in self.keys]


class _LazyFolderUnavailable(Exception):
    """A folder listed earlier is gone or replaced before its expansion."""


def _lazy_folder_size(node: dict) -> int:
    """Set each folder's size to the total of what it lists (like strict)."""
    if node["type"] == "folder":
        node["size"] = sum(_lazy_folder_size(child) for child in node["children"])
    return node["size"]


def _mount_id(descriptor: int) -> str:
    with open(f"/proc/self/fdinfo/{descriptor}", encoding="ascii") as details:
        for line in details:
            if line.startswith("mnt_id:"):
                return line.split(":", 1)[1].strip()
    raise FilesPolicyError("Secure Files requires Linux mount identity support")


class FilesPolicyError(ValueError):
    pass


class UnsupportedEntryError(FilesPolicyError):
    """An individual child is outside the public Files boundary."""


class EntryLimitError(FilesPolicyError):
    """The operation exceeded its entry budget."""


class FileChangedError(FilesPolicyError):
    """A file's size or times changed while it was being read (retryable)."""

    def __init__(self) -> None:
        super().__init__("File changed while being read; retry the operation")


class SkillFolderTooLargeError(FilesPolicyError):
    """A live skill folder is too large to verify in one helper call.

    ``code``: ``too_many_files`` (over the bundle file-count cap),
    ``too_many_bytes`` (more supported bytes than one call can hash) or
    ``scan_time`` (the hash pass ran into its time budget).
    """

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class RevisionConflictError(Exception):
    """A revision-checked write found different current content."""

    def __init__(self, current_sha256: str | None) -> None:
        super().__init__("The file changed since the expected revision")
        self.current_sha256 = current_sha256


class _LimitedBuffer(io.BytesIO):
    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > MAX_ZIP_BYTES:
            raise FilesPolicyError(
                "ZIP output exceeds the 8 MiB export limit; select a smaller subfolder"
            )
        return super().write(data)


class UnsupportedFilesystemError(RuntimeError):
    pass


RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2


def _renameat2(
    source_parent: int,
    source: str,
    destination_parent: int,
    destination: str,
    flags: int,
) -> None:
    """``renameat2(2)`` with ``flags``; typed errors for the callers.

    ``FileExistsError`` for ``EEXIST`` (NOREPLACE), ``UnsupportedFilesystemError``
    when the filesystem cannot honour the flag (Docker Desktop binds), any
    other failure as ``OSError``.
    """
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
            source_parent,
            source.encode(),
            destination_parent,
            destination.encode(),
            flags,
        )
        != 0
    ):
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(error, "Destination already exists")
        if error in {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP}:
            raise UnsupportedFilesystemError(
                "Atomic rename flags are unsupported by this workspace filesystem"
            )
        raise OSError(error, "Rename failed")


def _rename_noreplace(
    source_parent: int, source: str, destination_parent: int, destination: str
) -> None:
    try:
        _renameat2(
            source_parent, source, destination_parent, destination, RENAME_NOREPLACE
        )
    except FileExistsError as error:
        raise FilesPolicyError("Destination already exists") from error
    # UnsupportedFilesystemError propagates unchanged: execute() maps it to a
    # fixed 501 message and never reads the exception text.


# ---------------------------------------------------------------------------
# Skill bundle publication (F03). The backend stages a complete, verified
# skill directory in a PRIVATE area outside ``.claude/skills`` and the helper
# swaps the whole directory in atomically; a live skill folder is never
# mutated in place. Path rules and content identity mirror
# ``backend/app/skills/bundles.py`` (parity pinned by the backend tests).
#
#   .claude/.cubicle-skill-bundles/staging/<publication_id>/{meta.json,bundle/}
#   .claude/.cubicle-skill-bundles/journal/<publication_id>.json
#   .claude/.cubicle-skill-bundles/retired/<skill>/<epoch>-<random>/
#
# ``.claude/<anything but skills>`` is a protected path, so the area is
# invisible to public Files, skill discovery, workspace_setup symlinks and
# the task-Agent snapshot reader. Retired versions stay at least
# ``SKILL_RETIRED_GRACE_SECONDS`` (the newest ``SKILL_RETIRED_KEEP`` always),
# so a snapshot that opened the old version never loses it mid-copy.
# ---------------------------------------------------------------------------
SKILL_BUNDLE_MANIFEST = ".cubicle-bundle.json"
SKILL_BUNDLE_FORMAT = 1
SKILL_BUNDLE_AREA = (".claude", ".cubicle-skill-bundles")
SKILL_BUNDLE_MAX_FILES = 1000
SKILL_BUNDLE_MAX_FILE_BYTES = 8 * 1024 * 1024
SKILL_BUNDLE_MAX_TOTAL_BYTES = 32 * 1024 * 1024
SKILL_BUNDLE_MAX_PATH_BYTES = 1024
SKILL_BUNDLE_MAX_PATH_DEPTH = 16
SKILL_BUNDLE_MANIFEST_MAX_BYTES = 512 * 1024
SKILL_STAGE_PUT_MAX_BYTES = 3 * 1024 * 1024
SKILL_STAGE_PUT_MAX_FILES = 256
SKILL_PARAMS_MAX_BYTES = 4 * 1024 * 1024
SKILL_STAGING_STALE_SECONDS = 3600
SKILL_RETIRED_GRACE_SECONDS = 1800
SKILL_RETIRED_KEEP = 2
SKILL_BUNDLE_ENTRY_LIMIT = 40_000
# Pruning retired versions and abandoned staging runs on a budget of its own
# (at most this much of the helper deadline, a fresh entry budget): a huge
# retired tree is removed over several calls instead of failing the action.
SKILL_HOUSEKEEPING_SECONDS = 3
# A live folder whose supported files total more than this is not hashed:
# it is reported "unverified" instead of risking the helper deadline (a
# managed bundle is at most SKILL_BUNDLE_MAX_TOTAL_BYTES, so it always fits).
SKILL_LIVE_HASH_MAX_BYTES = 256 * 1024 * 1024
# Time kept back from the helper deadline while hashing a live folder, so a
# commit still has time to verify, swap and retire after the scan.
SKILL_LIVE_SCAN_MARGIN_SECONDS = 8
SKILL_JOURNAL_RECOVERY_LIMIT = 64
_SKILL_MODES = {0o644, 0o755}
_SKILL_PUBLICATION_ID = re.compile(r"[0-9a-f]{32}")
_SKILL_SHA256 = re.compile(r"[0-9a-f]{64}")
_SKILL_RETIRED_NAME = re.compile(r"(\d{10})-[0-9a-f]{12}")
# ``_skill_write_json``'s temporary name for a journal record: one left by a
# helper that died before its rename is garbage.
_SKILL_JOURNAL_TEMP = re.compile(r"\.[0-9a-f]{32}\.json\.[0-9a-f]{32}\.tmp")
# A folder found on the live path in the journaled gap (an agent's direct
# write), retired for the publication that then landed: the name carries
# that publication's id, so a read-back can report it too.
_SKILL_OCCUPANT_NAME = re.compile(r"(\d{10})-[0-9a-f]{12}-occupant-([0-9a-f]{32})")
_SKILL_PUBLISH_MODES = frozenset({"update", "ensure", "create"})
# ``rename`` of the staged directory onto an occupied live path.
_SKILL_OCCUPIED_ERRNOS = frozenset(
    {errno.EEXIST, errno.ENOTEMPTY, errno.EISDIR, errno.ENOTDIR}
)
# The ``bundle`` identity ``fs_list_skills`` reports for a published folder.
# Deliberately no ``modified`` flag: that needs a full content hash of every
# published folder on each listing (``fs_skill_status`` computes it).
SKILL_BUNDLE_IDENTITY_KEYS = (
    "bundle_sha256",
    "publication_id",
    "published_at",
    "source_kind",
    "source_revision",
)
_SKILL_SOURCE_KEYS = (
    "kind",
    "template_id",
    "repository",
    "ref",
    "commit",
    "path",
    "revision",
)
_SKILL_SKIPPABLE_ERRNOS = frozenset(
    {errno.ENOENT, errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}
)


def _now() -> float:
    """Wall clock for retention decisions (tests monkeypatch it)."""
    return time.time()


def skill_name_violation(name: object) -> str | None:
    """Why ``name`` cannot be a skill folder, or ``None`` (the backend's
    ``app/skills/bundles.skill_name_violation``; parity pinned by
    ``backend/tests/unit/test_skill_bundles.py``)."""
    if not isinstance(name, str) or not name.strip():
        return "empty name"
    if len(name.encode()) > 255:
        return "name too long"
    if "/" in name or "\\" in name:
        return "path separators are not allowed"
    if name in (".", ".."):
        return "not a valid name"
    if name[0] in ".~-":
        return "names may not start with '.', '~' or '-'"
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        return "control characters are not allowed"
    if _DRIVE_QUALIFIED.match(name):
        return "drive-qualified names are not allowed"
    if protected_path((".claude", "skills", name)):
        return "protected runtime names are not allowed"
    return None


def skill_bundle_path_violation(path: object) -> str | None:
    """Why ``path`` cannot be a file in a published skill, or ``None``."""
    if not isinstance(path, str) or not path:
        return "empty path"
    if "\\" in path:
        return "backslashes are not allowed"
    if path.startswith("/"):
        return "path must be relative"
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        return "control characters are not allowed"
    parts = path.split("/")
    if "" in parts or "." in parts or ".." in parts:
        return "empty, '.' or '..' components are not allowed"
    try:
        encoded = path.encode()
    except UnicodeEncodeError:
        # os.scandir surrogate-escapes a name whose bytes are not UTF-8
        # (e.g. unzipped from a cp1252 archive): unsupported, never fatal.
        return "the name is not valid UTF-8"
    if len(encoded) > SKILL_BUNDLE_MAX_PATH_BYTES:
        return "path is too long"
    if len(parts) > SKILL_BUNDLE_MAX_PATH_DEPTH:
        return "path is too deep"
    if any(len(part.encode()) > 255 for part in parts):
        return "a path component is too long"
    if any(_DRIVE_QUALIFIED.match(part) for part in parts):
        return "drive-qualified components are not allowed"
    # Case-insensitively (backend parity): on a case-insensitive workspace
    # ``Params.json`` IS the root params.json that commit replaces.
    if any(part.casefold() == SKILL_BUNDLE_MANIFEST for part in parts):
        return "the manifest name is reserved"
    if len(parts) == 1 and parts[0].casefold() == "params.json":
        return "the root params.json is reserved"
    if protected_path(tuple(parts)) or protected_path(
        (".claude", "skills", "x", *parts)
    ):
        return "protected runtime paths cannot be part of a skill"
    return None


def _skill_digest_lines(entries: list[dict], unsupported: list[str] = ()) -> str:
    """SHA-256 over one line per entry, sorted by the UTF-8 bytes of the path.

    A file is ``"<mode octal> <size> <sha256> <path>\\n"``. An entry of a LIVE
    folder that no bundle can contain (a link, special or oversize file, a
    protected name) is ``"! <path>\\n"``, so such a folder never equals a
    published bundle.
    """
    lines = [
        (
            str(entry["path"]).encode(),
            f"{int(entry['mode']):o} {int(entry['size'])} {entry['sha256']} "
            f"{entry['path']}\n".encode(),
        )
        for entry in entries
    ]
    # An unsupported path may be a surrogate-escaped non-UTF-8 name: encode
    # it back to its original bytes rather than failing the digest.
    lines.extend(
        (
            path.encode(errors="surrogateescape"),
            f"! {path}\n".encode(errors="surrogateescape"),
        )
        for path in unsupported
    )
    digest = hashlib.sha256()
    for _path, line in sorted(lines):
        digest.update(line)
    return digest.hexdigest()


def skill_bundle_sha256(entries: list[dict]) -> str:
    """Canonical content identity: one line per file, sorted by path bytes."""
    return _skill_digest_lines(entries)


def skill_bundle_manifest_bytes(manifest: dict) -> bytes:
    """The exact ``.cubicle-bundle.json`` written for ``manifest``.

    Refuses (``FilesPolicyError``) a manifest the helper's own reader could
    not read back (``SKILL_BUNDLE_MANIFEST_MAX_BYTES``): writer and reader
    share one limit, which the backend mirrors before staging.
    """
    payload = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
    if len(payload) > SKILL_BUNDLE_MANIFEST_MAX_BYTES:
        raise FilesPolicyError(
            "The skill bundle's manifest would exceed "
            f"{SKILL_BUNDLE_MANIFEST_MAX_BYTES // 1024} KiB; publish fewer files "
            "or shorter paths"
        )
    return payload


# The live "digest" of a non-directory where a skill folder should be.
_NOT_A_DIRECTORY_DIGEST = hashlib.sha256(b"!not-a-directory\n").hexdigest()


class SkillCommitUncertainError(Exception):
    """A commit failed after its journal was written: the swap may have
    happened (or recovery may complete it). Never a definitive refusal —
    the caller reads the state back with ``fs_skill_status``."""


class SkillPublicationRefusedError(Exception):
    """A commit refused before its journal was written; nothing changed."""


class SkillBundleConflictError(Exception):
    """A publication precondition failed; nothing in the live skill changed."""

    def __init__(
        self, message: str, code: str, current_digest: str | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.current_digest = current_digest


def _skill_publication_id(params: dict) -> str:
    value = params.get("publication_id")
    if not isinstance(value, str) or not _SKILL_PUBLICATION_ID.fullmatch(value):
        raise FilesPolicyError("publication_id must be 32 lowercase hex characters")
    return value


def _skill_name(params: dict) -> str:
    name = params.get("skill_name")
    violation = skill_name_violation(name)
    if violation:
        raise FilesPolicyError(f"Invalid skill name: {violation}")
    return name


_SKILL_RESERVED_ROOT_NAMES = (SKILL_BUNDLE_MANIFEST, "params.json")


def _skill_reserved_root_entry(directory: int, name: str) -> bool:
    """Whether a live skill root entry is the platform manifest or params.

    The exact names always are. A case variant (``Params.json``) is only
    when it IS the canonical file — the same inode, as on a
    case-insensitive workspace. On a case-sensitive workspace it is a
    separate user file: it must not be skipped, so the path rule reports it
    as unsupported and the folder never equals a published bundle.
    """
    if name in _SKILL_RESERVED_ROOT_NAMES:
        return True
    canonical = name.casefold()
    if canonical not in _SKILL_RESERVED_ROOT_NAMES:
        return False
    try:
        entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
        reserved = os.stat(canonical, dir_fd=directory, follow_symlinks=False)
    except OSError:
        return False
    return (entry.st_dev, entry.st_ino) == (reserved.st_dev, reserved.st_ino)


def skill_case_collision(paths: list[str]) -> tuple[str, str] | None:
    """Two paths a case-insensitive workspace would merge (backend parity)."""
    files: dict[str, str] = {}
    directories: dict[str, str] = {}
    for path in paths:
        parts = path.split("/")
        for depth in range(1, len(parts)):
            prefix = "/".join(parts[:depth])
            folded = prefix.casefold()
            if folded in files:
                return files[folded], path
            spelled = directories.setdefault(folded, prefix)
            if spelled != prefix:
                return spelled, prefix
        folded = path.casefold()
        if folded in files:
            return files[folded], path
        if folded in directories:
            return directories[folded], path
        files[folded] = path
    return None


def _skill_manifest_files(manifest: object) -> tuple[list[dict], str, dict]:
    """Validated ``(files, bundle_sha256, source)`` of a commit manifest."""
    if not isinstance(manifest, dict):
        raise FilesPolicyError("manifest must be an object")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise FilesPolicyError("manifest.files must be a non-empty list")
    if len(files) > SKILL_BUNDLE_MAX_FILES:
        raise FilesPolicyError(
            f"A skill bundle may hold at most {SKILL_BUNDLE_MAX_FILES} files"
        )
    clean: list[dict] = []
    seen: set[str] = set()
    total = 0
    for entry in files:
        if not isinstance(entry, dict):
            raise FilesPolicyError("manifest.files entries must be objects")
        path = entry.get("path")
        violation = skill_bundle_path_violation(path)
        if violation:
            raise FilesPolicyError(f"Invalid bundle path {path!r}: {violation}")
        if path.casefold() in seen:
            raise FilesPolicyError(f"Duplicate bundle path {path!r}")
        size, digest, mode = entry.get("size"), entry.get("sha256"), entry.get("mode")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= SKILL_BUNDLE_MAX_FILE_BYTES
        ):
            raise FilesPolicyError(f"Invalid size for {path!r}")
        if not isinstance(digest, str) or not _SKILL_SHA256.fullmatch(digest):
            raise FilesPolicyError(f"Invalid sha256 for {path!r}")
        if mode not in _SKILL_MODES:
            raise FilesPolicyError(f"Invalid mode for {path!r}")
        total += size
        seen.add(path.casefold())
        clean.append({"path": path, "size": size, "sha256": digest, "mode": mode})
    if total > SKILL_BUNDLE_MAX_TOTAL_BYTES:
        raise FilesPolicyError("The skill bundle exceeds the total size limit")
    collision = skill_case_collision([entry["path"] for entry in clean])
    if collision is not None:
        raise FilesPolicyError(
            f"Bundle paths {collision[0]!r} and {collision[1]!r} differ only by "
            "letter case"
        )
    if not any(entry["path"] == "SKILL.md" for entry in clean):
        raise FilesPolicyError("The skill bundle has no root SKILL.md")
    bundle_sha = manifest.get("bundle_sha256")
    if bundle_sha != skill_bundle_sha256(clean):
        raise FilesPolicyError("manifest.bundle_sha256 does not match its files")
    raw_source = manifest.get("source")
    source: dict = {}
    if isinstance(raw_source, dict):
        for key in _SKILL_SOURCE_KEYS:
            value = raw_source.get(key)
            if isinstance(value, str) and value and len(value) <= 300:
                source[key] = value
    return clean, bundle_sha, source


class _SkillBundleActions:
    """``fs_skill_*`` actions, mixed into :class:`SecureWorkspace`.

    Every action raises the entry budget (a bundle holds up to 1000 files
    and a commit walks the staged and the live version), then finishes or
    drops any publication interrupted between its renames (journal).
    """

    root_fd: int
    mount_id: str
    entries: int
    entry_limit: int
    deadline: float
    # Skill -> why its journaled publication could not be finished, from the
    # latest recovery (see :meth:`_skill_recover_journals`).
    _skill_recovery_errors: dict[str, str]

    # -- plumbing --------------------------------------------------------

    def _skill_begin_action(self) -> None:
        self.entry_limit = SKILL_BUNDLE_ENTRY_LIMIT
        self._skill_recover_journals()

    def _skill_recovery_error(self, name: str) -> str | None:
        """Why an interrupted publication of ``name`` is still unfinished."""
        for skill, reason in self._skill_recovery_errors.items():
            if skill.casefold() == name.casefold():  # a case-insensitive host
                return reason
        return None

    def _skill_refuse_if_stuck(self, name: str) -> None:
        """Refuse to change a skill whose publication recovery could not
        finish: its live folder may be the gap between the two renames."""
        reason = self._skill_recovery_error(name)
        if reason is not None:
            raise FilesPolicyError(
                f"An interrupted publication of skill {name!r} could not be "
                f"finished ({reason}), so its folder cannot be changed yet; "
                "nothing was changed"
            )

    @contextmanager
    def _skill_housekeeping(self):
        """Run best-effort cleanup on a budget of its own.

        Pruning can meet a huge tree (an ``npm install`` inside a retired
        skill). It gets a fresh entry budget and at most
        ``SKILL_HOUSEKEEPING_SECONDS`` of the helper deadline; whatever it
        leaves is removed by a later call. The caller's budget is restored,
        so cleanup never makes the action itself fail.
        """
        saved = (self.entries, self.entry_limit, self.deadline)
        self.entries, self.entry_limit = 0, SKILL_BUNDLE_ENTRY_LIMIT
        self.deadline = min(
            self.deadline, time.monotonic() + SKILL_HOUSEKEEPING_SECONDS
        )
        try:
            yield
        except (FileNotFoundError, FilesPolicyError, TimeoutError, OSError):
            pass
        finally:
            self.entries, self.entry_limit, self.deadline = saved

    def _skill_tidy(self, skill: str | None = None) -> None:
        """Best-effort cleanup on one housekeeping budget: abandoned staging,
        the retired versions of deleted skills and, for ``skill``, its retired
        versions beyond the retention."""
        with self._skill_housekeeping():
            self._skill_prune_stale_staging()
            if skill is not None:
                self._skill_prune_retired(skill)
            self._skill_prune_orphaned_retired()

    def _skill_recover_before_access(self) -> None:
        """Finish interrupted publications before touching ``.claude/skills``.

        Discovery and ordinary Files writes under the skills root run this
        first, so neither can observe (or write into) the window a journaled
        two-rename swap leaves between its renames. Recovery gets the bundle
        entry budget; the caller's own budget is restored afterwards.
        """
        entries, limit = self.entries, self.entry_limit
        self.entry_limit = SKILL_BUNDLE_ENTRY_LIMIT
        try:
            self._skill_recover_journals()
        finally:
            self.entries, self.entry_limit = entries, limit

    @contextmanager
    def _skill_area(self, *parts: str, create: bool = False):
        with self._directory((*SKILL_BUNDLE_AREA, *parts), create=create) as fd:
            yield fd

    @contextmanager
    def _skills_root(self, *, create: bool = False):
        with self._directory((".claude", "skills"), create=create) as fd:
            yield fd

    @staticmethod
    def _skill_stat(parent: int, name: str) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def _skill_open_dir(self, parent: int, name: str) -> int:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
        try:
            self._validate_fd(descriptor, directory=True)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _skill_write_new(
        self, parent: int, name: str, data: bytes, mode: int = 0o644
    ) -> None:
        """Create ``name`` exclusively with ``data``, fsynced."""
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_FLAGS,
            mode,
            dir_fd=parent,
        )
        try:
            if data and os.write(descriptor, data) != len(data):
                raise OSError("Incomplete file write")
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _skill_read_small(self, parent: int, name: str, limit: int) -> bytes | None:
        """A regular single-link file's bytes; ``None`` when it is absent."""
        try:
            descriptor = os.open(name, os.O_RDONLY | _FILE_FLAGS, dir_fd=parent)
        except FileNotFoundError:
            return None
        try:
            metadata = self._validate_fd(descriptor, directory=False)
            if metadata.st_size > limit:
                raise FilesPolicyError(f"{name} exceeds its size limit")
            return self._read_fd(descriptor, limit)
        finally:
            os.close(descriptor)

    def _skill_write_json(self, parent: int, name: str, value: dict) -> None:
        """Atomically replace a private JSON file (journal, staging meta)."""
        temporary = f".{name}.{uuid.uuid4().hex}.tmp"
        payload = json.dumps(value, sort_keys=True).encode()
        self._skill_write_new(parent, temporary, payload, 0o600)
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)

    def _skill_remove_tree(self, parent: int, name: str) -> None:
        """Delete a private-area entry recursively, never following links."""
        self._tick(1)
        metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            descriptor = self._skill_open_dir(parent, name)
            try:
                for child in os.listdir(descriptor):
                    self._skill_remove_tree(descriptor, child)
            finally:
                os.close(descriptor)
            os.rmdir(name, dir_fd=parent)
        else:
            os.unlink(name, dir_fd=parent)

    # -- reading a skill tree -------------------------------------------

    def _skill_measure(self, descriptor: int) -> None:
        """Refuse to hash a live folder too large to verify — by stat alone.

        Walks the tree with the listing's rules (root skip, path rule, no
        links) and counts the files the listing would hash. Raises
        :class:`SkillFolderTooLargeError` before any hashing when there are
        more than the bundle file cap or more bytes than
        ``SKILL_LIVE_HASH_MAX_BYTES``; an entry-budget breach raises
        :class:`EntryLimitError`.
        """
        count = 0
        total = 0

        def visit(directory: int, parts: tuple[str, ...]) -> None:
            nonlocal count, total
            with os.scandir(directory) as entries:
                names = [entry.name for entry in entries]
            for name in names:
                self._tick(1)
                if not parts and _skill_reserved_root_entry(directory, name):
                    continue
                child = (*parts, name)
                if skill_bundle_path_violation("/".join(child)) is not None:
                    continue
                try:
                    metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if stat.S_ISDIR(metadata.st_mode):
                        child_fd = self._skill_open_dir(directory, name)
                        try:
                            visit(child_fd, child)
                        finally:
                            os.close(child_fd)
                        continue
                except UnsupportedEntryError:
                    continue
                except OSError as error:
                    if error.errno not in _SKILL_SKIPPABLE_ERRNOS:
                        raise
                    continue
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_size > SKILL_BUNDLE_MAX_FILE_BYTES
                ):
                    continue
                count += 1
                total += metadata.st_size
                if count > SKILL_BUNDLE_MAX_FILES:
                    raise SkillFolderTooLargeError(
                        "The skill folder exceeds the bundle file-count limit",
                        "too_many_files",
                    )
                if total > SKILL_LIVE_HASH_MAX_BYTES:
                    raise SkillFolderTooLargeError(
                        "The skill folder holds more data than one pass can verify",
                        "too_many_bytes",
                    )

        visit(descriptor, ())

    def _skill_listing(
        self,
        descriptor: int,
        *,
        live_root: bool,
        soft_deadline: float | None = None,
    ) -> tuple[list[dict], list[str]]:
        """``(files, unsupported)`` of a skill tree; links are never followed.

        ``files`` are regular single-link files within the per-file cap, as
        manifest entries. ``unsupported`` names everything else: links,
        special files, hardlinks, mounted or oversize entries, protected or
        reserved names. At a LIVE root the platform manifest and the ROOT
        ``params.json`` are skipped — the task-Agent snapshot reader excludes
        that same root file only, so a nested ``params.json`` is an ordinary
        skill resource (published, hashed and snapshotted).
        """
        files: list[dict] = []
        unsupported: list[str] = []

        def visit(directory: int, parts: tuple[str, ...]) -> None:
            with os.scandir(directory) as entries:
                names = sorted(entry.name for entry in entries)
            for name in names:
                self._tick(1)
                if soft_deadline is not None and time.monotonic() >= soft_deadline:
                    raise SkillFolderTooLargeError(
                        "The skill folder could not be verified in time",
                        "scan_time",
                    )
                if (
                    live_root
                    and not parts
                    and _skill_reserved_root_entry(directory, name)
                ):
                    continue
                child = (*parts, name)
                relative = "/".join(child)
                if skill_bundle_path_violation(relative) is not None:
                    unsupported.append(relative)
                    continue
                entry = self._skill_entry(directory, name, relative)
                if entry == "directory":
                    child_fd = self._skill_open_dir(directory, name)
                    try:
                        visit(child_fd, child)
                    finally:
                        os.close(child_fd)
                elif entry is None:
                    unsupported.append(relative)
                else:
                    files.append(entry)
                    if len(files) > SKILL_BUNDLE_MAX_FILES:
                        raise SkillFolderTooLargeError(
                            "The skill folder exceeds the bundle file-count limit",
                            "too_many_files",
                        )

        visit(descriptor, ())
        return files, unsupported

    def _skill_entry(self, directory: int, name: str, relative: str):
        """``"directory"``, a manifest entry for a supported file, or None."""
        try:
            metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                probe = self._skill_open_dir(directory, name)
                os.close(probe)
                return "directory"
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                return None
            descriptor = os.open(name, os.O_RDONLY | _FILE_FLAGS, dir_fd=directory)
            try:
                checked = self._validate_fd(descriptor, directory=False)
                digest, size = self._digest_fd(
                    descriptor, SKILL_BUNDLE_MAX_FILE_BYTES, initial=checked
                )
            finally:
                os.close(descriptor)
        except UnsupportedEntryError:
            return None
        except OSError as error:
            if error.errno not in _SKILL_SKIPPABLE_ERRNOS:
                raise
            return None
        if digest is None:
            return None
        return {
            "path": relative,
            "size": size,
            "sha256": digest,
            "mode": 0o755 if checked.st_mode & 0o111 else 0o644,
        }

    def _skill_read_manifest(self, descriptor: int, name: str) -> dict | None:
        """Identity from the live folder's platform manifest, or ``None``."""
        try:
            raw = self._skill_read_small(
                descriptor, SKILL_BUNDLE_MANIFEST, SKILL_BUNDLE_MANIFEST_MAX_BYTES
            )
            manifest = json.loads(raw) if raw is not None else None
        except (FilesPolicyError, OSError, ValueError):
            return None
        if (
            not isinstance(manifest, dict)
            or manifest.get("format") != SKILL_BUNDLE_FORMAT
            or manifest.get("skill") != name
            or not isinstance(manifest.get("bundle_sha256"), str)
            or not _SKILL_SHA256.fullmatch(manifest["bundle_sha256"])
            or not isinstance(manifest.get("publication_id"), str)
            or not _SKILL_PUBLICATION_ID.fullmatch(manifest["publication_id"])
        ):
            return None
        source = manifest.get("source")
        source = source if isinstance(source, dict) else {}
        return {
            "bundle_sha256": manifest["bundle_sha256"],
            "publication_id": manifest["publication_id"],
            "published_at": str(manifest.get("published_at") or "")[:40] or None,
            "source": {
                key: source[key]
                for key in _SKILL_SOURCE_KEYS
                if isinstance(source.get(key), str) and len(source[key]) <= 300
            },
        }

    def _skill_live_state(self, skills: int, name: str) -> dict:
        """What ``.claude/skills/<name>`` holds right now."""
        metadata = self._skill_stat(skills, name)
        if metadata is None:
            return {"exists": False, "digest": None}
        if not stat.S_ISDIR(metadata.st_mode):
            return {
                "exists": True,
                "managed": False,
                "modified": False,
                "digest": _NOT_A_DIRECTORY_DIGEST,
                "has_skill_md": False,
                "file_count": 0,
                "unsupported_count": 1,
            }
        descriptor = self._skill_open_dir(skills, name)
        try:
            manifest = self._skill_read_manifest(descriptor, name)
            entries = self.entries
            try:
                # Size first, by stat: an oversized folder is detected by
                # count or bytes, not by running out the helper deadline.
                self._skill_measure(descriptor)
                self.entries = entries
                files, unsupported = self._skill_listing(
                    descriptor,
                    live_root=True,
                    soft_deadline=self.deadline - SKILL_LIVE_SCAN_MARGIN_SECONDS,
                )
            except (SkillFolderTooLargeError, EntryLimitError) as error:
                # Too large to verify (e.g. an ``npm install`` inside the
                # skill): report it instead of blocking linking and
                # reinstalling. Every other error — a file changing while it
                # is read, the workspace root changing — stays a retryable
                # refusal and is never reported as "unverified".
                self.entries = entries
                return self._skill_unverified_state(descriptor, manifest, error)
        finally:
            os.close(descriptor)
        digest = _skill_digest_lines(files, unsupported)
        state = {
            "exists": True,
            "managed": manifest is not None,
            "modified": manifest is not None and digest != manifest["bundle_sha256"],
            "digest": digest,
            "has_skill_md": any(entry["path"] == "SKILL.md" for entry in files),
            "file_count": len(files),
            "unsupported_count": len(unsupported),
        }
        if manifest is not None:
            state.update(manifest)
        return state

    def _skill_unverified_state(
        self, descriptor: int, manifest: dict | None, error: Exception
    ) -> dict:
        """Live state of a folder too large to hash in one helper call.

        The digest is a sentinel that can never equal a real bundle: a hash
        of a bounded fingerprint of the top level (names, sizes, mtimes),
        so an update's compare-and-swap still notices top-level changes. A
        commit over such a folder retires it whole, like any replaced
        version; nothing is deleted.
        """
        fingerprint = hashlib.sha256(b"!unverified\n")
        with os.scandir(descriptor) as entries:
            names = sorted(entry.name for entry in entries)
        for child in names[:256]:
            try:
                metadata = os.stat(child, dir_fd=descriptor, follow_symlinks=False)
            except OSError:
                continue
            fingerprint.update(
                f"{child}\0{metadata.st_mode:o}\0{metadata.st_size}"
                f"\0{metadata.st_mtime_ns}\n".encode(errors="surrogateescape")
            )
        fingerprint.update(f"#{len(names)}\n".encode())
        try:
            skill_md = os.stat("SKILL.md", dir_fd=descriptor, follow_symlinks=False)
            has_skill_md = stat.S_ISREG(skill_md.st_mode)
        except OSError:
            has_skill_md = False
        state = {
            "exists": True,
            "managed": manifest is not None,
            "modified": manifest is not None,  # unknown: assume edited
            "digest": fingerprint.hexdigest(),
            "has_skill_md": has_skill_md,
            "file_count": None,
            "unsupported_count": None,
            "unverified": True,
            "unverified_reason": str(error),
            "unverified_reason_code": getattr(error, "code", "entry_budget"),
        }
        if manifest is not None:
            state.update(manifest)
        return state

    def _skill_live_publication(self, name: str) -> str | None:
        """The publication id the live folder carries, if any."""
        try:
            with self._skills_root() as skills:
                metadata = self._skill_stat(skills, name)
                if metadata is None or not stat.S_ISDIR(metadata.st_mode):
                    return None
                descriptor = self._skill_open_dir(skills, name)
                try:
                    manifest = self._skill_read_manifest(descriptor, name)
                finally:
                    os.close(descriptor)
        except (FileNotFoundError, UnsupportedEntryError):
            return None
        return manifest["publication_id"] if manifest else None

    # -- journals, recovery and retention -------------------------------

    def _skill_journal_ids(self) -> list[str]:
        try:
            with self._skill_area("journal") as journal:
                names = os.listdir(journal)
        except FileNotFoundError:
            return []
        return sorted(
            name[: -len(".json")]
            for name in names
            if name.endswith(".json")
            and _SKILL_PUBLICATION_ID.fullmatch(name[: -len(".json")])
        )

    def _skill_drop_journal(self, publication_id: str) -> None:
        with self._skill_area("journal", create=True) as journal:
            try:
                os.unlink(f"{publication_id}.json", dir_fd=journal)
            except FileNotFoundError:
                return
            os.fsync(journal)

    def _skill_drop_staging(self, publication_id: str) -> None:
        """Remove a publication's staging — never a displaced live version.

        After a RENAME_EXCHANGE the staging slot holds the version it
        replaced until that moves to ``retired``. A committed (finalized)
        slot whose bundle is not this publication's own is therefore
        retired, not deleted; a slot whose content cannot be identified is
        kept.
        """
        try:
            with self._skill_area("staging") as staging:
                if self._skill_stat(staging, publication_id) is None:
                    return
                with self._skill_area("staging", publication_id) as stage:
                    if not self._skill_retire_displaced(stage, publication_id):
                        return
                self._skill_remove_tree(staging, publication_id)
                os.fsync(staging)
        except FileNotFoundError:
            pass

    def _skill_retire_displaced(self, stage: int, publication_id: str) -> bool:
        """Retire a displaced version in a staging slot; whether it is empty
        of anything that must be kept (so the slot may be removed)."""
        if self._skill_stat(stage, "bundle") is None:
            return True
        raw = self._skill_read_small(stage, "meta.json", 4096)
        try:
            meta = json.loads(raw) if raw else {}
        except ValueError:
            return False
        if not isinstance(meta, dict):
            return False
        if not meta.get("finalized"):
            return True  # an upload that never reached its swap
        skill = meta.get("skill")
        if skill_name_violation(skill) is not None:
            return False
        if self._skill_staged_is(stage, publication_id, skill):
            return True  # this publication's own bundle: never swapped in
        self._skill_move_to_retired(
            stage, "bundle", skill, self._skill_new_retired_name()
        )
        return True

    def _skill_release_landed(self, publication_id: str) -> None:
        """Best effort: drop the staging of a publication that is live.

        The outcome is already known, so a failure here is never reported.
        A journal recovery has not reached yet still owns the slot (after an
        exchange it holds the previous version, bound for ``retired``).
        """
        try:
            if publication_id not in self._skill_journal_ids():
                self._skill_drop_staging(publication_id)
        except (FilesPolicyError, OSError):
            pass

    @staticmethod
    def _skill_new_retired_name() -> str:
        return f"{int(_now()):010d}-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _skill_occupant_name(publication_id: str) -> str:
        return f"{int(_now()):010d}-{uuid.uuid4().hex[:12]}-occupant-{publication_id}"

    def _skill_retired_listing(self, skill: str) -> list[str]:
        """Entries of ``retired/<skill>``; none when it is absent or is not a
        usable folder (an agent can put anything there)."""
        try:
            with self._skill_area("retired", skill) as retired:
                return os.listdir(retired)
        except TimeoutError:
            raise
        except (FilesPolicyError, OSError):
            return []

    def _skill_occupant_names(self, skill: str) -> list[str]:
        """Retired occupants of ``skill``, newest first."""
        return sorted(
            (
                name
                for name in self._skill_retired_listing(skill)
                if _SKILL_OCCUPANT_NAME.fullmatch(name)
            ),
            reverse=True,
        )

    def _skill_occupant_of(self, skill: str, publication_id: str) -> str | None:
        """The occupant retired when ``publication_id`` landed, if any."""
        for name in self._skill_occupant_names(skill):
            match = _SKILL_OCCUPANT_NAME.fullmatch(name)
            if match and match.group(2) == publication_id:
                return name
        return None

    def _skill_retired_exists(self, skill: str, retired_name: str) -> bool:
        try:
            with self._skill_area("retired", skill) as retired:
                return self._skill_stat(retired, retired_name) is not None
        except FileNotFoundError:
            return False

    def _skill_move_to_retired(
        self, parent: int, name: str, skill: str, retired_name: str
    ) -> None:
        with self._skill_area("retired", skill, create=True) as retired:
            os.rename(name, retired_name, src_dir_fd=parent, dst_dir_fd=retired)
            os.fsync(retired)
        os.fsync(parent)

    def _skill_recover_journals(self) -> None:
        """Finish or drop publications interrupted around their renames.

        Whether the swap happened is read from the STAGING slot, never from
        the live folder's manifest (which an agent can edit or break):

        * ``retiring`` (journaled two-rename fallback): the slot holds the
          NEW version until it lands. If the old version was already retired,
          the staged version moves in — an occupant that appeared on the live
          path in the gap is retired first, never the new version. If the
          old version is still live, nothing moved.
        * ``swapping`` (exchange or no-replace): the slot still holding THIS
          publication's bundle means nothing moved (the journal is dropped,
          the staging kept for a re-commit or the prune); anything else in it
          is the version the exchange replaced, which moves to ``retired``
          under the journal's name.
        * An empty slot: the staged version landed (or its predecessor was
          already retired).

        A record whose recovery fails (``retired/<skill>`` is not a folder,
        a rename is refused) keeps its journal for the next attempt and only
        blocks its own skill (:meth:`_skill_refuse_if_stuck`;
        ``fs_skill_status`` reports it). Discovery and every other skill
        carry on.
        """
        self._skill_recovery_errors = {}
        self._skill_drop_journal_temps()
        for publication_id in self._skill_journal_ids()[:SKILL_JOURNAL_RECOVERY_LIMIT]:
            try:
                with self._skill_area("journal") as journal:
                    raw = self._skill_read_small(
                        journal, f"{publication_id}.json", 64 * 1024
                    )
                record = json.loads(raw) if raw else {}
            except TimeoutError:
                raise
            except (FilesPolicyError, OSError, ValueError):
                record = {}
            record = record if isinstance(record, dict) else {}
            skill = record.get("skill")
            retired_name = record.get("retired_name")
            try:
                if skill_name_violation(skill) or not (
                    isinstance(retired_name, str)
                    and _SKILL_RETIRED_NAME.fullmatch(retired_name)
                ):
                    self._skill_drop_journal(publication_id)
                    continue
                self._skill_recover_record(publication_id, record)
            except TimeoutError:
                raise
            except (FilesPolicyError, OSError) as error:
                reason = getattr(error, "strerror", None) or str(error)
                if isinstance(skill, str) and skill_name_violation(skill) is None:
                    self._skill_recovery_errors[skill] = reason

    def _skill_recover_record(self, publication_id: str, record: dict) -> None:
        """Finish or drop one journaled publication (see above)."""
        skill, retired_name = record["skill"], record["retired_name"]
        landed = False
        try:
            with self._skill_area("staging", publication_id) as stage:
                if self._skill_stat(stage, "bundle") is None:
                    landed = True
                elif record.get("state") == "retiring":
                    with self._skills_root(create=True) as skills:
                        occupied = self._skill_stat(skills, skill) is not None
                        if not occupied or self._skill_retired_exists(
                            skill, retired_name
                        ):
                            self._skill_land_journaled(
                                stage, skills, skill, publication_id
                            )
                            landed = True
                elif not self._skill_staged_is(stage, publication_id, skill):
                    self._skill_move_to_retired(stage, "bundle", skill, retired_name)
                    landed = True
        except FileNotFoundError:
            pass
        self._skill_drop_journal(publication_id)
        if landed:
            self._skill_drop_staging(publication_id)

    def _skill_drop_journal_temps(self) -> None:
        """Remove journal records a helper died writing (never renamed in).

        Every helper action holds the workspace lock, so no live writer can
        own one; the record itself was either never written or was replaced.
        """
        try:
            with self._skill_area("journal") as journal:
                for name in os.listdir(journal):
                    if _SKILL_JOURNAL_TEMP.fullmatch(name):
                        os.unlink(name, dir_fd=journal)
        except FileNotFoundError:
            pass

    def _skill_staged_is(self, stage: int, publication_id: str, skill: str) -> bool:
        """Whether a staging slot's ``bundle`` is this publication's own.

        Anything else there — another publication's manifest, none, an
        unreadable one, a file — is a version an exchange displaced.
        """
        try:
            bundle = self._skill_open_dir(stage, "bundle")
        except (UnsupportedEntryError, OSError):
            return False
        try:
            manifest = self._skill_read_manifest(bundle, skill)
        finally:
            os.close(bundle)
        return manifest is not None and manifest["publication_id"] == publication_id

    def _skill_retired_names(self, skill: str) -> list[str]:
        return sorted(
            (
                name
                for name in self._skill_retired_listing(skill)
                if _SKILL_RETIRED_NAME.fullmatch(name)
            ),
            reverse=True,
        )

    def _skill_prune_retired(self, skill: str, keep: int = SKILL_RETIRED_KEEP) -> None:
        """Best effort: keep the newest versions and anything retired recently.

        Retired occupants have their own count (the newest ``keep``) and
        grace period, so versions retired later never push one out early.
        """
        try:
            groups = (
                self._skill_retired_names(skill),
                self._skill_occupant_names(skill),
            )
            with self._skill_area("retired", skill) as retired:
                for names in groups:
                    for index, name in enumerate(names):
                        if index < keep:
                            continue
                        if _now() - int(name[:10]) < SKILL_RETIRED_GRACE_SECONDS:
                            continue
                        self._skill_remove_tree(retired, name)
        except (FileNotFoundError, FilesPolicyError, TimeoutError, OSError):
            pass

    def _skill_prune_orphaned_retired(self) -> None:
        """Best effort: the retired versions of skills that no longer exist.

        A deleted skill is not published again, so the newest-two rule of
        :meth:`_skill_prune_retired` would keep its versions forever. Once
        the live folder is gone, every retired version and occupant past the
        grace period is removed, then the empty folder. A skill whose
        publication recovery is unfinished keeps its versions: one of them
        may be its only copy.
        """
        try:
            with self._skill_area("retired") as retired:
                skills = sorted(
                    name
                    for name in os.listdir(retired)
                    if skill_name_violation(name) is None
                    and self._skill_recovery_error(name) is None
                )
            try:
                with self._skills_root() as live:
                    orphans = [
                        name for name in skills if self._skill_stat(live, name) is None
                    ]
            except FileNotFoundError:
                orphans = skills
            for name in orphans:
                self._skill_prune_retired(name, keep=0)
                with self._skill_area("retired") as retired:
                    try:
                        os.rmdir(name, dir_fd=retired)
                    except OSError:
                        continue  # still within its grace period
                    os.fsync(retired)
        except (FileNotFoundError, FilesPolicyError, TimeoutError, OSError):
            pass

    def _skill_prune_stale_staging(self) -> None:
        """Best effort: remove staging abandoned for over an hour."""
        journals = set(self._skill_journal_ids())
        try:
            with self._skill_area("staging") as staging:
                for name in os.listdir(staging):
                    if name in journals:
                        continue
                    created = None
                    if _SKILL_PUBLICATION_ID.fullmatch(name):
                        try:
                            with self._skill_area("staging", name) as stage:
                                raw = self._skill_read_small(stage, "meta.json", 4096)
                            created = json.loads(raw).get("created") if raw else None
                        except (FilesPolicyError, OSError, ValueError, AttributeError):
                            created = None
                    if not isinstance(created, (int, float)):
                        created = os.stat(
                            name, dir_fd=staging, follow_symlinks=False
                        ).st_mtime
                    if _now() - created >= SKILL_STAGING_STALE_SECONDS:
                        if _SKILL_PUBLICATION_ID.fullmatch(name):
                            self._skill_drop_staging(name)
                        else:
                            self._skill_remove_tree(staging, name)
        except (FileNotFoundError, FilesPolicyError, TimeoutError, OSError):
            pass

    # -- staging --------------------------------------------------------

    def _skill_open_parent(self, bundle: int, path: str, *, create: bool) -> int:
        """Descriptor of ``path``'s parent inside a staged bundle."""
        parent = os.dup(bundle)
        try:
            for part in path.split("/")[:-1]:
                if create:
                    try:
                        os.mkdir(part, mode=0o755, dir_fd=parent)
                    except FileExistsError:
                        pass
                child = self._skill_open_dir(parent, part)
                os.close(parent)
                parent = child
        except BaseException:
            os.close(parent)
            raise
        return parent

    def _skill_stage_begin(self, params: dict) -> dict:
        self._skill_begin_action()
        publication_id = _skill_publication_id(params)
        name = _skill_name(params)
        if publication_id in self._skill_journal_ids():
            raise SkillBundleConflictError(
                "This publication is already being committed",
                "skill_publication_committing",
            )
        self._skill_refuse_if_stuck(name)
        self._skill_tidy(name)
        self._skill_drop_staging(publication_id)
        with self._skill_area("staging", create=True) as staging:
            if self._skill_stat(staging, publication_id) is not None:
                raise SkillBundleConflictError(
                    "This publication's staging holds a version that could not "
                    "be identified; retry with a new publication",
                    "skill_publication_committing",
                )
            os.mkdir(publication_id, mode=0o755, dir_fd=staging)
            with self._skill_area("staging", publication_id) as stage:
                os.mkdir("bundle", mode=0o755, dir_fd=stage)
                self._skill_write_json(
                    stage, "meta.json", {"skill": name, "created": _now()}
                )
            os.fsync(staging)
        return {"publication_id": publication_id, "skill": name, "staged": True}

    def _skill_stage_put(self, params: dict) -> dict:
        self._skill_begin_action()
        publication_id = _skill_publication_id(params)
        entries = params.get("files")
        if not isinstance(entries, list) or not entries:
            raise FilesPolicyError("files must be a non-empty list")
        if len(entries) > SKILL_STAGE_PUT_MAX_FILES:
            raise FilesPolicyError("Too many files in one stage request")
        chunks: list[tuple[str, int, bytes]] = []
        total = 0
        for entry in entries:
            if not isinstance(entry, dict):
                raise FilesPolicyError("files entries must be objects")
            path = entry.get("path")
            violation = skill_bundle_path_violation(path)
            if violation:
                raise FilesPolicyError(f"Invalid bundle path {path!r}: {violation}")
            offset = entry.get("offset", 0)
            if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                raise FilesPolicyError("offset must be a non-negative integer")
            encoded = entry.get("data_base64", "")
            if not isinstance(encoded, str):
                raise FilesPolicyError("data_base64 must be a string")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as error:
                raise FilesPolicyError("data_base64 is not valid base64") from error
            total += len(data)
            if total > SKILL_STAGE_PUT_MAX_BYTES:
                raise FilesPolicyError("Stage request exceeds the size limit")
            if offset + len(data) > SKILL_BUNDLE_MAX_FILE_BYTES:
                raise FilesPolicyError(f"{path} exceeds the per-file limit")
            chunks.append((path, offset, data))
        written = []
        with self._skill_area("staging", publication_id, "bundle") as bundle:
            for path, offset, data in chunks:
                parent = self._skill_open_parent(bundle, path, create=True)
                try:
                    self._tick()
                    leaf = path.split("/")[-1]
                    flags = os.O_WRONLY | _FILE_FLAGS
                    if offset == 0:
                        flags |= os.O_CREAT | os.O_TRUNC
                    descriptor = os.open(leaf, flags, 0o644, dir_fd=parent)
                    try:
                        metadata = self._validate_fd(descriptor, directory=False)
                        if offset and metadata.st_size != offset:
                            raise FilesPolicyError(
                                f"offset mismatch for {path}: a chunk was dropped "
                                "or reordered"
                            )
                        if data and os.pwrite(descriptor, data, offset) != len(data):
                            raise OSError("Incomplete file write")
                        os.fsync(descriptor)
                        size = os.fstat(descriptor).st_size
                    finally:
                        os.close(descriptor)
                finally:
                    os.close(parent)
                written.append({"path": path, "size": size})
        return {"publication_id": publication_id, "written": written}

    def _skill_abort(self, params: dict) -> dict:
        self._skill_begin_action()
        publication_id = _skill_publication_id(params)
        skill_name = params.get("skill_name")
        if (
            skill_name_violation(skill_name) is None
            and self._skill_live_publication(skill_name) == publication_id
        ):
            # Recovery (run above) completed this publication: it is live,
            # and nothing here may remove it.
            self._skill_release_landed(publication_id)
            return {
                "publication_id": publication_id,
                "aborted": False,
                "committed": True,
                "occupant_retired_as": self._skill_occupant_of(
                    skill_name, publication_id
                ),
            }
        if publication_id in self._skill_journal_ids():
            raise SkillBundleConflictError(
                "This publication is already being committed",
                "skill_publication_committing",
            )
        self._skill_drop_staging(publication_id)
        return {"publication_id": publication_id, "aborted": True}

    def _skill_retire(self, params: dict) -> dict:
        """Take a skill's live folder out of ``.claude/skills`` (skill delete).

        One rename moves the entry into ``retired/<skill>/`` without walking
        it, so a folder Files cannot delete (protected names such as ``.git``
        or ``.env``, links, more entries than one Files call may visit) goes
        as a whole, like a version an install replaces. It is kept for the
        retired grace period, then pruned. A missing folder is a 404.
        """
        self._skill_begin_action()
        name = _skill_name(params)
        self._skill_refuse_if_stuck(name)
        retired_name = self._skill_new_retired_name()
        with self._skills_root() as skills:
            if self._skill_stat(skills, name) is None:
                raise FileNotFoundError(name)
            self._skill_move_to_retired(skills, name, name, retired_name)
        self._skill_tidy(name)
        return {"skill": name, "retired_as": retired_name}

    def _skill_status(self, params: dict) -> dict:
        self._skill_begin_action()
        name = _skill_name(params)
        self._skill_tidy()
        try:
            with self._skills_root() as skills:
                live = self._skill_live_state(skills, name)
        except FileNotFoundError:
            live = {"exists": False, "digest": None}
        result = {
            "skill": name,
            "live": live,
            "retired": self._skill_retired_names(name)[:10],
            "occupants": self._skill_occupant_names(name)[:10],
        }
        recovery_error = self._skill_recovery_error(name)
        if recovery_error is not None:
            result["recovery_error"] = recovery_error
        if params.get("publication_id") is not None:
            publication_id = _skill_publication_id(params)
            result["publication_id"] = publication_id
            result["committed"] = live.get("publication_id") == publication_id
            result["occupant_retired_as"] = self._skill_occupant_of(
                name, publication_id
            )
            try:
                with self._skill_area("staging", publication_id) as stage:
                    result["staged"] = self._skill_stat(stage, "bundle") is not None
            except FileNotFoundError:
                result["staged"] = False
        return result

    # -- commit ----------------------------------------------------------

    def _skill_verify_staged(self, bundle: int, files: list[dict]) -> None:
        """The staged tree must equal the manifest exactly (no extras)."""
        staged, unsupported = self._skill_listing(bundle, live_root=True)
        if unsupported:
            raise FilesPolicyError(
                "The staged bundle holds unsupported entries: "
                + ", ".join(unsupported[:5])
            )
        expected = {entry["path"]: entry for entry in files}
        actual = {entry["path"]: entry for entry in staged}
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        if missing or extra:
            raise FilesPolicyError(
                "The staged bundle does not match its manifest "
                f"(missing {missing[:5]}, unexpected {extra[:5]})"
            )
        for path, entry in expected.items():
            if (actual[path]["size"], actual[path]["sha256"]) != (
                entry["size"],
                entry["sha256"],
            ):
                raise FilesPolicyError(f"Staged {path} does not match its manifest")
        skill_md = self._skill_read_small(
            bundle, "SKILL.md", SKILL_BUNDLE_MAX_FILE_BYTES
        )
        try:
            text = (skill_md or b"").decode("utf-8")
        except UnicodeDecodeError as error:
            raise FilesPolicyError("The staged SKILL.md is not valid UTF-8") from error
        if not text.strip():
            raise FilesPolicyError("The staged SKILL.md is empty")

    def _skill_finalize_staged(
        self,
        bundle: int,
        files: list[dict],
        manifest: dict,
        live_params: bytes | None,
    ) -> None:
        """Apply modes, write the manifest and carry the live params.json."""
        # Refused before anything changes: a manifest the reader cannot read
        # would make a landed publication look like an unmanaged folder.
        payload = skill_bundle_manifest_bytes(manifest)
        for entry in files:
            parent = self._skill_open_parent(bundle, entry["path"], create=False)
            try:
                leaf = entry["path"].split("/")[-1]
                descriptor = os.open(leaf, os.O_RDONLY | _FILE_FLAGS, dir_fd=parent)
                try:
                    self._validate_fd(descriptor, directory=False)
                    os.fchmod(descriptor, entry["mode"])
                finally:
                    os.close(descriptor)
            finally:
                os.close(parent)
        for reserved in (SKILL_BUNDLE_MANIFEST, "params.json"):
            try:
                os.unlink(reserved, dir_fd=bundle)
            except FileNotFoundError:
                pass
        self._skill_write_new(bundle, SKILL_BUNDLE_MANIFEST, payload)
        if live_params is not None:
            self._skill_write_new(bundle, "params.json", live_params)
        os.fsync(bundle)

    def _skill_live_params(self, skills: int, name: str) -> bytes | None:
        metadata = self._skill_stat(skills, name)
        if metadata is None or not stat.S_ISDIR(metadata.st_mode):
            return None
        live = self._skill_open_dir(skills, name)
        try:
            try:
                return self._skill_read_small(
                    live, "params.json", SKILL_PARAMS_MAX_BYTES
                )
            except OSError as error:
                if error.errno not in {errno.ELOOP, errno.ENOTDIR, errno.EISDIR}:
                    raise
                raise UnsupportedEntryError("params.json is not a file") from error
        except (UnsupportedEntryError, FilesPolicyError) as error:
            raise SkillBundleConflictError(
                "The live skill's params.json is not a regular file within its "
                "size limit, so its values cannot be carried over. Fix or delete "
                "it, then publish again.",
                "skill_params_unsupported",
            ) from error
        finally:
            os.close(live)

    def _skill_land_journaled(
        self, stage: int, skills: int, name: str, publication_id: str
    ) -> str | None:
        """Second rename of the journaled fallback (commit and recovery).

        The old version is already retired. If something occupied the live
        path in between, it is retired too (once) and the staged version
        moves in; the occupant's retired name is returned, else ``None``.
        """
        occupant = None
        try:
            os.rename("bundle", name, src_dir_fd=stage, dst_dir_fd=skills)
        except OSError as error:
            if error.errno not in _SKILL_OCCUPIED_ERRNOS:
                raise
            if self._skill_stat(skills, name) is None:
                raise
            occupant = self._skill_occupant_name(publication_id)
            self._skill_move_to_retired(skills, name, name, occupant)
            os.rename("bundle", name, src_dir_fd=stage, dst_dir_fd=skills)
        os.fsync(skills)
        return occupant

    def _skill_swap(
        self, stage: int, skills: int, record: dict, *, live_exists: bool
    ) -> tuple[str, str | None]:
        """Swap the staged directory in; never a partially active version.

        Returns the swap kind and the retired name of an occupant the
        journaled fallback found on the live path, if any.
        """
        name, retired_name = record["skill"], record["retired_name"]
        if not live_exists:
            try:
                _renameat2(stage, "bundle", skills, name, RENAME_NOREPLACE)
                swap = "noreplace"
            except FileExistsError as error:
                raise SkillBundleConflictError(
                    "A skill folder appeared while publishing; nothing changed",
                    "skill_bundle_conflict",
                ) from error
            except UnsupportedFilesystemError:
                if self._skill_stat(skills, name) is not None:
                    raise SkillBundleConflictError(
                        "A skill folder appeared while publishing; nothing changed",
                        "skill_bundle_conflict",
                    ) from None
                try:
                    os.rename("bundle", name, src_dir_fd=stage, dst_dir_fd=skills)
                except OSError as error:
                    if error.errno not in _SKILL_OCCUPIED_ERRNOS:
                        raise
                    # The rename refused an occupied path: nothing moved.
                    raise SkillBundleConflictError(
                        "A skill folder appeared while publishing; nothing changed",
                        "skill_bundle_conflict",
                    ) from error
                swap = "rename"
            os.fsync(skills)
            os.fsync(stage)
            return swap, None
        try:
            _renameat2(stage, "bundle", skills, name, RENAME_EXCHANGE)
        except UnsupportedFilesystemError:
            # Journaled two-rename fallback: the live folder is briefly
            # missing between the renames; journal recovery finishes it.
            record["state"] = "retiring"
            with self._skill_area("journal", create=True) as journal:
                self._skill_write_json(
                    journal, f"{record['publication_id']}.json", record
                )
            self._skill_move_to_retired(skills, name, name, retired_name)
            occupant = self._skill_land_journaled(
                stage, skills, name, record["publication_id"]
            )
            return "journaled", occupant
        os.fsync(skills)
        os.fsync(stage)
        self._skill_move_to_retired(stage, "bundle", name, retired_name)
        return "exchange", None

    def _skill_commit(self, params: dict) -> dict:
        self._skill_begin_action()
        publication_id = _skill_publication_id(params)
        name = _skill_name(params)
        try:
            self._skill_refuse_if_stuck(name)
            mode = params.get("mode", "update")
            if mode not in _SKILL_PUBLISH_MODES:
                raise FilesPolicyError("mode must be update, ensure or create")
            files, bundle_sha, source = _skill_manifest_files(params.get("manifest"))
            expected = params.get("expected_live_digest")
            if (
                expected is not None
                and expected != "none"
                and not (
                    isinstance(expected, str) and _SKILL_SHA256.fullmatch(expected)
                )
            ):
                raise FilesPolicyError(
                    "expected_live_digest must be 'none' or a sha256"
                )
            if mode == "update" and expected is None:
                raise FilesPolicyError(
                    "update publications require expected_live_digest"
                )
            return self._skill_commit_checked(
                publication_id, name, mode, files, bundle_sha, source, expected
            )
        except (SkillBundleConflictError, FilesPolicyError) as error:
            # A definitive refusal before the swap took effect: drop this
            # publication's staging now (up to the bundle cap) instead of
            # leaving it to the hourly prune. A journaled swap that already
            # started retiring the old version is left to recovery.
            if not self._skill_journal_retiring(publication_id):
                try:
                    self._skill_drop_journal(publication_id)
                    self._skill_drop_staging(publication_id)
                except (FilesPolicyError, OSError):
                    pass  # the refusal stands; the prune retries the cleanup
            if isinstance(error, FilesPolicyError):
                # Tagged, so the backend knows nothing was swapped in.
                raise SkillPublicationRefusedError(str(error)) from error
            raise

    def _skill_journal_retiring(self, publication_id: str) -> bool:
        try:
            with self._skill_area("journal") as journal:
                raw = self._skill_read_small(
                    journal, f"{publication_id}.json", 64 * 1024
                )
            record = json.loads(raw) if raw else {}
        except (FileNotFoundError, FilesPolicyError, OSError, ValueError):
            return False
        return isinstance(record, dict) and record.get("state") == "retiring"

    def _skill_commit_checked(
        self,
        publication_id: str,
        name: str,
        mode: str,
        files: list[dict],
        bundle_sha: str,
        source: dict,
        expected: str | None,
    ) -> dict:
        with self._skills_root(create=True) as skills:
            live = self._skill_live_state(skills, name)
            if live.get("publication_id") == publication_id:
                self._skill_release_landed(publication_id)
                return {
                    "status": "published",
                    "skill": name,
                    "publication_id": publication_id,
                    "bundle_sha256": live.get("bundle_sha256"),
                    "published_at": live.get("published_at"),
                    "already_committed": True,
                }
            if live["exists"] and mode == "ensure":
                self._skill_drop_staging(publication_id)
                return {"status": "exists", "skill": name, "live": live}
            if live["exists"] and mode == "create":
                raise SkillBundleConflictError(
                    f"A skill folder named {name!r} already exists; nothing was "
                    "overwritten",
                    "skill_exists",
                    live["digest"],
                )
            current = live["digest"] if live["exists"] else "none"
            if expected is not None and current != expected:
                raise SkillBundleConflictError(
                    "The skill folder changed since it was inspected; nothing "
                    "was published",
                    "skill_bundle_conflict",
                    current,
                )
            live_params = self._skill_live_params(skills, name)
            published_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            record = {
                "format": SKILL_BUNDLE_FORMAT,
                "publication_id": publication_id,
                "skill": name,
                "retired_name": self._skill_new_retired_name(),
                "state": "swapping",
                "created": _now(),
            }
            with self._skill_area("staging", publication_id) as stage:
                raw_meta = self._skill_read_small(stage, "meta.json", 4096)
                try:
                    meta = json.loads(raw_meta) if raw_meta else {}
                except ValueError:
                    meta = {}
                if not isinstance(meta, dict) or meta.get("skill") != name:
                    raise FilesPolicyError(
                        "The staged publication is for another skill"
                    )
                bundle = self._skill_open_dir(stage, "bundle")
                try:
                    self._skill_verify_staged(bundle, files)
                    self._skill_finalize_staged(
                        bundle,
                        files,
                        {
                            "format": SKILL_BUNDLE_FORMAT,
                            "skill": name,
                            "bundle_sha256": bundle_sha,
                            "publication_id": publication_id,
                            "published_at": published_at,
                            "source": source,
                            "files": files,
                        },
                        live_params,
                    )
                finally:
                    os.close(bundle)
                # From here the slot may hold a displaced live version (after
                # an exchange): staging drops retire it instead of deleting.
                self._skill_write_json(stage, "meta.json", {**meta, "finalized": True})
                with self._skill_area("journal", create=True) as journal:
                    self._skill_write_json(journal, f"{publication_id}.json", record)
                try:
                    swap, occupant = self._skill_swap(
                        stage, skills, record, live_exists=live["exists"]
                    )
                except (SkillBundleConflictError, TimeoutError):
                    raise
                except (FilesPolicyError, OSError) as error:
                    # After the journal: the swap may have happened, or the
                    # next recovery may complete it. Not a refusal.
                    raise SkillCommitUncertainError(
                        "The skill folder swap did not complete cleanly; read "
                        "the publication state back before retrying"
                    ) from error
        try:
            self._skill_drop_journal(publication_id)
            self._skill_drop_staging(publication_id)
        except (FilesPolicyError, OSError):
            pass  # published; the next recovery drops the leftovers
        self._skill_tidy(name)
        if not live["exists"]:
            previous = "none"
        elif not live.get("managed"):
            previous = "unmanaged"
        else:
            previous = "modified" if live.get("modified") else "managed"
        if occupant is not None and previous in ("managed", "none"):
            previous = "modified"  # content nobody inspected was replaced
        return {
            "status": "published",
            "skill": name,
            "publication_id": publication_id,
            "bundle_sha256": bundle_sha,
            "published_at": published_at,
            "swap": swap,
            "previous": {
                "state": previous,
                "bundle_sha256": live.get("bundle_sha256"),
                "retired_as": record["retired_name"] if live["exists"] else None,
                "unverified": bool(live.get("unverified")),
                "occupant_retired_as": occupant,
            },
        }


class SecureWorkspace(_SkillBundleActions):
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
        self.entry_limit = MAX_ENTRIES
        self._skill_recovery_errors = {}

    def __enter__(self) -> "SecureWorkspace":
        return self

    def __exit__(self, *unused: object) -> None:
        os.close(self.root_fd)

    def _tick(self, count: int = 0) -> None:
        self.entries += count
        if self.entries > self.entry_limit:
            raise EntryLimitError(
                f"Workspace operation exceeds the {self.entry_limit}-entry limit; select a smaller subfolder"
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
            raise UnsupportedEntryError(
                "Mounted paths are not accessible through Files"
            )
        if directory:
            if not stat.S_ISDIR(metadata.st_mode):
                raise UnsupportedEntryError("Not a directory")
        elif not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise UnsupportedEntryError("Only regular, single-link files are supported")
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
        self._assert_unchanged(initial, self._validate_fd(descriptor, directory=False))
        return b"".join(chunks)

    def _digest_fd(
        self,
        descriptor: int,
        limit: int,
        initial: os.stat_result | None = None,
    ) -> tuple[str | None, int]:
        """SHA-256 of a validated regular file, or ``(None, size)`` above limit.

        ``initial`` is the caller's own ``_validate_fd`` result for this
        descriptor, when it already has one; otherwise it is validated here.
        """
        if initial is None:
            initial = self._validate_fd(descriptor, directory=False)
        if initial.st_size > limit:
            return None, initial.st_size
        digest = hashlib.sha256()
        total = 0
        offset = 0
        while True:
            self._tick()
            data = os.pread(descriptor, 65536, offset)
            if not data:
                break
            offset += len(data)
            total += len(data)
            if total > limit:
                return None, total
            digest.update(data)
        self._assert_unchanged(initial, self._validate_fd(descriptor, directory=False))
        return digest.hexdigest(), total

    @staticmethod
    def _assert_unchanged(initial: os.stat_result, final: os.stat_result) -> None:
        """Refuse a read whose file changed size or times meanwhile."""
        if (initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) != (
            final.st_size,
            final.st_mtime_ns,
            final.st_ctime_ns,
        ):
            raise FileChangedError()

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
        lazy = params.get("lazy")
        if lazy is not None and not isinstance(lazy, bool):
            raise FilesPolicyError("Invalid tree request: lazy must be a boolean")
        if lazy:
            return self._lazy_tree(params)
        parts = self._parts(params.get("subfolder", "") or "", root_allowed=True)
        skipped_entries = 0
        # Browsing describes supported entries without following links. A local
        # race or unreadable child should not hide every healthy sibling. Other
        # operations still reject these entries, including ZIP and explicit roots.
        unavailable_child_errors = {
            errno.ENOENT,
            errno.ELOOP,
            errno.ENOTDIR,
            errno.EACCES,
            errno.EPERM,
        }

        def visit(descriptor: int, current: tuple[str, ...], depth: int) -> dict:
            nonlocal skipped_entries
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
                    child = None
                    try:
                        child_metadata = os.stat(
                            name, dir_fd=descriptor, follow_symlinks=False
                        )
                        directory = stat.S_ISDIR(child_metadata.st_mode)
                        if not directory and (
                            not stat.S_ISREG(child_metadata.st_mode)
                            or child_metadata.st_nlink != 1
                        ):
                            skipped_entries += 1
                            continue
                        child = os.open(
                            name,
                            (
                                _DIRECTORY_FLAGS
                                if directory
                                else os.O_RDONLY | _FILE_FLAGS
                            ),
                            dir_fd=descriptor,
                        )
                        if directory:
                            children.append(visit(child, child_parts, depth + 1))
                        else:
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
                    except UnsupportedEntryError:
                        skipped_entries += 1
                    except OSError as error:
                        if error.errno not in unavailable_child_errors:
                            raise
                        skipped_entries += 1
                    finally:
                        if child is not None:
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

        previous_limit = self.entry_limit
        self.entry_limit = MAX_TREE_ENTRIES
        try:
            with self._directory(parts) as descriptor:
                result = {**visit(descriptor, parts, 0), "root": "/workspace"}
                # A root replacement or deadline cannot become an omitted child.
                self._tick()
                if skipped_entries:
                    result["skipped_entries"] = skipped_entries
                return result
        finally:
            self.entry_limit = previous_limit

    def _lazy_tree(self, params: dict) -> dict:
        """Breadth-first tree that never fails because a workspace is large.

        Nodes have the strict tree's shapes. A folder whose children were
        not listed (entry, byte or soft time budget, or depth) comes back
        with ``children: []``, ``size: 0`` and ``children_loaded: false``; a
        folder with more listable children than LAZY_DIRECTORY_ENTRIES lists
        the first ones in tree order (folders first, then case-insensitive
        name) with ``truncated: true`` and ``total_entries``. The root carries
        ``partial: true`` when any folder is unloaded or truncated.

        The requested folder is always listed, however large it is: a scan
        still running after LAZY_SCAN_SECONDS lists the first names it found
        (``truncated`` without ``total_entries``), and checking its entries
        stops at the soft budget (``truncated``).

        Every other failure is the strict tree's: an invalid, protected or
        missing subfolder, a root replacement, mount identity or policy
        violations, systemic child errors and the hard deadline. Unsupported
        entries (links, special files, hard-linked files, names Files cannot
        address, unavailable children) are skipped and counted in
        ``skipped_entries``, for the folders that are listed.
        """
        parts = self._parts(params.get("subfolder", "") or "", root_allowed=True)
        started = _lazy_clock()
        soft_deadline = started + LAZY_TREE_SECONDS
        scan_deadline = min(started + LAZY_SCAN_SECONDS, soft_deadline)
        previous_limit = self.entry_limit
        # Entry counts never fail a lazy listing; its own budgets decide what
        # is listed. ``_tick`` still enforces the deadline and root identity.
        self.entry_limit = sys.maxsize
        try:
            with self._directory(parts) as descriptor:
                root = self._lazy_folder_node(
                    parts[-1] if parts else "workspace",
                    "/".join(parts),
                    self._validate_fd(descriptor, directory=True),
                )
                state = {
                    "entries": 0,
                    "bytes": _lazy_node_bytes(root["name"], root["path"]),
                    "skipped": 0,
                    "partial": False,
                    "soft_deadline": soft_deadline,
                    "scan_deadline": scan_deadline,
                }
                queue = deque([(root, (), None, 0)])
                while queue:
                    node, relative, identity, depth = queue.popleft()
                    subfolders = self._lazy_expand(
                        descriptor, parts, node, relative, identity, depth, state
                    )
                    if subfolders is None:
                        node["children_loaded"] = False
                        state["partial"] = True
                        continue
                    for child, child_identity in subfolders:
                        child_relative = (*relative, child["name"])
                        queue.append((child, child_relative, child_identity, depth + 1))
                _lazy_folder_size(root)
                result = {**root, "root": "/workspace"}
                # A root replacement or deadline cannot become an omitted child.
                self._tick()
                if state["skipped"]:
                    result["skipped_entries"] = state["skipped"]
                if state["partial"]:
                    result["partial"] = True
                return result
        finally:
            self.entry_limit = previous_limit

    @staticmethod
    def _lazy_folder_node(name: str, path: str, metadata: os.stat_result) -> dict:
        return {
            "name": name,
            "path": path,
            "type": "folder",
            "size": 0,
            "modified": datetime.fromtimestamp(
                metadata.st_mtime, timezone.utc
            ).isoformat(),
            "children": [],
        }

    def _lazy_expand(
        self,
        base: int,
        parts: tuple[str, ...],
        node: dict,
        relative: tuple[str, ...],
        identity: tuple[int, int] | None,
        depth: int,
        state: dict,
    ) -> list[tuple[dict, tuple[int, int]]] | None:
        """List ``node``'s children and return its subfolders to expand.

        ``None`` leaves the folder unloaded. The requested folder (no
        ``relative`` parts) is always listed.
        """
        if not relative:
            return self._lazy_list(base, parts, node, state, requested=True)
        if (
            depth >= TREE_DEPTH
            or state["entries"] >= LAZY_TREE_ENTRIES
            or state["bytes"] >= LAZY_TREE_BYTES
            or _lazy_clock() >= state["soft_deadline"]
        ):
            return None
        try:
            with self._lazy_reopen(base, relative, identity) as descriptor:
                return self._lazy_list(
                    descriptor, (*parts, *relative), node, state, requested=False
                )
        except (_LazyFolderUnavailable, UnsupportedEntryError):
            return None
        except OSError as error:
            if error.errno not in _TREE_UNAVAILABLE_ERRNOS:
                raise
            return None

    @contextmanager
    def _lazy_reopen(
        self, base: int, relative: tuple[str, ...], identity: tuple[int, int]
    ):
        """Reopen a folder listed earlier below ``base``, never following links.

        Raises :class:`_LazyFolderUnavailable` when the path names another
        folder now; the listing then leaves it unloaded.
        """
        descriptor = os.dup(base)
        try:
            for part in relative:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                try:
                    self._validate_fd(child, directory=True)
                except BaseException:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
            metadata = os.fstat(descriptor)
            if (metadata.st_dev, metadata.st_ino) != identity:
                raise _LazyFolderUnavailable()
            self._tick()
            yield descriptor
        finally:
            os.close(descriptor)

    def _lazy_list(
        self,
        descriptor: int,
        current: tuple[str, ...],
        node: dict,
        state: dict,
        *,
        requested: bool,
    ) -> list[tuple[dict, tuple[int, int]]] | None:
        """Fill ``node`` with the first children of ``descriptor``.

        Returns the listed subfolders with their identities, or ``None`` when
        the folder must stay unloaded: the soft budget passed, or its listing
        does not fit the response's remaining entry or byte budget.

        The requested folder is always listed. Its scan stops at the scan
        deadline and lists the first names found (``truncated`` without
        ``total_entries``); the soft budget and the byte budget stop checking
        its entries (``truncated``).
        """
        soft_deadline = state["soft_deadline"]
        limit = (
            min(LAZY_DIRECTORY_ENTRIES, LAZY_TREE_ENTRIES)
            if requested
            else LAZY_DIRECTORY_ENTRIES
        )
        folders, files, skipped, scanned = self._lazy_scan(
            descriptor,
            current,
            limit,
            state["scan_deadline"] if requested else soft_deadline,
        )
        if not scanned and not requested:
            return None
        total = folders.count + files.count
        if not requested and min(total, limit) > LAZY_TREE_ENTRIES - state["entries"]:
            return None
        room = LAZY_TREE_BYTES - state["bytes"]
        used = 0
        invalid = 0
        children: list[dict] = []
        subfolders: list[tuple[dict, tuple[int, int]]] = []
        # Complete only when every entry was scanned and every one listed.
        complete = scanned
        candidates = ((folders.first(), folders.count), (files.first(), files.count))
        for names, seen in candidates:
            for name in names:
                if len(children) >= limit:
                    complete = False
                    break
                if _lazy_clock() >= soft_deadline:
                    if not requested:
                        return None
                    complete = False
                    break
                self._tick()
                listed = self._lazy_child(descriptor, current, name)
                if listed is None:
                    invalid += 1
                    continue
                child, identity = listed
                cost = _lazy_node_bytes(child["name"], child["path"])
                if used + cost > room:
                    if not requested:
                        return None
                    complete = False
                    break
                used += cost
                children.append(child)
                if identity is not None:
                    subfolders.append((child, identity))
            else:
                if len(names) == seen:
                    continue
                # Names past the retained candidates were never checked, and
                # they come before the next group in tree order.
                complete = False
            break
        node["children"] = children
        if not complete:
            node["truncated"] = True
            if scanned:
                node["total_entries"] = total - invalid
            state["partial"] = True
        state["entries"] += len(children)
        state["bytes"] += used
        state["skipped"] += skipped + invalid
        return subfolders

    def _lazy_scan(
        self,
        descriptor: int,
        current: tuple[str, ...],
        keep: int,
        deadline: float,
    ) -> tuple[_FirstNames, _FirstNames, int, bool]:
        """Listable folder and file names of ``descriptor``, the skipped
        count, and whether every entry was scanned before ``deadline``.

        Names are typed from directory entries (no stat per name) and
        checked against the Files rules by their own name only
        (:class:`_ChildRules`). Hidden, ignored and protected names are left
        out uncounted, like the strict tree. Links, special files, vanished
        entries and names Files cannot address are skipped and counted.
        """
        rules = _ChildRules(current)
        folders, files, skipped = _FirstNames(keep), _FirstNames(keep), 0
        with os.scandir(descriptor) as entries:
            for index, entry in enumerate(entries):
                if index % _LAZY_SCAN_CHECK_EVERY == 0:
                    self._tick()
                    if _lazy_clock() >= deadline:
                        return folders, files, skipped, False
                name = entry.name
                if name.startswith(".") or name in _TREE_IGNORED_NAMES:
                    continue
                if rules.protected(name):
                    continue
                if not rules.addressable(name):
                    skipped += 1
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        folders.add(name)
                    elif entry.is_file(follow_symlinks=False):
                        files.add(name)
                    else:
                        skipped += 1  # a link, a special file or a vanished name
                except OSError as error:
                    if error.errno not in _TREE_UNAVAILABLE_ERRNOS:
                        raise
                    skipped += 1
        return folders, files, skipped, True

    def _lazy_child(
        self, descriptor: int, current: tuple[str, ...], name: str
    ) -> tuple[dict, tuple[int, int] | None] | None:
        """One validated child node (with a folder's identity), or ``None``
        when it is outside the Files boundary or unavailable (strict rules)."""
        child = None
        try:
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            directory = stat.S_ISDIR(metadata.st_mode)
            if not directory and (
                not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
            ):
                return None
            child = os.open(
                name,
                _DIRECTORY_FLAGS if directory else os.O_RDONLY | _FILE_FLAGS,
                dir_fd=descriptor,
            )
            checked = self._validate_fd(child, directory=directory)
        except UnsupportedEntryError:
            return None
        except OSError as error:
            if error.errno not in _TREE_UNAVAILABLE_ERRNOS:
                raise
            return None
        finally:
            if child is not None:
                os.close(child)
        path = "/".join((*current, name))
        if directory:
            return self._lazy_folder_node(name, path, checked), (
                checked.st_dev,
                checked.st_ino,
            )
        return {
            "name": name,
            "path": path,
            "type": "file",
            "size": checked.st_size,
            "file_kind": _classify_file(name),
            "modified": datetime.fromtimestamp(
                checked.st_mtime, timezone.utc
            ).isoformat(),
        }, None

    def _read(self, params: dict) -> dict:
        relative_path = params.get("path", "")
        with self._file(relative_path) as (descriptor, metadata):
            kind = _classify_file(relative_path)
            result = {
                "path": relative_path,
                "content": None,
                "size": metadata.st_size,
                "file_kind": kind,
            }
            if kind == "text":
                raw = self._read_fd(descriptor, MAX_READ_BYTES)
                result["content"] = raw.decode(errors="replace")
                # Revision of the RAW bytes; the decoded text alone cannot
                # reproduce them when they are not valid UTF-8.
                result["sha256"] = hashlib.sha256(raw).hexdigest()
                try:
                    raw.decode("utf-8")
                    result["utf8_valid"] = True
                except UnicodeDecodeError:
                    result["utf8_valid"] = False
            else:
                # The revision of a non-text file is best-effort: one being
                # written right now still returns its metadata (sha256 None,
                # as above MAX_HASH_BYTES); a later read carries the hash.
                # fs_hash and fs_write_revision stay strict.
                try:
                    digest, _size = self._digest_fd(descriptor, MAX_HASH_BYTES)
                except FileChangedError:
                    digest = None
                result["sha256"] = digest
            return result

    def _hash(self, params: dict) -> dict:
        """Existence, type, size and SHA-256 of a path (no 404 for absence)."""
        relative_path = params.get("path", "")
        parts = self._parts(relative_path)
        try:
            with self._directory(parts[:-1]) as parent:
                try:
                    metadata = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    return {"path": relative_path, "exists": False}
                if stat.S_ISDIR(metadata.st_mode):
                    child = os.open(parts[-1], _DIRECTORY_FLAGS, dir_fd=parent)
                    try:
                        self._validate_fd(child, directory=True)
                    finally:
                        os.close(child)
                    return {
                        "path": relative_path,
                        "exists": True,
                        "type": "directory",
                        "size": 0,
                        "sha256": None,
                    }
                descriptor = os.open(
                    parts[-1], os.O_RDONLY | _FILE_FLAGS, dir_fd=parent
                )
                try:
                    digest, size = self._digest_fd(descriptor, MAX_HASH_BYTES)
                finally:
                    os.close(descriptor)
                return {
                    "path": relative_path,
                    "exists": True,
                    "type": "file",
                    "size": size,
                    "sha256": digest,
                }
        except (FileNotFoundError, NotADirectoryError):
            return {"path": relative_path, "exists": False}

    def _write_revision(self, params: dict) -> dict:
        """Compare-and-swap text write inside the workspace lock.

        Identical content is ``unchanged`` (idempotent retry). Otherwise
        ``expect_absent`` / ``expected_sha256`` must match the current file or
        :class:`RevisionConflictError` is raised and nothing is written.
        """
        relative_path = params.get("path", "")
        content = params.get("content", "")
        if not isinstance(content, str) or len(content.encode()) > MAX_READ_BYTES:
            raise FilesPolicyError("Content exceeds the write limit")
        expected = params.get("expected_sha256")
        if expected is not None and (
            not isinstance(expected, str) or not _SKILL_SHA256.fullmatch(expected)
        ):
            raise FilesPolicyError("expected_sha256 must be a lowercase SHA-256")
        expect_absent = params.get("expect_absent", False) is True
        data = content.encode()
        new_sha = hashlib.sha256(data).hexdigest()
        parts = self._parts(relative_path)
        current_sha = None
        exists = False
        # The preconditions look the parent up WITHOUT creating it: a refused
        # write must leave nothing behind (B3-bugs-02). A missing parent means
        # the file is absent; ``_replace`` creates parents once every
        # precondition has passed. A file in a parent position keeps its
        # NotADirectoryError outcome.
        try:
            with self._directory(parts[:-1]) as parent:
                try:
                    existing = os.open(
                        parts[-1], os.O_RDONLY | _FILE_FLAGS, dir_fd=parent
                    )
                except FileNotFoundError:
                    pass
                else:
                    exists = True
                    try:
                        if stat.S_ISDIR(os.fstat(existing).st_mode) and (
                            expect_absent or expected is not None
                        ):
                            # A folder occupies the path: an exclusive create
                            # or a revision-checked save conflicts with it
                            # (409), exactly as the read-back path reports —
                            # not the generic "only regular files" policy
                            # error (400).
                            raise RevisionConflictError(None)
                        current_sha, _size = self._digest_fd(
                            existing, MAX_HASH_BYTES
                        )
                    finally:
                        os.close(existing)
        except FileNotFoundError:
            pass
        if exists and current_sha == new_sha:
            return {
                "path": relative_path,
                "outcome": "unchanged",
                "sha256": new_sha,
                "previous_sha256": current_sha,
                "size": len(data),
            }
        if expect_absent and exists:
            raise RevisionConflictError(current_sha)
        if expected is not None and (not exists or current_sha != expected):
            raise RevisionConflictError(current_sha)
        self._replace(relative_path, data)
        return {
            "path": relative_path,
            "outcome": "written",
            "sha256": new_sha,
            "previous_sha256": current_sha,
            "size": len(data),
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
        """Create a folder; ``created`` says whether this call made it.

        A real folder already at the path is success with ``created: False``:
        a retry after a lost answer finds the folder its first attempt made,
        and must not report failure. A file, or a link planted in the
        folder's place, is refused and left untouched.
        """
        relative_path = params.get("path", "")
        parts = self._parts(relative_path)
        with self._directory(parts[:-1], create=True) as parent:
            try:
                os.mkdir(parts[-1], mode=0o755, dir_fd=parent)
            except FileExistsError:
                try:
                    existing = os.open(parts[-1], _DIRECTORY_FLAGS, dir_fd=parent)
                except OSError as error:
                    if error.errno not in (errno.ENOTDIR, errno.ELOOP):
                        raise
                    raise FilesPolicyError(
                        "A file already exists at this path; no folder was created"
                    ) from error
                try:
                    self._validate_fd(existing, directory=True)
                finally:
                    os.close(existing)
                return {"path": relative_path, "created": False}
        return {"path": relative_path, "created": True}

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
        """List skill folders with bounded raw SKILL.md heads.

        This helper never parses YAML (it runs without site-packages). Each
        skill carries ``skill_md_head`` (≤32 KiB, text), ``skill_md_head_truncated``
        and ``skill_md_size``; the daemon host parses them with the shared
        skill-metadata contract. An oversized SKILL.md is reported with a
        truncated head instead of failing the whole listing. The listing's head
        budget is spent in JSON-escaped bytes: the head that crosses it is cut
        to fit and every later head is ``None`` (the daemon reports those skills
        ``not_evaluated``) — never a 413 for the whole office. ``display_name``
        / ``description`` keep their legacy keys with neutral values.
        """
        del params
        self._skill_recover_before_access()
        try:
            with self._directory((".claude", "skills")) as descriptor:
                names = []
                for name in self._skill_scan_names(descriptor, [0]):
                    try:
                        metadata = os.stat(
                            name, dir_fd=descriptor, follow_symlinks=False
                        )
                    except FileNotFoundError:
                        continue  # removed after it was listed: gone
                    if stat.S_ISDIR(metadata.st_mode):
                        names.append(name)
        except FileNotFoundError:  # no skills folder at all
            return {"skills": []}
        discovered = []
        budget = [SKILL_HEADS_TOTAL_BYTES]
        for name in sorted(names):
            # Each folder gets its own entry budget (RR-BND-3): one huge
            # folder (an ``npm install`` inside a skill) degrades to an
            # entry without a file list instead of failing discovery for
            # every skill. The helper deadline stays shared and fatal.
            saved = (self.entries, self.entry_limit)
            heads_left = budget[0]
            self.entries, self.entry_limit = 0, MAX_ENTRIES
            try:
                entry = self._skill_discovery_entry(name, budget)
            except EntryLimitError as error:
                # Only size degrades the whole folder; unsupported entries
                # are skipped inside it (see ``_skill_discovery_entry``).
                self.entries = 0
                budget[0] = heads_left  # its head is read once, below
                entry = self._skill_discovery_fallback(name, budget, error)
            finally:
                self.entries, self.entry_limit = saved
            if entry is not None:
                discovered.append(entry)
        # A root replacement or the deadline cannot pass as a shorter list.
        self._tick()
        return {"skills": discovered}

    def _skill_scan_names(self, directory: int, skipped: list[int]) -> list[str]:
        """Names in ``directory`` that Files can address, ticking the budget.

        A name Files cannot represent (control characters, a backslash, a
        drive prefix, an over-long component) is skipped and counted in
        ``skipped[0]`` instead of failing the listing.
        """
        names = []
        with os.scandir(directory) as entries:
            for entry in entries:
                self._tick(1)
                try:
                    if "\\" in entry.name:
                        raise FilesPolicyError("Backslashes are not addressable")
                    path_parts(entry.name)
                except (FilesPolicyError, UnicodeEncodeError):
                    # UnicodeEncodeError: a surrogate-escaped non-UTF-8 name.
                    skipped[0] += 1
                    continue
                names.append(entry.name)
        return sorted(names, key=str.casefold)

    def _skill_head_of(
        self, descriptor: int, size: int, budget: list[int]
    ) -> tuple[str | None, bool]:
        """``(head, truncated)`` of an open, validated SKILL.md."""
        if budget[0] <= 0:
            return None, True
        raw = os.pread(descriptor, SKILL_MD_HEAD_BYTES, 0)
        self._validate_fd(descriptor, directory=False)
        return self._skill_fit_head(raw, size, budget)

    @staticmethod
    def _skill_fit_head(
        raw: bytes, size: int, budget: list[int]
    ) -> tuple[str | None, bool]:
        text = raw.decode("utf-8", errors="replace")
        if size > len(raw) and text.endswith("\ufffd"):
            # A multi-byte character cut at the boundary.
            text = text[:-1]
        fitted = _fit_escaped(text, budget[0])
        cut = len(fitted) < len(text)
        if cut:
            budget[0] = 0  # later heads: not evaluated
        else:
            budget[0] -= _escaped_size(fitted)
        if fitted or not cut:  # an empty SKILL.md is ""
            return fitted, size > len(raw) or cut
        return None, True

    def _skill_discovery_open(self, directory: int, name: str, flags: int):
        """``os.open`` for the discovery walk: ``None`` when unsupported.

        A link (``O_NOFOLLOW``), an entry Files cannot read and anything the
        mount/type/link-count check refuses is not served, so it is skipped;
        the caller counts it. ``FileNotFoundError`` (the entry vanished after
        it was listed) propagates: the caller skips it without counting.
        """
        try:
            descriptor = os.open(name, flags, dir_fd=directory)
        except OSError as error:
            if isinstance(error, (TimeoutError, FileNotFoundError)):
                raise
            if error.errno not in _SKILL_SKIPPABLE_ERRNOS:
                raise
            return None
        try:
            self._validate_fd(
                descriptor, directory=bool(flags & getattr(os, "O_DIRECTORY", 0))
            )
        except UnsupportedEntryError:
            os.close(descriptor)
            return None
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _skill_discovery_entry(self, name: str, budget: list[int]) -> dict | None:
        """One skill folder's discovery entry (its files, head and bundle).

        The walk serves what Files serves and skips the rest, like
        ``fs_tree``: links, hard links, special files, mounted folders,
        names Files cannot address and folders deeper than the walk limit
        are counted in ``skipped_entries`` — an ``npm install`` or a
        virtualenv inside a skill never fails discovery for the office.
        Only the entry budget (a folder too large to list, see
        :meth:`_skill_discovery_fallback`), the shared deadline and a
        workspace root change end the walk. ``None``: the folder is no
        longer a folder Files serves (replaced by a link, or a mount).
        """
        files: list[dict] = []
        folders: set[str] = set()
        state = {
            "head": None,
            "head_truncated": False,
            "skill_md_size": None,
            "bundle": None,
        }
        skipped = [0]
        if not protected_path((".claude", "skills", name)):
            try:
                with self._skills_root() as skills:
                    folder = self._skill_discovery_open(skills, name, _DIRECTORY_FLAGS)
            except FileNotFoundError:
                return None  # removed after it was listed: gone
            if folder is None:
                return None
            try:
                self._skill_discovery_visit(
                    folder, name, (), files, folders, state, skipped, budget
                )
            finally:
                os.close(folder)
        files.extend(
            {"name": folder_name, "size": 0, "type": "folder", "is_skill_md": False}
            for folder_name in sorted(folders)
        )
        entry = {
            "name": name,
            "display_name": name,
            "description": "",
            "files": files,
            "has_skill_md": any(item["is_skill_md"] for item in files),
            "skill_md_head": state["head"],
            "skill_md_head_truncated": state["head_truncated"],
            "skill_md_size": state["skill_md_size"],
            "bundle": state["bundle"],
        }
        if skipped[0]:
            entry["skipped_entries"] = skipped[0]
        return entry

    def _skill_discovery_visit(
        self,
        directory: int,
        name: str,
        parts: tuple[str, ...],
        files: list[dict],
        folders: set[str],
        state: dict,
        skipped: list[int],
        budget: list[int],
    ) -> None:
        for child in self._skill_scan_names(directory, skipped):
            child_parts = (*parts, child)
            full = (".claude", "skills", name, *child_parts)
            if protected_path(full):
                continue
            if not parts and child == SKILL_BUNDLE_MANIFEST:
                # The platform manifest is not a skill file (F03).
                state["bundle"] = self._skill_bundle_identity(name)
                continue
            try:
                metadata = os.stat(child, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue  # vanished after it was listed: not an entry any more
            except OSError as error:
                if isinstance(error, TimeoutError):
                    raise
                if error.errno not in _SKILL_SKIPPABLE_ERRNOS:
                    raise
                skipped[0] += 1
                continue
            if stat.S_ISDIR(metadata.st_mode):
                if len(full) > MAX_DEPTH:
                    skipped[0] += 1
                    continue
                try:
                    child_fd = self._skill_discovery_open(
                        directory, child, _DIRECTORY_FLAGS
                    )
                except FileNotFoundError:
                    continue  # vanished after it was listed
                if child_fd is None:
                    skipped[0] += 1
                    continue
                try:
                    self._skill_discovery_visit(
                        child_fd,
                        name,
                        child_parts,
                        files,
                        folders,
                        state,
                        skipped,
                        budget,
                    )
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                skipped[0] += 1
                continue
            try:
                file_fd = self._skill_discovery_open(
                    directory, child, os.O_RDONLY | _FILE_FLAGS
                )
            except FileNotFoundError:
                continue  # vanished after it was listed
            if file_fd is None:
                skipped[0] += 1
                continue
            try:
                checked = os.fstat(file_fd)
                relative = "/".join(child_parts)
                is_skill = relative == "SKILL.md"
                files.append(
                    {
                        "name": relative,
                        "size": checked.st_size,
                        "type": "file",
                        "is_skill_md": is_skill,
                    }
                )
                for depth in range(1, len(child_parts)):
                    folders.add("/".join(child_parts[:depth]))
                if is_skill:
                    state["skill_md_size"] = checked.st_size
                    try:
                        state["head"], state["head_truncated"] = self._skill_head_of(
                            file_fd, checked.st_size, budget
                        )
                    except UnsupportedEntryError:  # replaced while read
                        state["head"], state["head_truncated"] = None, True
            finally:
                os.close(file_fd)

    def _skill_discovery_fallback(
        self, name: str, budget: list[int], error: Exception
    ) -> dict | None:
        """A folder too large to list: no file list, still usable.

        ``has_skill_md`` comes from a direct check of the root SKILL.md (a
        regular single-link file), and its head and the bundle identity are
        still reported, so the backend's install state and the page keep
        working for this and every other skill. ``None``: the folder is no
        longer one Files serves.
        """
        head = None
        head_truncated = False
        skill_md_size = None
        has_skill_md = False
        try:
            with self._skills_root() as skills:
                folder = self._skill_discovery_open(skills, name, _DIRECTORY_FLAGS)
        except FileNotFoundError:
            return None  # removed while listing: gone
        if folder is None:
            return None
        try:
            skill_md = self._skill_discovery_open(
                folder, "SKILL.md", os.O_RDONLY | _FILE_FLAGS
            )
        except FileNotFoundError:
            skill_md = None
        finally:
            os.close(folder)
        if skill_md is not None:
            try:
                has_skill_md = True
                skill_md_size = os.fstat(skill_md).st_size
                head, head_truncated = self._skill_head_of(
                    skill_md, skill_md_size, budget
                )
            except UnsupportedEntryError:
                head, head_truncated = None, True
            finally:
                os.close(skill_md)
        return {
            "name": name,
            "display_name": name,
            "description": "",
            "files": [],
            "has_skill_md": has_skill_md,
            "skill_md_head": head,
            "skill_md_head_truncated": head_truncated,
            "skill_md_size": skill_md_size,
            "bundle": self._skill_bundle_identity(name),
            "listing_truncated": True,
            "listing_error": str(error)[:300],
        }

    def _skill_bundle_identity(self, name: str) -> dict | None:
        """Published bundle identity of a live skill folder (``None``: none)."""
        try:
            with self._skills_root() as skills:
                descriptor = self._skill_open_dir(skills, name)
                try:
                    manifest = self._skill_read_manifest(descriptor, name)
                finally:
                    os.close(descriptor)
        except (OSError, FilesPolicyError):
            return None
        if manifest is None:
            return None
        return {  # key order: SKILL_BUNDLE_IDENTITY_KEYS (pinned by tests)
            "bundle_sha256": manifest["bundle_sha256"],
            "publication_id": manifest["publication_id"],
            "published_at": manifest["published_at"],
            "source_kind": manifest["source"].get("kind"),
            "source_revision": manifest["source"].get("revision"),
        }

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
            "fs_hash": self._hash,
            "fs_write_revision": self._write_revision,
            "fs_skill_status": self._skill_status,
            "fs_skill_stage_begin": self._skill_stage_begin,
            "fs_skill_stage_put": self._skill_stage_put,
            "fs_skill_commit": self._skill_commit,
            "fs_skill_abort": self._skill_abort,
            "fs_skill_retire": self._skill_retire,
        }
        if action not in handlers or not isinstance(params, dict):
            raise FilesPolicyError("Unknown or invalid filesystem request")
        if action in _SKILLS_ROOT_WRITES:
            targets = _skills_root_targets(params)
            if targets:
                self._skill_recover_before_access()
                for parts in targets:
                    if len(parts) > 2:
                        self._skill_refuse_if_stuck(parts[2])
        return handlers[action](params)


_SKILLS_ROOT_WRITES = frozenset(
    {
        "fs_write",
        "fs_write_revision",
        "fs_mkdir",
        "fs_rename",
        "fs_delete",
        "fs_upload_chunk",
    }
)


def _skills_root_targets(params: dict) -> list[tuple[str, ...]]:
    """The paths under ``.claude/skills`` a Files write names, as parts."""
    targets = []
    for key in ("path", "old_path", "new_path"):
        value = params.get(key)
        if not isinstance(value, str):
            continue
        try:
            parts = path_parts(value)
        except FilesPolicyError:
            continue
        if [part.casefold() for part in parts[:2]] == [".claude", "skills"]:
            targets.append(parts)
    return targets


def execute(request: dict, root: str | Path = "/workspace") -> dict:
    try:
        with SecureWorkspace(root) as workspace:
            return workspace.dispatch(
                request.get("action", ""), request.get("params", {})
            )
    except SkillCommitUncertainError as error:
        return {"error": str(error), "status": 500, "code": "skill_commit_uncertain"}
    except SkillPublicationRefusedError as error:
        return {"error": str(error), "status": 400, "code": "skill_publication_refused"}
    except SkillBundleConflictError as conflict:
        result = {"error": str(conflict), "status": 409, "code": conflict.code}
        if conflict.current_digest is not None:
            result["current_digest"] = conflict.current_digest
        return result
    except RevisionConflictError as conflict:
        return {
            "error": "The file changed since the expected revision; nothing was written",
            "status": 409,
            "code": "revision_conflict",
            "current_sha256": conflict.current_sha256,
        }
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
