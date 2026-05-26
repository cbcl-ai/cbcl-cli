"""Tests for fs_handler — classify/read/stat/download_chunk additions.

Focused on the image + PDF preview path and the streaming-download
foundation that the backend's /fs/raw endpoint depends on.
"""
from __future__ import annotations

import base64
import json

import pytest

from src.fs_handler import FsHandler, _classify_file


# ---------------------------------------------------------------------------
# _classify_file
# ---------------------------------------------------------------------------


class TestClassifyFile:
    """The classifier drives the frontend's preview renderer choice."""

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("notes.md", "text"),
            ("script.py", "text"),
            ("readme.txt", "text"),
            ("picture.png", "image"),
            ("photo.JPG", "image"),
            ("icon.svg", "image"),
            ("doc.pdf", "pdf"),
            ("DOC.PDF", "pdf"),
            ("archive.zip", "binary"),
            ("unknown.xyz", "binary"),
        ],
    )
    def test_by_extension(self, tmp_path, name, expected):
        (tmp_path / name).write_bytes(b"x")
        assert _classify_file(tmp_path / name) == expected

    def test_by_mime_type_when_extension_misleading(self, tmp_path, monkeypatch):
        # A `.bin` file that the OS guesses as image/png should still
        # classify as image — prevents misclassification of files with
        # stripped extensions.
        import mimetypes
        monkeypatch.setattr(
            mimetypes, "guess_type", lambda _: ("image/png", None),
        )
        (tmp_path / "weird.bin").write_bytes(b"x")
        assert _classify_file(tmp_path / "weird.bin") == "image"


# ---------------------------------------------------------------------------
# _read: text content vs non-text metadata
# ---------------------------------------------------------------------------


class TestRead:
    """Text files carry their content in the response; images / PDFs
    return only metadata so the UI fetches bytes via the streaming
    /fs/raw endpoint instead of a base64 JSON round-trip."""

    @pytest.mark.asyncio
    async def test_text_file_returns_content(self, tmp_path):
        (tmp_path / "hello.md").write_text("# hi")
        handler = FsHandler(str(tmp_path))
        result = handler._read({"path": "hello.md"})
        assert result["file_kind"] == "text"
        assert result["content"] == "# hi"
        assert result["size"] == 4

    @pytest.mark.asyncio
    async def test_image_file_omits_content(self, tmp_path):
        # A fake 1 KB PNG — content shouldn't be base64'd into the
        # response; the frontend will GET /fs/raw for the bytes.
        blob = b"\x89PNG\r\n\x1a\n" + b"\x00" * 1000
        (tmp_path / "pic.png").write_bytes(blob)
        handler = FsHandler(str(tmp_path))
        result = handler._read({"path": "pic.png"})
        assert result["file_kind"] == "image"
        assert result["content"] is None
        assert result["size"] == len(blob)

    @pytest.mark.asyncio
    async def test_pdf_file_omits_content(self, tmp_path):
        (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.5" + b"\x00" * 500)
        handler = FsHandler(str(tmp_path))
        result = handler._read({"path": "doc.pdf"})
        assert result["file_kind"] == "pdf"
        assert result["content"] is None


# ---------------------------------------------------------------------------
# _stat: cheap metadata probe
# ---------------------------------------------------------------------------


class TestStat:

    def test_returns_size_mime_kind(self, tmp_path):
        path = tmp_path / "doc.pdf"
        path.write_bytes(b"%PDF-1.5" + b"x" * 100)
        handler = FsHandler(str(tmp_path))
        result = handler._stat({"path": "doc.pdf"})
        assert result["size"] == 108
        assert result["mime_type"] == "application/pdf"
        assert result["file_kind"] == "pdf"
        assert "modified" in result

    def test_missing_file_raises(self, tmp_path):
        handler = FsHandler(str(tmp_path))
        with pytest.raises(FileNotFoundError):
            handler._stat({"path": "nope.bin"})

    def test_directory_raises(self, tmp_path):
        (tmp_path / "subdir").mkdir()
        handler = FsHandler(str(tmp_path))
        with pytest.raises(FileNotFoundError):
            handler._stat({"path": "subdir"})


# ---------------------------------------------------------------------------
# _download_chunk: range reads with validation + EOF
# ---------------------------------------------------------------------------


class TestDownloadChunk:

    def test_happy_path_first_chunk(self, tmp_path):
        payload = b"0123456789" * 1000  # 10 KB
        (tmp_path / "big.bin").write_bytes(payload)
        handler = FsHandler(str(tmp_path))

        result = handler._download_chunk({
            "path": "big.bin", "offset": 0, "length": 4000,
        })
        assert result["offset"] == 0
        assert result["chunk_size"] == 4000
        assert result["total_size"] == 10000
        assert result["eof"] is False
        got = base64.b64decode(result["chunk_base64"])
        assert got == payload[:4000]

    def test_middle_chunk(self, tmp_path):
        payload = b"ABCDEFGHIJ" * 100  # 1 KB
        (tmp_path / "mid.bin").write_bytes(payload)
        handler = FsHandler(str(tmp_path))

        result = handler._download_chunk({
            "path": "mid.bin", "offset": 300, "length": 200,
        })
        got = base64.b64decode(result["chunk_base64"])
        assert got == payload[300:500]
        assert result["eof"] is False

    def test_last_chunk_sets_eof(self, tmp_path):
        payload = b"." * 2500
        (tmp_path / "x.bin").write_bytes(payload)
        handler = FsHandler(str(tmp_path))

        result = handler._download_chunk({
            "path": "x.bin", "offset": 2400, "length": 1000,
        })
        assert result["chunk_size"] == 100  # only 100 bytes left
        assert result["eof"] is True

    def test_past_eof_returns_empty_terminator(self, tmp_path):
        (tmp_path / "tiny.bin").write_bytes(b"short")
        handler = FsHandler(str(tmp_path))
        result = handler._download_chunk({
            "path": "tiny.bin", "offset": 100, "length": 1000,
        })
        assert result["chunk_size"] == 0
        assert result["chunk_base64"] == ""
        assert result["eof"] is True

    def test_negative_offset_rejected(self, tmp_path):
        (tmp_path / "a.bin").write_bytes(b"x")
        handler = FsHandler(str(tmp_path))
        with pytest.raises(ValueError):
            handler._download_chunk({
                "path": "a.bin", "offset": -1, "length": 100,
            })

    def test_zero_length_rejected(self, tmp_path):
        (tmp_path / "a.bin").write_bytes(b"x")
        handler = FsHandler(str(tmp_path))
        with pytest.raises(ValueError):
            handler._download_chunk({
                "path": "a.bin", "offset": 0, "length": 0,
            })

    def test_oversized_length_rejected(self, tmp_path):
        # Cap is 4 MB; asking for 100 MB must be rejected so a
        # misbehaving caller can't balloon memory.
        (tmp_path / "a.bin").write_bytes(b"x")
        handler = FsHandler(str(tmp_path))
        with pytest.raises(ValueError):
            handler._download_chunk({
                "path": "a.bin", "offset": 0, "length": 100 * 1024 * 1024,
            })

    def test_missing_file_raises(self, tmp_path):
        handler = FsHandler(str(tmp_path))
        with pytest.raises(FileNotFoundError):
            handler._download_chunk({
                "path": "nope.bin", "offset": 0, "length": 100,
            })


# ---------------------------------------------------------------------------
# Chunked reassembly equivalence — the contract the backend relies on
# ---------------------------------------------------------------------------


class TestUploadChunk:
    """Upload is the mirror of download — the pair of actions must
    round-trip any file type byte-for-byte, reject corrupted chunks,
    and refuse unsafe inputs (oversized chunks, bad offsets,
    non-base64 payloads)."""

    def test_first_chunk_creates_file_and_truncates(self, tmp_path):
        # Pre-seed a file so offset=0 MUST truncate (not prepend).
        (tmp_path / "existing.bin").write_bytes(b"OLD CONTENT SHOULD GO AWAY")
        handler = FsHandler(str(tmp_path))
        result = handler._upload_chunk({
            "path": "existing.bin",
            "offset": 0,
            "chunk_base64": base64.b64encode(b"fresh").decode(),
            "done": True,
        })
        assert result["bytes_written"] == 5
        assert result["total_size"] == 5
        assert (tmp_path / "existing.bin").read_bytes() == b"fresh"

    def test_creates_parent_directories(self, tmp_path):
        handler = FsHandler(str(tmp_path))
        handler._upload_chunk({
            "path": "deep/nested/folder/file.bin",
            "offset": 0,
            "chunk_base64": base64.b64encode(b"hello").decode(),
        })
        assert (tmp_path / "deep/nested/folder/file.bin").read_bytes() == b"hello"

    def test_append_chunks_build_file(self, tmp_path):
        handler = FsHandler(str(tmp_path))
        chunks = [b"AAAA", b"BBBB", b"CCCC"]
        offset = 0
        for chunk in chunks:
            handler._upload_chunk({
                "path": "built.bin",
                "offset": offset,
                "chunk_base64": base64.b64encode(chunk).decode(),
            })
            offset += len(chunk)
        assert (tmp_path / "built.bin").read_bytes() == b"AAAABBBBCCCC"

    def test_offset_mismatch_rejected(self, tmp_path):
        """A reordered or dropped chunk MUST fail loudly rather than
        silently overwriting middle bytes."""
        handler = FsHandler(str(tmp_path))
        handler._upload_chunk({
            "path": "a.bin",
            "offset": 0,
            "chunk_base64": base64.b64encode(b"AAAA").decode(),
        })
        with pytest.raises(ValueError, match="offset mismatch"):
            handler._upload_chunk({
                "path": "a.bin",
                "offset": 100,  # file is 4 bytes, not 100.
                "chunk_base64": base64.b64encode(b"XXXX").decode(),
            })

    def test_negative_offset_rejected(self, tmp_path):
        handler = FsHandler(str(tmp_path))
        with pytest.raises(ValueError):
            handler._upload_chunk({
                "path": "a.bin",
                "offset": -1,
                "chunk_base64": base64.b64encode(b"x").decode(),
            })

    def test_bad_base64_rejected(self, tmp_path):
        handler = FsHandler(str(tmp_path))
        with pytest.raises(ValueError, match="not valid base64"):
            handler._upload_chunk({
                "path": "a.bin",
                "offset": 0,
                "chunk_base64": "!!!not base64!!!",
            })

    def test_oversized_chunk_rejected(self, tmp_path):
        # Chunks larger than 4 MB must be rejected so a malicious
        # caller can't blow up memory with a single frame.
        handler = FsHandler(str(tmp_path))
        big = b"\x00" * (5 * 1024 * 1024)
        with pytest.raises(ValueError, match="chunk too large"):
            handler._upload_chunk({
                "path": "a.bin",
                "offset": 0,
                "chunk_base64": base64.b64encode(big).decode(),
            })

    def test_empty_chunk_with_done_is_valid_terminator(self, tmp_path):
        # Backend sends a final empty chunk with done=True to signal
        # the stream finished cleanly. Must not raise.
        handler = FsHandler(str(tmp_path))
        handler._upload_chunk({
            "path": "a.bin",
            "offset": 0,
            "chunk_base64": base64.b64encode(b"hi").decode(),
        })
        # Final terminator — empty payload at current offset.
        result = handler._upload_chunk({
            "path": "a.bin",
            "offset": 2,
            "chunk_base64": "",
            "done": True,
        })
        assert result["done"] is True
        assert result["total_size"] == 2

    def test_accepts_any_file_type(self, tmp_path):
        # No extension / MIME filter — upload must accept archives,
        # binaries, whatever the user selects in the UI.
        handler = FsHandler(str(tmp_path))
        # Fake ZIP magic bytes just to prove arbitrary binary input
        # flows through unchanged.
        payload = b"PK\x03\x04" + bytes(range(250))
        handler._upload_chunk({
            "path": "archive.zip",
            "offset": 0,
            "chunk_base64": base64.b64encode(payload).decode(),
            "done": True,
        })
        assert (tmp_path / "archive.zip").read_bytes() == payload

    def test_extensionless_filename_in_nested_path(self, tmp_path):
        # Regression: when uploading a whole folder containing a
        # Makefile/Dockerfile the backend assembles the target path
        # using webkitRelativePath. The handler must land the file at
        # the exact path requested — no extension-based mangling.
        handler = FsHandler(str(tmp_path))
        payload = b"all: build\n\tgo build ./...\n"
        target = "inbox/sample-project/Makefile"
        handler._upload_chunk({
            "path": target,
            "offset": 0,
            "chunk_base64": base64.b64encode(payload).decode(),
            "done": True,
        })
        landed = tmp_path / target
        assert landed.read_bytes() == payload
        # And critically, NOT landed at the doubled-segment path
        # that the old buggy heuristic would produce.
        duplicated = tmp_path / "inbox/sample-project/Makefile/Makefile"
        assert not duplicated.exists()

    @pytest.mark.asyncio
    async def test_dispatch_via_handle_request(self, tmp_path):
        # Use pytest-asyncio rather than asyncio.run() — the latter
        # closes the default loop, which contaminates later tests in
        # the same pytest session that rely on their own loop policy.
        handler = FsHandler(str(tmp_path))
        sent: list[dict] = []

        async def _send(msg):
            sent.append(msg)

        await handler.handle_request(
            {
                "request_id": "up-1",
                "action": "fs_upload_chunk",
                "params": {
                    "path": "dispatched.bin",
                    "offset": 0,
                    "chunk_base64": base64.b64encode(b"ok").decode(),
                    "done": True,
                },
            },
            _send,
        )
        assert sent[0]["data"]["total_size"] == 2
        assert (tmp_path / "dispatched.bin").read_bytes() == b"ok"


class TestChunkedReassembly:
    """A streaming download is correct iff concatenating all chunks
    reproduces the file byte-for-byte. Lock that invariant here so a
    future chunk-size / offset change can't silently corrupt files."""

    def test_empty_file_stat(self, tmp_path):
        """stat on a 0-byte file returns size=0 without raising."""
        (tmp_path / "empty.bin").write_bytes(b"")
        handler = FsHandler(str(tmp_path))
        result = handler._stat({"path": "empty.bin"})
        assert result["size"] == 0
        # Still classifiable — extension-based, not content-based.
        assert result["file_kind"] == "binary"

    def test_empty_file_chunk_returns_eof_immediately(self, tmp_path):
        """First chunk of a 0-byte file must terminate the stream
        cleanly (empty chunk + eof=True) so the backend's streaming
        generator doesn't spin."""
        (tmp_path / "empty.bin").write_bytes(b"")
        handler = FsHandler(str(tmp_path))
        result = handler._download_chunk({
            "path": "empty.bin", "offset": 0, "length": 1024,
        })
        assert result["chunk_size"] == 0
        assert result["chunk_base64"] == ""
        assert result["total_size"] == 0
        assert result["eof"] is True

    def test_upload_then_download_roundtrip(self, tmp_path):
        """Reassembled upload + chunked download must reproduce the
        original bytes. Locks the full round-trip contract the /fs
        endpoints rely on."""
        handler = FsHandler(str(tmp_path))
        payload = bytes(range(256)) * 40  # 10.24 KB of varied bytes
        chunk_size = 1024

        # Upload in chunks.
        for offset in range(0, len(payload), chunk_size):
            slice_bytes = payload[offset : offset + chunk_size]
            result = handler._upload_chunk({
                "path": "roundtrip.bin",
                "offset": offset,
                "chunk_base64": base64.b64encode(slice_bytes).decode(),
                "done": offset + chunk_size >= len(payload),
            })
            assert result["bytes_written"] == len(slice_bytes)

        # Disk check.
        on_disk = (tmp_path / "roundtrip.bin").read_bytes()
        assert on_disk == payload

        # Download via chunked reader.
        assembled = bytearray()
        offset = 0
        for _ in range(100):
            chunk = handler._download_chunk({
                "path": "roundtrip.bin",
                "offset": offset,
                "length": chunk_size,
            })
            assembled.extend(base64.b64decode(chunk["chunk_base64"]))
            offset += chunk["chunk_size"]
            if chunk["eof"]:
                break
        assert bytes(assembled) == payload

    def test_reassemble_matches_file(self, tmp_path):
        payload = bytes(range(256)) * 50  # 12.8 KB of varied bytes
        (tmp_path / "whole.bin").write_bytes(payload)
        handler = FsHandler(str(tmp_path))

        chunk_size = 1024
        offset = 0
        assembled = bytearray()
        # Safety cap so a bug can't loop forever.
        for _ in range(1000):
            result = handler._download_chunk({
                "path": "whole.bin",
                "offset": offset,
                "length": chunk_size,
            })
            assembled.extend(base64.b64decode(result["chunk_base64"]))
            offset += result["chunk_size"]
            if result["eof"]:
                break
        assert bytes(assembled) == payload


# ---------------------------------------------------------------------------
# Dispatch wiring — the new actions land at their handlers
# ---------------------------------------------------------------------------


class TestDispatch:

    @pytest.mark.asyncio
    async def test_fs_stat_via_handle_request(self, tmp_path):
        (tmp_path / "a.txt").write_text("hi")
        handler = FsHandler(str(tmp_path))

        sent: list[dict] = []

        async def _send(msg):
            sent.append(msg)

        await handler.handle_request(
            {"request_id": "r1", "action": "fs_stat", "params": {"path": "a.txt"}},
            _send,
        )
        assert len(sent) == 1
        assert sent[0]["request_id"] == "r1"
        assert sent[0]["data"]["size"] == 2

    @pytest.mark.asyncio
    async def test_fs_download_chunk_via_handle_request(self, tmp_path):
        (tmp_path / "a.bin").write_bytes(b"hello world")
        handler = FsHandler(str(tmp_path))
        sent: list[dict] = []

        async def _send(msg):
            sent.append(msg)

        await handler.handle_request(
            {
                "request_id": "r2",
                "action": "fs_download_chunk",
                "params": {"path": "a.bin", "offset": 6, "length": 5},
            },
            _send,
        )
        data = sent[0]["data"]
        assert base64.b64decode(data["chunk_base64"]) == b"world"
        assert data["eof"] is True

    @pytest.mark.asyncio
    async def test_unknown_action_returns_400(self, tmp_path):
        handler = FsHandler(str(tmp_path))
        sent: list[dict] = []

        async def _send(msg):
            sent.append(msg)

        await handler.handle_request(
            {"request_id": "r3", "action": "fs_bogus", "params": {}},
            _send,
        )
        assert sent[0]["data"]["status"] == 400


# ---------------------------------------------------------------------------
# fs_tree subfolder scoping — Scripts mini-IDE feature
# ---------------------------------------------------------------------------


class TestTreeSubfolder:
    """The Scripts mini-IDE needs to render a tree of a single script
    folder without loading thousands of unrelated workspace files.
    The subfolder param scopes the walk but keeps paths workspace-
    relative so other /fs endpoints accept them unchanged.
    """

    def test_empty_subfolder_returns_full_workspace(self, tmp_path):
        # Backwards-compat: calling with no subfolder is identical to
        # the legacy full-tree behaviour.
        (tmp_path / "top.txt").write_text("t")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "nested.txt").write_text("n")
        handler = FsHandler(str(tmp_path))

        tree = handler._tree({})
        names = {c["name"] for c in tree["children"]}
        assert names == {"top.txt", "sub"}

    def test_subfolder_scopes_to_subtree(self, tmp_path):
        # Only the requested subfolder's children appear; top-level
        # siblings are NOT in the response.
        (tmp_path / "outside.txt").write_text("x")
        (tmp_path / ".scripts").mkdir()
        scripts_dir = tmp_path / ".scripts" / "my-script"
        scripts_dir.mkdir()
        (scripts_dir / "main.py").write_text("# entry")
        (scripts_dir / "lib").mkdir()
        (scripts_dir / "lib" / "util.py").write_text("# util")

        handler = FsHandler(str(tmp_path))
        tree = handler._tree({"subfolder": ".scripts/my-script"})

        # Top-level siblings (outside.txt, .scripts itself) are absent.
        names = {c["name"] for c in tree["children"]}
        assert names == {"main.py", "lib"}

    def test_subfolder_paths_remain_workspace_relative(self, tmp_path):
        # Critical invariant: node.path is workspace-rooted so the
        # frontend can pass it unchanged to fs/read, fs/write, fs/raw.
        # If we returned subfolder-relative paths these endpoints
        # would 404.
        script_dir = tmp_path / ".scripts" / "my-script"
        script_dir.mkdir(parents=True)
        (script_dir / "main.py").write_text("# entry")

        handler = FsHandler(str(tmp_path))
        tree = handler._tree({"subfolder": ".scripts/my-script"})

        main = next(c for c in tree["children"] if c["name"] == "main.py")
        assert main["path"] == ".scripts/my-script/main.py"

    def test_subfolder_missing_raises_file_not_found(self, tmp_path):
        # A stale UI reference to a deleted script folder should get a
        # clean 404 through handle_request, not a 500.
        handler = FsHandler(str(tmp_path))
        with pytest.raises(FileNotFoundError):
            handler._tree({"subfolder": ".scripts/ghost"})

    def test_subfolder_pointing_at_file_raises(self, tmp_path):
        # Early reject — the walker assumes it's handed a directory.
        (tmp_path / "a.txt").write_text("x")
        handler = FsHandler(str(tmp_path))
        with pytest.raises(ValueError):
            handler._tree({"subfolder": "a.txt"})

    def test_subfolder_traversal_rejected(self, tmp_path):
        # _safe_resolve blocks `..`; double-check through _tree.
        handler = FsHandler(str(tmp_path))
        with pytest.raises(ValueError):
            handler._tree({"subfolder": "../outside"})

    @pytest.mark.asyncio
    async def test_fs_tree_subfolder_via_handle_request(self, tmp_path):
        # End-to-end through the dispatcher that the backend actually
        # invokes — proves the param name and missing-subfolder status
        # are wired correctly.
        script_dir = tmp_path / ".scripts" / "s1"
        script_dir.mkdir(parents=True)
        (script_dir / "main.py").write_text("")

        handler = FsHandler(str(tmp_path))
        sent: list[dict] = []

        async def _send(msg):
            sent.append(msg)

        await handler.handle_request(
            {
                "request_id": "tree-1",
                "action": "fs_tree",
                "params": {"subfolder": ".scripts/s1"},
            },
            _send,
        )
        data = sent[0]["data"]
        assert data["children"][0]["name"] == "main.py"
        assert data["children"][0]["path"] == ".scripts/s1/main.py"

        sent.clear()
        await handler.handle_request(
            {
                "request_id": "tree-2",
                "action": "fs_tree",
                "params": {"subfolder": ".scripts/ghost"},
            },
            _send,
        )
        assert sent[0]["data"]["status"] == 404


# ─── skills_discovered ─────────────────────────────────────────────


class TestSkillsDiscovered:
    """Regression coverage for the daemon-side skill scan.

    Added when the platform backend (cbcl-v2) was discovered to be
    reading its OWN empty ``~/.cubicle/workspaces/`` when looking
    for skill files — the actual files live on the daemon machine
    (cbcl-stg) because the daemon owns the workspace bind-mount.
    Backend now delegates here via ``request_bridge``.
    """

    @pytest.mark.asyncio
    async def test_empty_when_no_skills_dir(self, tmp_path):
        handler = FsHandler(str(tmp_path))
        result = handler._skills_discovered({})
        assert result == {"skills": []}

    @pytest.mark.asyncio
    async def test_returns_skill_metadata_from_frontmatter(self, tmp_path):
        skill_dir = tmp_path / ".claude" / "skills" / "perplexity"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            '---\nname: "Perplexity Search"\n'
            'description: "Web search via Perplexity API"\n---\n\n'
            "# Playbook"
        )
        handler = FsHandler(str(tmp_path))
        result = handler._skills_discovered({})
        skills = result["skills"]
        assert len(skills) == 1
        s = skills[0]
        assert s["name"] == "perplexity"
        assert s["display_name"] == "Perplexity Search"
        assert s["description"] == "Web search via Perplexity API"
        assert s["has_skill_md"] is True
        # Files list contains SKILL.md as is_skill_md=True
        skill_md_entry = next(
            f for f in s["files"] if f["name"] == "SKILL.md"
        )
        assert skill_md_entry["is_skill_md"] is True
        assert skill_md_entry["type"] == "file"
        assert skill_md_entry["size"] > 0

    @pytest.mark.asyncio
    async def test_nested_resources_included(self, tmp_path):
        skill_dir = tmp_path / ".claude" / "skills" / "demo"
        (skill_dir / "resources").mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\n")
        (skill_dir / "resources" / "template.md").write_text("hello")
        handler = FsHandler(str(tmp_path))
        result = handler._skills_discovered({})
        files = result["skills"][0]["files"]
        names = {f["name"] for f in files}
        assert "SKILL.md" in names
        # The nested file appears with its relative-from-skill path.
        assert "resources/template.md" in names
        # The folder itself is also surfaced as type=folder.
        folder = next(f for f in files if f["name"] == "resources")
        assert folder["type"] == "folder"

    @pytest.mark.asyncio
    async def test_skill_without_skill_md_still_surfaces(self, tmp_path):
        """A skill folder without SKILL.md should still appear so
        the UI can prompt the user to create one — silently dropping
        such folders was the original behaviour and made it look
        like the folder didn't exist."""
        skill_dir = tmp_path / ".claude" / "skills" / "draft"
        skill_dir.mkdir(parents=True)
        (skill_dir / "notes.md").write_text("wip")
        handler = FsHandler(str(tmp_path))
        result = handler._skills_discovered({})
        skills = result["skills"]
        assert len(skills) == 1
        assert skills[0]["name"] == "draft"
        assert skills[0]["has_skill_md"] is False
        assert skills[0]["display_name"] == "draft"  # falls back to dir name
